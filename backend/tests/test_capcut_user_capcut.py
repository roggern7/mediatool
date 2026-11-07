"""Uso simultâneo: o dono tenta abrir o CapCut enquanto a automação exporta no modo isolado.

Verificado no real (2026-11-07, instância de teste): o CapCut só abre uma vez; o CapCut aberto pelo dono durante a
exportação isolada fecha na hora (o pedido vai para a instância oculta da automação) e nada aparece para ele. A sessão
isolada passa a detectar esse caso e o runner pausa a fila ao fim do vídeo atual, com o motivo, para liberar o CapCut.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from capcut_jobs_fakes import FakeCapCutError, FakeCapCutProcessor, add_media

from app.integrations.capcut import install as install_mod
from app.integrations.capcut import isolated
from app.integrations.capcut.jobspec import USER_CAPCUT_NOTICE
from app.jobs.capcut_runner import CapCutRunner
from app.services import capcut_service

P = install_mod.ProcessInfo


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def setup(harness, tmp_path):
    processor = FakeCapCutProcessor()
    harness.ctx.capcut_processor = processor
    runner = CapCutRunner(harness.ctx, clock=Clock())
    harness.ctx.capcut_runner = runner
    out = tmp_path / "saida"

    def make_job():
        media_id = add_media(harness, name=f"v{time.monotonic_ns()}")
        r = harness.client.post("/api/capcut/jobs", json={"media_ids": [media_id], "overrides": {"output_dir": str(out)}})
        assert r.status_code == 202, r.text
        return r.json()["created"][0]["id"]

    return harness, processor, runner, make_job


def test_watcher_flags_capcut_opened_outside_our_tree():
    """Proxy da sessão isolada: um CapCut.exe que não é da nossa árvore e foi criado depois do launch = dono."""
    table = [P(10, 4, "explorer.exe", 1), P(100, 4, "CapCut.exe", 1000), P(110, 100, "CapCut.exe", 1001)]
    controller = SimpleNamespace(_tree={100: 1000})
    proxy = isolated.IsolatedControllerProxy(desktop=None, worker=None, controller=controller,
                                             processes=lambda: list(table), poll=0.01)
    t = threading.Thread(target=proxy._watch, args=(1000,), daemon=True)
    t.start()
    time.sleep(0.1)
    assert proxy.user_capcut_attempt is False, "filhos da nossa árvore não contam"
    table.append(P(500, 10, "CapCut.exe", 2000))  # o dono abre o CapCut pelo Explorer
    for _ in range(100):
        if proxy.user_capcut_attempt:
            break
        time.sleep(0.01)
    proxy._watch_stop.set()
    t.join(1)
    assert proxy.user_capcut_attempt is True


def test_watcher_ignores_capcut_older_than_our_launch():
    table = [P(100, 4, "CapCut.exe", 1000), P(700, 4, "CapCut.exe", 500)]  # já existia antes (não é tentativa nova)
    proxy = isolated.IsolatedControllerProxy(desktop=None, worker=None, controller=SimpleNamespace(_tree={100: 1000}),
                                             processes=lambda: list(table), poll=0.01)
    t = threading.Thread(target=proxy._watch, args=(1000,), daemon=True)
    t.start()
    time.sleep(0.1)
    proxy._watch_stop.set()
    t.join(1)
    assert proxy.user_capcut_attempt is False


def test_runner_pauses_queue_with_reason_after_job_where_user_opened_capcut(setup):
    harness, processor, runner, make_job = setup
    first, second = make_job(), make_job()
    original = processor.__call__

    def with_notice(spec, **kw):
        result = original(spec, **kw)
        result.warnings = [*result.warnings, USER_CAPCUT_NOTICE]
        return result

    harness.ctx.capcut_processor = with_notice
    assert runner.run_next() is True
    repo = capcut_service.repo(harness.ctx)
    assert repo.get_job(first)["status"] == "completed", "o vídeo em andamento termina normalmente"
    status = harness.client.get("/api/capcut/status").json()["queue"]
    assert status["paused"] is True and status["pause_reason"] == USER_CAPCUT_NOTICE
    assert runner.run_next() is False, "fila pausada: o próximo não reabre o CapCut oculto"
    assert repo.get_job(second)["status"] == "pending"
    harness.client.post("/api/capcut/queue/resume")
    status = harness.client.get("/api/capcut/status").json()["queue"]
    assert status["paused"] is False and status["pause_reason"] is None
    harness.ctx.capcut_processor = processor
    assert runner.run_next() is True and repo.get_job(second)["status"] == "completed"


def test_runner_pauses_also_when_the_job_fails_after_user_opened_capcut(setup):
    harness, processor, runner, make_job = setup
    make_job()
    err = FakeCapCutError("ui_timeout", "O CapCut não respondeu.", transient=True)
    err.user_capcut_attempt = True
    processor.errors = [err]
    runner.run_next()
    assert harness.client.get("/api/capcut/status").json()["queue"]["paused"] is True
