"""Testes de integração com FFmpeg real."""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import pytest

from media_processor import (
    FFmpegNotFoundError,
    InvalidMediaError,
    MediaProcessor,
    ProcessingCancelledError,
    ProcessingFailedError,
    ProcessingOptions,
    ProcessorError,
    sha256_file,
)

from conftest import FFMPEG, FFPROBE, gray_frame, requires_ffmpeg, top_level_atoms

pytestmark = requires_ffmpeg

PLAIN = dict(trim_start=0.0, mirror=False, speed=1.0)
ALLOWED_FORMAT_TAGS = {"major_brand", "minor_version", "compatible_brands"}
ALLOWED_STREAM_TAGS = {"language", "handler_name"}


def leftovers(directory: Path) -> list[Path]:
    return [p for p in directory.iterdir() if ".part" in p.name]


def streams(result, kind):
    return [s for s in result.probe.raw["streams"] if s["codec_type"] == kind]


# ---------------- ferramentas ----------------

def test_tool_info_found(mp):
    for info in (mp.ffmpeg_info(), mp.ffprobe_info()):
        assert info.found and info.path and info.error is None
        assert info.version and "version" in info.version


def test_missing_ffmpeg_is_controlled(tmp_path, media):
    missing = str(tmp_path / "nao-existe" / "ffmpeg.exe")
    bad = MediaProcessor(ffmpeg_path=missing, ffprobe_path=missing)
    info = bad.ffmpeg_info()
    assert not info.found and info.error and info.path is None
    assert not bad.ffprobe_info().found
    with pytest.raises(FFmpegNotFoundError):
        bad.process(media["av"], tmp_path / "o.mp4", ProcessingOptions())
    with pytest.raises(FFmpegNotFoundError):
        bad.probe(media["av"])
    with pytest.raises(FFmpegNotFoundError):
        bad.make_thumbnail(media["av"], tmp_path / "t.jpg")
    assert not (tmp_path / "o.mp4").exists()


def test_env_var_location(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_TOOL_FFMPEG", str(tmp_path / "nope.exe"))
    monkeypatch.setenv("MEDIA_TOOL_FFPROBE", FFPROBE)
    m = MediaProcessor()
    assert not m.ffmpeg_info().found
    assert m.ffprobe_info().found
    # argumento explícito vence a variável de ambiente
    assert MediaProcessor(ffmpeg_path=FFMPEG).ffmpeg_info().found


# ---------------- probe ----------------

def test_probe_video(mp, media):
    p = mp.probe(media["av"])
    assert p.media_type == "video" and p.has_audio and p.has_video
    assert (p.width, p.height) == (640, 360) and p.fps == pytest.approx(30.0)
    assert p.duration == pytest.approx(3.0, abs=0.05)
    assert p.video_codec == "h264" and p.audio_codec == "aac" and p.rotation == 0
    assert p.tags.get("title") == "Titulo Secreto"


def test_probe_rotation_and_images(mp, media):
    r = mp.probe(media["rot90"])
    assert r.rotation in (90, 270) and (r.width, r.height) == (360, 640)
    j = mp.probe(media["exif_jpg"])
    assert j.media_type == "image" and j.rotation == 90 and (j.width, j.height) == (240, 320)
    assert mp.probe(media["png"]).media_type == "image"
    assert not mp.probe(media["noaudio"]).has_audio


def test_probe_invalid(mp, media, tmp_path):
    with pytest.raises(InvalidMediaError):
        mp.probe(media["invalid"])
    with pytest.raises(InvalidMediaError):
        mp.probe(tmp_path / "nao-existe.mp4")


# ---------------- vídeo ----------------

def test_default_process_output_format_and_hashes(mp, media, tmp_path):
    src = media["av"]
    before = sha256_file(src)
    progress: list[float] = []
    dst = tmp_path / "out.mp4"
    r = mp.process(src, dst, ProcessingOptions(), progress=progress.append)
    assert sha256_file(src) == before  # src intacto
    assert r.output_path == dst and dst.is_file() and r.size == dst.stat().st_size
    assert r.sha256 == sha256_file(dst) != before
    assert not r.forced_reencode and r.elapsed > 0 and r.command[0]
    v, a = streams(r, "video")[0], streams(r, "audio")[0]
    assert v["codec_name"] == "h264" and v["pix_fmt"] == "yuv420p" and v["profile"] == "High"
    assert v.get("sample_aspect_ratio", "1:1") in ("1:1", "N/A")
    assert a["codec_name"] == "aac"
    atoms = top_level_atoms(dst)
    assert atoms.index("moov") < atoms.index("mdat")  # faststart
    assert r.probe.duration == pytest.approx((3.0 - 0.5) / 1.05, abs=0.1)
    assert float(v["avg_frame_rate"].split("/")[0]) / float(v["avg_frame_rate"].split("/")[1]) == pytest.approx(30)
    # progresso monotônico até 1.0
    assert progress and progress[-1] == 1.0
    assert all(0.0 <= x <= 1.0 for x in progress)
    assert all(b >= a_ for a_, b in zip(progress, progress[1:]))
    assert not leftovers(tmp_path)


def test_metadata_cleanup(mp, media, tmp_path):
    r = mp.process(media["av"], tmp_path / "clean.mp4", ProcessingOptions())
    assert set(r.probe.tags) <= ALLOWED_FORMAT_TAGS
    for tags in r.probe.stream_tags:
        assert set(tags) <= ALLOWED_STREAM_TAGS
        assert tags.get("handler_name") in (None, "VideoHandler", "SoundHandler")
    data = (tmp_path / "clean.mp4").read_bytes()
    for needle in (b"Titulo Secreto", b"Comentario Secreto", b"EncoderSecreto", b"HandlerSecreto", b"+12.3456",
                   b"x264", b"Lavf", b"Lavc"):
        assert needle not in data, needle
    # controle: sem cleanup o título passa
    r2 = mp.process(media["av"], tmp_path / "dirty.mp4", ProcessingOptions(metadata_cleanup=False))
    assert r2.probe.tags.get("title") == "Titulo Secreto"


@pytest.mark.parametrize("trim", [0.0, 0.2, 0.5, 1.0, 2.0])
def test_trim(mp, media, tmp_path, trim):
    r = mp.process(media["av"], tmp_path / "t.mp4", ProcessingOptions(trim_start=trim, mirror=False, speed=1.0))
    assert r.probe.duration == pytest.approx(3.0 - trim, abs=0.1)


@pytest.mark.parametrize("speed", [0.8, 1.25, 2.0])
def test_speed(mp, media, tmp_path, speed):
    r = mp.process(media["av"], tmp_path / "s.mp4", ProcessingOptions(trim_start=0.0, mirror=False, speed=speed))
    assert r.probe.duration == pytest.approx(3.0 / speed, abs=0.1)
    fps = r.probe.fps
    assert fps == pytest.approx(30.0, abs=0.5)  # fps não inflado por setpts


@pytest.mark.parametrize("opts", [dict(), dict(trim_start=1.0, speed=1.5), dict(trim_start=0.2, speed=0.75)])
def test_av_sync(mp, media, tmp_path, opts):
    r = mp.process(media["av"], tmp_path / "sync.mp4", ProcessingOptions(**opts))
    v, a = streams(r, "video")[0], streams(r, "audio")[0]
    assert abs(float(v["duration"]) - float(a["duration"])) < 0.1
    assert abs(float(v.get("start_time", 0)) - float(a.get("start_time", 0))) < 0.05


def test_mirror_video(mp, media, tmp_path):
    src = media["gradient"]
    r = mp.process(src, tmp_path / "m.mp4", ProcessingOptions(mirror=True, trim_start=0.0, speed=1.0))
    w, h = 64, 48
    before, after = gray_frame(src, w, h), gray_frame(r.output_path, w, h)
    row = h // 2
    src_row = before[row * w:(row + 1) * w]
    out_row = after[row * w:(row + 1) * w]
    assert src_row[0] < src_row[-1]  # entrada: escuro -> claro
    assert out_row[0] > out_row[-1]  # saída: claro -> escuro
    assert max(abs(x - y) for x, y in zip(out_row, reversed(src_row))) <= 12


def test_video_without_audio(mp, media, tmp_path):
    r = mp.process(media["noaudio"], tmp_path / "na.mp4", ProcessingOptions())
    assert not r.probe.has_audio and r.probe.duration == pytest.approx((2.0 - 0.5) / 1.05, abs=0.1)


def test_rotation_applied(mp, media, tmp_path):
    r = mp.process(media["rot90"], tmp_path / "r.mp4", ProcessingOptions(mirror=False))
    v = streams(r, "video")[0]
    assert (v["width"], v["height"]) == (360, 640)
    assert r.probe.rotation == 0 and not v.get("side_data_list")


def test_odd_dimensions(mp, media, tmp_path):
    r = mp.process(media["odd"], tmp_path / "odd.mp4", ProcessingOptions())
    v = streams(r, "video")[0]
    assert (v["width"], v["height"]) == (320, 240) and v["pix_fmt"] == "yuv420p"
    assert v.get("sample_aspect_ratio", "1:1") in ("1:1", "N/A")


def test_reencode_false_forced(mp, media, tmp_path):
    r = mp.process(media["av"], tmp_path / "f.mp4", ProcessingOptions(reencode=False))
    assert r.forced_reencode and "libx264" in r.command
    assert r.probe.duration == pytest.approx((3.0 - 0.5) / 1.05, abs=0.1)


def test_stream_copy_cleanup_only(mp, media, tmp_path):
    opts = ProcessingOptions(reencode=False, **PLAIN)
    assert mp.output_suffix(mp.probe(media["av"]), opts) == ".mp4"
    r = mp.process(media["av"], tmp_path / "c.mp4", opts)
    assert not r.forced_reencode and r.command[r.command.index("-c") + 1] == "copy"
    assert "libx264" not in r.command
    assert r.probe.duration == pytest.approx(3.0, abs=0.05) and r.probe.has_audio
    assert set(r.probe.tags) <= ALLOWED_FORMAT_TAGS
    data = r.output_path.read_bytes()
    assert b"Titulo Secreto" not in data and b"EncoderSecreto" not in data
    atoms = top_level_atoms(r.output_path)
    assert atoms.index("moov") < atoms.index("mdat")


def test_weird_filename(mp, media, tmp_path):
    out = tmp_path / "-saída ç 日本 out.mp4"
    r = mp.process(media["weird"], out, ProcessingOptions())
    assert out.is_file() and r.probe.media_type == "video"
    assert media["weird"].is_file()


def test_invalid_input(mp, media, tmp_path):
    with pytest.raises(InvalidMediaError):
        mp.process(media["invalid"], tmp_path / "x.mp4", ProcessingOptions())
    with pytest.raises(InvalidMediaError):
        mp.process(tmp_path / "nao-existe.mp4", tmp_path / "y.mp4", ProcessingOptions())
    with pytest.raises(InvalidMediaError):
        mp.process(tmp_path, tmp_path / "z.mp4", ProcessingOptions())
    assert list(tmp_path.iterdir()) == []


def test_same_src_and_dst(mp, media):
    with pytest.raises(ProcessorError):
        mp.process(media["av"], media["av"], ProcessingOptions())


def test_trim_beyond_duration(mp, media, tmp_path):
    with pytest.raises(InvalidMediaError):
        mp.process(media["av"], tmp_path / "x.mp4", ProcessingOptions(trim_start=3.0))
    assert list(tmp_path.iterdir()) == []


def test_cancel_midway(mp, media, tmp_path):
    cancel = threading.Event()
    seen: list[float] = []

    def on_progress(value: float) -> None:
        seen.append(value)
        if value >= 0.05:
            cancel.set()

    src = media["long"]
    before = sha256_file(src)
    with pytest.raises(ProcessingCancelledError):
        mp.process(src, tmp_path / "long.mp4", ProcessingOptions(), progress=on_progress, cancel=cancel)
    assert seen and max(seen) < 1.0  # cancelado no meio, não no fim
    assert all(b >= a for a, b in zip(seen, seen[1:]))
    assert list(tmp_path.iterdir()) == []  # sem saída e sem temporário
    assert sha256_file(src) == before


def test_cancel_before_start(mp, media, tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ProcessingCancelledError):
        mp.process(media["av"], tmp_path / "x.mp4", ProcessingOptions(), cancel=cancel)
    assert list(tmp_path.iterdir()) == []


def test_timeout(mp, media, tmp_path):
    with pytest.raises(ProcessingFailedError) as exc:
        mp.process(media["long"], tmp_path / "x.mp4", ProcessingOptions(), timeout=0.5)
    assert "tempo limite" in str(exc.value)
    assert list(tmp_path.iterdir()) == []


# ---------------- imagem ----------------

def test_image_jpeg_exif(mp, media, tmp_path):
    src = media["exif_jpg"]
    before = sha256_file(src)
    progress: list[float] = []
    dst = tmp_path / "img.jpg"
    r = mp.process(src, dst, ProcessingOptions(), progress=progress.append)
    assert sha256_file(src) == before and progress == [1.0]
    assert r.probe.media_type == "image" and r.probe.video_codec == "mjpeg"
    assert (r.probe.width, r.probe.height) == (240, 320)  # orientação EXIF 6 aplicada
    assert r.probe.rotation == 0
    data = dst.read_bytes()
    assert b"Exif" not in data and b"EvilCam" not in data and b"Lavc" not in data


def test_image_jpeg_dimensions_and_mirror(mp, media, tmp_path):
    r = mp.process(media["exif_jpg_upright"], tmp_path / "up.jpg", ProcessingOptions())
    assert (r.probe.width, r.probe.height) == (320, 240)
    w, h = 32, 24
    src_px = gray_frame(media["exif_jpg_upright"], w, h)
    out_px = gray_frame(r.output_path, w, h)
    row = h // 2
    s = src_px[row * w:(row + 1) * w]
    o = out_px[row * w:(row + 1) * w]
    assert sum(abs(x - y) for x, y in zip(o, reversed(s))) < sum(abs(x - y) for x, y in zip(o, s))


def test_image_png_mirror_exact(mp, media, tmp_path):
    r = mp.process(media["png"], tmp_path / "g.png", ProcessingOptions())
    assert r.output_path.suffix == ".png" and r.probe.video_codec == "png"
    assert (r.probe.width, r.probe.height) == (64, 48)
    w, h = 64, 48
    s, o = gray_frame(media["png"], w, h), gray_frame(r.output_path, w, h)
    for row in range(h):
        assert o[row * w:(row + 1) * w] == bytes(reversed(s[row * w:(row + 1) * w]))


def test_image_cleanup_only_without_reencode(mp, media, tmp_path):
    r = mp.process(media["exif_jpg"], tmp_path / "c.jpg", ProcessingOptions(reencode=False, **PLAIN))
    assert r.forced_reencode
    data = r.output_path.read_bytes()
    assert b"Exif" not in data and b"EvilCam" not in data
    assert (r.probe.width, r.probe.height) == (240, 320)


# ---------------- miniatura e hash ----------------

def test_thumbnail_video_and_image(mp, media, tmp_path):
    t = mp.make_thumbnail(media["av"], tmp_path / "thumbs" / "v.jpg")
    p = mp.probe(t)
    assert p.media_type == "image" and (p.width, p.height) == (320, 180)
    t2 = mp.make_thumbnail(media["exif_jpg"], tmp_path / "thumbs" / "i.jpg")
    p2 = mp.probe(t2)
    assert p2.width == 320 and p2.height in (426, 427, 428)
    t3 = mp.make_thumbnail(media["gradient"], tmp_path / "thumbs" / "short.jpg")
    assert mp.probe(t3).width == 320
    assert not leftovers(tmp_path / "thumbs")


def test_sha256_file(tmp_path):
    f = tmp_path / "x.bin"
    payload = b"abc" * 1_000_000
    f.write_bytes(payload)
    assert sha256_file(f) == hashlib.sha256(payload).hexdigest()


def test_ffmpeg_failure_includes_stderr_summary(tmp_path):
    from media_processor.processor import run_ffmpeg

    with pytest.raises(ProcessingFailedError) as exc:
        run_ffmpeg([FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-i", str(tmp_path / "nada.mp4"),
                    "-f", "null", "-"])
    assert exc.value.returncode != 0 and exc.value.stderr
    assert "nada.mp4" in str(exc.value)


# ---------------- correções da revisão cruzada ----------------

def test_replace_failure_is_wrapped_and_temp_removed(mp, media, tmp_path, monkeypatch):
    from media_processor import processor as proc_mod

    def deny(_src, _dst):
        raise PermissionError(13, "Acesso negado")

    monkeypatch.setattr(proc_mod.os, "replace", deny)
    with pytest.raises(ProcessingFailedError) as exc:
        mp.process(media["av"], tmp_path / "o.mp4", ProcessingOptions())
    assert exc.value.code == "processing_failed" and "gravar o arquivo de saída" in str(exc.value)
    assert list(tmp_path.iterdir()) == []


def test_trim_leaving_no_useful_duration(mp, media, tmp_path):
    with pytest.raises(InvalidMediaError) as exc:
        mp.process(media["av"], tmp_path / "o.mp4", ProcessingOptions(trim_start=2.95))
    assert "O corte inicial deixa o vídeo sem duração útil" in str(exc.value)
    assert list(tmp_path.iterdir()) == []


def test_mpegts_trim_keeps_sync(mp, tmp_path):
    from conftest import ff

    src = tmp_path / "in.ts"
    ff("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=3", "-f", "lavfi", "-i", "sine=duration=3",
       "-c:v", "libx264", "-c:a", "aac", "-f", "mpegts", str(src))
    assert mp.probe(src).raw["format"].get("start_time") not in (None, "0.000000")
    r = mp.process(src, tmp_path / "o.mp4", ProcessingOptions(trim_start=1.0, speed=1.25))
    v, a = streams(r, "video")[0], streams(r, "audio")[0]
    assert float(v["duration"]) == pytest.approx(2.0 / 1.25, abs=0.1)
    assert abs(float(v["duration"]) - float(a["duration"])) < 0.1
    assert abs(float(v.get("start_time", 0)) - float(a.get("start_time", 0))) < 0.05


def test_raw_h264_processes_as_video(mp, tmp_path):
    from conftest import ff

    src = tmp_path / "x.h264"
    ff("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=3", "-c:v", "libx264", "-f", "h264", str(src))
    p = mp.probe(src)
    assert p.media_type == "video" and p.duration and p.duration > 1
    r = mp.process(src, tmp_path / ("o" + mp.output_suffix(p, ProcessingOptions())), ProcessingOptions())
    assert r.probe.media_type == "video" and r.probe.duration == pytest.approx((p.duration - 0.5) / 1.05, abs=0.15)


@pytest.mark.parametrize("name,args", [
    ("short.gif", ["-f", "lavfi", "-i", "testsrc2=size=64x48:rate=10:duration=0.8"]),
    ("anim.webp", ["-f", "lavfi", "-i", "testsrc2=size=64x48:rate=10:duration=2", "-c:v", "libwebp_anim",
                   "-loop", "0"]),
])
def test_animated_images_are_videos(mp, tmp_path, name, args):
    from conftest import ff

    src = tmp_path / name
    ff(*args, str(src))
    p = mp.probe(src)
    assert p.media_type == "video" and not p.has_audio
    opts = ProcessingOptions()
    assert mp.output_suffix(p, opts) == ".mp4"
    r = mp.process(src, tmp_path / "o.mp4", opts)
    v = streams(r, "video")[0]
    assert v["codec_name"] == "h264" and v["pix_fmt"] == "yuv420p" and not streams(r, "audio")
    assert r.probe.duration == pytest.approx((p.duration - 0.5) / 1.05, abs=0.2)


def test_single_frame_gif_is_image(mp, tmp_path):
    from conftest import ff

    src = tmp_path / "one.gif"
    ff("-f", "lavfi", "-i", "testsrc2=size=64x48", "-frames:v", "1", str(src))
    assert mp.probe(src).media_type == "image"


@pytest.mark.parametrize("alpha,suffix", [(True, ".png"), (False, ".jpg")])
def test_static_webp_alpha(mp, tmp_path, alpha, suffix):
    from conftest import ff

    src = tmp_path / "s.webp"
    pix = "rgba" if alpha else "yuv420p"
    ff("-f", "lavfi", "-i", f"color=c=red@0.5:s=64x48,format={pix}", "-frames:v", "1", "-update", "1", str(src))
    p = mp.probe(src)
    assert p.media_type == "image" and mp.output_suffix(p, ProcessingOptions()) == suffix
    r = mp.process(src, tmp_path / ("o" + suffix), ProcessingOptions())
    out = streams(r, "video")[0]
    if alpha:
        assert out["codec_name"] == "png" and "a" in out["pix_fmt"]
    else:
        assert out["codec_name"] == "mjpeg"


# ---------------- env filtrado, miniatura sem sobrescrever, HEVC hvc1 ----------------

def test_filtered_env_rules(monkeypatch):
    from media_processor.tools import filtered_env

    for name in ("INSTAGRAM_SESSIONID", "instagram_user", "MY_SESSION", "Some_Cookie_File", "GH_TOKEN",
                 "APP_SECRET", "DB_PASSWORD"):
        monkeypatch.setenv(name, "segredo-falso")
    monkeypatch.setenv("MEDIA_TOOL_KEEP", "ok")
    env = {k.upper(): v for k, v in filtered_env().items()}
    for name in ("INSTAGRAM_SESSIONID", "INSTAGRAM_USER", "MY_SESSION", "SOME_COOKIE_FILE", "GH_TOKEN",
                 "APP_SECRET", "DB_PASSWORD"):
        assert name not in env
    assert env["MEDIA_TOOL_KEEP"] == "ok" and "PATH" in env
    if os.name == "nt":
        assert "SYSTEMROOT" in env and ("TEMP" in env or "TMP" in env)
    assert "segredo-falso" not in env.values()


def test_secrets_never_reach_subprocess_env(mp, media, tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setenv("INSTAGRAM_SESSIONID", "sessao-falsa-123")
    monkeypatch.setenv("MEDIA_TOOL_SESSION_COOKIE", "cookie-falso-456")
    real_popen = subprocess.Popen
    envs: list = []

    def spy(*args, **kwargs):
        envs.append(kwargs.get("env"))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)  # subprocess.run também passa por aqui
    assert MediaProcessor().ffmpeg_info().found
    r = mp.process(media["av"], tmp_path / "o.mp4", ProcessingOptions())
    assert r.probe.media_type == "video" and r.output_path.is_file()
    mp.make_thumbnail(media["av"], tmp_path / "t.jpg")
    assert len(envs) >= 5  # version, probe(s), ffmpeg, probe da saída, miniatura
    for env in envs:
        assert env is not None
        assert "sessao-falsa-123" not in env.values() and "cookie-falso-456" not in env.values()
        assert not any(k.upper().startswith("INSTAGRAM_") for k in env)
        assert any(k.upper() == "PATH" for k in env)


def test_thumbnail_does_not_overwrite(mp, media, tmp_path):
    dst = tmp_path / "t.jpg"
    dst.write_bytes(b"old")
    with pytest.raises(ProcessorError):
        mp.make_thumbnail(media["av"], dst)
    assert dst.read_bytes() == b"old" and not leftovers(tmp_path)


def test_hevc_stream_copy_tagged_hvc1(mp, tmp_path):
    from conftest import ff

    src = tmp_path / "h.mkv"
    ff("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=2", "-c:v", "libx265", "-preset", "ultrafast",
       "-x265-params", "log-level=error", str(src))
    opts = ProcessingOptions(reencode=False, **PLAIN)
    r = mp.process(src, tmp_path / ("o" + mp.output_suffix(mp.probe(src), opts)), opts)
    v = streams(r, "video")[0]
    assert not r.forced_reencode and v["codec_name"] == "hevc" and v["codec_tag_string"] == "hvc1"
