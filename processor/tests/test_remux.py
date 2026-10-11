"""Junção de fluxos separados (remux.py) com mídias SINTÉTICAS do ffmpeg local: nenhum arquivo do CDN real."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from conftest import FFMPEG, ff, requires_ffmpeg
from media_processor import FFmpegNotFoundError, MediaProcessor, ProcessingCancelledError, ProcessingFailedError, sha256_file
from media_processor import remux
from media_processor.remux import mux_streams

pytestmark = requires_ffmpeg

FRAGMENTED = ("-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-f", "mp4")


def _has_vp9() -> bool:
    if not FFMPEG:
        return False
    out = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return any(line.split()[1:2] == ["libvpx-vp9"] for line in out.splitlines() if line.strip())


requires_vp9 = pytest.mark.skipif(not _has_vp9(), reason="ffmpeg sem libvpx-vp9")


def _video(path: Path, codec: str, seconds: float = 2) -> Path:
    options = ["-b:v", "200k", "-deadline", "realtime", "-cpu-used", "8"] if codec == "libvpx-vp9" else [
        "-preset", "ultrafast"]
    ff("-f", "lavfi", "-i", f"testsrc=size=108x192:rate=30:duration={seconds}", "-c:v", codec, *options,
       "-pix_fmt", "yuv420p", "-an", *FRAGMENTED, str(path))
    return path


def _audio(path: Path, seconds: float = 2) -> Path:
    ff("-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={seconds}", "-c:a", "aac", "-vn",
       *FRAGMENTED, str(path))
    return path


@pytest.fixture(scope="module")
def streams(tmp_path_factory) -> dict[str, Path]:
    directory = tmp_path_factory.mktemp("remux")
    result = {"h264": _video(directory / "v_h264.mp4", "libx264"), "audio": _audio(directory / "a.mp4")}
    if _has_vp9():
        result["vp9"] = _video(directory / "v_vp9.mp4", "libvpx-vp9")
    return result


@pytest.fixture
def paced(monkeypatch):
    """Faz o ffmpeg REAL ler as entradas em tempo real (`-re`), para a junção de um clipe de 2 s durar ~2 s e poder
    ser interrompida no meio. O resto do comando é o de produção."""
    import media_processor.processor as processor_module

    real = processor_module.run_ffmpeg

    def slow(cmd, **kwargs):
        at = cmd.index("-i")
        return real([*cmd[:at], "-re", *cmd[at:]], **kwargs)

    monkeypatch.setattr(processor_module, "run_ffmpeg", slow)


def _leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def _check_pair(mp: MediaProcessor, streams, tmp_path, key: str, codec: str) -> None:
    first, second = tmp_path / "one.mp4", tmp_path / "two" / "other-name.mp4"
    second.parent.mkdir()
    sources = {name: sha256_file(streams[name]) for name in (key, "audio")}
    mp.mux_streams(streams[key], streams["audio"], first)
    mp.mux_streams(streams[key], streams["audio"], second)
    assert sha256_file(first) == sha256_file(second), "mesma entrada -> mesmos bytes"
    assert _leftovers(tmp_path) == ["one.mp4", "two"] and _leftovers(second.parent) == ["other-name.mp4"]
    probe = mp.probe(first)
    assert (probe.video_codec, probe.audio_codec, probe.width, probe.height) == (codec, "aac", 108, 192)
    assert probe.has_video and probe.has_audio and probe.duration == pytest.approx(2.0, abs=0.2)
    assert probe.fps == pytest.approx(30.0)
    # -c copy: os pacotes de vídeo e de áudio são os mesmos das entradas (nada foi recodificado).
    for index, source in ((0, streams[key]), (1, streams["audio"])):
        assert _packets_md5(first, index) == _packets_md5(source, 0)
    assert {name: sha256_file(streams[name]) for name in (key, "audio")} == sources, "entradas intactas"
    head = first.read_bytes()
    assert head.index(b"moov") < head.index(b"mdat"), "faststart"
    assert b"Lavf" not in head, "bitexact: sem a tag do encoder"


def _packets_md5(path: Path, stream: int) -> str:
    out = subprocess.run([FFMPEG, "-hide_banner", "-v", "error", "-i", str(path), "-map", f"0:{stream}", "-c", "copy",
                          "-f", "md5", "-"], capture_output=True, text=True, check=True)
    return out.stdout.strip()


@requires_vp9
def test_mux_vp9_and_aac_is_deterministic_and_copies_the_streams(mp, streams, tmp_path):
    _check_pair(mp, streams, tmp_path, "vp9", "vp9")


def test_mux_h264_and_aac_is_deterministic_and_copies_the_streams(mp, streams, tmp_path):
    _check_pair(mp, streams, tmp_path, "h264", "h264")


def test_command_only_copies(monkeypatch, streams, tmp_path):
    """Nenhum filtro, recodificação ou limpeza de metadados no comando."""
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        Path(cmd[-1]).write_bytes(b"x")

    import media_processor.processor as processor_module

    monkeypatch.setattr(processor_module, "run_ffmpeg", fake_run)
    mux_streams("ffmpeg", streams["h264"], streams["audio"], tmp_path / "out.mp4")
    [cmd] = seen
    assert cmd[cmd.index("-c") + 1] == "copy" and cmd.count("-i") == 2
    assert [cmd[i + 1] for i, part in enumerate(cmd) if part == "-map"] == ["0:v:0", "1:a:0"]
    assert not any(part in cmd for part in ("-vf", "-af", "-filter:v", "-filter_complex", "-c:v", "-c:a", "-ss", "-t",
                                            "-map_metadata"))
    assert cmd[-1].endswith("out.mp4.part") and (tmp_path / "out.mp4").read_bytes() == b"x"


def test_cancel_before_start_runs_nothing(mp, streams, tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ProcessingCancelledError):
        mp.mux_streams(streams["h264"], streams["audio"], tmp_path / "out.mp4", cancel=cancel)
    assert _leftovers(tmp_path) == []


def test_cancel_midway_kills_ffmpeg_and_removes_the_part(mp, streams, tmp_path, paced):
    cancel = threading.Event()
    dst = tmp_path / "out.mp4"
    part = tmp_path / "out.mp4.part"
    seen_part = threading.Event()

    def watch() -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not dst.exists():
            if part.exists():
                seen_part.set()
                cancel.set()
                return
            time.sleep(0.001)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    started = time.monotonic()
    with pytest.raises(ProcessingCancelledError):
        mp.mux_streams(streams["h264"], streams["audio"], dst, cancel=cancel)
    watcher.join(timeout=5)
    assert seen_part.is_set(), "o cancelamento chegou com o .part já sendo escrito"
    assert time.monotonic() - started < 1.9, "o ffmpeg foi morto antes de terminar os 2 s do clipe"
    assert _leftovers(tmp_path) == []


def test_timeout_fails_and_removes_the_part(mp, streams, tmp_path, paced):
    with pytest.raises(ProcessingFailedError, match="tempo limite"):
        mp.mux_streams(streams["h264"], streams["audio"], tmp_path / "out.mp4", timeout=0.3)
    assert _leftovers(tmp_path) == []


def test_invalid_ffmpeg_path(streams, tmp_path):
    with pytest.raises(FFmpegNotFoundError):
        MediaProcessor(ffmpeg_path=str(tmp_path / "nao-existe" / "ffmpeg.exe")).mux_streams(
            streams["h264"], streams["audio"], tmp_path / "out.mp4")
    with pytest.raises(FFmpegNotFoundError):
        mux_streams(str(tmp_path / "nao-existe" / "ffmpeg.exe"), streams["h264"], streams["audio"],
                    tmp_path / "out.mp4")
    assert _leftovers(tmp_path) == []


def test_invalid_input_fails_without_output(mp, streams, tmp_path):
    garbage = tmp_path / "lixo.mp4"
    garbage.write_bytes(b"isto nao e um mp4")
    with pytest.raises(ProcessingFailedError):
        mp.mux_streams(garbage, streams["audio"], tmp_path / "out.mp4")
    with pytest.raises(ProcessingFailedError):   # arquivo de áudio sem stream de áudio
        mp.mux_streams(streams["h264"], streams["h264"], tmp_path / "out.mp4")
    assert _leftovers(tmp_path) == ["lixo.mp4"]


def test_default_timeout():
    assert remux.MUX_TIMEOUT == 300.0
