from __future__ import annotations

import sqlite3
import threading
from datetime import timedelta
from unittest.mock import MagicMock

import httpx
import pytest

from app.db.connection import parse_utc
from app.integrations.instagram.downloader import InstagramError
from app.integrations.instagram.web.errors import InstagramWebError
from app.jobs import profile_analysis_runner as runner_module
from app.jobs.profile_analysis_runner import (
    BACKOFF_SECONDS,
    CANARY_EVERY,
    CANARY_PROFILE,
    MAX_ATTEMPTS,
    PROFILE_DELAY,
    REST_DELAY,
    REST_EVERY,
    SLOW_PROFILE_DELAY,
    STORAGE_MAX_FAILURES,
    STORAGE_RETRY_SECONDS,
    ProfileAnalysisRunner,
    recover_profile_analyses,
    stamp,
)
from app.jobs.worker import PAUSE_REASON_KEY, PAUSE_STATE_KEY
from app.main import anonymous_profile_fetcher
from app.services.instagram_gate import (
    PAGES_STATE_KEY,
    GateCancelled,
    InstagramPaused,
    InstagramRequestGate,
)
from conftest import wait_until
from media_fakes import MemorySessionProvider
from profile_fakes import (
    ScriptedFetcher,
    SimClock,
    analysis_of,
    create,
    install_runner,
    items_of,
    profile_html,
    snapshot,
)


def web_error(code: str) -> InstagramWebError:
    return InstagramWebError(code, f"Mensagem de {code}.")


@pytest.fixture
def clock() -> SimClock:
    return SimClock()


def by_name(harness, analysis_id: int) -> dict[str, dict]:
    return {item["username"]: item for item in items_of(harness, analysis_id)}


def untouched_global_state(harness) -> None:
    ctx = harness.ctx
    assert ctx.app_settings.get_state(PAUSE_STATE_KEY) is None
    assert ctx.app_settings.get_state(PAUSE_REASON_KEY) is None
    assert ctx.instagram_gate.listing_slowed() is False
    assert ctx.instagram_gate.paused_scope() is None
    assert ctx.worker.downloads_paused() is False


# ---- fluxo normal -------------------------------------------------------------------------------------------------
def test_single_profile(harness, clock):
    fetcher = ScriptedFetcher(clock)
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "@perfil")
    assert analysis["status"] == "pending" and analysis["total"] == 1 and analysis["pending"] == 1
    assert runner.run_next() is True and runner.run_next() is False

    done = analysis_of(harness, analysis["id"])
    assert done["status"] == "completed" and done["report_ready"] is True and done["done"] == 1 and done["pending"] == 0
    assert done["summary"]["active"] == 1 and done["summary"]["stories_yes"] == 1
    assert done["summary"]["views_total"] == 150
    item = items_of(harness, analysis["id"])[0]
    assert item["state"] == "done" and item["attempts"] == 1 and item["account_status"] == "active"
    assert item["posts_count"] == 10 and item["highlights"] == "no"
    assert clock.waits == []   # a 1a consulta não tem intervalo antes


def test_many_profiles_in_order_with_individual_checked_at(harness, clock):
    fetcher = ScriptedFetcher(clock)
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "c1", "a2", "b3")
    runner.run_next()
    assert fetcher.calls == ["c1", "a2", "b3"]
    items = items_of(harness, analysis["id"])
    assert [item["position"] for item in items] == [1, 2, 3]
    moments = [parse_utc(item["checked_at"]) for item in items]
    # Cada um: logo depois de ELE responder (1,5 s da resposta + 3 s de intervalo entre perfis).
    start = parse_utc(analysis_of(harness, analysis["id"])["started_at"])
    assert moments == [start + timedelta(seconds=seconds) for seconds in (1.5, 6, 10.5)]
    assert items[0]["checked_at"].endswith(".500Z") and items[0]["checked_date"] == "01/11/2026"
    assert items[0]["checked_time"] == "12:00:01"


def test_checked_at_of_retried_item_is_the_deciding_attempt(harness, clock):
    fetcher = ScriptedFetcher(clock, {"a1": [web_error("timeout"), snapshot("a1")]})
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1")
    started = clock.now()
    runner.run_next()
    item = items_of(harness, analysis["id"])[0]
    assert item["attempts"] == 2 and item["account_status"] == "active" and item["error_code"] is None
    assert parse_utc(item["checked_at"]) == started + timedelta(seconds=1.5 + 3 + 1.5)


def test_fifo_one_analysis_at_a_time(harness, clock):
    fetcher = ScriptedFetcher(clock)
    runner = install_runner(harness, fetcher, clock)
    first, second = create(harness, "a1", "a2"), create(harness, "b1")
    assert runner.run_next() is True
    assert analysis_of(harness, first["id"])["status"] == "completed"
    assert analysis_of(harness, second["id"])["status"] == "pending"
    assert runner.run_next() is True
    assert fetcher.calls == ["a1", "a2", "b1"]


# ---- ritmo --------------------------------------------------------------------------------------------------------
def test_delay_between_profiles_uses_normal_interval(harness, clock):
    drawn = []

    def rng(low, high):
        drawn.append((low, high))
        return high

    runner = install_runner(harness, ScriptedFetcher(clock), clock, rng=rng)
    create(harness, "a1", "a2", "a3")
    runner.run_next()
    assert drawn == [PROFILE_DELAY, PROFILE_DELAY]
    assert clock.waits == [PROFILE_DELAY[1], PROFILE_DELAY[1]]


def test_delay_is_longer_while_gate_is_slowed(harness, clock):
    drawn = []
    rng = lambda low, high: drawn.append((low, high)) or low   # noqa: E731
    runner = install_runner(harness, ScriptedFetcher(clock), clock, rng=rng)
    harness.ctx.instagram_gate.slow_listing(3600)
    create(harness, "a1", "a2")
    runner.run_next()
    assert drawn == [SLOW_PROFILE_DELAY]


def test_rest_every_thirty_profiles(harness, clock):
    drawn = []
    rng = lambda low, high: drawn.append((low, high)) or low   # noqa: E731
    runner = install_runner(harness, ScriptedFetcher(clock), clock, rng=rng)
    create(harness, *[f"p{i}" for i in range(REST_EVERY + 2)])
    runner.run_next()
    assert drawn.count(REST_DELAY) == 1
    assert drawn.index(REST_DELAY) == REST_EVERY - 1


# ---- 429: backoff local -------------------------------------------------------------------------------------------
def test_rate_limit_backs_off_locally_without_touching_global_state(harness, clock):
    fetcher = ScriptedFetcher(clock, {"a1": [web_error("rate_limited"), snapshot("a1")]})
    runner = install_runner(harness, fetcher, clock)
    worker = harness.ctx.worker
    worker.pause_for = MagicMock(wraps=worker.pause_for)
    gate = harness.ctx.instagram_gate
    gate.slow_listing = MagicMock(wraps=gate.slow_listing)
    analysis = create(harness, "a1", "a2")
    seen = []

    def inspect(sim):
        row = harness.ctx.profile_analyses.get(analysis["id"])
        if not seen:
            seen.append(dict(row))
            seen.append(items_of(harness, analysis["id"])[0])

    clock.on_wait = inspect
    runner.run_next()

    waiting, item = seen
    assert waiting["status"] == "waiting" and waiting["wait_reason"] == "rate_limited" and waiting["wait_level"] == 1
    assert parse_utc(waiting["waiting_until"]) > clock.moment - timedelta(hours=1)
    assert item["state"] == "pending" and item["attempts"] == 0 and item["error_code"] is None
    done = analysis_of(harness, analysis["id"])
    assert done["status"] == "completed" and done["waiting_until"] is None and done["wait_reason"] is None
    assert fetcher.calls == ["a1", "a1", "a2"]
    assert harness.ctx.profile_analyses.get(analysis["id"])["wait_level"] == 0   # sucesso zera o degrau
    worker.pause_for.assert_not_called()
    gate.slow_listing.assert_not_called()
    untouched_global_state(harness)
    assert sum(clock.waits) >= BACKOFF_SECONDS[0]


def test_waiting_until_is_fifteen_minutes_after_the_response(harness, clock):
    fetcher = ScriptedFetcher(clock, {"a1": [web_error("rate_limited"), snapshot("a1")]})
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1")
    started = clock.now()
    untils = []
    clock.on_wait = lambda _sim: untils.append(harness.ctx.profile_analyses.get(analysis["id"])["waiting_until"])
    runner.run_next()
    assert untils[0] == stamp(started + timedelta(seconds=1.5 + 900))


def test_backoff_steps_grow_and_success_resets_them(harness, clock):
    limited = web_error("rate_limited")
    fetcher = ScriptedFetcher(clock, {"a1": [limited, limited, snapshot("a1")], "a2": [limited, snapshot("a2")]})
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1", "a2")
    levels = {}

    def note(_sim):
        row = harness.ctx.profile_analyses.get(analysis["id"])
        if row["status"] == "waiting":
            levels[row["waiting_until"]] = row["wait_level"]

    clock.on_wait = note
    runner.run_next()
    assert list(levels.values()) == [1, 2, 1]
    assert analysis_of(harness, analysis["id"])["status"] == "completed"


def test_exhausted_backoff_interrupts_and_marks_the_rest(harness, clock):
    fetcher = ScriptedFetcher(clock, {"a2": [web_error("rate_limited")]})
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1", "a2", "a3")
    runner.run_next()

    assert fetcher.calls == ["a1"] + ["a2"] * (len(BACKOFF_SECONDS) + 1)
    done = analysis_of(harness, analysis["id"])
    assert done["status"] == "interrupted" and done["report_ready"] is True and "não foram consultados" in done["error"]
    assert done["finished_at"] and done["pending"] == 0 and done["done"] == 1
    assert done["summary"]["cancelled"] == 2 and done["summary"]["unverified"] == 0
    items = by_name(harness, analysis["id"])
    assert items["a1"]["account_status"] == "active"
    for name in ("a2", "a3"):
        item = items[name]
        assert item["state"] == "cancelled" and item["account_status"] is None
        assert item["checked_at"] is None and item["checked_date"] is None and item["checked_time"] is None
        assert item["error_code"] == "rate_limited" and "Não consultado" in item["error_message"]
        assert item["stories"] is None and item["highlights"] is None
        assert item["views_state"] is None and item["posts_state"] is None
    untouched_global_state(harness)


# ---- pausa global -------------------------------------------------------------------------------------------------
def test_global_pause_waits_without_consuming_attempt(harness, clock):
    fetcher = ScriptedFetcher(clock)
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1")
    paused = {"value": True}
    harness.ctx.worker.downloads_paused = lambda: paused["value"]
    states = []

    def release(sim):
        states.append(dict(harness.ctx.profile_analyses.get(analysis["id"])))
        states.append(items_of(harness, analysis["id"])[0])
        assert fetcher.calls == []
        paused["value"] = len(sim.waits) < 3

    clock.on_wait = release
    runner.run_next()
    assert clock.waits == [runner_module.PAUSE_RECHECK_SECONDS] * 3
    assert states[0]["status"] == "waiting" and states[0]["wait_reason"] == "instagram_pause"
    assert states[0]["waiting_until"] is None
    assert states[1]["state"] == "pending" and states[1]["attempts"] == 0
    assert items_of(harness, analysis["id"])[0]["attempts"] == 1
    assert analysis_of(harness, analysis["id"])["status"] == "completed"


def test_instagram_paused_from_gate_returns_the_item(harness, clock):
    fetcher = ScriptedFetcher(clock, {"a1": [InstagramPaused("instagram"), snapshot("a1")]})
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1")
    runner.run_next()
    item = items_of(harness, analysis["id"])[0]
    assert fetcher.calls == ["a1", "a1"] and item["attempts"] == 1 and item["account_status"] == "active"


def test_real_gate_pause_reaches_default_fetcher_as_instagram_paused(harness):
    gate = harness.ctx.instagram_gate
    gate.publish_pause("instagram")
    fetch = anonymous_profile_fetcher(gate, transport=httpx.MockTransport(lambda _r: pytest.fail("enviou")))
    with pytest.raises(InstagramPaused):
        fetch("perfil", threading.Event())


# ---- erros --------------------------------------------------------------------------------------------------------
def test_timeout_retries_once_then_unverified(harness, clock):
    fetcher = ScriptedFetcher(clock, {"a1": [web_error("timeout")]})
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1", "a2")
    runner.run_next()
    assert fetcher.calls == ["a1", "a1", "a2"]
    items = by_name(harness, analysis["id"])
    assert items["a1"]["account_status"] == "unverified" and items["a1"]["error_code"] == "timeout"
    assert items["a1"]["attempts"] == 2 and items["a1"]["stories"] == "unverified"
    assert items["a2"]["account_status"] == "active"


def test_network_error_after_rate_limit_still_gets_its_retry(harness, clock):
    fetcher = ScriptedFetcher(clock, {"a1": [web_error("rate_limited"), web_error("network_error"), snapshot("a1")]})
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1")
    runner.run_next()
    item = items_of(harness, analysis["id"])[0]
    assert item["account_status"] == "active" and item["attempts"] == 2


@pytest.mark.parametrize("code", ["access_refused", "contract_changed"])
def test_other_errors_are_unverified_without_retry(harness, clock, code):
    fetcher = ScriptedFetcher(clock, {"a1": [web_error(code)]})
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1", "a2")
    runner.run_next()
    assert fetcher.calls == ["a1", "a2"]
    item = by_name(harness, analysis["id"])["a1"]
    assert item["account_status"] == "unverified" and item["error_code"] == code
    assert item["error_message"] == f"Mensagem de {code}." and item["attempts"] == 1
    assert item["checked_at"]


def test_unexpected_exception_is_captured_per_item(harness, clock, caplog):
    fetcher = ScriptedFetcher(clock, {"a1": [RuntimeError("boom")]})
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1", "a2")
    assert runner.run_next() is True
    items = by_name(harness, analysis["id"])
    assert items["a1"]["error_code"] == "internal_error" and items["a1"]["account_status"] == "unverified"
    assert "boom" not in items["a1"]["error_message"]
    assert items["a2"]["account_status"] == "active"
    assert analysis_of(harness, analysis["id"])["status"] == "completed"


def test_base_instagram_error_is_handled(harness, clock):
    fetcher = ScriptedFetcher(clock, {"a1": [InstagramError("unsupported_media", "Não suportado.")]})
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1")
    runner.run_next()
    assert items_of(harness, analysis["id"])[0]["account_status"] == "unverified"


# ---- disjuntor ----------------------------------------------------------------------------------------------------
def test_breaker_stops_consuming_the_list(harness, clock):
    names = [f"p{i}" for i in range(7)]
    refused = {name: [web_error("access_refused")] for name in names[:5]}
    fetcher = ScriptedFetcher(clock, refused)
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, *names)
    seen = []

    def note(_sim):
        row = harness.ctx.profile_analyses.get(analysis["id"])
        if row["status"] == "waiting" and not seen:
            seen.append((row["wait_reason"], row["wait_level"], list(fetcher.calls)))

    clock.on_wait = note
    runner.run_next()
    assert seen == [("too_many_failures", 1, names[:5])]
    assert analysis_of(harness, analysis["id"])["status"] == "completed"
    assert harness.ctx.profile_analyses.get(analysis["id"])["wait_level"] == 0
    untouched_global_state(harness)


def test_confirmed_status_resets_the_breaker(harness, clock):
    names = [f"p{i}" for i in range(9)]
    refused = {name: [web_error("access_refused")] for i, name in enumerate(names) if i != 4}
    refused[names[4]] = [snapshot(names[4], "private")]
    runner = install_runner(harness, ScriptedFetcher(clock, refused), clock)
    analysis = create(harness, *names)
    runner.run_next()
    assert all(wait <= PROFILE_DELAY[1] for wait in clock.waits)   # nenhum backoff
    assert analysis_of(harness, analysis["id"])["status"] == "completed"


def test_breaker_counts_unverified_snapshots_too(harness, clock):
    names = [f"p{i}" for i in range(5)]
    fetcher = ScriptedFetcher(clock, default=snapshot("x", "unverified", error_code="profile_unreadable"))
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, *names, "resto")
    fetcher.results = {"resto": [snapshot("resto")]}
    runner.run_next()
    assert any(wait >= 2.0 for wait in clock.waits) and sum(clock.waits) >= BACKOFF_SECONDS[0]
    assert analysis_of(harness, analysis["id"])["summary"]["unverified"] == 5


def test_breaker_exhausted_interrupts(harness, clock):
    names = [f"p{i}" for i in range(12)]
    fetcher = ScriptedFetcher(clock, default=web_error("access_refused"))
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, *names)
    runner.run_next()
    # 5 até o 1o disparo; depois de cada espera, UM perfil basta para o próximo degrau.
    assert len(fetcher.calls) == 5 + len(BACKOFF_SECONDS)
    done = analysis_of(harness, analysis["id"])
    assert done["status"] == "interrupted"
    rest = items_of(harness, analysis["id"])[len(fetcher.calls):]
    assert rest and all(item["error_code"] == "too_many_failures" for item in rest)
    assert all(item["state"] == "cancelled" and item["account_status"] is None for item in rest)
    assert all(item["checked_at"] is None and "Não consultado" in item["error_message"] for item in rest)
    assert done["summary"]["cancelled"] == len(rest)


# ---- cancelamento -------------------------------------------------------------------------------------------------
def test_cancel_during_run_keeps_results(harness, clock):
    fetcher = ScriptedFetcher(clock)
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1", "a2", "a3")

    def cancel(username):
        if username == "a2":
            response = harness.client.post(f"/api/profile-analyses/{analysis['id']}/cancel")
            assert response.status_code == 200

    fetcher.before = cancel
    runner.run_next()
    assert fetcher.calls == ["a1", "a2"]
    done = analysis_of(harness, analysis["id"])
    assert done["status"] == "cancelled" and done["finished_at"] and done["summary"]["cancelled"] == 1
    assert done["report_ready"] is True and done["done"] == 2
    items = by_name(harness, analysis["id"])
    assert items["a1"]["state"] == "done" and items["a3"]["state"] == "cancelled" and items["a3"]["checked_at"] is None
    assert items["a3"]["error_code"] is None and items["a3"]["error_message"] is None


def test_cancel_during_backoff_wait(harness, clock):
    fetcher = ScriptedFetcher(clock, {"a1": [web_error("rate_limited")]})
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1", "a2")

    def cancel(sim):
        if len(sim.waits) == 3:
            harness.client.post(f"/api/profile-analyses/{analysis['id']}/cancel")

    clock.on_wait = cancel
    runner.run_next()
    assert fetcher.calls == ["a1"] and len(clock.waits) == 3
    done = analysis_of(harness, analysis["id"])
    assert done["status"] == "cancelled" and done["waiting_until"] is None
    assert {item["state"] for item in items_of(harness, analysis["id"])} == {"cancelled"}


def test_cancel_during_global_pause(harness, clock):
    fetcher = ScriptedFetcher(clock)
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1")
    harness.ctx.worker.downloads_paused = lambda: True
    clock.on_wait = lambda _sim: harness.client.post(f"/api/profile-analyses/{analysis['id']}/cancel")
    runner.run_next()
    assert fetcher.calls == [] and analysis_of(harness, analysis["id"])["status"] == "cancelled"


def test_cancel_while_waiting_for_gate_slot(harness, clock):
    fetcher = ScriptedFetcher(clock)
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1", "a2")

    def cancelled_in_gate(username):
        harness.client.post(f"/api/profile-analyses/{analysis['id']}/cancel")
        raise GateCancelled()

    fetcher.before = cancelled_in_gate
    runner.run_next()
    items = items_of(harness, analysis["id"])
    assert [item["state"] for item in items] == ["cancelled", "cancelled"] and items[0]["attempts"] == 0
    assert analysis_of(harness, analysis["id"])["status"] == "cancelled"


def test_cancel_interrupts_the_event_wait_quickly(harness, monkeypatch):
    # Fatia de 60 s: só o evento acordado por `cancel()` faz o executor terminar dentro do prazo do `join`.
    monkeypatch.setattr(runner_module, "WAIT_SLICE_SECONDS", 60.0)
    ctx = harness.ctx
    ctx.profile_fetcher = ScriptedFetcher(default=web_error("rate_limited"))
    analysis = create(harness, "a1")
    runner = ProfileAnalysisRunner(ctx)
    ctx.profile_runner = runner
    worker = threading.Thread(target=runner.run_next, daemon=True)
    worker.start()
    assert wait_until(lambda: analysis_of(harness, analysis["id"])["status"] == "waiting", timeout=5)
    assert harness.client.post(f"/api/profile-analyses/{analysis['id']}/cancel").status_code == 200
    worker.join(5)
    assert not worker.is_alive()
    assert analysis_of(harness, analysis["id"])["status"] == "cancelled"


# ---- reinício -----------------------------------------------------------------------------------------------------
def test_recovery_requeues_running_item_and_resumes(harness_factory, clock):
    first = harness_factory()
    fetcher = ScriptedFetcher(clock)
    analysis = create(first, "a1", "a2")
    repo = first.ctx.profile_analyses
    repo.claim_next("2026-11-01T12:00:00.000Z")
    item = repo.next_item(analysis["id"])
    repo.start_item(item["id"])
    root = first.root
    first.__exit__(None, None, None)

    second = harness_factory(root=root)
    resumed = items_of(second, analysis["id"])[0]
    assert resumed["state"] == "pending" and resumed["attempts"] == 1
    assert analysis_of(second, analysis["id"])["status"] == "running"
    install_runner(second, fetcher, clock).run_next()
    assert analysis_of(second, analysis["id"])["status"] == "completed" and fetcher.calls == ["a1", "a2"]


def test_recovery_respects_persisted_waiting_until(harness_factory, clock):
    first = harness_factory()
    analysis = create(first, "a1")
    repo = first.ctx.profile_analyses
    repo.claim_next("2026-11-01T12:00:00.000Z")
    repo.set_waiting(analysis["id"], "rate_limited", stamp(clock.now() + timedelta(seconds=600)), 2)
    root = first.root
    first.__exit__(None, None, None)

    second = harness_factory(root=root)
    assert analysis_of(second, analysis["id"])["status"] == "waiting"
    fetcher = ScriptedFetcher(clock)
    calls_at = []
    fetcher.before = lambda _u: calls_at.append(clock.now())
    runner = install_runner(second, fetcher, clock)
    start = clock.now()
    runner.run_next()
    assert calls_at == [start + timedelta(seconds=600)]
    assert analysis_of(second, analysis["id"])["status"] == "completed"


def test_recovery_of_expired_wait_keeps_the_step(harness_factory, clock):
    first = harness_factory()
    analysis = create(first, "a1")
    repo = first.ctx.profile_analyses
    repo.claim_next("2026-11-01T12:00:00.000Z")
    repo.set_waiting(analysis["id"], "rate_limited", stamp(clock.now() - timedelta(seconds=1)), 3)
    root = first.root
    first.__exit__(None, None, None)

    second = harness_factory(root=root)
    fetcher = ScriptedFetcher(clock, {"a1": [web_error("rate_limited")]})
    install_runner(second, fetcher, clock).run_next()
    assert fetcher.calls == ["a1"]   # o degrau 3 já tinha sido usado: o novo 429 interrompe
    assert analysis_of(second, analysis["id"])["status"] == "interrupted"


def test_recovery_of_instagram_pause_wait_resumes(harness_factory, clock):
    first = harness_factory()
    analysis = create(first, "a1")
    repo = first.ctx.profile_analyses
    repo.claim_next("2026-11-01T12:00:00.000Z")
    repo.set_waiting(analysis["id"], "instagram_pause", None, 0)
    root = first.root
    first.__exit__(None, None, None)

    second = harness_factory(root=root)
    install_runner(second, ScriptedFetcher(clock), clock).run_next()
    assert analysis_of(second, analysis["id"])["status"] == "completed"


def test_recovery_finishes_cancel_requested(harness_factory):
    first = harness_factory()
    analysis = create(first, "a1", "a2")
    repo = first.ctx.profile_analyses
    repo.claim_next("2026-11-01T12:00:00.000Z")
    repo.start_item(repo.next_item(analysis["id"])["id"])
    assert repo.request_cancel(analysis["id"]) == "running"
    root = first.root
    first.__exit__(None, None, None)

    second = harness_factory(root=root)
    done = analysis_of(second, analysis["id"])
    assert done["status"] == "cancelled" and done["finished_at"] and done["summary"]["cancelled"] == 2
    assert recover_profile_analyses(second.ctx) == 0


def test_breaker_streak_survives_restart(harness_factory, clock):
    first = harness_factory()
    names = [f"p{i}" for i in range(6)]
    analysis = create(first, *names)
    repo = first.ctx.profile_analyses
    for _ in range(4):
        item = repo.next_item(analysis["id"])
        repo.save_result(item["id"], {"account_status": "unverified"}, "2026-11-01T12:00:00.000Z")
    root = first.root
    first.__exit__(None, None, None)

    second = harness_factory(root=root)
    fetcher = ScriptedFetcher(clock, {"p4": [web_error("access_refused")]})
    install_runner(second, fetcher, clock).run_next()
    assert sum(clock.waits) >= BACKOFF_SECONDS[0]


def test_stop_leaves_analysis_open_and_item_pending(harness, clock):
    fetcher = ScriptedFetcher(clock, {"a1": [web_error("rate_limited")]})
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1")
    clock.on_wait = lambda _sim: runner._stop.set()
    runner.run_next()
    assert analysis_of(harness, analysis["id"])["status"] == "waiting"
    assert analysis_of(harness, analysis["id"])["waiting_until"]
    assert items_of(harness, analysis["id"])[0]["state"] == "pending"
    runner._stop.clear()


# ---- ponta a ponta ------------------------------------------------------------------------------------------------
def test_end_to_end_with_thread_real_fetcher_and_no_session(harness_factory, monkeypatch):
    monkeypatch.setattr(runner_module, "PROFILE_DELAY", (0.0, 0.0))
    provider = MemorySessionProvider()
    provider.get = MagicMock(side_effect=AssertionError("a análise não pode ler a sessão"))
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/inexistente/reels/":
            return httpx.Response(404, text="x", headers={"content-type": "text/html"})
        return httpx.Response(200, text=profile_html(), headers={"content-type": "text/html; charset=utf-8"})

    harness = harness_factory(session_provider=provider)
    ctx = harness.ctx
    ctx.profile_fetcher = anonymous_profile_fetcher(ctx.instagram_gate, transport=httpx.MockTransport(handler))
    analysis = create(harness, "perfil", "inexistente")
    ctx.profile_runner.start()
    assert wait_until(lambda: analysis_of(harness, analysis["id"])["status"] == "completed", timeout=15)
    ctx.profile_runner.stop()

    items = by_name(harness, analysis["id"])
    assert items["perfil"]["account_status"] == "active" and items["perfil"]["posts_count"] == 8630
    assert items["inexistente"]["account_status"] == "unavailable"
    assert len(requests) == 2 and all("cookie" not in request.headers for request in requests)
    provider.get.assert_not_called()
    assert harness.ctx.app_settings.get_state(PAUSE_STATE_KEY) is None


def test_default_fetcher_respects_cancel_in_gate(harness):
    gate = InstagramRequestGate(interval=(50.0, 50.0))
    with gate.slot():
        pass
    cancel = threading.Event()
    cancel.set()
    fetch = anonymous_profile_fetcher(gate, transport=httpx.MockTransport(lambda _r: pytest.fail("enviou")))
    with pytest.raises(GateCancelled):
        fetch("perfil", cancel)


def test_default_fetcher_stays_out_of_the_hourly_page_cap(harness):
    ctx = harness.ctx
    gate = ctx.instagram_gate
    gate.wait_listing_page = MagicMock(side_effect=AssertionError("a análise não usa o teto por hora"))
    slots = MagicMock(wraps=gate.slot)
    gate.slot = slots
    fetch = anonymous_profile_fetcher(gate, transport=httpx.MockTransport(
        lambda _r: httpx.Response(200, text=profile_html(), headers={"content-type": "text/html"})))
    assert fetch("perfil", None).account_status == "active"
    gate.wait_listing_page.assert_not_called()
    assert ctx.app_settings.get_state(PAGES_STATE_KEY) is None
    slots.assert_called_once()   # o portão continua: 1 em voo, intervalo e pausa publicada


# ---- isolamento do item ------------------------------------------------------------------------------------------
def test_stop_wakes_the_event_wait(harness, monkeypatch):
    monkeypatch.setattr(runner_module, "WAIT_SLICE_SECONDS", 60.0)
    ctx = harness.ctx
    ctx.profile_fetcher = ScriptedFetcher(default=web_error("rate_limited"))
    analysis = create(harness, "a1")
    runner = ProfileAnalysisRunner(ctx)
    ctx.profile_runner = runner
    runner.start()
    thread = runner._thread
    assert wait_until(lambda: analysis_of(harness, analysis["id"])["status"] == "waiting", timeout=5)
    runner.stop(timeout=5)
    assert not thread.is_alive()
    assert analysis_of(harness, analysis["id"])["status"] == "waiting"


def test_pause_published_only_in_the_gate_is_respected(harness, clock):
    fetcher = ScriptedFetcher(clock)
    runner = install_runner(harness, fetcher, clock)
    gate = harness.ctx.instagram_gate
    gate.publish_pause("instagram")
    analysis = create(harness, "a1")

    def release(sim):
        assert fetcher.calls == []
        if len(sim.waits) == 2:
            gate.clear_pause()

    clock.on_wait = release
    runner.run_next()
    assert clock.waits == [runner_module.PAUSE_RECHECK_SECONDS] * 2 and fetcher.calls == ["a1"]
    assert analysis_of(harness, analysis["id"])["status"] == "completed"


@pytest.mark.parametrize("answer", ["não é um retrato", 7])
def test_invalid_fetcher_answer_is_contained(harness, clock, answer):
    fetcher = ScriptedFetcher(clock, {"a1": [answer]})
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1", "a2")
    runner.run_next()
    items = by_name(harness, analysis["id"])
    assert items["a1"]["error_code"] == "internal_error" and items["a2"]["account_status"] == "active"


def test_result_the_database_refuses_becomes_unverified_and_keeps_the_interval(harness, clock):
    fetcher = ScriptedFetcher(clock, {"a1": [snapshot("a1", posts_count=10**20)]})
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1", "a2")
    runner.run_next()
    assert fetcher.calls == ["a1", "a2"] and clock.waits == [PROFILE_DELAY[0]]
    items = by_name(harness, analysis["id"])
    assert items["a1"]["state"] == "done" and items["a1"]["account_status"] == "unverified"
    assert items["a1"]["error_code"] == "internal_error" and items["a1"]["posts_count"] is None
    assert items["a1"]["attempts"] == 1 and items["a2"]["account_status"] == "active"
    assert analysis_of(harness, analysis["id"])["status"] == "completed"


def test_failure_after_saving_does_not_redo_the_item(harness, clock):
    fetcher = ScriptedFetcher(clock, {"a1": [web_error("access_refused")]})
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1", "a2")
    repo = harness.ctx.profile_analyses
    real = repo.trailing_unverified
    repo.trailing_unverified = MagicMock(side_effect=[sqlite3.OperationalError("database is locked")])

    def heal(_sim):
        repo.trailing_unverified = real

    clock.on_wait = heal
    runner.run_next()
    assert fetcher.calls == ["a1", "a2"]
    assert by_name(harness, analysis["id"])["a1"]["error_code"] == "access_refused"
    assert clock.waits[0] == STORAGE_RETRY_SECONDS[0]


def test_storage_failure_never_becomes_a_get_loop(harness, clock):
    fetcher = ScriptedFetcher(clock)
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1", "a2")
    repo = harness.ctx.profile_analyses
    real = repo.save_result
    repo.save_result = MagicMock(side_effect=sqlite3.OperationalError("disk I/O error"))

    def heal(sim):
        if len(sim.waits) == 7:
            repo.save_result = real

    clock.on_wait = heal
    runner.run_next()
    assert fetcher.calls == ["a1"] * (MAX_ATTEMPTS + 1) + ["a2"]
    assert max(clock.waits) == STORAGE_RETRY_SECONDS[-1]         # backoff em memória, sem GET
    items = by_name(harness, analysis["id"])
    assert items["a1"]["state"] == "done" and items["a1"]["account_status"] == "unverified"
    assert items["a1"]["error_code"] == "internal_error" and items["a2"]["account_status"] == "active"


def test_permanent_failure_gives_up_and_frees_the_queue(harness, clock):
    fetcher = ScriptedFetcher(clock)
    runner = install_runner(harness, fetcher, clock)
    first = create(harness, "a1", "a2")
    second = create(harness, "b1")
    runner._global_pause = MagicMock(side_effect=RuntimeError("defeito"))
    runner.run_next()
    assert fetcher.calls == []
    finished = analysis_of(harness, first["id"])
    assert finished["status"] == "interrupted" and "registrar o andamento" in finished["error"]
    assert {item["state"] for item in items_of(harness, first["id"])} == {"cancelled"}
    assert {item["error_code"] for item in items_of(harness, first["id"])} == {"internal_error"}
    assert all(item["checked_at"] is None for item in items_of(harness, first["id"]))
    runner._global_pause = MagicMock(return_value=False)
    runner.run_next()
    assert analysis_of(harness, second["id"])["status"] == "completed"


def test_rate_limit_with_failing_write_has_a_ceiling(harness, clock):
    fetcher = ScriptedFetcher(clock, results={"a1": [InstagramWebError("rate_limited", "limite")]})
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1", "a2")
    harness.ctx.profile_analyses.set_waiting = MagicMock(side_effect=sqlite3.OperationalError("disk I/O error"))
    runner.run_next()
    assert len(fetcher.calls) <= STORAGE_MAX_FAILURES
    assert analysis_of(harness, analysis["id"])["status"] == "interrupted"


def test_canary_with_failing_write_has_a_ceiling(harness, clock):
    usernames = [f"u{index}" for index in range(CANARY_EVERY + 1)]
    fetcher = ScriptedFetcher(clock, default=snapshot("x", status="unavailable"))
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, *usernames)
    harness.ctx.profile_analyses.reopen_items = MagicMock(side_effect=sqlite3.OperationalError("disk I/O error"))
    runner.run_next()
    assert fetcher.calls.count(CANARY_PROFILE) <= STORAGE_MAX_FAILURES
    assert analysis_of(harness, analysis["id"])["status"] == "interrupted"


def test_private_canary_does_not_confirm_the_sequence(harness, clock):
    usernames = [f"u{index}" for index in range(CANARY_EVERY)]
    answers = {CANARY_PROFILE: [snapshot(CANARY_PROFILE, status="private")]}
    fetcher = ScriptedFetcher(clock, results=answers, default=snapshot("x", status="unavailable"))
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, *usernames)

    def cancel_after_canary(_sim):
        if CANARY_PROFILE in fetcher.calls:
            harness.ctx.profile_analyses.request_cancel(analysis["id"])

    clock.on_wait = cancel_after_canary
    runner.run_next()
    assert fetcher.calls.count(CANARY_PROFILE) == 1
    assert all(item["checked_at"] is None for item in items_of(harness, analysis["id"]))


def test_attempts_survive_reclaim_and_restart(harness):
    analysis = create(harness, "a1")
    repo = harness.ctx.profile_analyses
    repo.claim_next("2026-11-01T12:00:00.000Z")
    repo.start_item(repo.next_item(analysis["id"])["id"])
    repo.claim_next("2026-11-01T12:00:00.000Z")
    assert items_of(harness, analysis["id"])[0]["attempts"] == 1
    repo.start_item(repo.next_item(analysis["id"])["id"])
    repo.requeue_running(analysis["id"])
    assert items_of(harness, analysis["id"])[0]["attempts"] == 2


class Crash(BaseException):
    """Processo caindo durante a consulta: passa por todos os `except Exception` do executor."""


def test_crash_cycle_stops_at_the_attempt_ceiling(harness, clock):
    def crash(_username):
        raise Crash

    fetcher = ScriptedFetcher(clock)
    fetcher.before = crash
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1", "a2")
    for _ in range(10):
        try:
            runner.run_next()
        except Crash:
            assert recover_profile_analyses(harness.ctx) == 1
            continue
        break
    else:
        pytest.fail("o ciclo de quedas não terminou")
    assert fetcher.calls.count("a1") == MAX_ATTEMPTS + 1
    item = items_of(harness, analysis["id"])[0]
    assert item["account_status"] == "unverified" and item["error_code"] == "internal_error"
    assert item["state"] == "done" and item["checked_at"] is None


def test_item_in_flight_at_restart_is_consulted_once_more(harness, clock):
    fetcher = ScriptedFetcher(clock)

    def crash_once(_username):
        fetcher.before = None
        raise Crash

    fetcher.before = crash_once
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1")
    with pytest.raises(Crash):
        runner.run_next()
    recover_profile_analyses(harness.ctx)
    runner.run_next()
    item = items_of(harness, analysis["id"])[0]
    assert fetcher.calls == ["a1", "a1"] and item["account_status"] == "active" and item["attempts"] == 2


def test_attempt_ceiling_across_restarts_takes_away_the_timeout_retry(harness, clock):
    fetcher = ScriptedFetcher(clock, {"a1": [web_error("timeout")]})
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1")
    repo = harness.ctx.profile_analyses
    repo.claim_next("2026-11-01T12:00:00.000Z")
    repo.start_item(repo.next_item(analysis["id"])["id"])
    recover_profile_analyses(harness.ctx)
    runner.run_next()
    item = items_of(harness, analysis["id"])[0]
    assert fetcher.calls == ["a1"] and item["error_code"] == "timeout" and item["attempts"] == 2


# ---- disjuntor no fim da lista e cancelamento no último degrau ---------------------------------------------------
def test_breaker_on_the_last_item_finishes_completed(harness, clock):
    names = [f"p{i}" for i in range(5)]
    fetcher = ScriptedFetcher(clock, default=web_error("access_refused"))
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, *names)
    statuses = []
    clock.on_wait = lambda _s: statuses.append(harness.ctx.profile_analyses.get(analysis["id"])["status"])
    runner.run_next()
    assert "waiting" not in statuses and clock.waits == [PROFILE_DELAY[0]] * 4
    done = analysis_of(harness, analysis["id"])
    assert done["status"] == "completed" and done["error"] is None and done["summary"]["unverified"] == 5


def test_breaker_with_steps_used_on_the_last_item_is_not_interrupted(harness, clock):
    names = [f"p{i}" for i in range(5 + len(BACKOFF_SECONDS))]
    fetcher = ScriptedFetcher(clock, default=web_error("access_refused"))
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, *names)
    runner.run_next()
    done = analysis_of(harness, analysis["id"])
    assert done["status"] == "completed" and done["error"] is None and done["summary"]["cancelled"] == 0


def test_cancel_during_the_final_step_ends_cancelled(harness, clock):
    fetcher = ScriptedFetcher(clock, {"a1": [web_error("rate_limited")]})
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1", "a2")

    def cancel_on_last(_username):
        if len(fetcher.calls) == len(BACKOFF_SECONDS) + 1:
            harness.client.post(f"/api/profile-analyses/{analysis['id']}/cancel")

    fetcher.before = cancel_on_last
    runner.run_next()
    done = analysis_of(harness, analysis["id"])
    assert done["status"] == "cancelled" and done["error"] is None
    items = items_of(harness, analysis["id"])
    assert all(item["state"] == "cancelled" and item["error_code"] is None for item in items)


def test_interruption_is_one_transaction(harness):
    analysis = create(harness, "a1")
    repo = harness.ctx.profile_analyses
    repo.claim_next("2026-11-01T12:00:00.000Z")
    harness.ctx.db.execute(
        "CREATE TRIGGER falha BEFORE UPDATE OF status ON profile_analyses WHEN NEW.status = 'interrupted'"
        " BEGIN SELECT RAISE(ABORT, 'falha'); END")
    with pytest.raises(sqlite3.IntegrityError):
        repo.finish_interrupted(analysis["id"], code="rate_limited", message="m", error="e", now="agora")
    assert items_of(harness, analysis["id"])[0]["state"] == "pending"


# ---- canário ------------------------------------------------------------------------------------------------------
def gone(names, *then) -> dict:
    return {name: [snapshot(name, "unavailable", error_code="profile_unavailable"), *then] for name in names}


def test_active_canary_accepts_the_sequence_and_creates_no_item(harness, clock):
    names = [f"u{i}" for i in range(CANARY_EVERY + 2)]
    fetcher = ScriptedFetcher(clock, gone(names))
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, *names)
    runner.run_next()
    assert fetcher.calls == [*names[:CANARY_EVERY], CANARY_PROFILE, *names[CANARY_EVERY:]]
    done = analysis_of(harness, analysis["id"])
    assert done["status"] == "completed" and done["total"] == len(names)
    assert done["summary"]["unavailable"] == len(names)
    assert CANARY_PROFILE not in by_name(harness, analysis["id"])
    assert all(wait <= PROFILE_DELAY[1] for wait in clock.waits)


def test_active_canary_resets_the_count(harness, clock):
    names = [f"u{i}" for i in range(CANARY_EVERY * 2 + 1)]
    fetcher = ScriptedFetcher(clock, gone(names))
    runner = install_runner(harness, fetcher, clock)
    create(harness, *names)
    runner.run_next()
    assert fetcher.calls.count(CANARY_PROFILE) == 2
    assert fetcher.calls.index(CANARY_PROFILE) == CANARY_EVERY


def test_canary_runs_on_the_last_item_too(harness, clock):
    names = [f"u{i}" for i in range(CANARY_EVERY)]
    fetcher = ScriptedFetcher(clock, gone(names))
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, *names)
    runner.run_next()
    assert fetcher.calls[-1] == CANARY_PROFILE
    assert analysis_of(harness, analysis["id"])["status"] == "completed"


def test_blocked_canary_returns_the_sequence_and_backs_off(harness, clock):
    names = [f"u{i}" for i in range(CANARY_EVERY + 1)]
    first_pass = gone(names[:CANARY_EVERY], snapshot("depois"))
    fetcher = ScriptedFetcher(clock, {**first_pass, CANARY_PROFILE: [snapshot(CANARY_PROFILE, "unavailable")]})
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, *names)
    seen = []

    def note(_sim):
        row = harness.ctx.profile_analyses.get(analysis["id"])
        if row["status"] == "waiting" and not seen:
            seen.append((row["wait_reason"], row["wait_level"], items_of(harness, analysis["id"])))

    clock.on_wait = note
    runner.run_next()
    reason, level, frozen = seen[0]
    assert reason == "too_many_failures" and level == 1
    for item in frozen[:CANARY_EVERY]:
        assert item["state"] == "pending" and item["account_status"] is None and item["checked_at"] is None
        assert item["error_code"] is None and item["error_message"] is None and item["attempts"] == 0
    assert fetcher.calls.count(names[0]) == 2 and fetcher.calls.count(CANARY_PROFILE) == 1
    done = analysis_of(harness, analysis["id"])
    assert done["status"] == "completed" and done["summary"]["active"] == len(names)
    untouched_global_state(harness)


@pytest.mark.parametrize(("answer", "reason"), [
    (web_error("rate_limited"), "rate_limited"),
    (web_error("timeout"), "too_many_failures"),
    (RuntimeError("boom"), "too_many_failures"),
    (snapshot(CANARY_PROFILE, "unverified"), "too_many_failures"),
])
def test_canary_without_confirmation_backs_off_with_its_reason(harness, clock, answer, reason):
    names = [f"u{i}" for i in range(CANARY_EVERY)]
    fetcher = ScriptedFetcher(clock, {**gone(names, snapshot("depois")), CANARY_PROFILE: [answer, snapshot("ok")]})
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, *names)
    reasons = set()
    clock.on_wait = lambda _s: reasons.add(harness.ctx.profile_analyses.get(analysis["id"])["wait_reason"])
    runner.run_next()
    assert reason in reasons and analysis_of(harness, analysis["id"])["status"] == "completed"


def test_blocked_canary_exhausts_into_interrupted(harness, clock):
    names = [f"u{i}" for i in range(CANARY_EVERY + 2)]
    fetcher = ScriptedFetcher(clock, default=snapshot("x", "unavailable"))
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, *names)
    runner.run_next()
    assert fetcher.calls.count(CANARY_PROFILE) == len(BACKOFF_SECONDS) + 1
    done = analysis_of(harness, analysis["id"])
    assert done["status"] == "interrupted" and "não foram consultados" in done["error"]
    assert done["summary"]["cancelled"] == len(names) and done["summary"]["unavailable"] == 0
    for item in items_of(harness, analysis["id"]):
        assert item["state"] == "cancelled" and item["error_code"] == "too_many_failures"
        assert item["checked_at"] is None and item["account_status"] is None


def test_canary_sequence_survives_restart(harness_factory, clock):
    first = harness_factory()
    names = [f"u{i}" for i in range(CANARY_EVERY + 1)]
    analysis = create(first, *names)
    repo = first.ctx.profile_analyses
    for _ in range(CANARY_EVERY):
        item = repo.next_item(analysis["id"])
        repo.save_result(item["id"], {"account_status": "unavailable"}, "2026-11-01T12:00:00.000Z")
    root = first.root
    first.__exit__(None, None, None)

    second = harness_factory(root=root)
    fetcher = ScriptedFetcher(clock)
    install_runner(second, fetcher, clock).run_next()
    assert fetcher.calls == [CANARY_PROFILE, names[-1]]


def test_fast_pace_has_no_own_delay_only_the_rare_rest(harness, clock):
    drawn = []

    def rng(low, high):
        drawn.append((low, high))
        return low

    fetcher = ScriptedFetcher(clock)
    runner = install_runner(harness, fetcher, clock, rng, start_fast=True)
    analysis = create(harness, *[f"p{i}" for i in range(runner_module.FAST_REST_EVERY + 2)])
    runner.run_next()
    assert drawn == [runner_module.FAST_REST_DELAY]
    assert clock.waits == [runner_module.FAST_REST_DELAY[0]]
    assert analysis_of(harness, analysis["id"])["cautious_pace"] is False


def test_never_two_consultations_at_once(harness, clock):
    active = []

    def fetch(username, _cancel):
        active.append(username)
        assert len(active) == 1
        active.clear()
        return snapshot(username)

    runner = install_runner(harness, fetch, clock, start_fast=True)
    create(harness, "a1", "a2", "a3")
    assert runner.run_next() is True


def test_one_unreadable_profile_is_not_a_limit_sign(harness, clock):
    fetcher = ScriptedFetcher(clock, {"a2": [snapshot("a2", "unverified")]})
    runner = install_runner(harness, fetcher, clock, start_fast=True)
    analysis = create(harness, "a1", "a2", "a3")
    runner.run_next()
    assert analysis_of(harness, analysis["id"])["cautious_pace"] is False


def test_two_unverified_in_a_row_are_a_limit_sign(harness, clock):
    answers = {name: [snapshot(name, "unverified")] for name in ("a2", "a3")}
    fetcher = ScriptedFetcher(clock, answers)
    runner = install_runner(harness, fetcher, clock, start_fast=True)
    analysis = create(harness, "a1", "a2", "a3", "a4")
    runner.run_next()
    assert analysis_of(harness, analysis["id"])["cautious_pace"] is True


@pytest.mark.parametrize("code", ["profile_restricted", "profile_unreadable"])
def test_doubtful_answer_is_read_again_once_and_the_second_answer_counts(harness, clock, code):
    doubtful = snapshot("a1", "restricted" if code == "profile_restricted" else "unverified", error_code=code)
    fetcher = ScriptedFetcher(clock, {"a1": [doubtful, snapshot("a1")]})
    runner = install_runner(harness, fetcher, clock, start_fast=True)
    analysis = create(harness, "a1", "a2")
    runner.run_next()
    assert fetcher.calls == ["a1", "a1", "a2"]
    assert by_name(harness, analysis["id"])["a1"]["account_status"] == "active"


def test_cancel_between_read_and_reread_costs_one_read_on_resume_and_one_row(harness, clock):
    doubtful = snapshot("a1", "unverified", error_code="profile_unreadable")
    fetcher = ScriptedFetcher(clock, {"a1": [doubtful]})
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1", "a2")
    clock.on_wait = lambda _sim: harness.ctx.profile_analyses.request_cancel(analysis["id"])
    runner.run_next()
    assert fetcher.calls == ["a1"]
    assert analysis_of(harness, analysis["id"])["status"] == "cancelled"
    clock.on_wait = None
    assert harness.client.post(f"/api/profile-analyses/{analysis['id']}/resume").status_code == 200
    runner.run_next()
    assert fetcher.calls == ["a1", "a1", "a1", "a2"]
    items = items_of(harness, analysis["id"])
    assert [item["username"] for item in items] == ["a1", "a2"]
    assert items[0]["account_status"] == "unverified" and items[0]["attempts"] == 2


def test_restricted_confirmed_on_the_second_read_is_not_a_limit_sign(harness, clock):
    restricted = snapshot("a1", "restricted", error_code="profile_restricted", posts_state="known", posts_count=51,
                          posts_exact=1)
    fetcher = ScriptedFetcher(clock, {"a1": [restricted]})
    runner = install_runner(harness, fetcher, clock, start_fast=True)
    analysis = create(harness, "a1", "a2")
    runner.run_next()
    item = by_name(harness, analysis["id"])["a1"]
    assert fetcher.calls == ["a1", "a1", "a2"] and item["account_status"] == "restricted"
    assert item["posts_count"] == 51 and item["stories"] == "unverified" and item["highlights"] == "unverified"
    done = analysis_of(harness, analysis["id"])
    assert done["summary"]["restricted"] == 1 and done["cautious_pace"] is False


@pytest.mark.parametrize("answer", [web_error("rate_limited"), web_error("access_refused")])
def test_first_limit_sign_slows_down_until_the_end(harness, clock, answer):
    fetcher = ScriptedFetcher(clock, {"a2": [answer, snapshot("a2")]})
    runner = install_runner(harness, fetcher, clock, start_fast=True)
    analysis = create(harness, "a1", "a2", "a3", "a4")
    runner.run_next()
    assert analysis_of(harness, analysis["id"])["cautious_pace"] is True
    assert PROFILE_DELAY[0] <= clock.waits[-1] <= PROFILE_DELAY[1]


def test_network_retry_alone_keeps_the_fast_pace(harness, clock):
    fetcher = ScriptedFetcher(clock, {"a1": [web_error("timeout"), snapshot("a1")]})
    runner = install_runner(harness, fetcher, clock, start_fast=True)
    analysis = create(harness, "a1", "a2")
    runner.run_next()
    assert analysis_of(harness, analysis["id"])["cautious_pace"] is False


def test_unavailable_alone_keeps_the_fast_pace(harness, clock):
    fetcher = ScriptedFetcher(clock, {"a1": [snapshot("a1", "unavailable")]})
    runner = install_runner(harness, fetcher, clock, start_fast=True)
    analysis = create(harness, "a1", "a2")
    runner.run_next()
    assert analysis_of(harness, analysis["id"])["cautious_pace"] is False


def test_failed_canary_slows_down(harness, clock):
    names = [f"u{i}" for i in range(CANARY_EVERY)] + ["depois"]
    fetcher = ScriptedFetcher(clock, default=snapshot("x", "unavailable"))
    runner = install_runner(harness, fetcher, clock, start_fast=True)
    analysis = create(harness, *names)
    clock.on_wait = lambda _sim: harness.ctx.profile_analyses.request_cancel(analysis["id"])
    runner.run_next()
    assert analysis_of(harness, analysis["id"])["cautious_pace"] is True


def test_cautious_pace_survives_restart_and_resume(harness_factory, clock):
    first = harness_factory()
    analysis = create(first, "a1", "a2", "a3")
    first.ctx.profile_analyses.set_cautious(analysis["id"])
    root = first.root
    first.__exit__(None, None, None)

    second = harness_factory(root=root)
    fetcher = ScriptedFetcher(clock)
    runner = install_runner(second, fetcher, clock, start_fast=True)
    runner.run_next()
    assert clock.waits and all(PROFILE_DELAY[0] <= wait <= PROFILE_DELAY[1] for wait in clock.waits)
    second.client.post(f"/api/profile-analyses/{analysis['id']}/cancel")
    assert analysis_of(second, analysis["id"])["cautious_pace"] is True


def test_global_slow_pace_also_applies_to_the_fast_pace(harness, clock):
    drawn = []

    def rng(low, high):
        drawn.append((low, high))
        return low

    fetcher = ScriptedFetcher(clock)
    runner = install_runner(harness, fetcher, clock, rng, start_fast=True)
    harness.ctx.instagram_gate.slow_listing()
    create(harness, "a1", "a2")
    runner.run_next()
    assert drawn == [SLOW_PROFILE_DELAY]


def test_pace_is_pinned():
    assert runner_module.FAST_REST_EVERY == 100 and runner_module.FAST_REST_DELAY == (30.0, 60.0)
    assert PROFILE_DELAY == (3.0, 6.0) and SLOW_PROFILE_DELAY == (10.0, 20.0)
    assert REST_EVERY == 30 and REST_DELAY == (45.0, 90.0)
    assert BACKOFF_SECONDS == (900.0, 1800.0, 3600.0)
    assert (CANARY_EVERY, CANARY_PROFILE, MAX_ATTEMPTS) == (10, "instagram", 2)
    assert runner_module.BREAKER_LIMIT == 5


def test_next_analysis_gets_a_fresh_interrupt(harness, clock):
    seen = []
    first = create(harness, "a1", "a2")

    def fetch(username, cancel):
        seen.append(cancel.is_set())
        if username == "a1":
            harness.ctx.profile_analyses.request_cancel(first["id"])
            runner.cancel(first["id"])
        return snapshot(username)

    runner = install_runner(harness, fetch, clock)
    create(harness, "b1")
    runner.run_next()
    runner.run_next()
    assert seen == [False, False]


def test_running_analysis_keeps_its_turn_over_an_older_resumed_one(harness, clock):
    fetcher = ScriptedFetcher(clock)
    runner = install_runner(harness, fetcher, clock)
    older = create(harness, "a1", "a2")
    harness.client.post(f"/api/profile-analyses/{older['id']}/cancel")
    newer = create(harness, "b1")
    repo = harness.ctx.profile_analyses
    repo.claim_next("2026-11-01T12:00:00.000Z")
    assert harness.client.post(f"/api/profile-analyses/{older['id']}/resume").status_code == 200
    assert repo.current_id() == newer["id"]
    runner.run_next()
    assert fetcher.calls == ["b1"]
    assert repo.current_id() == older["id"]
    assert harness.client.get("/api/profile-analyses").json()["open_id"] == older["id"]


def test_item_closed_without_answer_has_no_time(harness, clock):
    fetcher = ScriptedFetcher(clock)
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1")
    repo = harness.ctx.profile_analyses
    repo.claim_next("2026-11-01T12:00:00.000Z")
    item = repo.next_item(analysis["id"])
    for _ in range(MAX_ATTEMPTS + 1):
        repo.start_item(item["id"])
    repo.requeue_running(analysis["id"])
    runner.run_next()
    assert fetcher.calls == []
    closed = by_name(harness, analysis["id"])["a1"]
    assert closed["account_status"] == "unverified" and closed["error_code"] == "internal_error"
    assert closed["checked_at"] is None and closed["checked_time"] is None


def test_resume_clears_the_attempts_of_what_was_not_consulted(harness):
    analysis = create(harness, "a1")
    repo = harness.ctx.profile_analyses
    repo.claim_next("2026-11-01T12:00:00.000Z")
    item = repo.next_item(analysis["id"])
    repo.start_item(item["id"])
    harness.client.post(f"/api/profile-analyses/{analysis['id']}/cancel")
    repo.finish_cancelled(analysis["id"], "2026-11-01T12:00:01.000Z")
    assert harness.client.post(f"/api/profile-analyses/{analysis['id']}/resume").status_code == 200
    assert by_name(harness, analysis["id"])["a1"]["attempts"] == 0


def test_validated_sequence_is_not_checked_again_after_restart(harness_factory, clock):
    first = harness_factory()
    names = [f"u{i}" for i in range(CANARY_EVERY)] + ["depois"]
    analysis = create(first, *names)
    fetcher = ScriptedFetcher(clock)
    runner = install_runner(first, fetcher, clock)
    repo = first.ctx.profile_analyses
    for _ in range(CANARY_EVERY):
        item = repo.next_item(analysis["id"])
        repo.save_result(item["id"], {"account_status": "unavailable"}, "2026-11-01T12:00:00.000Z")
    runner._canary(analysis["id"], repo.trailing_unavailable(analysis["id"], 0, CANARY_EVERY))
    assert fetcher.calls == [CANARY_PROFILE]
    root = first.root
    first.__exit__(None, None, None)

    second = harness_factory(root=root)
    again = ScriptedFetcher(clock, {"instagram": [snapshot("x", "unavailable")]})
    install_runner(second, again, clock).run_next()
    assert again.calls == ["depois"]
    assert all(item["state"] == "done" for item in items_of(second, analysis["id"]))


def test_unavailable_does_not_reset_the_breaker(harness, clock):
    names = ["p1", "p2", "p3", "p4", "u5", "p6", "p7"]
    refused = {name: [web_error("access_refused")] for name in names[:4] + ["p6"]}
    fetcher = ScriptedFetcher(clock, {**refused, "u5": [snapshot("u5", "unavailable")]})
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, *names)
    seen = []

    def note(_sim):
        row = harness.ctx.profile_analyses.get(analysis["id"])
        if row["status"] == "waiting" and not seen:
            seen.append((row["wait_reason"], list(fetcher.calls)))

    clock.on_wait = note
    runner.run_next()
    assert seen == [("too_many_failures", names[:6])]


def test_unavailable_does_not_reset_the_backoff_step(harness, clock):
    limited = web_error("rate_limited")
    fetcher = ScriptedFetcher(clock, {"a1": [limited, snapshot("a1", "unavailable")], "a2": [limited, snapshot("a2")]})
    runner = install_runner(harness, fetcher, clock)
    analysis = create(harness, "a1", "a2")
    levels = {}

    def note(_sim):
        row = harness.ctx.profile_analyses.get(analysis["id"])
        if row["status"] == "waiting":
            levels[row["waiting_until"]] = row["wait_level"]

    clock.on_wait = note
    runner.run_next()
    assert list(levels.values()) == [1, 2]
    assert harness.ctx.profile_analyses.get(analysis["id"])["wait_level"] == 0   # só `active` zerou
