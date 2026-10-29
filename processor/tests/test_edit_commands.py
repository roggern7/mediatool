"""Testes puros dos parâmetros de edição (max_duration, sharpen): opções, plano e filtros (sem executar ffmpeg)."""

from __future__ import annotations

import pytest

from media_processor import ProcessingOptions
from media_processor.commands import (
    SHARPEN_FILTER,
    audio_filters,
    effective_max_duration,
    plan,
    video_filters,
)
from media_processor.probe import parse_probe

from test_commands import arg_after, build, image_raw, video_raw

# Parâmetros que o backend usa na edição (sem cleanup, sem espelho, sem corte inicial, sem auto_adjust).
EDIT = dict(metadata_cleanup=False, trim_start=0.0, mirror=False, speed=1.0, auto_adjust=False, reencode=True)
PADRAO = dict(metadata_cleanup=True, trim_start=0.5, mirror=True, speed=1.05, auto_adjust=True, reencode=True)
ANTI = {**PADRAO, "zoom": 3.0, "brightness": 0.01, "contrast": 1.02, "saturation": 1.03, "trim_end": 0.3}


def long_raw(duration=40.0, **kwargs):
    raw = video_raw(**kwargs)
    raw["format"]["duration"] = str(duration)
    raw["streams"][0]["duration"] = str(duration)
    if len(raw["streams"]) > 1:
        raw["streams"][1]["sample_rate"] = "48000"
    return raw


# ---------------- opções ----------------

def test_from_params_accepts_new_keys():
    o = ProcessingOptions.from_params({"max_duration": 15.03, "sharpen": True})
    assert o.max_duration == 15.03 and o.sharpen is True
    assert ProcessingOptions().max_duration == 0.0 and ProcessingOptions().sharpen is False
    assert isinstance(ProcessingOptions.from_params({"max_duration": 15}).max_duration, float)


@pytest.mark.parametrize("params", [{"max_duration": -0.1}, {"max_duration": 86400.5},
                                    {"max_duration": float("nan")}, {"max_duration": float("inf")},
                                    {"max_duration": "15"}, {"max_duration": True},
                                    {"sharpen": "yes"}, {"sharpen": 1}, {"sharpen": None}])
def test_new_options_reject_invalid(params):
    with pytest.raises(ValueError):
        ProcessingOptions.from_params(params)


# ---------------- neutros: comando idêntico ao de antes ----------------

@pytest.mark.parametrize("base", [PADRAO, ANTI, EDIT, {**EDIT, "reencode": False}])
@pytest.mark.parametrize("raw", [video_raw(), video_raw(audio=False), long_raw(), image_raw()])
def test_neutral_new_params_keep_command(base, raw):
    probe = parse_probe(raw)
    before = build(probe, ProcessingOptions.from_params(base))
    after = build(probe, ProcessingOptions.from_params({**base, "max_duration": 0.0, "sharpen": False,
                                                        "audio_sync_start": False}))
    assert before == after
    assert all("cas=" not in a and "trim=end" not in a and a != "-enc_time_base:v" and "STARTPTS/" not in a
               for a in after)


# ---------------- effective_max_duration e plano ----------------

def test_effective_max_duration():
    probe = parse_probe(long_raw(40.0))
    assert effective_max_duration(probe, ProcessingOptions(**EDIT, max_duration=15.03)) == 15.03
    # duração natural da saída (20 s a 1,5x = 13,33 s) já cabe no alvo: sem corte
    short = parse_probe(long_raw(20.0))
    assert effective_max_duration(short, ProcessingOptions(**{**EDIT, "speed": 1.5}, max_duration=15.03)) is None
    assert effective_max_duration(short, ProcessingOptions(**EDIT, max_duration=15.03)) == 15.03
    # 10 s com limite 15: nunca prolonga
    assert effective_max_duration(parse_probe(long_raw(10.0)), ProcessingOptions(**EDIT, max_duration=15.03)) is None
    assert effective_max_duration(probe, ProcessingOptions(**EDIT)) is None
    assert effective_max_duration(parse_probe(image_raw()), ProcessingOptions(**EDIT, max_duration=1.0)) is None
    # cortes inicial/final entram na conta: 16 - 0,5 - 0,3 = 15,2 > 15,03
    trimmed = ProcessingOptions(**{**EDIT, "trim_start": 0.5, "trim_end": 0.3}, max_duration=15.03)
    assert effective_max_duration(parse_probe(long_raw(16.0)), trimmed) == 15.03
    assert effective_max_duration(parse_probe(long_raw(15.5)), trimmed) is None


def test_plan_forced_encode_and_expected_duration():
    probe = parse_probe(long_raw(40.0))
    copy = {**EDIT, "reencode": False}
    assert plan(probe, ProcessingOptions(**copy)).kind == "copy"
    for extra in ({"max_duration": 15.03}, {"sharpen": True}):
        p = plan(probe, ProcessingOptions(**copy, **extra))
        assert p.kind == "encode" and p.forced_reencode
    p = plan(probe, ProcessingOptions(**{**EDIT, "speed": 1.5}, max_duration=15.03))
    assert p.expected_duration == pytest.approx(15.03)
    p = plan(parse_probe(long_raw(20.0)), ProcessingOptions(**{**EDIT, "speed": 1.5}, max_duration=15.03))
    assert p.expected_duration == pytest.approx(20 / 1.5)
    # sem corte efetivo e reencode=False: continua copy
    assert plan(parse_probe(long_raw(10.0)), ProcessingOptions(**copy, max_duration=15.03)).kind == "copy"


# ---------------- ordem dos filtros ----------------

def test_cut_after_speed_and_fps():
    probe = parse_probe(long_raw(40.0))
    opts = ProcessingOptions(**{**EDIT, "speed": 1.5}, max_duration=15.03)
    assert video_filters(probe, opts) == ["setpts=PTS/1.5", "fps=30/1", "trim=end=15.03"]
    assert audio_filters(probe, opts) == ["atempo=1.5", "atrim=end=15.03"]
    cmd = build(probe, opts)
    assert arg_after(cmd, "-filter:v") == "setpts=PTS/1.5,fps=30/1,trim=end=15.03"
    assert arg_after(cmd, "-filter:a") == "atempo=1.5,atrim=end=15.03"
    # só o corte, a 1,0x
    only = ProcessingOptions(**EDIT, max_duration=15.03)
    assert video_filters(probe, only) == ["trim=end=15.03"]
    assert audio_filters(probe, only) == ["atrim=end=15.03"]


def test_cut_with_full_preset_chain():
    probe = parse_probe(long_raw(40.0))
    opts = ProcessingOptions.from_params({**ANTI, "pitch_shift": 2.0, "max_duration": 15.03, "sharpen": True,
                                          "grain": 1.0})
    vf = video_filters(probe, opts)
    assert vf[-3:] == ["fps=30/1", "trim=end=15.03", "setsar=1"]
    assert vf.index("eq=brightness=0.01:contrast=1.02:saturation=1.03") + 1 == vf.index(SHARPEN_FILTER)
    assert vf.index(SHARPEN_FILTER) + 1 == vf.index("noise=alls=1:allf=t")
    af = audio_filters(probe, opts)
    assert af[0].startswith("atrim=start=0.5:end=") and af[-1] == "atrim=end=15.03"
    assert any(a.startswith("asetrate=") for a in af) and af.index("atrim=end=15.03") > af.index("aresample=48000")


def test_sharpen_filter_position_and_images():
    probe = parse_probe(long_raw(40.0))
    assert video_filters(probe, ProcessingOptions(**EDIT, sharpen=True)) == ["cas=strength=0.25:planes=1"]
    vf = video_filters(probe, ProcessingOptions(**{**EDIT, "brightness": 0.05}, sharpen=True))
    assert vf == ["eq=brightness=0.05", "cas=strength=0.25:planes=1"]
    # imagens: nitidez ignorada (cas só na luma estragaria a cor de imagens RGB)
    image = build(parse_probe(image_raw()), ProcessingOptions(**EDIT, sharpen=True))
    assert all("cas=" not in a for a in image)


def test_no_audio_has_no_audio_filter():
    probe = parse_probe(long_raw(40.0, audio=False))
    cmd = build(probe, ProcessingOptions(**{**EDIT, "speed": 1.25}, max_duration=15.03))
    assert "-filter:a" not in cmd and "-c:a" not in cmd
    assert arg_after(cmd, "-filter:v").endswith("trim=end=15.03")


def test_encoder_time_base_only_with_cut():
    probe = parse_probe(long_raw(40.0))
    cut = build(probe, ProcessingOptions(**EDIT, max_duration=15.03))
    assert arg_after(cut, "-enc_time_base:v") == "filter"
    assert cut.index("-enc_time_base:v") > cut.index("-c:v")
    for opts in (ProcessingOptions(**EDIT, sharpen=True), ProcessingOptions(**{**EDIT, "speed": 1.5}),
                 ProcessingOptions(**EDIT, max_duration=50.0)):  # sem corte efetivo
        assert "-enc_time_base:v" not in build(probe, opts)


def test_audio_sync_start_scales_audio_start_after_atempo():
    probe = parse_probe(long_raw(40.0))
    synced = {**EDIT, "speed": 1.5, "audio_sync_start": True}
    assert audio_filters(probe, ProcessingOptions(**synced, max_duration=15.03)) == [
        "atempo=1.5", "asetpts=PTS-STARTPTS+STARTPTS/1.5", "atrim=end=15.03"]
    assert audio_filters(probe, ProcessingOptions(**synced)) == ["atempo=1.5", "asetpts=PTS-STARTPTS+STARTPTS/1.5"]
    # sem velocidade não há atempo nem asetpts; sem o parâmetro o comando é o de antes
    assert audio_filters(probe, ProcessingOptions(**{**synced, "speed": 1.0})) == []
    assert audio_filters(probe, ProcessingOptions(**{**EDIT, "speed": 1.5})) == ["atempo=1.5"]
    assert video_filters(probe, ProcessingOptions(**synced)) == video_filters(probe, ProcessingOptions(**{**EDIT, "speed": 1.5}))
    with pytest.raises(ValueError):
        ProcessingOptions.from_params({"audio_sync_start": "yes"})
