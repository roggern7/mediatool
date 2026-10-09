"""Lote por link: a recusa em espera é UMA só por lote (`BatchRepository.links_hold`), compartilhada pelo runner
(próximo link) e pela renovação de URL. Qualquer `access_refused` de `resolve_post` a arma sob a trava do lote; só a
ação do dono ("Retomar importação" / "Tentar falhas novamente") a desarma; depois de desarmada, no máximo UMA consulta
recusada acontece no lote inteiro, em qualquer dos dois caminhos. 100% offline, valores FAKE."""

from __future__ import annotations

import threading
import time

from app.db.connection import utc_now
from app.integrations.instagram.downloader import InstagramError
from app.jobs.import_runner import LINKS_REFUSED_MESSAGE
from app.jobs.pipeline import (
    LINKS_HOLD_DONE_MESSAGE,
    LINKS_HOLD_LISTING_MESSAGE,
    LINKS_REFRESH_BLOCKED_LISTING_MESSAGE,
    LINKS_REFRESH_BLOCKED_MESSAGE,
    JobExecutor,
    links_query_lock,
)
from import_links_fakes import LinksDownloader, create_links, link, link_rows, links_app, photo

A, B, C, D = "AAAAA11111", "BBBBB22222", "CCCCC33333", "DDDDD44444"
REFUSED_MESSAGE = InstagramError("access_refused").message


def refusals(codes, times: int = 8) -> dict:
    return {code: [InstagramError("access_refused") for _ in range(times)] for code in codes}


def expire(dl: LinksDownloader, *codes: str) -> None:
    dl.expired.update(item.media_url for code in codes for item in dl.posts[code])


def state(h, batch_id: int) -> tuple:
    row = h.ctx.batches.get(batch_id)
    return row["status"], row["listing_state"], row["error_code"], row["listing_retry_at"]


def resume(h, batch_id: int) -> None:
    assert h.client.post(f"/api/batches/{batch_id}/resume-listing").status_code == 200


def retry_failed(h, batch_id: int) -> None:
    assert h.client.post(f"/api/batches/{batch_id}/retry-failed").status_code == 200


def settle(h, times: int = 4) -> None:
    for _ in range(times):
        h.run_all()


def run_job(h, job: dict, cancel: threading.Event | None = None) -> None:
    JobExecutor(h.ctx, h.ctx.worker, job, cancel or threading.Event()).run()
    h.ctx.jobs.release(job["id"], job["claimed_by"])


def test_labels_in_the_recorded_messages_are_the_real_buttons():
    for message in (LINKS_REFUSED_MESSAGE, LINKS_HOLD_LISTING_MESSAGE, LINKS_REFRESH_BLOCKED_LISTING_MESSAGE):
        assert '"Retomar importação"' in message and "Não haverá nova tentativa automática" in message
    for message in (LINKS_HOLD_DONE_MESSAGE, LINKS_REFRESH_BLOCKED_MESSAGE, LINKS_REFRESH_BLOCKED_LISTING_MESSAGE):
        assert '"Tentar falhas novamente"' in message
    assert "Retomar" not in LINKS_HOLD_DONE_MESSAGE + LINKS_REFRESH_BLOCKED_MESSAGE


# --------------------------------------------------------------------------- cenário 1 da revisão


def test_last_link_refused_twice_keeps_the_hold_and_the_expired_job_does_not_query(harness_factory):
    """[A, B]: A resolvido, B recusado. A URL de A vence. UM "Retomar importação": B é recusado pela 2a vez (link
    falho, era o último pendente, o lote sai de `listing`) e o job de A NÃO consulta em seguida."""
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B)})
    dl.errors = refusals([B])
    batch_id = create_links(h, [link(A), link(B)])["id"]
    h.run_imports()
    assert dl.calls == [A, B] and state(h, batch_id)[1:3] == ("needs_attention", "access_refused")
    expire(dl, A)
    dl.errors[A] = refusals([A])[A]
    settle(h)
    assert dl.calls == [A, B], "com a espera armada ninguém consulta"
    [job] = h.ctx.jobs.for_batch(batch_id)
    assert (job["status"], job["error_code"], job["error_message"]) == (
        "failed", "access_refused", LINKS_REFRESH_BLOCKED_LISTING_MESSAGE)
    # O job falho volta à fila só para a ação seguinte ter um job que precisa renovar (como na reprodução).
    h.ctx.jobs.reset_for_retry([job["id"]])

    resume(h, batch_id)                                   # UMA ação do dono
    settle(h)
    assert dl.calls == [A, B, B], "exatamente 1 consulta (recusada) para a ação; A não é consultada sozinha"
    assert [(r["status"], r["error_code"]) for r in link_rows(h, batch_id)] == [
        ("resolved", None), ("failed", "access_refused")]
    saved = h.ctx.batches.get(batch_id)
    assert (saved["status"], saved["listing_state"], saved["error_code"], saved["error_message"]) == (
        "completed", "done", "access_refused", LINKS_HOLD_DONE_MESSAGE), "a espera não foi liberada de carona"
    [job] = h.ctx.jobs.for_batch(batch_id)
    assert (job["status"], job["error_code"], job["error_message"]) == (
        "failed", "access_refused", LINKS_REFRESH_BLOCKED_MESSAGE)

    # Reinício do app com a espera armada: 0 consultas.
    dl2 = LinksDownloader(posts=dict(dl.posts), expired=set(dl.expired), errors=refusals([A, B]))
    h2 = harness_factory(root=h.root, downloader=dl2)
    settle(h2)
    assert dl2.calls == []

    # "Tentar falhas novamente": exatamente +1 (recusada de novo: arma de novo).
    retry_failed(h2, batch_id)
    settle(h2)
    assert dl2.calls == [A]
    saved = h2.ctx.batches.get(batch_id)
    assert (saved["status"], saved["error_code"], saved["error_message"]) == (
        "completed", "access_refused", LINKS_HOLD_DONE_MESSAGE)
    [job] = h2.ctx.jobs.for_batch(batch_id)
    assert (job["status"], job["error_code"], job["error_message"]) == ("failed", "access_refused", REFUSED_MESSAGE)
    assert saved["fail_streak"] == 0 and saved["paused_until"] is None, "a recusa não conta no disjuntor nem pausa"

    # Repetir só o job (fila) também é ação do dono: +1.
    assert h2.client.post(f"/api/jobs/{job['id']}/retry").status_code == 200
    settle(h2)
    assert dl2.calls == [A, A] and h2.ctx.batches.links_hold(batch_id) is True

    # O Instagram volta a responder: a ação seguinte renova, baixa e o lote conclui sem erro.
    dl2.errors = {}
    retry_failed(h2, batch_id)
    settle(h2)
    assert dl2.calls == [A, A, A]
    done = h2.batch(batch_id)
    assert (done["status"], done["error_code"], done["progress"]["downloaded"]) == ("completed", None, 1)


def test_last_link_refused_twice_without_open_jobs_finishes_normally(harness_factory):
    """Sem job aberto não há o que consultar sozinho: o lote encerra com o aviso normal de links falhos."""
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B)})
    dl.errors = refusals([B])
    batch_id = create_links(h, [link(A), link(B)])["id"]
    settle(h)                                             # o job de A já baixou
    resume(h, batch_id)
    settle(h)
    assert dl.calls == [A, B, B]
    done = h.batch(batch_id)
    assert (done["status"], done["error_code"], done["error_message"]) == (
        "completed", None, "1 de 2 links não puderam ser importados.")


# --------------------------------------------------------------------------- cenário 3 da revisão


def test_worker_reaching_the_refresh_before_the_runner_makes_a_single_request(harness_factory):
    """Depois de "Retomar importação", o worker chega à renovação antes do runner: a renovação recusada arma a
    espera e o runner não pede o link pendente."""
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B), C: photo(C)})
    dl.errors = refusals([B, C])
    batch_id = create_links(h, [link(A), link(B), link(C)])["id"]
    h.run_imports()
    assert dl.calls == [A, B]
    expire(dl, A)
    dl.errors[A] = refusals([A])[A]
    h.ctx.db.execute("UPDATE jobs SET next_attempt_at = ? WHERE batch_id = ?", (utc_now(3600), batch_id))
    settle(h)                                             # o job de A ainda não está na vez
    assert dl.calls == [A, B]
    resume(h, batch_id)
    h.ctx.db.execute("UPDATE jobs SET next_attempt_at = NULL WHERE batch_id = ?", (batch_id,))
    h.run_jobs()                                          # o worker primeiro
    assert dl.calls == [A, B, A]
    for _ in range(4):
        h.run_imports()                                   # depois o runner
        h.run_jobs()
    assert dl.calls == [A, B, A], "uma consulta para a ação; o runner não pede B"
    saved = h.ctx.batches.get(batch_id)
    assert state(h, batch_id) == ("listing", "needs_attention", "access_refused", None)
    assert saved["error_message"] == LINKS_HOLD_LISTING_MESSAGE and saved["listing_attention_tries"] == 0
    assert [r["status"] for r in link_rows(h, batch_id)] == ["resolved", "pending", "pending"]
    assert link_rows(h, batch_id)[1]["error_code"] is None

    # A ordem inversa (runner primeiro) também dá uma consulta só.
    h.ctx.jobs.reset_for_retry(h.ctx.jobs.failed_ids(batch_id))
    resume(h, batch_id)
    h.run_imports()
    settle(h)
    assert dl.calls == [A, B, A, B]
    assert state(h, batch_id) == ("listing", "needs_attention", "access_refused", None)
    assert h.ctx.batches.get(batch_id)["error_message"] == LINKS_REFUSED_MESSAGE


# --------------------------------------------------------------------------- cenário 2 da revisão


def test_refresh_refused_between_chunks_stops_the_runner(harness_factory):
    """Lote ainda listando (parado por falha de rede, com retomada automática): a renovação de A é recusada; o
    runner NÃO pede o próximo link sozinho e o lote passa a esperar "Retomar importação"."""
    h, dl = links_app(harness_factory, {code: photo(code) for code in (A, B, C, D)})
    dl.errors = {B: [InstagramError("network_error")], **refusals([C, D])}
    batch_id = create_links(h, [link(code) for code in (A, B, C, D)])["id"]
    h.run_imports()
    assert dl.calls == [A, B] and state(h, batch_id)[1:3] == ("paused", "network_error")
    expire(dl, A)
    dl.errors[A] = refusals([A])[A]
    h.run_jobs()
    assert dl.calls == [A, B, A] and h.ctx.batches.links_hold(batch_id) is True
    assert h.ctx.batches.get(batch_id)["error_message"] == LINKS_HOLD_LISTING_MESSAGE
    h.ctx.db.execute("UPDATE batches SET listing_retry_at = ? WHERE id = ?", (utc_now(-60), batch_id))
    settle(h)
    assert dl.calls == [A, B, A], "o backoff venceu, mas a espera segura o runner"
    assert state(h, batch_id) == ("listing", "needs_attention", "access_refused", None)
    resume(h, batch_id)
    settle(h)
    assert dl.calls == [A, B, A, B, C], "a ação libera: B responde e C é a única consulta recusada"
    assert state(h, batch_id) == ("listing", "needs_attention", "access_refused", None)


def test_refresh_refused_while_the_runner_resolves_a_link_makes_one_refused_request(harness_factory):
    """A renovação de A corre (thread do worker) enquanto o runner resolve B: as duas consultas nunca ficam em voo
    juntas e, recusada a renovação, o runner não pede C sozinho. Qualquer que seja a ordem na trava: 1 recusada."""
    h, dl = links_app(harness_factory, {code: photo(code) for code in (A, B, C, D)})
    dl.errors = refusals([A, C, D])
    dl.errors[A] = []                                     # a 1a consulta de A (resolução) responde
    workers: list[threading.Thread] = []
    in_flight = {"now": 0, "max": 0}
    guard = threading.Lock()

    def on_resolve(code: str) -> None:
        with guard:
            in_flight["now"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["now"])
        try:
            if code == B and not workers:
                expire(dl, A)
                dl.errors[A] = [InstagramError("access_refused") for _ in range(8)]
                job = h.ctx.jobs.claim(allow_downloads=True, owner="worker")
                thread = threading.Thread(target=run_job, args=(h, job))
                workers.append(thread)
                thread.start()
            time.sleep(0.3)                               # a outra thread chega à trava enquanto esta consulta
        finally:
            with guard:
                in_flight["now"] -= 1

    dl.on_resolve = on_resolve
    batch_id = create_links(h, [link(code) for code in (A, B, C, D)])["id"]
    h.run_imports()
    workers[0].join(15)
    assert not workers[0].is_alive()
    settle(h)
    assert in_flight["max"] == 1, "duas consultas do mesmo lote nunca ficam em voo ao mesmo tempo"
    assert dl.calls[:2] == [A, B] and len(dl.calls) == 3 and dl.calls[2] in (A, C), dl.calls
    assert state(h, batch_id) == ("listing", "needs_attention", "access_refused", None)


# --------------------------------------------------------------------------- concorrência e cancelamento


def test_many_concurrent_jobs_and_the_runner_make_a_single_refused_request(harness_factory):
    codes = [f"POST{i:02d}xxxx" for i in range(8)]
    more = [f"MORE{i:02d}xxxx" for i in range(4)]
    h, dl = links_app(harness_factory, {code: photo(code) for code in codes + more})
    dl.errors = {more[0]: [InstagramError("network_error")]}
    batch_id = create_links(h, [link(code) for code in codes + more])["id"]
    h.run_imports()
    assert dl.calls == codes + [more[0]]
    expire(dl, *codes)
    dl.errors = refusals(codes + more, 40)
    dl.on_resolve = lambda code: time.sleep(0.05)
    h.ctx.db.execute("UPDATE batches SET listing_retry_at = NULL WHERE id = ?", (batch_id,))
    claimed = [h.ctx.jobs.claim(allow_downloads=True, owner=f"t{i}") for i in range(8)]
    assert all(claimed)
    start = threading.Barrier(9)

    def worker(job: dict) -> None:
        start.wait(10)
        run_job(h, job)

    def runner() -> None:
        start.wait(10)
        for _ in range(5):
            h.ctx.import_runner.run_next()

    threads = [threading.Thread(target=worker, args=(job,)) for job in claimed] + [threading.Thread(target=runner)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not any(thread.is_alive() for thread in threads)
    settle(h)
    assert len(dl.calls) == len(codes) + 2, dl.calls[len(codes) + 1:]
    assert state(h, batch_id) == ("listing", "needs_attention", "access_refused", None)
    jobs = h.ctx.jobs.for_batch(batch_id)
    assert [(j["status"], j["error_code"]) for j in jobs] == [("failed", "access_refused")] * 8
    assert h.ctx.batches.get(batch_id)["fail_streak"] == 0 and h.ctx.worker.downloads_paused() is False


def test_job_cancelled_while_waiting_for_the_batch_lock_leaves_quickly(harness_factory):
    h, dl = links_app(harness_factory, {A: photo(A)})
    batch_id = create_links(h, [link(A)])["id"]
    h.run_imports()
    expire(dl, A)
    job = h.ctx.jobs.claim(allow_downloads=True, owner="t1")
    cancel = threading.Event()
    thread = threading.Thread(target=run_job, args=(h, job, cancel))
    with links_query_lock(h.ctx, batch_id, threading.Event()):     # outra consulta do lote está em voo
        thread.start()
        time.sleep(0.4)
        assert thread.is_alive(), "o job espera a trava do lote"
        began = time.monotonic()
        cancel.set()
        thread.join(5)
        assert not thread.is_alive() and time.monotonic() - began < 2
    assert dl.calls == [A] and h.ctx.jobs.get(job["id"])["status"] == "cancelled"


def test_runner_cancelled_while_waiting_for_the_batch_lock_leaves_quickly(harness_factory):
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B)})
    batch_id = create_links(h, [link(A), link(B)])["id"]
    thread = threading.Thread(target=h.ctx.import_runner.run_next)
    with links_query_lock(h.ctx, batch_id, threading.Event()):
        thread.start()
        time.sleep(0.4)
        assert thread.is_alive(), "o runner espera a trava do lote"
        began = time.monotonic()
        assert h.client.post(f"/api/batches/{batch_id}/cancel").status_code == 200
        thread.join(5)
        assert not thread.is_alive() and time.monotonic() - began < 2
    settle(h)
    assert dl.calls == [] and h.batch(batch_id)["status"] == "cancelled"


def test_refusal_during_a_cancelled_job_still_arms_the_hold(harness_factory):
    """Decisão: o job cancelado EXATAMENTE durante a consulta recusada fica `cancelled`, mas a recusa aconteceu: a
    espera do lote é armada do mesmo jeito e nenhum outro job consulta."""
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B)})
    batch_id = create_links(h, [link(A), link(B)])["id"]
    h.run_imports()
    expire(dl, A, B)
    dl.errors = refusals([A, B])
    cancel = threading.Event()
    dl.on_resolve = lambda code: cancel.set()
    job = h.ctx.jobs.claim(allow_downloads=True, owner="t1")
    run_job(h, job, cancel)
    dl.on_resolve = None
    assert h.ctx.jobs.get(job["id"])["status"] == "cancelled" and len(dl.calls) == 3
    assert h.ctx.batches.links_hold(batch_id) is True
    settle(h)
    assert len(dl.calls) == 3
    other = next(j for j in h.ctx.jobs.for_batch(batch_id) if j["id"] != job["id"])
    assert (other["status"], other["error_code"], other["error_message"]) == (
        "failed", "access_refused", LINKS_REFRESH_BLOCKED_MESSAGE)


def test_refusal_while_the_app_is_stopping_still_arms_the_hold(harness_factory):
    """A consulta do runner é recusada com o app encerrando: o trecho é abandonado, mas a espera fica armada e o
    próximo início não consulta nada (e deixa o lote no estado que "Retomar importação" aceita)."""
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B), C: photo(C)})
    dl.errors = refusals([B])
    runner = h.ctx.import_runner

    def stopping(code: str) -> None:
        if code == B:
            runner._stop.set()
            runner._current[1].set()

    dl.on_resolve = stopping
    batch_id = create_links(h, [link(A), link(B), link(C)])["id"]
    h.run_imports()
    assert dl.calls == [A, B] and h.ctx.batches.links_hold(batch_id) is True
    assert [r["status"] for r in link_rows(h, batch_id)] == ["resolved", "pending", "pending"]
    dl2 = LinksDownloader(posts=dict(dl.posts), errors=refusals([A, B, C]))
    h2 = harness_factory(root=h.root, downloader=dl2)
    settle(h2)
    assert dl2.calls == [] and state(h2, batch_id) == ("listing", "needs_attention", "access_refused", None)
    resume(h2, batch_id)
    settle(h2)
    assert dl2.calls == [B]


# --------------------------------------------------------------------------- escopo: um lote não trava o outro


def test_hold_of_one_link_batch_does_not_touch_another(harness_factory):
    h, dl = links_app(harness_factory, {code: photo(code) for code in (A, B, C, D)})
    first = create_links(h, [link(A), link(B)])["id"]
    h.run_imports()
    expire(dl, A, B)
    dl.errors = refusals([A, B])
    settle(h)
    assert dl.calls == [A, B, A] and h.ctx.batches.links_hold(first) is True
    # Outro lote por link: resolve, renova uma URL vencida e conclui normalmente enquanto o primeiro espera.
    second = create_links(h, [link(C), link(D)])["id"]
    h.run_imports()
    expire(dl, C)
    settle(h)
    assert dl.calls == [A, B, A, C, D, C]
    assert h.ctx.batches.links_hold(second) is False and h.batch(second)["status"] == "completed"
    assert h.batch(second)["progress"]["downloaded"] == 2 and h.ctx.batches.links_hold(first) is True
    # E a ação num lote não desarma o outro.
    third = create_links(h, [link("EEEEE55555")])["id"]
    settle(h)
    assert h.ctx.batches.clear_links_hold(third) is False and h.ctx.batches.links_hold(first) is True


def test_hold_helpers_ignore_profile_batches(harness_factory):
    from media_fakes import FakeInstagramDownloader, remote

    dl = FakeInstagramDownloader(items=[remote("P0")])
    h = harness_factory(downloader=dl)
    batch_id = h.import_profile()["id"]
    h.run_all()
    h.ctx.batches.arm_links_hold(batch_id, "x")
    assert h.ctx.batches.get(batch_id)["error_code"] is None, "lote por perfil nunca é armado"
    h.ctx.batches.update(batch_id, error_code="access_refused", error_message="erro da listagem por perfil")
    assert h.ctx.batches.links_hold(batch_id) is False and h.ctx.batches.clear_links_hold(batch_id) is False
    assert h.ctx.batches.get(batch_id)["error_code"] == "access_refused"


# --------------------------------------------------------------------------- quem desarma (e quem não)


def test_retrying_one_job_while_the_batch_is_still_listing_does_not_disarm(harness_factory):
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B), C: photo(C)})
    dl.errors = refusals([B, C])
    batch_id = create_links(h, [link(A), link(B), link(C)])["id"]
    h.run_imports()
    expire(dl, A)
    dl.errors[A] = refusals([A])[A]
    settle(h)
    [job] = h.ctx.jobs.for_batch(batch_id)
    assert dl.calls == [A, B] and job["error_message"] == LINKS_REFRESH_BLOCKED_LISTING_MESSAGE
    assert h.client.post(f"/api/batches/{batch_id}/retry-failed").status_code == 409, "lote ainda em listing"
    assert h.client.post(f"/api/jobs/{job['id']}/retry").status_code == 200
    settle(h)
    assert dl.calls == [A, B], "com o lote em listing só \"Retomar importação\" desarma"
    assert state(h, batch_id) == ("listing", "needs_attention", "access_refused", None)
    [job] = h.ctx.jobs.for_batch(batch_id)
    assert (job["status"], job["error_message"]) == ("failed", LINKS_REFRESH_BLOCKED_LISTING_MESSAGE)


def test_retry_failed_disarms_a_batch_that_is_still_running(harness_factory):
    """Lote fora de `listing` mas ainda `running` (há job na fila): "Tentar falhas novamente" desarma do mesmo
    jeito, e a ação vale exatamente uma consulta recusada."""
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B)})
    batch_id = create_links(h, [link(A), link(B)])["id"]
    h.run_imports()
    expire(dl, A, B)
    dl.errors = refusals([A, B])
    waiting = h.ctx.jobs.for_batch(batch_id)[1]["id"]
    h.ctx.db.execute("UPDATE jobs SET next_attempt_at = ? WHERE id = ?", (utc_now(3600), waiting))
    settle(h)
    assert dl.calls == [A, B, A] and state(h, batch_id)[::2] == ("running", "access_refused")
    assert h.ctx.batches.get(batch_id)["error_message"] == LINKS_HOLD_DONE_MESSAGE
    retry_failed(h, batch_id)
    assert h.ctx.batches.links_hold(batch_id) is False and h.ctx.batches.get(batch_id)["error_message"] is None
    h.ctx.db.execute("UPDATE jobs SET next_attempt_at = NULL WHERE batch_id = ?", (batch_id,))
    settle(h)
    assert len(dl.calls) == 4, "uma consulta para a ação, com dois jobs precisando renovar"
    assert [(j["status"], j["error_code"]) for j in h.ctx.jobs.for_batch(batch_id)] == [
        ("failed", "access_refused")] * 2


def test_chunk_chosen_before_the_refusal_does_not_start_nor_erase_the_hold(harness_factory):
    """O runner escolheu o lote (`paused` por backoff vencido) e, antes de o trecho começar, uma renovação foi
    recusada: o trecho não começa, o erro do lote não é apagado e nada é consultado."""
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B)})
    dl.errors = {B: [InstagramError("network_error")]}
    batch_id = create_links(h, [link(A), link(B)])["id"]
    h.run_imports()
    h.ctx.db.execute("UPDATE batches SET listing_retry_at = ? WHERE id = ?", (utc_now(-60), batch_id))
    chosen = h.ctx.import_runner._next_eligible()
    assert chosen is not None and chosen["listing_state"] == "paused"
    expire(dl, A)
    dl.errors = refusals([A, B])
    h.run_jobs()
    assert dl.calls == [A, B, A]
    h.ctx.import_runner._run_listing(chosen)
    assert dl.calls == [A, B, A] and h.ctx.batches.links_hold(batch_id) is True
    settle(h)
    assert dl.calls == [A, B, A] and state(h, batch_id) == ("listing", "needs_attention", "access_refused", None)


def test_other_chunk_error_does_not_erase_a_hold_armed_meanwhile(harness_factory, monkeypatch):
    """O runner recebe falha de rede em B enquanto a renovação de A espera a trava; a renovação então é recusada e
    arma a espera ANTES de o runner tratar o erro dele: o backoff de rede não pode sobrescrever a espera."""
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B), C: photo(C)})
    dl.errors = {B: [InstagramError("network_error")], **refusals([C])}
    workers: list[threading.Thread] = []

    def on_resolve(code: str) -> None:
        if code == B and not workers:
            expire(dl, A)
            dl.errors[A] = refusals([A])[A]
            job = h.ctx.jobs.claim(allow_downloads=True, owner="worker")
            workers.append(threading.Thread(target=run_job, args=(h, job)))
            workers[0].start()
            time.sleep(0.3)                               # a renovação já espera a trava do lote

    wake = h.ctx.wake_worker

    def wake_after_the_refresh() -> None:                 # chamado pelo runner logo depois do trecho
        if workers:
            workers[0].join(15)
        wake()

    dl.on_resolve = on_resolve
    monkeypatch.setattr(h.ctx, "wake_worker", wake_after_the_refresh)
    batch_id = create_links(h, [link(A), link(B), link(C)])["id"]
    h.run_imports()
    assert workers and not workers[0].is_alive()
    assert dl.calls == [A, B, A]
    assert state(h, batch_id) == ("listing", "needs_attention", "access_refused", None)
    assert h.ctx.batches.get(batch_id)["error_message"] == LINKS_HOLD_LISTING_MESSAGE
    h.ctx.db.execute("UPDATE batches SET listing_retry_at = ? WHERE id = ?", (utc_now(-60), batch_id))
    settle(h)
    assert dl.calls == [A, B, A], "nenhuma retomada automática por causa da falha de rede"
    assert state(h, batch_id) == ("listing", "needs_attention", "access_refused", None)


# --------------------------------------------------------------------------- fim do lote com a espera armada (D1)


def test_hold_is_released_when_the_batch_completes_without_failed_jobs(harness_factory):
    """[A, B]: A resolvido com o job ainda aberto; B recusado duas vezes (último pendente): o lote sai de `listing`
    com a espera. A baixa normalmente (URL válida): lote concluído SEM job falho -> espera desarmada, aviso normal."""
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B)})
    dl.errors = refusals([B])
    batch_id = create_links(h, [link(A), link(B)])["id"]
    h.run_imports()
    resume(h, batch_id)
    h.run_imports()
    assert dl.calls == [A, B, B]
    saved = h.ctx.batches.get(batch_id)
    assert (saved["status"], saved["error_code"], saved["error_message"]) == (
        "running", "access_refused", LINKS_HOLD_DONE_MESSAGE)
    assert "Se algum item do lote falhar, use \"Tentar falhas novamente\"" in LINKS_HOLD_DONE_MESSAGE
    settle(h)
    done = h.batch(batch_id)
    assert (done["status"], done["error_code"], done["error_message"]) == (
        "completed", None, "1 de 2 links não puderam ser importados.")
    assert (done["progress"]["downloaded"], done["progress"]["failed"]) == (1, 0)
    assert h.ctx.batches.links_hold(batch_id) is False and dl.calls == [A, B, B]
    assert h.client.post(f"/api/batches/{batch_id}/retry-failed").status_code == 409


def test_hold_released_at_completion_leaves_no_message_when_no_link_failed(harness_factory):
    """Renovação recusada, o job é cancelado pelo dono e o resto baixa: lote concluído sem falha e sem link falho ->
    nenhum erro pendurado."""
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B)})
    batch_id = create_links(h, [link(A), link(B)])["id"]
    h.run_imports()
    expire(dl, A)
    dl.errors = refusals([A])
    cancel = threading.Event()
    dl.on_resolve = lambda code: cancel.set()
    run_job(h, h.ctx.jobs.claim(allow_downloads=True, owner="t1"), cancel)
    dl.on_resolve = None
    assert h.ctx.batches.links_hold(batch_id) is True
    settle(h)
    done = h.batch(batch_id)
    assert (done["status"], done["error_code"], done["error_message"]) == ("completed", None, None)
    assert h.ctx.batches.links_hold(batch_id) is False and dl.calls == [A, B, A]


def test_batch_leaving_listing_with_only_failed_jobs_keeps_the_hold(harness_factory):
    """O lote sai de `listing` só com job falho (nenhum aberto): a espera permanece, o botão existe e vale 1 consulta."""
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B)})
    dl.errors = refusals([B])
    batch_id = create_links(h, [link(A), link(B)])["id"]
    h.run_imports()
    expire(dl, A)
    dl.errors[A] = refusals([A])[A]
    settle(h)                                             # A falha sem consultar (espera armada, lote em listing)
    assert [j["status"] for j in h.ctx.jobs.for_batch(batch_id)] == ["failed"]
    resume(h, batch_id)
    settle(h)
    assert dl.calls == [A, B, B]
    done = h.batch(batch_id)
    assert (done["status"], done["error_code"], done["error_message"], done["progress"]["failed"]) == (
        "completed", "access_refused", LINKS_HOLD_DONE_MESSAGE, 1)
    assert h.ctx.batches.links_hold(batch_id) is True
    retry_failed(h, batch_id)
    settle(h)
    assert dl.calls == [A, B, B, A], "\"Tentar falhas novamente\": exatamente 1 consulta"
    assert h.ctx.batches.links_hold(batch_id) is True


def test_cancelling_a_batch_ends_the_hold_and_keeps_only_the_reason(harness_factory):
    from app.db.repositories.batches import LINKS_HOLD_CANCELLED_MESSAGE

    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B), C: photo(C)})
    dl.errors = refusals([A, B, C])
    dl.errors[A] = []
    batch_id = create_links(h, [link(A), link(B), link(C)])["id"]
    h.run_imports()
    assert h.ctx.batches.links_hold(batch_id) is True
    assert h.client.post(f"/api/batches/{batch_id}/cancel").status_code == 200
    saved = h.ctx.batches.get(batch_id)
    assert (saved["status"], saved["error_code"], saved["error_message"]) == (
        "cancelled", "access_refused", LINKS_HOLD_CANCELLED_MESSAGE)
    assert "Retomar" not in saved["error_message"] and "Tentar" not in saved["error_message"]
    assert h.ctx.batches.links_hold(batch_id) is False
    h.ctx.batches.arm_links_hold(batch_id, LINKS_REFUSED_MESSAGE)   # recusa que chega depois do cancelamento
    assert h.ctx.batches.get(batch_id)["error_message"] == LINKS_HOLD_CANCELLED_MESSAGE
    settle(h)
    assert dl.calls == [A, B]


# --------------------------------------------------------------------------- fim do trecho sob a trava (O1)


def test_hold_armed_between_the_check_and_the_end_of_chunk_write_survives(harness_factory, monkeypatch):
    """Intercalação forçada: logo depois de o fim do trecho conferir a espera (ainda desarmada), uma renovação tenta
    consultar. Com o fim do trecho sob a trava do lote ela só consulta (e arma) DEPOIS da gravação do backoff de
    rede; a espera sobrevive e nada é consultado sozinho quando o backoff vence."""
    import sys

    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B), C: photo(C)})
    dl.errors = {B: [InstagramError("network_error")], **refusals([C])}
    workers: list[threading.Thread] = []
    original = h.ctx.batches.links_hold

    def links_hold(batch_id: int) -> bool:
        result = original(batch_id)
        if sys._getframe(1).f_code.co_name == "_after_links_chunk_locked" and not workers:
            assert result is False
            expire(dl, A)
            dl.errors[A] = refusals([A])[A]
            job = h.ctx.jobs.claim(allow_downloads=True, owner="worker")
            workers.append(threading.Thread(target=run_job, args=(h, job)))
            workers[0].start()
            workers[0].join(1.0)          # sem a trava, a renovação consultaria e armaria aqui, antes da gravação
            assert workers[0].is_alive() and dl.calls == [A, B], "a renovação espera o fim do trecho"
        return result

    monkeypatch.setattr(h.ctx.batches, "links_hold", links_hold)
    batch_id = create_links(h, [link(A), link(B), link(C)])["id"]
    h.run_imports()
    assert workers
    workers[0].join(15)
    assert not workers[0].is_alive() and dl.calls == [A, B, A]
    assert original(batch_id) is True, "a gravação do fim do trecho não apagou a espera"
    h.ctx.db.execute("UPDATE batches SET listing_retry_at = ? WHERE id = ?", (utc_now(-60), batch_id))
    settle(h)
    assert dl.calls == [A, B, A], "0 consultas automáticas depois"
    assert state(h, batch_id) == ("listing", "needs_attention", "access_refused", None)
