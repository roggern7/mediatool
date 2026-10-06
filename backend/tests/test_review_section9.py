"""Revisão adversarial independente da seção 9 de docs/BATCH_ROBUSTNESS.md (10.000 mídias). 100% offline, valores FAKE.

Nenhuma requisição ao Instagram (cliente httpx com MockTransport; o app usa `trust_env=False`), nada de
C:\\media-tool\\data\\secrets (sessão DPAPI num diretório temporário), nenhuma porta 8765 aberta (TestClient).
Relógio simulado: `Sim` desloca o `utc_now` dos módulos do app, o relógio das pausas do worker e o relógio do portão,
de modo que "horas" de indisponibilidade rodam em segundos reais.

Cenários: (1) 5xx/timeout/rede por horas; (2) access_refused; (3) cursor antigo recusado; (4) renovação recusada +
disjuntor; (5) janela de prova, ritmo lento e teto de páginas/hora; (6) disco cheio; (7) dois pools; (8) rodízio,
409 e backpressure global; (9) reinício abrupto; (10) request_auth_failed isolado; (11) processar por filtro;
(12) nenhum segredo em logs/APIs (cada teste termina com `scan_secrets`).
"""

from __future__ import annotations

import errno
import hashlib
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from media_processor import ProcessingCancelledError

import app.db.repositories.batches as batches_repo
import app.db.repositories.jobs as jobs_repo
import app.jobs.import_runner as runner_mod
import app.jobs.pipeline as pipeline_mod
import app.jobs.worker as worker_mod
import app.services.presenters as presenters_mod
from app.db import connection as conn_mod
from app.db.connection import parse_utc
from app.integrations.instagram.downloader import InstagramError, SessionTestResult
from app.integrations.instagram.web.graphql import InstagramWebGraphQL
from app.integrations.instagram.web.provider import InstagramWebDownloader
from app.jobs.import_runner import MAX_PENDING_INGEST_GLOBAL
from app.services.instagram_gate import InstagramRequestGate
from app.services.instagram_recovery import AUTH_FAIL_STREAK_KEY
from conftest import wait_until
from media_fakes import FakeInstagramDownloader, FakeProcessor, remote
from test_auth_escalation import (
    LazyPagedDownloader,
    assert_session_intact,
    batch_row,
    internal_batch,
    session_fixture,
)
from test_review_batch_robustness import FakeCdn, Feed, SessionWithCookie

REAL_UTC_NOW = conn_mod.utc_now
SECRET_MARKERS = ("FAKE-CURSOR", "FAKE_CURSOR", "FAKE_SIGNED", "cdninstagram", "scontent", "FAKE-SESSION",
                  "FAKE_SESSION", "FAKECSRF", "sessionid=", "oh=", "oe=")
FAKE_SESSION_ID = "FAKE-SESSION-BR-0001"
OK_DISK = (True, 10**12, 1)


# ============================================================================================ infraestrutura


class Sim:
    """Relógio simulado: tudo que o app lê como "agora" soma `offset` segundos."""

    def __init__(self) -> None:
        self.offset = 0.0
        self.sleeps: list[float] = []

    def utc_now(self, offset_seconds: float = 0.0) -> str:
        return REAL_UTC_NOW(offset_seconds + self.offset)

    def now(self) -> datetime:
        return datetime.now(UTC) + timedelta(seconds=self.offset)

    def mono(self) -> float:
        return 1_000_000.0 + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.offset += seconds

    def attach(self, h) -> None:
        h.ctx.worker.clock = self.now
        h.ctx.worker.disk_check = lambda: OK_DISK


@pytest.fixture
def sim(monkeypatch):
    s = Sim()
    for module in (runner_mod, pipeline_mod, jobs_repo, batches_repo, worker_mod, presenters_mod):
        monkeypatch.setattr(module, "utc_now", s.utc_now)
    return s


@pytest.fixture(autouse=True)
def _debug_logs(caplog):
    caplog.set_level(logging.DEBUG)


def scan_secrets(h, caplog, *, extra_texts: dict[str, str] | None = None) -> None:
    """(12) Nada de cursor, URL de mídia/assinada ou valor de sessão em logs, arquivos de log, APIs e colunas de erro."""
    texts: dict[str, str] = {"caplog": caplog.text, **(extra_texts or {})}
    if h.settings.log_dir.exists():
        for path in h.settings.log_dir.glob("*"):
            if path.is_file():
                texts[f"logfile:{path.name}"] = path.read_text(encoding="utf-8", errors="replace")
    ids = [row[0] for row in h.ctx.db.fetchall("SELECT id FROM batches")]
    paths = ["/api/batches?limit=200", "/api/jobs?limit=200", "/api/settings", "/api/settings/diagnostics",
             "/api/health", "/api/media?limit=100"]
    paths += [f"/api/batches/{i}?include_jobs=true" for i in ids]
    for path in paths:
        response = h.client.get(path)
        assert response.status_code == 200, (path, response.status_code, response.text[:200])
        texts[f"api:{path}"] = response.text
    texts["db:errors"] = " | ".join(
        str(r[0]) for r in h.ctx.db.fetchall(
            "SELECT error_message FROM jobs WHERE error_message IS NOT NULL UNION ALL "
            "SELECT error_message FROM batches WHERE error_message IS NOT NULL"))
    assert sum(len(t) for t in texts.values()) > 0
    for name, text in texts.items():
        for marker in SECRET_MARKERS:
            assert marker not in text, f"segredo/cursor/URL ({marker!r}) vazou em {name}"


def lrow(h, batch_id: int) -> dict:
    return dict(h.ctx.db.fetchone("SELECT * FROM batches WHERE id = ?", (batch_id,)))


def job_keys(h, batch_id: int) -> list[str]:
    return [j["source_post_id"] for j in h.ctx.jobs.for_batch(batch_id)]


def status_counts(h, batch_id: int | None = None) -> dict[str, int]:
    where, params = ("WHERE batch_id = ?", (batch_id,)) if batch_id is not None else ("", ())
    return {r[0]: r[1] for r in h.ctx.db.fetchall(f"SELECT status, COUNT(*) FROM jobs {where} GROUP BY status", params)}


def drive(h, batch_id: int, rounds: int = 600, *, per_round: int = 200) -> dict:
    for _ in range(rounds):
        h.run_imports()
        h.ctx.worker.run_pending(max_jobs=per_round)
        if h.ctx.batches.get(batch_id)["status"] in ("completed", "failed", "cancelled"):
            break
    return lrow(h, batch_id)


def internal_h(harness_factory, tmp_path, dl, sim, **kw):
    provider, digest = session_fixture(tmp_path)
    h = harness_factory(downloader=dl, session_provider=provider, **kw)
    sim.attach(h)
    return h, provider, digest


class V:
    """Verificação mínima falsa do controlador (uma por vencimento)."""

    def __init__(self, *results) -> None:
        self.results, self.calls = list(results), 0

    def __call__(self):
        self.calls += 1
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


def ok_result() -> SessionTestResult:
    return SessionTestResult(ok=True, code="ok", message="Sessão válida.", username="conta")


def fail_result(code: str) -> SessionTestResult:
    return SessionTestResult(ok=False, code=code, message="Recusado.", username=None)


def remaining(h, sim: Sim) -> float:
    state = h.ctx.worker.recovery.state()
    assert state is not None
    return (parse_utc(state.next_attempt_at) - sim.now()).total_seconds()


def expire(h, sim: Sim) -> None:
    sim.advance(remaining(h, sim) + 1)


class Outage:
    """Falhas controladas do servidor falso (POSTs de página): `ok_left` páginas boas antes de falhar."""

    def __init__(self) -> None:
        self.mode = "ok"            # ok | 503 | timeout | network | 429
        self.ok_left = 0
        self.ok_afters: list[str | None] = []
        self.stamps: list[float] = []   # relógio simulado de cada POST
        self.sim: Sim | None = None

    def override(self, index, variables, request):
        if self.sim is not None:
            self.stamps.append(self.sim.mono())
        if self.mode != "ok":
            if self.ok_left > 0:
                self.ok_left -= 1
            else:
                if self.mode == "503":
                    return httpx.Response(503, text="<html>Service Unavailable FAKE</html>")
                if self.mode == "429":
                    return httpx.Response(429, json={"message": "Please wait a few minutes", "status": "fail"})
                if self.mode == "timeout":
                    raise httpx.ReadTimeout("fake timeout")
                raise httpx.ConnectError("fake network down")
        self.ok_afters.append(variables.get("after"))
        return None


def web_harness(harness_factory, feed: Feed, cdn: FakeCdn, sim: Sim, *, gate=None, env=None, root=None, **kw):
    """Harness com o `InstagramWebDownloader` real sobre MockTransport, portão e esperas no relógio simulado."""
    provider = SessionWithCookie()
    h = harness_factory(root=root, session_provider=provider, processor=FakeProcessor(), **kw)
    sim.attach(h)
    gate = gate or InstagramRequestGate(interval=(0.0, 0.0), clock=sim.mono, sleep=sim.sleep)
    h.ctx.instagram_gate = gate
    holder: dict = {}

    def graphql(http, bootstrap):
        delay = holder["dl"]._listing_delay((0.0, 0.0)) or (0.0, 0.0)
        return InstagramWebGraphQL(http, bootstrap, overrides={}, env=env, page_delay=delay, sleep=sim.sleep)

    dl = InstagramWebDownloader(provider, cdn_downloader=cdn, transport=feed.transport(), page_delay=(0.0, 0.0),
                                request_gate=gate, env=env, graphql_factory=graphql)
    holder["dl"] = dl
    h.downloader = dl
    return h


def internal_web_batch(h, limit: int, profile: str = "perfil_teste") -> int:
    batch_id = h.ctx.batches.create(source="instagram_import", status="listing", source_profile=profile,
                                    content_type="all", requested_limit=limit)
    h.ctx.wake_import_runner()
    return batch_id


class ProfileFeed(LazyPagedDownloader):
    """Um perfil falso por nome de usuário (códigos distintos por perfil)."""

    def iter_profile_media(self, username, content, limit, **kw):
        self.prefix = username
        return super().iter_profile_media(username, content, limit, **kw)


# ============================================================================================ (1) 5xx/rede por horas


def test_01_hours_of_5xx_timeout_network_never_complete_partial_backoff_persisted_then_finish_2000(
        harness_factory, sim, caplog):
    n = 2000
    feed, cdn, outage = Feed(["img"] * n), FakeCdn(), Outage()
    outage.sim, outage.ok_left, outage.mode = sim, 5, "503"
    feed.override = outage.override
    h = web_harness(harness_factory, feed, cdn, sim)
    root = h.root
    batch_id = internal_web_batch(h, n)

    modes = ["timeout", "network", "503"]
    delays: list[int] = []
    started = sim.offset
    for rnd in range(10):
        h.run_imports()
        h.run_jobs()
        row = lrow(h, batch_id)
        assert row["status"] == "listing" and row["listing_state"] == "paused", (rnd, row)
        assert row["error_code"] in ("network_error", "timeout"), row
        assert row["found"] < n and h.batch(batch_id)["status"] == "listing"
        delay = (parse_utc(row["listing_retry_at"]) - sim.now()).total_seconds()
        delays.append(round(delay))
        before = (len(feed.posts_vars), feed.gets)
        sim.advance(delay / 2)
        h.run_imports()
        assert (len(feed.posts_vars), feed.gets) == before, "dentro do backoff nada pode ir ao Instagram"
        sim.advance(delay / 2 + 1)
        outage.mode = modes[rnd % 3]
        if rnd == 4:   # reinício do app no meio da indisponibilidade: o backoff está no banco
            persisted = lrow(h, batch_id)
            h.__exit__(None, None, None)
            h = web_harness(harness_factory, feed, cdn, sim, root=root)
            again = lrow(h, batch_id)
            assert again["listing_retry_at"] == persisted["listing_retry_at"]
            assert again["listing_retry_level"] == persisted["listing_retry_level"]
            assert again["listing_next_page"] == persisted["listing_next_page"]
    assert all(abs(d - e) <= 3 for d, e in zip(delays, [60, 300, 900, 3600, 3600, 3600, 3600, 3600, 3600, 3600],
                                                strict=True)), delays
    assert sim.offset - started > 6 * 3600, "o teste simulou horas de indisponibilidade"

    outage.mode = "ok"
    sim.advance(3700)
    final = drive(h, batch_id)
    assert final["status"] == "completed", final
    keys = job_keys(h, batch_id)
    assert len(keys) == len(set(keys)) == n
    assert h.batch(batch_id)["found"] == n and final["error_code"] is None and final["listing_cursor"] is None
    assert all(cdn.count(f"CODE{i:05d}") == 1 for i in range(n)), "nada baixado duas vezes"
    assert len(outage.ok_afters) == len(set(outage.ok_afters)), "nenhuma página boa pedida duas vezes"
    assert feed.unexpected == []
    scan_secrets(h, caplog)


def test_01b_bootstrap_failures_and_first_page_failures_pause_instead_of_failing(harness_factory, sim, caplog):
    feed, cdn = Feed(["img"] * 60), FakeCdn()
    state = {"down": True}
    original = feed.handle

    def handle(request):
        if state["down"] and request.method == "GET":
            return httpx.Response(502, text="bad gateway")
        return original(request)

    feed.transport = lambda: httpx.MockTransport(handle)
    h = web_harness(harness_factory, feed, cdn, sim)
    batch_id = internal_web_batch(h, 60)
    for _ in range(4):
        h.run_imports()
        row = lrow(h, batch_id)
        assert row["status"] == "listing" and row["listing_state"] == "paused", row
        sim.advance(3601)
    state["down"] = False
    final = drive(h, batch_id)
    assert final["status"] == "completed" and h.batch(batch_id)["found"] == 60
    scan_secrets(h, caplog)


# ============================================================================================ (2) access_refused


@pytest.mark.parametrize("code", ["access_refused", "contract_changed"])
def test_02_access_refused_mid_batch_needs_attention_resume_from_checkpoint_without_duplicates(
        harness_factory, tmp_path, sim, caplog, code):
    dl = ProfileFeed(2000, page_size=50, fail_pages={6: 1}, fail_code=code)
    h, provider, digest = internal_h(harness_factory, tmp_path, dl, sim)
    batch_id = internal_batch(h, 2000)
    other = h.ctx.batches.create(source="instagram_import", status="listing", source_profile="outro_perfil",
                                 content_type="all", requested_limit=100)
    for _ in range(60):
        h.run_imports()
        h.ctx.worker.run_pending(max_jobs=200)
        if lrow(h, batch_id)["listing_state"] == "needs_attention":
            break
    row = lrow(h, batch_id)
    assert row["status"] == "listing" and row["listing_state"] == "needs_attention", row
    assert row["error_code"] == code and row["error_message"]
    assert row["listing_next_page"] == 6 and row["listing_cursor"] == "FAKE-CURSOR-6", "checkpoint mantido"
    assert row["listing_attention_tries"] == 1 and row["listing_retry_at"] is not None  # nova tentativa em 6 h
    frozen = list(dl.page_requests)
    for _ in range(5):   # horas depois (antes da nova tentativa automática de 6 h): só a ação do usuário retoma
        sim.advance(3600)
        h.run_imports()
        h.ctx.worker.run_pending(max_jobs=200)
    other_pages = [p for p in dl.page_requests[len(frozen):]]
    assert lrow(h, batch_id)["listing_state"] == "needs_attention" and lrow(h, batch_id)["status"] == "listing"
    assert lrow(h, other)["status"] in ("running", "completed"), "outro lote não fica preso atrás do needs_attention"
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM jobs WHERE batch_id = ?", (batch_id,)) == 250
    # os requests extras vieram só do outro lote (que recomeça na página 1)
    assert dl.page_requests.count(6) == 1 and max(other_pages or [0]) <= 2
    api = h.batch(batch_id)
    assert api["listing_state"] == "needs_attention" and api["error_code"] == code

    resumed = h.client.post(f"/api/batches/{batch_id}/resume-listing")
    assert resumed.status_code == 200 and resumed.json()["listing_state"] == "pending"
    final = drive(h, batch_id)
    assert final["status"] == "completed", final
    keys = job_keys(h, batch_id)
    assert len(keys) == len(set(keys)) == 2000
    mine = [p for p in dl.page_requests if p >= 3]
    assert mine.count(6) == 2 and all(mine.count(p) >= 1 for p in range(3, 41))
    assert lrow(h, batch_id)["listing_next_page"] is None or lrow(h, batch_id)["listing_state"] == "done"
    assert_session_intact(provider, digest)
    scan_secrets(h, caplog)


# ============================================================================================ (3) cursor antigo


class StaleCursorDownloader(LazyPagedDownloader):
    refuse = False
    refuse_code = "contract_changed"

    def iter_profile_media(self, username, content, limit, *, cancel=None, resume=None, on_checkpoint=None):
        if self.refuse and resume is not None and resume.resume_cursor is not None:
            self.page_requests.append(resume.next_page_index)
            raise InstagramError(self.refuse_code)
        yield from super().iter_profile_media(username, content, limit, cancel=cancel, resume=resume,
                                              on_checkpoint=on_checkpoint)


@pytest.mark.parametrize("code", ["access_refused", "contract_changed", "content_unavailable"])
def test_03_old_cursor_refused_retries_same_cursor_then_needs_attention_never_restarts_even_across_app_restart(
        harness_factory, tmp_path, sim, caplog, code):
    # Política final (§9): nenhum recomeço automático da página 1; cursor recusado -> mesma tentativa depois do
    # backoff -> needs_attention com o checkpoint; recomeço só por restart-listing (com confirmação).
    dl = StaleCursorDownloader(600, page_size=50)
    dl.refuse_code = code
    h, provider, digest = internal_h(harness_factory, tmp_path, dl, sim)
    batch_id = internal_batch(h, 600)
    h.run_imports()
    saved = lrow(h, batch_id)
    assert saved["listing_cursor"] is not None
    h.ctx.worker.run_pending(max_jobs=200)
    dl.refuse = True
    h.run_imports()          # 1a recusa -> pausa com o backoff do lote, mesmo cursor
    row = lrow(h, batch_id)
    assert row["listing_state"] == "paused" and row["listing_cursor_refusals"] == 1, row
    assert (row["listing_cursor"], row["listing_next_page"]) == (saved["listing_cursor"], saved["listing_next_page"])
    assert dl.page_requests.count(1) == 1, "sem recomeço automático da página 1"
    h.run_imports()
    assert lrow(h, batch_id)["listing_state"] == "paused", "dentro do backoff não tenta de novo"

    root = h.root
    h.__exit__(None, None, None)
    h = harness_factory(root=root, downloader=dl, session_provider=provider)
    sim.attach(h)
    assert lrow(h, batch_id)["listing_cursor_refusals"] == 1, "a recusa é persistida"
    sim.advance(61)
    h.run_imports()          # 2a recusa do MESMO cursor -> needs_attention com o checkpoint
    row = lrow(h, batch_id)
    assert row["listing_state"] == "needs_attention" and row["error_code"] == code, row
    assert (row["listing_cursor"], row["listing_next_page"]) == (saved["listing_cursor"], saved["listing_next_page"])
    assert dl.page_requests.count(1) == 1, "nunca recomeça sozinho"
    for _ in range(2):
        sim.advance(7200)
        h.run_imports()
    assert dl.page_requests.count(1) == 1 and lrow(h, batch_id)["listing_state"] == "needs_attention"

    warn = h.client.post(f"/api/batches/{batch_id}/restart-listing")
    assert warn.status_code == 409 and warn.json()["code"] == "restart_confirmation_required"
    dl.refuse = False
    restarted = h.client.post(f"/api/batches/{batch_id}/restart-listing", json={"confirm": True})
    assert restarted.status_code == 200 and restarted.json()["listing_state"] == "pending"
    final = drive(h, batch_id)
    assert final["status"] == "completed"
    assert dl.page_requests.count(1) == 2, "recomeço da página 1 só pela ação do usuário"
    assert len(job_keys(h, batch_id)) == len(set(job_keys(h, batch_id))) == 600
    assert len(dl.downloaded) == len(set(dl.downloaded)) == 600
    scan_secrets(h, caplog)


@pytest.mark.parametrize("code", ["access_refused", "contract_changed", "content_unavailable"])
def test_03b_persistently_refused_cursor_ends_in_needs_attention_without_loss_or_duplicates(
        harness_factory, tmp_path, sim, caplog, code):
    """Invariantes neutras quanto à política (recomeço automático x nova tentativa): nunca falha/conclui parcial, nunca
    duplica jobs, chega a needs_attention com motivo seguro, e a retomada manual conclui os 600 itens."""
    dl = StaleCursorDownloader(600, page_size=50)
    dl.refuse_code = code
    h, provider, digest = internal_h(harness_factory, tmp_path, dl, sim)
    batch_id = internal_batch(h, 600)
    h.run_imports()
    h.ctx.worker.run_pending(max_jobs=200)
    dl.refuse = True
    for _ in range(12):
        h.run_imports()
        h.ctx.worker.run_pending(max_jobs=200)
        row = lrow(h, batch_id)
        assert row["status"] == "listing", row
        if row["listing_state"] == "needs_attention":
            break
        sim.advance(3601)
    row = lrow(h, batch_id)
    assert row["listing_state"] == "needs_attention" and row["error_code"] == code and row["error_message"], row
    keys = job_keys(h, batch_id)
    assert len(keys) == len(set(keys))
    api = h.batch(batch_id)
    assert api["listing_state"] == "needs_attention" and "FAKE-CURSOR" not in str(api)
    dl.refuse = False
    assert h.client.post(f"/api/batches/{batch_id}/resume-listing").status_code == 200
    final = drive(h, batch_id)
    assert final["status"] == "completed"
    keys = job_keys(h, batch_id)
    assert len(keys) == len(set(keys)) == 600 and len(dl.downloaded) == len(set(dl.downloaded)) == 600
    scan_secrets(h, caplog)


# ============================================================================================ (4) renovação recusada


class ExpiringDownloader(LazyPagedDownloader):
    """Toda URL listada está expirada; `refresh_page` falha com `refresh_code` até `refresh_ok` ficar ligado."""

    def __init__(self, *a, sim: Sim, refresh_code: str, **kw) -> None:
        super().__init__(*a, **kw)
        self.sim, self.refresh_code, self.refresh_ok = sim, refresh_code, False
        self.refresh_calls: list[tuple[int, float]] = []
        self.refresh_lock = threading.Lock()

    def download(self, item, dest_dir, *, cancel=None, progress=None):
        if "FRESH" not in (item.media_url or "x"):
            raise InstagramError("media_url_expired", retryable=False)
        return super().download(item, dest_dir, cancel=cancel, progress=progress)

    def refresh_page(self, profile, ref, *, cancel=None):
        with self.refresh_lock:
            self.refresh_calls.append((ref.page_index, self.sim.offset))
        if not self.refresh_ok:
            raise InstagramError(self.refresh_code)
        return [replace(i, media_url=i.media_url + "?FRESH", listing_ref=ref) for i in self._page_items(ref.page_index)]


def assert_refresh_spacing(dl: ExpiringDownloader) -> None:
    by_page: dict[int, list[float]] = {}
    for page, at in dl.refresh_calls:
        by_page.setdefault(page, []).append(at)
    for page, stamps in by_page.items():
        for a, b in zip(stamps, stamps[1:], strict=False):
            assert b - a >= 599, f"página {page} renovada duas vezes em {b - a:.0f}s"


@pytest.mark.parametrize("refresh_code", ["access_refused", "network_error"])
def test_04_refused_refresh_300_jobs_25_pages_one_request_per_page_per_10min_breaker_pauses_and_resumes(
        harness_factory, tmp_path, sim, caplog, refresh_code):
    dl = ExpiringDownloader(300, page_size=12, sim=sim, refresh_code=refresh_code)
    h, provider, digest = internal_h(harness_factory, tmp_path, dl, sim)
    batch_id = internal_batch(h, 300)
    paused_seen = False
    for step in range(400):   # ~3,3 h simulados em passos de 30 s
        h.run_imports()
        h.ctx.worker.run_pending(max_jobs=50)
        pause = h.ctx.batches.get(batch_id)["paused_until"]
        if pause and pause > sim.utc_now():
            paused_seen = True
            ran = h.ctx.worker.run_pending(max_jobs=50)
            pages_before = len(dl.page_requests)
            h.run_imports()
            assert ran == 0 and len(dl.page_requests) == pages_before, "lote pausado: sem claims e sem listagem"
            api = h.batch(batch_id)
            assert api["paused_reason"] in ("access_refused", "network_error") and api["paused_until"]
        sim.advance(30)
        if status_counts(h, batch_id).get("failed", 0) + status_counts(h, batch_id).get("completed", 0) == 300:
            break
    assert paused_seen, "o disjuntor deveria pausar o lote"
    assert_refresh_spacing(dl)
    windows = (sim.offset // 600) + 2
    assert len(dl.refresh_calls) <= 25 * windows
    # retomada: a renovação volta a funcionar; o lote continua e conclui sem falhas novas
    dl.refresh_ok = True
    h.ctx.batches.get(batch_id)
    sim.advance(7300)
    h.client.post(f"/api/batches/{batch_id}/retry-failed")
    final = drive(h, batch_id, rounds=800)
    counts = status_counts(h, batch_id)
    assert final["status"] in ("completed", "listing", "running"), final
    if final["status"] != "completed":
        for _ in range(40):
            sim.advance(700)
            final = drive(h, batch_id, rounds=60)
            if final["status"] == "completed":
                break
    counts = status_counts(h, batch_id)
    assert final["status"] == "completed", (final, counts)
    assert counts.get("completed", 0) + counts.get("failed", 0) == 300
    assert_refresh_spacing(dl)
    assert_session_intact(provider, digest)
    scan_secrets(h, caplog)


def test_04b_sibling_jobs_of_a_failed_page_do_not_hit_the_instagram_again(harness_factory, tmp_path, sim, caplog):
    dl = ExpiringDownloader(60, page_size=12, sim=sim, refresh_code="request_auth_failed")
    h, provider, digest = internal_h(harness_factory, tmp_path, dl, sim)
    batch_id = internal_batch(h, 60)
    h.run_imports()
    ran = h.ctx.worker.run_pending(max_jobs=500)
    assert len(dl.refresh_calls) == 1, dl.refresh_calls   # request_auth_failed pausa a sessão: 1 chamada e para
    pages = {p for p, _ in dl.refresh_calls}
    assert len(pages) == 1 and ran >= 1
    assert h.ctx.worker.pause_state()[1] == "auth_check"
    assert status_counts(h, batch_id).get("failed", 0) == 0, "pausa não consome tentativa nem falha jobs"
    assert h.ctx.db.scalar("SELECT COALESCE(MAX(attempts), 0) FROM jobs WHERE batch_id = ?", (batch_id,)) == 0
    for _ in range(5):
        h.ctx.worker.run_pending(max_jobs=500)
    assert len(dl.refresh_calls) == 1, "durante a pausa nenhuma renovação"
    assert_session_intact(provider, digest)
    scan_secrets(h, caplog)


# ============================================================================================ (5) 429 / ritmo / teto


def test_05a_429_probation_window_raises_step_and_resets_outside_window(harness_factory, tmp_path, sim, caplog):
    dl = LazyPagedDownloader(300, page_size=50, fail_pages={2: 3}, fail_code="rate_limited")
    h, provider, digest = internal_h(harness_factory, tmp_path, dl, sim)
    worker = h.ctx.worker
    batch_id = internal_batch(h, 300)
    verify = V(ok_result())
    worker.recovery.verify = verify
    h.run_imports()
    assert worker.pause_state()[1] == "rate_limited" and round(remaining(h, sim)) == 900
    assert h.ctx.instagram_gate.listing_slowed(), "429 liga o ritmo lento"
    assert lrow(h, batch_id)["listing_state"] == "paused"
    seen = [round(remaining(h, sim))]
    for _ in range(2):   # 429 de novo logo depois da verificação ok: sobe um degrau
        expire(h, sim)
        assert worker.recovery.tick() == "ok"
        assert worker.pause_state() == (None, None)
        h.run_imports()
        seen.append(round(remaining(h, sim)))
    assert seen == [900, 1800, 3600], seen
    assert verify.calls == 2
    expire(h, sim)
    assert worker.recovery.tick() == "ok"
    sim.advance(2 * 3600 + 60)               # fora da janela de prova (2 x 1 h)
    dl.fail_pages[2] = 1
    h.run_imports()
    assert round(remaining(h, sim)) == 900, "fora da janela o backoff recomeça do 1o degrau"
    expire(h, sim)
    assert worker.recovery.tick() == "ok"
    final = drive(h, batch_id)
    assert final["status"] == "completed"
    assert dl.page_requests.count(1) == 1, "retomou do checkpoint, sem refazer a página 1"
    assert_session_intact(provider, digest)
    scan_secrets(h, caplog)


def test_05b_slow_pace_for_one_hour_after_429_then_normal(harness_factory, sim, caplog):
    feed, cdn, outage = Feed(["img"] * 600), FakeCdn(), Outage()
    outage.sim, outage.mode, outage.ok_left = sim, "429", 4
    feed.override = outage.override
    h = web_harness(harness_factory, feed, cdn, sim)
    worker = h.ctx.worker
    batch_id = internal_web_batch(h, 600)
    h.run_imports()
    assert sim.sleeps == [], "antes do 429 o ritmo é normal"
    t429 = sim.offset
    assert worker.pause_state()[1] == "rate_limited" and lrow(h, batch_id)["listing_state"] == "paused"
    worker.recovery.verify = V(ok_result())
    expire(h, sim)
    assert worker.recovery.tick() == "ok"
    outage.mode = "ok"
    pages_before = len(feed.posts_vars)
    h.run_imports()
    assert len(feed.posts_vars) > pages_before and sim.sleeps
    assert all(6.0 <= s <= 12.0 for s in sim.sleeps), sim.sleeps
    persisted = h.ctx.app_settings.get_state("instagram_slow_pace_until")
    assert persisted and 3500 < (parse_utc(persisted) - parse_utc(REAL_UTC_NOW(t429))).total_seconds() <= 3601
    h.run_jobs()
    sim.advance(3601 - (sim.offset - t429))   # 1 h depois do 429
    count = len(sim.sleeps)
    pages_before = len(feed.posts_vars)
    h.run_imports()
    assert len(feed.posts_vars) > pages_before, "a listagem continua"
    assert len(sim.sleeps) == count, "passada 1 h o ritmo volta ao normal"
    final = drive(h, batch_id)
    assert final["status"] == "completed" and h.batch(batch_id)["found"] == 600
    scan_secrets(h, caplog)


def test_05c_hourly_page_cap_is_never_exceeded(harness_factory, sim, caplog):
    cap = 15
    feed, cdn, outage = Feed(["img"] * 800), FakeCdn(), Outage()
    outage.sim = sim
    feed.override = outage.override
    h = web_harness(harness_factory, feed, cdn, sim, env={"MEDIA_TOOL_IG_MAX_PAGES_PER_HOUR": str(cap)})
    batch_id = internal_web_batch(h, 800)
    final = drive(h, batch_id)
    assert final["status"] == "completed" and h.batch(batch_id)["found"] == 800
    stamps = sorted(outage.stamps)
    assert len(stamps) >= 60
    for i, start in enumerate(stamps):
        in_window = [t for t in stamps[i:] if t - start < 3600]
        assert len(in_window) <= cap, f"{len(in_window)} páginas em 1 h (teto {cap})"
    assert stamps[-1] - stamps[0] >= 3 * 3600, "o teto atrasou a listagem (relógio simulado)"
    scan_secrets(h, caplog)


# ============================================================================================ (6) disco cheio


class FlakyDisk:
    def __init__(self, every: int) -> None:
        self.every, self.calls, self.forced_bad = every, 0, False

    def __call__(self):
        self.calls += 1
        bad = self.forced_bad or self.calls % self.every == 0
        return (not bad, 1 if bad else 10**12, 10**9)


def test_06_disk_full_midway_through_1000_fails_nothing_consumes_no_attempt_and_resumes(
        harness_factory, sim, caplog):
    n = 1000
    items = [remote(f"D{i:04d}") for i in range(n)]
    dl = FakeInstagramDownloader(items=items)
    # download que encontra o disco cheio no meio do job (ENOSPC), uma vez, para 1 em cada 10 itens
    for i in range(0, n, 10):
        dl.download_errors[f"D{i:04d}:0"] = [OSError(errno.ENOSPC, "No space left on device")]
    h = harness_factory(downloader=dl)
    sim.attach(h)
    disk = FlakyDisk(every=4)
    h.ctx.worker.disk_check = disk
    batch_id = h.import_profile(limit=100)["id"]       # API; o lote grande abaixo usa o caminho interno
    big = h.ctx.batches.create(source="instagram_import", status="listing", source_profile="grande_perfil",
                               content_type="all", requested_limit=n)
    rng = random.Random(9)
    for _ in range(3000):
        h.ctx.worker._disk_next_check = 0.0     # a releitura de 1 min do worker roda no relógio real
        disk.forced_bad = rng.random() < 0.45
        listed, downloaded = len(dl.page_requests), len(dl.downloads)
        h.run_imports()
        ran = h.ctx.worker.run_pending(max_jobs=7)
        if disk.forced_bad:
            assert len(dl.page_requests) == listed, "disco insuficiente: a listagem para"
            assert len(dl.downloads) == downloaded and ran == 0, "disco insuficiente: nenhum claim de ingest"
        counts = status_counts(h)
        assert counts.get("failed", 0) == 0, counts
        assert h.ctx.db.scalar("SELECT COALESCE(MAX(attempts), 0) FROM jobs") == 0, "disco não consome tentativa"
        if all(h.ctx.batches.get(b)["status"] == "completed" for b in (batch_id, big)):
            break
    assert h.ctx.batches.get(big)["status"] == "completed" and h.ctx.batches.get(batch_id)["status"] == "completed"
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM media") == n
    keys = job_keys(h, big)
    assert len(keys) == len(set(keys)) == n
    assert status_counts(h).get("completed") == n + 100   # lote da API (100) + lote grande (n); mídia só n
    settings = h.client.get("/api/settings").json()
    assert set(settings["disk"]) >= {"free_bytes", "required_bytes", "ok", "disk_full"}
    disk.forced_bad = True
    disk.every = 10**9
    assert h.client.get("/api/settings").json()["disk"]["ok"] is False
    scan_secrets(h, caplog)


def test_06b_real_disk_check_wiring_blocks_listing_and_claims_until_space_returns(harness_factory, caplog):
    from app.services import disk as disk_mod

    dl = FakeInstagramDownloader(items=[remote(f"W{i:03d}") for i in range(40)])
    h = harness_factory(downloader=dl)           # disk_check real (services/disk.disk_ok)
    batch_id = h.import_profile(limit=100)["id"]
    try:
        disk_mod.configure(h.ctx.storage.root, 10**18)    # exige um exabyte livre
        h.ctx.worker._disk_next_check = 0.0
        h.run_imports()
        assert dl.listings == 0 and h.ctx.jobs.count_for_batch(batch_id) == 0, "listagem para sem espaço"
        assert h.ctx.worker.run_pending(max_jobs=10) == 0
        state = h.client.get("/api/settings").json()["disk"]
        assert state["ok"] is False and state["disk_full"] is True and state["required_bytes"] == 10**18
        assert state["free_bytes"] > 0
    finally:
        h.ctx.worker._configure_disk()
    h.ctx.worker._disk_next_check = 0.0
    h.run_all()
    assert h.ctx.batches.get(batch_id)["status"] == "completed" and h.batch(batch_id)["found"] == 40
    assert h.ctx.db.scalar("SELECT COALESCE(MAX(attempts), 0) FROM jobs") == 0
    scan_secrets(h, caplog)


# ============================================================================================ (7) dois pools


@dataclass
class TrackingDownloader(FakeInstagramDownloader):
    delay: float = 0.004

    def __post_init__(self) -> None:
        self._tlock = threading.Lock()
        self.cur = 0
        self.max = 0

    def download(self, item, dest_dir, *, cancel=None, progress=None):
        with self._tlock:
            self.cur += 1
            self.max = max(self.max, self.cur)
        try:
            time.sleep(self.delay)
            return super().download(item, dest_dir, cancel=cancel, progress=progress)
        finally:
            with self._tlock:
                self.cur -= 1


class TrackingProcessor(FakeProcessor):
    def __init__(self, hold: threading.Event, delay: float = 0.01) -> None:
        super().__init__()
        self.hold, self.delay = hold, delay
        self._plock = threading.Lock()
        self.active: set[str] = set()
        self.cur = self.max = 0
        self.same_media: list[str] = []

    def process(self, src, dst, opts, *, progress=None, cancel=None, timeout=None):
        key = str(src)
        with self._plock:
            if key in self.active:
                self.same_media.append(key)
            self.active.add(key)
            self.cur += 1
            self.max = max(self.max, self.cur)
        try:
            while not self.hold.wait(0.01):
                if cancel is not None and cancel.is_set():
                    raise ProcessingCancelledError("cancelado")
            time.sleep(self.delay)
            return super().process(src, dst, opts, progress=progress, cancel=cancel, timeout=timeout)
        finally:
            with self._plock:
                self.active.discard(key)
                self.cur -= 1


@pytest.mark.parametrize("repeat", range(4))
def test_07_downloads_do_not_wait_for_slow_ffmpeg_and_never_two_ffmpeg_on_the_same_job(
        harness_factory, monkeypatch, caplog, repeat):
    n = 60
    hold = threading.Event()
    dl = TrackingDownloader(items=[remote(f"F{i:03d}") for i in range(n)])
    proc = TrackingProcessor(hold)
    running: set[int] = set()
    duplicates: list[int] = []
    lock = threading.Lock()

    class Tracked(worker_mod.JobExecutor):
        def run(self):
            with lock:
                if self.job_id in running:
                    duplicates.append(self.job_id)
                running.add(self.job_id)
            try:
                super().run()
            finally:
                with lock:
                    running.discard(self.job_id)

    monkeypatch.setattr(worker_mod, "JobExecutor", Tracked)
    h = harness_factory(downloader=dl, processor=proc, start_worker=True, concurrency=2, download_concurrency=4,
                        poll_interval=0.01)
    h.ctx.worker.disk_check = lambda: OK_DISK
    assert h.ctx.worker.concurrency == 2 and h.ctx.worker.download_concurrency == 4
    batch_id = h.import_profile(limit=100, process=True)["id"]

    peaks = {"processing": 0, "downloading": 0}
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            counts = status_counts(h)
            peaks["processing"] = max(peaks["processing"], counts.get("processing", 0))
            peaks["downloading"] = max(peaks["downloading"], counts.get("downloading", 0))
            time.sleep(0.002)

    thread = threading.Thread(target=sampler, daemon=True)
    thread.start()
    try:
        # FFmpeg preso: os downloads seguem sem esperar o FFmpeg até o teto do backlog de processamento do lote
        # (seção 9: `max_process_backlog`, 50); os outros 10 aguardam o FFmpeg andar.
        cap = h.settings.max_process_backlog
        assert wait_until(lambda: (lambda c: c.get("downloaded", 0) + c.get("processing", 0) == cap)(status_counts(h)),
                          timeout=40), status_counts(h)
        assert 1 <= proc.cur <= 2 and len(dl.downloads) == cap
        assert status_counts(h).get("pending", 0) == n - cap
        assert h.ctx.batches.get(batch_id)["status"] == "running"
        hold.set()
        assert wait_until(lambda: h.ctx.batches.get(batch_id)["status"] == "completed", timeout=60), status_counts(h)
    finally:
        hold.set()
        stop.set()
        thread.join(2)
    assert proc.max <= 2 and peaks["processing"] <= 2 and peaks["downloading"] <= 4 and dl.max <= 4
    assert proc.same_media == [] and duplicates == [], (proc.same_media, duplicates)
    counts = status_counts(h)
    assert counts == {"completed": n}, counts
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM renditions") == n
    assert h.ctx.db.scalar("SELECT COALESCE(MAX(attempts), 0) FROM jobs") == 0
    assert len(dl.downloads) == len(set(dl.downloads)) == n
    scan_secrets(h, caplog)


@pytest.mark.parametrize("repeat", range(3))
def test_07b_fast_ffmpeg_pool_race_never_double_claims_a_downloaded_job(harness_factory, monkeypatch, caplog, repeat):
    """FFmpeg rápido: o pool de processamento reivindica o job `downloaded` assim que o download libera o lease."""
    n = 150
    hold = threading.Event()
    hold.set()
    dl = TrackingDownloader(items=[remote(f"Q{i:03d}") for i in range(n)], delay=0.0)
    proc = TrackingProcessor(hold, delay=0.0)
    running: set[int] = set()
    duplicates: list[int] = []
    lock = threading.Lock()

    class Tracked(worker_mod.JobExecutor):
        def run(self):
            with lock:
                if self.job_id in running:
                    duplicates.append(self.job_id)
                running.add(self.job_id)
            try:
                super().run()
            finally:
                with lock:
                    running.discard(self.job_id)

    monkeypatch.setattr(worker_mod, "JobExecutor", Tracked)
    h = harness_factory(downloader=dl, processor=proc, start_worker=True, concurrency=2, download_concurrency=4,
                        poll_interval=0.005)
    h.ctx.worker.disk_check = lambda: OK_DISK
    batch_id = h.import_profile(limit=100, process=True)["id"]   # 100 pelo API
    big = h.ctx.batches.create(source="instagram_import", status="listing", source_profile="grande_perfil",
                               content_type="all", requested_limit=n, preset_id=1, preset_name="p",
                               params=h.ctx.presets.get(1)["params"], process=True)
    h.ctx.wake_import_runner()
    assert wait_until(lambda: all(h.ctx.batches.get(i)["status"] == "completed" for i in (batch_id, big)), timeout=90),         status_counts(h)
    assert duplicates == [] and proc.same_media == [] and proc.max <= 2 and dl.max <= 4
    counts = status_counts(h)
    assert set(counts) == {"completed"} and h.ctx.db.scalar("SELECT COALESCE(MAX(attempts), 0) FROM jobs") == 0
    assert len(proc.processed) == len({str(x) for x in proc.processed}), "nenhum FFmpeg repetido"
    scan_secrets(h, caplog)


# ============================================================================================ (8) lotes concorrentes


def test_08a_two_concurrent_batches_are_served_round_robin(harness_factory, caplog):
    dl = ProfileFeed(100, page_size=50)
    h = harness_factory(downloader=dl)
    h.ctx.worker.disk_check = lambda: OK_DISK
    ids = [h.import_profile(profile=p, limit=100)["id"] for p in ("alfa_perfil", "beta_perfil")]
    h.run_imports()
    assert all(h.ctx.jobs.count_for_batch(i) == 100 for i in ids)
    worker = h.ctx.worker
    done_a = done_b = 0
    for step in range(1, 121):
        assert worker.run_pending(max_jobs=1) == 1
        done_a = status_counts(h, ids[0]).get("completed", 0)
        done_b = status_counts(h, ids[1]).get("completed", 0)
        if min(done_a, done_b) < 100 and step <= 160:
            assert abs(done_a - done_b) <= 1, (step, done_a, done_b)
    worker.run_pending(max_jobs=500)
    assert all(h.ctx.batches.get(i)["status"] == "completed" for i in ids)
    scan_secrets(h, caplog)


def test_08b_repeated_profile_is_409_in_every_spelling_and_under_concurrency(harness):
    h = harness
    payload = {"profile": "perfil_x", "content_type": "all", "limit": 100, "preset_id": None, "process": False}
    assert h.client.post("/api/imports", json=payload).status_code == 202
    for spelling in ("perfil_x", "PERFIL_X", "@perfil_x", "https://www.instagram.com/perfil_x/",
                     "https://instagram.com/Perfil_X"):
        r = h.client.post("/api/imports", json={**payload, "profile": spelling})
        assert r.status_code == 409 and r.json()["code"] == "profile_already_importing", (spelling, r.text)
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 1

    other = {**payload, "profile": "concorrente_y"}
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: h.client.post("/api/imports", json=other).status_code, range(8)))
    assert sorted(results) == [202] + [409] * 7, results
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM batches WHERE source_profile = 'concorrente_y'") == 1


def test_08c_global_backpressure_never_exceeds_200_with_real_threads(harness_factory, caplog):
    dl = TrackingDownloader(items=[remote(f"G{i:03d}") for i in range(100)], delay=0.002)
    # perfis diferentes precisam de itens diferentes: um downloader por perfil
    feeds = {f"perfil_{c}": [remote(f"{c.upper()}{i:03d}", profile=f"perfil_{c}") for i in range(100)] for c in "abcd"}

    class Multi(TrackingDownloader):
        def iter_profile_media(self, username, content, limit, **kw):
            self.items = feeds[username]
            return super().iter_profile_media(username, content, limit, **kw)

    dl = Multi(items=[], delay=0.002)
    h = harness_factory(downloader=dl, start_worker=True, download_concurrency=2, poll_interval=0.01)
    h.ctx.worker.disk_check = lambda: OK_DISK
    peak = {"global": 0, "per_batch": 0}
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            peak["global"] = max(peak["global"], h.ctx.batches.pending_ingest_global())
            for b in h.ctx.db.fetchall("SELECT id FROM batches"):
                peak["per_batch"] = max(peak["per_batch"], h.ctx.jobs.pending_ingest_count(b[0]))
            time.sleep(0.001)

    thread = threading.Thread(target=sampler, daemon=True)
    thread.start()
    try:
        ids = [h.import_profile(profile=f"perfil_{c}", limit=100)["id"] for c in "abcd"]
        assert wait_until(lambda: all(h.ctx.batches.get(i)["status"] == "completed" for i in ids), timeout=90)
    finally:
        stop.set()
        thread.join(2)
    assert peak["global"] <= MAX_PENDING_INGEST_GLOBAL, peak
    assert peak["per_batch"] <= 100, peak
    assert [h.batch(i)["found"] for i in ids] == [100] * 4
    scan_secrets(h, caplog)


def test_08d_concurrent_resume_listing_clicks_resume_exactly_once(harness_factory, tmp_path, sim, caplog):
    dl = LazyPagedDownloader(200, page_size=50, fail_pages={2: 1}, fail_code="access_refused")
    h, provider, digest = internal_h(harness_factory, tmp_path, dl, sim)
    batch_id = internal_batch(h, 200)
    h.run_imports()
    assert lrow(h, batch_id)["listing_state"] == "needs_attention"

    def click(_):
        return h.client.post(f"/api/batches/{batch_id}/resume-listing").status_code

    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(click, range(8)))
    assert sorted(codes) == [200] + [409] * 7, codes
    final = drive(h, batch_id)
    assert final["status"] == "completed" and len(set(job_keys(h, batch_id))) == 200
    scan_secrets(h, caplog)


# ============================================================================================ (9) reinício abrupto


def test_09_abrupt_restart_mid_download_and_mid_ffmpeg_consumes_no_attempt_and_repeats_no_work(
        harness_factory, tmp_path, caplog):
    n = 30
    provider, digest = session_fixture(tmp_path)
    dl = FakeInstagramDownloader(items=[remote(f"A{i:03d}") for i in range(n)])
    h1 = harness_factory(downloader=dl, session_provider=provider)
    h1.ctx.worker.disk_check = lambda: OK_DISK
    batch_id = h1.import_profile(limit=100, process=True)["id"]
    h1.run_imports()
    worker = h1.ctx.worker
    for _ in range(10):                                   # 10 já baixados (aguardam FFmpeg)
        job, event = worker._claim("download")
        worker._execute(job, event)
    in_ffmpeg = worker._claim("process")                   # FFmpeg "em andamento" quando o app morre
    in_download = [worker._claim("download") for _ in range(3)]   # downloads "em andamento"
    assert in_ffmpeg[0]["status"] == "processing" and all(j[0]["status"] == "downloading" for j in in_download)
    ids = [in_ffmpeg[0]["id"], *(j[0]["id"] for j in in_download)]
    h1.ctx.db.execute(f"UPDATE jobs SET attempts = max_attempts - 1 WHERE id IN ({','.join(map(str, ids))})")
    temp = h1.ctx.storage.job_temp_dir(in_ffmpeg[0]["id"])
    (temp / "processed.mp4").write_bytes(b"PARTIAL")      # saída parcial do FFmpeg morto

    root = h1.root
    current = h1
    for _ in range(1):   # reinícios abruptos seguidos (a instância anterior nem chega a encerrar; 3 seguidos no mesmo job = interrupted_repeatedly)
        current = harness_factory(root=root, downloader=dl, session_provider=provider)
        current.ctx.worker.disk_check = lambda: OK_DISK
        rows = {r["id"]: r for r in map(dict, current.ctx.db.fetchall(
            f"SELECT id, status, attempts, max_attempts, claimed_by FROM jobs WHERE id IN ({','.join(map(str, ids))})"))}
        assert rows[ids[0]]["status"] == "downloaded", rows[ids[0]]
        assert all(rows[i]["status"] == "pending" for i in ids[1:]), rows
        assert all(r["attempts"] == r["max_attempts"] - 1 and r["claimed_by"] is None for r in rows.values())
        assert status_counts(current).get("failed", 0) == 0
        # outro job é reivindicado e abandonado a cada reinício
        job, _ = current.ctx.worker._claim("download")
        ids.append(job["id"])
        current.ctx.db.execute("UPDATE jobs SET attempts = max_attempts - 1 WHERE id = ?", (job["id"],))
    current = harness_factory(root=root, downloader=dl, session_provider=provider)
    current.ctx.worker.disk_check = lambda: OK_DISK
    assert (temp / "processed.mp4").exists() is False, "temporário órfão do FFmpeg morto é limpo"
    current.run_imports()
    current.ctx.worker.run_pending(max_jobs=500)
    counts = status_counts(current)
    assert counts == {"completed": n}, counts
    assert len(dl.downloads) == len(set(dl.downloads)) == n, "nenhum download repetido (nem o já concluído)"
    assert current.ctx.db.scalar("SELECT COUNT(*) FROM renditions") == n
    assert len({str(p) for p in current.processor.processed}) == len(current.processor.processed) == n
    assert current.ctx.batches.get(batch_id)["status"] == "completed"
    assert_session_intact(provider, digest)
    scan_secrets(current, caplog)


# ============================================================================================ (10) request_auth_failed


def test_10a_isolated_request_auth_failed_pauses_session_scope_downloads_and_ffmpeg_continue(
        harness_factory, tmp_path, sim, caplog):
    dl = LazyPagedDownloader(100, page_size=50, fail_pages={2: 1})   # request_auth_failed na página 2
    h, provider, digest = internal_h(harness_factory, tmp_path, dl, sim)
    worker = h.ctx.worker
    preset = h.ctx.presets.get(1)
    batch_id = h.ctx.batches.create(source="instagram_import", status="listing", source_profile="perfil_teste",
                                    content_type="all", requested_limit=100, preset_id=preset["id"],
                                    preset_name=preset["name"], params=preset["params"], process=True)
    h.run_imports()
    until, reason = worker.pause_state()
    assert reason == "auth_check" and worker.current_pause_scope() == "session"
    assert 14 * 60 < (parse_utc(until) - sim.now()).total_seconds() <= 15 * 60
    assert lrow(h, batch_id)["listing_state"] == "paused" and h.ctx.jobs.count_for_batch(batch_id) == 50
    # 5 jobs sem URL válida (precisariam da sessão para renovar): esperam sem consumir tentativa
    stuck = [r[0] for r in h.ctx.db.fetchall("SELECT id FROM jobs WHERE batch_id = ? ORDER BY id LIMIT 5", (batch_id,))]
    h.ctx.db.execute(f"UPDATE job_remote_refs SET media_url = NULL WHERE job_id IN ({','.join(map(str, stuck))})")
    h.ctx.worker.run_pending(max_jobs=500)
    counts = status_counts(h, batch_id)
    assert counts.get("completed", 0) == 45, counts            # download (URL válida) + FFmpeg continuam
    assert counts.get("pending", 0) == 5 and counts.get("failed", 0) == 0
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM renditions") == 45
    assert h.ctx.db.scalar("SELECT COALESCE(MAX(attempts), 0) FROM jobs") == 0
    assert dl.page_requests == [1, 2], "nenhuma requisição ao Instagram durante a pausa"
    assert_session_intact(provider, digest)
    assert h.client.get("/api/settings").json()["instagram"]["session_configured"] is True
    scan_secrets(h, caplog)


def test_10b_two_consecutive_failures_without_success_session_invalid_but_session_untouched(
        harness_factory, tmp_path, sim, caplog):
    dl = LazyPagedDownloader(300, page_size=50, fail_pages={2: 1, 4: 1})
    h, provider, digest = internal_h(harness_factory, tmp_path, dl, sim)
    worker = h.ctx.worker
    batch_id = internal_batch(h, 300)
    h.run_imports()
    assert worker.pause_state()[1] == "auth_check"
    worker.recovery.verify = V(fail_result("request_auth_failed"))
    expire(h, sim)
    assert worker.recovery.tick() == "failed"                 # 2a falha seguida, nenhum sucesso entre elas
    assert worker.pause_state()[1] == "session_invalid" and round(remaining(h, sim)) == 1800
    assert worker.current_pause_scope() == "session"
    assert h.ctx.app_settings.get_state(AUTH_FAIL_STREAK_KEY) == "2"
    assert_session_intact(provider, digest)                   # nada apagado
    assert h.client.get("/api/settings").json()["instagram"]["session_configured"] is True
    worker.recovery.verify = V(ok_result())
    expire(h, sim)
    assert worker.recovery.tick() == "ok"
    assert h.ctx.app_settings.get_state(AUTH_FAIL_STREAK_KEY) is None
    final = drive(h, batch_id)                                 # página 4 falha 1x, isolada: auth_check de novo
    assert worker.pause_state()[1] == "auth_check", "falha separada por sucesso volta ao 1o degrau"
    worker.recovery.verify = V(ok_result())
    expire(h, sim)
    assert worker.recovery.tick() == "ok"
    final = drive(h, batch_id)
    assert final["status"] == "completed"
    assert_session_intact(provider, digest)
    scan_secrets(h, caplog)


# ============================================================================================ (11) processar por filtro


def test_11_process_by_filter_repeated_and_concurrent_never_creates_extra_jobs(harness, caplog):
    h = harness
    h.ctx.worker.disk_check = lambda: OK_DISK
    h.downloader.items = [remote(f"M{i:02d}") for i in range(20)]
    h.import_profile(limit=100)
    h.run_all()
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM media") == 20
    presets = [p["id"] for p in h.client.get("/api/presets").json()][:2]
    body = {"view": "all", "profile": "perfil_teste", "preset_id": presets[0]}

    def post(_):
        return h.client.post("/api/media/process-by-filter", json=body).status_code

    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(post, range(8)))
    assert sorted(codes) == [202] + [409] * 7, codes
    process_jobs = lambda: h.ctx.db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'process'")  # noqa: E731
    assert process_jobs() == 20
    assert h.ctx.db.scalar("SELECT MAX(c) FROM (SELECT COUNT(*) c FROM jobs WHERE kind='process' AND media_id IS NOT NULL"
                           " GROUP BY media_id)") == 1
    h.run_jobs()
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM renditions") == 20
    for _ in range(3):
        assert post(0) == 409
    assert process_jobs() == 20 and h.ctx.db.scalar("SELECT COUNT(*) FROM renditions") == 20
    rendition = h.ctx.db.scalar("SELECT id FROM renditions ORDER BY id LIMIT 1")
    assert h.client.delete(f"/api/renditions/{rendition}").status_code == 204
    assert post(0) == 202 and process_jobs() == 21, "só a mídia sem rendition volta a ser processada"
    assert post(0) == 409
    if len(presets) > 1:
        other = h.client.post("/api/media/process-by-filter", json={**body, "preset_id": presets[1]})
        assert other.status_code == 202
        # Regra final da seção 9: só exclui mídia com job aberto do MESMO preset/params. A mídia com job aberto do
        # 1o preset entra no lote do 2o preset: 21 + 20 = 41.
        assert process_jobs() == 41
        assert h.ctx.db.scalar(
            "SELECT MAX(c) FROM (SELECT COUNT(*) c FROM jobs WHERE kind = 'process' AND status IN"
            " ('pending', 'downloading', 'downloaded', 'processing') GROUP BY media_id, preset_id, params)") == 1
    scan_secrets(h, caplog)


# ============================================================================================ extras adversariais


def test_00_scan_secrets_has_teeth(harness, caplog):
    for marker in ("FAKE-CURSOR-9", "https://scontent.cdninstagram.com/v/x.jpg?oh=FAKE_SIGNED_1&oe=2",
                   "sessionid=FAKE-SESSION-BR-0001"):
        with pytest.raises(AssertionError, match="vazou"):
            scan_secrets(harness, caplog, extra_texts={"controle": f"linha com {marker} no meio"})


def test_02b_cancel_and_resume_edges_of_a_needs_attention_batch(harness_factory, tmp_path, sim, caplog):
    dl = LazyPagedDownloader(400, page_size=50, fail_pages={6: 1}, fail_code="access_refused")
    h, provider, digest = internal_h(harness_factory, tmp_path, dl, sim)
    batch_id = internal_batch(h, 400)
    for _ in range(20):
        h.run_imports()
        h.ctx.worker.run_pending(max_jobs=200)
        if lrow(h, batch_id)["listing_state"] == "needs_attention":
            break
    assert lrow(h, batch_id)["listing_state"] == "needs_attention"
    assert h.client.post("/api/batches/999999/resume-listing").status_code == 404
    cancelled = h.client.post(f"/api/batches/{batch_id}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    row = lrow(h, batch_id)
    assert row["status"] == "cancelled" and row["listing_cursor"] is None, "cursor apagado no cancelamento"
    assert row["listing_state"] == "cancelled"
    again = h.client.post(f"/api/batches/{batch_id}/resume-listing")
    assert again.status_code == 409 and again.json()["code"] == "listing_not_resumable"
    pages = len(dl.page_requests)
    for _ in range(3):
        sim.advance(7 * 3600)
        h.run_imports()
    assert len(dl.page_requests) == pages, "lote cancelado nunca volta a listar"
    assert status_counts(h, batch_id).get("pending", 0) == 0
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM job_remote_refs r JOIN jobs j ON j.id = r.job_id "
                           "WHERE j.batch_id = ?", (batch_id,)) == 0, "URLs/cursores apagados no cancelamento"
    scan_secrets(h, caplog)


class SelectiveFailDownloader(ProfileFeed):
    failing_prefix: str | None = "alfa"

    def download(self, item, dest_dir, *, cancel=None, progress=None):
        if self.failing_prefix and item.source_post_id.startswith(self.failing_prefix):
            raise InstagramError("download_failed", "falha simulada", retryable=False)
        return super().download(item, dest_dir, cancel=cancel, progress=progress)


def _ms(moment: datetime) -> datetime:
    """Trunca em milissegundos, como utc_now grava."""
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


def test_04c_twenty_failing_jobs_pause_only_that_batch_with_escalating_steps_and_success_resets(
        harness_factory, tmp_path, sim, caplog):
    dl = SelectiveFailDownloader(100, page_size=50)
    h, provider, digest = internal_h(harness_factory, tmp_path, dl, sim)
    a = h.import_profile(profile="alfa_perfil", limit=100)["id"]
    b = h.import_profile(profile="beta_perfil", limit=100)["id"]
    h.run_imports()
    # O relógio simulado anda com o relógio real: o degrau é conferido contra a janela medida em volta do run_pending
    # (o momento da pausa está nela), não contra o "agora" depois de o resto do lote b rodar (lento sob carga).
    before = _ms(sim.now())
    h.ctx.worker.run_pending(max_jobs=500)
    after = sim.now()
    assert h.ctx.batches.get(b)["status"] == "completed", "o outro lote não é afetado"
    row = lrow(h, a)
    assert row["paused_until"] and row["paused_reason"] == "repeated_failures", row
    api = h.batch(a)
    assert api["paused_reason"] == "download_failed" and api["paused_until"]
    counts = status_counts(h, a)
    assert 20 <= counts.get("failed", 0) < 100 and counts.get("pending", 0) > 0, counts
    assert h.ctx.worker.run_pending(max_jobs=50) == 0, "lote pausado: nada é reivindicado"
    until = parse_utc(row["paused_until"])
    assert before + timedelta(seconds=600) <= until <= after + timedelta(seconds=600), (before, until, after)
    first = (until - sim.now()).total_seconds()
    sim.advance(first + 1)
    assert h.batch(a)["paused_until"] is None and h.batch(a)["paused_reason"] is None, "campos só durante a pausa"
    before = _ms(sim.now())
    h.ctx.worker.run_pending(max_jobs=500)
    after = sim.now()
    until = parse_utc(lrow(h, a)["paused_until"])
    assert before + timedelta(seconds=1800) <= until <= after + timedelta(seconds=1800), (before, until, after)
    second = (until - sim.now()).total_seconds()
    dl.failing_prefix = None
    sim.advance(second + 1)
    h.ctx.worker.run_pending(max_jobs=500)
    assert status_counts(h, a).get("pending", 0) == 0
    assert h.ctx.db.scalar("SELECT pause_level FROM batches WHERE id = ?", (a,)) == 0, "sucesso zera o degrau"
    assert h.ctx.batches.get(a)["status"] == "completed"
    scan_secrets(h, caplog)


LEAKY_MESSAGE = ("GET https://scontent-fake.cdninstagram.com/v/a.jpg?oh=FAKE_SIGNED_1&oe=1 falhou, "
                 "cursor=FAKE_CURSOR_9")


class LeakyMessageDownloader(LazyPagedDownloader):
    def iter_profile_media(self, *a, **kw):
        raise InstagramError("network_error", LEAKY_MESSAGE, retryable=True)
        yield

    def download(self, item, dest_dir, *, cancel=None, progress=None):
        raise InstagramError("download_failed", LEAKY_MESSAGE, retryable=False)


def test_12b_error_messages_carrying_signed_urls_or_cursors_are_scrubbed(harness_factory, tmp_path, sim, caplog):
    h, provider, digest = internal_h(harness_factory, tmp_path, LeakyMessageDownloader(60), sim)
    batch_id = internal_batch(h, 60)
    h.run_imports()
    assert lrow(h, batch_id)["error_code"] == "network_error"
    h.ctx.batches.update_listing(batch_id, listing_state="pending", listing_retry_at=None)
    h.ctx.jobs.create_ingest(batch_id=batch_id, source={"source_post_id": "L1", "source_media_index": 0,
                                                       "media_url": "https://scontent.cdninstagram.com/x.jpg"},
                             preset_id=None, preset_name=None, params=None)
    h.ctx.worker.run_pending(max_jobs=5)
    scan_secrets(h, caplog)


# ============================================================================================ (12) segredos, sem sucesso


def test_12_secrets_do_not_leak_when_every_error_path_runs_in_one_app(harness_factory, tmp_path, sim, caplog):
    """Erros de listagem (5xx/429/auth/acesso), renovação recusada e disco, tudo no mesmo app, e depois a varredura."""
    dl = ExpiringDownloader(60, page_size=12, sim=sim, refresh_code="access_refused")
    h, provider, digest = internal_h(harness_factory, tmp_path, dl, sim)
    batch_id = internal_batch(h, 60)
    h.run_imports()
    h.ctx.worker.run_pending(max_jobs=200)
    for code in ("rate_limited", "request_auth_failed", "action_blocked", "access_refused", "network_error"):
        h.ctx.worker.pause_for(code)
    scan_secrets(h, caplog)
    assert_session_intact(provider, digest)
    assert hashlib.sha256(provider.path.read_bytes()).hexdigest() == digest
