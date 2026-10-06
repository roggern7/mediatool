"""Testes puros: opções e construção de argumentos (não executam ffmpeg)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from media_processor import MediaProcessor, ProbeResult, ProcessingOptions
from media_processor.commands import atempo_chain, plan
from media_processor.probe import parse_probe

FFMPEG = "C:/tools/ffmpeg.exe" if os.name == "nt" else "/opt/ffmpeg"


def video_raw(*, audio=True, width=640, height=360, rate="30/1", side_data=None, filename="C:/in/clip.mp4",
              channels=2, pix_fmt="yuv420p", sar="1:1"):
    v = {"index": 0, "codec_type": "video", "codec_name": "h264", "width": width, "height": height,
         "avg_frame_rate": rate, "r_frame_rate": rate, "pix_fmt": pix_fmt, "sample_aspect_ratio": sar,
         "disposition": {"attached_pic": 0}, "duration": "3.0"}
    if side_data:
        v["side_data_list"] = side_data
    streams = [v]
    if audio:
        streams.append({"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": channels})
    return {"streams": streams, "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "3.0",
                                           "filename": filename, "tags": {"title": "x"}}}


def image_raw(codec="mjpeg", fmt="image2"):
    return {"streams": [{"index": 0, "codec_type": "video", "codec_name": codec, "width": 320, "height": 240,
                         "avg_frame_rate": "25/1", "r_frame_rate": "25/1"}],
            "format": {"format_name": fmt, "duration": "0.04", "filename": "C:/in/a.jpg"}}


def build(probe: ProbeResult, opts: ProcessingOptions, src="C:/in/clip.mp4", dst="C:/out/clip.mp4"):
    return MediaProcessor(ffmpeg_path=FFMPEG).build_command(Path(src), Path(dst), probe, opts)


def arg_after(cmd, flag):
    return cmd[cmd.index(flag) + 1]


# ---------------- opções ----------------

def test_options_defaults():
    o = ProcessingOptions()
    assert (o.metadata_cleanup, o.trim_start, o.mirror, o.speed, o.auto_adjust, o.reencode) == (
        True, 0.5, True, 1.05, True, True)


def test_from_params_full_and_partial():
    o = ProcessingOptions.from_params({"metadata_cleanup": False, "trim_start": 2, "mirror": False, "speed": 1,
                                       "auto_adjust": False, "reencode": False})
    assert o == ProcessingOptions(False, 2.0, False, 1.0, False, False)
    assert ProcessingOptions.from_params({"speed": 1.5}).trim_start == 0.5


@pytest.mark.parametrize("params", [{"extra": 1}, {"speed": 0}, {"speed": -1}, {"trim_start": -0.1},
                                    {"mirror": "yes"}, {"speed": "fast"}, {"trim_start": float("nan")}])
def test_from_params_rejects(params):
    with pytest.raises(ValueError):
        ProcessingOptions.from_params(params)


# ---------------- probe (parse) ----------------

def test_parse_probe_video_and_rotation():
    p = parse_probe(video_raw(side_data=[{"side_data_type": "Display Matrix", "rotation": -90}]))
    assert p.media_type == "video" and p.rotation == 90
    assert (p.width, p.height) == (360, 640)  # dimensões de exibição
    assert p.fps == 30.0 and p.has_audio and p.video_codec == "h264" and p.audio_codec == "aac"
    assert parse_probe(video_raw(rate="30000/1001")).fps == pytest.approx(29.97, abs=0.01)


def test_parse_probe_rotate_tag():
    raw = video_raw()
    raw["streams"][0]["tags"] = {"rotate": "180"}
    assert parse_probe(raw).rotation == 180


@pytest.mark.parametrize("codec,fmt", [("mjpeg", "image2"), ("png", "png_pipe"), ("webp", "webp_pipe"),
                                       ("bmp", "bmp_pipe")])
def test_parse_probe_image(codec, fmt):
    p = parse_probe(image_raw(codec, fmt))
    assert p.media_type == "image" and p.duration is None and p.fps is None and not p.has_audio


def test_parse_probe_packet_count_decides():
    raw = image_raw("webp_anim", "webp_anim")
    raw["streams"][0]["nb_read_packets"] = "19"
    raw["streams"][0]["avg_frame_rate"] = "10/1"
    raw["format"].pop("duration")
    p = parse_probe(raw)
    assert p.media_type == "video" and p.duration == pytest.approx(1.9)
    raw = image_raw()
    raw["streams"][0]["nb_read_packets"] = "1"
    assert parse_probe(raw).media_type == "image"


# ---------------- vídeo ----------------

def test_default_video_command_order_and_filters():
    cmd = build(parse_probe(video_raw()), ProcessingOptions())
    assert cmd[0] == FFMPEG
    assert "-ss" not in cmd  # trim por filtro (funciona em containers com start_time != 0, ex.: MPEG-TS)
    assert arg_after(cmd, "-filter:v") == "trim=start=0.5,hflip,setpts=(PTS-0.5/TB)/1.05,fps=30/1,setsar=1"
    assert arg_after(cmd, "-filter:a") == "atrim=start=0.5,asetpts=PTS-0.5/TB,atempo=1.05"
    for a, b in [("-c:v", "libx264"), ("-crf", "20"), ("-preset", "medium"), ("-profile:v", "high"),
                 ("-pix_fmt", "yuv420p"), ("-c:a", "aac"), ("-b:a", "160k"), ("-map_metadata", "-1"),
                 ("-map_chapters", "-1"), ("-movflags", "+faststart"), ("-f", "mp4"), ("-progress", "pipe:1")]:
        assert arg_after(cmd, a) == b
    assert "-nostats" in cmd and "+bitexact" in cmd and "-ac" not in cmd
    assert arg_after(cmd, "-flags:v") == "+bitexact" and arg_after(cmd, "-flags:a") == "+bitexact"
    assert cmd[cmd.index("-map") + 1] == "0:0" and "0:1" in cmd
    assert "-noautorotate" not in cmd


def test_no_audio_has_no_audio_args():
    cmd = build(parse_probe(video_raw(audio=False)), ProcessingOptions())
    assert "-filter:a" not in cmd and "-c:a" not in cmd and "0:1" not in cmd


def test_trim_without_speed_shifts_both_streams_equally():
    cmd = build(parse_probe(video_raw()), ProcessingOptions(trim_start=1.0, mirror=False, speed=1.0))
    assert arg_after(cmd, "-filter:v") == "trim=start=1,setpts=(PTS-1/TB),setsar=1"
    assert arg_after(cmd, "-filter:a") == "atrim=start=1,asetpts=PTS-1/TB"


def test_speed_one_and_no_trim_no_mirror():
    cmd = build(parse_probe(video_raw()), ProcessingOptions(trim_start=0, mirror=False, speed=1.0))
    assert "-ss" not in cmd and "-filter:a" not in cmd
    assert arg_after(cmd, "-filter:v") == "setsar=1"


def test_only_needed_filters_without_auto_adjust():
    cmd = build(parse_probe(video_raw()), ProcessingOptions(trim_start=0, mirror=False, speed=1.0,
                                                           auto_adjust=False))
    assert "-filter:v" not in cmd and "-noautorotate" in cmd and "-fflags" in cmd  # -fflags +bitexact (saída)
    assert "+genpts" not in cmd


def test_atempo_chain():
    assert atempo_chain(1.05) == ["atempo=1.05"]
    assert atempo_chain(3.0) == ["atempo=2", "atempo=1.5"]
    assert atempo_chain(0.3) == ["atempo=0.5", "atempo=0.6"]
    assert atempo_chain(2.0) == ["atempo=2"] and atempo_chain(0.5) == ["atempo=0.5"]


def test_fps_not_inflated_and_capped():
    cmd = build(parse_probe(video_raw(rate="120/1")), ProcessingOptions(speed=1.5))
    assert "fps=60" in arg_after(cmd, "-filter:v")
    cmd = build(parse_probe(video_raw(rate="30000/1001")), ProcessingOptions(speed=0.8))
    assert "setpts=(PTS-0.5/TB)/0.8,fps=30000/1001" in arg_after(cmd, "-filter:v")


def test_odd_dimensions_cropped_to_even():
    cmd = build(parse_probe(video_raw(width=321, height=241)), ProcessingOptions(mirror=False))
    assert arg_after(cmd, "-filter:v").startswith("trim=start=0.5,crop=w=trunc(iw/2)*2:h=trunc(ih/2)*2:x=0:y=0,")


def test_non_square_sar_scaled():
    cmd = build(parse_probe(video_raw(sar="4:3")), ProcessingOptions(mirror=False))
    assert arg_after(cmd, "-filter:v").startswith("trim=start=0.5,scale=w=trunc(iw*sar/2)*2:h=trunc(ih/2)*2,")


def test_downmix_above_stereo_only():
    assert arg_after(build(parse_probe(video_raw(channels=6)), ProcessingOptions()), "-ac") == "2"
    assert "-ac" not in build(parse_probe(video_raw(channels=1)), ProcessingOptions())


def test_cleanup_off_has_no_metadata_args():
    cmd = build(parse_probe(video_raw()), ProcessingOptions(metadata_cleanup=False))
    assert "-map_metadata" not in cmd and "+bitexact" not in cmd and "-bsf:v" not in cmd


def test_reencode_false_with_filters_is_forced():
    probe = parse_probe(video_raw())
    p = plan(probe, ProcessingOptions(reencode=False))
    assert p.kind == "encode" and p.forced_reencode and p.suffix == ".mp4"
    assert "libx264" in build(probe, ProcessingOptions(reencode=False))
    # auto_adjust com algo a corrigir (rotação) também força
    rot = parse_probe(video_raw(side_data=[{"side_data_type": "Display Matrix", "rotation": 90}]))
    assert plan(rot, ProcessingOptions(trim_start=0, mirror=False, speed=1, reencode=False)).forced_reencode


def test_cleanup_only_is_stream_copy_to_mp4():
    opts = ProcessingOptions(trim_start=0, mirror=False, speed=1, reencode=False)
    for name in ("clip.mov", "clip.mkv", "clip.mp4"):
        probe = parse_probe(video_raw(filename=f"C:/in/{name}"))
        p = plan(probe, opts)
        # sempre mp4: matroska/webm gravam MuxingApp "Lavf" e mov grava vendor_id "FFMP"
        assert p.kind == "copy" and not p.forced_reencode and p.suffix == ".mp4" and p.muxer == "mp4"
        assert MediaProcessor(ffmpeg_path=FFMPEG).output_suffix(probe, opts) == ".mp4"
    cmd = build(probe, opts)
    assert arg_after(cmd, "-c") == "copy" and "-filter:v" not in cmd and "-ss" not in cmd
    assert arg_after(cmd, "-map_metadata") == "-1" and "libx264" not in cmd and arg_after(cmd, "-f") == "mp4"
    assert "-avoid_negative_ts" not in cmd and arg_after(cmd, "-movflags") == "+faststart"
    # codec que não vai para mp4 sem recodificar -> reencode forçado
    raw = video_raw(filename="C:/in/clip.webm")
    raw["streams"][0]["codec_name"] = "vp8"
    vp8 = plan(parse_probe(raw), opts)
    assert vp8.kind == "encode" and vp8.forced_reencode and vp8.suffix == ".mp4"


def test_expected_duration_for_progress():
    p = plan(parse_probe(video_raw()), ProcessingOptions(trim_start=0.5, speed=1.25))
    assert p.expected_duration == pytest.approx(2.0)


# ---------------- imagem ----------------

def test_image_jpeg_command():
    probe = parse_probe(image_raw())
    cmd = build(probe, ProcessingOptions(), src="C:/in/a.jpg", dst="C:/out/a.jpg")
    assert arg_after(cmd, "-filter:v") == "hflip"
    joined = " ".join(cmd)
    for forbidden in ("setpts", "atempo", "-ss", "trim", "libx264", "fps="):
        assert forbidden not in joined
    assert arg_after(cmd, "-c:v") == "mjpeg" and arg_after(cmd, "-q:v") == "2"
    assert arg_after(cmd, "-frames:v") == "1" and arg_after(cmd, "-f") == "image2"
    assert arg_after(cmd, "-map_metadata") == "-1"
    assert MediaProcessor(ffmpeg_path=FFMPEG).output_suffix(probe, ProcessingOptions()) == ".jpg"


@pytest.mark.parametrize("codec,fmt,suffix", [("png", "png_pipe", ".png"), ("webp", "webp_pipe", ".jpg"),
                                              ("bmp", "bmp_pipe", ".jpg")])
def test_image_output_format(codec, fmt, suffix):
    probe = parse_probe(image_raw(codec, fmt))
    assert plan(probe, ProcessingOptions()).suffix == suffix
    cmd = build(probe, ProcessingOptions(mirror=False))
    assert "-filter:v" not in cmd
    assert arg_after(cmd, "-c:v") == ("png" if suffix == ".png" else "mjpeg")


@pytest.mark.parametrize("pix_fmt,suffix", [("yuva420p", ".png"), ("argb", ".png"), ("yuv420p", ".jpg")])
def test_webp_alpha_goes_to_png(pix_fmt, suffix):
    raw = image_raw("webp", "webp_pipe")
    raw["streams"][0]["pix_fmt"] = pix_fmt
    assert plan(parse_probe(raw), ProcessingOptions()).suffix == suffix


def test_image_reencode_false_is_forced():
    p = plan(parse_probe(image_raw()), ProcessingOptions(mirror=False, reencode=False))
    assert p.kind == "image" and p.forced_reencode


# ---------------- caminhos ----------------

def test_paths_are_absolute_and_never_options(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd = build(parse_probe(video_raw()), ProcessingOptions(), src="-evil.mp4", dst="-out ação.mp4")
    src_arg = arg_after(cmd, "-i")
    assert os.path.isabs(src_arg) and not src_arg.startswith("-") and src_arg.endswith("-evil.mp4")
    assert os.path.isabs(cmd[-1]) and not cmd[-1].startswith("-") and cmd[-1].endswith("-out ação.mp4")


def test_hevc_copy_gets_hvc1_tag():
    raw = video_raw()
    raw["streams"][0]["codec_name"] = "hevc"
    opts = ProcessingOptions(trim_start=0, mirror=False, speed=1, reencode=False)
    cmd = build(parse_probe(raw), opts)
    assert arg_after(cmd, "-c") == "copy" and arg_after(cmd, "-tag:v") == "hvc1"
    assert "-tag:v" not in build(parse_probe(video_raw()), opts)
