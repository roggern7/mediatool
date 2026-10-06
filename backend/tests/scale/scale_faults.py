"""Cenarios de FALHA em escala (offline), em cima de scale_harness.py. Um cenario por processo:
    python scale_faults.py <cenario> <tamanho> <tmp_dir> [mem]

Cenarios: list_outage (erro de listagem rede/5xx no meio do lote), disk_full (disco cheio temporario: verificacao previa
e ENOSPC no meio do job), refresh_refused (renovacao recusada em parte das paginas), auth_failed (request_auth_failed
isolado), slow_processor (processamento ligado, processor lento, threads reais: downloads x FFmpeg).

O tempo das pausas e SIMULADO: quando o laco fica ocioso, `advance` vence os prazos (listing_retry_at, paused_until,
relogio do worker, recheck do disco) e soma o tempo que teria esperado em `sim_wait_s`.
"""

from __future__ import annotations

import errno
import json
import sys
import threading
import time
import tracemalloc
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import scale_harness as sh  # noqa: E402
from scale_harness import (  # noqa: E402
    PAGE, QueryCounter, Run, Stats, TERMINAL, api_probe, create_batch, db_sizes, final_checks, make_downloader,
    make_processor, rss_mb,
)
from app.db.connection import parse_utc, utc_now  # noqa: E402
from app.integrations.instagram.downloader import InstagramError  # noqa: E402
from conftest import Harness  # noqa: E402
from media_fakes import FakeProcessor  # noqa: E402

FAULT_SCENARIOS = ("list_outage", "disk_full", "refresh_refused", "auth_failed", "slow_processor")


@dataclass
class Fault:
    """Ganchos chamados pelo ScaleDownloader (threads do worker: contadores simples, GIL basta)."""

    list_down_remaining: int = 0
    list_error_code: str = "network_error"
    list_errors: int = 0
    auth_fail_page: int | None = None
    auth_fired: bool = False
    refuse_pct: int = 0  # % das paginas cuja renovacao e recusada
    refuse_code: str = "access_refused"
    refused_calls: int = 0
    enospc_remaining: int = 0
    enospc_raised: int = 0
    download_delay: float = 0.0
    dl_active: int = 0
    dl_max_active: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def page_refused(self, page: int) -> bool:
        return self.refuse_pct > 0 and (page * 7919) % 100 < self.refuse_pct

    def on_list_page(self, page: int) -> None:
        if self.auth_fail_page == page and not self.auth_fired:
            self.auth_fired = True
            raise InstagramError("request_auth_failed", retryable=True)
        if self.list_down_remaining > 0:
            self.list_down_remaining -= 1
            self.list_errors += 1
            raise InstagramError(self.list_error_code, retryable=True)

    def on_refresh(self, page: int) -> None:
        if self.page_refused(page):
            self.refused_calls += 1
            raise InstagramError(self.refuse_code, retryable=False)

    def on_download(self, i: int) -> None:
        if self.enospc_remaining > 0:
            self.enospc_remaining -= 1
            self.enospc_raised += 1
            raise OSError(errno.ENOSPC, "No space left on device")
        if self.download_delay:
            with self._lock:
                self.dl_active += 1
                self.dl_max_active = max(self.dl_max_active, self.dl_active)
            try:
                time.sleep(self.download_delay)
            finally:
                with self._lock:
                    self.dl_active -= 1


class SlowProcessor(FakeProcessor):
    """FakeProcessor cuja etapa de FFmpeg demora `delay` s; conta processados e concorrencia maxima."""

    def __init__(self, delay: float):
        super().__init__()
        self.delay = delay
        self.n_processed = 0
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()
        self.thumbnails = sh._NoList()
        self.processed = sh._NoList()

    def process(self, src, dst, opts, *, progress=None, cancel=None, timeout=None):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay)
            result = super().process(src, dst, opts, progress=progress, cancel=cancel, timeout=timeout)
        finally:
            with self._lock:
                self.active -= 1
        with self._lock:
            self.n_processed += 1
        return result


class Ctl:
    """Tempo simulado + gatilhos de uma execucao de falha."""

    def __init__(self, h: Harness, batch_id: int, stats: Stats, fault: Fault, n: int):
        self.h, self.batch_id, self.stats, self.fault, self.n = h, batch_id, stats, fault, n
        self.offset = 0.0  # segundos simulados somados ao relogio do worker
        self.sim_wait_s = 0.0
        self.advances = 0
        self.events: list[dict[str, Any]] = []
        self.disk_down = False
        h.ctx.worker.clock = lambda: datetime.now(UTC) + timedelta(seconds=self.offset)

    def note(self, what: str, **extra: Any) -> None:
        db = self.h.ctx.db
        self.events.append({
            "event": what, "completed": self.stats.completed(), "sim_wait_s": round(self.sim_wait_s),
            "attempts_sum": db.scalar("SELECT COALESCE(SUM(attempts),0) FROM jobs WHERE batch_id = ?",
                                      (self.batch_id,)),
            "pages_requested": sum(self.stats.page_requests.values()),
            "listing_state": self.h.ctx.batches.get(self.batch_id)["listing_state"], **extra})

    def advance(self) -> bool:
        """Simula a passagem do tempo ate o proximo prazo. False se nada estava esperando."""
        h, db = self.h, self.h.ctx.db
        worker = h.ctx.worker
        now = utc_now()
        row = db.fetchone("SELECT listing_retry_at, paused_until FROM batches WHERE id = ?", (self.batch_id,))
        wait = 0.0
        for col in ("listing_retry_at", "paused_until"):
            val = row[col]
            if val and val > now:
                wait = max(wait, (parse_utc(val) - parse_utc(now)).total_seconds())
                db.execute(f"UPDATE batches SET {col} = ? WHERE id = ?", (utc_now(-1), self.batch_id))
        until, _reason = worker.pause_state()
        if until:
            wait = max(wait, (parse_utc(until) - worker.clock()).total_seconds())
        if getattr(worker, "_disk_full", False):
            wait = max(wait, sh.__dict__.get("DISK_WAIT", 60.0))
            worker._disk_next_check = 0.0
        if self.disk_down:
            wait = max(wait, 180.0)  # disco "liberado" depois de 3 min simulados
            self.disk_down = False
            self.note("disk_released")
        if wait <= 0:
            return False
        self.offset += wait + 1
        self.sim_wait_s += wait
        self.advances += 1
        worker.recovery.tick()
        return True


def drive(ctl: Ctl, run: Run, on_iter, *, max_advances: int = 400) -> str:
    h, batch_id = ctl.h, ctl.batch_id
    idle = 0
    while True:
        ran = h.ctx.import_runner.run_next()
        run.sample(h, batch_id)
        jobs = h.ctx.worker.run_pending(max_jobs=25)
        run.sample(h, batch_id)
        on_iter(ctl.stats.completed())
        if h.ctx.batches.get(batch_id)["status"] in TERMINAL:
            return "terminal"
        idle = idle + 1 if not ran and not jobs else 0
        if idle >= 3:
            if ctl.advances >= max_advances:
                return "advance_cap"
            if not ctl.advance():
                return "idle"
            idle = 0


def _build(name: str, n: int):
    stats = Stats(n)
    fault = Fault()
    kw: dict[str, Any] = {}
    if name == "refresh_refused":
        kw["expired_pct"] = 30
        fault.refuse_pct = 25
    dl = make_downloader(stats, fault=fault, **kw)
    return stats, fault, dl


def fault_scenario(name: str, n: int, tmp: Path, use_tracemalloc: bool = False) -> dict[str, Any]:
    assert name in FAULT_SCENARIOS
    if name == "slow_processor":
        return slow_processor(n, tmp, use_tracemalloc)
    root = tmp / f"{name}-{n}"
    root.mkdir(parents=True)
    stats, fault, dl = _build(name, n)
    qc = QueryCounter()
    qc.install()
    import app as _app

    report: dict[str, Any] = {"app_path": str(Path(_app.__file__).parent), "scenario": name, "size": n,
                              "tracemalloc": use_tracemalloc}
    if use_tracemalloc:
        tracemalloc.start()
    h = None
    try:
        h = Harness(root, downloader=dl, processor=make_processor()).__enter__()
        batch_id = create_batch(h, n, report)
        ctl = Ctl(h, batch_id, stats, fault, n)
        run = Run(n, root, qc, stats, use_tracemalloc)
        probes: dict[str, Any] = {}
        state = {"phase": 0}

        if name == "list_outage":
            def on_iter(done: int) -> None:
                if state["phase"] == 0 and done >= 0.4 * n:
                    state["phase"] = 1
                    fault.list_down_remaining = 3  # 3 tentativas seguidas falham: 1m + 5m + 15m simulados
                    ctl.note("outage_start", completed_mark=done)
                elif state["phase"] == 1 and fault.list_down_remaining == 0:
                    state["phase"] = 2
                    ctl.note("outage_end")
        elif name == "disk_full":
            def on_iter(done: int) -> None:
                if state["phase"] == 0 and done >= 0.5 * n:
                    state["phase"] = 1
                    ctl.disk_down = True
                    ok = lambda: (not ctl.disk_down, 1 << 30, 5 << 30)  # noqa: E731
                    h.ctx.worker.disk_check = ok
                    ctl.note("disk_down_start")
                elif state["phase"] == 1 and not ctl.disk_down:
                    state["phase"] = 2
                elif state["phase"] == 2 and done >= 0.75 * n:
                    state["phase"] = 3
                    fault.enospc_remaining = 30  # 30 downloads batem em ENOSPC no meio do job
                    ctl.note("enospc_start")
        elif name == "auth_failed":
            fault.auth_fail_page = (n // PAGE) // 2

            def on_iter(done: int) -> None:
                if fault.auth_fired and state["phase"] == 0:
                    state["phase"] = 1
                    ctl.note("auth_failed_fired")
        else:
            def on_iter(done: int) -> None:
                return None

        report["end_reason"] = drive(ctl, run, on_iter)
        ctl.note("end")
        run.sample(h, batch_id, force=True)
        rss_pre, peak_pre = rss_mb()
        report["rss_peak_before_api_mb"] = peak_pre and round(peak_pre, 1)
        if use_tracemalloc:
            report["traced_peak_before_api_mb"] = round(tracemalloc.get_traced_memory()[1] / 2**20, 2)
            tracemalloc.reset_peak()
        probes["final"] = api_probe(h, batch_id, n)
        settings = h.client.get("/api/settings")
        settings_ms = []
        for _ in range(3):
            t = time.perf_counter()
            h.client.get("/api/settings")
            settings_ms.append((time.perf_counter() - t) * 1000)
        probes["settings"] = {"bytes": len(settings.content), "ms": round(sorted(settings_ms)[1], 2),
                              "disk": settings.json().get("disk")}
        checks = final_checks(h, batch_id, stats)
        db = h.ctx.db
        brow = db.fetchone("SELECT listing_retry_level, pause_level, error_code, paused_reason, refresh_fail_streak, "
                           "listing_cursor_refusals FROM batches WHERE id = ?", (batch_id,))
        report.update({
            "elapsed_s": round(time.perf_counter() - run.t0, 2), "queries_total": qc.total,
            "queries_per_item": round(qc.total / n, 2), "max_pending_ingest": run.max_pending,
            "db_final": db_sizes(root), "db_peak": run.max_db, "progress": run.progress, "api": probes,
            "top_query_shapes": qc.shapes.most_common(15), "sim_wait_s": round(ctl.sim_wait_s),
            "advances": ctl.advances, "events": ctl.events, "batch_row": dict(brow) if brow else None,
            "attempts_sum": db.scalar("SELECT COALESCE(SUM(attempts),0) FROM jobs WHERE batch_id = ?", (batch_id,)),
            "failed_codes": {r[0]: r[1] for r in db.fetchall(
                "SELECT COALESCE(error_code,'-'), COUNT(*) FROM jobs WHERE batch_id = ? AND status = 'failed' "
                "GROUP BY 1", (batch_id,))},
            "fault": {"list_errors": fault.list_errors, "auth_fired": fault.auth_fired,
                      "refused_calls": fault.refused_calls, "enospc_raised": fault.enospc_raised},
            **checks,
        })
        if name == "refresh_refused":
            per_page = stats.refresh_pages
            refused_pages = sum(1 for p in per_page if fault.page_refused(p))
            expect_fail = sum(1 for i in range(n) if dl._is_expired(i) and fault.page_refused(i // PAGE + 1))
            report["refresh"] = {
                "expired_items": sum(1 for i in range(n) if dl._is_expired(i)),
                "pages_refreshed": len(per_page), "refresh_calls": sum(per_page.values()),
                "max_refresh_per_page": max(per_page.values(), default=0), "refused_pages_called": refused_pages,
                "expected_failed_jobs": expect_fail, "pages_total": len(stats.page_requests)}
        rss, peak = rss_mb()
        report["rss_end_mb"], report["rss_peak_mb"] = rss and round(rss, 1), peak and round(peak, 1)
        if use_tracemalloc:
            cur, pk = tracemalloc.get_traced_memory()
            report["traced_peak_api_probe_mb"], report["traced_current_end_mb"] = (
                round(pk / 2**20, 2), round(cur / 2**20, 2))
            report["traced_peak_mb"] = report["traced_peak_before_api_mb"]
        return report
    finally:
        if h is not None:
            try:
                h.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        if use_tracemalloc:
            tracemalloc.stop()
        qc.uninstall()


def slow_processor(n: int, tmp: Path, use_tracemalloc: bool, delay: float = 0.02) -> dict[str, Any]:
    """Processamento ligado, threads reais do worker e do import runner, FFmpeg falso de `delay` s/job."""
    root = tmp / f"slow_processor-{n}"
    root.mkdir(parents=True)
    stats = Stats(n)
    fault = Fault(download_delay=0.002)
    dl = make_downloader(stats, fault=fault)
    proc = SlowProcessor(delay)
    qc = QueryCounter()
    qc.install()
    report: dict[str, Any] = {"scenario": "slow_processor", "size": n, "ffmpeg_delay_s": delay,
                              "tracemalloc": use_tracemalloc}
    if use_tracemalloc:
        tracemalloc.start()
    h = None
    stop = threading.Event()
    try:
        h = Harness(root, downloader=dl, processor=proc, start_worker=True).__enter__()
        presets = h.client.get("/api/presets").json()
        presets = presets if isinstance(presets, list) else presets.get("items", [])
        preset_id = presets[0]["id"]
        report["preset_id"] = preset_id
        resp = h.client.post("/api/imports", json={"profile": "perfil_teste", "content_type": "all", "limit": n,
                                                   "preset_id": preset_id, "process": True})
        assert resp.status_code == 202, resp.text
        batch_id = resp.json()["id"]
        settings0 = h.client.get("/api/settings").json()
        report["pools"] = {"concurrency": settings0.get("concurrency"),
                           "download_concurrency": settings0.get("download_concurrency")}
        t0 = time.perf_counter()
        samples: list[dict[str, Any]] = []
        marks = {"downloads_done_s": None, "processed_done_s": None}
        db = h.ctx.db

        def sampler() -> None:
            while not stop.wait(0.5):
                try:
                    backlog = db.scalar("SELECT COUNT(*) FROM jobs WHERE batch_id = ? AND kind='ingest' "
                                        "AND status IN ('downloaded','processing')", (batch_id,))
                    pend = h.ctx.jobs.pending_ingest_count(batch_id)
                    glob = h.ctx.batches.pending_ingest_global()
                except Exception:  # noqa: BLE001
                    continue
                t = time.perf_counter() - t0
                samples.append({"t": round(t, 1), "dl_ok": stats.completed(), "processed": proc.n_processed,
                                "backlog_downloaded": backlog, "pending": pend, "pending_global": glob})
                if marks["downloads_done_s"] is None and stats.completed() >= n:
                    marks["downloads_done_s"] = round(t, 1)

        th = threading.Thread(target=sampler, daemon=True)
        th.start()
        deadline = time.time() + 3000
        status = None
        while time.time() < deadline:
            status = h.ctx.batches.get(batch_id)["status"]
            if status in TERMINAL:
                break
            time.sleep(0.5)
        marks["processed_done_s"] = round(time.perf_counter() - t0, 1)
        stop.set()
        th.join(2)
        report["end_reason"] = "terminal" if status in TERMINAL else "timeout"
        checks = final_checks(h, batch_id, stats)
        rss_pre, peak_pre = rss_mb()
        report["rss_peak_before_api_mb"] = peak_pre and round(peak_pre, 1)
        if use_tracemalloc:
            report["traced_peak_before_api_mb"] = round(tracemalloc.get_traced_memory()[1] / 2**20, 2)
        probes = {"final": api_probe(h, batch_id, n)}
        report.update({
            "elapsed_s": marks["processed_done_s"], "downloads_done_s": marks["downloads_done_s"],
            "processed_done_s": marks["processed_done_s"], "n_processed": proc.n_processed,
            "max_concurrent_ffmpeg": proc.max_active, "max_concurrent_downloads": fault.dl_max_active,
            "max_backlog_downloaded": max((s["backlog_downloaded"] for s in samples), default=0),
            "max_pending_ingest": max((s["pending"] for s in samples), default=0),
            "max_pending_global": max((s["pending_global"] for s in samples), default=0),
            "samples": samples[:: max(1, len(samples) // 25)], "queries_total": qc.total,
            "queries_per_item": round(qc.total / n, 2), "db_final": db_sizes(root), "api": probes,
            "renditions": db.scalar("SELECT COUNT(*) FROM renditions"), **checks,
        })
        if marks["downloads_done_s"]:
            report["download_vs_ffmpeg_ratio"] = round(marks["downloads_done_s"] / marks["processed_done_s"], 3)
        rss, peak = rss_mb()
        report["rss_end_mb"], report["rss_peak_mb"] = rss and round(rss, 1), peak and round(peak, 1)
        if use_tracemalloc:
            cur, pk = tracemalloc.get_traced_memory()
            report["traced_peak_mb"] = report["traced_peak_before_api_mb"]
            report["traced_current_end_mb"] = round(cur / 2**20, 2)
        return report
    finally:
        stop.set()
        if h is not None:
            try:
                h.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        if use_tracemalloc:
            tracemalloc.stop()
        qc.uninstall()


if __name__ == "__main__":
    result = fault_scenario(sys.argv[1], int(sys.argv[2]), Path(sys.argv[3]), use_tracemalloc=len(sys.argv) > 4)
    print("@@JSON@@" + json.dumps(result, default=str))
