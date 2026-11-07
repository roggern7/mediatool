"""Lote das Legendas CapCut no modo só edição com FFmpeg real (API, worker, executor e disco), com vídeo sintético
curto: a saída copiada para a pasta do lote tem a duração esperada do início do corte, da velocidade e do limite."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from media_processor import MediaProcessor

from app.jobs.capcut_runner import CapCutRunner

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                       reason="FFmpeg/ffprobe não estão no PATH"),
]


def _clip(path: Path, secs: float) -> Path:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc2=size=160x120:rate=30:duration={secs}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={secs}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                    str(path)], check=True, timeout=120)
    return path


def _duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", str(path)],
                         check=True, capture_output=True, text=True, timeout=60).stdout
    return float(json.loads(out)["format"]["duration"])


def test_edit_only_batch_applies_start_speed_and_limit(harness_factory, tmp_path):
    harness = harness_factory(processor=MediaProcessor())
    runner = CapCutRunner(harness.ctx)
    harness.ctx.capcut_runner = runner
    relative = "originals/test/clip.mp4"
    source = harness.ctx.storage.root / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    _clip(source, 8)
    media_id = harness.ctx.media.insert({
        "source_platform": "local", "media_type": "video", "original_filename": "clip.mp4",
        "original_path": relative, "original_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "file_size": source.stat().st_size, "role": "original"})
    out = tmp_path / "saida"

    def run(edit: dict) -> Path:
        response = harness.client.post("/api/capcut/batches", json={
            "mode": "edit", "media_ids": [media_id], "include_done": True, "edit": edit,
            "caption": {"overrides": {"output_dir": str(out)}}})
        assert response.status_code == 202, response.text
        batch = response.json()["batch"]
        harness.run_jobs()
        assert runner.run_next() is True
        [job] = harness.client.get(f"/api/capcut/batches/{batch['id']}/jobs").json()["items"]
        assert job["status"] == "completed", job
        output = Path(job["output_path"])
        assert output.parent == Path(batch["output_dir"]) and output.suffix == ".mp4"
        return output

    # 8 s, corte no início de 2 s e 2,0x: (8 - 2) / 2 = 3 s.
    faster = run({"duration_mode": "original", "speed": 2.0, "sharpen": False, "start_seconds": 2})
    assert faster.name == "clip_editado.mp4"
    assert _duration(faster) == pytest.approx(3.0, abs=0.15)
    # Limite personalizado de 1,5 s (+0,03 s de margem), 1,0x e nitidez.
    limited = run({"duration_mode": "custom", "custom_seconds": 1.5, "speed": 1.0, "sharpen": True,
                   "start_seconds": 0})
    assert limited.name == "clip_editado.mp4" and limited.parent != faster.parent
    assert _duration(limited) == pytest.approx(1.53, abs=0.1)
