"""Ajustes opcionais (zoom, cor, corte final, tom, granulação): comando e resultado com FFmpeg real."""

from __future__ import annotations

import statistics
import subprocess
from pathlib import Path

import pytest

from media_processor import MediaProcessor, ProcessingOptions, sha256_file
from media_processor.commands import MIN_DURATION_AFTER_TRIM_END, effective_trim_end, plan
from media_processor.probe import parse_probe

from conftest import FFMPEG, exif_app1, ff, gray_frame, requires_ffmpeg
from test_commands import FFMPEG as FAKE_FFMPEG
from test_commands import arg_after, build, image_raw, video_raw

PLAIN = dict(trim_start=0.0, mirror=False, speed=1.0)
NEUTRAL = dict(zoom=0, brightness=0, contrast=1, saturation=1, trim_end=0, pitch_shift=0, grain=0)
ANTI_DUP = dict(zoom=3, brightness=0.01, contrast=1.02, saturation=1.03, trim_end=0.3)


def raw_with_rate(**kwargs):
    raw = video_raw(**kwargs)
    raw["streams"][1]["sample_rate"] = "44100"
    return raw


# ---------------- opções ----------------

def test_new_options_default_to_neutral():
    o = ProcessingOptions()
    assert (o.zoom, o.brightness, o.contrast, o.saturation, o.trim_end, o.pitch_shift, o.grain) == (
        0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 0.0)
    assert ProcessingOptions.from_params(NEUTRAL) == o
    o = ProcessingOptions.from_params(ANTI_DUP)
    assert (o.zoom, o.trim_end, o.trim_start, o.speed) == (3.0, 0.3, 0.5, 1.05)


@pytest.mark.parametrize("params", [
    {"zoom": -1}, {"zoom": 10.5}, {"brightness": 0.2}, {"brightness": -0.11}, {"contrast": 0.5}, {"contrast": 1.2},
    {"saturation": 0.7}, {"saturation": 1.3}, {"trim_end": -0.1}, {"trim_end": 31}, {"pitch_shift": 4},
    {"pitch_shift": -3.5}, {"grain": -1}, {"grain": 5}, {"zoom": "3"}, {"grain": True}, {"contrast": float("nan")},
    {"sharpen": 1},
])
def test_new_options_rejected(params):
    with pytest.raises(ValueError):
        ProcessingOptions.from_params(params)


# ---------------- comando (puro) ----------------

# Lista de argumentos do preset Padrão em um vídeo 640x360 com áudio, como era antes dos ajustes opcionais.
DEFAULT_COMMAND = [
    FAKE_FFMPEG, "-hide_banner", "-nostdin", "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
    "-fflags", "+genpts", "-i", "C:\\in\\clip.mp4", "-map", "0:0", "-map", "0:1",
    "-filter:v", "trim=start=0.5,hflip,setpts=(PTS-0.5/TB)/1.05,fps=30/1,setsar=1",
    "-filter:a", "atrim=start=0.5,asetpts=PTS-0.5/TB,atempo=1.05",
    "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-profile:v", "high", "-pix_fmt", "yuv420p",
    "-bsf:v", "filter_units=remove_types=6", "-c:a", "aac", "-b:a", "160k",
    "-map_metadata", "-1", "-map_chapters", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact",
    "-flags:a", "+bitexact", "-metadata:s:v", "encoder=", "-metadata:s:a", "encoder=",
    "-movflags", "+faststart", "-f", "mp4", "C:\\out\\clip.mp4",
]
DEFAULT_SIX = {"metadata_cleanup": True, "trim_start": 0.5, "mirror": True, "speed": 1.05, "auto_adjust": True,
               "reencode": True}


@pytest.mark.skipif(not FAKE_FFMPEG.startswith("C:"), reason="caminhos do literal são do Windows")
def test_default_preset_command_is_unchanged():
    """Trava: com os ajustes novos ausentes ou neutros, o comando do Padrão é idêntico ao de antes."""
    probe = parse_probe(raw_with_rate())
    for opts in (ProcessingOptions(), ProcessingOptions.from_params(DEFAULT_SIX),
                 ProcessingOptions.from_params({**DEFAULT_SIX, **NEUTRAL})):
        assert build(probe, opts) == DEFAULT_COMMAND


def test_neutral_values_never_change_any_command():
    variants = [dict(), dict(reencode=False), dict(**PLAIN), dict(**PLAIN, reencode=False, auto_adjust=False),
                dict(trim_start=1.0, speed=0.8, mirror=False)]
    probes = [parse_probe(raw_with_rate()), parse_probe(video_raw(audio=False, width=321, height=241)),
              parse_probe(video_raw(sar="4:3")), parse_probe(image_raw())]
    for probe in probes:
        for variant in variants:
            base, neutral = ProcessingOptions(**variant), ProcessingOptions(**variant, **NEUTRAL)
            assert build(probe, base) == build(probe, neutral)
            assert plan(probe, base) == plan(probe, neutral)


def test_anti_duplicate_command():
    cmd = build(parse_probe(raw_with_rate()), ProcessingOptions(**ANTI_DUP))
    assert arg_after(cmd, "-filter:v") == (
        "trim=start=0.5:end=2.7,hflip,crop=w=622:h=350,scale=w=640:h=360:flags=lanczos,"
        "eq=brightness=0.01:contrast=1.02:saturation=1.03,setpts=(PTS-0.5/TB)/1.05,fps=30/1,setsar=1")
    assert arg_after(cmd, "-filter:a") == "atrim=start=0.5:end=2.7,asetpts=PTS-0.5/TB,atempo=1.05"
    assert plan(parse_probe(raw_with_rate()), ProcessingOptions(**ANTI_DUP)).expected_duration == pytest.approx(
        (3.0 - 0.5 - 0.3) / 1.05)


def test_only_non_neutral_color_and_grain():
    probe = parse_probe(video_raw())
    vf = arg_after(build(probe, ProcessingOptions(**PLAIN, contrast=1.05, grain=2)), "-filter:v")
    assert vf == "eq=contrast=1.05,noise=alls=2:allf=t,setsar=1"
    vf = arg_after(build(probe, ProcessingOptions(**PLAIN, saturation=0.9)), "-filter:v")
    assert vf == "eq=saturation=0.9,setsar=1"


def test_trim_end_alone_and_short_video_guard():
    probe = parse_probe(video_raw())  # 3 s
    cmd = build(probe, ProcessingOptions(**PLAIN, trim_end=0.5))
    assert arg_after(cmd, "-filter:v") == "trim=end=2.5,setsar=1" and arg_after(cmd, "-filter:a") == "atrim=end=2.5"
    assert plan(probe, ProcessingOptions(**PLAIN, trim_end=0.5)).expected_duration == pytest.approx(2.5)
    # sobraria menos de 1 s: o corte final é ignorado e o resto do preset continua valendo
    assert MIN_DURATION_AFTER_TRIM_END == 1.0
    short = ProcessingOptions(trim_start=0.5, trim_end=1.6)
    assert effective_trim_end(probe, short) == 0.0
    assert build(probe, short) == build(probe, ProcessingOptions(trim_start=0.5))
    assert plan(probe, short).expected_duration == pytest.approx(2.5 / 1.05)
    assert effective_trim_end(probe, ProcessingOptions(trim_start=0.5, trim_end=1.5)) == 1.5  # sobra exatamente 1 s


def test_pitch_shift_compensated_in_atempo():
    probe = parse_probe(raw_with_rate())
    cmd = build(probe, ProcessingOptions(**PLAIN, pitch_shift=3))
    assert arg_after(cmd, "-filter:a") == "asetrate=45423,aresample=44100,atempo=0.970874"
    assert arg_after(cmd, "-filter:v") == "setsar=1"
    cmd = build(probe, ProcessingOptions(pitch_shift=-2))
    assert arg_after(cmd, "-filter:a") == (
        "atrim=start=0.5,asetpts=PTS-0.5/TB,asetrate=43218,aresample=44100,atempo=1.071429")
    # velocidade igual ao fator: a duração já fica certa, sem atempo
    cmd = build(probe, ProcessingOptions(trim_start=0, mirror=False, speed=1.03, pitch_shift=3))
    assert arg_after(cmd, "-filter:a") == "asetrate=45423,aresample=44100"
    # sem áudio: nada
    silent = parse_probe(video_raw(audio=False))
    assert "-filter:a" not in build(silent, ProcessingOptions(pitch_shift=3))
    assert build(silent, ProcessingOptions(pitch_shift=3)) == build(silent, ProcessingOptions())


@pytest.mark.parametrize("extra", [dict(zoom=1), dict(brightness=0.01), dict(contrast=1.01), dict(saturation=1.01),
                                   dict(trim_end=0.5), dict(pitch_shift=1), dict(grain=1)])
def test_any_new_adjustment_forces_reencode(extra):
    probe = parse_probe(raw_with_rate())
    copy_opts = dict(**PLAIN, reencode=False)
    assert plan(probe, ProcessingOptions(**copy_opts)).kind == "copy"
    p = plan(probe, ProcessingOptions(**copy_opts, **extra))
    assert p.kind == "encode" and p.forced_reencode
    assert not plan(probe, ProcessingOptions(**PLAIN, **extra)).forced_reencode


def test_ignored_adjustments_do_not_force_reencode():
    opts = ProcessingOptions(**PLAIN, reencode=False, trim_end=2.5, pitch_shift=2)
    assert plan(parse_probe(video_raw(audio=False)), opts).kind == "copy"  # vídeo curto demais e sem áudio


def test_image_uses_zoom_color_grain_and_ignores_trim_end_and_pitch():
    probe = parse_probe(image_raw())  # 320x240
    cmd = build(probe, ProcessingOptions(zoom=5, brightness=0.02, grain=1, trim_end=1, pitch_shift=2),
                src="C:/in/a.jpg", dst="C:/out/a.jpg")
    assert arg_after(cmd, "-filter:v") == (
        "hflip,crop=w=304:h=228,scale=w=320:h=240:flags=lanczos,eq=brightness=0.02,noise=alls=1:allf=t")
    assert "trim" not in " ".join(cmd) and "asetrate" not in " ".join(cmd)
    raw = image_raw("png", "png_pipe")
    raw["streams"][0]["pix_fmt"] = "rgba"
    vf = arg_after(build(parse_probe(raw), ProcessingOptions(mirror=False, grain=2)), "-filter:v")
    assert vf == "noise=c0s=2:c0f=t:c1s=2:c1f=t:c2s=2:c2f=t"  # alpha sem granulação


def test_zoom_dimensions_after_rotation_sar_and_odd_crop():
    def zoom_part(raw, **opts):
        vf = arg_after(build(parse_probe(raw), ProcessingOptions(mirror=False, zoom=10, **opts)), "-filter:v")
        return [f for f in vf.split(",") if f.startswith(("crop=w=", "scale=w=")) and "trunc" not in f]

    rotated = video_raw(side_data=[{"side_data_type": "Display Matrix", "rotation": -90}])
    assert zoom_part(rotated) == ["crop=w=328:h=582", "scale=w=360:h=640:flags=lanczos"]
    assert zoom_part(rotated, auto_adjust=False) == ["crop=w=582:h=328", "scale=w=640:h=360:flags=lanczos"]
    assert zoom_part(video_raw(width=321, height=241)) == ["crop=w=290:h=218", "scale=w=320:h=240:flags=lanczos"]
    assert zoom_part(video_raw(sar="4:3", width=480, height=360)) == [
        "crop=w=582:h=328", "scale=w=640:h=360:flags=lanczos"]
    # mídia minúscula: recorte nunca abaixo de 2 px; sem o que recortar, o zoom não entra na cadeia
    assert zoom_part(video_raw(width=4, height=2)) == []
    assert zoom_part(video_raw(width=16, height=2)) == ["crop=w=14:h=2", "scale=w=16:h=2:flags=lanczos"]
    tiny = image_raw("png", "png_pipe")
    tiny["streams"][0].update(width=1, height=1)
    assert "-filter:v" not in build(parse_probe(tiny), ProcessingOptions(mirror=False, zoom=10))


# ---------------- FFmpeg real ----------------

def planes(path: Path, width: int, height: int, at: float = 0.0) -> tuple[bytes, bytes, bytes]:
    """Planos Y, U e V (yuv444p) do frame em `at` segundos."""
    out = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1",
         "-vf", f"scale={width}:{height}", "-f", "rawvideo", "-pix_fmt", "yuv444p", "-"],
        check=True, stdout=subprocess.PIPE,
    ).stdout
    n = width * height
    assert len(out) == 3 * n
    return out[:n], out[n:2 * n], out[2 * n:]


def sine_frequency(path: Path) -> float:
    """Frequência dominante do áudio (cruzamentos por zero no trecho central de 1 s)."""
    rate = 44100
    out = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(rate),
         "-f", "s16le", "-"], check=True, stdout=subprocess.PIPE,
    ).stdout
    samples = memoryview(out).cast("h")
    start = len(samples) // 2 - rate // 2
    window = samples[start:start + rate]
    crossings = sum(1 for a, b in zip(window, window[1:]) if (a < 0) != (b < 0))
    return crossings / 2.0


def streams(result, kind):
    return [s for s in result.probe.raw["streams"] if s["codec_type"] == kind]


@requires_ffmpeg
def test_zoom_keeps_dimensions_and_enlarges_content(mp, media, tmp_path):
    src = media["gradient"]  # 64x48, luminância cresce 4 por pixel da esquerda para a direita
    r = mp.process(src, tmp_path / "z.mp4", ProcessingOptions(**PLAIN, zoom=10))
    assert (r.probe.width, r.probe.height) == (64, 48)
    w, h = 64, 48
    before, after = gray_frame(src, w, h), gray_frame(r.output_path, w, h)
    row = slice((h // 2) * w, (h // 2 + 1) * w)
    src_row, out_row = before[row], after[row]
    # recorte central: as bordas somem (esquerda mais clara, direita mais escura) e o centro fica onde estava
    assert out_row[8] >= src_row[8] + 4 and out_row[-9] <= src_row[-9] - 4
    assert abs(out_row[w // 2] - src_row[w // 2]) <= 6
    # inclinação menor: o mesmo conteúdo ocupa mais pixels
    assert (out_row[-9] - out_row[8]) < (src_row[-9] - src_row[8]) * 0.96


@requires_ffmpeg
def test_color_adjustments(mp, media, tmp_path):
    src = media["av"]

    def stats(**opts):
        out = tmp_path / f"c{len(list(tmp_path.iterdir()))}.mp4"
        mp.process(src, out, ProcessingOptions(**PLAIN, **opts))
        y, u, _ = planes(out, 160, 90, at=1.0)
        return statistics.fmean(y), statistics.pstdev(y), statistics.pstdev(u)

    base = stats()
    assert stats(brightness=0.1)[0] > base[0] + 10 and stats(brightness=-0.1)[0] < base[0] - 10
    assert stats(contrast=1.1)[1] > base[1] * 1.03 and stats(contrast=0.9)[1] < base[1] * 0.97
    assert stats(saturation=1.2)[2] > base[2] * 1.05 and stats(saturation=0.8)[2] < base[2] * 0.95


@requires_ffmpeg
def test_trim_end_duration_and_av_alignment(mp, media, tmp_path):
    progress: list[float] = []
    r = mp.process(media["av"], tmp_path / "e.mp4", ProcessingOptions(trim_start=0.5, trim_end=0.5, mirror=False,
                                                                     speed=1.0), progress=progress.append)
    v, a = streams(r, "video")[0], streams(r, "audio")[0]
    assert r.probe.duration == pytest.approx(2.0, abs=0.1)
    assert float(v["duration"]) == pytest.approx(2.0, abs=0.07)
    assert abs(float(v["duration"]) - float(a["duration"])) < 0.1
    assert abs(float(v.get("start_time", 0)) - float(a.get("start_time", 0))) < 0.05
    assert progress[-1] == 1.0 and all(b >= a_ for a_, b in zip(progress, progress[1:]))
    # só o corte final, com velocidade
    r = mp.process(media["av"], tmp_path / "e2.mp4", ProcessingOptions(trim_start=0, trim_end=1.0, speed=1.25))
    v, a = streams(r, "video")[0], streams(r, "audio")[0]
    assert r.probe.duration == pytest.approx(2.0 / 1.25, abs=0.1)
    assert abs(float(v["duration"]) - float(a["duration"])) < 0.1


@requires_ffmpeg
def test_trim_end_ignored_on_short_video(mp, media, tmp_path):
    # noaudio tem 2 s: 2 - 0.5 - 0.8 = 0.7 s < 1 s -> o corte final é ignorado, o resto do preset vale
    opts = ProcessingOptions(trim_start=0.5, trim_end=0.8, zoom=3)
    r = mp.process(media["noaudio"], tmp_path / "s.mp4", opts)
    assert "end=" not in " ".join(r.command) and "crop=w=622:h=350" in " ".join(r.command)
    assert r.probe.duration == pytest.approx((2.0 - 0.5) / 1.05, abs=0.1)
    # corte final maior que o vídeo inteiro também não falha
    r = mp.process(media["noaudio"], tmp_path / "s2.mp4", ProcessingOptions(**PLAIN, trim_end=30))
    assert r.probe.duration == pytest.approx(2.0, abs=0.1)
    # com folga o corte vale: 2 - 0.5 - 0.4 = 1.1 s
    r = mp.process(media["noaudio"], tmp_path / "s3.mp4", ProcessingOptions(trim_start=0.5, trim_end=0.4, speed=1.0))
    assert r.probe.duration == pytest.approx(1.1, abs=0.1)


@requires_ffmpeg
@pytest.mark.parametrize("speed", [1.0, 1.05])
@pytest.mark.parametrize("pitch", [3.0, -3.0])
def test_pitch_shift_changes_tone_not_duration(mp, media, tmp_path, speed, pitch):
    src = media["av"]  # seno de 440 Hz, 3 s
    assert sine_frequency(src) == pytest.approx(440, abs=2)
    r = mp.process(src, tmp_path / "p.mp4", ProcessingOptions(trim_start=0, mirror=False, speed=speed,
                                                             pitch_shift=pitch))
    assert sine_frequency(r.output_path) == pytest.approx(440 * (1 + pitch / 100), abs=3)
    v, a = streams(r, "video")[0], streams(r, "audio")[0]
    assert r.probe.duration == pytest.approx(3.0 / speed, abs=0.1)
    assert abs(float(v["duration"]) - float(a["duration"])) < 0.1
    assert abs(float(v.get("start_time", 0)) - float(a.get("start_time", 0))) < 0.05
    assert a["sample_rate"] == streams(mp.process(src, tmp_path / "p0.mp4", ProcessingOptions(**PLAIN)),
                                       "audio")[0]["sample_rate"]


@requires_ffmpeg
def test_pitch_shift_without_audio_is_noop(mp, media, tmp_path):
    a = mp.process(media["noaudio"], tmp_path / "a.mp4", ProcessingOptions(pitch_shift=3))
    b = mp.process(media["noaudio"], tmp_path / "b.mp4", ProcessingOptions())
    assert a.sha256 == b.sha256 and not a.probe.has_audio


@requires_ffmpeg
def test_grain_adds_noise(mp, media, tmp_path):
    src = media["gradient"]
    clean = mp.process(src, tmp_path / "g0.mp4", ProcessingOptions(**PLAIN))
    noisy = mp.process(src, tmp_path / "g4.mp4", ProcessingOptions(**PLAIN, grain=4))
    assert clean.sha256 != noisy.sha256 and (noisy.probe.width, noisy.probe.height) == (64, 48)
    a, b = gray_frame(clean.output_path, 64, 48), gray_frame(noisy.output_path, 64, 48)
    diff = statistics.fmean(abs(x - y) for x, y in zip(a, b))
    assert 0.2 < diff < 6  # ruído leve: visível nos pixels, sem descaracterizar a imagem
    assert noisy.probe.duration == pytest.approx(2.0, abs=0.1)


@requires_ffmpeg
def test_image_zoom_and_color(mp, media, tmp_path):
    src = media["png"]  # 64x48 em tons de cinza, gradiente horizontal
    plain = mp.process(src, tmp_path / "i0.png", ProcessingOptions(mirror=False))
    r = mp.process(src, tmp_path / "i1.png", ProcessingOptions(mirror=False, zoom=10, brightness=0.1, trim_end=5,
                                                              pitch_shift=2))
    assert r.probe.media_type == "image" and (r.probe.width, r.probe.height) == (64, 48)
    a, b = gray_frame(plain.output_path, 64, 48), gray_frame(r.output_path, 64, 48)
    assert statistics.fmean(b) > statistics.fmean(a) + 10  # mais claro
    zoomed = mp.process(src, tmp_path / "i2.png", ProcessingOptions(mirror=False, zoom=10))
    z = gray_frame(zoomed.output_path, 64, 48)
    row = slice(24 * 64, 25 * 64)
    assert z[row][8] >= a[row][8] + 4 and z[row][-9] <= a[row][-9] - 4  # bordas recortadas
    # JPEG com orientação EXIF: dimensões de exibição mantidas
    j = mp.process(media["exif_jpg"], tmp_path / "j.jpg", ProcessingOptions(zoom=5, saturation=1.1, grain=2))
    assert (j.probe.width, j.probe.height) == (240, 320) and j.probe.rotation == 0


@requires_ffmpeg
def test_image_alpha_survives_color_and_grain(mp, tmp_path):
    src = tmp_path / "alpha.png"
    ff("-f", "lavfi", "-i", "color=c=red@0.5:size=33x21,format=rgba", "-frames:v", "1", "-update", "1", str(src))

    def alpha(path):
        return subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-i", str(path), "-vf",
                               "alphaextract", "-f", "rawvideo", "-pix_fmt", "gray", "-"], check=True,
                              stdout=subprocess.PIPE).stdout

    r = mp.process(src, tmp_path / "out.png", ProcessingOptions(zoom=5, brightness=0.05, saturation=1.1, grain=3))
    assert (r.probe.width, r.probe.height) == (33, 21) and r.output_path.suffix == ".png"
    # sem granulação no alpha (tolerância de 1 nível pelo arredondamento da reescala/conversão de formato)
    assert max(abs(x - y) for x, y in zip(alpha(r.output_path), alpha(src))) <= 1


@requires_ffmpeg
def test_tiny_media_with_zoom_does_not_break(mp, tmp_path):
    video = tmp_path / "tiny.mp4"
    ff("-f", "lavfi", "-i", "testsrc2=size=16x16:rate=10:duration=2", "-c:v", "libx264", "-preset", "ultrafast",
       "-pix_fmt", "yuv420p", str(video))
    r = mp.process(video, tmp_path / "tiny_out.mp4", ProcessingOptions(zoom=10, trim_end=0.3, grain=2))
    assert (r.probe.width, r.probe.height) == (16, 16)
    for size in ("1x1", "2x2", "3x5"):
        image = tmp_path / f"tiny_{size}.png"
        ff("-f", "lavfi", "-i", f"color=c=gray:size={size}", "-frames:v", "1", "-update", "1", str(image))
        out = mp.process(image, tmp_path / f"tiny_{size}_out.png", ProcessingOptions(zoom=10, contrast=1.05))
        w, h = (int(n) for n in size.split("x"))
        assert (out.probe.width, out.probe.height) == (w, h)


@requires_ffmpeg
def test_default_preset_output_is_unchanged_by_neutral_values(mp, media, tmp_path):
    """Padrão com as chaves novas ausentes ou neutras: mesmo comando (fora o destino) e mesmos bytes."""
    six = ProcessingOptions.from_params(DEFAULT_SIX)
    for name, suffix in (("av", ".mp4"), ("exif_jpg", ".jpg")):
        a = mp.process(media[name], tmp_path / f"a{suffix}", six)
        b = mp.process(media[name], tmp_path / f"b{suffix}", ProcessingOptions.from_params({**DEFAULT_SIX, **NEUTRAL}))
        assert a.command[:-1] == b.command[:-1] and a.sha256 == b.sha256 == sha256_file(a.output_path)
    video = " ".join(mp.process(media["av"], tmp_path / "c.mp4", six).command)
    assert "-filter:v trim=start=0.5,hflip,setpts=(PTS-0.5/TB)/1.05,fps=30/1,setsar=1 " in video
    assert "-filter:a atrim=start=0.5,asetpts=PTS-0.5/TB,atempo=1.05 " in video


@requires_ffmpeg
def test_anti_duplicate_preset_end_to_end(mp, media, tmp_path):
    r = mp.process(media["av"], tmp_path / "anti.mp4", ProcessingOptions.from_params({**DEFAULT_SIX, **ANTI_DUP}))
    v, a = streams(r, "video")[0], streams(r, "audio")[0]
    assert (v["width"], v["height"]) == (640, 360) and not r.forced_reencode
    assert r.probe.duration == pytest.approx((3.0 - 0.5 - 0.3) / 1.05, abs=0.1)
    assert abs(float(v["duration"]) - float(a["duration"])) < 0.1
    assert r.sha256 != mp.process(media["av"], tmp_path / "padrao.mp4", ProcessingOptions()).sha256


# ---------------- zoom não altera o SAR ----------------

def test_zoom_restores_source_sar_in_command():
    def vf(raw, **opts):
        return arg_after(build(parse_probe(raw), ProcessingOptions(**PLAIN, zoom=3, **opts)), "-filter:v")

    # auto_adjust: o setsar=1 do fim da cadeia já basta
    assert vf(video_raw()) == "crop=w=622:h=350,scale=w=640:h=360:flags=lanczos,setsar=1"
    # sem auto_adjust: o SAR de origem volta logo depois do scale (a rotação fica como metadata, sem inverter)
    assert vf(video_raw(), auto_adjust=False) == "crop=w=622:h=350,scale=w=640:h=360:flags=lanczos,setsar=1"
    rotated = dict(side_data=[{"side_data_type": "Display Matrix", "rotation": -90}])
    for extra in (dict(), rotated):
        assert vf(video_raw(sar="4:3", width=480, **extra), auto_adjust=False) == (
            "crop=w=466:h=350,scale=w=480:h=360:flags=lanczos,setsar=r=4/3:max=100")
    assert vf(video_raw(sar="1215:1216"), auto_adjust=False).endswith(",setsar=r=1215/1216:max=1216")
    # SAR desconhecido: o scale não o altera, nada a restaurar
    for unknown in ("0:1", "N/A", None):
        assert vf(video_raw(sar=unknown), auto_adjust=False) == "crop=w=622:h=350,scale=w=640:h=360:flags=lanczos"
    # imagem: SAR de origem; invertido quando o autorotate gira o frame (EXIF 6/8)
    raw = image_raw()
    raw["streams"][0]["sample_aspect_ratio"] = "1:1"
    assert vf(raw) == "crop=w=310:h=234,scale=w=320:h=240:flags=lanczos,setsar=1"
    raw["streams"][0]["sample_aspect_ratio"] = "4:3"
    assert vf(raw).endswith(",setsar=r=4/3:max=100")
    side = [{"side_data_type": "Display Matrix", "rotation": -90}]
    cmd = build(parse_probe(raw, side), ProcessingOptions(**PLAIN, zoom=3))
    assert arg_after(cmd, "-filter:v") == "crop=w=234:h=310,scale=w=240:h=320:flags=lanczos,setsar=r=3/4:max=100"


def geometry(result) -> tuple:
    """(largura, altura, SAR, rotação) da saída, lidos pelo ffprobe."""
    v = streams(result, "video")[0]
    return v["width"], v["height"], v.get("sample_aspect_ratio"), result.probe.rotation


@pytest.fixture(scope="module")
def sar_media(tmp_path_factory) -> dict[str, Path]:
    """Vídeos de 1 s com pixels quadrados e não quadrados (SAR 4:3 e 8:9), com e sem rotação de exibição."""
    if not FFMPEG:
        pytest.skip("ffmpeg/ffprobe não estão no PATH")
    d = tmp_path_factory.mktemp("sar")
    m: dict[str, Path] = {}
    for name, size, sar in (("square", "640x360", "1/1"), ("sar43", "480x360", "4/3"), ("sar89", "640x360", "8/9"),
                            ("sar109", "640x360", "10/9"), ("sarbig", "640x360", "1215/1216")):
        m[name] = d / f"{name}.mp4"
        ff("-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration=1", "-vf", f"setsar=r={sar}:max=2000",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(m[name]))
        for rotation in (90, 270):
            m[f"{name}_r{rotation}"] = d / f"{name}_r{rotation}.mp4"
            ff("-display_rotation", str(rotation), "-i", str(m[name]), "-c", "copy", str(m[f"{name}_r{rotation}"]))
    return m


# (fonte, auto_adjust) -> (largura, altura, SAR, rotação) esperados, com ou sem zoom
ZOOM_GEOMETRY = {
    ("square", True): (640, 360, "1:1", 0), ("square", False): (640, 360, "1:1", 0),
    ("square_r90", True): (360, 640, "1:1", 0), ("square_r90", False): (640, 360, "1:1", 270),
    ("square_r270", True): (360, 640, "1:1", 0), ("square_r270", False): (640, 360, "1:1", 90),
    ("sar43", True): (640, 360, "1:1", 0), ("sar43", False): (480, 360, "4:3", 0),
    # girado, o SAR do frame inverte: 360 de largura x 3/4 = 270
    ("sar43_r90", True): (270, 480, "1:1", 0), ("sar43_r90", False): (480, 360, "4:3", 270),
    ("sar43_r270", True): (270, 480, "1:1", 0), ("sar43_r270", False): (480, 360, "4:3", 90),
    ("sar89", True): (568, 360, "1:1", 0), ("sar89", False): (640, 360, "8:9", 0),
    # girado: 360 de largura x 9/8 = 405 -> 404 (par)
    ("sar89_r90", True): (404, 640, "1:1", 0), ("sar89_r90", False): (640, 360, "8:9", 270),
    ("sar89_r270", True): (404, 640, "1:1", 0), ("sar89_r270", False): (640, 360, "8:9", 90),
    # 10:9 girado: 360 x 9/10 = 324 exato; a conta tem de ser a do ffmpeg (9/10, não 1/(10/9)) para não cair em 322
    ("sar109", True): (710, 360, "1:1", 0), ("sar109", False): (640, 360, "10:9", 0),
    ("sar109_r90", True): (324, 640, "1:1", 0), ("sar109_r90", False): (640, 360, "10:9", 270),
    ("sar109_r270", True): (324, 640, "1:1", 0), ("sar109_r270", False): (640, 360, "10:9", 90),
    # termos acima de 100: o setsar precisa do max para devolver a razão exata
    ("sarbig", True): (638, 360, "1:1", 0), ("sarbig", False): (640, 360, "1215:1216", 0),
}


@requires_ffmpeg
@pytest.mark.parametrize("name,auto_adjust", list(ZOOM_GEOMETRY))
def test_zoom_keeps_dimensions_and_sar_of_video(mp, sar_media, tmp_path, name, auto_adjust):
    """Com zoom, largura, altura, SAR e rotação da saída são os mesmos de sem zoom (rotação e SAR combinados)."""
    expected = ZOOM_GEOMETRY[name, auto_adjust]
    for zoom in (0, 3, 10):
        r = mp.process(sar_media[name], tmp_path / f"z{zoom}.mp4",
                       ProcessingOptions(**PLAIN, auto_adjust=auto_adjust, zoom=zoom))
        assert ("crop=w=" in " ".join(r.command)) == (zoom > 0)
        assert geometry(r) == expected, f"zoom={zoom}"


@requires_ffmpeg
def test_zoom_keeps_dimensions_and_sar_of_images(mp, media, tmp_path):
    def image(name, source, *filters):
        path = tmp_path / name
        ff("-f", "lavfi", "-i", ",".join((source, *filters)), "-frames:v", "1", "-update", "1", str(path))
        return path

    def with_exif(path, orientation):
        data = path.read_bytes()
        out = path.with_name(f"exif{orientation}_{path.name}")
        out.write_bytes(data[:2] + exif_app1(orientation) + data[2:])
        return out

    jpg = image("plain.jpg", "testsrc2=size=320x240")
    wide = image("wide.jpg", "testsrc2=size=320x240", "setsar=4/3")  # pixels não quadrados
    cases = [
        (jpg, (320, 240, "1:1", 0)),
        (media["exif_jpg"], (240, 320, "1:1", 0)),  # EXIF 6
        (with_exif(jpg, 8), (240, 320, "1:1", 0)),
        (image("alpha.png", "color=c=red@0.5:size=33x21", "format=rgba"), (33, 21, "1:1", 0)),
        (image("odd.jpg", "color=c=gray:size=33x21"), (33, 21, "1:1", 0)),
        (image("odd.png", "color=c=gray:size=35x51"), (35, 51, "1:1", 0)),
        (wide, (320, 240, "4:3", 0)),
        (with_exif(wide, 6), (240, 320, "3:4", 0)),  # girado pelo EXIF: SAR invertido, com ou sem zoom
        (with_exif(wide, 8), (240, 320, "3:4", 0)),
        (with_exif(wide, 3), (320, 240, "4:3", 0)),  # 180 graus: SAR não inverte
    ]
    for index, (src, expected) in enumerate(cases):
        for zoom in (0, 3, 10):
            r = mp.process(src, tmp_path / f"out{index}_{zoom}{src.suffix}", ProcessingOptions(mirror=False, zoom=zoom))
            assert ("crop=w=" in " ".join(r.command)) == (zoom > 0)
            assert geometry(r) == expected, f"{src.name} zoom={zoom}"
