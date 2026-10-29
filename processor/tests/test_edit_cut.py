"""Recorte manual da edição (trim_start + cut_end): opções, comandos, verificação e FFmpeg real em vídeo sintético."""

from __future__ import annotations

import hashlib

import pytest

from media_processor import MediaProcessor, ProcessingOptions
from media_processor.commands import audio_filters, effective_max_duration, expected_spans, plan, video_filters
from media_processor.probe import parse_probe

from conftest import ff, requires_ffmpeg
from test_commands import build, image_raw, video_raw
from test_edit_commands import ANTI, EDIT, PADRAO, long_raw
from test_edit_duration import deltas, measure, onsets, stream_spans

# ---------------- comandos de antes (presets e edições sem recorte) ----------------

SYNC = {**EDIT, "audio_sync_start": True}
GOLDEN_RAWS = {
    "v": video_raw(),
    "na": video_raw(audio=False),
    "l40": long_raw(40.0),
    "l40_25": long_raw(40.0, rate="25/1"),
    "img": image_raw(),
}
GOLDEN_OPTS = {
    "padrao": PADRAO,
    "anti": ANTI,
    "anti_tom": {**ANTI, "pitch_shift": 2.0, "grain": 1.0},
    "edit_mp4": SYNC,
    "edit_auto": {**SYNC, "reencode": False},
    "edit_vel": {**SYNC, "speed": 1.25},
    "edit_15": {**SYNC, "max_duration": 15.03},
    "edit_tudo": {**SYNC, "speed": 1.5, "max_duration": 30.03, "sharpen": True},
}
# sha256 (16 primeiros) dos argumentos gerados ANTES do recorte existir (commit 9648098), sem o executável.
GOLDEN = {
    "padrao/v": "838e315ad1281aec", "padrao/na": "942726ed6ae54296", "padrao/l40": "838e315ad1281aec",
    "padrao/l40_25": "9a057262369287bd", "padrao/img": "b0a69d5c7e104224",
    "anti/v": "0d294f3f248afa9e", "anti/na": "6bbe793a5c2e6683", "anti/l40": "c9386e4d64e2a4cd",
    "anti/l40_25": "0c9da2e692e4dc8a", "anti/img": "2f9f3b669016cb72",
    "anti_tom/v": "c3249e62b83aad6f", "anti_tom/na": "bdd7fa5f8fb7ce62", "anti_tom/l40": "db66bdd9f82c675a",
    "anti_tom/l40_25": "10c66e262ed67355", "anti_tom/img": "7b42b16ce0c157a9",
    "edit_mp4/v": "710a43398dd0d825", "edit_mp4/na": "b40e03043c561ea3", "edit_mp4/l40": "710a43398dd0d825",
    "edit_mp4/l40_25": "710a43398dd0d825", "edit_mp4/img": "0e1420a8d2e9e79a",
    "edit_auto/v": "60080e4cf7a0e6bb", "edit_auto/na": "258945906975cefd", "edit_auto/l40": "60080e4cf7a0e6bb",
    "edit_auto/l40_25": "60080e4cf7a0e6bb", "edit_auto/img": "0e1420a8d2e9e79a",
    "edit_vel/v": "d7b5d8b668835512", "edit_vel/na": "ea4ff3542b50750e", "edit_vel/l40": "d7b5d8b668835512",
    "edit_vel/l40_25": "95b2df5c5515c95c", "edit_vel/img": "0e1420a8d2e9e79a",
    "edit_15/v": "710a43398dd0d825", "edit_15/na": "b40e03043c561ea3", "edit_15/l40": "a5926f6a54c5b7cd",
    "edit_15/l40_25": "a5926f6a54c5b7cd", "edit_15/img": "0e1420a8d2e9e79a",
    "edit_tudo/v": "05a5df2bdc76c04c", "edit_tudo/na": "6de543674ca149d7", "edit_tudo/l40": "05a5df2bdc76c04c",
    "edit_tudo/l40_25": "f20ea6121e34df34", "edit_tudo/img": "0e1420a8d2e9e79a",
}


def _digest(cmd: list[str]) -> str:
    return hashlib.sha256("\x00".join(cmd[1:]).encode()).hexdigest()[:16]


def golden_digests() -> dict[str, str]:
    return {f"{o}/{r}": _digest(build(parse_probe(raw), ProcessingOptions.from_params(opts)))
            for o, opts in GOLDEN_OPTS.items() for r, raw in GOLDEN_RAWS.items()}


def test_commands_without_cut_are_unchanged():
    # presets e edições sem recorte: exatamente os argumentos de antes (cut_end ausente ou 0)
    assert golden_digests() == GOLDEN
    for name, opts in GOLDEN_OPTS.items():
        for raw in GOLDEN_RAWS.values():
            probe = parse_probe(raw)
            assert build(probe, ProcessingOptions.from_params({**opts, "cut_end": 0.0})) == build(
                probe, ProcessingOptions.from_params(opts)), name


# ---------------- opções ----------------

def test_cut_end_option():
    assert ProcessingOptions().cut_end == 0.0
    o = ProcessingOptions.from_params({**SYNC, "trim_start": 3.25, "cut_end": 12.8})
    assert (o.trim_start, o.cut_end) == (3.25, 12.8)
    assert isinstance(ProcessingOptions.from_params({**SYNC, "cut_end": 5}).cut_end, float)


@pytest.mark.parametrize("params", [{"cut_end": -1.0}, {"cut_end": 86401.0}, {"cut_end": float("nan")},
                                    {"cut_end": "5"}, {"cut_end": True},
                                    {"trim_start": 5.0, "cut_end": 5.0}, {"trim_start": 6.0, "cut_end": 5.0}])
def test_cut_end_rejects_invalid(params):
    with pytest.raises(ValueError):
        ProcessingOptions.from_params({**SYNC, **params})


# ---------------- alvo, filtros e spans ----------------

def cut(start: float, end: float, **extra) -> ProcessingOptions:
    return ProcessingOptions.from_params({**SYNC, "trim_start": start, "cut_end": end, **extra})


def test_effective_target_with_cut():
    probe = parse_probe(long_raw(40.0))
    assert effective_max_duration(probe, cut(3.25, 12.8)) == pytest.approx(9.55)
    assert effective_max_duration(probe, cut(3.25, 12.8, speed=1.25)) == pytest.approx(7.64)
    # sem margem: é o trecho marcado; max_duration também ligado -> vale o menor
    assert effective_max_duration(probe, cut(0.0, 20.0, max_duration=15.03)) == pytest.approx(15.03)
    assert effective_max_duration(probe, cut(0.0, 10.0, max_duration=15.03)) == pytest.approx(10.0)
    # fim no fim do vídeo (ou depois): só o corte inicial, sem trim=end
    assert effective_max_duration(probe, cut(3.25, 40.0)) is None
    assert effective_max_duration(probe, cut(3.25, 45.0)) is None
    assert effective_max_duration(parse_probe(image_raw()), cut(0.0, 1.0)) is None


def test_filters_with_cut_without_speed():
    probe = parse_probe(long_raw(40.0))
    opts = cut(3.25, 12.8)
    assert video_filters(probe, opts) == ["trim=start=3.25", "setpts=(PTS-3.25/TB)", "trim=end=9.55"]
    assert audio_filters(probe, opts) == ["atrim=start=3.25", "asetpts=PTS-3.25/TB", "atrim=end=9.55"]
    cmd = build(probe, opts)
    assert cmd[cmd.index("-enc_time_base:v") + 1] == "filter"
    assert plan(probe, opts).expected_duration == pytest.approx(9.55)


def test_filters_with_cut_and_speed():
    probe = parse_probe(long_raw(40.0))
    opts = cut(3.25, 12.8, speed=1.25, sharpen=True)
    assert video_filters(probe, opts) == [
        "trim=start=3.25", "cas=strength=0.25:planes=1", "setpts=(PTS-3.25/TB)/1.25", "fps=30/1", "trim=end=7.64"]
    assert audio_filters(probe, opts) == [
        "atrim=start=3.25", "asetpts=PTS-3.25/TB", "atempo=1.25", "asetpts=PTS-STARTPTS+STARTPTS/1.25",
        "atrim=end=7.64"]


def test_cut_from_zero_and_cut_to_end():
    probe = parse_probe(long_raw(40.0))
    assert video_filters(probe, cut(0.0, 10.0)) == ["trim=end=10"]
    assert audio_filters(probe, cut(0.0, 10.0)) == ["atrim=end=10"]
    assert video_filters(probe, cut(5.0, 40.0)) == ["trim=start=5", "setpts=(PTS-5/TB)"]


def test_expected_spans_with_cut():
    probe = parse_probe(long_raw(40.0))
    spans = expected_spans(probe, cut(3.25, 12.8, speed=1.25))
    assert (spans["video"].start, spans["video"].end, spans["video"].end_known) == (0.0, pytest.approx(7.64), True)
    # a origem sintética não informa a duração do áudio: só o limite superior do fim
    assert (spans["audio"].start, spans["audio"].end, spans["audio"].end_known) == (0.0, pytest.approx(7.64), False)
    assert expected_spans(probe, cut(3.25, 40.0)) is None


# ---------------- FFmpeg real ----------------

FLASHES = "between(t,3,3.2)+between(t,6,6.2)+between(t,9,9.2)"  # flash branco e beep juntos em 3, 6 e 9 s


@pytest.fixture(scope="module")
def cut_sources(tmp_path_factory):
    d = tmp_path_factory.mktemp("cut")
    s = {"sync": d / "sync.mp4", "aoff": d / "aoff.mp4", "s25": d / "s25.mp4"}
    for name, rate in (("sync", 30), ("s25", 25)):
        ff("-f", "lavfi", "-i", f"color=c=black:size=160x120:rate={rate}:duration=12",
           "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=12",
           "-filter:v", f"geq=lum='if({FLASHES.replace('t,', 'T,')},235,16)':cb=128:cr=128",
           "-filter:a", f"volume=volume='if({FLASHES},1,0)':eval=frame",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(s[name]))
    # áudio começa 0,2 s depois do vídeo
    ff("-i", str(s["sync"]), "-itsoffset", "0.2", "-i", str(s["sync"]), "-map", "0:v", "-map", "1:a", "-c", "copy",
       str(s["aoff"]))
    return s


def run_cut(mp: MediaProcessor, src, out, start, end, speed=1.0):
    result = mp.process(src, out, cut(start, end, speed=speed))
    return result, measure(out)


@requires_ffmpeg
def test_real_cut_exact_on_frame_grid(mp, cut_sources, tmp_path):
    out = tmp_path / "out.mp4"
    result, m = run_cut(mp, cut_sources["sync"], out, 2.5, 10.0)
    assert m["frames"] == 225  # quadros 75..299 de 30 fps: nem um a mais, nem um a menos
    assert m["video"] == pytest.approx(7.5, abs=0.001)
    assert m["audio"] == pytest.approx(7.5, abs=0.0005)
    assert abs(m["v_start"]) <= 0.001 and abs(m["a_start"]) <= 0.025
    v, a = onsets(out)
    assert v == pytest.approx([0.5, 3.5, 6.5], abs=1 / 30 + 0.005)  # o flash de 3 s cai em 3 - 2,5
    assert a == pytest.approx([0.5, 3.5, 6.5], abs=0.025)  # silencedetect mede em blocos do AAC (~21 ms)
    assert "trim=start=2.5" in result.command[result.command.index("-filter:v") + 1]


@requires_ffmpeg
@pytest.mark.parametrize("speed", [1.05, 1.25, 1.5])
def test_real_cut_with_speed(mp, cut_sources, tmp_path, speed):
    out = tmp_path / "out.mp4"
    _, m = run_cut(mp, cut_sources["sync"], out, 2.5, 10.0, speed=speed)
    target = 7.5 / speed
    frame = 1 / 30
    assert target - frame - 0.005 <= m["video"] <= target + frame + 0.005, m
    assert m["audio"] == pytest.approx(target, abs=0.025)
    v, a = onsets(out)
    expected = [(t - 2.5) / speed for t in (3, 6, 9)]
    assert v == pytest.approx(expected, abs=frame + 0.01)
    assert a == pytest.approx(expected, abs=0.025)


@requires_ffmpeg
def test_real_cut_off_grid_25fps(mp, cut_sources, tmp_path):
    # início e fim fora da grade de 25 fps (40 ms): o primeiro quadro começa até um quadro depois e a verificação
    # aceita; vídeo e áudio continuam sincronizados
    out = tmp_path / "out.mp4"
    _, m = run_cut(mp, cut_sources["s25"], out, 2.51, 9.987)
    assert 0 <= m["v_start"] <= 0.04 + 0.005
    assert m["audio"] == pytest.approx(9.987 - 2.51, abs=0.0005)
    assert abs(m["v_start"] + m["video"] - (9.987 - 2.51)) <= 0.04 + 0.005
    for d in deltas(out):
        assert d == pytest.approx(0.0, abs=0.04 + 0.01)


@requires_ffmpeg
@pytest.mark.parametrize("speed", [1.0, 1.25])
def test_real_cut_keeps_audio_offset_sync(mp, cut_sources, tmp_path, speed):
    out = tmp_path / "out.mp4"
    run_cut(mp, cut_sources["aoff"], out, 1.0, 11.0, speed=speed)
    for before, after in zip(deltas(cut_sources["aoff"]), deltas(out)):
        assert after == pytest.approx(before / speed, abs=0.025)
    assert stream_spans(out)["audio"][1] == pytest.approx(10.0 / speed, abs=0.025)


@requires_ffmpeg
def test_real_cut_to_end_has_no_end_trim(mp, cut_sources, tmp_path):
    out = tmp_path / "out.mp4"
    result, m = run_cut(mp, cut_sources["sync"], out, 4.0, 12.0)
    assert "trim=end" not in " ".join(result.command)
    assert m["video"] == pytest.approx(8.0, abs=1 / 30 + 0.005)
    assert abs(m["frames"] - 240) <= 1
