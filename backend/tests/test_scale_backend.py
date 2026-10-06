"""Escala da API e do banco (docs/BATCH_ROBUSTNESS.md, seções 6 e 8). Offline, valores FAKE."""

from __future__ import annotations

import gc
import logging
import re
import threading
import time
import tracemalloc
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from app.db.connection import JOURNAL_SIZE_LIMIT, utc_now
from app.integrations.instagram.downloader import InstagramError
from app.jobs import pipeline
from app.jobs.import_runner import MAX_PENDING_INGEST
from app.jobs.pipeline import JobExecutor
from app.services import import_service
from media_fakes import FakeInstagramDownloader, FakeProcessor, remote

PROFILE = "perfil_teste"
# SCAN do índice parcial de cancelamento percorre só os jobs com cancel_requested = 1 (aceito).
_SCAN_JOBS = re.compile(r"^SCAN (jobs|j|l)\b(?! USING (COVERING )?INDEX ix_jobs_cancel)")
_FULL_RANGE = re.compile(r"\((batch_id|media_id)>\?\)")


# ---- utilitários --------------------------------------------------------------------------------------------------
@contextmanager
def _trace(h):
    statements: list[str] = []
    conn = h.ctx.db.conn
    conn.set_trace_callback(statements.append)
    try:
        yield statements
    finally:
        conn.set_trace_callback(None)


def _plans(h, statements: list[str]) -> dict[str, list[str]]:
    plans = {}
    for sql in statements:
        head = sql.lstrip().split(None, 1)[0].upper()
        if head not in ("SELECT", "UPDATE", "DELETE", "INSERT", "WITH"):
            continue
        rows = h.ctx.db.fetchall(f"EXPLAIN QUERY PLAN {sql}")
        plans[sql] = [row["detail"] for row in rows]
    return plans


def _batch(h, *, status: str = "running", source: str = "instagram_import") -> int:
    return h.ctx.batches.create(source=source, status=status, source_profile=PROFILE, content_type="all",
                                requested_limit=10000)


def _ingest(h, batch_id: int, code: str, *, url: bool = True, page: int = 1, cursor: str | None = None) -> int:
    source = {
        "source_platform": "instagram", "source_profile": PROFILE, "source_url": f"https://www.instagram.com/p/{code}/",
        "source_post_id": code, "source_media_index": 0, "media_type": "image",
        "media_url": f"https://scontent.cdninstagram.com/v/{code}_0.jpg?oh=SIGFAKE" if url else None,
        "listing_page_index": page, "listing_page_size": 12, "listing_cursor": cursor,
    }
    return h.ctx.jobs.create_ingest(batch_id=batch_id, source=source, preset_id=None, preset_name=None, params=None)


def _media(h, n: int, profile: str = PROFILE, start: int = 0) -> list[int]:
    ids = []
    for i in range(start, start + n):
        ids.append(h.ctx.media.insert({
            "source_platform": "instagram", "source_profile": profile, "source_post_id": f"M{profile}{i}",
            "source_media_index": 0, "media_type": "image", "original_path": f"originals/fake{profile}{i}.jpg",
            "original_sha256": f"{profile}-{i:060d}"[-64:], "file_size": 10,
        }))
    return ids


def _ref(h, job_id: int) -> dict:
    return h.ctx.jobs.get_remote_ref(job_id)


# ---- planos de consulta -------------------------------------------------------------------------------------------
def test_hot_queries_use_indexes_without_scanning_jobs(harness):
    h = harness
    batch_id = _batch(h)
    other = _batch(h)
    for i in range(60):
        _ingest(h, batch_id, f"A{i}", page=1 + i // 12, cursor=None if i < 12 else f"FAKE-CURSOR-{i // 12}")
        _ingest(h, other, f"B{i}")
    h.ctx.db.execute("UPDATE jobs SET status = 'completed', finished_at = ? WHERE id % 3 = 0", (utc_now(),))
    h.ctx.db.execute("UPDATE jobs SET cancel_requested = 1 WHERE id % 7 = 0 AND status = 'pending'")
    _media(h, 20)
    jobs = h.ctx.jobs
    with _trace(h) as statements:
        h.ctx.batches.refresh_status(batch_id)
        jobs.pending_ingest_count(batch_id)
        jobs.count_on_page(batch_id, 2)
        jobs.page_refreshed_at(batch_id, 2)
        jobs.non_terminal_ids(batch_id=batch_id)
        jobs.batch_stats([batch_id, other])
        jobs.list(batch_id=batch_id, limit=20, before_id=40)
        jobs.list(batch_id=batch_id, statuses=["pending"], limit=20)
        jobs.next_wakeup()
        jobs.recover_stale(cutoff=utc_now(), exclude_ids=[])
        jobs.recover_interrupted()
        jobs.finalize_cancel_requests()
        claimed = jobs.claim(allow_downloads=True, owner="t1")
        jobs.claim(allow_downloads=False, owner="t2")
        jobs.claim(allow_downloads=True, url_only=True, owner="t3")
        jobs.cancel_batch_jobs(other)
        h.ctx.media.list(profile=PROFILE.upper(), limit=10, before_id=15)
    assert claimed is not None
    plans = _plans(h, statements)
    assert len(plans) >= 17
    offenders = {sql: plan for sql, plan in plans.items()
                 if any(_SCAN_JOBS.match(d) or _FULL_RANGE.search(d) for d in plan)}
    assert not offenders, offenders
    joined = "\n".join(d for plan in plans.values() for d in plan)
    for index in ("ix_jobs_batch_cover", "ix_jobs_status_id", "ix_jobs_cancel", "ix_jobs_claimed",
                  "ix_media_profile_nocase", "ix_jobs_batch_page"):
        assert index in joined, index


def test_migration_moves_remote_refs_and_creates_indexes(harness):
    columns = {row["name"] for row in harness.ctx.db.fetchall("PRAGMA table_info(jobs)")}
    assert not {"media_url", "thumbnail_url", "listing_cursor"} & columns
    assert {"listing_page_index", "listing_page_size"} <= columns
    indexes = {row["name"] for row in harness.ctx.db.fetchall("PRAGMA index_list(jobs)")}
    assert {"ix_jobs_batch_cover", "ix_jobs_status_id", "ix_jobs_rendition", "ix_jobs_cancel",
            "ix_jobs_batch_page"} <= indexes
    assert "ix_jobs_batch" not in indexes
    assert "ix_media_profile_nocase" in {r["name"] for r in harness.ctx.db.fetchall("PRAGMA index_list(media)")}


# ---- API ----------------------------------------------------------------------------------------------------------
def test_batch_detail_has_no_jobs_by_default_and_caps_included_jobs(harness):
    h = harness
    batch_id = _batch(h)
    ids = [_ingest(h, batch_id, f"J{i}") for i in range(520)]
    plain = h.client.get(f"/api/batches/{batch_id}").json()
    assert plain["jobs"] is None and plain["jobs_truncated"] is None
    assert plain["progress"]["pending"] == 520
    full = h.client.get(f"/api/batches/{batch_id}", params={"include_jobs": "true"}).json()
    assert len(full["jobs"]) == 500 and full["jobs_truncated"] is True
    assert [j["id"] for j in full["jobs"]] == sorted(ids)[-500:]
    listed = h.client.get("/api/batches").json()["items"][0]
    assert listed.get("jobs") is None


def test_keyset_pagination_of_jobs_is_stable_while_items_arrive(harness):
    h = harness
    batch_id = _batch(h)
    original = [_ingest(h, batch_id, f"K{i}") for i in range(35)]
    seen: list[int] = []
    before = None
    pages = 0
    while True:
        params = {"batch_id": batch_id, "limit": 10, **({"before_id": before} if before else {})}
        body = h.client.get("/api/jobs", params=params).json()
        seen.extend(j["id"] for j in body["items"])
        assert body["counts"]["pending"] >= 35
        _ingest(h, batch_id, f"NEW{pages}")  # entra item novo entre as páginas
        pages += 1
        before = body["next_before_id"]
        if before is None:
            break
    assert seen == sorted(original, reverse=True)
    assert pages == 4


def test_keyset_pagination_of_media_is_stable_while_items_arrive(harness):
    h = harness
    original = _media(h, 25)
    seen: list[int] = []
    before = None
    while True:
        body = h.client.get("/api/media", params={"limit": 7, **({"before_id": before} if before else {})}).json()
        seen.extend(m["id"] for m in body["items"])
        _media(h, 1, start=1000 + len(seen))
        before = body["next_before_id"]
        if before is None:
            break
    assert seen == sorted(original, reverse=True)
    last = h.client.get("/api/media", params={"limit": 200}).json()
    assert last["next_before_id"] is None and last["total"] == len(last["items"])


def test_process_by_filter_creates_jobs_in_one_transaction_with_cap(harness, monkeypatch):
    h = harness
    _media(h, 6, profile="alvo")
    _media(h, 3, profile="outro")
    r = h.client.post("/api/media/process-by-filter", json={"view": "all", "profile": "@ALVO", "preset_id": 1})
    assert r.status_code == 202, r.text
    batch = r.json()
    assert batch["source"] == "library" and batch["found"] == 6 and batch["jobs"] is None
    jobs = h.ctx.jobs.for_batch(batch["id"])
    assert len(jobs) == 6 and {j["kind"] for j in jobs} == {"process"}
    assert {j["source_profile"] for j in jobs} == {"alvo"}
    none = h.client.post("/api/media/process-by-filter", json={"view": "all", "q": "nada-disso", "preset_id": 1})
    assert none.status_code == 422 and none.json()["code"] == "no_matching_media"
    monkeypatch.setattr(import_service, "MAX_BULK_PROCESS", 5)
    count = h.ctx.db.scalar("SELECT COUNT(*) FROM batches")
    over = h.client.post("/api/media/process-by-filter", json={"view": "all", "preset_id": 1})
    assert over.status_code == 422 and over.json()["code"] == "bulk_limit_exceeded"
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM batches") == count  # nada criado
    extra = h.client.post("/api/media/process-by-filter", json={"view": "all", "preset_id": 1, "extra": 1})
    assert extra.status_code == 422


def test_import_accepts_large_quantities_free_n_and_all(harness):
    # Mudança confirmada pelo dono: N livre (1 a 1.000.000) e "Todos" (null) aceitos; antes só a lista fechada.
    h = harness
    for limit in (250, 500, 1000, 2000, 5000, 10000, 3, 300, 20000, None):  # perfis distintos (mesmo perfil: 409)
        assert h.import_profile(profile=f"perfil_{limit}".lower(), limit=limit)["requested_limit"] == limit
    for limit in (0, -5, 1_000_001):
        r = h.client.post("/api/imports", json={"profile": PROFILE, "content_type": "all", "limit": limit,
                                                "preset_id": None, "process": False})
        assert r.status_code == 422


def test_batch_cancel_is_one_update_and_clears_refs_and_cursor(harness):
    h = harness
    batch_id = _batch(h, status="listing")
    h.ctx.batches.update_listing(batch_id, listing_cursor="FAKE-CURSOR-9", listing_next_page=9)
    ids = [_ingest(h, batch_id, f"C{i}", cursor="FAKE-CURSOR-1") for i in range(300)]
    running = h.ctx.jobs.claim(allow_downloads=True, owner=h.ctx.worker.owner)
    event = threading.Event()
    h.ctx.worker._active[running["id"]] = event
    with _trace(h) as statements:
        import_service.cancel_batch(h.ctx, batch_id)
    assert h.client.get(f"/api/batches/{batch_id}").json()["status"] == "cancelled"
    updates = [s for s in statements if s.lstrip().upper().startswith("UPDATE JOBS")]
    assert len(updates) == 1, updates
    assert event.is_set()
    rows = {j["id"]: j for j in h.ctx.jobs.for_batch(batch_id)}
    assert rows[running["id"]]["status"] == "downloading" and rows[running["id"]]["cancel_requested"]
    assert all(rows[i]["status"] == "cancelled" for i in ids if i != running["id"])
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM job_remote_refs") == 0
    assert h.ctx.db.scalar("SELECT listing_cursor FROM batches WHERE id = ?", (batch_id,)) is None


# ---- job_remote_refs ----------------------------------------------------------------------------------------------
def test_remote_refs_lifecycle(harness):
    h = harness
    h.downloader.download_errors = {"R0:0": [InstagramError("content_unavailable")]}
    batch_id = _batch(h)
    failed_id = _ingest(h, batch_id, "R0", page=2, cursor="FAKE-CURSOR-2")
    sibling_id = _ingest(h, batch_id, "R1", page=2, cursor="FAKE-CURSOR-2")
    h.ctx.worker.run_pending(max_jobs=1)
    failed = h.ctx.jobs.get(failed_id)
    assert failed["status"] == "failed"
    ref = _ref(h, failed_id)
    assert ref["media_url"] is None and ref["thumbnail_url"] is None
    assert ref["listing_cursor"] == "FAKE-CURSOR-2" and ref["listing_page_index"] == 2  # retry com renovação
    assert _ref(h, sibling_id)["media_url"]  # job pendente mantém a URL
    h.ctx.worker.run_pending()
    assert h.ctx.jobs.get(sibling_id)["status"] == "completed"
    assert h.batch(batch_id)["status"] == "completed"
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM job_remote_refs") == 0  # lote concluído: tudo apagado


def test_completed_job_deletes_its_ref_while_batch_open(harness):
    h = harness
    batch_id = _batch(h)
    done_id = _ingest(h, batch_id, "D0")
    waiting_id = _ingest(h, batch_id, "D1")
    h.ctx.db.execute("UPDATE jobs SET next_attempt_at = ? WHERE id = ?", (utc_now(3600), waiting_id))
    h.ctx.worker.run_pending()
    assert h.ctx.jobs.get(done_id)["status"] == "completed" and _ref(h, done_id) == {}
    assert _ref(h, waiting_id)["media_url"]


# ---- manutenção: retenção e WAL -----------------------------------------------------------------------------------
def test_retention_keeps_last_job_per_media_and_open_batches(harness):
    h = harness
    old = "2020-01-01T00:00:00.000Z"
    (media_id,) = _media(h, 1)
    media = h.ctx.media.get(media_id)
    closed = _batch(h, status="completed", source="library")
    open_batch = _batch(h, status="running", source="library")
    first = h.ctx.jobs.create_process(media=media, batch_id=closed, preset_id=1, preset_name="Padrão", params={})
    last_of_media = h.ctx.jobs.create_process(media=media, batch_id=closed, preset_id=1, preset_name="Padrão",
                                              params={})
    loose = h.ctx.jobs.create_process(media=media, batch_id=None, preset_id=1,
                                      preset_name="Padrão", params={})
    in_open = h.ctx.jobs.create_process(media=media, batch_id=open_batch, preset_id=1, preset_name="Padrão",
                                        params={})
    recent = h.ctx.jobs.create_process(media=media, batch_id=closed, preset_id=1, preset_name="Padrão", params={})
    h.ctx.db.execute("UPDATE jobs SET status = 'completed', finished_at = ?", (old,))
    h.ctx.db.execute("UPDATE jobs SET finished_at = ? WHERE id = ?", (utc_now(), recent))
    newest = h.ctx.jobs.create_process(media=media, batch_id=closed, preset_id=1, preset_name="Padrão", params={})
    h.ctx.db.execute("UPDATE jobs SET status = 'failed', finished_at = ? WHERE id = ?", (old, newest))
    h.ctx.worker.maintenance()
    remaining = {row["id"] for row in h.ctx.db.fetchall("SELECT id FROM jobs")}
    assert remaining == {in_open, recent, newest}
    assert first not in remaining and last_of_media not in remaining and loose not in remaining
    assert h.client.get(f"/api/media/{media_id}").json()["last_job"]["id"] == newest


def test_wal_is_limited_and_truncated_when_idle(harness):
    h = harness
    assert h.ctx.db.scalar("PRAGMA journal_size_limit") == JOURNAL_SIZE_LIMIT == 16 * 1024 * 1024
    batch_id = _batch(h)
    for i in range(200):
        _ingest(h, batch_id, f"W{i}")
    wal = h.ctx.db.path.with_name(h.ctx.db.path.name + "-wal")
    assert wal.stat().st_size > 0
    h.ctx.worker.maintenance()
    assert wal.stat().st_size == 0


def test_vacuum_runs_only_when_freelist_is_large(harness, monkeypatch):
    from app.jobs import worker as worker_module

    h = harness
    assert h.ctx.worker._vacuum_if_fragmented() is False
    monkeypatch.setattr(worker_module, "VACUUM_MIN_FREE_PAGES", 1)
    monkeypatch.setattr(worker_module, "VACUUM_MIN_FREE_RATIO", 0.0)
    batch_id = _batch(h)
    for i in range(300):
        _ingest(h, batch_id, f"V{i}")
    h.ctx.db.execute("DELETE FROM jobs")
    assert h.ctx.db.scalar("PRAGMA freelist_count") > 0
    assert h.ctx.worker._vacuum_if_fragmented() is True
    assert h.ctx.db.scalar("PRAGMA freelist_count") == 0


# ---- progresso ----------------------------------------------------------------------------------------------------
def test_progress_written_at_most_every_two_seconds(harness, monkeypatch):
    h = harness
    batch_id = _batch(h)
    job = h.ctx.jobs.get(_ingest(h, batch_id, "G0"))
    writes = []
    monkeypatch.setattr(h.ctx.jobs, "update", lambda job_id, **fields: writes.append(fields))
    clock = [1000.0]
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: clock[0])
    executor = JobExecutor(h.ctx, h.ctx.worker, job, threading.Event())
    for step in range(1, 101):
        executor._report_progress(step / 100)
        clock[0] += 0.05  # 100 chamadas em 5 s
    assert 2 <= len(writes) <= 3
    executor._stage("finalizing", progress=1.0)  # mudança de etapa sempre grava
    assert writes[-1]["stage"] == "finalizing"


# ---- backpressure rígido ------------------------------------------------------------------------------------------
def test_backpressure_stops_mid_page_before_exceeding_max_pending(harness_factory):
    # Páginas de 30 itens: a fronteira de página sozinha deixaria passar de 100 pendentes.
    dl = FakeInstagramDownloader(items=[remote(f"H{i:04d}") for i in range(200)], page_size=30)
    h = harness_factory(downloader=dl)
    batch_id = h.ctx.batches.create(source="instagram_import", status="listing", source_profile=PROFILE,
                                    content_type="all", requested_limit=200)
    h.run_imports()
    pending = h.ctx.jobs.pending_ingest_count(batch_id)
    assert pending <= MAX_PENDING_INGEST
    for _ in range(50):
        h.ctx.worker.run_pending(max_jobs=30)
        h.run_imports()
        assert h.ctx.jobs.pending_ingest_count(batch_id) <= MAX_PENDING_INGEST
        if h.batch(batch_id)["status"] == "completed":
            break
    batch = h.client.get(f"/api/batches/{batch_id}").json()
    assert batch["status"] == "completed" and batch["found"] == 200
    assert len({j["source_post_id"] for j in h.ctx.jobs.for_batch(batch_id)}) == 200


# ---- pausas (escopo) e portão de requisições ----------------------------------------------------------------------
def _set_scope(h, monkeypatch, scope: str) -> None:
    worker = h.ctx.worker
    monkeypatch.setattr(worker, "current_pause_scope", lambda: scope)
    monkeypatch.setattr(worker, "ingest_paused", lambda: scope == "instagram")
    monkeypatch.setattr(worker, "downloads_paused", lambda: True)


def test_claim_respects_pause_scope(harness, monkeypatch):
    h = harness
    batch_id = _batch(h)
    with_url = _ingest(h, batch_id, "S0")
    without_url = _ingest(h, batch_id, "S1", url=False)
    (media_id,) = _media(h, 1)
    process = h.ctx.jobs.create_process(media=h.ctx.media.get(media_id), batch_id=None, preset_id=1,
                                        preset_name="Padrão", params={})
    _set_scope(h, monkeypatch, "instagram")
    claimed = h.ctx.worker._claim()
    assert claimed[0]["id"] == process and h.ctx.worker._claim() is None  # process nunca bloqueado
    h.ctx.jobs.release(process, h.ctx.worker.owner)
    h.ctx.worker._active.clear()
    h.ctx.db.execute("UPDATE jobs SET status = 'completed' WHERE id = ?", (process,))
    _set_scope(h, monkeypatch, "session")
    claimed = h.ctx.worker._claim()
    assert claimed[0]["id"] == with_url  # CDN não usa a sessão
    assert h.ctx.worker._claim() is None  # sem URL precisaria do Instagram
    assert h.ctx.jobs.get(without_url)["status"] == "pending"


@dataclass
class ExpiringDownloader(FakeInstagramDownloader):
    expired_prefix: str = "https://scontent.cdninstagram.com/v/"
    refresh_calls: list = field(default_factory=list)
    in_flight: int = 0
    max_in_flight: int = 0
    refresh_delay: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)
    gate: Any = None  # portão adquirido aqui, por requisição (papel do cliente HTTP do Instagram)

    def download(self, item, dest_dir, *, cancel=None, progress=None):
        if "FRESH" not in item.media_url:
            raise InstagramError("media_url_expired")
        return super().download(item, dest_dir, cancel=cancel, progress=progress)

    def refresh_page(self, profile, ref, *, cancel=None):
        with self.gate.slot(cancel=cancel) if self.gate is not None else nullcontext():
            return self._refresh_page(ref)

    def _refresh_page(self, ref):
        with self.lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.refresh_calls.append(ref.page_index)
        try:
            if self.refresh_delay:
                time.sleep(self.refresh_delay)
            size = ref.page_size
            return [replace(item, media_url=f"https://cdn.fake/FRESH-{item.source_post_id}.jpg", listing_ref=ref)
                    for item in self.items[(ref.page_index - 1) * size: ref.page_index * size]]
        finally:
            with self.lock:
                self.in_flight -= 1


class FakeGate:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.acquired = 0

    @contextmanager
    def slot(self, cancel=None):
        with self.lock:
            self.in_flight += 1
            self.acquired += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            yield
        finally:
            with self.lock:
                self.in_flight -= 1


def test_refresh_during_pause_requeues_without_attempt_and_resumes_through_gate(harness_factory, monkeypatch):
    items = [remote(f"E{i}") for i in range(3)]
    dl = ExpiringDownloader(items=items)
    h = harness_factory(downloader=dl)
    gate = FakeGate()
    h.ctx.instagram_gate = gate
    dl.gate = gate
    batch_id = _batch(h)
    ids = [_ingest(h, batch_id, f"E{i}") for i in range(3)]
    monkeypatch.setattr(h.ctx.worker, "current_pause_scope", lambda: "session")
    monkeypatch.setattr(h.ctx.worker, "ingest_paused", lambda: False)
    monkeypatch.setattr(h.ctx.worker, "downloads_paused", lambda: True)
    h.ctx.worker.run_pending()
    for job_id in ids:
        job = h.ctx.jobs.get(job_id)
        assert job["status"] == "pending" and job["stage"] == "waiting_retry" and job["attempts"] == 0
        assert job["error_code"] is None and _ref(h, job_id)["media_url"] is None
    assert dl.refresh_calls == [] and gate.acquired == 0
    assert h.ctx.worker.run_pending() == 0  # nada é reivindicado de novo durante a pausa
    monkeypatch.setattr(h.ctx.worker, "current_pause_scope", lambda: None)
    monkeypatch.setattr(h.ctx.worker, "downloads_paused", lambda: False)
    h.ctx.worker.run_pending()
    assert all(h.ctx.jobs.get(job_id)["status"] == "completed" for job_id in ids)
    assert dl.refresh_calls == [1] and gate.acquired == 1  # uma renovação de página (portão só no cliente)


def test_listing_suspended_by_any_pause_and_resumes_from_checkpoint(harness_factory, monkeypatch):
    dl = FakeInstagramDownloader(items=[remote(f"L{i:03d}") for i in range(48)])
    h = harness_factory(downloader=dl)
    gate = FakeGate()
    h.ctx.instagram_gate = gate
    batch_id = h.ctx.batches.create(source="instagram_import", status="listing", source_profile=PROFILE,
                                    content_type="all", requested_limit=48)
    for scope in ("session", "instagram"):
        monkeypatch.setattr(h.ctx.worker, "current_pause_scope", lambda scope=scope: scope)
        monkeypatch.setattr(h.ctx.worker, "downloads_paused", lambda: True)
        assert h.ctx.import_runner.run_next() is False
        assert dl.page_requests == []
    paused = {"on": False}
    monkeypatch.setattr(h.ctx.worker, "downloads_paused", lambda: paused["on"])
    original = h.ctx.import_runner._flush

    def flush_then_pause(batch, buffer, checkpoint):
        created = original(batch, buffer, checkpoint)
        if checkpoint is not None and checkpoint.next_page_index == 3:
            paused["on"] = True  # pausa começa durante a listagem (fim da página 2)
        return created

    monkeypatch.setattr(h.ctx.import_runner, "_flush", flush_then_pause)
    h.run_imports()
    assert dl.page_requests == [1, 2]
    assert h.ctx.db.scalar("SELECT listing_state FROM batches WHERE id = ?", (batch_id,)) == "paused"
    assert h.ctx.import_runner.run_next() is False
    paused["on"] = False
    h.run_imports()
    assert dl.page_requests == [1, 2, 3, 4]  # retomou do checkpoint
    assert gate.acquired == 0  # o runner não segura o portão; quem adquire é o cliente HTTP, por requisição
    assert h.ctx.jobs.count_for_batch(batch_id) == 48


def test_no_mass_release_after_pause_with_5000_waiting_jobs(harness_factory, monkeypatch):
    items = [remote(f"T{i:04d}") for i in range(5000)]
    dl = ExpiringDownloader(items=items, refresh_delay=0.01)
    h = harness_factory(downloader=dl)
    gate = FakeGate()
    h.ctx.instagram_gate = gate
    dl.gate = gate
    batch_id = _batch(h)
    with h.ctx.db.transaction():
        for i in range(5000):
            page = 1 + i // 12
            _ingest(h, batch_id, f"T{i:04d}", url=False, page=page,
                    cursor=None if page == 1 else f"FAKE-CURSOR-{page}")
    paused = {"on": True}
    monkeypatch.setattr(h.ctx.worker, "current_pause_scope", lambda: "instagram" if paused["on"] else None)
    monkeypatch.setattr(h.ctx.worker, "ingest_paused", lambda: paused["on"])
    monkeypatch.setattr(h.ctx.worker, "downloads_paused", lambda: paused["on"])
    assert h.ctx.worker.run_pending() == 0 and dl.refresh_calls == []
    monkeypatch.setattr(h.ctx.worker, "current_pause_scope", lambda: "session" if paused["on"] else None)
    assert h.ctx.worker.run_pending() == 0  # escopo session: sem URL não é reivindicado
    paused["on"] = False
    worker = h.ctx.worker
    worker.set_concurrency(4)
    peak_active = 0
    worker.start()
    try:
        deadline = time.monotonic() + 30
        while len(dl.refresh_calls) < 10 and time.monotonic() < deadline:
            peak_active = max(peak_active, worker.active_count)
            time.sleep(0.002)
    finally:
        worker.stop()
    assert len(dl.refresh_calls) >= 10
    assert gate.max_in_flight == 1 and dl.max_in_flight == 1  # 1 renovação em voo, pelo portão do cliente
    assert peak_active <= 4  # o claim nunca libera milhares de jobs de uma vez
    assert len(dl.refresh_calls) == len(set(dl.refresh_calls))  # cada página renovada uma vez



# ---- rodada final de escala ---------------------------------------------------------------------------------------
def test_batch_job_listings_have_no_temp_btree_and_counts_in_one_scan(harness):
    h = harness
    batch_id, other = _batch(h), _batch(h)
    for i in range(120):
        _ingest(h, batch_id, f"K{i}")
        _ingest(h, other, f"O{i}")
    h.ctx.db.execute("UPDATE jobs SET status = 'failed' WHERE id % 5 = 0")
    jobs = h.ctx.jobs
    calls = [
        {"batch_id": batch_id, "limit": 20},
        {"batch_id": batch_id, "limit": 20, "before_id": 150},
        {"batch_id": batch_id, "limit": 20, "offset": 60},
        {"batch_id": batch_id, "statuses": ["failed"], "limit": 20, "before_id": 200},
        {"batch_id": batch_id, "statuses": ["pending", "failed"], "limit": 20, "before_id": 200},
    ]
    for kwargs in calls:
        with _trace(h) as statements:
            items, total, counts, _ = jobs.list(**kwargs)
        assert len(statements) == 2, statements  # uma agregação (counts + total) e a página
        plans = _plans(h, statements)
        details = [d for plan in plans.values() for d in plan]
        assert not any("TEMP B-TREE" in d for d in details), (kwargs, plans)
        assert not any(_SCAN_JOBS.match(d) for d in details), (kwargs, plans)
        joined = "\n".join(details)
        assert "COVERING INDEX ix_jobs_batch_status_id" in joined  # GROUP BY status coberto
        assert "ix_jobs_batch_id" in joined or "ix_jobs_batch_status_id (batch_id=? AND status=?" in joined
        wanted = set(kwargs.get("statuses") or counts)
        assert total == sum(counts[s] for s in wanted)
        assert counts == {**dict.fromkeys(counts, 0), "pending": 96, "failed": 24}
    with _trace(h) as statements:
        jobs.recent_for_batch(batch_id, 10)
        jobs.for_batch(batch_id)
    assert not any("TEMP B-TREE" in d for plan in _plans(h, statements).values() for d in plan)


def test_new_indexes_and_final_counter_columns_exist(harness):
    db = harness.ctx.db
    indexes = {row["name"] for row in db.fetchall("PRAGMA index_list(jobs)")}
    assert {"ix_jobs_batch_id", "ix_jobs_batch_status_id"} <= indexes
    columns = {row["name"] for row in db.fetchall("PRAGMA table_info(batches)")}
    assert {"final_found", "final_downloaded", "final_processed", "final_skipped", "final_failed"} <= columns


def test_ingest_job_has_no_extra_transaction_for_remote_refs(harness):
    h = harness
    batch_id = _batch(h)
    ids = [_ingest(h, batch_id, f"Q{i}") for i in range(20)]
    with _trace(h) as statements:
        assert h.ctx.worker.run_pending() == 20
    assert all(h.ctx.jobs.get(job_id)["status"] == "completed" for job_id in ids)
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM job_remote_refs") == 0
    upper = [sql.lstrip().upper() for sql in statements]
    # Nenhuma transação explícita por job (as poucas que sobram são da manutenção do run_pending).
    assert sum(sql.startswith("BEGIN") for sql in upper) <= 3
    assert sum(sql.startswith("DELETE FROM JOB_REMOTE_REFS") for sql in upper) == 20  # 1 escrita por job
    assert sum(sql.startswith("SELECT * FROM JOB_REMOTE_REFS") for sql in upper) == 20  # 1 leitura por job
    assert sum(sql.startswith("UPDATE BATCHES") for sql in upper) <= 22  # 1 por job (+ o trigger da conclusão)
    assert len(statements) / 20 <= 30, len(statements) / 20


def test_retention_keeps_frozen_counters_of_finished_batches(harness):
    h = harness
    h.downloader.download_errors = {"F1:0": [InstagramError("content_unavailable")]}
    batch_id = _batch(h)
    for code in ("F0", "F1", "F2"):
        _ingest(h, batch_id, code)
    h.ctx.worker.run_pending()
    before = h.client.get(f"/api/batches/{batch_id}").json()
    assert before["status"] == "completed"
    assert before["progress"]["found"] == 3 and before["progress"]["downloaded"] == 2
    assert before["progress"]["failed"] == 1 and before["progress"]["skipped"] == 0
    frozen = h.ctx.batches.get(batch_id)
    assert (frozen["final_found"], frozen["final_downloaded"], frozen["final_failed"]) == (3, 2, 1)
    # Retenção apaga os jobs antigos do lote encerrado (o último job de cada mídia fica; aqui ele é de outro lote).
    later = _batch(h, source="library")
    for media in h.ctx.db.fetchall("SELECT * FROM media"):
        job_id = h.ctx.jobs.create_process(media=dict(media), batch_id=later, preset_id=1, preset_name="Padrão",
                                           params={})
        h.ctx.db.execute("UPDATE jobs SET status = 'completed', finished_at = ? WHERE id = ?", (utc_now(), job_id))
    h.ctx.db.execute("UPDATE jobs SET finished_at = '2020-01-01T00:00:00.000Z' WHERE batch_id = ?", (batch_id,))
    assert h.ctx.jobs.purge_finished_before(utc_now()) == 3
    assert h.ctx.jobs.count_for_batch(batch_id) == 0
    after = h.client.get(f"/api/batches/{batch_id}").json()
    assert after["found"] == 3
    assert {k: after["progress"][k] for k in ("found", "downloaded", "processed", "skipped", "failed")} == {
        k: before["progress"][k] for k in ("found", "downloaded", "processed", "skipped", "failed")}


def test_cancelled_batch_freezes_counters_and_reopen_unfreezes(harness):
    h = harness
    batch_id = _batch(h)
    _ingest(h, batch_id, "Z0")
    waiting = _ingest(h, batch_id, "Z1")
    h.ctx.db.execute("UPDATE jobs SET next_attempt_at = ? WHERE id = ?", (utc_now(3600), waiting))
    h.ctx.worker.run_pending()
    import_service.cancel_batch(h.ctx, batch_id)
    batch = h.ctx.batches.get(batch_id)
    assert batch["status"] == "cancelled" and (batch["final_found"], batch["final_downloaded"]) == (2, 1)
    h.ctx.db.execute("UPDATE jobs SET status = 'failed' WHERE batch_id = ? AND status = 'cancelled'", (batch_id,))
    import_service.retry_failed(h.ctx, batch_id)
    reopened = h.ctx.batches.get(batch_id)
    assert reopened["status"] == "running" and reopened["final_found"] is None


def test_keyed_locks_are_dropped_after_use(harness_factory):
    items = [remote(f"V{i}") for i in range(24)]
    dl = ExpiringDownloader(items=items)
    h = harness_factory(downloader=dl)
    batch_id = _batch(h)
    for i in range(24):
        page = 1 + i // 12
        _ingest(h, batch_id, f"V{i}", page=page, cursor=None if page == 1 else f"FAKE-CURSOR-{page}")
    h.ctx.worker.run_pending()
    assert all(j["status"] == "completed" for j in h.ctx.jobs.for_batch(batch_id))
    assert dl.refresh_calls == [1, 2]
    assert pipeline._refresh_locks == {}
    assert h.ctx.rendition_locks._locks == {}


class _NoList(list):
    def append(self, _x) -> None:
        return None


@pytest.mark.slow
def test_retained_memory_does_not_grow_with_number_of_jobs(harness_factory):
    """tracemalloc em 2k vs 6k jobs: a memória retida pelo app não cresce com o número de jobs do lote."""
    dl = FakeInstagramDownloader()
    dl.downloads = _NoList()
    proc = FakeProcessor()
    proc.thumbnails, proc.processed = _NoList(), _NoList()
    h = harness_factory(downloader=dl, processor=proc)
    batch_id = _batch(h)
    with h.ctx.db.transaction():
        for i in range(6000):
            _ingest(h, batch_id, f"U{i:05d}")

    def run(n: int) -> int:
        while n > 0:
            n -= h.ctx.worker.run_pending(max_jobs=min(n, 500))
        gc.collect()
        return tracemalloc.get_traced_memory()[0]

    # Os registros de log INFO de cada job ficariam retidos pela captura de log do pytest (não pelo app).
    logging.disable(logging.INFO)
    tracemalloc.start(10)
    try:
        run(300)  # aquecimento (caches de statements, imports tardios)
        at_2k = run(1700)
        snap_2k = tracemalloc.take_snapshot()
        at_6k = run(4000)
        snap_6k = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()
        logging.disable(logging.NOTSET)
    assert h.batch(batch_id)["status"] == "completed"
    growth = snap_6k.compare_to(snap_2k, "traceback")
    # Retenção por job aparece como muitos objetos novos no mesmo ponto de alocação (o achado era 1 por job).
    leaks = [str(stat) for stat in growth if stat.count_diff >= 400]
    assert not leaks, leaks
    # Bytes retidos por job, sem os redimensionamentos pontuais de tabelas internas (count 1-2: ex.: o dict de
    # sys.intern usado pelo pathlib cresce em degraus com a rotatividade de nomes, não com a retenção).
    per_job = sum(stat.size_diff for stat in growth if abs(stat.count_diff) > 2) / 4000
    assert per_job < 50, (at_2k, at_6k, per_job, [str(stat) for stat in growth[:3]])


# ---- pendências da rodada final ---------------------------------------------------------------------------------
def test_last_job_failing_in_worker_last_barrier_closes_the_batch(harness, monkeypatch):
    from app.jobs import worker as worker_module

    class Exploding:
        def __init__(self, *args, **kwargs):
            pass

        def run(self):
            raise RuntimeError("falha fora do pipeline")

    h = harness
    batch_id = _batch(h)
    job_id = _ingest(h, batch_id, "B0")
    monkeypatch.setattr(worker_module, "JobExecutor", Exploding)
    assert h.ctx.worker.run_pending() == 1
    job = h.ctx.jobs.get(job_id)
    assert job["status"] == "failed" and job["error_code"] == "internal_error"
    assert _ref(h, job_id).get("media_url") is None
    batch = h.ctx.batches.get(batch_id)
    assert batch["status"] == "completed" and batch["final_failed"] == 1


def test_closed_batch_summary_uses_frozen_counters_without_aggregating(harness):
    from app.services import presenters

    h = harness
    batch_id = _batch(h)
    for code in ("S0", "S1"):
        _ingest(h, batch_id, code)
    h.ctx.worker.run_pending()
    with _trace(h) as statements:
        data = presenters.batch_single(h.ctx, batch_id)
    assert not [s for s in statements if "FROM jobs" in s], statements
    assert data["status"] == "completed" and data["progress"]["found"] == 2 and data["progress"]["downloaded"] == 2
    assert data["counts"]["completed"] == 2 and data["counts"]["pending"] == 0
    open_batch = _batch(h)
    _ingest(h, open_batch, "S2")
    with _trace(h) as statements:
        data = presenters.batch_single(h.ctx, open_batch)
    assert [s for s in statements if "FROM jobs" in s]  # lote aberto continua agregando
    assert data["counts"]["pending"] == 1


def test_cancelled_batch_with_running_job_freezes_only_after_it_ends(harness):
    from app.services import presenters

    h = harness
    batch_id = _batch(h)
    for code in ("C0", "C1"):
        _ingest(h, batch_id, code)
    running = h.ctx.jobs.claim(allow_downloads=True, owner=h.ctx.worker.owner)
    import_service.cancel_batch(h.ctx, batch_id)
    assert h.ctx.batches.get(batch_id)["final_found"] is None  # ainda há job em execução
    assert presenters.batch_single(h.ctx, batch_id)["counts"]["downloading"] == 1
    JobExecutor(h.ctx, h.ctx.worker, running, threading.Event()).run()  # vê o cancelamento e termina
    batch = h.ctx.batches.get(batch_id)
    assert batch["final_found"] == 2 and batch["final_cancelled"] == 2


def test_job_reads_neither_settings_nor_cancel_flag_per_job(harness):
    h = harness
    batch_id = _batch(h)
    for i in range(20):
        _ingest(h, batch_id, f"R{i}")
    with _trace(h) as statements:
        assert h.ctx.worker.run_pending() == 20
    upper = [sql.lstrip().upper() for sql in statements]
    assert sum(sql.startswith("SELECT KEY, VALUE FROM SETTINGS") for sql in upper) <= 1
    assert not any(sql.startswith("SELECT CANCEL_REQUESTED") for sql in upper)


def test_cancel_requested_before_event_registration_is_seen_at_next_stage(harness):
    h = harness
    batch_id = _batch(h)
    job_id = _ingest(h, batch_id, "X0")
    job = h.ctx.jobs.claim(allow_downloads=True, owner="t")
    h.ctx.jobs.request_cancel(job_id)  # pedido sem evento sinalizado (corrida entre claim e registro)
    JobExecutor(h.ctx, h.ctx.worker, job, threading.Event()).run()
    assert h.ctx.jobs.get(job_id)["status"] == "cancelled"
    assert h.downloader.downloads == []  # parou na etapa de download, antes de baixar
