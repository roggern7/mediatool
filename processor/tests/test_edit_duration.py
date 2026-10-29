"""Duração alvo (max_duration) e nitidez com FFmpeg real: tabela de durações, sincronia e verificação."""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

import pytest

from media_processor import MediaProcessor, ProcessingFailedError, ProcessingOptions
from media_processor.probe import ProbeResult

from conftest import FFPROBE, ff, requires_ffmpeg

pytestmark = requires_ffmpeg

T = 15.03  # alvo de 15 s + margem de 0,03 s somada pelo backend
EDIT = dict(metadata_cleanup=False, trim_start=0.0, mirror=False, speed=1.0, auto_adjust=False, reencode=True,
            audio_sync_start=True)  # params que o backend envia na edição
RATES = {"25": "25", "30": "30", "29.97": "30000/1001", "60": "60"}


def _source(path: Path, rate: str, secs: float, audio: bool = True) -> Path:
    args = ["-f", "lavfi", "-i", f"testsrc2=size=160x120:rate={rate}:duration={secs}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={secs}"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    ff(*args, str(path))
    return path


@pytest.fixture(scope="module")
def sources(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("edit")
    s = {f"{name}": _source(d / f"s{name}.mp4", rate, 40) for name, rate in RATES.items()}
    s["noaudio"] = _source(d / "noaudio.mp4", "30", 40, audio=False)
    s["short10"] = _source(d / "short10.mp4", "30", 10)
    s["mid20"] = _source(d / "mid20.mp4", "30", 20)
    # VFR: timestamps irregulares (o mp4 pode regularizar; o teste confirma que não quebra)
    s["vfr"] = d / "vfr.mp4"
    ff("-f", "lavfi", "-i", "testsrc2=size=160x120:rate=30:duration=40,setpts='N/30/TB+0.01*sin(N)/TB'",
       "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=40", "-fps_mode", "passthrough",
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(s["vfr"]))
    return s


def measure(path: Path) -> dict:
    raw = json.loads(subprocess.run(
        [FFPROBE, "-v", "error", "-count_frames", "-print_format", "json", "-show_format", "-show_streams",
         str(path)], check=True, capture_output=True).stdout)
    v = next(st for st in raw["streams"] if st["codec_type"] == "video")
    a = next((st for st in raw["streams"] if st["codec_type"] == "audio"), None)
    num, den = (int(x) for x in v["avg_frame_rate"].split("/"))
    return {
        "container": float(raw["format"]["duration"]),
        "video": float(v["duration"]),
        "frames": int(v["nb_read_frames"]),
        "fps": num / den,
        "v_start": float(v["start_time"]),
        "audio": float(a["duration"]) if a else None,
        "a_start": float(a["start_time"]) if a else None,
    }


def run(mp: MediaProcessor, src: Path, out: Path, **opts):
    result = mp.process(src, out, ProcessingOptions(**{**EDIT, **opts}))
    assert not [p for p in out.parent.iterdir() if ".part" in p.name]
    return result, measure(out)


def assert_within(m: dict, target: float) -> None:
    frame = 1.0 / m["fps"]
    assert target - frame - 0.005 <= m["video"] <= target + frame + 0.005, m
    assert target - frame - 0.005 <= m["container"] <= target + frame + 0.005, m
    assert abs(m["v_start"]) <= 0.05, m
    if m["audio"] is not None:
        assert abs(m["audio"] - target) <= 0.025, m
        assert abs(m["video"] - m["audio"]) <= frame + 0.025, m  # sincronia
        assert abs(m["a_start"]) <= 0.05, m


@pytest.mark.parametrize("speed", [1.0, 1.25, 1.5])
@pytest.mark.parametrize("name", ["25", "30", "29.97", "60", "noaudio"])
def test_duration_table(mp, sources, tmp_path, name, speed):
    result, m = run(mp, sources[name], tmp_path / "out.mp4", speed=speed, max_duration=T)
    print(f"\nMEDIDA {name} {speed}x: container={m['container']:.4f} video={m['video']:.4f} "
          f"frames={m['frames']} audio={m['audio']} fps={m['fps']:.3f}")
    assert_within(m, T)
    if name == "noaudio":
        assert m["audio"] is None and not result.probe.has_audio  # sem áudio não cria áudio
    else:
        assert m["audio"] == pytest.approx(T, abs=0.0005)  # exato por amostra
    # vídeo quantizado por quadro: nunca passa de alvo + 1 quadro
    assert m["frames"] in (math.ceil(T * m["fps"]) - 1, math.ceil(T * m["fps"]))
    if name == "30" and speed == 1.0:
        assert m["frames"] == 451 and m["video"] == pytest.approx(15.0333, abs=0.0005)


def test_shorter_than_limit_is_unchanged(mp, sources, tmp_path):
    result, m = run(mp, sources["short10"], tmp_path / "out.mp4", max_duration=T)
    src = measure(sources["short10"])
    assert "trim=end" not in " ".join(result.command)
    assert m["video"] == pytest.approx(src["video"], abs=0.001)
    assert m["frames"] == src["frames"] == 300
    assert m["container"] == pytest.approx(src["container"], abs=0.03)


def test_speed_without_cut_when_output_fits(mp, sources, tmp_path):
    result, m = run(mp, sources["mid20"], tmp_path / "out.mp4", speed=1.5, max_duration=T)
    assert "trim=end" not in " ".join(result.command)
    assert m["video"] == pytest.approx(20 / 1.5, abs=1 / 30 + 0.005)
    assert m["audio"] == pytest.approx(20 / 1.5, abs=0.025)


def test_vfr_source_within_tolerance(mp, sources, tmp_path):
    _, m = run(mp, sources["vfr"], tmp_path / "out.mp4", max_duration=T)
    assert_within(m, T)


@pytest.mark.parametrize("speed", [1.0, 1.25])
def test_sharpen_on_off(mp, sources, tmp_path, speed):
    off, m_off = run(mp, sources["30"], tmp_path / "off.mp4", speed=speed, max_duration=T)
    on, m_on = run(mp, sources["30"], tmp_path / "on.mp4", speed=speed, max_duration=T, sharpen=True)
    vf_on = on.command[on.command.index("-filter:v") + 1]
    assert "cas=strength=0.25:planes=1" in vf_on
    assert all("cas=" not in a for a in off.command)
    assert on.sha256 != off.sha256
    assert on.probe.media_type == "video" and on.probe.video_codec == "h264" and on.probe.audio_codec == "aac"
    assert (on.probe.width, on.probe.height) == (off.probe.width, off.probe.height) == (160, 120)
    assert_within(m_on, T)


def test_sharpen_only_without_cut(mp, sources, tmp_path):
    result, m = run(mp, sources["short10"], tmp_path / "out.mp4", sharpen=True)
    assert result.command[result.command.index("-filter:v") + 1] == "cas=strength=0.25:planes=1"
    assert m["frames"] == 300


def test_duration_check_failure_removes_part(mp, sources, tmp_path, monkeypatch):
    real_probe = MediaProcessor.probe

    def bad_probe(self, path):
        result: ProbeResult = real_probe(self, path)
        if ".part" in Path(path).name:
            result.raw["format"]["duration"] = "20.0"  # saída "longa demais"
        return result

    monkeypatch.setattr(MediaProcessor, "probe", bad_probe)
    out = tmp_path / "out.mp4"
    with pytest.raises(ProcessingFailedError) as info:
        mp.process(sources["30"], out, ProcessingOptions(**EDIT, max_duration=T))
    assert "duração final fora da tolerância" in info.value.message
    assert not out.exists()
    assert not [p for p in tmp_path.iterdir() if ".part" in p.name]


def test_duration_check_skipped_without_cut(mp, sources, tmp_path, monkeypatch):
    real_probe = MediaProcessor.probe

    def odd_probe(self, path):
        result = real_probe(self, path)
        if ".part" in Path(path).name:
            result.raw["format"]["duration"] = "99.0"
        return result

    monkeypatch.setattr(MediaProcessor, "probe", odd_probe)
    out = tmp_path / "out.mp4"
    mp.process(sources["short10"], out, ProcessingOptions(**EDIT, max_duration=T))  # sem corte: sem verificação
    assert out.is_file()


# ---------------- streams com durações e inícios diferentes ----------------

EVENTS = "between(t,3,3.2)+between(t,9,9.2)+between(t,12,12.2)"


@pytest.fixture(scope="module")
def offsets(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("offsets")
    s: dict[str, Path] = {}
    # flash branco e beep nos mesmos instantes (3, 9 e 12 s) para medir a sincronia
    s["sync"] = d / "sync.mp4"
    ff("-f", "lavfi", "-i", "color=c=black:size=160x120:rate=30:duration=40",
       "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=40",
       "-filter:v", f"geq=lum='if({EVENTS.replace('t,', 'T,')},235,16)':cb=128:cr=128",
       "-filter:a", f"volume=volume='if({EVENTS},1,0)':eval=frame",
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(s["sync"]))
    s["voff"] = d / "voff.mp4"  # vídeo começa 0,2 s depois do áudio
    ff("-itsoffset", "0.2", "-i", str(s["sync"]), "-i", str(s["sync"]), "-map", "0:v", "-map", "1:a", "-c", "copy",
       str(s["voff"]))
    s["aoff"] = d / "aoff.mp4"  # áudio começa 0,2 s depois do vídeo
    ff("-i", str(s["sync"]), "-itsoffset", "0.2", "-i", str(s["sync"]), "-map", "0:v", "-map", "1:a", "-c", "copy",
       str(s["aoff"]))
    s["aoff5"] = d / "aoff5.mp4"  # áudio começa 0,5 s depois do vídeo
    ff("-i", str(s["sync"]), "-itsoffset", "0.5", "-i", str(s["sync"]), "-map", "0:v", "-map", "1:a", "-c", "copy",
       str(s["aoff5"]))
    s["ts"] = d / "sync.ts"  # MPEG-TS com start_time alto
    ff("-i", str(s["sync"]), "-c", "copy", "-output_ts_offset", "1.4", "-f", "mpegts", str(s["ts"]))
    s["ashort"] = _mixed(d / "ashort.mp4", 15.5, 15.0)
    s["vshort"] = _mixed(d / "vshort.mp4", 15.0, 15.5)
    return s


def _mixed(path: Path, vsecs: float, asecs: float) -> Path:
    ff("-f", "lavfi", "-i", f"testsrc2=size=160x120:rate=30:duration={vsecs}",
       "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={asecs}",
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path))
    return path


def onsets(path: Path) -> tuple[list[float], list[float]]:
    """Instantes (linha do tempo de apresentação) em que o flash começa e o beep começa."""
    import re

    def log(*args):
        return subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), *args, "-f", "null", "-"],
                              capture_output=True, text=True).stderr
    v = log("-vf", "blackdetect=d=0.05:pix_th=0.5", "-an")
    a = log("-af", "silencedetect=n=-30dB:d=0.05", "-vn")
    return ([float(x) for x in re.findall(r"black_end:([\d.]+)", v)][:3],
            [float(x) for x in re.findall(r"silence_end: ([\d.]+)", a)][:3])


def stream_spans(path: Path) -> dict[str, tuple[float, float]]:
    raw = json.loads(subprocess.run([FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", str(path)],
                                    check=True, capture_output=True).stdout)
    return {s["codec_type"]: (float(s["start_time"]), float(s["start_time"]) + float(s["duration"]))
            for s in raw["streams"]}


def deltas(path: Path) -> list[float]:
    v, a = onsets(path)
    assert len(v) == len(a) == 3, (v, a)
    return [x - y for x, y in zip(v, a)]


@pytest.mark.parametrize("name, v_end, a_end", [("ashort", T, 15.0), ("vshort", 15.0, T)])
def test_shorter_stream_is_not_extended(mp, offsets, tmp_path, name, v_end, a_end):
    out = tmp_path / "out.mp4"
    mp.process(offsets[name], out, ProcessingOptions(**EDIT, max_duration=T))
    spans = stream_spans(out)
    assert spans["video"][1] == pytest.approx(v_end, abs=1 / 30 + 0.005), spans
    assert spans["audio"][1] == pytest.approx(a_end, abs=0.025), spans


@pytest.mark.parametrize("speed", [1.0, 1.25, 1.5])
def test_video_start_offset_keeps_sync(mp, offsets, tmp_path, speed):
    out = tmp_path / "out.mp4"
    mp.process(offsets["voff"], out, ProcessingOptions(**{**EDIT, "speed": speed}, max_duration=T))
    spans = stream_spans(out)
    assert spans["video"][0] == pytest.approx(0.2 / speed, abs=1 / 30)  # o vídeo não é "puxado" para 0
    assert spans["video"][1] == pytest.approx(T, abs=1 / 30 + 0.005)
    assert spans["audio"][1] == pytest.approx(T, abs=0.025)
    src, got = deltas(offsets["voff"]), deltas(out)
    for before, after in zip(src, got):
        assert after == pytest.approx(before / speed, abs=1 / 30 + 0.01), (src, got)


@pytest.mark.parametrize("name", ["aoff", "aoff5"])
def test_audio_start_offset_keeps_sync(mp, offsets, tmp_path, name):
    out = tmp_path / "out.mp4"
    mp.process(offsets[name], out, ProcessingOptions(**EDIT, max_duration=T))
    spans = stream_spans(out)
    assert spans["video"] == pytest.approx((0.0, 15.0333), abs=0.001)
    assert spans["audio"][1] == pytest.approx(T, abs=0.025)
    for before, after in zip(deltas(offsets[name]), deltas(out)):
        assert after == pytest.approx(before, abs=0.01)


@pytest.mark.parametrize("name", ["aoff", "aoff5"])
@pytest.mark.parametrize("speed", [1.25, 1.5])
@pytest.mark.parametrize("cut", [T, 0.0])
def test_audio_start_offset_with_speed_keeps_sync(mp, offsets, tmp_path, name, speed, cut):
    # o atempo mantém o PTS inicial; com audio_sync_start o início do áudio também é dividido pela velocidade
    out = tmp_path / "out.mp4"
    result = mp.process(offsets[name], out, ProcessingOptions(**{**EDIT, "speed": speed}, max_duration=cut))
    assert f"asetpts=PTS-STARTPTS+STARTPTS/{speed:g}" in result.command[result.command.index("-filter:a") + 1]
    # início do áudio = início na origem / velocidade (a origem já traz o desconto do priming AAC, ~21 ms)
    assert stream_spans(out)["audio"][0] == pytest.approx(stream_spans(offsets[name])["audio"][0] / speed, abs=0.03)
    for before, after in zip(deltas(offsets[name]), deltas(out)):
        # medido: até 15 ms (o mesmo resíduo do atempo que existe sem deslocamento nenhum)
        assert after == pytest.approx(before / speed, abs=0.025)


def test_audio_start_offset_without_sync_param_keeps_old_behavior(mp, offsets, tmp_path):
    # presets (audio_sync_start desligado): comando de antes, sem asetpts depois do atempo
    out = tmp_path / "out.mp4"
    result = mp.process(offsets["aoff5"], out, ProcessingOptions(**{**EDIT, "speed": 1.5, "audio_sync_start": False}))
    assert "asetpts" not in " ".join(result.command)


@pytest.mark.parametrize("speed", [1.0, 1.5])
def test_mpegts_high_start_time(mp, offsets, tmp_path, speed):
    out = tmp_path / "out.mp4"
    result = mp.process(offsets["ts"], out, ProcessingOptions(**{**EDIT, "speed": speed}, max_duration=T))
    assert result.command[result.command.index("-enc_time_base:v") + 1] == "filter"
    m = measure(out)
    assert m["container"] <= T + 1 / 30, m  # antes: 15,067 (PTS do vídeo arredondado pelo encoder)
    assert_within_end(stream_spans(out), T)
    # sincronia igual à do conteúdo original (o TS é um remux de sync.mp4)
    for before, after in zip(deltas(offsets["sync"]), deltas(out)):
        assert after == pytest.approx(before / speed, abs=0.015)


def assert_within_end(spans: dict, target: float) -> None:
    assert target - 1 / 30 - 0.005 <= spans["video"][1] <= target + 1 / 30 + 0.005, spans
    assert abs(spans["audio"][1] - target) <= 0.025, spans


# ---------------- alvos longos (60/45/90/120 s + margem) com os params do edit_service ----------------

LONG_SECS = 190  # 120,03 s a 1,5x exige >= 180,05 s de entrada
LONG_TARGETS = [60.03, 45.03, 90.03, 120.03]  # "60" e personalizados 45/90/120, já com a margem de 0,03 s


@pytest.fixture(scope="module")
def long_sources(tmp_path_factory):
    """Uma fonte longa por fps, criada na primeira vez que for pedida e reaproveitada no módulo."""
    d = tmp_path_factory.mktemp("long")
    cache: dict[str, Path] = {}

    def get(key: str) -> Path:
        if key not in cache:
            if key == "noaudio":
                cache[key] = _source(d / "long_noaudio.mp4", "30", LONG_SECS, audio=False)
            elif key == "short50":
                cache[key] = _source(d / "short50.mp4", "30", 50)
            else:
                cache[key] = _source(d / f"long{key}.mp4", RATES[key], LONG_SECS)
        return cache[key]

    return get


def _check_long(m: dict, target: float, label: str) -> None:
    print(f"\nDUR {label}: alvo={target} video={m['video']:.4f} audio={m['audio']} "
          f"container={m['container']:.4f} frames={m['frames']} fps={m['fps']:.3f}")
    assert_within(m, target)
    assert m["frames"] in (math.ceil(target * m["fps"]) - 1, math.ceil(target * m["fps"])), m
    if m["audio"] is not None:
        assert m["audio"] == pytest.approx(target, abs=0.025), m


@pytest.mark.slow
@pytest.mark.parametrize("speed", [1.0, 1.5])
@pytest.mark.parametrize("target", LONG_TARGETS)
@pytest.mark.parametrize("name", ["25", "29.97", "30", "60"])
def test_long_duration_table(mp, long_sources, tmp_path, name, target, speed):
    _, m = run(mp, long_sources(name), tmp_path / "out.mp4", speed=speed, max_duration=target)
    _check_long(m, target, f"{name}fps {speed}x")


@pytest.mark.slow
@pytest.mark.parametrize("speed", [1.0, 1.5])
def test_long_duration_without_audio(mp, long_sources, tmp_path, speed):
    result, m = run(mp, long_sources("noaudio"), tmp_path / "out.mp4", speed=speed, max_duration=60.03)
    assert m["audio"] is None and not result.probe.has_audio
    _check_long(m, 60.03, f"noaudio {speed}x")


@pytest.mark.slow
def test_long_limit_shorter_video_unchanged(mp, long_sources, tmp_path):
    src = long_sources("short50")
    result, m = run(mp, src, tmp_path / "out.mp4", max_duration=60.03)  # 50 s com limite 60: sem corte
    ref = measure(src)
    print(f"\nDUR short50 1.0x: video={m['video']:.4f} audio={m['audio']} frames={m['frames']}")
    assert "trim=end" not in " ".join(result.command)
    assert m["frames"] == ref["frames"] == 1500
    assert m["video"] == pytest.approx(ref["video"], abs=0.001)
    assert m["audio"] == pytest.approx(ref["audio"], abs=0.03)


@pytest.mark.slow
def test_long_limit_shorter_video_with_speed(mp, long_sources, tmp_path):
    result, m = run(mp, long_sources("short50"), tmp_path / "out.mp4", speed=1.5, max_duration=60.03)
    print(f"\nDUR short50 1.5x: video={m['video']:.4f} audio={m['audio']} frames={m['frames']}")
    assert "trim=end" not in " ".join(result.command)
    assert m["video"] == pytest.approx(50 / 1.5, abs=1 / 30 + 0.005)  # 33,33 s
    assert m["audio"] == pytest.approx(50 / 1.5, abs=0.025)
    assert abs(m["video"] - m["audio"]) <= 1 / 30 + 0.025
