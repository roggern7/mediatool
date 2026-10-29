"""Edição com FFmpeg real pelo app inteiro (API, fila, worker, banco e disco), com vídeos sintéticos."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from media_processor import MediaProcessor, sha256_file

from media_fakes import FakeInstagramDownloader, remote

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                       reason="FFmpeg/ffprobe não estão no PATH"),
]

T = 15.03


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, timeout=120)


def _clip(path: Path, secs: float, rate: str = "30", audio: bool = True, size: str = "160x120") -> Path:
    args = ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={secs}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={secs}"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    _ff(*args, str(path))
    return path


def _probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                          str(path)], check=True, capture_output=True, text=True).stdout
    raw = json.loads(out)
    v = next(s for s in raw["streams"] if s["codec_type"] == "video")
    a = next((s for s in raw["streams"] if s["codec_type"] == "audio"), None)
    num, den = (int(x) for x in v["avg_frame_rate"].split("/"))
    return {"container": float(raw["format"]["duration"]), "video": float(v["duration"]), "fps": num / den,
            "audio": float(a["duration"]) if a else None, "width": v["width"], "height": v["height"],
            "vcodec": v["codec_name"], "acodec": a["codec_name"] if a else None}


@pytest.fixture
def app(harness_factory, tmp_path):
    d = tmp_path / "samples"
    d.mkdir()
    files = {
        "L30": _clip(d / "l30.mp4", 40),
        "L2997": _clip(d / "l2997.mp4", 40, rate="30000/1001"),
        "NOA": _clip(d / "noa.mp4", 40, audio=False),
        "S10": _clip(d / "s10.mp4", 10),
        "ODD": _clip(d / "odd.mp4", 20, size="161x121"),
    }
    downloader = FakeInstagramDownloader(
        items=[remote(k, media_type="video") for k in files],
        source_files={f"{k}:0": p for k, p in files.items()},
    )
    harness = harness_factory(downloader=downloader, processor=MediaProcessor())
    batch = harness.import_profile(process=False)
    harness.run_all()
    media = {j["source_post_id"]: j["media_id"] for j in harness.batch(batch["id"])["jobs"]}
    assert len(media) == len(files)
    return harness, media


def _edit(harness, ids, **settings) -> dict:
    body = {"media_ids": ids, "settings": {"duration_mode": "15", "custom_seconds": None, "speed": 1.0,
                                           "sharpen": False, "output_type": "mp4", **settings}}
    response = harness.client.post("/api/edits", json=body)
    assert response.status_code == 202, response.text
    harness.run_jobs()
    return harness.batch(response.json()["id"])


def _output(harness, job) -> Path:
    rendition = harness.ctx.media.get_rendition(job["rendition_id"])
    return harness.ctx.storage.resolve(rendition["processed_path"])


@pytest.mark.parametrize("speed", [1.0, 1.5])
def test_edit_cut_speed_and_sharpen(app, speed):
    harness, media = app
    ids = [media["L30"], media["L2997"], media["NOA"], media["S10"], media["ODD"]]
    originals = {i: harness.ctx.media.get(i)["original_sha256"] for i in ids}
    batch = _edit(harness, ids, speed=speed, sharpen=True)
    assert batch["status"] == "completed" and batch["preset_name"] == "Edição", batch
    jobs = {j["media_id"]: j for j in batch["jobs"]}
    assert all(j["status"] == "completed" and j["stage"] == "done" for j in jobs.values()), jobs

    for key in ("L30", "L2997", "NOA"):
        m = _probe(_output(harness, jobs[media[key]]))
        frame = 1 / m["fps"]
        assert T - frame - 0.005 <= m["video"] <= T + frame + 0.005, (key, m)
        assert T - frame - 0.005 <= m["container"] <= T + frame + 0.005, (key, m)
        if key == "NOA":
            assert m["audio"] is None
        else:
            assert abs(m["audio"] - T) <= 0.025 and abs(m["video"] - m["audio"]) <= frame + 0.025, (key, m)
        assert (m["vcodec"], m["width"], m["height"]) == ("h264", 160, 120)

    short = _probe(_output(harness, jobs[media["S10"]]))  # 10 s: nunca prolonga; a 1,5x vira 6,67 s
    assert short["video"] == pytest.approx(10 / speed, abs=1 / 30 + 0.005)
    odd = _probe(_output(harness, jobs[media["ODD"]]))  # 20 s: a 1,0x corta em 15,03; a 1,5x fica 13,33 s
    assert odd["video"] == pytest.approx(T if speed == 1.0 else 20 / 1.5, abs=1 / 30 + 0.005)
    assert (odd["width"], odd["height"]) == (160, 120)  # yuv420p: recorte de 1 px nas dimensões ímpares

    for media_id, sha in originals.items():
        path = harness.ctx.storage.resolve(harness.ctx.media.get(media_id)["original_path"])
        assert sha256_file(path) == sha  # originais intactos
    temp = harness.ctx.storage.temp
    assert not any(p.name.startswith("job-") for p in temp.iterdir())


def test_auto_output_without_cut_keeps_duration(app):
    harness, media = app
    # 10 s com limite 15 em "Automático" e sem outras edições: sem corte (o processor decide copiar os fluxos)
    batch = _edit(harness, [media["S10"]], output_type="auto")
    job = batch["jobs"][0]
    assert job["status"] == "completed", job
    out = _output(harness, job)
    original = harness.ctx.storage.resolve(harness.ctx.media.get(media["S10"])["original_path"])
    m, src = _probe(out), _probe(original)
    assert m["video"] == pytest.approx(src["video"], abs=0.001)
    # mesma configuração de novo: reaproveita a rendition
    again = _edit(harness, [media["S10"]], output_type="auto")["jobs"][0]
    assert again["stage"] == "skipped" and again["rendition_id"] == job["rendition_id"]


@pytest.fixture
def long_app(harness_factory, tmp_path):
    d = tmp_path / "samples"
    d.mkdir()
    files = {"LONG": _clip(d / "long.mp4", 130)}
    downloader = FakeInstagramDownloader(
        items=[remote(k, media_type="video") for k in files],
        source_files={f"{k}:0": p for k, p in files.items()},
    )
    harness = harness_factory(downloader=downloader, processor=MediaProcessor())
    batch = harness.import_profile(process=False)
    harness.run_all()
    media = {j["source_post_id"]: j["media_id"] for j in harness.batch(batch["id"])["jobs"]}
    return harness, media


@pytest.mark.parametrize("settings, target", [
    ({"duration_mode": "custom", "custom_seconds": 45}, 45.03),
    ({"duration_mode": "60"}, 60.03),
])
def test_edit_long_duration_modes(long_app, settings, target):
    harness, media = long_app
    batch = _edit(harness, [media["LONG"]], **settings)
    job = batch["jobs"][0]
    assert batch["status"] == "completed" and job["status"] == "completed", batch
    m = _probe(_output(harness, job))
    print(f"\nE2E {settings}: video={m['video']:.4f} audio={m['audio']} container={m['container']:.4f}")
    frame = 1 / m["fps"]
    assert target - frame - 0.005 <= m["video"] <= target + frame + 0.005, m
    assert target - frame - 0.005 <= m["container"] <= target + frame + 0.005, m
    assert abs(m["audio"] - target) <= 0.025 and abs(m["video"] - m["audio"]) <= frame + 0.025, m
