"""Grupos de importação (vários perfis de uma vez): validação da lista, lote pai + um filho por perfil, fila serial,
erros isolados, cancelamento, reinício, teto de páginas compartilhado, 1 requisição em voo, deduplicação e
compatibilidade. 100% offline, valores FAKE (docs/CONTRACTS.md, "Grupos de importação").
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.db.connection import Database
from app.db.migrate import apply_migrations, schema_version
from app.db.repositories.batches import BatchRepository
from app.integrations.instagram.downloader import InstagramError
from app.integrations.instagram.web.provider import ENV_PAGE_RATE_LIMIT, InstagramWebDownloader, page_rate_limit
from app.services.import_service import MAX_GROUP_PROFILES, parse_profile_entries
from app.services.instagram_gate import DEFAULT_MAX_PAGES_PER_HOUR, PAGE_WINDOW_SECONDS, InstagramRequestGate
from conftest import wait_until
from import_group_fakes import (
    CookieSession,
    FakeCdn,
    MultiProfileSite,
    Profile,
    ProfilesDownloader,
    closed,
    drive,
    items_for,
    web_app,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "app" / "db" / "migrations"


def create_group(h, profiles, *, content: str = "all", limit: int | None = 10, expect: int = 202, **extra) -> dict:
    response = h.client.post("/api/import-groups", json={"profiles": profiles, "content_type": content,
                                                         "limit": limit, **extra})
    assert response.status_code == expect, response.text
    return response.json()


def get_group(h, group_id: int, **params) -> dict:
    response = h.client.get(f"/api/import-groups/{group_id}", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def child_ids(h, group_id: int) -> list[int]:
    return [r["id"] for r in h.ctx.db.fetchall(
        "SELECT id FROM batches WHERE group_id = ? ORDER BY group_position", (group_id,))]


def finish_group(h, group_id: int) -> dict:
    drive(h, closed(h, *child_ids(h, group_id)))
    return get_group(h, group_id)


def profiles_app(harness_factory, profiles: dict[str, int], **kwargs):
    dl = ProfilesDownloader(profiles={name: items_for(name, count) for name, count in profiles.items()}, **kwargs)
    return harness_factory(downloader=dl), dl


# --------------------------------------------------------------------------- 6. lista de perfis

MIXED = [
    "usuario_um", "@Usuario_Dois", "https://www.instagram.com/usuario.tres/",
    "http://instagram.com/usuario4?hl=pt-br#topo", "instagram.com/usuario5/", "   ", "",
    "@usuario_um", "https://instagram.com/USUARIO_DOIS",                 # repetidas depois de normalizar
    "https://www.instagram.com/p/ABCDE12345/", "https://www.instagram.com/reel/ABCDE12345/",
    "https://www.instagram.com/explore/tags/fake/", "https://example.com/usuario", "usuário_acento", "a..b",
    "x" * 31, "nome com espaço",
]
VALID = ["usuario_um", "usuario_dois", "usuario.tres", "usuario4", "usuario5"]


def test_preview_normalizes_dedupes_and_reports_invalid_without_side_effects(harness):
    body = harness.client.post("/api/import-groups/preview", json={"entries": MIXED}).json()
    assert body["valid"] == VALID and body["duplicates"] == 2
    assert [entry["entry"] for entry in body["invalid"]] == MIXED[9:]
    assert all(entry["reason"] for entry in body["invalid"])
    # Sem efeitos: nada criado e nenhuma chamada ao provider do Instagram.
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM import_groups") == 0
    assert harness.downloader.listings == 0


def test_preview_accepts_pasted_text_with_blank_lines_and_separators(harness):
    text = ("usuario_um\r\n@usuario_dois\n\n   \n  usuario.tres , usuario4;usuario5\n\nusuario_um\n/p/\n"
            "perfil a perfil_b\n\tcom_tab aqui\n")
    body = harness.client.post("/api/import-groups/preview", json={"text": text}).json()
    assert body["valid"] == VALID and body["duplicates"] == 1
    # Um perfil por linha: espaço NÃO separa; a linha com espaço interno é UMA entrada inválida (pontas aparadas).
    assert [entry["entry"] for entry in body["invalid"]] == ["/p/", "perfil a perfil_b", "com_tab aqui"]
    assert all(entry["reason"] for entry in body["invalid"])
    assert harness.client.post("/api/import-groups/preview", json={}).json() == {
        "valid": [], "invalid": [], "duplicates": 0}


@pytest.mark.parametrize("entry", [
    "usuario_teste", "@usuario_teste", "  usuario_teste  ", "instagram.com/usuario_teste",
    "www.instagram.com/usuario_teste/", "https://instagram.com/usuario_teste",
    "http://www.instagram.com/usuario_teste/", "https://www.instagram.com/usuario_teste/?hl=pt-br#topo",
    "https://www.instagram.com/usuario_teste/reels/", "https://www.instagram.com/usuario_teste/tagged/",
    "instagram.com/Usuario_Teste/reels",
])
def test_profile_forms_accepted(entry):
    assert parse_profile_entries([entry]) == (["usuario_teste"], [], 0)


@pytest.mark.parametrize("entry", [
    "https://www.instagram.com/p/ABCDE12345/", "https://www.instagram.com/reel/ABCDE12345/",
    "https://www.instagram.com/reels/ABCDE12345/", "https://www.instagram.com/reels/",
    "https://www.instagram.com/explore/", "https://www.instagram.com/explore/tags/fake/",
    "https://www.instagram.com/stories/usuario_teste/123/", "https://www.instagram.com/accounts/login/",
    "https://www.instagram.com/direct/inbox/", "https://www.instagram.com/tv/ABCDE12345/",
    "https://www.instagram.com/usuario_teste/p/ABCDE12345/", "https://www.instagram.com/usuario_teste/reel/ABCDE1/",
    "https://www.instagram.com/usuario_teste/followers/", "https://www.instagram.com/usuario_teste/reels/extra/",
    "https://www.instagram.com/", "https://example.com/usuario_teste", "https://instagram.com.example.com/usuario",
    "ftp://instagram.com/usuario_teste", "/usuario_teste/", "nome com espaço", "usuario teste",
    # nomes reservados nunca são usuário
    "p", "reel", "reels", "explore", "stories", "accounts", "direct", "tv", "about", "legal", "developer", "api",
    "@explore", "instagram.com/accounts", "https://www.instagram.com/api/",
])
def test_profile_forms_rejected(entry):
    valid, invalid, duplicates = parse_profile_entries([entry])
    assert valid == [] and duplicates == 0, entry
    assert len(invalid) == 1 and invalid[0]["entry"] == entry.strip() and invalid[0]["reason"]


def test_create_rejects_invalid_entries_with_the_list_and_keeps_the_valid_ones(harness):
    body = create_group(harness, MIXED, expect=422)
    assert body["code"] == "invalid_profiles" and body["detail"]
    assert [entry["entry"] for entry in body["invalid"]] == MIXED[9:]
    assert body["valid"] == VALID and body["duplicates"] == 2, "as válidas voltam prontas para reenviar"
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0
    created = create_group(harness, body["valid"])
    assert [child["source_profile"] for child in created["children"]] == VALID


def test_hundreds_of_profiles_in_one_request(harness):
    names = [f"perfil_{i:03d}" for i in range(400)]
    entries = [(name, f"@{name}", f"https://www.instagram.com/{name}/")[i % 3] for i, name in enumerate(names)]
    created = create_group(harness, entries + entries[:7], limit=None)
    group = created["group"]
    assert created["duplicates"] == 7 and created["skipped"] == []
    assert group["profiles"] == {"total": 400, "waiting": 400, "active": 0, "completed": 0, "attention": 0,
                                 "failed": 0, "cancelled": 0}
    assert len(created["children"]) == 100 and created["next_after_position"] == 100
    seen, after = [], None
    while True:   # paginação keyset dos filhos, na ordem da lista
        page = get_group(harness, group["id"], children_limit=150, **({"after_position": after} if after else {}))
        seen += [(child["group_position"], child["source_profile"]) for child in page["children"]]
        after = page["next_after_position"]
        if after is None:
            break
    assert seen == list(enumerate(names, start=1))


def test_sanity_cap_of_profiles_per_request(harness):
    assert MAX_GROUP_PROFILES == 10_000
    valid, invalid, duplicates = parse_profile_entries([f"p{i}" for i in range(MAX_GROUP_PROFILES)])
    assert len(valid) == MAX_GROUP_PROFILES and not invalid and duplicates == 0
    body = create_group(harness, [f"p{i}" for i in range(MAX_GROUP_PROFILES + 1)], expect=422)
    assert body["code"] == "too_many_profiles"
    assert create_group(harness, ["  ", ""], expect=422)["code"] == "no_profiles"


# --------------------------------------------------------------------------- 7. um filho por perfil


def test_group_creates_one_child_per_profile_with_quantity_per_profile(harness_factory):
    h, dl = profiles_app(harness_factory, {"perfil_a": 15, "perfil_b": 4, "perfil_c": 30})
    created = create_group(h, ["perfil_a", "@perfil_b", "https://www.instagram.com/perfil_c/"], limit=10)
    group_id = created["group"]["id"]
    children = created["children"]
    assert [(c["source_profile"], c["group_position"], c["group_id"], c["requested_limit"], c["status"],
             c["group_state"]) for c in children] == [
        ("perfil_a", 1, group_id, 10, "listing", "waiting"), ("perfil_b", 2, group_id, 10, "listing", "waiting"),
        ("perfil_c", 3, group_id, 10, "listing", "waiting")]
    assert created["group"]["status"] == "active" and created["group"]["requested_limit"] == 10
    assert created["group"]["finished_at"] is None

    detail = finish_group(h, group_id)
    assert dl.order == ["perfil_a", "perfil_b", "perfil_c"], "um perfil por vez, na ordem da lista"
    assert dl.limits == [("perfil_a", 10), ("perfil_b", 10), ("perfil_c", 10)], "a quantidade é POR PERFIL"
    assert [(c["source_profile"], c["found"], c["listing_end"], c["status"], c["group_state"])
            for c in detail["children"]] == [
        ("perfil_a", 10, "limit", "completed", "completed"), ("perfil_b", 4, "profile_end", "completed", "completed"),
        ("perfil_c", 10, "limit", "completed", "completed")]
    group = detail["group"]
    assert group["status"] == "completed" and group["attention"] is False and group["finished_at"]
    assert group["profiles"] == {"total": 3, "waiting": 0, "active": 0, "completed": 3, "attention": 0, "failed": 0,
                                 "cancelled": 0}
    assert group["progress"] == {"found": 24, "downloaded": 24, "processed": 0, "skipped": 0, "failed": 0,
                                 "pending": 0, "active": 0}
    assert group["profile_end_count"] == 1 and group["listing_dropped_total"] == 0

    listing = h.client.get("/api/import-groups").json()
    assert listing["total"] == 1 and listing["items"] == [group] and listing["next_before_id"] is None
    # Lista de lotes: por padrão continua mostrando os filhos; `ungrouped` e `group_id` filtram.
    assert h.client.get("/api/batches").json()["total"] == 3
    assert h.client.get("/api/batches", params={"ungrouped": "true"}).json()["total"] == 0
    assert h.client.get("/api/batches", params={"group_id": group_id}).json()["total"] == 3
    single = h.import_profile(profile="perfil_avulso")
    assert single["group_id"] is None and single["group_state"] is None
    assert [b["id"] for b in h.client.get("/api/batches", params={"ungrouped": "true"}).json()["items"]] == [
        single["id"]]


def test_group_live_aggregates_and_keyset_list(harness_factory):
    h, dl = profiles_app(harness_factory, {f"perfil_{c}": 6 for c in "abcde"})
    first = create_group(h, ["perfil_a", "perfil_b"], limit=None)["group"]["id"]
    second = create_group(h, ["perfil_c"], limit=None)["group"]["id"]
    third = create_group(h, ["perfil_d", "perfil_e"], limit=None)["group"]["id"]
    assert h.ctx.import_runner.run_next()   # um trecho: só o 1o perfil do 1o grupo
    group = get_group(h, first)
    assert group["group"]["profiles"]["waiting"] == 1 and group["group"]["profiles"]["active"] == 1
    assert group["group"]["progress"]["found"] == 6 and group["group"]["progress"]["pending"] == 6
    assert [c["group_state"] for c in group["children"]] == ["active", "waiting"]
    page = h.client.get("/api/import-groups", params={"limit": 2}).json()
    assert [g["id"] for g in page["items"]] == [third, second] and page["next_before_id"] == second
    page = h.client.get("/api/import-groups", params={"limit": 2, "before_id": page["next_before_id"]}).json()
    assert [g["id"] for g in page["items"]] == [first] and page["next_before_id"] is None and page["total"] == 3
    assert h.client.get("/api/import-groups/999").status_code == 404


def test_profiles_already_importing_are_skipped_and_all_skipped_is_409(harness_factory):
    h, _dl = profiles_app(harness_factory, {"perfil_a": 3, "perfil_b": 3, "perfil_c": 3})
    h.import_profile(profile="perfil_b")   # importação avulsa ainda listando
    created = create_group(h, ["perfil_a", "perfil_b", "perfil_c"])
    assert created["skipped"] == [{"profile": "perfil_b", "reason": "profile_already_importing"}]
    assert [(c["source_profile"], c["group_position"]) for c in created["children"]] == [
        ("perfil_a", 1), ("perfil_c", 2)]
    again = create_group(h, ["perfil_a", "@perfil_b", "perfil_c"], expect=409)
    assert again["code"] == "profile_already_importing" and len(again["skipped"]) == 3
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM import_groups") == 1, "o pedido recusado não deixa grupo vazio"


# --------------------------------------------------------------------------- 8. erro em um perfil só


def test_error_in_one_profile_does_not_affect_the_others(harness_factory):
    h, dl = profiles_app(
        harness_factory, {"perfil_ok1": 5, "perfil_sumiu": 0, "perfil_privado": 0, "perfil_quebrado": 30,
                          "perfil_ok2": 5},
        errors={"perfil_sumiu": InstagramError("profile_not_found"),
                "perfil_privado": InstagramError("profile_private"),
                "perfil_quebrado": InstagramError("contract_changed")},
        error_after={"perfil_quebrado": 12},
    )
    names = ["perfil_ok1", "perfil_sumiu", "perfil_privado", "perfil_quebrado", "perfil_ok2"]
    group_id = create_group(h, names, limit=100)["group"]["id"]
    for _ in range(3):
        h.run_imports()
        h.run_jobs()
    detail = get_group(h, group_id)
    assert dl.order == names, "a fila segue depois de cada erro, sem repetir ninguém"
    assert [(c["source_profile"], c["status"], c["group_state"], c["error_code"], c["found"])
            for c in detail["children"]] == [
        ("perfil_ok1", "completed", "completed", None, 5),
        ("perfil_sumiu", "failed", "failed", "profile_not_found", 0),
        ("perfil_privado", "failed", "failed", "profile_private", 0),
        ("perfil_quebrado", "listing", "attention", "contract_changed", 12),
        ("perfil_ok2", "completed", "completed", None, 5),
    ]
    broken = detail["children"][3]
    assert broken["listing_state"] == "needs_attention" and broken["error_message"]
    assert broken["progress"]["downloaded"] == 12, "o que já foi listado não é apagado"
    group = detail["group"]
    assert group["status"] == "attention" and group["attention"] is True and group["finished_at"] is None
    assert group["profiles"] == {"total": 5, "waiting": 0, "active": 0, "completed": 2, "attention": 1, "failed": 2,
                                 "cancelled": 0}
    assert group["progress"]["found"] == 22 and group["progress"]["downloaded"] == 22

    # O filho parado é retomado pela rota de lote existente; o grupo fecha.
    dl.errors.pop("perfil_quebrado")
    assert h.client.post(f"/api/batches/{broken['id']}/resume-listing").status_code == 200
    final = finish_group(h, group_id)
    assert final["children"][3]["found"] == 30 and final["children"][3]["status"] == "completed"
    assert final["group"]["status"] == "completed" and final["group"]["attention"] is True, "2 perfis falharam"
    assert final["group"]["progress"]["found"] == 40


# --------------------------------------------------------------------------- 9. cancelamento


def test_cancel_one_child_and_then_the_group(harness_factory):
    h, dl = profiles_app(harness_factory, {"perfil_a": 30, "perfil_b": 5, "perfil_c": 5, "perfil_d": 5})
    created = create_group(h, ["perfil_a", "perfil_b", "perfil_c", "perfil_d"], limit=None)
    group_id = created["group"]["id"]
    a, b, c, d = (child["id"] for child in created["children"])
    assert h.ctx.import_runner.run_next()
    h.run_jobs()
    assert h.ctx.batches.get(a)["status"] == "completed"

    one = h.client.post(f"/api/batches/{c}/cancel")
    assert one.status_code == 200 and one.json()["status"] == "cancelled" and one.json()["group_state"] == "cancelled"
    detail = get_group(h, group_id)
    assert detail["group"]["status"] == "active", "cancelar um filho não cancela o grupo"
    assert [ch["group_state"] for ch in detail["children"]] == ["completed", "waiting", "cancelled", "waiting"]

    response = h.client.post(f"/api/import-groups/{group_id}/cancel")
    assert response.status_code == 200
    detail = response.json()
    assert [ch["status"] for ch in detail["children"]] == ["completed", "cancelled", "cancelled", "cancelled"]
    assert detail["group"]["status"] == "completed", "o filho concluído fica; o grupo não está todo cancelado"
    assert detail["group"]["profiles"]["completed"] == 1 and detail["group"]["profiles"]["cancelled"] == 3
    assert detail["group"]["progress"]["downloaded"] == 30 and detail["group"]["finished_at"]
    h.run_imports()
    assert dl.order == ["perfil_a"], "nenhum filho da fila chegou a listar"
    assert h.client.post(f"/api/import-groups/{group_id}/cancel").json()["code"] == "group_finished"
    assert h.client.post("/api/import-groups/999/cancel").status_code == 404
    assert {d, b} == {b, d} and h.ctx.batches.get(b)["listing_state"] == "cancelled"


def test_cancel_group_of_300_profiles_leaves_no_queued_child_listing(harness_factory):
    names = [f"perfil_{i:03d}" for i in range(300)]
    h, dl = profiles_app(harness_factory, {name: 30 for name in names})
    group_id = create_group(h, names, limit=None)["group"]["id"]
    assert h.ctx.import_runner.run_next() and dl.order == ["perfil_000"]   # o 1o listou; 299 na fila

    transactions = []
    original = h.ctx.db.transaction

    def counting():
        if not h.ctx.db.conn.in_transaction:   # só as transações de verdade (as aninhadas reusam a aberta)
            transactions.append(1)
        return original()

    h.ctx.db.transaction = counting
    try:
        response = h.client.post(f"/api/import-groups/{group_id}/cancel")
    finally:
        del h.ctx.db.transaction
    assert response.status_code == 200 and len(transactions) == 1, "a parte de banco é UMA transação"
    group = response.json()["group"]
    assert group["status"] == "cancelled" and group["profiles"]["cancelled"] == 300
    assert group["profiles"]["waiting"] == group["profiles"]["active"] == 0
    for _ in range(3):
        h.run_imports()
        h.run_jobs()
    assert dl.order == ["perfil_000"], "nenhum filho da fila começou a listar depois do cancelamento"
    assert dl.downloads == [], "os jobs do filho que já tinha listado foram cancelados"
    states = {r[0] for r in h.ctx.db.fetchall(
        "SELECT status || '/' || listing_state FROM batches WHERE group_id = ? AND group_position > 1", (group_id,))}
    assert states == {"cancelled/cancelled"}
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM jobs WHERE status <> 'cancelled'") == 0


def test_cancel_group_stops_the_child_that_is_listing(harness_factory):
    # Cancelamento cooperativo: o filho em listagem (thread do runner) para; a fila não anda.
    import threading

    names = ["perfil_a", "perfil_b", "perfil_c"]
    h, dl = profiles_app(harness_factory, {name: 60 for name in names})
    dl.block = None
    group_id = create_group(h, names, limit=None)["group"]["id"]
    reached, release = threading.Event(), threading.Event()
    original = dl.iter_profile_media

    def slow(username, content, limit, **kwargs):
        for index, item in enumerate(original(username, content, limit, **kwargs)):
            if index == 5:
                reached.set()
                release.wait(10)
            yield item

    dl.iter_profile_media = slow
    thread = threading.Thread(target=h.run_imports, daemon=True)
    thread.start()
    assert reached.wait(10)
    assert h.client.post(f"/api/import-groups/{group_id}/cancel").status_code == 200
    release.set()
    thread.join(10)
    assert not thread.is_alive()
    h.run_imports()
    assert dl.order == ["perfil_a"]
    detail = get_group(h, group_id)
    assert [c["status"] for c in detail["children"]] == ["cancelled"] * 3
    assert detail["children"][0]["listing_state"] == "cancelled"
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM jobs WHERE status NOT IN ('cancelled')") == 0


def test_cancel_whole_group_while_everything_waits(harness_factory):
    h, dl = profiles_app(harness_factory, {"perfil_a": 5, "perfil_b": 5})
    group_id = create_group(h, ["perfil_a", "perfil_b"])["group"]["id"]
    detail = h.client.post(f"/api/import-groups/{group_id}/cancel").json()
    assert detail["group"]["status"] == "cancelled" and detail["group"]["profiles"]["cancelled"] == 2
    h.run_imports()
    assert dl.order == []


# --------------------------------------------------------------------------- 10. reinício no meio do grupo


def test_restart_in_the_middle_of_a_group_keeps_order_and_checkpoint(harness_factory):
    sizes = {"perfil_a": 5, "perfil_b": 150, "perfil_c": 5, "perfil_d": 5}
    h, dl = profiles_app(harness_factory, sizes)
    group_id = create_group(h, list(sizes), limit=None)["group"]["id"]
    a, b, c, d = child_ids(h, group_id)
    h.run_imports()   # sem downloads: `a` lista inteiro; `b` para no backpressure; `c` e `d` continuam na fila
    assert dl.order == ["perfil_a", "perfil_b"], "o próximo perfil não começa enquanto o da vez espera downloads"
    state = h.ctx.batches.get(b)
    assert state["listing_state"] == "waiting_backpressure" and state["listing_next_page"] == 9
    assert [ch["group_state"] for ch in get_group(h, group_id)["children"]] == ["active", "active", "waiting",
                                                                                 "waiting"]
    found_before = h.ctx.batches.get(b)["found"]
    assert found_before == 96

    # "Reinício": app novo sobre o mesmo banco (provider novo, sem memória do anterior).
    dl2 = ProfilesDownloader(profiles={name: items_for(name, count) for name, count in sizes.items()})
    h2 = harness_factory(root=h.root, downloader=dl2)
    restarted = get_group(h2, group_id)
    assert [ch["id"] for ch in restarted["children"]] == [a, b, c, d]
    assert [ch["group_state"] for ch in restarted["children"]] == ["active", "active", "waiting", "waiting"]
    assert h2.ctx.batches.get(b)["listing_next_page"] == 9 and h2.ctx.batches.get(b)["found"] == 96

    final = finish_group(h2, group_id)
    assert dl2.order[0] == "perfil_b" and "perfil_a" not in dl2.order, "o filho já listado não é listado de novo"
    assert dl2.page_requests[0] == 9, "o filho em listagem retoma do checkpoint, não da página 1"
    firsts = list(dict.fromkeys(dl2.order))
    assert firsts == ["perfil_b", "perfil_c", "perfil_d"], "a fila continua na mesma ordem"
    assert [ch["found"] for ch in final["children"]] == [5, 150, 5, 5]
    assert final["group"]["status"] == "completed" and final["group"]["progress"]["downloaded"] == 165
    keys = h2.ctx.db.fetchall("SELECT source_post_id, source_media_index FROM jobs")
    assert len(keys) == 165 == len({tuple(k) for k in keys}), "nada duplicado"
    downloads = dl.downloads + dl2.downloads
    assert len(downloads) == 165 == len(set(downloads)), "nada baixado duas vezes"


# --------------------------------------------------------------------------- 11. teto de páginas por hora


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.on_sleep = None

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep()
        self.now += seconds


def test_default_cap_is_600_pages_per_hour_and_the_601st_waits():
    assert DEFAULT_MAX_PAGES_PER_HOUR == 600
    assert page_rate_limit(None) == page_rate_limit({}) == 600
    assert page_rate_limit({ENV_PAGE_RATE_LIMIT: "abc"}) == 600, "valor inválido volta ao padrão"
    assert page_rate_limit({ENV_PAGE_RATE_LIMIT: "900"}) == 900 and page_rate_limit({ENV_PAGE_RATE_LIMIT: "300"}) == 300
    assert page_rate_limit({ENV_PAGE_RATE_LIMIT: "0"}) == 0, "0 continua desativando o teto explicitamente"
    clock = Clock()
    gate = InstagramRequestGate(interval=(0, 0), clock=clock, sleep=clock.sleep)
    for _ in range(600):
        gate.wait_listing_page()
    assert clock.sleeps == [], "600 páginas na hora sem espera"
    gate.wait_listing_page()
    assert clock.sleeps == [pytest.approx(PAGE_WINDOW_SECONDS)], "a 601a espera a janela"


@pytest.mark.parametrize("env, pages, expected_waits", [({}, 601, 1), ({ENV_PAGE_RATE_LIMIT: "50"}, 101, 2),
                                                        ({ENV_PAGE_RATE_LIMIT: "0"}, 700, 0)])
def test_provider_applies_the_configured_cap(env, pages, expected_waits):
    clock = Clock()
    gate = InstagramRequestGate(interval=(0, 0), clock=clock, sleep=clock.sleep)
    site = MultiProfileSite({"perfil_teste": Profile(pages, per_page=1)}, record=False)
    dl = InstagramWebDownloader(CookieSession(), transport=site.transport, page_delay=(0, 0), request_gate=gate,
                                env=env)
    assert sum(1 for _ in dl.iter_profile_media("perfil_teste", "all", None)) == pages
    assert len(clock.sleeps) == expected_waits


def test_hourly_window_is_shared_by_all_profiles_of_the_group(harness_factory):
    clock = Clock()
    gate = InstagramRequestGate(interval=(0, 0), clock=clock, sleep=clock.sleep)
    names = ["perfil_a", "perfil_b", "perfil_c"]
    site = MultiProfileSite({name: Profile(4, per_page=2) for name in names})
    h = web_app(harness_factory, site, env={ENV_PAGE_RATE_LIMIT: "5"}, gate=gate)
    group_id = create_group(h, names, limit=None)["group"]["id"]
    snapshots: list[list[tuple]] = []

    def snapshot() -> None:   # durante a espera do teto (a thread da listagem está parada no portão)
        detail = get_group(h, group_id)
        snapshots.append([(c["source_profile"], c["group_state"], c["listing_state"], c["rate_limit_pages_used"],
                           c["rate_limit_pages_max"]) for c in detail["children"]])
        assert detail["group"]["status"] == "active"

    clock.on_sleep = snapshot
    final = finish_group(h, group_id)
    # 12 páginas com teto de 5 por hora PARA TODOS os perfis: espera na 6a (2a do perfil_b) e na 11a (3a do perfil_c).
    assert len(clock.sleeps) == 2 and all(s == pytest.approx(PAGE_WINDOW_SECONDS) for s in clock.sleeps)
    assert snapshots[0][1] == ("perfil_b", "active", "waiting_rate_limit", 5, 5)
    # Filho que ainda não começou: `listing_state` nulo (ou "pending"); quem diz "na fila" é `group_state`.
    assert snapshots[0][2][:3] == ("perfil_c", "waiting", None), "a fila espera atrás do perfil da vez"
    assert snapshots[1][2] == ("perfil_c", "active", "waiting_rate_limit", 5, 5)
    assert [profile for profile, _page in site.order] == [n for n in names for _ in range(4)], "a fila continua"
    assert final["group"]["status"] == "completed" and final["group"]["progress"]["found"] == 24
    assert all(c["listing_state"] == "done" and c["rate_limit_resume_at"] is None for c in final["children"])


# --------------------------------------------------------------------------- 12. nenhum GraphQL paralelo


def test_never_more_than_one_instagram_request_in_flight_across_profiles(harness_factory):
    names = [f"perfil_{c}" for c in "abcd"]
    site = MultiProfileSite({name: Profile(6) for name in names}, delay=0.003)
    h = web_app(harness_factory, site, cdn=FakeCdn(delay=0.004), start_worker=True)   # threads reais do app
    group_id = create_group(h, names, limit=None)["group"]["id"]
    assert wait_until(lambda: get_group(h, group_id)["group"]["status"] == "completed", timeout=120.0)
    group = get_group(h, group_id)["group"]
    assert group["progress"]["downloaded"] == 4 * 6 * 12 == len(h.cdn.calls)
    assert site.posts == 24 and site.max_in_flight == 1, "nunca duas requisições ao Instagram ao mesmo tempo"
    assert h.cdn.max_in_flight >= 2, "os downloads (CDN) seguem em paralelo, fora do portão"
    assert [profile for profile, _page in site.order] == [n for n in names for _ in range(6)], "um perfil por vez"


# --------------------------------------------------------------------------- 13. deduplicação entre importações


def test_dedup_across_groups_and_republished_media(harness_factory):
    site = MultiProfileSite({"perfil_original": Profile(1), "perfil_espelho": Profile(1, code_prefix="PERFILORIGINAL")})
    h = web_app(harness_factory, site)
    first = finish_group(h, create_group(h, ["perfil_original", "@perfil_original"], limit=None)["group"]["id"])
    assert first["group"]["profiles"]["total"] == 1 and first["group"]["progress"]["downloaded"] == 12
    assert len(h.cdn.calls) == 12

    again = finish_group(h, create_group(h, ["perfil_original"], limit=None)["group"]["id"])   # mesmo perfil de novo
    mirror = finish_group(h, create_group(h, ["perfil_espelho"], limit=None)["group"]["id"])   # republica as mesmas
    for detail in (again, mirror):
        assert detail["group"]["status"] == "completed" and detail["group"]["progress"]["found"] == 12
        assert detail["group"]["progress"]["downloaded"] == 0, "já está na biblioteca: nenhum download novo"
        assert detail["group"]["listing_dropped_total"] == 0
    assert len(h.cdn.calls) == 12 and h.ctx.db.scalar("SELECT COUNT(*) FROM media") == 12
    reasons = {r[0] for r in h.ctx.db.fetchall(
        "SELECT duplicate_reason FROM jobs WHERE batch_id IN (?, ?)",
        (again["children"][0]["id"], mirror["children"][0]["id"]))}
    assert reasons == {"source_id"}


# --------------------------------------------------------------------------- 15. compatibilidade


@pytest.mark.parametrize("content", ["reels", "posts", "videos", "images", "all", "photos_carousels"])
def test_old_single_profile_import_still_works(harness, content):
    harness.downloader.items = items_for("perfil_teste", 3)
    response = harness.client.post("/api/imports", json={"profile": "perfil_teste", "content_type": content,
                                                         "limit": 10, "preset_id": None, "process": False})
    assert response.status_code == 202
    batch = response.json()
    assert (batch["group_id"], batch["group_position"], batch["group_state"], batch["listing_end"]) == (
        None, None, None, None)
    harness.run_all()
    done = harness.batch(batch["id"])
    assert done["status"] == "completed" and done["found"] == 3 and done["listing_end"] == "profile_end"
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM import_groups") == 0, "um perfil só não cria grupo"


def test_migration_0012_over_schema_0011_with_data_keeps_old_batches(tmp_path, harness_factory):
    root = tmp_path / "antigo"
    old = tmp_path / "migrations_0011"
    old.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if path.name < "0012":
            shutil.copy(path, old)
    db = Database(root / "data" / "media-tool.db")
    try:
        apply_migrations(db, old)
        assert schema_version(db) == 11
        batches = BatchRepository(db)
        batch_id = batches.create(source="instagram_import", status="listing", source_profile="perfil_antigo",
                                  content_type="posts", requested_limit=50)
        db.execute("UPDATE batches SET status = 'completed', listing_state = 'done', found = 7,"
                   " listing_dropped_unexpected = 2 WHERE id = ?", (batch_id,))
        before = dict(db.fetchone("SELECT * FROM batches WHERE id = ?", (batch_id,)))
        assert apply_migrations(db) == [12, 13]   # 0013 (exportações) só cria tabelas novas
        after = dict(db.fetchone("SELECT * FROM batches WHERE id = ?", (batch_id,)))
        assert {k: after[k] for k in before} == before, "nenhum dado antigo reescrito"
        assert (after["group_id"], after["group_position"], after["listing_end"]) == (None, None, None)
        assert db.scalar("SELECT COUNT(*) FROM import_groups") == 0
    finally:
        db.close_all()

    h = harness_factory(root=root)   # o app abre o banco migrado e lê o lote antigo igual
    listed = h.client.get("/api/batches").json()
    assert listed["total"] == 1
    batch = listed["items"][0]
    assert (batch["id"], batch["source_profile"], batch["content_type"], batch["requested_limit"], batch["found"],
            batch["status"]) == (batch_id, "perfil_antigo", "posts", 50, 7, "completed")
    assert (batch["group_id"], batch["group_state"], batch["listing_end"]) == (None, None, None)
    assert batch["listing_dropped"]["unexpected_total"] == 2
    assert h.client.get("/api/batches", params={"ungrouped": "true"}).json()["total"] == 1
    assert h.client.get("/api/import-groups").json() == {"items": [], "total": 0, "next_before_id": None}


# --------------------------------------------------------------------------- método gallery_dl: erro claro na criação


@pytest.mark.parametrize("content, limit, refused", [
    ("all", None, True), ("reels", None, True), ("photos_carousels", 10, True), ("photos_carousels", None, True),
    ("all", 10, False), ("reels", 1000, False), ("all", 5000, False), ("all", 5001, True), ("reels", 20000, True),
])
def test_gallery_dl_method_refuses_all_and_photos_carousels_at_creation(harness, content, limit, refused):
    assert harness.client.put("/api/settings", json={"instagram_provider": "gallery_dl"}).status_code == 200
    single = harness.client.post("/api/imports", json={"profile": "perfil_um", "content_type": content,
                                                       "limit": limit})
    group = harness.client.post("/api/import-groups", json={"profiles": ["perfil_dois", "perfil_tres"],
                                                            "content_type": content, "limit": limit})
    if not refused:
        assert single.status_code == 202 and group.status_code == 202
        return
    for response in (single, group):
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "not_supported_by_provider"
        assert "Todos" in body["detail"] and "Publicações do feed" in body["detail"] and "web" in body["detail"]
        assert "5.000" in body["detail"]
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM import_groups") == 0
    # Com o método web (padrão) ou auto o mesmo pedido é aceito.
    assert harness.client.put("/api/settings", json={"instagram_provider": "auto"}).status_code == 200
    assert harness.client.post("/api/imports", json={"profile": "perfil_um", "content_type": content,
                                                     "limit": limit}).status_code == 202


# --------------------------------------------------------------------------- reinício: needs_attention e teto por hora


def test_restart_with_a_child_in_needs_attention_lets_the_queue_move_on(harness_factory):
    sizes = {"perfil_a": 5, "perfil_b": 30, "perfil_c": 5, "perfil_d": 5}
    broken = {"errors": {"perfil_b": InstagramError("contract_changed")}, "error_after": {"perfil_b": 12}}
    h, dl = profiles_app(harness_factory, sizes, **broken)
    group_id = create_group(h, list(sizes), limit=None)["group"]["id"]
    a, b, c, d = child_ids(h, group_id)
    assert h.ctx.import_runner.run_next() and h.ctx.import_runner.run_next()   # `a` inteiro; `b` quebra na página 2
    assert dl.order == ["perfil_a", "perfil_b"] and h.ctx.batches.get(b)["listing_state"] == "needs_attention"

    dl2 = ProfilesDownloader(profiles={n: items_for(n, count) for n, count in sizes.items()}, **broken)
    h2 = harness_factory(root=h.root, downloader=dl2)   # reinício
    for _ in range(3):
        h2.run_imports()
        h2.run_jobs()
    detail = get_group(h2, group_id)
    assert dl2.order == ["perfil_c", "perfil_d"], "os seguintes andam, na ordem; ninguém é listado de novo"
    assert [ch["id"] for ch in detail["children"]] == [a, b, c, d]
    assert [(ch["group_state"], ch["found"]) for ch in detail["children"]] == [
        ("completed", 5), ("attention", 12), ("completed", 5), ("completed", 5)]
    stored = h2.ctx.batches.get(b)
    assert stored["listing_next_page"] == 2 and stored["listing_cursor"], "checkpoint do filho parado preservado"
    keys = h2.ctx.db.fetchall("SELECT source_post_id, source_media_index FROM jobs")
    assert len(keys) == 27 == len({tuple(k) for k in keys})
    assert len(dl.downloads + dl2.downloads) == 27 == len(set(dl.downloads + dl2.downloads))
    assert detail["group"]["status"] == "attention"


def test_restart_with_a_child_waiting_for_the_hourly_cap(harness_factory):
    import json
    import time

    from app.db.connection import utc_now
    from app.services.instagram_gate import PAGES_STATE_KEY

    names = ["perfil_a", "perfil_b", "perfil_c"]
    env = {ENV_PAGE_RATE_LIMIT: "5"}
    site = MultiProfileSite({name: Profile(4, per_page=2) for name in names})
    h = web_app(harness_factory, site, env=env)
    group_id = create_group(h, names, limit=None)["group"]["id"]
    a, b, c = child_ids(h, group_id)
    assert h.ctx.import_runner.run_next()   # `a` lista as 4 páginas dele (4 das 5 da janela)
    # O app "fecha" com `b` esperando o teto: janela cheia persistida e espera gravada no lote (como o runner grava).
    h.ctx.app_settings.set_state(PAGES_STATE_KEY, json.dumps([time.time()] * 5))
    h.ctx.batches.update_listing(b, listing_state="running", rate_limit_resume_at=utc_now(1800),
                                 rate_limit_pages_used=5, rate_limit_pages_max=5)

    site2 = MultiProfileSite({name: Profile(4, per_page=2) for name in names})
    h2 = web_app(harness_factory, site2, env=env, root=h.root)   # reinício
    detail = get_group(h2, group_id)
    assert [ch["id"] for ch in detail["children"]] == [a, b, c]
    assert [(ch["group_state"], ch["listing_state"]) for ch in detail["children"]] == [
        ("active", "done"), ("active", "waiting_rate_limit"), ("waiting", None)]
    assert detail["children"][1]["rate_limit_pages_used"] == 5 and detail["children"][1]["rate_limit_pages_max"] == 5

    # A janela libera (relógio simulado no portão): a fila continua de onde parou, na mesma ordem.
    clock = Clock()
    gate = InstagramRequestGate(interval=(0, 0), clock=clock, sleep=clock.sleep)
    h2.ctx.instagram_gate = gate
    gate.listing_wait_observer = h2.ctx.import_runner._on_rate_limit_wait
    h2.downloader = InstagramWebDownloader(CookieSession(), cdn_downloader=h2.cdn, transport=site2.transport,
                                           page_delay=(0, 0), request_gate=gate, env=env)
    final = finish_group(h2, group_id)
    assert [profile for profile, _page in site2.order] == ["perfil_b"] * 4 + ["perfil_c"] * 4
    assert [ch["found"] for ch in final["children"]] == [8, 8, 8] and final["group"]["status"] == "completed"
    assert all(ch["rate_limit_resume_at"] is None for ch in final["children"])
    keys = h2.ctx.db.fetchall("SELECT source_post_id, source_media_index FROM jobs")
    assert len(keys) == 24 == len({tuple(k) for k in keys}), "nada duplicado"
