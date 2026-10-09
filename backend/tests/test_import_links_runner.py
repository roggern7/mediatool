"""Importação por link: execução no import runner (um link por vez, erros isolados, pausas, retomada, cancelamento,
backpressure, deduplicação), renovação de URL expirada, Biblioteca e exportação. 100% offline, valores FAKE."""

from __future__ import annotations

import io
import threading
import zipfile

import pytest

from app.db.connection import parse_utc, utc_now
from app.integrations.instagram.downloader import InstagramError
from app.jobs.import_runner import ATTENTION_RETRY_SECONDS
from app.jobs.pipeline import JobExecutor
from app.services.instagram_gate import InstagramPaused
from export_helpers import export_to, files_in, prepare, run_exports
from import_links_fakes import (
    OWNER,
    LinksDownloader,
    carousel,
    create_links,
    fresh_url,
    link,
    link_rows,
    links_app,
    listing_state,
    photo,
    reel,
    video,
)

A, B, C, D, E, F = "AAAAA11111", "BBBBB22222", "CCCCC33333", "DDDDD44444", "EEEEE55555", "FFFFF66666"


def statuses(h, batch_id: int) -> list[str]:
    return [row["status"] for row in link_rows(h, batch_id)]


def end_backoff(h) -> None:
    h.ctx.db.execute("UPDATE batches SET listing_retry_at = NULL")   # simula o fim do intervalo de espera


# --------------------------------------------------------------------------- caminho feliz


def test_photo_reel_video_and_whole_carousel_are_imported_in_pasted_order(harness_factory, caplog):
    h, dl = links_app(harness_factory, {A: photo(A), B: reel(B), C: video(C, profile="outro_dono"), D: carousel(D, 4)})
    with caplog.at_level("DEBUG"):
        batch = create_links(h, [link(D), link(B, "reel"), link(A), link(C)])
        h.run_imports()
    assert dl.calls == [D, B, A, C], "uma chamada por link, na ordem colada"
    listed = h.batch(batch["id"])
    assert (listed["status"], listed["found"], listed["listing_state"], listed["listing_end"]) == (
        "running", 7, "done", None)
    assert listed["links"] == {"total": 4, "pending": 0, "resolved": 4, "failed": 0}
    jobs = h.ctx.jobs.for_batch(batch["id"])
    assert [(j["source_post_id"], j["source_media_index"], j["media_type"]) for j in jobs] == [
        (D, 0, "image"), (D, 1, "video"), (D, 2, "image"), (D, 3, "image"), (B, 0, "video"), (A, 0, "image"),
        (C, 0, "video")]
    assert all(j["listing_page_index"] is None and j["listing_page_size"] is None for j in jobs)
    refs = h.ctx.db.fetchall("SELECT * FROM job_remote_refs")
    assert len(refs) == 7 and all(r["listing_cursor"] is None and r["listing_page_index"] is None for r in refs)
    assert [(r["status"], r["items_found"], r["profile"]) for r in link_rows(h, batch["id"])] == [
        ("resolved", 4, OWNER), ("resolved", 1, OWNER), ("resolved", 1, OWNER), ("resolved", 1, "outro_dono")]

    h.run_jobs()
    done = h.batch(batch["id"])
    assert (done["status"], done["error_code"], done["error_message"]) == ("completed", None, None)
    assert done["progress"] == {"found": 7, "downloaded": 7, "processed": 0, "skipped": 0, "failed": 0,
                                "pending": 0, "active": 0}
    assert dl.calls == [D, B, A, C] and h.ctx.batches.has_active_work() is False
    # Biblioteca: todas as mídias, com o perfil do dono da publicação.
    media = h.client.get("/api/media", params={"limit": 50}).json()
    assert media["total"] == 7
    assert {(m["source_post_id"], m["source_media_index"], m["source_profile"]) for m in media["items"]} == {
        (D, 0, OWNER), (D, 1, OWNER), (D, 2, OWNER), (D, 3, OWNER), (B, 0, OWNER), (A, 0, OWNER), (C, 0, "outro_dono")}
    assert h.client.get("/api/media", params={"profile": "outro_dono"}).json()["total"] == 1
    # Log: só ids de lote e contagens; nunca shortcode, link do usuário ou URL de mídia.
    blob = "\n".join(f"{r.getMessage()} {r.args}" for r in caplog.records)
    assert f"Lote {batch['id']}" in blob
    for needle in (A, B, C, D, "instagram.com/p/", "scontent"):
        assert needle not in blob, needle


# --------------------------------------------------------------------------- erros de link


def test_link_errors_in_the_middle_do_not_stop_the_other_links(harness_factory):
    h, dl = links_app(harness_factory, {A: photo(A), E: [], F: carousel(F, 2)})
    dl.errors = {B: [InstagramError("content_unavailable")], C: [InstagramError("profile_private")],
                 D: [InstagramError("unsupported_media")]}
    batch = create_links(h, [link(code) for code in (A, B, C, F, D, E)])
    h.run_all()
    assert dl.calls == [A, B, C, F, D, E], "cada link é pedido uma única vez"
    rows = link_rows(h, batch["id"])
    assert [(r["status"], r["error_code"]) for r in rows] == [
        ("resolved", None), ("failed", "content_unavailable"), ("failed", "profile_private"), ("resolved", None),
        ("failed", "unsupported_media"), ("failed", "content_unavailable")]
    assert all(r["error_message"] for r in rows if r["status"] == "failed")
    assert all(r["items_found"] is None for r in rows if r["status"] == "failed")
    done = h.batch(batch["id"])
    assert (done["status"], done["found"], done["error_code"]) == ("completed", 3, None)
    assert done["error_message"] == "4 de 6 links não puderam ser importados."
    assert done["links"] == {"total": 6, "pending": 0, "resolved": 2, "failed": 4}
    assert done["progress"]["downloaded"] == 3 and done["progress"]["failed"] == 0
    assert h.ctx.worker.downloads_paused() is False


@pytest.mark.parametrize("codes, message", [
    ((A, B), "Nenhum dos 2 links pôde ser importado."),
    ((A,), "O link não pôde ser importado."),
])
def test_all_links_failed_fails_the_batch_with_a_clear_message(harness_factory, codes, message):
    h, dl = links_app(harness_factory, {})
    dl.errors = {A: [InstagramError("profile_not_found")], B: [InstagramError("invalid_input")]}
    batch = create_links(h, [link(code) for code in codes])
    h.run_all()
    done = h.batch(batch["id"])
    assert (done["status"], done["error_code"], done["error_message"]) == ("failed", "links_failed", message)
    assert done["found"] == 0 and done["finished_at"] and done["listing_state"] == "done"
    assert statuses(h, batch["id"]) == ["failed"] * len(codes) and dl.calls == list(codes)
    h.run_imports()
    assert dl.calls == list(codes), "lote encerrado: nada é pedido de novo"
    retry = h.client.post(f"/api/batches/{batch['id']}/retry-failed")
    assert retry.status_code == 409 and retry.json()["code"] == "nothing_to_retry"


# --------------------------------------------------------------------------- erros do lote/ambiente


@pytest.mark.parametrize("error", [InstagramError("rate_limited"), InstagramPaused("instagram")])
def test_instagram_pause_keeps_the_link_pending_and_resumes_alone(harness_factory, error):
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B), C: photo(C)})
    dl.errors = {B: [error]}
    if isinstance(error, InstagramPaused):
        # O portão recusou a requisição: a pausa já estava publicada por outra requisição.
        dl.on_resolve = lambda code: h.ctx.worker.pause_for("rate_limited") if dl.calls == [A, B] else None
    batch = create_links(h, [link(A), link(B), link(C)])
    h.run_imports()
    h.run_imports()
    assert dl.calls == [A, B], "durante a pausa nenhum outro link é pedido"
    paused = h.batch(batch["id"])
    assert (paused["status"], paused["listing_state"], paused["found"]) == ("listing", "paused", 1)
    assert statuses(h, batch["id"]) == ["resolved", "pending", "pending"]
    assert h.ctx.worker.downloads_paused() is True
    if isinstance(error, InstagramError):
        assert paused["error_code"] == "rate_limited" and paused["error_message"]

    assert h.client.delete("/api/settings/instagram/pause").status_code == 200
    h.run_all()
    done = h.batch(batch["id"])
    assert (done["status"], done["found"], done["error_code"], done["error_message"]) == ("completed", 3, None, None)
    assert dl.calls == [A, B, B, C] and statuses(h, batch["id"]) == ["resolved"] * 3


def test_network_error_pauses_with_backoff_and_resumes_from_the_pending_links(harness_factory):
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B), C: photo(C)})
    dl.errors = {B: [InstagramError("network_error"), InstagramError("timeout")]}
    batch = create_links(h, [link(A), link(B), link(C)])
    h.run_all()
    h.run_imports()
    assert dl.calls == [A, B], "dentro do intervalo de espera nada é pedido"
    row = h.ctx.batches.get(batch["id"])
    assert (row["status"], row["listing_state"], row["error_code"], row["listing_retry_level"]) == (
        "listing", "paused", "network_error", 1)
    first_wait = (parse_utc(row["listing_retry_at"]) - parse_utc(utc_now())).total_seconds()
    assert 0 < first_wait <= 60 and h.ctx.worker.downloads_paused() is False
    assert h.batch(batch["id"])["progress"]["downloaded"] == 1, "o que já foi resolvido baixa normalmente"

    end_backoff(h)
    h.run_imports()   # 2a falha seguida sem progresso: o degrau do backoff sobe
    row = h.ctx.batches.get(batch["id"])
    assert (row["listing_state"], row["error_code"], row["listing_retry_level"]) == ("paused", "timeout", 2)
    assert (parse_utc(row["listing_retry_at"]) - parse_utc(utc_now())).total_seconds() > 60
    end_backoff(h)
    h.run_all()
    done = h.batch(batch["id"])
    assert (done["status"], done["found"], done["error_code"]) == ("completed", 3, None)
    assert dl.calls == [A, B, B, B, C], "o link resolvido nunca é pedido de novo"
    assert h.ctx.batches.get(batch["id"])["listing_retry_level"] == 0


@pytest.mark.parametrize("error, code", [
    (InstagramError("contract_changed"), "contract_changed"),
    (InstagramError("session_missing"), "session_missing"),
    (InstagramError("internal_error"), "internal_error"),
    (RuntimeError("falha FAKE"), "internal_error"),
])
def test_systemic_error_needs_attention_without_requesting_the_other_links(harness_factory, error, code):
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B), C: photo(C), D: photo(D)})
    dl.errors = {B: [error]}
    batch = create_links(h, [link(A), link(B), link(C), link(D)])
    h.run_all()
    h.run_imports()
    assert dl.calls == [A, B], "um problema sistêmico não dispara uma requisição por link"
    waiting = h.batch(batch["id"])
    assert (waiting["status"], waiting["listing_state"], waiting["error_code"]) == ("listing", "needs_attention", code)
    assert waiting["error_message"] and "FAKE" not in waiting["error_message"]
    assert statuses(h, batch["id"]) == ["resolved", "pending", "pending", "pending"]
    row = h.ctx.batches.get(batch["id"])
    hours = (parse_utc(row["listing_retry_at"]) - parse_utc(utc_now())).total_seconds() / 3600
    assert 5.9 < hours <= ATTENTION_RETRY_SECONDS / 3600 and row["listing_attention_tries"] == 1
    assert h.ctx.batches.has_active_work() is False and h.ctx.worker.downloads_paused() is False

    resumed = h.client.post(f"/api/batches/{batch['id']}/resume-listing")
    assert resumed.status_code == 200 and resumed.json()["listing_state"] == "pending"
    h.run_all()
    done = h.batch(batch["id"])
    assert (done["status"], done["found"], done["error_code"]) == ("completed", 4, None)
    assert dl.calls == [A, B, B, C, D]
    assert h.ctx.batches.get(batch["id"])["listing_attention_tries"] == 0


def test_needs_attention_retries_automatically_at_most_four_times(harness_factory):
    h, dl = links_app(harness_factory, {A: photo(A)})
    # Erro de sessão nunca marca o link como falho: o lote espera a ação com o link pendente.
    dl.errors = {A: [InstagramError("session_missing") for _ in range(5)]}
    batch = create_links(h, [link(A)])
    for expected in (1, 2, 3, 4):
        h.run_imports()
        row = h.ctx.batches.get(batch["id"])
        assert row["listing_state"] == "needs_attention" and row["listing_attention_tries"] == expected
        assert row["listing_retry_at"] is not None
        h.ctx.db.execute("UPDATE batches SET listing_retry_at = ?", (utc_now(-1),))   # as 6 h passaram
    h.run_imports()
    row = h.ctx.batches.get(batch["id"])
    assert row["listing_state"] == "needs_attention" and row["listing_retry_at"] is None
    h.run_imports()
    assert len(dl.calls) == 5, "tentativas automáticas esgotadas: só 'Retomar' volta a pedir"
    assert h.client.post(f"/api/batches/{batch['id']}/resume-listing").status_code == 200
    h.run_all()
    assert h.batch(batch["id"])["status"] == "completed" and len(dl.calls) == 6
    assert statuses(h, batch["id"]) == ["resolved"]


def _resume(h, batch_id: int) -> None:
    assert h.client.post(f"/api/batches/{batch_id}/resume-listing").status_code == 200
    h.run_all()


@pytest.mark.parametrize("code", ["contract_changed", "internal_error"])   # access_refused: test_import_links_refused
def test_link_that_always_fails_with_a_batch_error_is_failed_on_the_second_time(harness_factory, code):
    h, dl = links_app(harness_factory, {A: photo(A), C: photo(C)})
    dl.errors = {B: [InstagramError(code) for _ in range(10)]}
    batch = create_links(h, [link(A), link(B), link(C)])
    h.run_all()
    # 1a vez: needs_attention, link pendente, nenhum outro pedido.
    first = h.batch(batch["id"])
    assert (first["listing_state"], first["error_code"]) == ("needs_attention", code)
    assert dl.calls == [A, B] and statuses(h, batch["id"]) == ["resolved", "pending", "pending"]
    # 2a vez do MESMO link: ele fica falho com esse erro e o lote segue para os próximos.
    _resume(h, batch["id"])
    assert dl.calls == [A, B, B, C], "o link travado não é pedido uma 3a vez e os seguintes são pedidos"
    rows = link_rows(h, batch["id"])
    assert [(r["status"], r["error_code"]) for r in rows] == [
        ("resolved", None), ("failed", code), ("resolved", None)]
    assert rows[1]["error_message"]
    done = h.batch(batch["id"])
    assert (done["status"], done["found"], done["error_code"]) == ("completed", 2, None)
    assert done["error_message"] == "1 de 3 links não puderam ser importados."


def test_unknown_post_shape_is_a_link_error_on_the_first_time(harness_factory):
    h, dl = links_app(harness_factory, {A: photo(A), C: photo(C)})
    error = InstagramError("contract_changed")
    error.diagnostic = {"stage": "normalize", "detail": "post_shape_unknown"}
    dl.errors = {B: [error]}
    batch = create_links(h, [link(A), link(B), link(C)])
    h.run_all()
    assert dl.calls == [A, B, C]
    assert [(r["status"], r["error_code"]) for r in link_rows(h, batch["id"])] == [
        ("resolved", None), ("failed", "contract_changed"), ("resolved", None)]
    assert h.batch(batch["id"])["status"] == "completed"


@pytest.mark.parametrize("code", ["session_missing", "session_expired", "checkpoint_required"])
def test_session_errors_never_fail_the_link(harness_factory, code):
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B)})
    dl.errors = {A: [InstagramError(code) for _ in range(3)]}
    batch = create_links(h, [link(A), link(B)])
    h.run_all()
    for attempt in (1, 2, 3):
        waiting = h.batch(batch["id"])
        assert (waiting["status"], waiting["error_code"]) == ("listing", code), attempt
        assert waiting["listing_state"] in ("needs_attention", "paused")
        assert statuses(h, batch["id"]) == ["pending", "pending"] and dl.calls == [A] * attempt
        h.client.delete("/api/settings/instagram/pause")
        _resume(h, batch["id"])
    assert dl.calls == [A, A, A, A, B] and h.batch(batch["id"])["status"] == "completed"
    assert statuses(h, batch["id"]) == ["resolved", "resolved"]


# --------------------------------------------------------------------------- disjuntor de links falhos seguidos


def test_three_failed_links_in_a_row_stop_the_chunk_and_resume_continues(harness_factory):
    h, dl = links_app(harness_factory, {A: photo(A), E: photo(E), F: photo(F)})
    dl.errors = {B: [InstagramError("content_unavailable")], C: [InstagramError("profile_private")]}   # D: sem mídia
    batch = create_links(h, [link(code) for code in (A, B, C, D, E, F)])
    h.run_all()
    h.run_imports()
    assert dl.calls == [A, B, C, D], "depois de 3 falhas seguidas nenhum outro link é pedido"
    stopped = h.batch(batch["id"])
    assert (stopped["status"], stopped["listing_state"], stopped["error_code"]) == (
        "listing", "needs_attention", "links_failing")
    assert stopped["error_message"].startswith("3 links seguidos não puderam ser importados.")
    assert "Retomar" in stopped["error_message"]
    assert statuses(h, batch["id"]) == ["resolved", "failed", "failed", "failed", "pending", "pending"]
    row = h.ctx.batches.get(batch["id"])
    hours = (parse_utc(row["listing_retry_at"]) - parse_utc(utc_now())).total_seconds() / 3600
    assert 5.9 < hours <= 6 and row["listing_attention_tries"] == 1 and row["link_fail_streak"] == 0

    _resume(h, batch["id"])
    assert dl.calls == [A, B, C, D, E, F], "os falhos ficam falhos; segue pelos pendentes"
    done = h.batch(batch["id"])
    assert (done["status"], done["found"], done["error_code"]) == ("completed", 3, None)
    assert done["error_message"] == "3 de 6 links não puderam ser importados."


def test_failed_link_streak_survives_chunks_and_a_resolved_link_resets_it(harness_factory):
    h, dl = links_app(harness_factory, {C: photo(C), F: photo(F)})
    # A e B falham; C resolve (zera); D e E falham; F: rede (o trecho para), depois falha -> 3a seguida entre trechos.
    dl.errors = {F: [InstagramError("network_error"), InstagramError("content_unavailable")]}
    extra = "GGGGG77777"
    batch = create_links(h, [link(code) for code in (A, B, C, D, E, F, extra)])
    h.run_all()
    assert dl.calls == [A, B, C, D, E, F] and listing_state(h, batch["id"]) == "paused"
    assert h.ctx.batches.get(batch["id"])["link_fail_streak"] == 2
    end_backoff(h)
    h.run_all()
    assert dl.calls == [A, B, C, D, E, F, F]
    stopped = h.batch(batch["id"])
    assert (stopped["listing_state"], stopped["error_code"]) == ("needs_attention", "links_failing")
    assert statuses(h, batch["id"])[-1] == "pending"


def test_breaker_counter_restarts_after_resume_and_short_batches_finish_normally(harness_factory):
    h, dl = links_app(harness_factory, {})   # nenhuma publicação responde com mídia
    batch = create_links(h, [link(code) for code in (A, B, C, D, E)])
    h.run_all()
    assert dl.calls == [A, B, C] and h.batch(batch["id"])["error_code"] == "links_failing"
    _resume(h, batch["id"])   # contador zerado: D e E falham (2 seguidas) e os links acabam
    assert dl.calls == [A, B, C, D, E]
    done = h.batch(batch["id"])
    assert (done["status"], done["error_code"]) == ("failed", "links_failed")
    assert done["error_message"] == "Nenhum dos 5 links pôde ser importado."

    # 3 links, todos falhos: os pendentes acabam junto com a 3a falha e o lote conclui normalmente.
    short = create_links(h, [link(code) for code in (A, B, C)])
    h.run_all()
    done = h.batch(short["id"])
    assert (done["status"], done["error_code"], done["listing_state"]) == ("failed", "links_failed", "done")


# --------------------------------------------------------------------------- cancelamento, backpressure, reinício


def test_cancel_in_the_middle_stops_the_requests_and_shows_pending_links_as_cancelled(harness_factory):
    h, dl = links_app(harness_factory, {A: carousel(A, 2), B: photo(B), C: photo(C)})
    batch = create_links(h, [link(A), link(B), link(C)])

    def cancel_on_second(code: str) -> None:
        if code == B:
            assert h.client.post(f"/api/batches/{batch['id']}/cancel").status_code == 200

    dl.on_resolve = cancel_on_second
    h.run_all()
    h.run_all()
    assert dl.calls == [A, B], "depois do cancelamento nenhum link é pedido"
    done = h.batch(batch["id"])
    assert (done["status"], done["listing_state"], done["found"]) == ("cancelled", "cancelled", 2)
    assert done["counts"]["cancelled"] == 2 and dl.downloads == []
    assert [(r["status"], r["items_found"]) for r in link_rows(h, batch["id"])] == [
        ("resolved", 2), ("cancelled", None), ("cancelled", None)]
    # O estado gravado continua `pending` (a contagem do lote reflete o banco); nada de job para o link B.
    assert done["links"] == {"total": 3, "pending": 2, "resolved": 1, "failed": 0}
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM jobs WHERE source_post_id = ?", (B,)) == 0
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM job_remote_refs") == 0


def test_cancel_before_the_first_chunk_requests_nothing(harness_factory):
    h, dl = links_app(harness_factory, {A: photo(A)})
    batch = create_links(h, [link(A)])
    assert h.client.post(f"/api/batches/{batch['id']}/cancel").json()["status"] == "cancelled"
    h.run_all()
    assert dl.calls == [] and statuses(h, batch["id"]) == ["cancelled"]


def test_backpressure_stops_between_links_and_resumes_after_downloads(harness_factory):
    codes = [f"CARRO{i:05d}" for i in range(12)]
    h, dl = links_app(harness_factory, {code: carousel(code, 10) for code in codes})
    batch = create_links(h, [link(code) for code in codes])
    h.run_imports()
    # 9 carrosséis = 90 ingests pendentes: o próximo link passaria do teto de 100.
    assert dl.calls == codes[:9] and listing_state(h, batch["id"]) == "waiting_backpressure"
    waiting = h.batch(batch["id"])
    assert (waiting["status"], waiting["found"], waiting["links"]["pending"]) == ("listing", 90, 3)
    h.run_imports()
    assert dl.calls == codes[:9], "enquanto os downloads não andam, nada é pedido"
    h.run_all()
    h.run_all()
    done = h.batch(batch["id"])
    assert (done["status"], done["found"], done["progress"]["downloaded"]) == ("completed", 120, 120)
    assert dl.calls == codes, "cada link pedido uma única vez"


def test_restart_resumes_only_the_pending_links(harness_factory):
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B), C: carousel(C, 3)})
    dl.errors = {B: [InstagramError("network_error")]}
    batch = create_links(h, [link(A), link(B), link(C)])
    h.run_all()
    assert dl.calls == [A, B] and statuses(h, batch["id"]) == ["resolved", "pending", "pending"]

    dl2 = LinksDownloader(posts=dict(dl.posts))
    h2 = harness_factory(root=h.root, downloader=dl2)   # reinício sobre os mesmos dados
    end_backoff(h2)
    h2.run_all()
    assert dl2.calls == [B, C], "o link já resolvido não é pedido de novo"
    done = h2.batch(batch["id"])
    assert (done["status"], done["found"], done["progress"]["downloaded"]) == ("completed", 5, 5)
    assert dl.downloads.count(f"{A}:0") == 1 and f"{A}:0" not in dl2.downloads


def test_link_requested_again_after_a_crash_does_not_duplicate_jobs(harness_factory):
    h, dl = links_app(harness_factory, {A: carousel(A, 3)})
    batch = create_links(h, [link(A)])
    # Estado de "app caiu depois de criar parte dos jobs": o link segue pendente e um job dele já existe.
    row = h.ctx.batches.get(batch["id"])
    assert h.ctx.import_runner._add_item(row, carousel(A, 3)[1]) is True
    h.run_all()
    assert dl.calls == [A]
    done = h.batch(batch["id"])
    assert (done["status"], done["found"], done["progress"]["downloaded"]) == ("completed", 3, 3)
    assert sorted(j["source_media_index"] for j in h.ctx.jobs.for_batch(batch["id"])) == [0, 1, 2]


def test_rate_limit_wait_is_visible_on_the_link_batch(harness_factory):
    h, dl = links_app(harness_factory, {A: photo(A)})
    batch = create_links(h, [link(A)])
    seen: list[dict] = []

    def wait(code: str) -> None:
        # O portão avisa a espera pelo teto de páginas dentro da requisição do link (thread da listagem).
        h.ctx.import_runner._on_rate_limit_wait("enter", {"seconds": 900, "used": 300, "max": 300})
        seen.append(h.batch(batch["id"]))
        h.ctx.import_runner._on_rate_limit_wait("exit", {"reason": "released", "waited": 900})

    dl.on_resolve = wait
    h.run_all()
    assert (seen[0]["listing_state"], seen[0]["rate_limit_pages_used"], seen[0]["rate_limit_pages_max"]) == (
        "waiting_rate_limit", 300, 300)
    done = h.batch(batch["id"])
    assert done["status"] == "completed" and done["rate_limit_resume_at"] is None


# --------------------------------------------------------------------------- deduplicação


def test_media_already_in_the_library_is_not_downloaded_again(harness_factory, tmp_path):
    h, dl = links_app(harness_factory, {A: carousel(A, 2), B: photo(B)})
    first = create_links(h, [link(A)])
    h.run_all()
    assert h.batch(first["id"])["status"] == "completed" and len(dl.downloads) == 2

    second = create_links(h, [link(B), link(A, "reel")])
    h.run_imports()
    jobs = {(j["source_post_id"], j["source_media_index"]): j for j in h.ctx.jobs.for_batch(second["id"])}
    for index in (0, 1):
        job = jobs[(A, index)]
        assert (job["status"], job["stage"], job["is_duplicate"], job["duplicate_reason"]) == (
            "completed", "skipped", True, "source_id")
    h.run_jobs()
    done = h.batch(second["id"])
    assert (done["status"], done["found"]) == ("completed", 3)
    assert done["progress"]["skipped"] == 2 and done["progress"]["downloaded"] == 1
    assert sorted(dl.downloads) == [f"{A}:0", f"{A}:1", f"{B}:0"], "nenhum download repetido"
    assert h.client.get("/api/media").json()["total"] == 3
    # A mídia que já existia entra na exportação do lote novo.
    dest = prepare(h, tmp_path)
    export = export_to(h, {"kind": "batch", "id": second["id"]}, dest)
    assert (export["status"], export["total"], export["copied"]) == ("completed", 3, 3)
    assert len(files_in(dest)) == 3


def test_same_post_in_two_open_batches_downloads_once(harness_factory):
    h, dl = links_app(harness_factory, {A: photo(A)})
    first = create_links(h, [link(A)])
    second = create_links(h, [link(A)])
    h.run_imports()
    assert dl.calls == [A, A], "uma requisição por link em cada lote"
    job = h.ctx.jobs.for_batch(second["id"])[0]
    assert job["duplicate_reason"] == "duplicate_in_flight" and job["primary_job_id"] == h.ctx.jobs.for_batch(
        first["id"])[0]["id"]
    h.run_jobs()
    h.run_jobs()
    assert [h.batch(b["id"])["status"] for b in (first, second)] == ["completed", "completed"]
    assert dl.downloads == [f"{A}:0"] and h.client.get("/api/media").json()["total"] == 1


# --------------------------------------------------------------------------- renovação de URL expirada


def _expire(dl: LinksDownloader, *items) -> None:
    dl.expired.update(item.media_url for item in items)


def test_expired_url_asks_the_post_once_and_refreshes_all_its_jobs(harness_factory):
    post = carousel(A, 3)
    h, dl = links_app(harness_factory, {A: post, B: photo(B)})
    batch = create_links(h, [link(A), link(B)])
    h.run_imports()
    _expire(dl, *post)
    h.run_jobs()
    assert dl.calls == [A, B, A], "uma única consulta de renovação para a publicação inteira"
    jobs = h.ctx.jobs.for_batch(batch["id"])
    assert all(j["status"] == "completed" and j["attempts"] == 0 for j in jobs)
    assert dl.download_urls[f"{A}:0"] == [post[0].media_url, fresh_url(post[0])]
    assert dl.download_urls[f"{A}:1"] == [fresh_url(post[1])] and dl.download_urls[f"{A}:2"] == [fresh_url(post[2])]
    assert h.batch(batch["id"])["status"] == "completed"


def test_media_missing_from_the_refreshed_post_is_content_unavailable(harness_factory):
    post = carousel(A, 2)
    h, dl = links_app(harness_factory, {A: post})
    dl.missing = {f"{A}:1"}
    batch = create_links(h, [link(A)])
    h.run_imports()
    _expire(dl, post[1])
    h.run_jobs()
    jobs = {j["source_media_index"]: j for j in h.ctx.jobs.for_batch(batch["id"])}
    assert jobs[0]["status"] == "completed"
    assert (jobs[1]["status"], jobs[1]["error_code"]) == ("failed", "content_unavailable")
    assert "publicação" in jobs[1]["error_message"] and "perfil" not in jobs[1]["error_message"].lower()
    assert dl.calls == [A, A] and dl.download_urls[f"{A}:1"] == [post[1].media_url]


def test_url_still_refused_after_refresh_fails_without_a_second_request(harness_factory):
    post = carousel(A, 2)
    h, dl = links_app(harness_factory, {A: post})
    batch = create_links(h, [link(A)])
    h.run_imports()
    _expire(dl, *post)
    dl.expired.update(fresh_url(item) for item in post)
    h.run_jobs()
    jobs = h.ctx.jobs.for_batch(batch["id"])
    assert all((j["status"], j["error_code"], j["attempts"]) == ("failed", "content_unavailable", 1) for j in jobs)
    assert all("perfil" not in j["error_message"].lower() for j in jobs)
    assert dl.calls == [A, A], "o irmão não repete a consulta dentro do intervalo mínimo"
    # Repetir logo em seguida (URLs já apagadas): ainda dentro do intervalo, falha sem nova consulta.
    assert h.client.post(f"/api/batches/{batch['id']}/retry-failed").status_code == 200
    h.run_jobs()
    assert dl.calls == [A, A] and all(j["status"] == "failed" for j in h.ctx.jobs.for_batch(batch["id"]))


@pytest.mark.parametrize("code, paused, status", [
    ("rate_limited", True, "pending"),
    ("contract_changed", False, "failed"),
    ("content_unavailable", False, "failed"),
])
def test_refresh_failure_follows_normal_classification(harness_factory, code, paused, status):
    post = photo(A)
    h, dl = links_app(harness_factory, {A: post})
    batch = create_links(h, [link(A)])
    h.run_imports()
    _expire(dl, *post)
    dl.errors = {A: [InstagramError(code)]}
    h.run_jobs()
    h.run_jobs()
    job = h.ctx.jobs.for_batch(batch["id"])[0]
    assert (job["status"], job["error_code"]) == (status, code)
    assert dl.calls == [A, A] and h.ctx.worker.downloads_paused() is paused
    if status == "pending":
        assert job["stage"] == "waiting_retry" and job["attempts"] == 0


# (`access_refused` saiu daqui: a renovação recusada espera a ação do dono; test_import_links_refresh_refused.py.)
@pytest.mark.parametrize("code", ["content_unavailable", "contract_changed", "network_error"])
def test_failed_refresh_of_a_carousel_makes_a_single_request_for_all_siblings(harness_factory, code):
    post = carousel(A, 10)
    h, dl = links_app(harness_factory, {A: post})
    batch = create_links(h, [link(A)])
    h.run_imports()
    _expire(dl, *post)
    dl.errors = {A: [InstagramError(code) for _ in range(30)]}
    h.run_jobs()
    h.run_jobs()
    assert dl.calls == [A, A], "uma consulta de importação e UMA de renovação para os 10 itens"
    jobs = h.ctx.jobs.for_batch(batch["id"])
    assert len(jobs) == 10 and all(j["error_code"] == code for j in jobs)
    if code == "network_error":
        # Retryable: os irmãos voltam pelo retry controlado, ainda sem nova consulta dentro do intervalo.
        assert all((j["status"], j["stage"]) == ("pending", "waiting_retry") for j in jobs)
        h.ctx.db.execute("UPDATE jobs SET next_attempt_at = NULL WHERE batch_id = ?", (batch["id"],))
        h.run_jobs()
        assert dl.calls == [A, A]
    else:
        assert all(j["status"] == "failed" for j in jobs)
        assert sum("há pouco" in j["error_message"] for j in jobs) == 9, "os irmãos repetem o erro sem requisição"
    assert h.ctx.worker.downloads_paused() is False
    record = h.ctx.jobs.listing_page(batch["id"], 1)
    assert (record["last_refresh_ok"], record["last_refresh_code"], record["refresh_attempts"]) == (0, code, 1)
    # Passado o intervalo mínimo, a renovação volta a ser tentada (uma vez).
    h.ctx.db.execute("UPDATE listing_pages SET last_refresh_at = ?", (utc_now(-3600),))
    # (Na rede, as 20 falhas seguidas de job já pausaram o lote pelo disjuntor existente: simula o fim da pausa.)
    h.ctx.db.execute("UPDATE batches SET paused_until = NULL")
    dl.errors = {}
    h.client.post(f"/api/batches/{batch['id']}/retry-failed")
    h.ctx.db.execute("UPDATE jobs SET next_attempt_at = NULL WHERE batch_id = ?", (batch["id"],))
    h.run_jobs()
    assert dl.calls == [A, A, A] and h.batch(batch["id"])["progress"]["downloaded"] == 10


def test_three_failed_refreshes_in_a_row_pause_the_batch(harness_factory):
    codes = [A, B, C, D]
    h, dl = links_app(harness_factory, {code: photo(code) for code in codes})
    batch = create_links(h, [link(code) for code in codes])
    h.run_imports()
    for code in codes:
        _expire(dl, *dl.posts[code])
    dl.errors = {code: [InstagramError("contract_changed")] for code in codes}
    h.run_jobs()
    assert dl.calls == codes + [A, B, C], "3 renovações seguidas com o mesmo erro pausam o lote"
    paused = h.batch(batch["id"])
    assert paused["paused_reason"] == "contract_changed" and paused["paused_until"]
    assert [j["status"] for j in h.ctx.jobs.for_batch(batch["id"])] == ["failed", "failed", "failed", "pending"]


def test_refresh_waits_for_the_instagram_pause_without_a_request(harness_factory):
    post = photo(A)
    h, dl = links_app(harness_factory, {A: post})
    batch = create_links(h, [link(A)])
    h.run_imports()
    _expire(dl, *post)
    job = h.ctx.jobs.claim(allow_downloads=True, owner="teste")
    assert h.ctx.worker.pause_for("rate_limited") is True   # pausa publicada depois do claim
    JobExecutor(h.ctx, h.ctx.worker, job, threading.Event()).run()
    h.ctx.jobs.release(job["id"], "teste")
    waiting = h.ctx.jobs.get(job["id"])
    assert (waiting["status"], waiting["stage"], waiting["attempts"]) == ("pending", "waiting_retry", 0)
    assert dl.calls == [A], "nenhuma consulta durante a pausa"
    assert h.client.delete("/api/settings/instagram/pause").status_code == 200
    h.run_jobs()
    assert h.ctx.jobs.get(job["id"])["status"] == "completed" and dl.calls == [A, A]
    assert h.batch(batch["id"])["status"] == "completed"


def test_retry_after_urls_were_erased_asks_the_post_again(harness_factory):
    post = carousel(A, 2)
    h, dl = links_app(harness_factory, {A: post})
    dl.download_errors = {f"{A}:1": [InstagramError("content_unavailable")]}
    batch = create_links(h, [link(A)])
    h.run_all()
    done = h.batch(batch["id"])
    assert (done["status"], done["progress"]["failed"]) == ("completed", 1)
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM job_remote_refs") == 0, "lote concluído: URLs apagadas"

    retried = h.client.post(f"/api/batches/{batch['id']}/retry-failed")
    assert retried.status_code == 200 and retried.json()["status"] == "running"
    h.run_jobs()
    assert dl.calls == [A, A], "sem URL guardada, a publicação é consultada de novo"
    done = h.batch(batch["id"])
    assert (done["status"], done["progress"]["failed"], done["progress"]["downloaded"]) == ("completed", 0, 2)
    assert dl.download_urls[f"{A}:1"] == [post[1].media_url, fresh_url(post[1])]
    # Repetir um job (API de jobs) segue o mesmo caminho.
    job = h.ctx.jobs.for_batch(batch["id"])[0]
    assert h.client.post(f"/api/jobs/{job['id']}/retry").status_code == 409   # concluído não repete


# --------------------------------------------------------------------------- exportação, histórico, retenção


def test_completed_link_batch_exports_to_folder_and_zip(harness_factory, tmp_path):
    h, _ = links_app(harness_factory, {A: carousel(A, 3), B: reel(B, profile="outro_dono"), C: photo(C)})
    dest = prepare(h, tmp_path)
    batch = create_links(h, [link(A), link(B, "reel"), link(C)])
    h.run_all()
    assert h.batch(batch["id"])["status"] == "completed"
    source = {"kind": "batch", "id": batch["id"]}

    export = export_to(h, source, dest)
    assert (export["status"], export["mode"], export["total"], export["copied"], export["failed"]) == (
        "completed", "folder", 5, 5, 0)
    names = files_in(dest)
    assert names == sorted([
        f"{OWNER}_2026-01-01_{A}_1.jpg", f"{OWNER}_2026-01-01_{A}_2.mp4", f"{OWNER}_2026-01-01_{A}_3.jpg",
        f"outro_dono_2026-01-01_{B}.mp4", f"{OWNER}_2026-01-01_{C}.jpg"])
    listed = h.client.get("/api/batches", params={"source": "instagram_import"}).json()["items"][0]
    assert listed["id"] == batch["id"] and listed["last_export"]["id"] == export["id"]
    assert listed["last_export"]["copied"] == 5 and listed["import_mode"] == "links"

    created = h.client.post("/api/exports", json={"source": source, "destination": {"zip": True}})
    assert created.status_code == 202, created.text
    run_exports(h)
    zipped = h.client.get(f"/api/exports/{created.json()['id']}").json()
    assert (zipped["status"], zipped["mode"], zipped["copied"], zipped["zip_ready"]) == ("completed", "zip", 5, True)
    response = h.client.get(zipped["download_url"])
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert sorted(archive.namelist()) == names and archive.testzip() is None

    # Retenção: os jobs antigos saem, o lote, os links e a exportação continuam íntegros.
    h.ctx.jobs.purge_finished_before(utc_now(60))
    kept = h.batch(batch["id"])
    assert (kept["status"], kept["found"], kept["links"]["resolved"]) == ("completed", 5, 3)
    assert statuses(h, batch["id"]) == ["resolved"] * 3
    other = tmp_path / "saida2"
    other.mkdir()
    again = export_to(h, source, other)
    assert (again["status"], again["copied"]) == ("completed", 5) and files_in(other) == names
