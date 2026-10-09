"""QA: Anti-duplicidade x Padrão em mídias de borda (sem áudio, rotação, dimensões ímpares, SAR, vídeo curto, imagens)."""

from __future__ import annotations

import pytest

from media_processor import InvalidMediaError, MediaProcessor, ProcessingOptions

from conftest import ff, requires_ffmpeg
from test_processing import ALLOWED_FORMAT_TAGS, ALLOWED_STREAM_TAGS

DEFAULT = ProcessingOptions()
ANTI = ProcessingOptions.from_params({
    "metadata_cleanup": True, "trim_start": 0.5, "mirror": True, "speed": 1.05, "auto_adjust": True,
    "reencode": True, "zoom": 3.0, "brightness": 0.01, "contrast": 1.02, "saturation": 1.03, "trim_end": 0.3})


def _clip(path, seconds, extra_vf=""):
    vf = f"-vf {extra_vf}".split() if extra_vf else []
    ff("-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=25:duration={seconds}",
       "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", *vf,
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path))
    return path


def _dims(r):
    return r.probe.width, r.probe.height


def _clean(r):
    assert set(r.probe.tags) <= ALLOWED_FORMAT_TAGS
    for tags in r.probe.stream_tags:
        assert set(tags) <= ALLOWED_STREAM_TAGS


@requires_ffmpeg
@pytest.mark.parametrize("name", ["av", "noaudio", "rot90", "odd"])
def test_anti_matches_default_dimensions_on_edge_videos(mp, media, tmp_path, name):
    a = mp.process(media[name], tmp_path / "padrao.mp4", DEFAULT)
    b = mp.process(media[name], tmp_path / "anti.mp4", ANTI)
    assert _dims(a) == _dims(b) and b.sha256 != a.sha256
    assert b.probe.media_type == "video" and b.probe.duration > 0
    _clean(b)
    has_audio = any(s["codec_type"] == "audio" for s in b.probe.raw["streams"])
    assert has_audio == any(s["codec_type"] == "audio" for s in a.probe.raw["streams"])
    if has_audio:
        v = next(s for s in b.probe.raw["streams"] if s["codec_type"] == "video")
        au = next(s for s in b.probe.raw["streams"] if s["codec_type"] == "audio")
        assert abs(float(v["duration"]) - float(au["duration"])) < 0.1


@requires_ffmpeg
def test_anti_on_non_square_sar(mp, tmp_path):
    src = _clip(tmp_path / "sar.mp4", 2, "setsar=4/3")
    a = mp.process(src, tmp_path / "padrao.mp4", DEFAULT)
    b = mp.process(src, tmp_path / "anti.mp4", ANTI)
    assert _dims(a) == _dims(b) and _dims(b)[0] % 2 == 0 and _dims(b)[1] % 2 == 0
    _clean(b)


@requires_ffmpeg
@pytest.mark.parametrize("seconds", [1.2, 0.7])
def test_anti_on_short_videos_ignores_trim_end(mp, tmp_path, seconds):
    src = _clip(tmp_path / "short.mp4", seconds)
    r = mp.process(src, tmp_path / "anti.mp4", ANTI)
    assert "end=" not in " ".join(r.command)
    assert r.probe.duration == pytest.approx((seconds - 0.5) / 1.05, abs=0.15)
    ref = mp.process(src, tmp_path / "padrao.mp4", DEFAULT)
    assert _dims(r) == _dims(ref)


@requires_ffmpeg
def test_very_short_video_still_fails_on_trim_start(mp, tmp_path):
    src = _clip(tmp_path / "tiny.mp4", 0.55)
    for opts in (DEFAULT, ANTI):
        with pytest.raises(InvalidMediaError) as exc:
            mp.process(src, tmp_path / "o.mp4", opts)
        assert "O corte inicial deixa o vídeo sem duração útil" in str(exc.value)
    assert not (tmp_path / "o.mp4").exists()


@requires_ffmpeg
@pytest.mark.parametrize("name,suffix", [("exif_jpg", ".jpg"), ("png", ".png")])
def test_anti_on_images(mp, media, tmp_path, name, suffix):
    a = mp.process(media[name], tmp_path / f"padrao{suffix}", DEFAULT)
    b = mp.process(media[name], tmp_path / f"anti{suffix}", ANTI)
    assert b.probe.media_type == "image" and _dims(a) == _dims(b)
    assert b.output_path.suffix == suffix
    _clean(b)
    assert b"Exif" not in b.output_path.read_bytes()[:2048] and b"EvilCam" not in b.output_path.read_bytes()
    assert b.sha256 != a.sha256
