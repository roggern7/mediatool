"""Testes puros de verify.check_output_duration e commands.expected_spans (JSONs montados à mão, sem FFmpeg)."""

from __future__ import annotations

import pytest

from media_processor import ProcessingOptions
from media_processor.commands import StreamSpan, expected_spans
from media_processor.probe import parse_probe
from media_processor.verify import check_output_duration

T = 15.03
BOTH = {"video": StreamSpan(0.0, T, True), "audio": StreamSpan(0.0, T, True)}


def out_raw(*, video=15.0333, audio: float | None | bool = 15.03, container=15.0333, rate="30/1", v_start="0.000000",
            a_start="0.000000", f_start=None):
    v = {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 64, "height": 48,
         "avg_frame_rate": rate, "r_frame_rate": rate, "disposition": {"attached_pic": 0}}
    if video is not None:
        v["duration"] = str(video)
    if v_start is not None:
        v["start_time"] = v_start
    streams = [v]
    if audio is not False:
        a = {"index": 1, "codec_type": "audio", "codec_name": "aac", "start_time": a_start}
        if audio is not None:
            a["duration"] = str(audio)
        streams.append(a)
    fmt = {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": str(container)}
    if f_start is not None:
        fmt["start_time"] = f_start
    return {"streams": streams, "format": fmt}


def check(raw, spans=None, hint=None):
    return check_output_duration(parse_probe(raw), spans or BOTH, hint)


VIDEO_ONLY = {"video": StreamSpan(0.0, T, True)}


@pytest.mark.parametrize("kwargs, spans", [
    ({}, None),  # 30 fps: vídeo 451 quadros, áudio exato
    ({"video": 15.0484, "container": 15.0484, "rate": "30000/1001"}, None),
    ({"video": 15.0150, "container": 15.0300, "rate": "30000/1001"}, None),
    ({"video": 15.04, "container": 15.04, "rate": "25/1"}, None),
    ({"video": 15.0333, "container": 15.0333, "rate": "60/1"}, None),
    ({"audio": False}, VIDEO_ONLY),  # sem áudio
    ({"audio": 15.01}, None),  # 20 ms: dentro de 25 ms
    # áudio mais curto que o vídeo na origem: termina no fim natural (15,0), sem ser prolongado
    ({"audio": 15.0}, {"video": StreamSpan(0.0, T, True), "audio": StreamSpan(0.0, 15.0, True)}),
    # vídeo começando 0,2 s depois do áudio: termina no alvo, começa em 0,2
    ({"video": 14.8333, "v_start": "0.200000"}, {"video": StreamSpan(0.2, T, True), "audio": StreamSpan(0.0, T, True)}),
    # duração da stream desconhecida na origem: só o limite superior
    ({"audio": 12.0}, {"video": StreamSpan(0.0, T, True), "audio": StreamSpan(0.0, T, False)}),
])
def test_within_tolerance(kwargs, spans):
    assert check(out_raw(**kwargs), spans) is None


@pytest.mark.parametrize("kwargs, word", [
    ({"video": 15.10, "container": 15.10}, "vídeo termina"),  # > alvo + 1 quadro + 5 ms
    ({"video": 14.98, "container": 15.03}, "vídeo termina"),  # < alvo - 1 quadro - 5 ms
    ({"audio": 15.06}, "áudio termina"),  # 30 ms acima
    ({"audio": 15.00}, "áudio termina"),  # 30 ms abaixo (o áudio da origem ia até o alvo)
    ({"container": 14.99}, "arquivo"),
    ({"container": 15.10}, "arquivo"),
    ({"video": 14.9333, "v_start": "0.100000"}, "vídeo começa"),
    ({"audio": 15.11, "a_start": "-0.080000"}, "áudio começa"),
])
def test_outside_tolerance(kwargs, word):
    msg = check(out_raw(**kwargs))
    assert msg is not None and word in msg, msg


def test_unknown_end_still_bounded_above():
    spans = {"video": StreamSpan(0.0, T, True), "audio": StreamSpan(0.0, T, False)}
    assert "áudio termina" in check(out_raw(audio=15.2), spans)


def test_sync_violation():
    # vídeo deveria começar junto com o áudio, mas começa 40 ms depois e o áudio 30 ms antes (60 fps)
    raw = out_raw(video=15.0333 - 0.04, v_start="0.040000", audio=15.06, a_start="-0.030000", rate="60/1")
    msg = check(raw)
    assert msg is not None and "dessincronizados" in msg, msg


def test_missing_expected_stream():
    assert "sem stream de áudio" in check(out_raw(audio=False))


def test_unknown_fps_uses_hint_then_fallback():
    raw = out_raw(video=15.09, container=15.09, rate="0/0", audio=False)
    assert check(raw, VIDEO_ONLY) is None  # fallback de 0,1 s
    assert check(raw, VIDEO_ONLY, hint=30.0) is not None  # com o fps da origem: 1/30 + 5 ms
    assert check(out_raw(video=15.20, container=15.20, rate="0/0", audio=False), VIDEO_ONLY) is not None


def test_missing_stream_durations_use_container():
    assert check(out_raw(video=None, audio=None, container=15.03)) is None
    assert check(out_raw(video=None, audio=None, container=15.2)) is not None


def test_container_start_offset():
    # container com start_time: fim = start + duração
    raw = out_raw(video=15.0, v_start="0.033333", audio=15.03, f_start="0.000000", container=15.0333)
    assert check(raw) is None


# ---------------- expected_spans ----------------

def src_raw(*, v=(0.0, 40.0), a=(0.0, 40.0), f=(0.0, 40.0)):
    streams = [{"index": 0, "codec_type": "video", "codec_name": "h264", "width": 64, "height": 48,
                "avg_frame_rate": "30/1", "r_frame_rate": "30/1", "disposition": {"attached_pic": 0},
                "start_time": str(v[0]), "duration": str(v[1])}]
    if a is not None:
        streams.append({"index": 1, "codec_type": "audio", "codec_name": "aac", "start_time": str(a[0]),
                        "duration": str(a[1])})
    return {"streams": streams, "format": {"format_name": "mov,mp4", "start_time": str(f[0]), "duration": str(f[1])}}


EDIT = dict(metadata_cleanup=False, trim_start=0.0, mirror=False, auto_adjust=False, reencode=True)


def spans_for(raw, speed=1.0, **extra):
    return expected_spans(parse_probe(raw), ProcessingOptions(**{**EDIT, "speed": speed, **extra}, max_duration=T))


def test_spans_basic_and_no_cut():
    assert spans_for(src_raw()) == BOTH
    assert spans_for(src_raw(v=(0, 10), a=(0, 10), f=(0, 10))) is None  # sem corte
    assert spans_for(src_raw(a=None)) == VIDEO_ONLY


def test_spans_shorter_streams_are_not_extended():
    s = spans_for(src_raw(v=(0, 15.5), a=(0, 15.0), f=(0, 15.5)))
    assert s["video"] == StreamSpan(0.0, T, True) and s["audio"] == StreamSpan(0.0, 15.0, True)
    s = spans_for(src_raw(v=(0, 15.0), a=(0, 15.5), f=(0, 15.5)))
    assert s["video"] == StreamSpan(0.0, 15.0, True) and s["audio"] == StreamSpan(0.0, T, True)


def test_spans_keep_offsets_relative_to_container():
    s = spans_for(src_raw(v=(0.2, 40.0), a=(0.0, 40.0), f=(0.0, 40.2)), speed=1.5)
    assert s["video"].start == pytest.approx(0.2 / 1.5) and s["video"].end == T
    s = spans_for(src_raw(v=(2.8, 40.0), a=(2.7787, 39.9147), f=(2.7787, 40.0213)))  # MPEG-TS
    assert s["video"].start == pytest.approx(0.0213) and s["audio"].start == 0.0


def test_spans_trim_start_and_unknown_duration():
    s = spans_for(src_raw(v=(0.2, 40.0), a=(0.0, 40.0), f=(0.0, 40.2)), trim_start=0.5)
    assert s["video"].start == 0.0 and s["audio"].start == 0.0
    raw = src_raw()
    del raw["streams"][1]["duration"]
    s = spans_for(raw)
    assert s["audio"] == StreamSpan(0.0, T, False)


def test_spans_audio_start_follows_audio_sync_start():
    raw = src_raw(v=(0.0, 40.0), a=(0.5, 40.0), f=(0.0, 40.5))
    assert spans_for(raw, speed=1.5)["audio"].start == pytest.approx(0.5)  # atempo mantém o início
    assert spans_for(raw, speed=1.5, audio_sync_start=True)["audio"].start == pytest.approx(0.5 / 1.5)
