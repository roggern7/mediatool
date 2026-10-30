"""Filtro de visualizações mínimas no lote: vereditos somados uma única vez por página, como os descartes.

Offline (provider web real sobre httpx.MockTransport + import runner + banco de teste); só valores FAKE. Os vereditos
da página entram no lote (`listing_views`) na transação do checkpoint dela; página refeita não soma duas vezes; a
página sem checkpoint fica como estado (`listing_views_partial`) e entra uma vez no encerramento ou cancelamento.
"""

from __future__ import annotations

import json
import logging

import pytest

import test_graphql_partial_errors as gp
from test_graphql_partial_errors import _nothing_sensitive_in_any_log  # noqa: F401 - autouse também neste arquivo
from test_listing_dropped_counters import WebListingFakeDownload, drain, tight_backpressure
from reels_tab_fakes import MirroredSite
from test_web_provider_min_views import HIGH, LOW, MIN, reel

@pytest.fixture(autouse=True)
def _views_filter_on(monkeypatch):
    """O filtro está desligado no app (import_service.MIN_VIEWS_ENABLED); aqui o backend dele é testado ligado."""
    from app.services import import_service

    monkeypatch.setattr(import_service, "MIN_VIEWS_ENABLED", True)


BROKEN = {"end_cursor": None, "errors": [{"message": "partial"}]}   # paginação quebrada depois de entregar os nós


def views(approved: int = 0, below: int = 0, no_count: int = 0) -> dict:
    return {"approved": approved, "below": below, "no_count": no_count, "out_of_period": 0}


def api(approved: int = 0, below: int = 0, no_count: int = 0, pending: int = 0) -> dict:
    return {"min_views": MIN, "posted_within_days": None, "posted_after": None, "analyzed": approved + below + no_count,
            "approved": approved, "below": below, "no_count": no_count, "out_of_period": 0, "pending": pending,
            "max_views": None, "above": 0}


def batch(h, limit: int | None = 100, content: str = "reels", min_views: int | None = MIN) -> int:
    return h.ctx.batches.create(source="instagram_import", status="listing", source_profile="perfil_teste",
                                content_type=content, requested_limit=limit, min_views=min_views)


def state(h, batch_id: int) -> dict:
    row = dict(h.ctx.db.fetchone(
        "SELECT status, listing_state, found, listing_next_page AS next_page, min_views, listing_views AS total,"
        " listing_views_partial AS partial FROM batches WHERE id = ?", (batch_id,)))
    for key in ("total", "partial"):
        row[key] = json.loads(row[key]) if row[key] else None
    return row


def run_until_listed(h, batch_id: int, rounds: int = 12) -> None:
    for _ in range(rounds):
        drain(h, batch_id)
        h.run_imports()
        if state(h, batch_id)["listing_state"] == "done":
            return
    raise AssertionError("a listagem não terminou")


def two_pages() -> gp.Site:
    return MirroredSite(gp.body(1, [reel(1, 0, play_count=HIGH), reel(1, 1, play_count=LOW), reel(1, 2)]),
                   gp.body(2, [reel(2, 0, play_count=MIN), reel(2, 1, like_count=HIGH)], has_next=False))


def test_each_page_counts_once_and_the_api_shows_analyzed_approved_and_downloaded(harness_factory):
    site = two_pages()
    h = harness_factory(downloader=WebListingFakeDownload(gp.downloader(site)))
    batch_id = batch(h)
    h.run_imports()
    row = state(h, batch_id)
    assert (row["listing_state"], row["found"], row["total"], row["partial"]) == ("done", 2, views(2, 1, 2), None)
    assert h.batch(batch_id)["views_filter"] == api(2, 1, 2)
    h.run_jobs()
    done = h.batch(batch_id)
    assert (done["status"], done["progress"]["downloaded"]) == ("completed", 2)
    assert done["views_filter"] == api(2, 1, 2)
    assert h.client.get("/api/batches").json()["items"][0]["views_filter"] == api(2, 1, 2)


def test_limit_inside_the_last_page_adds_the_partial_once(harness_factory):
    site = MirroredSite(gp.body(1, [reel(1, 0, play_count=HIGH), reel(1, 1)]),
                   gp.body(2, [reel(2, 0, play_count=LOW), reel(2, 1, play_count=HIGH), reel(2, 2, play_count=HIGH)]),
                   gp.body(3, [reel(3, 0, play_count=HIGH)], has_next=False))
    h = harness_factory(downloader=gp.downloader(site))
    batch_id = batch(h, limit=2)
    h.run_imports()
    row = state(h, batch_id)
    # Página 2 sem checkpoint (limite no 2o item dela): entram só os vereditos até o último item gravado.
    assert (row["listing_state"], row["found"], row["next_page"]) == ("done", 2, 2)
    assert (row["total"], row["partial"]) == (views(2, 1, 1), None)
    assert site.afters == [None, gp.cursor(1)]
    again = harness_factory(root=h.root, downloader=gp.downloader(site))   # reinício: nada é somado de novo
    again.run_imports()
    assert state(again, batch_id)["total"] == views(2, 1, 1) and again.batch(batch_id)["views_filter"] == api(2, 1, 1)


def test_page_redone_after_hard_backpressure_counts_once(harness_factory, monkeypatch):
    tight_backpressure(monkeypatch)
    page = [reel(1, i, play_count=HIGH if i % 2 == 0 else LOW) for i in range(14)]   # 7 aprovados, 7 abaixo
    site = MirroredSite(gp.body(1, page), gp.body(2, [reel(2, 0)], has_next=False))
    h = harness_factory(downloader=gp.downloader(site))
    batch_id = batch(h)
    h.run_imports()
    row = state(h, batch_id)
    # Parou no meio da página 1 (sem checkpoint): nada somado; os vereditos ficam só como estado da página.
    assert (row["listing_state"], row["next_page"], row["found"], row["total"]) == (
        "waiting_backpressure", None, 5, None)
    assert row["partial"] == views(5, 4, 0)
    assert h.batch(batch_id)["views_filter"] == api(5, 4, 0, pending=9)
    run_until_listed(h, batch_id)
    row = state(h, batch_id)
    assert site.afters.count(None) >= 2, "a página 1 foi refeita..."
    assert (row["total"], row["partial"], row["found"]) == (views(7, 7, 1), None, 7)   # ...e contou uma vez


def test_app_restart_between_pages_keeps_the_right_total(harness_factory, monkeypatch):
    tight_backpressure(monkeypatch)                  # um trecho por página: o "reinício" cai entre duas páginas
    site = MirroredSite(gp.body(1, [reel(1, i, play_count=HIGH) for i in range(4)] + [reel(1, 4, play_count=LOW)]),
                   gp.body(2, [reel(2, 0), reel(2, 1, play_count=HIGH)], has_next=False))
    first = harness_factory(downloader=gp.downloader(site))
    batch_id = batch(first)
    first.run_imports()
    row = state(first, batch_id)
    assert (row["listing_state"], row["next_page"], row["total"]) == ("waiting_backpressure", 2, views(4, 1, 0))
    site.afters.clear()
    second = harness_factory(root=first.root, downloader=gp.downloader(site))   # novo contexto, mesmo banco
    run_until_listed(second, batch_id)
    row = state(second, batch_id)
    assert None not in site.afters                   # a página 1 (já contada) não é pedida de novo
    assert (row["total"], row["found"]) == (views(5, 1, 1), 5)


def _needs_attention_on_page_two(harness_factory):
    page_two = [reel(2, 0, play_count=HIGH), reel(2, 1, play_count=LOW), reel(2, 2)]
    site = MirroredSite(gp.body(1, [reel(1, 0, play_count=HIGH), reel(1, 1)]), gp.body(2, page_two, **BROKEN))
    h = harness_factory(downloader=gp.downloader(site))
    batch_id = batch(h)
    h.run_imports()
    row = state(h, batch_id)
    assert (row["status"], row["listing_state"], row["next_page"], row["found"]) == ("listing", "needs_attention", 2, 2)
    # Página 2 sem checkpoint: os vereditos dela (até o último nó consumido) ficam como estado, já visíveis na API.
    assert (row["total"], row["partial"]) == (views(1, 0, 1), views(1, 1, 1))
    assert h.batch(batch_id)["views_filter"] == api(2, 1, 2, pending=3)
    return h, site, batch_id, page_two


def test_resume_after_needs_attention_does_not_count_the_redone_page_twice(harness_factory):
    h, site, batch_id, page_two = _needs_attention_on_page_two(harness_factory)
    site.pages[1] = gp.body(2, page_two, has_next=False)
    assert h.client.post(f"/api/batches/{batch_id}/resume-listing").status_code == 200
    h.run_imports()
    row = state(h, batch_id)
    assert site.afters == [None, gp.cursor(1), gp.cursor(1)]
    assert (row["listing_state"], row["total"], row["partial"], row["found"]) == ("done", views(2, 1, 2), None, 2)


def test_restart_listing_zeroes_the_counters(harness_factory):
    h, site, batch_id, page_two = _needs_attention_on_page_two(harness_factory)
    site.pages[1] = gp.body(2, page_two, has_next=False)
    response = h.client.post(f"/api/batches/{batch_id}/restart-listing", json={"confirm": True})
    assert response.status_code == 200 and response.json()["views_filter"] == api()
    assert (state(h, batch_id)["total"], state(h, batch_id)["partial"], state(h, batch_id)["min_views"]) == (
        None, None, MIN)
    h.run_imports()
    row = state(h, batch_id)
    assert (row["listing_state"], row["total"], row["found"]) == ("done", views(2, 1, 2), 2)   # cada página uma vez


def test_cancelling_with_an_open_page_adds_its_verdicts_once_and_creates_no_job(harness_factory):
    h, _site, batch_id, _ = _needs_attention_on_page_two(harness_factory)
    jobs_before = h.ctx.jobs.count_for_batch(batch_id)
    response = h.client.post(f"/api/batches/{batch_id}/cancel")
    assert response.status_code == 200 and response.json()["views_filter"] == api(2, 1, 2)
    row = state(h, batch_id)
    assert (row["status"], row["total"], row["partial"]) == ("cancelled", views(2, 1, 2), None)
    h.run_imports()
    assert state(h, batch_id)["total"] == views(2, 1, 2) and h.ctx.jobs.count_for_batch(batch_id) == jobs_before


def test_cancel_during_the_listing_stops_at_the_page_boundary(harness_factory):
    site = MirroredSite(gp.body(1, [reel(1, 0, play_count=HIGH), reel(1, 1, play_count=LOW)]),
                   gp.body(2, [reel(2, 0, play_count=HIGH)], has_next=False))
    h = harness_factory(downloader=gp.downloader(site))
    batch_id = batch(h)
    real_flush = h.ctx.import_runner._flush

    def flush_then_cancel(b, buffer, checkpoint):
        created = real_flush(b, buffer, checkpoint)
        if checkpoint is not None:   # o dono cancela logo depois do checkpoint da página 1
            h.client.post(f"/api/batches/{batch_id}/cancel")
        return created

    h.ctx.import_runner._flush = flush_then_cancel
    h.run_imports()
    row = state(h, batch_id)
    assert site.afters == [None], "a página 2 não é pedida depois do cancelamento"
    assert (row["status"], row["total"], row["partial"], row["found"]) == ("cancelled", views(1, 1, 0), None, 1)


def test_duplicates_follow_the_filter_and_never_touch_the_library(harness_factory):
    site = MirroredSite(gp.body(1, [reel(1, 0, play_count=HIGH), reel(1, 1, play_count=LOW), reel(1, 2)],
                           has_next=False))
    h = harness_factory(downloader=WebListingFakeDownload(gp.downloader(site)))
    first = batch(h, min_views=None)                 # sem filtro: os 3 vídeos vão para a Biblioteca
    h.run_imports()
    h.run_jobs()
    assert h.batch(first)["progress"]["downloaded"] == 3
    library = [dict(r) for r in h.ctx.db.fetchall("SELECT * FROM media ORDER BY id")]
    second = batch(h)
    h.run_imports()
    h.run_jobs()
    jobs = [dict(r) for r in h.ctx.db.fetchall(
        "SELECT source_post_id, is_duplicate, duplicate_reason, status FROM jobs WHERE batch_id = ?", (second,))]
    # O aprovado vira job duplicado como hoje; o abaixo e o sem contagem não viram job.
    assert jobs == [{"source_post_id": "SYNP01N00", "is_duplicate": 1, "duplicate_reason": "source_id",
                     "status": "completed"}]
    done = h.batch(second)
    assert done["views_filter"] == api(1, 1, 1) and done["progress"]["downloaded"] == 0
    assert [dict(r) for r in h.ctx.db.fetchall("SELECT * FROM media ORDER BY id")] == library


def test_single_warning_when_nothing_was_approved_and_videos_had_no_count(harness_factory, caplog):
    site = MirroredSite(gp.body(1, [reel(1, i) for i in range(5)] + [reel(1, 5, play_count=LOW)], has_next=False))
    h = harness_factory(downloader=gp.downloader(site))
    batch_id = batch(h)
    h.run_imports()
    assert state(h, batch_id)["total"] == views(0, 1, 5)
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno == logging.WARNING and "contagem de visualizações" in r.getMessage()]
    assert len(warnings) == 1 and "5 sem contagem" in warnings[0]
    totals = [r.getMessage() for r in caplog.records if ": filtros: " in r.getMessage()]
    assert totals == [f"Lote {batch_id}: filtros: 6 analisados, 0 aprovados, 1 abaixo do mínimo, 0 acima do máximo, 5 sem contagem, 0 fora do período"]
    assert str(LOW) not in caplog.text and str(MIN) in caplog.text   # só o mínimo configurado, nunca a contagem


def test_totals_log_matches_the_api_and_includes_above_the_maximum(harness_factory, caplog):
    site = MirroredSite(gp.body(1, [reel(1, 0, play_count=HIGH), reel(1, 1, play_count=LOW),
                                    reel(1, 2, play_count=MIN), reel(1, 3)], has_next=False))
    h = harness_factory(downloader=gp.downloader(site))
    batch_id = h.ctx.batches.create(source="instagram_import", status="listing", source_profile="perfil_teste",
                                    content_type="reels", requested_limit=100, min_views=MIN, max_views=MIN)
    h.run_imports()
    api_filter = h.batch(batch_id)["views_filter"]
    assert (api_filter["analyzed"], api_filter["approved"], api_filter["below"], api_filter["above"],
            api_filter["no_count"]) == (4, 1, 1, 1, 1)
    totals = [r.getMessage() for r in caplog.records if ": filtros: " in r.getMessage()]
    assert totals == [f"Lote {batch_id}: filtros: 4 analisados, 1 aprovados, 1 abaixo do mínimo, 1 acima do máximo, "
                      "1 sem contagem, 0 fora do período"]
