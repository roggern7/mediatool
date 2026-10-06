"""Controlador de recuperação e portão de requisições (docs/BATCH_ROBUSTNESS.md, seção 8), com relógio simulado.

Offline, valores FAKE; a sessão DPAPI fica num secrets_dir temporário e nunca muda.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest

from app.integrations.instagram.downloader import InstagramError, SessionTestResult
from app.services.instagram_gate import InstagramRequestGate
from app.services.instagram_recovery import AUTH_FAIL_STREAK_KEY, LADDERS, STATE_KEY
from media_fakes import FakeInstagramDownloader, remote
from test_auth_escalation import (
    PAGE_SIZE,
    FakeClock,
    LazyPagedDownloader,
    assert_session_intact,
    batch_row,
    drive,
    internal_batch,
    make_harness,
    session_fixture,
)

MIN = 60.0
HOUR = 3600.0


class CountingVerify:
    """Verificação mínima falsa: devolve os resultados em ordem e conta as chamadas."""

    def __init__(self, *results) -> None:
        self.results = list(results)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


def ok() -> SessionTestResult:
    return SessionTestResult(ok=True, code="ok", message="Sessão válida.", username="conta")


def fail(code: str, result: str | None = None) -> SessionTestResult:
    return SessionTestResult(ok=False, code=code, message="Recusado.", username=None, result=result)


def remaining(h, clock: FakeClock) -> float:
    state = h.ctx.worker.recovery.state()
    assert state is not None
    return (datetime.fromisoformat(state.next_attempt_at.replace("Z", "+00:00")) - clock.now).total_seconds()


def expire(h, clock: FakeClock) -> None:
    clock.advance(seconds=remaining(h, clock) + 1)


@pytest.fixture
def env(harness_factory, tmp_path):
    h, provider, digest, clock = make_harness(harness_factory, tmp_path, LazyPagedDownloader(50))
    yield h, clock
    assert_session_intact(provider, digest)


# --------------------------------------------------------------------------- pausa e degraus


def test_pause_has_no_attempts_during_cooldown(env):
    h, clock = env
    worker = h.ctx.worker
    verify = CountingVerify(ok())
    worker.recovery.verify = verify
    assert worker.pause_for("rate_limited") is True
    state = worker.recovery.state()
    assert state.key == "rate_limited" and state.level == 0 and state.scope == "instagram"
    assert 15 * MIN - 1 < remaining(h, clock) <= 15 * MIN
    for _ in range(5):
        clock.advance(minutes=2)
        assert worker.recovery.tick() is None
    assert verify.calls == 0, "nenhuma tentativa ao Instagram durante a pausa"
    clock.advance(minutes=6)
    assert worker.recovery.tick() == "ok" and verify.calls == 1
    assert worker.pause_state() == (None, None) and worker.recovery.state() is None
    assert h.ctx.app_settings.get_state(STATE_KEY) is None


@pytest.mark.parametrize("code,expected,reasons", [
    ("request_auth_failed", [15 * MIN, 30 * MIN, HOUR, 2 * HOUR, 4 * HOUR, 4 * HOUR],
     ["auth_check"] + ["session_invalid"] * 5),
    ("rate_limited", [15 * MIN, 30 * MIN, HOUR, 2 * HOUR, 4 * HOUR, 6 * HOUR, 6 * HOUR], ["rate_limited"] * 7),
    ("action_blocked", [HOUR, 2 * HOUR, 4 * HOUR, 8 * HOUR, 12 * HOUR, 12 * HOUR], ["action_blocked"] * 6),
    ("checkpoint_required", [6 * HOUR] * 3, ["checkpoint_required"] * 3),
    ("session_invalid", [6 * HOUR] * 3, ["session_invalid"] * 3),
])
def test_backoff_steps_and_cap_per_reason(env, code, expected, reasons):
    h, clock = env
    worker = h.ctx.worker
    verify = CountingVerify(fail(code))
    worker.recovery.verify = verify
    worker.pause_for(code)
    seen, seen_reasons = [], []
    for step in range(len(expected)):
        seen.append(round(remaining(h, clock)))
        seen_reasons.append(worker.pause_state()[1])
        if step < len(expected) - 1:
            expire(h, clock)
            assert worker.recovery.tick() == "failed"
    assert seen == [int(x) for x in expected]
    assert seen_reasons == reasons
    assert verify.calls == len(expected) - 1, "uma verificação por vencimento, nada além"
    status = h.client.get("/api/settings").json()["instagram"]
    assert status["action_required"] is (code in ("checkpoint_required", "session_invalid"))
    assert status["next_attempt_at"] == worker.recovery.state().next_attempt_at
    assert LADDERS  # tabela única de degraus


def test_graver_failure_switches_reason_and_network_keeps_step(env):
    h, clock = env
    worker = h.ctx.worker
    worker.recovery.verify = CountingVerify(fail("rate_limited"), InstagramError("network_error"),
                                            fail("action_blocked", "restricted"))
    worker.pause_for("rate_limited")
    expire(h, clock)
    assert worker.recovery.tick() == "failed"
    assert worker.recovery.state().level == 1 and round(remaining(h, clock)) == 30 * MIN
    expire(h, clock)
    assert worker.recovery.tick() == "network"   # rede indisponível: mesmo degrau
    assert worker.recovery.state().level == 1 and round(remaining(h, clock)) == 30 * MIN
    expire(h, clock)
    assert worker.recovery.tick() == "failed"    # mais grave: troca o motivo, 1o degrau dele
    state = worker.recovery.state()
    assert state.key == "action_blocked" and state.level == 0 and round(remaining(h, clock)) == HOUR
    # Recusa mais leve durante o cooldown (requests em voo) não mexe no degrau nem no motivo.
    assert worker.pause_for("rate_limited") is True
    assert worker.recovery.state() == state


def test_repeated_failures_climb_and_later_recovery_resets_backoff(env):
    h, clock = env
    worker = h.ctx.worker
    worker.recovery.verify = CountingVerify(fail("request_auth_failed"), fail("request_auth_failed"), ok())
    worker.pause_for("request_auth_failed")
    for level in (1, 2):
        expire(h, clock)
        worker.recovery.tick()
        assert worker.recovery.state().level == level
    assert h.ctx.app_settings.get_state(AUTH_FAIL_STREAK_KEY) == "3"
    expire(h, clock)
    assert worker.recovery.tick() == "ok"
    assert worker.recovery.state() is None and h.ctx.app_settings.get_state(AUTH_FAIL_STREAK_KEY) is None
    # Seção 9 (A2): o backoff só zera depois da janela de prova (2 x a última pausa, 1 h) sem falhas.
    clock.advance(hours=2, seconds=1)
    worker.pause_for("request_auth_failed")
    assert worker.recovery.state().level == 0 and worker.pause_state()[1] == "auth_check"


def test_expired_pause_waits_for_controller_verification(env):
    h, clock = env
    worker = h.ctx.worker
    worker.pause_for("rate_limited")
    expire(h, clock)
    assert worker.downloads_paused() and worker.ingest_paused(), "vencida, aguarda a verificação do controlador"
    assert h.client.post("/api/imports", json={"profile": "perfil_teste", "content_type": "all", "limit": 10,
                                               "preset_id": None, "process": False}).status_code == 409


# --------------------------------------------------------------------------- reinício


def test_restart_during_cooldown_keeps_step_and_next_attempt(harness_factory, tmp_path):
    provider, digest = session_fixture(tmp_path)
    first = harness_factory(downloader=LazyPagedDownloader(10), session_provider=provider)
    clock = FakeClock()
    first.ctx.worker.clock = clock
    first.ctx.worker.recovery.verify = CountingVerify(fail("action_blocked", "restricted"))
    first.ctx.worker.pause_for("action_blocked")
    expire(first, clock)
    first.ctx.worker.recovery.tick()
    state = first.ctx.worker.recovery.state()
    assert state.level == 1
    clock.advance(minutes=30)
    root = first.root
    first.__exit__(None, None, None)

    second = harness_factory(root=root, downloader=LazyPagedDownloader(10), session_provider=provider)
    second.ctx.worker.clock = clock
    verify = CountingVerify(ok())
    second.ctx.worker.recovery.verify = verify
    assert second.ctx.worker.recovery.state() == state, "nova instância retoma o degrau e o horário salvos"
    assert second.ctx.worker.pause_state()[1] == "action_blocked"
    assert second.ctx.worker.recovery.tick() is None and verify.calls == 0
    clock.advance(seconds=remaining(second, clock) - 1)
    assert second.ctx.worker.recovery.tick() is None and verify.calls == 0
    clock.advance(seconds=2)
    assert second.ctx.worker.recovery.tick() == "ok" and verify.calls == 1
    assert second.ctx.worker.pause_state() == (None, None)
    assert_session_intact(provider, digest)


# --------------------------------------------------------------------------- retomada automática da listagem


def test_automatic_resume_continues_listing_from_checkpoint(harness_factory, tmp_path):
    dl = LazyPagedDownloader(400, fail_pages={4: 1}, fail_code="rate_limited")
    h, provider, digest, clock = make_harness(harness_factory, tmp_path, dl)
    worker = h.ctx.worker
    batch_id = internal_batch(h, 400)
    drive(h, batch_id, stop_when_paused=True)
    assert worker.pause_state()[1] == "rate_limited"
    requested = list(dl.page_requests)
    assert requested == [1, 2, 3, 4]
    # Escopo "instagram": downloads de ingest também aguardam.
    downloaded = len(dl.downloaded)
    drive(h, batch_id)
    assert len(dl.downloaded) == downloaded and dl.page_requests == requested
    worker.recovery.verify = CountingVerify(ok())
    expire(h, clock)
    assert worker.recovery.tick() == "ok"
    drive(h, batch_id)
    assert batch_row(h, batch_id)["status"] == "completed"
    assert dl.page_requests == [1, 2, 3, 4, 4, 5, 6, 7, 8], "retoma do checkpoint, sem refazer páginas"
    assert len(set(dl.downloaded)) == 400
    assert_session_intact(provider, digest)


def test_processing_continues_during_pauses(harness_factory, tmp_path):
    dl = LazyPagedDownloader(20, prefix="P")
    h, provider, digest, clock = make_harness(harness_factory, tmp_path, dl)
    worker = h.ctx.worker
    first = internal_batch(h, 20)
    drive(h, first)
    media_ids = [m["id"] for m in h.client.get("/api/media", params={"limit": 100}).json()["items"]]
    assert len(media_ids) == 20
    preset_id = h.client.get("/api/presets").json()[0]["id"]

    for reason in ("rate_limited", "request_auth_failed"):
        h.downloader = LazyPagedDownloader(PAGE_SIZE, prefix="R" if reason == "rate_limited" else "S")
        second = internal_batch(h, PAGE_SIZE)
        h.run_imports()   # lista antes da pausa: ingest pendentes com URL
        worker.pause_for(reason)
        library = h.client.post("/api/media/process", json={"media_ids": media_ids, "preset_id": preset_id})
        assert library.status_code == 202, library.text
        worker.run_pending(max_jobs=500)
        done = h.ctx.db.scalar("SELECT COUNT(*) FROM jobs WHERE batch_id = ? AND status = 'completed'",
                               (library.json()["id"],))
        assert done == 20, f"processamento local continua durante {reason}"
        ingested = len(h.downloader.downloaded)
        if reason == "rate_limited":
            assert ingested == 0, "rate_limited: ingest aguarda"
        else:
            assert ingested == PAGE_SIZE, "pausa de sessão: download com URL válida continua"
        worker.clear_pause()
        drive(h, second)
        assert batch_row(h, second)["status"] == "completed"
    assert_session_intact(provider, digest)


def test_job_needing_url_renewal_waits_during_session_pause_and_completes_after(harness_factory, tmp_path):
    provider, digest = session_fixture(tmp_path)
    dl = FakeInstagramDownloader(items=[remote("EXP1")], refreshable=True,
                                 download_errors={"EXP1:0": [InstagramError("media_url_expired")]})
    h = harness_factory(downloader=dl, session_provider=provider)
    clock = FakeClock()
    worker = h.ctx.worker
    worker.clock = clock
    batch = h.import_profile(limit=10)
    h.run_imports()
    worker.pause_for("request_auth_failed")
    worker.run_pending()
    job = h.ctx.db.fetchone("SELECT status, error_code FROM jobs WHERE batch_id = ?", (batch["id"],))
    assert job["status"] == "pending", dict(job)
    assert dl.refreshes == [], "renovação de URL não acontece durante a pausa"
    worker.recovery.verify = CountingVerify(ok())
    expire(h, clock)
    assert worker.recovery.tick() == "ok"
    for _ in range(5):
        worker.run_pending()
        if h.ctx.db.scalar("SELECT status FROM jobs WHERE batch_id = ?", (batch["id"],)) == "completed":
            break
        h.ctx.db.execute("UPDATE jobs SET next_attempt_at = NULL WHERE batch_id = ?", (batch["id"],))
    assert h.ctx.db.scalar("SELECT status FROM jobs WHERE batch_id = ?", (batch["id"],)) == "completed"
    assert dl.refreshes == [1]
    assert_session_intact(provider, digest)


# --------------------------------------------------------------------------- portão global


def test_gate_5000_renewals_one_in_flight_and_interval_respected():
    clock = {"t": 0.0}
    lock = threading.Lock()

    def now() -> float:
        with lock:
            return clock["t"]

    def sleep(seconds: float) -> None:
        with lock:
            clock["t"] += seconds

    gate = InstagramRequestGate(interval=(2.0, 4.0), clock=now, sleep=sleep)
    in_flight = {"n": 0, "max": 0}
    spans: list[tuple[float, float]] = []

    def renew(count: int) -> None:
        for _ in range(count):
            with gate.slot():
                with lock:
                    in_flight["n"] += 1
                    in_flight["max"] = max(in_flight["max"], in_flight["n"])
                    start = clock["t"]
                    clock["t"] += 0.5   # duração simulada da requisição
                    end = clock["t"]
                    in_flight["n"] -= 1
                spans.append((start, end))

    threads = [threading.Thread(target=renew, args=(625,)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(spans) == 5000
    assert in_flight["max"] == 1, "no máximo 1 requisição em voo"
    spans.sort()
    gaps = [b[0] - a[1] for a, b in zip(spans, spans[1:], strict=False)]
    assert min(gaps) >= 2.0 - 1e-9, "intervalo mínimo entre requisições"
    assert all(b[0] >= a[1] for a, b in zip(spans, spans[1:], strict=False)), "nada simultâneo"


def test_gate_cancel_while_waiting():
    gate = InstagramRequestGate(interval=(0.0, 0.0))
    cancel = threading.Event()
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with gate.slot():
            entered.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    entered.wait(5)
    cancel.set()
    with pytest.raises(Exception) as info:
        with gate.slot(cancel):
            pass
    assert getattr(info.value, "code", None) == "cancelled"
    release.set()
    t.join()


def test_checkpoint_verifies_every_6h_without_bypass(env):
    h, clock = env
    worker = h.ctx.worker
    verify = CountingVerify(fail("checkpoint_required", "checkpoint_required"))
    worker.recovery.verify = verify
    worker.pause_for("checkpoint_required")
    for i in range(1, 4):
        clock.advance(hours=5, minutes=59)
        assert worker.recovery.tick() is None and verify.calls == i - 1
        clock.advance(minutes=2)
        assert worker.recovery.tick() == "failed" and verify.calls == i
        assert worker.pause_state()[1] == "checkpoint_required"
        assert round(remaining(h, clock)) == 6 * HOUR
    status = h.client.get("/api/settings").json()["instagram"]
    assert status["action_required"] is True and status["pause_scope"] == "session"
    assert h.downloader.page_requests == [], "nenhuma outra requisição: só a verificação mínima"
    assert timedelta(hours=6).total_seconds() == LADDERS["checkpoint_required"][0]


def test_verification_does_not_hold_the_gate_requests_take_their_own_slot(env):
    """Seção 8: o controlador não segura o portão; a verificação (cujas requisições ocupam a própria vaga, como o
    cliente HTTP do provider web) não pode travar no portão não reentrante."""
    h, clock = env
    worker = h.ctx.worker
    gate = InstagramRequestGate(interval=(0.0, 0.0))
    h.ctx.instagram_gate = gate
    taken = []

    def verify():
        for _ in range(2):   # bootstrap + 1 consulta, cada uma com a própria vaga
            with gate.slot():
                taken.append(True)
        return ok()

    worker.recovery.verify = verify
    assert worker.pause_for("rate_limited") is True
    expire(h, clock)
    out: list = []
    t = threading.Thread(target=lambda: out.append(worker.recovery.tick()), daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive(), "deadlock: a verificação travou no portão"
    assert out == ["ok"] and len(taken) == 2
