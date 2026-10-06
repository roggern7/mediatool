"""Harness de escala do Media Tool (offline). Roda o app real (create_app, SQLite em tmp) com downloader falso que gera
as paginas de 12 posts SOB DEMANDA (nunca materializa a lista), bytes pequenos e FakeProcessor (sem FFmpeg).

Uso: tests/scale/test_scale.py (pytest -m slow) e tools/scale_bench.py (relatorio JSON).
Cenarios: clean, expired, restart, cancel, unproductive, download_only (corpo padrao da API, so o pool de download).
"""

from __future__ import annotations

import array
import ctypes
import json
import os
import re
import sys
import time
import tracemalloc
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[1]
for extra in (BACKEND, BACKEND / "tests", BACKEND.parent / "processor"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from app.db import connection as db_connection  # noqa: E402
from app.integrations.instagram.downloader import (  # noqa: E402
    InstagramError,
    ListingCheckpoint,
    ListingRef,
)
from conftest import Harness  # noqa: E402
from media_fakes import FakeInstagramDownloader, FakeProcessor, remote  # noqa: E402

PAGE = 12
SCENARIOS = ("clean", "expired", "restart", "cancel", "unproductive", "download_only")
TERMINAL = ("completed", "failed", "cancelled", "completed_with_errors", "partial")


# ---------------------------------------------------------------------------------------------------------------------
# Medicoes de processo
# ---------------------------------------------------------------------------------------------------------------------
class _PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def rss_mb() -> tuple[float | None, float | None]:
    """(RSS atual, pico) em MB. psutil se existir; senao GetProcessMemoryInfo (Windows); senao (None, None)."""
    try:
        import psutil  # type: ignore

        info = psutil.Process().memory_info()
        peak = getattr(info, "peak_wset", info.rss)
        return info.rss / 2**20, peak / 2**20
    except ImportError:
        pass
    if os.name == "nt":
        counters = _PMC()
        counters.cb = ctypes.sizeof(_PMC)
        api = ctypes.windll.psapi.GetProcessMemoryInfo
        api.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PMC), ctypes.c_ulong]
        ctypes.windll.kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if api(handle, ctypes.byref(counters), counters.cb):
            return counters.WorkingSetSize / 2**20, counters.PeakWorkingSetSize / 2**20
    return None, None


class QueryCounter:
    """Conta statements SQL de todas as conexoes (set_trace_callback) e agrupa por forma normalizada."""

    _norm = re.compile(r"'(?:[^']|'')*'|\b\d+\b")

    def __init__(self) -> None:
        self.total = 0
        self.shapes: Counter[str] = Counter()
        self._orig = db_connection.Database._open

    def install(self) -> None:
        counter, orig = self, self._orig

        def traced_open(db_self):
            conn = orig(db_self)
            conn.set_trace_callback(counter._on_sql)
            return conn

        db_connection.Database._open = traced_open

    def uninstall(self) -> None:
        db_connection.Database._open = self._orig

    def _on_sql(self, sql: str) -> None:
        self.total += 1
        shape = " ".join(self._norm.sub("?", sql).split())[:110]
        if len(self.shapes) < 400 or shape in self.shapes:
            self.shapes[shape] += 1


# ---------------------------------------------------------------------------------------------------------------------
# Downloader falso sob demanda
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class Stats:
    """Contadores compartilhados entre instancias (reinicio); arrays compactos, sem objetos por item."""

    total: int
    ok: array.array = field(init=False)  # downloads concluidos por item
    attempts: array.array = field(init=False)
    page_requests: Counter = field(default_factory=Counter)  # pagina -> vezes pedida
    refresh_pages: Counter = field(default_factory=Counter)  # pagina -> chamadas de refresh_page
    refresh_times: dict = field(default_factory=dict)
    expired_hits: int = 0

    def __post_init__(self) -> None:
        self.ok = array.array("B", bytes(self.total))
        self.attempts = array.array("B", bytes(self.total))
        self.n_ok = 0

    def completed(self) -> int:
        return self.n_ok

    def repeated(self) -> int:
        return sum(v - 1 for v in self.ok if v > 1)


class _NoList(list):
    """Lista que nao guarda nada (o fake base acumula `downloads`)."""

    def append(self, _x) -> None:
        return None


@dataclass
class ScaleDownloader(FakeInstagramDownloader):
    stats: Stats = None  # type: ignore[assignment]
    total: int = 0
    expired_pct: int = 0  # % dos itens cuja 1a URL e recusada (media_url_expired)
    empty_between: int = 0  # paginas improdutivas (sem posts) entre paginas produtivas
    refreshable: bool = True
    fault: Any = None  # scale_faults.Fault: ganchos de falha (listagem/renovacao/download)

    def _is_expired(self, i: int) -> bool:
        return self.expired_pct > 0 and ((i * 2654435761) >> 7) % 100 < self.expired_pct

    @staticmethod
    def _item(i: int, *, fresh: bool = False):
        item = remote(f"S{i:06d}", 0, "image")
        return replace(item, media_url=item.media_url + "?FRESH=1") if fresh else item

    def _page_range(self, page: int) -> range:
        step = self.empty_between + 1
        if (page - 1) % step != 0:
            return range(0)
        start = ((page - 1) // step) * PAGE
        return range(start, min(start + PAGE, self.total))

    def _last_page(self) -> int:
        return max(1, -(-self.total // PAGE)) * (self.empty_between + 1)

    @staticmethod
    def index_of(item) -> int:
        return int(item.source_post_id[1:])

    def iter_profile_media(self, username, content, limit, *, cancel=None, resume=None,
                           on_checkpoint=None) -> Iterator:
        self.listings += 1
        produced = 0
        last = self._last_page()
        page = resume.next_page_index if resume is not None else 1
        pages = resume.pages if resume is not None else 0
        seen = resume.posts_seen if resume is not None else 0
        streak = resume.unproductive_streak if resume is not None else 0
        while page <= last:
            if cancel is not None and cancel.is_set():
                return
            if limit is not None and produced >= limit:
                return
            if self.fault is not None:
                self.fault.on_list_page(page)
            self.stats.page_requests[page] += 1
            self.page_requests.append(page)
            cursor = None if page == 1 else f"FAKE-CURSOR-{page}"
            rng = self._page_range(page)
            streak = 0 if len(rng) else streak + 1
            for i in rng:
                if cancel is not None and cancel.is_set():
                    return
                if limit is not None and produced >= limit:
                    return
                produced += 1
                yield replace(self._item(i), listing_ref=ListingRef(page_index=page, cursor=cursor, page_size=PAGE))
            pages += 1
            seen += len(rng)
            has_next = page < last
            if on_checkpoint is not None:
                on_checkpoint(ListingCheckpoint(
                    resume_cursor=f"FAKE-CURSOR-{page + 1}" if has_next else None, next_page_index=page + 1,
                    pages=pages, posts_seen=seen, items_compatible=seen, unproductive_streak=streak,
                    has_next=has_next))
            page += 1

    def refresh_page(self, profile, ref, *, cancel=None):
        self.stats.refresh_pages[ref.page_index] += 1
        self.stats.refresh_times.setdefault(ref.page_index, []).append(time.monotonic())
        if self.fault is not None:
            self.fault.on_refresh(ref.page_index)
        return [replace(self._item(i, fresh=True), listing_ref=ref) for i in self._page_range(ref.page_index)]

    def download(self, item, dest_dir, *, cancel=None, progress=None):
        i = self.index_of(item)
        self.stats.attempts[i] = min(255, self.stats.attempts[i] + 1)
        if self.fault is not None:
            self.fault.on_download(i)
        if self._is_expired(i) and "FRESH" not in item.media_url:
            self.stats.expired_hits += 1
            raise InstagramError("media_url_expired", retryable=False)
        path = super().download(item, dest_dir, cancel=cancel, progress=progress)
        if self.stats.ok[i] == 0:
            self.stats.n_ok += 1
        self.stats.ok[i] = min(255, self.stats.ok[i] + 1)
        return path


def make_downloader(stats: Stats, **kw) -> ScaleDownloader:
    dl = ScaleDownloader(stats=stats, total=stats.total, **kw)
    dl.downloads = _NoList()
    return dl


class CountingProcessor(FakeProcessor):
    """Conta as chamadas de processamento (FFmpeg) sem guardar nada por job."""

    process_calls = 0

    def process(self, src, dst, opts, **kw):
        self.process_calls += 1
        return super().process(src, dst, opts, **kw)


def make_processor() -> FakeProcessor:
    """FakeProcessor sem as listas de auditoria (thumbnails/processed): elas reteriam 1 Path por job e apareceriam
    como memoria retida do app (pipeline._ensure_thumbnail / storage.job_temp_dir) na passada tracemalloc."""
    proc = CountingProcessor()
    proc.thumbnails = _NoList()
    proc.processed = _NoList()
    return proc


# ---------------------------------------------------------------------------------------------------------------------
# Execucao
# ---------------------------------------------------------------------------------------------------------------------
def db_sizes(root: Path) -> dict[str, int]:
    out = {"db": 0, "wal": 0}
    for p in (root / "data").glob("media-tool.db*"):
        key = "wal" if p.name.endswith("-wal") else "shm" if p.name.endswith("-shm") else "db"
        out[key] = p.stat().st_size
    return out


def create_batch(h: Harness, n: int, report: dict, *, default_body: bool = False) -> int:
    """`default_body`: so os campos obrigatorios (sem `process`/`preset_id`): o padrao da API e download-only."""
    body = {"profile": "perfil_teste", "content_type": "all", "limit": n}
    if not default_body:
        body.update(preset_id=None, process=False)
    response = h.client.post("/api/imports", json=body)
    report["api_accepts_limit"] = response.status_code == 202
    if response.status_code == 202:
        return response.json()["id"]
    report["api_limit_rejection"] = f"{response.status_code} {response.text[:120]}"
    return h.ctx.batches.create(source="instagram_import", status="listing", source_profile="perfil_teste",
                                content_type="all", requested_limit=n)


def api_probe(h: Harness, batch_id: int, total: int) -> dict[str, Any]:
    """Tamanho (bytes) e tempo (ms, mediana de 3) das rotas de API do lote."""

    def timed(url: str) -> tuple[Any, float, int]:
        times, resp = [], None
        for _ in range(3):
            t = time.perf_counter()
            resp = h.client.get(url)
            times.append(time.perf_counter() - t)
        assert resp.status_code == 200, (url, resp.text[:200])
        return resp.json(), sorted(times)[1] * 1000, len(resp.content)

    out: dict[str, Any] = {}
    body, ms, size = timed(f"/api/batches/{batch_id}")
    out["batch_summary"] = {"bytes": size, "ms": round(ms, 2), "jobs_in_body": len(body.get("jobs") or [])}
    body, ms, size = timed(f"/api/jobs?batch_id={batch_id}&limit=100")
    out["jobs_page"] = {"bytes": size, "ms": round(ms, 2), "items": len(body["items"]),
                        "has_next_before_id_key": "next_before_id" in body,
                        "next_before_id": body.get("next_before_id")}
    nxt = body.get("next_before_id")
    if nxt:
        body2, ms2, size2 = timed(f"/api/jobs?batch_id={batch_id}&limit=100&before_id={nxt}")
        out["jobs_keyset_page2"] = {"bytes": size2, "ms": round(ms2, 2), "items": len(body2["items"])}
    body3, ms3, size3 = timed(f"/api/jobs?batch_id={batch_id}&limit=100&offset={max(0, total - 100)}")
    out["jobs_offset_deep"] = {"bytes": size3, "ms": round(ms3, 2), "items": len(body3["items"])}
    _, ms4, size4 = timed("/api/batches")
    out["batches_list"] = {"bytes": size4, "ms": round(ms4, 2)}
    return out


def _has_table(db, name: str) -> bool:
    return bool(db.scalar("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name = ?", (name,)))


def final_checks(h: Harness, batch_id: int, stats: Stats) -> dict[str, Any]:
    db = h.ctx.db
    statuses = {r[0]: r[1] for r in db.fetchall("SELECT status, COUNT(*) FROM jobs WHERE batch_id = ? GROUP BY status",
                                                (batch_id,))}
    dup_jobs = db.scalar("SELECT COUNT(*) FROM (SELECT 1 FROM jobs WHERE batch_id = ? AND kind = 'ingest' "
                         "GROUP BY source_post_id, source_media_index HAVING COUNT(*) > 1)", (batch_id,))
    batch = h.ctx.batches.get(batch_id)
    job_cols = {r[1] for r in db.fetchall("PRAGMA table_info(jobs)")}
    return {
        "batch_status": batch["status"], "found": batch["found"], "listing_state": batch["listing_state"],
        "job_statuses": statuses, "duplicate_jobs": dup_jobs, "media_rows": db.scalar("SELECT COUNT(*) FROM media"),
        "downloads_ok": stats.completed(), "downloads_repeated": stats.repeated(),
        "items_never_downloaded": sum(1 for v in stats.ok if v == 0),
        "pages_requested": sum(stats.page_requests.values()), "distinct_pages": len(stats.page_requests),
        "extra_page_requests": sum(v - 1 for v in stats.page_requests.values() if v > 1),
        "job_remote_refs_rows_left": db.scalar("SELECT COUNT(*) FROM job_remote_refs")
        if _has_table(db, "job_remote_refs") else None,
        "jobs_with_media_url_left": db.scalar("SELECT COUNT(*) FROM jobs WHERE media_url IS NOT NULL")
        if "media_url" in job_cols else None,
        "migrations": [r[0] for r in db.fetchall("SELECT version FROM schema_migrations ORDER BY 1")]
        if _has_table(db, "schema_migrations") else None,
        "has_job_remote_refs": _has_table(db, "job_remote_refs"),
    }


class Run:
    """Estado de uma execucao: contadores, picos e pontos de progresso."""

    def __init__(self, n: int, root: Path, qc: QueryCounter, stats: Stats, use_tracemalloc: bool):
        self.n, self.root, self.qc, self.stats = n, root, qc, stats
        self.tracemalloc = use_tracemalloc
        self.t0 = time.perf_counter()
        self.max_pending = 0
        self.max_db = {"db": 0, "wal": 0}
        self.progress: list[dict[str, Any]] = []
        self._next_mark = 0.1
        self.iterations = 0

    def sample(self, h: Harness, batch_id: int, force: bool = False) -> None:
        self.max_pending = max(self.max_pending, h.ctx.jobs.pending_ingest_count(batch_id))
        self.iterations += 1
        done = self.stats.completed()
        if self.iterations % 20 == 0 or force:
            sizes = db_sizes(self.root)
            for k in ("db", "wal"):
                self.max_db[k] = max(self.max_db[k], sizes.get(k, 0))
        if force or done >= self._next_mark * self.n:
            rss, peak = rss_mb()
            point: dict[str, Any] = {
                "completed": done, "elapsed_s": round(time.perf_counter() - self.t0, 2), "queries": self.qc.total,
                "rss_mb": rss and round(rss, 1), "rss_peak_mb": peak and round(peak, 1), "db": db_sizes(self.root)}
            if self.tracemalloc:
                cur, peak_t = tracemalloc.get_traced_memory()
                point["traced_current_mb"], point["traced_peak_mb"] = round(cur / 2**20, 2), round(peak_t / 2**20, 2)
            self.progress.append(point)
            while self._next_mark * self.n <= done:
                self._next_mark += 0.1


def run_download_pool(h: Harness, max_jobs: int) -> int:
    """Executa ate `max_jobs` claims SO do pool de download (o pool de FFmpeg nunca anda)."""
    worker = h.ctx.worker
    worker.maintenance()
    count = 0
    while count < max_jobs and (claimed := worker._claim("download")) is not None:
        worker._execute(*claimed)
        count += 1
    return count


def drive(h: Harness, run: Run, batch_id: int, *, until_completed: int | None = None, probes: dict | None = None,
          probe_at: tuple[float, ...] = (0.25, 0.5), download_pool_only: bool = False) -> str:
    """Alterna um trecho de listagem e ate 25 jobs, como runner/worker reais, ate terminar/parar/ficar ocioso."""
    todo = list(probe_at) if probes is not None else []
    idle = 0
    while True:
        ran = h.ctx.import_runner.run_next()
        run.sample(h, batch_id)
        jobs = run_download_pool(h, 25) if download_pool_only else h.ctx.worker.run_pending(max_jobs=25)
        run.sample(h, batch_id)
        done = run.stats.completed()
        if todo and done >= todo[0] * run.n:
            frac = todo.pop(0)
            probes[f"at_{int(frac * 100)}pct"] = api_probe(h, batch_id, run.n)
        if h.ctx.batches.get(batch_id)["status"] in TERMINAL:
            return "terminal"
        if until_completed is not None and done >= until_completed:
            return "target"
        idle = idle + 1 if not ran and not jobs else 0
        if idle >= 3:
            return "idle"


def scenario(name: str, n: int, tmp: Path, use_tracemalloc: bool = False) -> dict[str, Any]:
    assert name in SCENARIOS
    root = tmp / f"{name}-{n}"
    root.mkdir(parents=True)
    stats = Stats(n)
    kw: dict[str, Any] = {}
    if name == "expired":
        kw["expired_pct"] = 30
    if name == "unproductive":
        kw["empty_between"] = 3
    qc = QueryCounter()
    qc.install()
    import app as _app

    report: dict[str, Any] = {"app_path": str(Path(_app.__file__).parent), "scenario": name, "size": n, "expired_pct": kw.get("expired_pct", 0),
                              "empty_between": kw.get("empty_between", 0), "tracemalloc": use_tracemalloc}
    if use_tracemalloc:
        tracemalloc.start()
    harnesses: list[Harness] = []
    try:
        dl = make_downloader(stats, **kw)
        proc = make_processor()
        h = Harness(root, downloader=dl, processor=proc).__enter__()
        harnesses.append(h)
        batch_id = create_batch(h, n, report, default_body=name == "download_only")
        run = Run(n, root, qc, stats, use_tracemalloc)
        probes: dict[str, Any] = {}
        rss0, _ = rss_mb()
        report["rss_start_mb"] = rss0 and round(rss0, 1)

        if name in ("clean", "expired", "unproductive"):
            report["end_reason"] = drive(h, run, batch_id, probes=None if use_tracemalloc else probes)
        elif name == "download_only":
            report["end_reason"] = drive(h, run, batch_id, probes=None if use_tracemalloc else probes,
                                         download_pool_only=True)
            db = h.ctx.db
            report["download_only"] = {
                "batch_process": bool(h.ctx.batches.get(batch_id)["process"]),
                "process_jobs": db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'process'"),
                "jobs_with_processing": db.scalar(
                    "SELECT COUNT(*) FROM jobs WHERE params IS NOT NULL OR rendition_id IS NOT NULL"),
                "renditions": db.scalar("SELECT COUNT(*) FROM renditions"),
                "ffmpeg_process_calls": proc.process_calls,
                "ffmpeg_pool_claimable": h.ctx.worker._claim("process") is not None,
                "process_backlog": sum(h.ctx.jobs.process_backlog().values()),
            }
        elif name == "restart":
            report["end_reason"] = drive(h, run, batch_id, until_completed=n // 2,
                                         probes=None if use_tracemalloc else probes, probe_at=(0.25,))
            saved = h.ctx.db.fetchone("SELECT listing_next_page, listing_state FROM batches WHERE id = ?",
                                      (batch_id,))
            before = {"completed_before": stats.completed(), "pages_before": sum(stats.page_requests.values()),
                      "next_page_saved": saved["listing_next_page"], "listing_state": saved["listing_state"]}
            h.__exit__(None, None, None)
            harnesses.clear()
            dl2 = make_downloader(stats, **kw)
            h = Harness(root, downloader=dl2, processor=make_processor()).__enter__()
            harnesses.append(h)
            t = time.perf_counter()
            report["end_reason"] = drive(h, run, batch_id)
            report["restart"] = {**before, "resume_s": round(time.perf_counter() - t, 2),
                                 "first_page_requested_after_restart": (dl2.page_requests or [None])[0],
                                 "pages_requested_after_restart": len(dl2.page_requests)}
        elif name == "cancel":
            report["end_reason"] = drive(h, run, batch_id, until_completed=int(n * 0.3))
            done_before, pages_before = stats.completed(), sum(stats.page_requests.values())
            q0, t = qc.total, time.perf_counter()
            response = h.client.post(f"/api/batches/{batch_id}/cancel")
            cancel_ms = (time.perf_counter() - t) * 1000
            q_cancel = qc.total - q0
            drive(h, run, batch_id)
            for _ in range(5):
                h.ctx.import_runner.run_next()
                h.ctx.worker.run_pending()
            report["cancel"] = {
                "http": response.status_code, "api_ms": round(cancel_ms, 1), "queries": q_cancel,
                "downloads_after_cancel": stats.completed() - done_before,
                "pages_after_cancel": sum(stats.page_requests.values()) - pages_before,
                "non_terminal_after": h.ctx.db.scalar(
                    "SELECT COUNT(*) FROM jobs WHERE batch_id = ? AND status NOT IN "
                    "('completed','failed','cancelled')", (batch_id,)),
                "batch_status": h.ctx.batches.get(batch_id)["status"],
            }
        run.sample(h, batch_id, force=True)
        # Memoria do processamento do lote, ANTES das sondas de API (a resposta com jobs de um lote grande pesa MB).
        rss_pre, peak_pre = rss_mb()
        report["rss_peak_before_api_mb"] = peak_pre and round(peak_pre, 1)
        if use_tracemalloc:
            report["traced_peak_before_api_mb"] = round(tracemalloc.get_traced_memory()[1] / 2**20, 2)
            tracemalloc.reset_peak()
        probes["final"] = api_probe(h, batch_id, n)
        report.update({
            "elapsed_s": round(time.perf_counter() - run.t0, 2), "queries_total": qc.total,
            "queries_per_item": round(qc.total / n, 2), "max_pending_ingest": run.max_pending,
            "db_final": db_sizes(root), "db_peak": run.max_db, "progress": run.progress, "api": probes,
            "top_query_shapes": qc.shapes.most_common(40),
            **final_checks(h, batch_id, stats),
        })
        rss, peak = rss_mb()
        report["rss_end_mb"], report["rss_peak_mb"] = rss and round(rss, 1), peak and round(peak, 1)
        if use_tracemalloc:
            cur, pk = tracemalloc.get_traced_memory()
            report["traced_peak_api_probe_mb"], report["traced_current_end_mb"] = (
                round(pk / 2**20, 2), round(cur / 2**20, 2))
            report["traced_peak_mb"] = report["traced_peak_before_api_mb"]
        if kw.get("expired_pct"):
            per_page = stats.refresh_pages
            gaps = [b - a for ts in stats.refresh_times.values() for a, b in zip(ts, ts[1:])]
            report["refresh"] = {
                "expired_items": sum(1 for i in range(n) if dl._is_expired(i)), "expired_hits": stats.expired_hits,
                "refresh_calls": sum(per_page.values()), "pages_refreshed": len(per_page),
                "max_refresh_per_page": max(per_page.values(), default=0),
                "min_gap_same_page_s": round(min(gaps), 2) if gaps else None,
                "pages_total": len(stats.page_requests),
            }
        return report
    finally:
        for hh in reversed(harnesses):
            try:
                hh.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        if use_tracemalloc:
            tracemalloc.stop()
        qc.uninstall()


if __name__ == "__main__":  # um cenario por processo: scale_harness.py <cenario> <tamanho> <tmp_dir> [mem]
    result = scenario(sys.argv[1], int(sys.argv[2]), Path(sys.argv[3]), use_tracemalloc=len(sys.argv) > 4)
    print("@@JSON@@" + json.dumps(result, default=str))
