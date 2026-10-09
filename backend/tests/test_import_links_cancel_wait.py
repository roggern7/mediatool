"""Fim de trecho x cancelamento: um lote cancelado enquanto o runner termina o trecho (inclusive enquanto ele espera
a trava de consultas do lote por link) continua `cancelled`. Nenhuma gravação atrasada do runner o tira do estado
terminal. Também: a espera não é armada em lote já encerrado e a liberação no fim do lote só vale para lote por link.
100% offline, valores FAKE."""

from __future__ import annotations

import sys
import threading
import time

import pytest

from app.integrations.instagram.downloader import InstagramError
from app.jobs import import_runner as runner_module
from app.jobs.import_runner import LINKS_REFUSED_MESSAGE
from app.jobs.pipeline import links_query_lock
from app.services import import_service
from import_links_fakes import create_links, link, link_rows, links_app, photo
from media_fakes import FakeInstagramDownloader, remote

A, B = "AAAAA11111", "BBBBB22222"


def at_end_of_chunk(h, monkeypatch, action) -> list:
    """Executa `action()` UMA vez no fim do trecho: depois de `_after_chunk` conferir que o lote não está cancelado e
    antes de qualquer gravação de fim de trecho (o runner chama `wake_worker` exatamente ali)."""
    wake = h.ctx.wake_worker
    fired: list = []

    def hooked() -> None:
        if sys._getframe(1).f_code.co_name == "_after_chunk" and not fired:
            fired.append(True)
            action()
        wake()

    monkeypatch.setattr(h.ctx, "wake_worker", hooked)
    return fired


def assert_stays_cancelled(h, batch_id: int) -> None:
    for _ in range(3):
        h.run_all()
    row = h.ctx.batches.get(batch_id)
    assert (row["status"], row["listing_state"]) == ("cancelled", "cancelled"), dict(row)
    jobs = h.ctx.jobs.for_batch(batch_id)
    assert all(j["status"] == "cancelled" for j in jobs), [j["status"] for j in jobs]
    api = h.batch(batch_id)
    assert api["status"] == "cancelled" and api["progress"]["downloaded"] == 0


# --------------------------------------------------------------------------- reprodução da revisão


def test_cancel_while_the_end_of_chunk_waits_for_the_batch_lock_keeps_the_batch_cancelled(harness_factory, monkeypatch):
    """Uma consulta do lote em voo segura a trava; o runner chega ao fim do trecho (todos os links resolvidos) e
    espera; o lote é cancelado; a trava é solta. O lote NÃO pode voltar a `running`/`completed`."""
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B)})
    ids: dict = {}
    acquired, release = threading.Event(), threading.Event()
    threads: list[threading.Thread] = []

    def holder() -> None:
        with links_query_lock(h.ctx, ids["batch"], threading.Event()):
            acquired.set()
            release.wait(15)

    def canceller() -> None:
        time.sleep(0.4)                                   # o runner já espera a trava do lote
        import_service.cancel_batch(h.ctx, ids["batch"])
        release.set()

    def action() -> None:
        threads.extend([threading.Thread(target=holder), threading.Thread(target=canceller)])
        threads[0].start()
        assert acquired.wait(5)
        threads[1].start()

    fired = at_end_of_chunk(h, monkeypatch, action)
    ids["batch"] = create_links(h, [link(A), link(B)])["id"]
    h.run_imports()
    for thread in threads:
        thread.join(15)
    assert fired and dl.calls == [A, B]
    assert_stays_cancelled(h, ids["batch"])
    assert dl.calls == [A, B] and [r["status"] for r in link_rows(h, ids["batch"])] == ["resolved", "resolved"]


# --------------------------------------------------------------------------- variantes de fim de trecho (lote por link)


def _refused_twice(h, dl, monkeypatch, batch_id):
    h.ctx.db.execute("UPDATE batch_links SET attention_count = 1 WHERE batch_id = ? AND position = 2", (batch_id,))


@pytest.mark.parametrize("variant", ["finish", "network_error", "refused_twice", "paused", "backpressure"])
def test_cancel_landing_at_the_end_of_a_link_chunk_is_never_undone(harness_factory, monkeypatch, variant):
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B)})
    ids: dict = {}
    if variant == "network_error":
        dl.errors = {B: [InstagramError("network_error")]}
    elif variant == "refused_twice":
        dl.errors = {B: [InstagramError("access_refused")]}
    elif variant == "paused":
        dl.on_resolve = lambda code: h.ctx.worker.pause_for("rate_limited")
    elif variant == "backpressure":
        dl.on_resolve = lambda code: monkeypatch.setattr(runner_module, "MAX_PENDING_INGEST", 0)
    fired = at_end_of_chunk(h, monkeypatch, lambda: import_service.cancel_batch(h.ctx, ids["batch"]))
    ids["batch"] = create_links(h, [link(A), link(B)])["id"]
    if variant == "refused_twice":
        _refused_twice(h, dl, monkeypatch, ids["batch"])
    h.run_imports()
    assert fired
    expected_calls = [A] if variant in ("paused", "backpressure") else [A, B]
    assert dl.calls == expected_calls
    monkeypatch.setattr(runner_module, "MAX_PENDING_INGEST", 100)
    assert_stays_cancelled(h, ids["batch"])
    assert dl.calls == expected_calls, "nenhuma consulta nova"
    rows = link_rows(h, ids["batch"])
    if variant == "finish":
        assert [r["status"] for r in rows] == ["resolved", "resolved"]
    else:
        assert [r["status"] for r in rows] == ["resolved", "cancelled"], "o link da vez não é marcado depois do cancelamento"
    assert h.ctx.batches.links_hold(ids["batch"]) is False


# --------------------------------------------------------------------------- lote por perfil (código compartilhado)


@pytest.mark.parametrize("variant", ["profile_end", "limit", "network_error", "listing_error"])
def test_cancel_landing_at_the_end_of_a_profile_chunk_is_never_undone(harness_factory, monkeypatch, variant):
    dl = FakeInstagramDownloader(items=[remote(f"R{i:04d}") for i in range(5)])
    if variant == "network_error":
        dl.listing_error, dl.listing_error_after = InstagramError("network_error"), 3
    elif variant == "listing_error":
        dl.listing_error, dl.listing_error_after = InstagramError("access_refused"), 3
    h = harness_factory(downloader=dl)
    ids: dict = {}
    fired = at_end_of_chunk(h, monkeypatch, lambda: import_service.cancel_batch(h.ctx, ids["batch"]))
    ids["batch"] = h.import_profile(limit=3 if variant == "limit" else 100)["id"]
    h.run_imports()
    assert fired
    listings = dl.listings
    assert_stays_cancelled(h, ids["batch"])
    assert dl.listings == listings, "nenhuma listagem nova"


def test_profile_chunk_end_without_cancel_is_unchanged(harness_factory):
    dl = FakeInstagramDownloader(items=[remote(f"R{i:04d}") for i in range(5)])
    h = harness_factory(downloader=dl)
    batch_id = h.import_profile()["id"]
    h.run_all()
    done = h.batch(batch_id)
    assert (done["status"], done["found"], done["progress"]["downloaded"]) == ("completed", 5, 5)


# --------------------------------------------------------------------------- observações a) e b)


def test_hold_is_not_armed_on_a_batch_that_already_finished(harness_factory):
    h, dl = links_app(harness_factory, {A: photo(A)})
    batch_id = create_links(h, [link(A)])["id"]
    h.run_all()
    assert h.batch(batch_id)["status"] == "completed"
    h.ctx.batches.arm_links_hold(batch_id, LINKS_REFUSED_MESSAGE)
    row = h.ctx.batches.get(batch_id)
    assert (row["error_code"], row["error_message"]) == (None, None) and h.ctx.batches.links_hold(batch_id) is False
    failed = create_links(h, [link("GONE100001")])["id"]      # publicação sem mídia: lote `failed`
    h.run_all()
    assert h.batch(failed)["status"] == "failed"
    h.ctx.batches.arm_links_hold(failed, LINKS_REFUSED_MESSAGE)
    assert h.ctx.batches.get(failed)["error_code"] == "links_failed"


def test_profile_batch_completing_with_access_refused_keeps_its_error(harness_factory):
    """A liberação no fim do lote é só da recusa em espera de lote por LINK: o `access_refused` de uma listagem
    parcial por perfil continua no lote concluído."""
    dl = FakeInstagramDownloader(items=[remote(f"R{i:04d}") for i in range(5)])
    dl.listing_error, dl.listing_error_after = InstagramError("access_refused"), 3
    h = harness_factory(downloader=dl)
    batch_id = h.import_profile()["id"]
    h.run_imports()
    before = h.ctx.batches.get(batch_id)
    assert (before["status"], before["error_code"]) == ("running", "access_refused") and before["error_message"]
    h.run_jobs()
    done = h.ctx.batches.get(batch_id)
    assert (done["status"], done["error_code"], done["error_message"]) == (
        "completed", "access_refused", before["error_message"])
    assert h.batch(batch_id)["progress"]["failed"] == 0
