"""Revisão cruzada: casos de borda adicionais (mídia sintética gerada em tmp_path)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading

import pytest

from media_processor import (
    InvalidMediaError,
    MediaProcessor,
    ProcessingCancelledError,
    ProcessingFailedError,
    ProcessingOptions,
    ProcessorError,
)
from media_processor import processor as proc_mod
from conftest import FFPROBE, ff, requires_ffmpeg

pytestmark = requires_ffmpeg

DEFAULT = ProcessingOptions()


@pytest.fixture(scope="module")
def mp():
    return MediaProcessor()


def probe_json(path) -> dict:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        check=True, stdout=subprocess.PIPE,
    ).stdout
    return json.loads(out)


def streams(path, kind):
    return [s for s in probe_json(path)["streams"] if s["codec_type"] == kind]


def sha(path) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def leftovers(folder):
    return [p.name for p in folder.iterdir() if ".part" in p.name]


def video(path, *, dur=3, rate=30, size="320x240", audio=None):
    args = ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={dur}"]
    if audio:
        args += ["-f", "lavfi", "-i", audio]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac"]
    ff(*args, str(path))
    return path


# ---------- entradas de tipo/estrutura incomuns ----------

def test_audio_only_rejected_clearly(mp, tmp_path):
    src = tmp_path / "a.m4a"
    ff("-f", "lavfi", "-i", "sine=duration=2", str(src))
    dst = tmp_path / "o.mp4"
    with pytest.raises(InvalidMediaError):
        mp.process(src, dst, DEFAULT)
    assert not dst.exists() and not leftovers(tmp_path)


def test_audio_longer_than_video(mp, tmp_path):
    src = tmp_path / "along.mp4"
    ff("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=2", "-f", "lavfi", "-i", "sine=duration=4",
       "-c:v", "libx264", "-c:a", "aac", str(src))
    r = mp.process(src, tmp_path / "o.mp4", DEFAULT)
    v, a = streams(r.output_path, "video")[0], streams(r.output_path, "audio")[0]
    assert float(v["duration"]) == pytest.approx((2 - 0.5) / 1.05, abs=0.1)
    assert float(a["duration"]) == pytest.approx((4 - 0.5) / 1.05, abs=0.15)
    assert float(a["start_time"]) < 0.1 and float(v["start_time"]) < 0.1


def test_audio_start_offset_preserved_relative(mp, tmp_path):
    src = tmp_path / "aoff.mp4"
    ff("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=4", "-itsoffset", "1.0", "-f", "lavfi",
       "-i", "sine=duration=3", "-c:v", "libx264", "-c:a", "aac", str(src))
    in_off = float(streams(src, "audio")[0]["start_time"])
    r = mp.process(src, tmp_path / "o.mp4", ProcessingOptions(speed=1.0, mirror=False))
    out_off = float(streams(r.output_path, "audio")[0]["start_time"])
    assert out_off == pytest.approx(in_off - 0.5, abs=0.08)


@pytest.mark.parametrize("layout,rate", [("mono", 44100), ("5.1", 48000), ("stereo", 11025), ("stereo", 96000)])
def test_audio_layouts_and_sample_rates(mp, tmp_path, layout, rate):
    src = tmp_path / "s.mp4"
    ff("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=2", "-f", "lavfi",
       "-i", f"anullsrc=cl={layout}:r={rate}", "-t", "2", "-c:v", "libx264", "-c:a", "aac", str(src))
    r = mp.process(src, tmp_path / "o.mp4", DEFAULT)
    a = streams(r.output_path, "audio")[0]
    assert a["codec_name"] == "aac" and a["channels"] <= 2
    assert float(a["duration"]) == pytest.approx(float(streams(r.output_path, "video")[0]["duration"]), abs=0.1)


@pytest.mark.parametrize("fps", [120, 240])
def test_high_fps(mp, tmp_path, fps):
    src = video(tmp_path / "hf.mp4", dur=2, rate=fps)
    r = mp.process(src, tmp_path / "o.mp4", DEFAULT)
    v = streams(r.output_path, "video")[0]
    num, den = map(int, v["avg_frame_rate"].split("/"))
    assert num / den <= 60.01
    assert float(v["duration"]) == pytest.approx((2 - 0.5) / 1.05, abs=0.1)


@pytest.mark.parametrize("rot", [180, 270])
def test_rotation_180_270(mp, tmp_path, rot):
    base = video(tmp_path / "b.mp4", dur=2, size="320x240")
    src = tmp_path / "r.mp4"
    ff("-display_rotation", str(rot), "-i", str(base), "-c", "copy", str(src))
    # -display_rotation é anti-horário; o probe reporta rotação horária de exibição
    assert mp.probe(src).rotation == {180: 180, 270: 90}[rot]
    r = mp.process(src, tmp_path / "o.mp4", DEFAULT)
    v = streams(r.output_path, "video")[0]
    assert (v["width"], v["height"]) == ((240, 320) if rot == 270 else (320, 240))
    assert not any("rotation" in sd for sd in v.get("side_data_list", []))
    assert r.probe.rotation == 0


def test_vfr_input(mp, tmp_path):
    src = tmp_path / "vfr.mp4"
    ff("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=3", "-vf",
       "select='not(mod(n,3))+gt(n,60)',setpts=N/30/TB", "-fps_mode", "vfr", "-c:v", "libx264", str(src))
    in_dur = float(probe_json(src)["format"]["duration"])
    r = mp.process(src, tmp_path / "o.mp4", DEFAULT)
    assert r.probe.duration == pytest.approx((in_dur - 0.5) / 1.05, abs=0.15)


def test_gif_long_is_video(mp, tmp_path):
    src = tmp_path / "a.gif"
    ff("-f", "lavfi", "-i", "testsrc2=size=64x64:rate=10:duration=3", str(src))
    r = mp.process(src, tmp_path / "o.mp4", DEFAULT)
    assert r.probe.media_type == "video" and r.probe.duration == pytest.approx(2.4, abs=0.2)


# ---------- limites de speed/trim ----------

@pytest.mark.parametrize("speed,expect", [(0.5, 5.0), (2.0, 1.25)])
def test_speed_limits(mp, tmp_path, speed, expect):
    src = video(tmp_path / "b.mp4", dur=3, audio="sine=duration=3")
    r = mp.process(src, tmp_path / "o.mp4", ProcessingOptions(speed=speed))
    assert r.probe.duration == pytest.approx(expect, abs=0.1)
    assert float(streams(r.output_path, "audio")[0]["duration"]) == pytest.approx(expect, abs=0.1)


def test_trim_equal_duration_rejected_without_leftovers(mp, tmp_path):
    src = video(tmp_path / "b.mp4", dur=3)
    with pytest.raises(InvalidMediaError):
        mp.process(src, tmp_path / "o.mp4", ProcessingOptions(trim_start=3.0))
    assert not (tmp_path / "o.mp4").exists() and not leftovers(tmp_path)


def test_trim_within_one_frame_of_end_is_controlled_error(mp, tmp_path):
    src = video(tmp_path / "b.mp4", dur=3)
    dst = tmp_path / "o.mp4"
    with pytest.raises(ProcessorError):
        mp.process(src, dst, ProcessingOptions(trim_start=2.99))
    assert not dst.exists() and not leftovers(tmp_path)


# ---------- imagens ----------

def test_png_alpha_preserved(mp, tmp_path):
    src = tmp_path / "a.png"
    ff("-f", "lavfi", "-i", "color=c=red@0.5:s=64x64,format=rgba", "-frames:v", "1", "-update", "1", str(src))
    r = mp.process(src, tmp_path / "o.png", DEFAULT)
    assert streams(r.output_path, "video")[0]["pix_fmt"] == "rgba"


def test_large_image_keeps_dimensions(mp, tmp_path):
    src = tmp_path / "big.jpg"
    ff("-f", "lavfi", "-i", "testsrc2=size=9000x6000", "-frames:v", "1", "-update", "1", "-q:v", "10", str(src))
    r = mp.process(src, tmp_path / "o.jpg", DEFAULT)
    assert (r.probe.width, r.probe.height) == (9000, 6000)


def test_odd_size_image_not_cropped(mp, tmp_path):
    src = tmp_path / "odd.jpg"
    ff("-f", "lavfi", "-i", "color=c=red:s=101x51", "-pix_fmt", "yuvj444p", "-frames:v", "1", "-update", "1", str(src))
    assert (mp.probe(src).width, mp.probe(src).height) == (101, 51)
    r = mp.process(src, tmp_path / "o.jpg", DEFAULT)
    assert (r.probe.width, r.probe.height) == (101, 51)


# ---------- nomes de arquivo / caminhos ----------

@pytest.mark.parametrize("name", ["a%d.mp4", "100%.mp4", "-rf.mp4", "x y  z.mp4", "ação 日本.mp4", "a;b&c.mp4",
                                  "$(calc).mp4", "--version.mp4"])
def test_hostile_video_names(mp, tmp_path, name):
    base = video(tmp_path / "base.mp4", dur=2)
    src = tmp_path / name
    src.write_bytes(base.read_bytes())
    before = sha(src)
    r = mp.process(src, tmp_path / ("out_" + name), DEFAULT)
    assert r.output_path.is_file() and sha(src) == before
    assert not leftovers(tmp_path)


@pytest.mark.parametrize("name", ["im%d.jpg", "im%03d.jpg", "100%.jpg", "-i.jpg", "ação 日本.jpg", "a[1].jpg"])
def test_hostile_image_names(mp, tmp_path, name):
    base = tmp_path / "base.jpg"
    ff("-f", "lavfi", "-i", "testsrc2=size=100x50", "-frames:v", "1", "-update", "1", str(base))
    src = tmp_path / name
    src.write_bytes(base.read_bytes())
    r = mp.process(src, tmp_path / ("o_" + name), DEFAULT)
    assert r.output_path.is_file() and r.probe.media_type == "image"
    assert mp.make_thumbnail(src, tmp_path / ("t_" + name)).is_file()


def test_relative_paths_and_dash_names(mp, tmp_path, monkeypatch):
    base = video(tmp_path / "base.mp4", dur=2)
    (tmp_path / "-dash.mp4").write_bytes(base.read_bytes())
    monkeypatch.chdir(tmp_path)
    r = mp.process("-dash.mp4", "-out.mp4", DEFAULT)
    assert (tmp_path / "-out.mp4").is_file() and r.output_path == tmp_path / "-out.mp4"


@pytest.mark.skipif(sys.platform != "win32", reason="UNC/maiúsculas só no Windows")
def test_same_file_via_case_unc_and_hardlink(mp, tmp_path):
    src = video(tmp_path / "base.mp4", dur=2)
    before = sha(src)
    with pytest.raises(ProcessorError):
        mp.process(src, tmp_path / "BASE.MP4", DEFAULT)
    os.link(src, tmp_path / "hl.mp4")
    with pytest.raises(ProcessorError):
        mp.process(src, tmp_path / "hl.mp4", DEFAULT)
    unc = "\\\\localhost\\" + str(src.resolve()).replace(":", "$", 1)
    if os.path.exists(unc):
        with pytest.raises(ProcessorError):
            mp.process(unc, src, DEFAULT)
        assert mp.process(unc, tmp_path / "unc_out.mp4", DEFAULT).output_path.is_file()
    assert sha(src) == before and not leftovers(tmp_path)


def test_existing_dst_is_never_overwritten(mp, tmp_path):
    # decisão do orquestrador: process() não sobrescreve dst existente (o backend gera nomes únicos)
    src = video(tmp_path / "base.mp4", dur=2)
    dst = tmp_path / "o.mp4"
    dst.write_bytes(b"old")
    with pytest.raises(ProcessorError):
        mp.process(src, dst, DEFAULT)
    assert dst.read_bytes() == b"old" and not leftovers(tmp_path)


# ---------- processos / progresso ----------

@pytest.fixture
def spawned(monkeypatch):
    procs = []
    real = subprocess.Popen

    def spy(*a, **kw):
        p = real(*a, **kw)
        procs.append(p)
        return p

    monkeypatch.setattr(proc_mod.subprocess, "Popen", spy)
    return procs


def test_cancel_timeout_callback_error_leave_no_orphans(mp, tmp_path, spawned):
    src = video(tmp_path / "big.mp4", dur=40, size="1920x1080")
    cancel = threading.Event()
    threading.Timer(1.5, cancel.set).start()
    with pytest.raises(ProcessingCancelledError):
        mp.process(src, tmp_path / "c.mp4", DEFAULT, cancel=cancel)
    with pytest.raises(ProcessingFailedError):
        mp.process(src, tmp_path / "t.mp4", DEFAULT, timeout=1.5)

    def boom(_):
        raise RuntimeError("callback")

    with pytest.raises(RuntimeError):
        mp.process(src, tmp_path / "e.mp4", DEFAULT, progress=boom)
    encoders = [p for p in spawned if "-progress" in p.args]
    assert len(encoders) == 3 and all(p.poll() is not None for p in spawned)
    assert not leftovers(tmp_path)
    assert not [p for p in tmp_path.iterdir() if p.name in ("c.mp4", "t.mp4", "e.mp4")]


def test_progress_monotonic_and_ends_at_one(mp, tmp_path):
    src = video(tmp_path / "b.mp4", dur=6, audio="sine=duration=6")
    seen = []
    mp.process(src, tmp_path / "o.mp4", DEFAULT, progress=seen.append)
    assert seen == sorted(seen) and all(0.0 <= v <= 1.0 for v in seen) and seen[-1] == 1.0


# ---------- bugs encontrados na revisão (corrigidos) ----------

def test_bug_readonly_dst_not_wrapped(mp, tmp_path):
    src = video(tmp_path / "b.mp4", dur=2)
    dst = tmp_path / "ro.mp4"
    dst.write_bytes(b"x")
    os.chmod(dst, 0o444)
    try:
        with pytest.raises(ProcessorError):
            mp.process(src, dst, DEFAULT)
    finally:
        os.chmod(dst, 0o666)


def test_bug_mkv_copy_keeps_encoder_tag(mp, tmp_path):
    src = tmp_path / "c.mkv"
    ff("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=2", "-c:v", "libx264", "-metadata", "title=T",
       str(src))
    opts = ProcessingOptions(trim_start=0, speed=1.0, mirror=False, reencode=False)
    suffix = mp.output_suffix(mp.probe(src), opts)
    assert suffix == ".mp4"  # stream copy remuxa para mp4 (o matroska grava MuxingApp "Lavf")
    r = mp.process(src, tmp_path / ("o" + suffix), opts)
    assert "copy" in r.command
    tags = {k.lower() for k in probe_json(r.output_path)["format"].get("tags", {})}
    assert "encoder" not in tags and "title" not in tags


def test_bug_trim_on_mpegts(mp, tmp_path):
    src = tmp_path / "x.ts"
    ff("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=3", "-c:v", "libx264", "-f", "mpegts", str(src))
    r = mp.process(src, tmp_path / "o.mp4", DEFAULT)
    assert r.probe.duration == pytest.approx((3 - 0.5) / 1.05, abs=0.2)


def test_bug_raw_h264_classified_as_image(mp, tmp_path):
    src = tmp_path / "x.h264"
    ff("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=3", "-c:v", "libx264", "-f", "h264", str(src))
    assert mp.probe(src).media_type != "image"
