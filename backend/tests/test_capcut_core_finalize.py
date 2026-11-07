"""Núcleo CapCut: conferência do exportado, áudio original preservado bit a bit, nome seguro, limpeza."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from app.integrations.capcut.errors import CapCutError
from app.integrations.capcut.finalize import finalize_output, safe_file_name

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg ausente")


def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)


def make_original(path: Path, seconds: float = 2.0) -> Path:
    ff("-f", "lavfi", "-i", f"testsrc=size=320x240:rate=30:duration={seconds}", "-f", "lavfi", "-i",
       f"sine=frequency=440:duration={seconds}:sample_rate=48000", "-c:v", "libx264", "-pix_fmt", "yuv420p",
       "-c:a", "aac", "-b:a", "96k", "-shortest", str(path))
    return path


def fake_capcut_export(original: Path, out: Path, seconds: float | None = None) -> Path:
    """O que o CapCut entrega: vídeo recodificado e áudio próprio (AAC 44,1 kHz)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    limit = ["-t", str(seconds)] if seconds else []
    ff("-i", str(original), "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100", "-map", "0:v", "-map", "1:a",
       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", *limit, str(out))
    return out


def audio_hash(path: Path) -> str:
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a", "-c", "copy",
                          "-f", "data", "-"], capture_output=True, check=True).stdout
    assert raw
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def media(tmp_path):
    original = make_original(tmp_path / "origem.mp4")
    videos = tmp_path / "CapCut" / "Videos"
    exported = fake_capcut_export(original, videos / "MT_1_m1.mp4")
    return original, videos, exported


def test_remux_keeps_original_audio_bit_exact_and_cleans_intermediate(media, tmp_path):
    original, videos, exported = media
    assert audio_hash(exported) != audio_hash(original)
    dest = tmp_path / "saida"
    report = finalize_output(exported, original, dest, "Meu vídeo", True, expected_short_side=240, expected_fps=30,
                             capcut_videos_dir=videos, created_by_job=True)
    assert report.output_path == dest / "Meu vídeo.mp4"
    assert report.audio_from_original is True
    assert audio_hash(report.output_path) == audio_hash(original)
    assert report.probe["sample_rate"] == 48000 and report.probe["audio_codec"] == "aac"
    assert report.probe["video_codec"] == "h264" and report.probe["width"] == 320
    assert report.warnings == []
    assert report.intermediate_deleted is True and not exported.exists()
    assert not list(dest.glob(".*part*"))


def test_never_overwrites_and_keeps_foreign_intermediate(media, tmp_path):
    original, videos, exported = media
    dest = tmp_path / "saida"
    dest.mkdir()
    (dest / "Meu vídeo.mp4").write_bytes(b"do dono")
    report = finalize_output(exported, original, dest, "Meu vídeo", True, capcut_videos_dir=videos,
                             created_by_job=False)
    assert report.output_path == dest / "Meu vídeo (2).mp4"
    assert (dest / "Meu vídeo.mp4").read_bytes() == b"do dono"
    assert exported.exists() and report.intermediate_deleted is False  # não foi criado por este job
    outside = tmp_path / "outra" / "x.mp4"
    outside.parent.mkdir()
    shutil.copy2(exported, outside)
    report = finalize_output(outside, original, dest, "Meu vídeo", True, capcut_videos_dir=videos,
                             created_by_job=True)
    assert report.output_path == dest / "Meu vídeo (3).mp4"
    assert outside.exists() and report.intermediate_deleted is False  # fora da pasta padrão do CapCut


def test_without_original_audio_keeps_capcut_audio(media, tmp_path):
    original, videos, exported = media
    expected = audio_hash(exported)
    report = finalize_output(exported, original, tmp_path / "saida", "x", False, capcut_videos_dir=videos,
                             created_by_job=True)
    assert report.audio_from_original is False
    assert audio_hash(report.output_path) == expected
    assert report.probe["sample_rate"] == 44100
    assert not exported.exists()


def test_rejects_wrong_duration_and_warns_on_resolution(media, tmp_path):
    original, videos, _exported = media
    short = fake_capcut_export(original, videos / "curto.mp4", seconds=1.0)
    with pytest.raises(CapCutError) as exc:
        finalize_output(short, original, tmp_path / "saida", "x", True)
    assert exc.value.code == "output_invalid" and exc.value.transient is True
    assert short.exists()
    report = finalize_output(_exported, original, tmp_path / "saida", "y", True, expected_short_side=1080,
                             expected_fps=60)
    assert any("Resolução" in w for w in report.warnings) and any("quadros" in w for w in report.warnings)
    with pytest.raises(CapCutError):
        finalize_output(tmp_path / "nao-existe.mp4", original, tmp_path / "saida", "z", True)


@pytest.mark.parametrize(("raw", "safe"), [("Meu vídeo", "Meu vídeo"), ('a<b>c:"d/e\\f|g?h*', "a_b_c__d_e_f_g_h_"),
                                          ("  ...  ", "video"), ("CON", "_CON"), ("x" * 300, "x" * 120),
                                          ("fim. ", "fim")])
def test_safe_file_name(raw, safe):
    assert safe_file_name(raw) == safe


def test_exports_record_only_discards_job_files_inside_capcut_videos(tmp_path):
    from app.integrations.capcut.finalize import EXPORTS_RECORD, discard_recorded_exports, record_export

    videos = tmp_path / "CapCut" / "Videos"
    videos.mkdir(parents=True)
    work = tmp_path / "work" / "7"
    work.mkdir(parents=True)
    mine = [videos / "MT_7_m3.mp4", videos / "MT_7_m3(1).mp4"]
    others = [videos / "MT_7_m31.mp4", videos / "outro.mp4", tmp_path / "fora" / "MT_7_m3.mp4"]
    for path in mine + others:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        record_export(work, "MT_7_m3", path)
    record_export(work, "MT_7_m3", mine[0])  # sem duplicar
    assert discard_recorded_exports(work, videos) == []
    assert not any(p.exists() for p in mine)
    assert all(p.exists() for p in others)
    assert not (work / EXPORTS_RECORD).exists()
    assert discard_recorded_exports(work, videos) == []  # sem registro: nada a fazer


def test_move_failure_becomes_transient_output_write_failed(media, tmp_path, monkeypatch):
    import app.integrations.capcut.finalize as fin

    original, videos, exported = media

    def locked(*_a, **_k):
        raise PermissionError("em uso")

    monkeypatch.setattr(fin.os, "replace", locked)
    with pytest.raises(CapCutError) as exc:
        finalize_output(exported, original, tmp_path / "saida", "x", False, capcut_videos_dir=videos,
                        created_by_job=False)
    assert exc.value.code == "output_write_failed" and exc.value.transient is True
    assert not list((tmp_path / "saida").glob(".*part*"))


@pytest.mark.parametrize(("raised", "code"), [
    (subprocess.TimeoutExpired(["ffmpeg"], 1), "ffmpeg_timeout"), (OSError("sumiu"), "ffmpeg_failed")])
def test_run_tool_system_errors_are_transient(monkeypatch, raised, code):
    from app.integrations.capcut import media as media_mod

    def boom(*_a, **_k):
        raise raised

    monkeypatch.setattr(media_mod.subprocess, "run", boom)
    with pytest.raises(CapCutError) as exc:
        media_mod.run_tool(["ffmpeg", "-version"])
    assert exc.value.code == code and exc.value.transient is True
