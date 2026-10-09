"""Lote por link: renovação de URL expirada com `access_refused`. Depois de uma recusa (na resolução de um link ou
numa renovação) nenhuma renovação consulta o Instagram sozinha: no máximo UMA consulta recusada por ação do dono, no
lote inteiro. Lote por perfil e os outros códigos não mudam. 100% offline, valores FAKE."""

from __future__ import annotations

import threading
import time

from app.integrations.instagram.downloader import InstagramError
from app.jobs.pipeline import (
    LINKS_HOLD_DONE_MESSAGE,
    LINKS_HOLD_LISTING_MESSAGE,
    LINKS_REFRESH_BLOCKED_LISTING_MESSAGE,
    LINKS_REFRESH_BLOCKED_MESSAGE,
    RECENT_REFRESH_FAILED_MESSAGE,
    JobExecutor,
)
from import_links_fakes import LinksDownloader, carousel, create_links, link, links_app, photo
from media_fakes import remote
from test_url_refresh import _job as profile_job
from test_url_refresh import _setup as profile_setup

CODES = [f"POST{i:02d}xxxx" for i in range(12)]
A, B, C = "AAAAA11111", "BBBBB22222", "CCCCC33333"
REFUSED_MESSAGE = InstagramError("access_refused").message


def refusals(codes, times: int = 5) -> dict:
    return {code: [InstagramError("access_refused") for _ in range(times)] for code in codes}


def expire_all(dl: LinksDownloader) -> None:
    dl.expired.update(item.media_url for items in dl.posts.values() for item in items)


def jobs_of(h, batch_id: int) -> list[dict]:
    return h.ctx.jobs.for_batch(batch_id)


def not_paused(h, batch_id: int) -> bool:
    batch = h.ctx.batches.get(batch_id)
    return batch["paused_until"] is None and h.ctx.worker.downloads_paused() is False


def test_twelve_expired_posts_and_a_refusal_make_exactly_one_request_per_owner_action(harness_factory, caplog):
    h, dl = links_app(harness_factory, {code: photo(code) for code in CODES})
    batch_id = create_links(h, [link(code) for code in CODES])["id"]
    h.run_imports()
    assert dl.calls == CODES
    expire_all(dl)
    dl.errors = refusals(CODES)
    with caplog.at_level("WARNING"):
        for _ in range(4):
            h.run_all()
    assert dl.calls == CODES + [CODES[0]], "UMA consulta recusada no lote inteiro, não uma por publicação"
    jobs = jobs_of(h, batch_id)
    assert [(j["status"], j["error_code"]) for j in jobs] == [("failed", "access_refused")] * 12
    assert [j["error_message"] for j in jobs] == [REFUSED_MESSAGE] + [LINKS_REFRESH_BLOCKED_MESSAGE] * 11
    assert "Não haverá nova tentativa automática" in LINKS_REFRESH_BLOCKED_MESSAGE
    assert f"Lote {batch_id}: renovação de link recusada (access_refused); aguardando ação manual" in caplog.text
    assert not_paused(h, batch_id), "sem pausa do Instagram nem do lote (nada retoma sozinho)"
    saved = h.ctx.batches.get(batch_id)
    assert saved["status"] == "completed" and saved["fail_streak"] == 0, "nem a recusa nem os bloqueados contam no disjuntor"

    # Ação do dono: exatamente +1 consulta (recusada de novo, para de novo), mesmo logo em seguida.
    assert h.client.post(f"/api/batches/{batch_id}/retry-failed").status_code == 200
    for _ in range(4):
        h.run_all()
    assert len(dl.calls) == 14 and dl.calls[-1] in CODES
    assert [(j["status"], j["error_code"]) for j in jobs_of(h, batch_id)] == [("failed", "access_refused")] * 12
    assert not_paused(h, batch_id)

    # Terceira e quarta ações, ainda recusadas: +1 cada; 3 renovações recusadas seguidas NÃO pausam o lote.
    for expected in (15, 16):
        assert h.client.post(f"/api/batches/{batch_id}/retry-failed").status_code == 200
        for _ in range(3):
            h.run_all()
        assert len(dl.calls) == expected and not_paused(h, batch_id)

    # O Instagram volta a responder: a ação do dono renova tudo (uma consulta por publicação) e o lote conclui.
    dl.errors = {}
    assert h.client.post(f"/api/batches/{batch_id}/retry-failed").status_code == 200
    h.run_all()
    assert sorted(dl.calls[16:]) == CODES
    done = h.batch(batch_id)
    assert (done["status"], done["progress"]["downloaded"], done["progress"]["failed"]) == ("completed", 12, 0)


def test_refused_refresh_of_a_carousel_blocks_the_siblings_until_the_owner_acts(harness_factory):
    post = carousel(A, 10)
    h, dl = links_app(harness_factory, {A: post})
    batch_id = create_links(h, [link(A)])["id"]
    h.run_imports()
    expire_all(dl)
    dl.errors = refusals([A], 30)
    h.run_jobs()
    h.run_jobs()
    assert dl.calls == [A, A], "uma consulta de importação e UMA de renovação para os 10 itens"
    jobs = jobs_of(h, batch_id)
    assert len(jobs) == 10 and all((j["status"], j["error_code"]) == ("failed", "access_refused") for j in jobs)
    assert sum(j["error_message"] == LINKS_REFRESH_BLOCKED_MESSAGE for j in jobs) == 9
    assert not_paused(h, batch_id)
    record = h.ctx.jobs.listing_page(batch_id, 1)
    assert (record["last_refresh_ok"], record["last_refresh_code"], record["refresh_attempts"]) == (
        0, "access_refused", 1)
    # A ação do dono destrava na hora (sem esperar o intervalo mínimo de renovação): uma consulta, e desta vez serve.
    dl.errors = {}
    assert h.client.post(f"/api/batches/{batch_id}/retry-failed").status_code == 200
    h.run_jobs()
    assert dl.calls == [A, A, A] and h.batch(batch_id)["progress"]["downloaded"] == 10


def test_batch_waiting_for_the_owner_after_a_refused_link_does_not_refresh_urls(harness_factory):
    post = carousel(A, 3)
    h, dl = links_app(harness_factory, {A: post, B: photo(B), C: photo(C)})
    dl.errors = refusals([B], 1)
    batch_id = create_links(h, [link(A), link(B), link(C)])["id"]
    h.run_imports()
    saved = h.ctx.batches.get(batch_id)
    assert (saved["listing_state"], saved["error_code"]) == ("needs_attention", "access_refused")
    assert dl.calls == [A, B]
    expire_all(dl)                      # o carrossel já resolvido ficou com as URLs vencidas
    for _ in range(4):
        h.run_all()
    assert dl.calls == [A, B], "nenhuma consulta de renovação enquanto o lote espera a ação do dono"
    jobs = jobs_of(h, batch_id)
    assert [(j["status"], j["error_code"], j["error_message"]) for j in jobs] == [
        ("failed", "access_refused", LINKS_REFRESH_BLOCKED_LISTING_MESSAGE)] * 3
    assert all(j["attempts"] == 1 for j in jobs) and not_paused(h, batch_id)
    assert h.ctx.jobs.listing_page(batch_id, 1) is None, "nenhuma tentativa de renovação registrada"
    saved = h.ctx.batches.get(batch_id)
    assert (saved["status"], saved["listing_state"], saved["listing_retry_at"]) == ("listing", "needs_attention", None)

    # Reinício do app sobre os mesmos dados: continua sem consultar.
    dl2 = LinksDownloader(posts=dict(dl.posts), expired=set(dl.expired), errors=refusals([A, B, C]))
    h2 = harness_factory(root=h.root, downloader=dl2)
    for _ in range(3):
        h2.run_all()
    assert dl2.calls == []


def test_app_restart_in_the_middle_of_a_refused_refresh_makes_no_new_request(harness_factory):
    codes = CODES[:4]
    h, dl = links_app(harness_factory, {code: photo(code) for code in codes})
    batch_id = create_links(h, [link(code) for code in codes])["id"]
    h.run_imports()
    expire_all(dl)
    dl.errors = refusals(codes)
    # Só o primeiro job roda (e é recusado); os outros 3 ainda estão na fila quando o app "reinicia".
    job = h.ctx.jobs.claim(allow_downloads=True, owner="teste")
    JobExecutor(h.ctx, h.ctx.worker, job, threading.Event()).run()
    h.ctx.jobs.release(job["id"], "teste")
    assert dl.calls == codes + [codes[0]]
    assert [j["status"] for j in jobs_of(h, batch_id)] == ["failed", "pending", "pending", "pending"]

    dl2 = LinksDownloader(posts=dict(dl.posts), expired=set(dl.expired), errors=refusals(codes))
    h2 = harness_factory(root=h.root, downloader=dl2)
    for _ in range(3):
        h2.run_all()
    assert dl2.calls == [], "reinício no meio: nenhuma consulta nova sem ação do dono"
    jobs = jobs_of(h2, batch_id)
    assert [(j["status"], j["error_code"]) for j in jobs] == [("failed", "access_refused")] * 4
    assert not_paused(h2, batch_id)
    # A ação do dono, depois do reinício, destrava exatamente uma consulta.
    assert h2.client.post(f"/api/batches/{batch_id}/retry-failed").status_code == 200
    for _ in range(3):
        h2.run_all()
    assert len(dl2.calls) == 1


def test_concurrent_refreshes_of_different_posts_make_a_single_refused_request(harness_factory):
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B)})
    batch_id = create_links(h, [link(A), link(B)])["id"]
    h.run_imports()
    expire_all(dl)
    dl.errors = refusals([A, B])
    dl.on_resolve = lambda code: time.sleep(0.3)   # a 2a thread chega à trava enquanto a 1a ainda consulta
    claimed = [h.ctx.jobs.claim(allow_downloads=True, owner=owner) for owner in ("t1", "t2")]
    threads = [threading.Thread(target=JobExecutor(h.ctx, h.ctx.worker, job, threading.Event()).run)
               for job in claimed]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(dl.calls) == 3, dl.calls
    jobs = jobs_of(h, batch_id)
    assert [(j["status"], j["error_code"]) for j in jobs] == [("failed", "access_refused")] * 2
    assert sorted(j["error_message"] for j in jobs) == sorted([REFUSED_MESSAGE, LINKS_REFRESH_BLOCKED_MESSAGE])


def test_sibling_already_refreshed_still_downloads_while_the_batch_waits(harness_factory):
    """O bloqueio só vale para quem PRECISA consultar: job com URL ainda válida baixa normalmente."""
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B)})
    batch_id = create_links(h, [link(A), link(B)])["id"]
    h.run_imports()
    dl.expired.update(item.media_url for item in dl.posts[A])
    dl.errors = refusals([A])
    h.run_jobs()
    jobs = {j["source_post_id"]: j for j in jobs_of(h, batch_id)}
    assert (jobs[A]["status"], jobs[A]["error_code"]) == ("failed", "access_refused")
    assert jobs[B]["status"] == "completed" and dl.calls == [A, B, A]


def test_profile_batch_with_refused_refresh_keeps_the_previous_behaviour(harness_factory):
    """Lote por PERFIL: sem espera manual. Cada página faz a própria renovação; os irmãos da página repetem o erro."""
    h, dl, batch_id = profile_setup(harness_factory, n=15, refresh_error=InstagramError("access_refused"))
    dl.expired.update(remote(code).media_url for code in ("P0", "P1", "P13"))
    h.run_jobs()
    h.run_jobs()
    assert [ref.page_index for ref in dl.refresh_calls] == [1, 2], "uma renovação por página, as duas consultadas"
    failed = [profile_job(h, batch_id, code) for code in ("P0", "P1", "P13")]
    assert [(j["status"], j["error_code"]) for j in failed] == [("failed", "access_refused")] * 3
    assert [j["error_message"] for j in failed] == [REFUSED_MESSAGE, RECENT_REFRESH_FAILED_MESSAGE, REFUSED_MESSAGE]
    assert h.ctx.worker.downloads_paused() is False
