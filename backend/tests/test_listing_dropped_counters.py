"""Descartes da listagem visíveis no lote: contadores persistentes por motivo, somados uma única vez por página.

Offline (provider web real sobre httpx.MockTransport + import runner + banco de teste); só valores FAKE. As contagens
da página entram no lote na transação do checkpoint dela; página refeita não soma duas vezes; a página em que o
limite é atingido no meio entra uma vez, no encerramento, só com os descartes até o último item gravado.
"""

from __future__ import annotations

import json
import shutil

import test_graphql_partial_errors as gp
from app.db.connection import Database
from app.db.migrate import MIGRATIONS_DIR, apply_migrations
from app.db.repositories.batches import BatchRepository
from app.db.repositories import batches as batches_repo
from app.integrations.instagram.web.normalize import DEGRADED_REASONS
from app.integrations.instagram.web.provider import _dropped_until
from app.jobs import import_runner
from conftest import make_settings
from media_fakes import FakeInstagramDownloader
from test_graphql_partial_errors import _nothing_sensitive_in_any_log  # noqa: F401 - autouse também neste arquivo

ZERO = {"unexpected_total": 0, "reasons": {}, "pending": 0}


def api(total: int, reasons: dict, pending: int = 0) -> dict:
    return {"unexpected_total": total, "reasons": reasons, "pending": pending}


def no_url(page: int, i: int) -> dict:
    """Imagem FAKE sem URL utilizável (descarte inesperado `image_no_url`)."""
    node = gp.image(page, i)
    node["image_versions2"] = {"candidates": [{"url": None, "width": 640, "height": 640}]}
    return node


class WebListingFakeDownload:
    """Lista pelo provider web real (offline) e "baixa" com o fake (nenhum acesso a CDN)."""

    def __init__(self, web) -> None:
        self.web = web
        self.fake = FakeInstagramDownloader()

    def __getattr__(self, name):
        return getattr(self.web, name)

    def download(self, item, dest_dir, **kwargs):
        return self.fake.download(item, dest_dir, **kwargs)


def state(h, batch_id: int) -> dict:
    row = dict(h.ctx.db.fetchone(
        "SELECT status, listing_state, error_code, found, listing_next_page AS next_page,"
        " listing_dropped_unexpected AS total, listing_dropped_reasons AS reasons,"
        " listing_dropped_partial AS partial FROM batches WHERE id = ?", (batch_id,)))
    for key in ("reasons", "partial"):
        row[key] = json.loads(row[key]) if row[key] else None
    return row


def drain(h, batch_id: int) -> None:
    """Tira os ingests pendentes do caminho (como se o worker os tivesse terminado), sem baixar nada."""
    h.ctx.db.execute("UPDATE jobs SET status = 'failed' WHERE batch_id = ? AND status = 'pending'", (batch_id,))


def run_until_listed(h, batch_id: int, rounds: int = 12) -> None:
    for _ in range(rounds):
        drain(h, batch_id)
        h.run_imports()
        if state(h, batch_id)["listing_state"] == "done":
            return
    raise AssertionError("a listagem não terminou")


def tight_backpressure(monkeypatch) -> None:
    """Teto de ingests pendentes minúsculo: a listagem para no meio da página (backpressure rígido)."""
    monkeypatch.setattr(import_runner, "MAX_PENDING_INGEST", 5)
    monkeypatch.setattr(import_runner, "RESUME_PENDING_INGEST", 3)
    monkeypatch.setattr(import_runner, "PAGE_ESTIMATE", 1)


# --------------------------------------------------------------------------- zero, um e vários descartes


def test_no_discards_keeps_everything_zeroed_and_silent(harness_factory, caplog):
    site = gp.Site(gp.body(1, gp.images(1)), gp.body(2, gp.images(2), has_next=False))
    h = harness_factory(downloader=gp.downloader(site))
    batch_id = gp._batch(h)
    h.run_imports()
    row = state(h, batch_id)
    assert (row["total"], row["reasons"], row["partial"], row["found"]) == (0, None, None, 24)
    assert h.batch(batch_id)["listing_dropped"] == ZERO
    assert h.client.get("/api/batches").json()["items"][0]["listing_dropped"] == ZERO
    assert gp.page_lines(caplog) == []


def test_one_discard_is_counted_and_the_batch_is_not_failed(harness_factory):
    site = gp.Site(gp.body(1, [gp.image(1, 0), no_url(1, 1), gp.image(1, 2)], has_next=False))
    h = harness_factory(downloader=gp.downloader(site))
    batch_id = gp._batch(h)
    h.run_imports()
    row = state(h, batch_id)
    assert (row["status"], row["listing_state"], row["error_code"], row["found"]) == ("running", "done", None, 2)
    assert (row["total"], row["reasons"]) == (1, {"unexpected": {"image_no_url": 1}})
    assert h.batch(batch_id)["listing_dropped"] == api(1, {"image_no_url": 1})


def test_many_reasons_across_pages_count_once_each_and_the_batch_completes(harness_factory):
    unknown = gp.image(1, 3)
    unknown["media_type"] = 99                       # tipo não suportado: filtro de propósito
    short = gp.carousel(2, 1, [gp.child(2, 1, 0), None, gp.child(2, 1, 2)])
    short["carousel_media_count"] = 5                # 2 filhos a menos que o declarado + 1 filho inválido
    degraded = gp.image(2, 2)                        # entregue em versão menor: não é item perdido
    degraded["image_versions2"]["candidates"].append({"url": None, "width": 1080, "height": 1080})
    site = gp.Site(
        gp.body(1, [gp.image(1, 0), no_url(1, 1), gp.image(1, 2), unknown, no_url(1, 4)],
                errors=[{"message": "partial"}]),
        gp.body(2, edges=[{"node": None}, {"node": short}, {"node": degraded}], has_next=False))
    h = harness_factory(downloader=WebListingFakeDownload(gp.downloader(site)))
    batch_id = gp._batch(h)
    h.run_imports()
    row = state(h, batch_id)
    unexpected = {"image_no_url": 2, "node_null": 1, "carousel_child_invalid": 1, "carousel_children_missing": 2}
    assert (row["status"], row["listing_state"], row["error_code"], row["found"]) == ("running", "done", None, 5)
    assert row["total"] == 6 and row["partial"] is None
    assert row["reasons"] == {"unexpected": unexpected, "expected": {"unsupported_type": 1},
                              "degraded": {"rendition_candidates_invalid": 1}}
    assert h.batch(batch_id)["listing_dropped"] == api(6, unexpected)
    assert site.afters == [None, gp.cursor(1)]       # nenhuma página pedida de novo por causa dos descartes
    # Os demais downloads seguem normalmente e o lote conclui (sem `failed`); os contadores continuam lá.
    h.run_jobs()
    done = h.batch(batch_id)
    assert (done["status"], done["progress"]["failed"], done["progress"]["downloaded"]) == ("completed", 0, 5)
    assert done["listing_dropped"] == api(6, unexpected)


def test_intentional_filters_are_not_counted_as_loss(harness_factory):
    unknown = gp.image(1, 3)
    unknown["media_type"] = 99
    site = gp.Site(gp.body(1, [gp.image(1, 0), gp.image(1, 1), gp.image(1, 2), unknown], has_next=False))
    h = harness_factory(downloader=gp.downloader(site))
    batch_id = h.ctx.batches.create(source="instagram_import", status="listing", source_profile="perfil_teste",
                                    content_type="videos", requested_limit=100)
    h.run_imports()
    row = state(h, batch_id)
    assert (row["total"], row["found"]) == (0, 0)
    assert row["reasons"] == {"expected": {"content_filter": 3, "unsupported_type": 1}}
    assert h.batch(batch_id)["listing_dropped"] == ZERO


# --------------------------------------------------------------------------- checkpoint, retomada e reinício


def test_page_redone_after_hard_backpressure_counts_its_discard_once(harness_factory, monkeypatch):
    tight_backpressure(monkeypatch)
    site = gp.Site(gp.body(1, [no_url(1, 0)] + [gp.image(1, i) for i in range(1, 12)]),
                   gp.body(2, [gp.image(2, 0), no_url(2, 1), gp.image(2, 2)], has_next=False))
    h = harness_factory(downloader=gp.downloader(site))
    batch_id = gp._batch(h)
    h.run_imports()
    row = state(h, batch_id)
    # Parou no meio da página 1 (sem checkpoint): nada somado ainda; o descarte fica só como estado da página.
    assert (row["listing_state"], row["next_page"], row["found"]) == ("waiting_backpressure", None, 5)
    assert (row["total"], row["reasons"], row["partial"]) == (0, None, {"image_no_url": 1})
    # A API já mostra o descarte da página aberta (como pendente), para não ficar invisível.
    assert h.batch(batch_id)["listing_dropped"] == api(1, {"image_no_url": 1}, pending=1)
    run_until_listed(h, batch_id)
    row = state(h, batch_id)
    assert site.afters.count(None) == 3              # a página 1 foi pedida 3 vezes (refeita duas)...
    assert (row["total"], row["reasons"]) == (2, {"unexpected": {"image_no_url": 2}})   # ...e contou uma vez
    assert (row["partial"], row["found"], row["listing_state"]) == (None, 13, "done")


def test_app_restart_in_the_middle_of_the_listing_keeps_the_right_total(harness_factory, monkeypatch):
    tight_backpressure(monkeypatch)                  # um trecho por página: o "reinício" cai entre duas páginas
    site = gp.Site(gp.body(1, [gp.image(1, 0), no_url(1, 1), gp.image(1, 2), gp.image(1, 3)]),
                   gp.body(2, edges=[{"node": None}, {"node": gp.image(2, 1)}, {"node": no_url(2, 2)}]),
                   gp.body(3, [gp.image(3, 0)], has_next=False))
    first = harness_factory(downloader=gp.downloader(site))
    batch_id = gp._batch(first)
    first.run_imports()
    row = state(first, batch_id)
    assert (row["listing_state"], row["next_page"], row["total"]) == ("waiting_backpressure", 2, 1)
    site.afters.clear()
    second = harness_factory(root=first.root, downloader=gp.downloader(site))   # novo contexto, mesmo banco
    run_until_listed(second, batch_id)
    row = state(second, batch_id)
    assert None not in site.afters                   # a página 1 (já contada) não é pedida de novo
    assert (row["total"], row["found"]) == (3, 5)
    assert row["reasons"] == {"unexpected": {"image_no_url": 2, "node_null": 1}}
    assert second.batch(batch_id)["listing_dropped"]["unexpected_total"] == 3


def _needs_attention_on_page_two(harness_factory):
    """Página 1 (1 descarte) com checkpoint; página 2 (1 descarte) entrega os itens e quebra a paginação."""
    page_two = [no_url(2, 0), gp.image(2, 1), gp.image(2, 2)]
    site = gp.Site(gp.body(1, [gp.image(1, 0), no_url(1, 1), gp.image(1, 2)]),
                   gp.body(2, page_two, end_cursor=None, errors=[{"message": "partial"}]))
    h = harness_factory(downloader=gp.downloader(site))
    batch_id = gp._batch(h)
    h.run_imports()
    row = state(h, batch_id)
    assert (row["status"], row["listing_state"], row["next_page"], row["found"]) == ("listing", "needs_attention", 2, 4)
    # A página 2 não ganhou checkpoint: o descarte dela ainda não entrou no total.
    assert (row["total"], row["partial"]) == (1, {"image_no_url": 1})
    assert h.batch(batch_id)["listing_dropped"] == api(2, {"image_no_url": 2}, pending=1)
    site.pages[1] = gp.body(2, page_two, has_next=False)   # o Instagram volta a responder direito
    return h, site, batch_id


def test_resume_listing_after_needs_attention_does_not_count_the_redone_page_twice(harness_factory):
    h, site, batch_id = _needs_attention_on_page_two(harness_factory)
    assert h.client.post(f"/api/batches/{batch_id}/resume-listing").status_code == 200
    h.run_imports()
    row = state(h, batch_id)
    assert site.afters == [None, gp.cursor(1), gp.cursor(1)]
    assert (row["listing_state"], row["total"], row["partial"], row["found"]) == ("done", 2, None, 4)
    assert row["reasons"] == {"unexpected": {"image_no_url": 2}}


def test_restart_listing_zeroes_the_counters_before_listing_again(harness_factory):
    h, site, batch_id = _needs_attention_on_page_two(harness_factory)
    response = h.client.post(f"/api/batches/{batch_id}/restart-listing", json={"confirm": True})
    assert response.status_code == 200 and response.json()["listing_dropped"] == ZERO
    row = state(h, batch_id)
    assert (row["total"], row["reasons"], row["partial"], row["next_page"]) == (0, None, None, None)
    h.run_imports()
    row = state(h, batch_id)
    assert site.afters == [None, gp.cursor(1), None, gp.cursor(1)]   # tudo listado de novo, da página 1
    assert (row["listing_state"], row["total"], row["found"]) == ("done", 2, 4)   # cada página contou uma vez


def test_limit_reached_inside_the_last_page_counts_only_discards_before_the_last_item(harness_factory):
    # Comportamento escolhido (docs/BATCH_ROBUSTNESS.md): a página em que o limite é atingido nunca ganha checkpoint;
    # entram, uma vez, no encerramento, os descartes dela até o último item gravado. O descarte do edge 1 estava
    # dentro do recorte pedido (conta); o do edge 4 vem depois do último item do lote (não conta).
    last = [gp.image(2, 0), no_url(2, 1), gp.image(2, 2), gp.image(2, 3), no_url(2, 4), gp.image(2, 5)]
    site = gp.Site(gp.body(1, gp.images(1, 3)), gp.body(2, last), gp.body(3, gp.images(3, 3), has_next=False))
    h = harness_factory(downloader=gp.downloader(site))
    batch_id = gp._batch(h, limit=6)
    h.run_imports()
    row = state(h, batch_id)
    assert (row["status"], row["listing_state"], row["found"], row["next_page"]) == ("running", "done", 6, 2)
    assert (row["total"], row["reasons"], row["partial"]) == (1, {"unexpected": {"image_no_url": 1}}, None)
    assert site.afters == [None, gp.cursor(1)]
    # Reinício depois do encerramento: nada é listado nem somado de novo.
    again = harness_factory(root=h.root, downloader=gp.downloader(site))
    again.run_imports()
    assert state(again, batch_id)["total"] == 1 and site.afters == [None, gp.cursor(1)]
    assert again.batch(batch_id)["listing_dropped"] == api(1, {"image_no_url": 1})


def test_discard_after_the_last_item_of_the_limit_is_outside_the_requested_slice(harness_factory):
    site = gp.Site(gp.body(1, [gp.image(1, 0), gp.image(1, 1), no_url(1, 2)]), gp.body(2, gp.images(2, 3)))
    h = harness_factory(downloader=gp.downloader(site))
    batch_id = gp._batch(h, limit=2)
    h.run_imports()
    row = state(h, batch_id)
    # O limite fechou no 2o item; o provider para ali (sem checkpoint) e o descarte do edge 2, depois do último item,
    # fica fora: ele não tirou nenhuma mídia do recorte pedido.
    assert (row["listing_state"], row["found"], row["total"], row["partial"]) == ("done", 2, 0, None)


# --------------------------------------------------------------------------- API: lote antigo (banco sem as colunas)


def test_old_batch_gets_zeroed_counters_after_the_migration(harness_factory, tmp_path):
    root = tmp_path / "antigo"
    root.mkdir()
    old_migrations = tmp_path / "migrations_ate_0010"
    old_migrations.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if not path.name.startswith("0011_"):
            shutil.copy(path, old_migrations / path.name)
    db = Database(make_settings(root).database_path)
    try:
        apply_migrations(db, old_migrations)
        columns = {row["name"] for row in db.fetchall("PRAGMA table_info(batches)")}
        assert not any(name.startswith("listing_dropped") for name in columns)
        batch_id = BatchRepository(db).create(source="instagram_import", status="completed",
                                             source_profile="perfil_teste", content_type="all", requested_limit=10)
    finally:
        db.close_all()
    h = harness_factory(root=root)   # a inicialização aplica a 0011 sobre o banco antigo
    assert h.batch(batch_id)["listing_dropped"] == ZERO
    assert h.client.get("/api/batches").json()["items"][0]["listing_dropped"] == ZERO
    row = state(h, batch_id)
    assert (row["total"], row["reasons"], row["partial"]) == (0, None, None)


# --------------------------------------------------------------------------- revisão D1: página sem checkpoint


BROKEN = {"end_cursor": None, "errors": [{"message": "partial"}]}   # paginação quebrada depois de entregar os nós


def test_discard_after_the_last_item_of_a_page_that_ends_with_error_is_counted(harness_factory):
    # Repro A: a 1a página entrega 2 itens, descarta o 3o e quebra a paginação. Mudança confirmada pelo dono: o fim
    # anômalo não encerra mais a listagem (antes: `done` + lote `running`); o lote fica em `needs_attention` com o
    # erro visível e o descarte já aparece na API como parcial da página aberta (não há item depois dele para
    # carregá-lo). Cancelado o lote (a página não será refeita), ele entra no total, uma única vez.
    site = gp.Site(gp.body(1, [gp.image(1, 0), gp.image(1, 1), no_url(1, 2)], **BROKEN))
    h = harness_factory(downloader=gp.downloader(site))
    batch_id = gp._batch(h)
    h.run_imports()
    row = state(h, batch_id)
    assert (row["status"], row["listing_state"], row["error_code"], row["found"]) == (
        "listing", "needs_attention", "contract_changed", 2)
    assert (row["total"], row["reasons"], row["partial"]) == (0, None, {"image_no_url": 1})
    assert h.batch(batch_id)["listing_dropped"] == api(1, {"image_no_url": 1}, pending=1)
    again = harness_factory(root=h.root, downloader=gp.downloader(site))   # reinício não soma nem pede de novo
    again.run_imports()
    assert state(again, batch_id)["partial"] == {"image_no_url": 1} and site.afters == [None]
    assert again.client.post(f"/api/batches/{batch_id}/cancel").status_code == 200
    row = state(again, batch_id)
    assert (row["total"], row["reasons"], row["partial"]) == (1, {"unexpected": {"image_no_url": 1}}, None)
    assert again.batch(batch_id)["listing_dropped"] == api(1, {"image_no_url": 1})


def test_page_with_only_discards_that_ends_with_error_and_no_item_fails_but_keeps_the_count(harness_factory):
    site = gp.Site(gp.body(1, [no_url(1, 0), no_url(1, 1)], **BROKEN))
    h = harness_factory(downloader=gp.downloader(site))
    batch_id = gp._batch(h)
    h.run_imports()
    row = state(h, batch_id)
    assert (row["status"], row["error_code"], row["found"]) == ("failed", "contract_changed", 0)
    assert (row["total"], row["partial"]) == (2, None)


def _needs_attention_with_nothing_delivered(harness_factory):
    """Repro B: página 1 limpa; página 2 só com descartes (nenhum item entregue) e paginação quebrada."""
    page_two = [no_url(2, 0), no_url(2, 1)]
    site = gp.Site(gp.body(1, gp.images(1, 3)), gp.body(2, page_two, **BROKEN))
    h = harness_factory(downloader=gp.downloader(site))
    batch_id = gp._batch(h)
    h.run_imports()
    row = state(h, batch_id)
    assert (row["status"], row["listing_state"], row["next_page"], row["found"]) == ("listing", "needs_attention", 2, 3)
    # Fica como estado da página aberta (pode ser refeita) e JÁ aparece na API, como pendente.
    assert (row["total"], row["partial"]) == (0, {"image_no_url": 2})
    assert h.batch(batch_id)["listing_dropped"] == api(2, {"image_no_url": 2}, pending=2)
    assert h.client.get("/api/batches").json()["items"][0]["listing_dropped"]["unexpected_total"] == 2
    return h, site, batch_id, page_two


def test_needs_attention_page_redone_with_success_replaces_the_pending_count(harness_factory):
    h, site, batch_id, page_two = _needs_attention_with_nothing_delivered(harness_factory)
    site.pages[1] = gp.body(2, page_two + [gp.image(2, 2)], has_next=False)
    assert h.client.post(f"/api/batches/{batch_id}/resume-listing").status_code == 200
    h.run_imports()
    row = state(h, batch_id)
    assert (row["listing_state"], row["total"], row["partial"], row["found"]) == ("done", 2, None, 4)
    assert h.batch(batch_id)["listing_dropped"] == api(2, {"image_no_url": 2})


def test_needs_attention_page_that_breaks_again_does_not_add_up(harness_factory):
    h, site, batch_id, _ = _needs_attention_with_nothing_delivered(harness_factory)
    for _ in range(2):
        assert h.client.post(f"/api/batches/{batch_id}/resume-listing").status_code == 200
        h.ctx.db.execute("UPDATE batches SET listing_retry_at = NULL WHERE id = ?", (batch_id,))
        h.run_imports()
    row = state(h, batch_id)
    assert site.afters.count(gp.cursor(1)) >= 2                      # a página 2 foi pedida de novo...
    assert (row["total"], row["partial"]) == (0, {"image_no_url": 2})   # ...e o pendente continua 2, não 4 nem 6
    assert h.batch(batch_id)["listing_dropped"] == api(2, {"image_no_url": 2}, pending=2)


def test_app_restart_while_waiting_attention_keeps_the_pending_count_once(harness_factory):
    h, site, batch_id, page_two = _needs_attention_with_nothing_delivered(harness_factory)
    second = harness_factory(root=h.root, downloader=gp.downloader(site))
    assert second.batch(batch_id)["listing_dropped"] == api(2, {"image_no_url": 2}, pending=2)
    site.pages[1] = gp.body(2, page_two, has_next=False)
    assert second.client.post(f"/api/batches/{batch_id}/resume-listing").status_code == 200
    second.run_imports()
    row = state(second, batch_id)
    assert (row["listing_state"], row["total"], row["partial"]) == ("done", 2, None)


def test_cancelling_with_an_open_page_adds_its_discards_once(harness_factory):
    h, _site, batch_id, _ = _needs_attention_with_nothing_delivered(harness_factory)
    response = h.client.post(f"/api/batches/{batch_id}/cancel")
    assert response.status_code == 200 and response.json()["listing_dropped"] == api(2, {"image_no_url": 2})
    row = state(h, batch_id)
    assert (row["status"], row["total"], row["reasons"], row["partial"]) == (
        "cancelled", 2, {"unexpected": {"image_no_url": 2}}, None)
    h.run_imports()
    assert state(h, batch_id)["total"] == 2


def test_cancelling_after_hard_backpressure_in_the_middle_of_a_page_adds_what_was_listed(harness_factory, monkeypatch):
    tight_backpressure(monkeypatch)
    site = gp.Site(gp.body(1, [no_url(1, 0)] + [gp.image(1, i) for i in range(1, 12)], has_next=False))
    h = harness_factory(downloader=gp.downloader(site))
    batch_id = gp._batch(h)
    h.run_imports()
    assert state(h, batch_id)["partial"] == {"image_no_url": 1}
    assert h.client.post(f"/api/batches/{batch_id}/cancel").status_code == 200
    row = state(h, batch_id)
    assert (row["status"], row["total"], row["partial"]) == ("cancelled", 1, None)


# --------------------------------------------------------------------------- revisão D4: posição do edge no limite


def test_dropped_until_counts_only_discards_up_to_the_edge():
    events = [(1, "node_null", 1), (4, "edge_null", 1), (2, "carousel_children_missing", 2),
              (0, "rendition_candidates_invalid", 1), (-1, "image_no_url", 1)]
    assert _dropped_until(events, 3, DEGRADED_REASONS) == {
        "node_null": 1, "carousel_children_missing": 2, "image_no_url": 1}
    assert _dropped_until(events, 0, DEGRADED_REASONS) == {"image_no_url": 1}   # só o de posição desconhecida
    assert _dropped_until(events, None, DEGRADED_REASONS) == {
        "node_null": 1, "edge_null": 1, "carousel_children_missing": 2, "image_no_url": 1}
    assert _dropped_until([(4, "edge_null", 1)], 3, DEGRADED_REASONS) is None


def test_limit_inside_the_page_counts_null_edges_before_the_last_item_only(harness_factory):
    edges = [{"node": gp.image(1, 0)}, {"node": None}, {"node": gp.image(1, 2)}, {"node": gp.image(1, 3)}, None,
             {"node": None}, {"node": gp.image(1, 6)}]
    site = gp.Site(gp.body(1, edges=edges), gp.body(2, gp.images(2, 3), has_next=False))
    h = harness_factory(downloader=gp.downloader(site))
    batch_id = gp._batch(h, limit=3)
    h.run_imports()
    row = state(h, batch_id)
    # O limite fechou no edge 3: conta o nó nulo do edge 1; os dos edges 4 e 5 vêm depois do último item do lote.
    assert (row["listing_state"], row["found"], row["total"]) == ("done", 3, 1)
    assert row["reasons"] == {"unexpected": {"node_null": 1}} and site.afters == [None]


# --------------------------------------------------------------------------- revisão: JSON corrompido no banco


def test_corrupted_counters_json_breaks_neither_the_api_nor_the_listing(harness_factory, monkeypatch, caplog):
    monkeypatch.setattr(batches_repo, "_bad_counts_logged", False)
    site = gp.Site(gp.body(1, [gp.image(1, 0), no_url(1, 1)]), gp.body(2, [gp.image(2, 0), no_url(2, 1)], has_next=False))
    h = harness_factory(downloader=gp.downloader(site))
    batch_id = gp._batch(h)
    h.ctx.db.execute("UPDATE batches SET listing_dropped_unexpected = 3, listing_dropped_reasons = '{oops',"
                     " listing_dropped_partial = '[1, 2]' WHERE id = ?", (batch_id,))
    assert h.batch(batch_id)["listing_dropped"] == api(3, {})
    assert h.client.get("/api/batches").status_code == 200
    h.run_imports()   # a listagem segue; o JSON ilegível é tratado como vazio e regravado a partir daqui
    row = state(h, batch_id)
    assert (row["listing_state"], row["found"], row["total"], row["partial"]) == ("done", 2, 2, None)
    assert row["reasons"] == {"unexpected": {"image_no_url": 2}}
    warnings = [r for r in caplog.records if "Contadores de descartes da listagem ilegíveis" in r.getMessage()]
    assert len(warnings) == 1
