"""Escala dos grupos de importação: 300 perfis pequenos + um perfil "Todos" com 2.100 páginas falsas num grupo só.
Afirma memória plana durante a listagem longa e ausência de N+1 nas rotas de grupo. Offline, valores FAKE; `slow`.
"""

from __future__ import annotations

import gc
import logging
import tracemalloc

import pytest

from app.integrations.instagram.web.provider import ENV_PAGE_RATE_LIMIT
from import_group_fakes import MultiProfileSite, Profile, web_app

GIANT = "perfil_gigante"
GIANT_PAGES = 2100


class QueryCounter:
    """Conta as instruções SQL do app (métodos do Database) enquanto ativo."""

    def __init__(self, db) -> None:
        self.db = db
        self.count = 0
        self._originals = {}

    def __enter__(self) -> QueryCounter:
        for name in ("execute", "fetchone", "fetchall", "scalar"):
            original = getattr(self.db, name)
            self._originals[name] = original

            def counted(*args, _original=original, **kwargs):
                self.count += 1
                return _original(*args, **kwargs)

            setattr(self.db, name, counted)
        return self

    def __exit__(self, *exc) -> None:
        for name in self._originals:
            delattr(self.db, name)


def queries(h, path: str, **params) -> tuple[int, dict]:
    with QueryCounter(h.ctx.db) as counter:
        response = h.client.get(path, params=params)
    assert response.status_code == 200, response.text
    return counter.count, response.json()


@pytest.mark.slow
def test_group_with_300_profiles_and_an_all_profile_of_2100_pages(harness_factory):
    small = [f"perfil_{i:03d}" for i in range(300)]
    site = MultiProfileSite({**{name: Profile(1, per_page=2) for name in small},
                             GIANT: Profile(GIANT_PAGES, per_page=1)}, record=False)
    # Sem o teto de páginas por hora (senão a 601a página esperaria de verdade); as demais proteções ficam.
    h = web_app(harness_factory, site, env={ENV_PAGE_RATE_LIMIT: "0"})
    response = h.client.post("/api/import-groups", json={"profiles": [*small, GIANT], "content_type": "all",
                                                         "limit": None})
    assert response.status_code == 202, response.text
    group_id = response.json()["group"]["id"]
    giant_id = h.ctx.db.scalar("SELECT id FROM batches WHERE group_id = ? AND source_profile = ?",
                               (group_id, GIANT))

    logging.disable(logging.CRITICAL)   # a captura de log do pytest guardaria um registro por página
    tracemalloc.start()
    early = None
    try:
        for _ in range(20_000):
            h.run_imports()
            h.ctx.worker.run_pending(max_jobs=1000)
            giant = h.ctx.batches.get(giant_id)
            if early is None and giant["found"] >= GIANT_PAGES // 3:
                gc.collect()
                early = (giant["found"], tracemalloc.get_traced_memory()[0])
            if giant["status"] == "completed":
                break
        gc.collect()
        late = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()
        logging.disable(logging.NOTSET)

    assert early is not None and giant["status"] == "completed"
    assert giant["found"] == GIANT_PAGES and giant["listing_end"] == "profile_end"
    assert site.posts_by_profile[GIANT] == GIANT_PAGES, "todas as páginas, cada uma pedida uma vez"
    assert site.max_in_flight == 1
    # Memória plana: da 1a amostra ao fim foram listadas ~1.400 páginas; acumular respostas/nós/jobs em memória
    # custaria vários KB por página.
    per_page = (late - early[1]) / (GIANT_PAGES - early[0])
    assert per_page < 1500, (early, late, per_page)

    # Sem N+1: o número de consultas das rotas de grupo não depende de quantos filhos/grupos existem.
    tiny_site_profiles = [f"mini_{i}" for i in range(3)]
    for name in tiny_site_profiles:
        site.profiles[name] = Profile(1, per_page=2)
    tiny_id = h.client.post("/api/import-groups", json={"profiles": tiny_site_profiles, "content_type": "all",
                                                        "limit": None}).json()["group"]["id"]
    big_count, big = queries(h, f"/api/import-groups/{group_id}", children_limit=500)
    tiny_count, tiny = queries(h, f"/api/import-groups/{tiny_id}", children_limit=500)
    assert len(big["children"]) == 301 and len(tiny["children"]) == 3
    # O grupo pequeno ainda tem filhos abertos (uma consulta agregada a mais dos jobs deles); nunca uma por filho.
    assert big_count <= tiny_count <= 8, (big_count, tiny_count)
    assert big["group"]["profiles"] == {"total": 301, "waiting": 0, "active": 0, "completed": 301, "attention": 0,
                                        "failed": 0, "cancelled": 0}
    assert big["group"]["progress"]["found"] == 300 * 2 + GIANT_PAGES == big["group"]["progress"]["downloaded"]
    assert big["group"]["profile_end_count"] == 301 and big["group"]["status"] == "completed"

    one_count, one = queries(h, "/api/import-groups", limit=1)
    two_count, two = queries(h, "/api/import-groups", limit=50)
    assert len(one["items"]) == 1 and len(two["items"]) == 2 and one_count == two_count <= 4
    page_count, page = queries(h, f"/api/import-groups/{group_id}", children_limit=100, after_position=200)
    assert [c["group_position"] for c in page["children"]] == list(range(201, 301)) and page_count <= big_count
    assert page["next_after_position"] == 300
