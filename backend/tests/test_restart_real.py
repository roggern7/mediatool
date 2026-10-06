"""Testes lentos com o backend como PROCESSO real (uvicorn + restart_app), FFmpeg real e kill -9 (taskkill /F /T).

Rodar: `pytest -m slow tests/test_restart_real.py`. Porta 8792, dados em diretório temporário.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from media_processor import sha256_file


BACKEND = Path(__file__).resolve().parents[1]
PORT = 8792
BASE = f"http://127.0.0.1:{PORT}"
HEADERS = {"X-Media-Tool-Client": "pytest"}
FFMPEG = shutil.which("ffmpeg")
pytestmark = [pytest.mark.slow, pytest.mark.skipif(FFMPEG is None, reason="ffmpeg ausente")]


def _wait(predicate: Callable[[], object], timeout: float = 90.0, interval: float = 0.03, what: str = ""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timeout esperando: {what}")


def _ffmpeg(*args: str) -> None:
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", *args], check=True, timeout=120)


def make_image(path: Path, color: str) -> Path:
    _ffmpeg("-f", "lavfi", "-i", f"color=c={color}:s=320x240", "-frames:v", "1", str(path))
    return path


def make_video(path: Path, seconds: int) -> Path:
    _ffmpeg(
        "-f", "lavfi", "-i", f"testsrc2=s=1280x720:r=30:d={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    )
    return path


def ffprobe_ok(path: Path) -> dict:
    out = subprocess.run(
        [shutil.which("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return json.loads(out.stdout)["format"]


class Server:
    """Backend em processo separado. `kill()` mata a árvore à força (sem lifespan/shutdown)."""

    def __init__(self, root: Path, manifest: Path):
        self.root = root
        self.manifest = manifest
        self.proc: subprocess.Popen | None = None
        self.db_path = root / "data" / "media-tool.db"
        self.runs = 0

    def start(self, *, worker: bool = True) -> None:
        env = {**os.environ, "RT_ROOT": str(self.root), "RT_MANIFEST": str(self.manifest), "RT_PORT": str(PORT),
               "PYTHONPATH": os.pathsep.join([str(BACKEND), str(BACKEND / "tests")]),
               "MEDIA_TOOL_ROOT": str(self.root)}
        env.pop("RT_NO_WORKER", None)
        if not worker:
            env["RT_NO_WORKER"] = "1"
        self.runs += 1
        log = open(self.root / f"server-{self.runs}.log", "wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "restart_app:app", "--host", "127.0.0.1", "--port", str(PORT),
             "--log-level", "warning"],
            cwd=BACKEND, env=env, stdout=log, stderr=subprocess.STDOUT,
        )

        def healthy() -> bool:
            if self.proc.poll() is not None:
                raise AssertionError(f"servidor morreu: {(self.root / f'server-{self.runs}.log').read_text()[-2000:]}")
            try:
                return httpx.get(f"{BASE}/api/health", timeout=1).status_code == 200
            except httpx.HTTPError:
                return False

        _wait(healthy, 30, 0.1, "servidor saudável")

    def kill(self) -> None:
        assert self.proc is not None
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)], capture_output=True, check=False)
        self.proc.wait(timeout=20)
        self.proc = None
        time.sleep(0.5)

    def stop(self) -> None:
        if self.proc is not None:
            self.kill()

    def api(self, method: str, path: str, **kwargs) -> httpx.Response:
        return httpx.request(method, f"{BASE}/api{path}", headers=HEADERS, timeout=30, **kwargs)

    def sql(self, query: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(query, params).fetchall()
        finally:
            conn.close()

    def job(self, job_id: int) -> sqlite3.Row:
        return self.sql("SELECT * FROM jobs WHERE id = ?", (job_id,))[0]

    def temp_files(self) -> list[Path]:
        temp = self.root / "storage" / "temp"
        return [p for p in temp.rglob("*") if p.is_file()] if temp.exists() else []

    def batch(self, batch_id: int) -> dict:
        response = self.api("GET", f"/batches/{batch_id}?include_jobs=true")  # lote sem jobs por padrão
        assert response.status_code == 200, response.text
        return response.json()

    def wait_batch(self, batch_id: int, status: str = "completed", timeout: float = 120.0) -> dict:
        _wait(lambda: self.batch(batch_id)["status"] == status, timeout, 0.2, f"lote {batch_id} {status}")
        return self.batch(batch_id)

    def import_profile(self, profile: str = "perfil_teste", process: bool = True) -> int:
        response = self.api("POST", "/imports", json={
            "profile": profile, "content_type": "all", "limit": 100, "preset_id": None, "process": process})
        assert response.status_code == 202, response.text
        return response.json()["id"]


@pytest.fixture(scope="module")
def media_files(tmp_path_factory) -> dict[str, Path]:
    base = tmp_path_factory.mktemp("rt-media")
    files: dict[str, Path] = {}
    colors = ["red", "green", "blue", "yellow", "magenta", "cyan", "orange", "purple", "pink", "brown", "gray", "teal"]
    for i, color in enumerate(colors):
        files[f"img{i}"] = make_image(base / f"img{i}.jpg", color)
    files["video"] = make_video(base / "long.mp4", 60)
    return files


@pytest.fixture
def server(tmp_path) -> Callable[[list[dict]], Server]:
    created: list[Server] = []

    def factory(entries: list[dict]) -> Server:
        root = tmp_path / "rt"
        root.mkdir()
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps(entries), encoding="utf-8")
        instance = Server(root, manifest)
        created.append(instance)
        return instance

    yield factory
    for instance in created:
        instance.stop()


def _entry(code: str, kind: str, file: Path, **extra) -> dict:
    return {"code": code, "type": kind, "file": str(file), **extra}


def _assert_never_false_completed(srv: Server) -> None:
    """Todo job completed tem original e (se for de processamento) rendition com arquivo existente."""
    for job in srv.sql("SELECT * FROM jobs WHERE status = 'completed'"):
        media = srv.sql("SELECT * FROM media WHERE id = ?", (job["media_id"],))[0]
        assert (srv.root / "storage" / media["original_path"]).is_file()
        if job["rendition_id"]:
            rend = srv.sql("SELECT * FROM renditions WHERE id = ?", (job["rendition_id"],))[0]
            assert (srv.root / "storage" / rend["processed_path"]).is_file()


def test_kill_during_processing_recovers_and_completes(server, media_files):
    entries = [
        _entry("IMG0", "image", media_files["img0"]),
        _entry("IMG1", "image", media_files["img1"]),
        _entry("LONGVID", "video", media_files["video"]),
        _entry("IMG2", "image", media_files["img2"]),
    ]
    srv = server(entries)
    srv.start()
    batch_id = srv.import_profile(process=True)

    long_job = _wait(
        lambda: next((r for r in srv.sql("SELECT * FROM jobs WHERE batch_id = ? AND source_post_id = 'LONGVID'",
                                         (batch_id,)) if r["status"] == "processing"), None),
        what="job do vídeo em processing",
    )
    job_id = long_job["id"]
    _wait(lambda: srv.temp_files(), what="temp parcial do ffmpeg")
    media_id = srv.job(job_id)["media_id"]
    assert media_id
    media = srv.sql("SELECT * FROM media WHERE id = ?", (media_id,))[0]
    original = srv.root / "storage" / media["original_path"]
    sha_before = sha256_file(original)
    assert sha_before == media["original_sha256"]
    attempts_before = srv.job(job_id)["attempts"]

    srv.kill()

    # estado deixado pelo processo morto
    row = srv.job(job_id)
    assert row["status"] == "processing"
    assert (srv.root / "storage" / "temp" / f"job-{job_id}").exists()
    assert srv.sql("SELECT COUNT(*) AS n FROM renditions WHERE media_id = ?", (media_id,))[0]["n"] == 0

    # reinício só com recovery (sem worker): verifica o estado recuperado
    srv.start(worker=False)
    row = srv.job(job_id)
    assert row["status"] == "downloaded"
    assert row["attempts"] == attempts_before  # reinício abrupto não consome tentativa (seção 9, M7)
    assert row["error_code"] == "interrupted"
    assert row["rendition_id"] is None
    assert srv.temp_files() == []
    assert not (srv.root / "storage" / "temp" / f"job-{job_id}").exists()
    assert sha256_file(original) == sha_before
    _assert_never_false_completed(srv)
    srv.kill()

    # reinício completo: tudo termina
    srv.start()
    result = srv.wait_batch(batch_id)
    assert result["progress"]["failed"] == 0
    jobs = {j["id"]: j for j in result["jobs"]}
    assert all(j["status"] == "completed" for j in jobs.values()), [j["status"] for j in jobs.values()]
    final = jobs[job_id]
    assert final["rendition_id"] and final["attempts"] == attempts_before
    rend = srv.sql("SELECT * FROM renditions WHERE id = ?", (final["rendition_id"],))[0]
    out = srv.root / "storage" / rend["processed_path"]
    assert out.is_file() and float(ffprobe_ok(out)["duration"]) > 30
    assert rend["processed_sha256"] == sha256_file(out)
    assert sha256_file(original) == sha_before
    assert srv.sql("SELECT COUNT(*) AS n FROM renditions WHERE media_id = ?", (media_id,))[0]["n"] == 1
    assert len(srv.sql("SELECT id FROM media")) == 4
    assert srv.temp_files() == []
    _assert_never_false_completed(srv)


def test_kill_during_downloading_recovers_and_completes(server, media_files):
    entries = [
        _entry("IMG0", "image", media_files["img0"]),
        _entry("SLOW", "image", media_files["img1"], slow=True),
        _entry("IMG2", "image", media_files["img2"]),
    ]
    srv = server(entries)
    srv.start()
    batch_id = srv.import_profile(process=True)
    slow = _wait(
        lambda: next((r for r in srv.sql("SELECT * FROM jobs WHERE batch_id = ? AND source_post_id = 'SLOW'",
                                         (batch_id,)) if r["status"] == "downloading"), None),
        what="job lento em downloading",
    )
    job_id = slow["id"]
    _wait(lambda: any(p.name == "download.part" for p in srv.temp_files()), what="parcial do download")
    srv.kill()

    assert srv.job(job_id)["status"] == "downloading"
    assert (srv.root / "storage" / "temp" / f"job-{job_id}" / "download.part").exists()
    assert srv.job(job_id)["media_id"] is None

    srv.start(worker=False)
    row = srv.job(job_id)
    assert row["status"] == "pending" and row["attempts"] == 0 and row["error_code"] == "interrupted"
    assert row["media_id"] is None
    assert srv.temp_files() == []
    _assert_never_false_completed(srv)
    srv.kill()

    (srv.root / "release").write_text("go")
    srv.start()
    result = srv.wait_batch(batch_id)
    assert [j["status"] for j in result["jobs"]] == ["completed"] * 3
    assert result["progress"] == {"found": 3, "downloaded": 3, "processed": 3, "skipped": 0, "failed": 0,
                                  "pending": 0, "active": 0}
    assert srv.temp_files() == []
    _assert_never_false_completed(srv)


def test_kill_during_listing_resumes_without_duplicates(server, media_files):
    entries = [_entry(f"L{i}", "image", media_files[f"img{i}"]) for i in range(4)]
    srv = server(entries)
    srv.start(worker=False)  # sem import runner: o lote fica em listing
    batch_id = srv.import_profile(process=False)
    assert srv.batch(batch_id)["status"] == "listing"
    srv.kill()
    srv.start()
    result = srv.wait_batch(batch_id)
    assert result["found"] == 4 and [j["status"] for j in result["jobs"]] == ["completed"] * 4
    assert len(srv.sql("SELECT id FROM media")) == 4


def test_partially_failed_batch_of_10_via_real_server(server, media_files, tmp_path):
    # lote semente: cria SEED1/SEED2 já processados (para duplicidade por source id e por sha256)
    seed = [
        _entry("SEED1", "image", media_files["img0"], profile="seed"),
        _entry("SEED2", "image", media_files["img1"], profile="seed"),
    ]
    srv = server(seed)
    srv.start()
    seed_batch = srv.wait_batch(srv.import_profile("seed", process=True))
    assert seed_batch["progress"]["processed"] == 2
    seed_media = srv.sql("SELECT id, original_sha256 FROM media ORDER BY id")
    seed_originals = {r["id"]: r["original_sha256"] for r in seed_media}
    seed_renditions = len(srv.sql("SELECT id FROM renditions"))

    bad = tmp_path / "invalido.mp4"
    bad.write_bytes(b"isto nao e um video" * 50)
    entries = [_entry(f"N{i}", "image", media_files[f"img{i + 2}"]) for i in range(6)] + [
        _entry("FAILPERM", "image", media_files["img8"], fail=True),
        _entry("BADFILE", "image", bad),
        _entry("SEED1", "image", media_files["img0"]),  # duplicado por source id (já existe, com rendition)
        _entry("SHADUP", "image", media_files["img1"]),  # source novo, conteúdo igual ao SEED2
    ]
    assert len(entries) == 10
    srv.manifest.write_text(json.dumps(entries), encoding="utf-8")

    batch_id = srv.import_profile("perfil_teste", process=True)
    result = srv.wait_batch(batch_id)
    assert result["found"] == 10 and len(result["jobs"]) == 10
    assert result["progress"] == {"found": 10, "downloaded": 6, "processed": 6, "skipped": 2, "failed": 2,
                                  "pending": 0, "active": 0}
    by_code = {j["source_post_id"]: j for j in result["jobs"]}
    assert by_code["FAILPERM"]["status"] == "failed" and by_code["FAILPERM"]["attempts"] == 1
    assert by_code["FAILPERM"]["error_code"] == "content_unavailable"
    assert by_code["BADFILE"]["status"] == "failed" and by_code["BADFILE"]["error_code"] == "invalid_media"
    assert by_code["SEED1"]["status"] == "completed" and by_code["SEED1"]["stage"] == "skipped"
    assert by_code["SEED1"]["duplicate_reason"] == "source_id" and by_code["SEED1"]["is_duplicate"]
    assert by_code["SHADUP"]["status"] == "completed" and by_code["SHADUP"]["stage"] == "skipped"
    assert by_code["SHADUP"]["duplicate_reason"] == "sha256"
    assert srv.sql("SELECT COUNT(*) AS n FROM media")[0]["n"] == 2 + 6
    assert len(srv.sql("SELECT id FROM renditions")) == seed_renditions + 6
    for media_id, sha in seed_originals.items():  # originais da semente intactos
        row = srv.sql("SELECT original_path FROM media WHERE id = ?", (media_id,))[0]
        assert sha256_file(srv.root / "storage" / row["original_path"]) == sha
    assert srv.temp_files() == []

    # corrige as duas causas e reenfileira só os falhos
    fixed = list(entries)
    fixed[6] = _entry("FAILPERM", "image", media_files["img8"])
    fixed[7] = _entry("BADFILE", "image", media_files["img9"])
    srv.manifest.write_text(json.dumps(fixed), encoding="utf-8")
    before = {j["id"]: srv.job(j["id"]) for j in result["jobs"]}
    time.sleep(1.1)  # updated_at tem resolução de segundo? garante diferença observável
    response = srv.api("POST", f"/batches/{batch_id}/retry-failed")
    assert response.status_code == 200, response.text
    retried = {by_code["FAILPERM"]["id"], by_code["BADFILE"]["id"]}
    final = srv.wait_batch(batch_id)
    assert final["progress"] == {"found": 10, "downloaded": 8, "processed": 8, "skipped": 2, "failed": 0,
                                 "pending": 0, "active": 0}
    for job_id, old in before.items():
        now = srv.job(job_id)
        if job_id in retried:
            assert now["status"] == "completed" and now["rendition_id"]
        else:
            assert dict(now) == dict(old), f"job {job_id} foi alterado pelo retry-failed"
    assert srv.api("POST", f"/batches/{batch_id}/retry-failed").status_code == 409
    completed_job = by_code["N0"]["id"]
    refused = srv.api("POST", f"/jobs/{completed_job}/retry")
    assert refused.status_code == 409 and refused.json()["code"] == "job_not_retryable"
    assert srv.job(completed_job)["status"] == "completed"
    _assert_never_false_completed(srv)
