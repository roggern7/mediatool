"""Leitura do `video_dash_manifest` (web/dash_manifest.py). Sem rede e sem ffmpeg; só valores FAKE.

O único manifesto vindo do real é ``observed_sanitized.mpd`` (URLs, ids e identificador trocados). Todo o resto é
forma sintética: as formas não observadas precisam cair no MP4 direto (seleção None), sem exceção."""

from __future__ import annotations

import pytest

from app.integrations.instagram.web.dash_manifest import (
    MAX_MANIFEST_BYTES,
    DashParse,
    select_dash_streams,
)
from dash_fixtures import HEAAC, OBSERVED_MPD, adaptation, dash_url, mpd, representation

CODE = "DASHaaaa001"
V = dash_url(CODE, "v")
A = dash_url(CODE, "a")


def video(**kwargs) -> str:
    kwargs.setdefault("url", V)
    return representation(**kwargs)


def audio(**kwargs) -> str:
    kwargs.setdefault("url", A)
    return representation(kind="audio", **kwargs)


def outcome(manifest: object) -> str:
    selection, parse = select_dash_streams(manifest)
    assert selection is None, parse
    return parse.outcome


# --------------------------------------------------------------------------- manifesto observado


def test_observed_manifest_has_no_real_host_or_signature():
    text = OBSERVED_MPD.read_text(encoding="utf-8")
    assert text.count("<BaseURL>") == 10 and text.count("https://video-fake.fna.fbcdn.net/") == 10
    assert "cdninstagram" not in text and "example.invalid" not in text and 'FBManifestIdentifier="FAKE"' in text


def test_observed_manifest_selects_1080p_59fps_and_the_only_audio():
    selection, parse = select_dash_streams(OBSERVED_MPD.read_text(encoding="utf-8"))
    assert parse == DashParse(outcome="ok", video_representations=9, audio_representations=1, chosen_height=1920,
                              chosen_fps=59)
    assert (selection.video.width, selection.video.height, selection.video.bandwidth) == (1080, 1920, 3279228)
    assert selection.video.fps == pytest.approx(59.0) and selection.video.content_length == 28366712
    assert selection.video.codecs.startswith("vp09.") and selection.video.url.endswith("oe=00000000")
    assert "&amp;" not in selection.video.url and "rep9.mp4" in selection.video.url
    assert (selection.audio.codecs, selection.audio.bandwidth, selection.audio.content_length) == (
        "mp4a.40.5", 87372, 758004)
    assert selection.audio.width is None and "rep10.mp4" in selection.audio.url
    assert selection.duration == pytest.approx(69.288345)


# --------------------------------------------------------------------------- critério de escolha


def test_larger_area_wins_over_fps_and_bitrate():
    manifest = mpd([video(width=720, height=1280, frame_rate="60", bandwidth=9_000_000, rep_id="a"),
                    video(width=1080, height=1920, frame_rate="30", bandwidth=1000, rep_id="b")], [audio()])
    selection, parse = select_dash_streams(manifest)
    assert (selection.video.width, selection.video.bandwidth) == (1080, 1000) and parse.chosen_fps == 30


def test_same_area_is_decided_by_fps_then_by_bitrate():
    by_fps = mpd([video(frame_rate="15360/512", bandwidth=5000), video(frame_rate="15104/256", bandwidth=4000)],
                 [audio()])
    selection, _ = select_dash_streams(by_fps)
    assert selection.video.bandwidth == 4000 and selection.video.fps == pytest.approx(59.0)
    by_rate = mpd([video(bandwidth=1000), video(bandwidth=3000), video(bandwidth=2000)], [audio()])
    assert select_dash_streams(by_rate)[0].video.bandwidth == 3000


def test_full_tie_keeps_the_first_representation():
    first, second = dash_url(CODE, "v1"), dash_url(CODE, "v2")
    selection, _ = select_dash_streams(mpd([video(url=first), video(url=second)], [audio()]))
    assert selection.video.url == first


def test_audio_of_highest_bitrate_in_the_same_adaptation_set():
    low, high = dash_url(CODE, "a1"), dash_url(CODE, "a2")
    selection, parse = select_dash_streams(mpd([video()], [audio(url=low, bandwidth=64000),
                                                           audio(url=high, bandwidth=128000)]))
    assert selection.audio.url == high and parse.audio_representations == 2


@pytest.mark.parametrize("codecs", ["mp4a.40.2", "mp4a.40.5"])
def test_aac_profiles_accepted(codecs):
    selection, _ = select_dash_streams(mpd([video()], [audio(codecs=codecs)]))
    assert selection.audio.codecs == codecs


@pytest.mark.parametrize("codecs", ["avc1.640028", "avc3.640028", "vp09.00.40.08"])
def test_video_codecs_accepted(codecs):
    """H.264 no manifesto NÃO foi observado; é aceito por inferência (a verificação por ffprobe cobre)."""
    assert select_dash_streams(mpd([video(codecs=codecs)], [audio()]))[0].video.codecs == codecs


def test_unusable_representations_are_ignored_when_a_usable_one_remains():
    manifest = mpd([video(codecs="av01.0.08M.08", width=2160, height=3840, rep_id="av1"),
                    video(url="https://evil.example.com/v.mp4", width=2160, height=3840, rep_id="host"),
                    video(width=540, height=960, rep_id="ok")], [audio()])
    selection, parse = select_dash_streams(manifest)
    assert (selection.video.width, parse.video_representations) == (540, 1)


def test_mime_and_content_type_inherited_or_missing():
    by_mime = mpd([], [], extra_adaptations=(
        adaptation("video", [video()], content_type=False).replace("<AdaptationSet", '<AdaptationSet mimeType="video/mp4"', 1)
        + adaptation("audio", [audio()], content_type=False).replace("<AdaptationSet", '<AdaptationSet mimeType="audio/mp4"', 1)))
    assert select_dash_streams(by_mime)[1].outcome == "ok"


def test_frame_rate_and_content_length_optional():
    selection, parse = select_dash_streams(mpd([video(frame_rate=None)], [audio()]))
    assert selection.video.fps is None and parse.chosen_fps is None
    assert selection.video.content_length is None and selection.audio.content_length is None
    selection, _ = select_dash_streams(mpd([video(content_length=123)], [audio(content_length="x")]))
    assert selection.video.content_length == 123 and selection.audio.content_length is None


@pytest.mark.parametrize("text, seconds", [("PT69.288345S", 69.288345), ("PT2S", 2.0), ("PT1M9.5S", 69.5),
                                           ("PT1H2M3S", 3723.0), ("PT2M", 120.0)])
def test_duration_formats(text, seconds):
    assert select_dash_streams(mpd([video()], [audio()], duration=text))[0].duration == pytest.approx(seconds)


def test_duration_absent_is_none():
    selection, parse = select_dash_streams(mpd([video()], [audio()], duration=None))
    assert parse.outcome == "ok" and selection.duration is None


# --------------------------------------------------------------------------- recusas (sem exceção)


@pytest.mark.parametrize("manifest", [None, "", "   ", 0, 12, b"<MPD/>", ["<MPD/>"], {"mpd": 1}, True])
def test_absent(manifest):
    assert outcome(manifest) == "absent"


def test_too_large():
    padding = " " * (MAX_MANIFEST_BYTES + 1)
    assert outcome(mpd([video()], [audio()]) + padding) == "too_large"
    multibyte = "é" * (MAX_MANIFEST_BYTES // 2 + 1)   # cabe em caracteres, não em bytes
    assert outcome(mpd([video()], [audio()], extra_xml=f"<X>{multibyte}</X>")) == "too_large"


@pytest.mark.parametrize("payload", [
    '<!DOCTYPE MPD [<!ENTITY xxe SYSTEM "file:///c:/windows/win.ini">]>',
    '<!DOCTYPE lolz [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>',
    "<!-- comentário -->",
    "<![CDATA[ x ]]>",
])
def test_unsafe_markup_is_refused_before_parsing(payload):
    good = mpd([video()], [audio()])
    assert outcome(good.replace("<Period", payload + "<Period", 1)) == "unsafe_xml"


@pytest.mark.parametrize("raw", ["https://video-fake.fbcdn.net/v.mp4?a=1&xxe;", "https://video-fake.fbcdn.net/v.mp4?a=1&b=2",
                                 "&xxe;"])
def test_unknown_entity_or_bare_ampersand_is_unsafe(raw):
    assert outcome(mpd([video(url=None, raw_url=raw)], [audio()])) == "unsafe_xml"


@pytest.mark.parametrize("manifest", ["<MPD", "não é xml", "<MPD></Period>", "<a><b></a>"])
def test_xml_error(manifest):
    assert outcome(manifest) == "xml_error"


def _unsupported_cases() -> dict[str, str]:
    template = '<SegmentTemplate media="seg-$Number$.m4s" initialization="init.mp4"/>'
    seg_list = '<SegmentList><SegmentURL media="s1.m4s"/></SegmentList>'
    base = f"<BaseURL>{V.replace('&', '&amp;')}</BaseURL>"
    return {
        "two_periods": mpd([video()], [audio()], period_count=2),
        "no_period": mpd([video()], [audio()], period_count=0),
        "segment_template_in_representation": mpd([video(extra=template)], [audio()]),
        "segment_template_in_adaptation": mpd([], [audio()], extra_adaptations=adaptation("video", [video()],
                                                                                         extra=template)),
        "segment_list": mpd([video(extra=seg_list)], [audio()]),
        "relative_base_url": mpd([video(url="video/stream.mp4")], [audio()]),
        "relative_audio_base_url": mpd([video()], [audio(url="a.mp4")]),
        "base_url_in_period": mpd([video()], [audio()], period_extra=base),
        "base_url_in_mpd": mpd([video()], [audio()], extra_xml=base),
        "base_url_in_adaptation": mpd([], [audio()], extra_adaptations=adaptation("video", [video()], extra=base)),
        "two_audio_adaptations": mpd([video()], [audio()], extra_adaptations=adaptation("audio", [audio()])),
        "two_video_adaptations": mpd([video()], [audio()], extra_adaptations=adaptation("video", [video()])),
        "unknown_adaptation": mpd([video()], [audio()], extra_adaptations=(
            '<AdaptationSet contentType="text"><Representation id="t" bandwidth="1"/></AdaptationSet>')),
        "dynamic": mpd([video()], [audio()], mpd_type="dynamic"),
        "other_root": mpd([video()], [audio()], root="Manifest"),
        "other_namespace": mpd([video()], [audio()], namespace="urn:fake:outro"),
        "bad_duration": mpd([video()], [audio()], duration="P1D"),
        "empty_duration": mpd([video()], [audio()], duration="PT"),
    }


@pytest.mark.parametrize("name", sorted(_unsupported_cases()))
def test_unsupported_form(name):
    assert outcome(_unsupported_cases()[name]) == "unsupported_form"


def test_no_usable_audio():
    assert outcome(mpd([video()], [])) == "no_usable_audio"
    assert outcome(mpd([video()], [audio(codecs="opus")])) == "no_usable_audio"
    assert outcome(mpd([video()], [audio(mime="audio/webm")])) == "no_usable_audio"


@pytest.mark.parametrize("rep", [
    dict(codecs="av01.0.08M.08"),
    dict(codecs="hev1.1.6.L93.B0"),
    dict(url="https://evil.example.com/v.mp4"),
    dict(url="https://fbcdn.net/v.mp4"),
    dict(url="http://video-fake.fbcdn.net/v.mp4"),
    dict(url="https://video-fake.fbcdn.net:8443/v.mp4"),
    dict(url="https://user:pass@video-fake.fbcdn.net/v.mp4"),
    dict(url="https://127.0.0.1/v.mp4"),
    dict(url=None),
    dict(segment_base=False),
    dict(mime="video/webm"),
    dict(bandwidth="rápido"),
    dict(width=None),
    dict(height="0"),
    dict(width="1080.5"),
    dict(frame_rate="trinta"),
    dict(frame_rate="30/0"),
])
def test_no_usable_video(rep):
    assert outcome(mpd([video(**rep)], [audio()])) == "no_usable_video"


@pytest.mark.parametrize("rep", [
    dict(width="1" + "0" * 14), dict(height="9" * 400), dict(width=16385), dict(bandwidth="1" + "0" * 14),
    dict(bandwidth="9" * 400), dict(bandwidth=0), dict(content_length="1" + "0" * 14), dict(content_length="9" * 400),
    dict(frame_rate="999999999999999"), dict(frame_rate="100000/1"), dict(frame_rate="9" * 400),
])
def test_absurd_numbers_make_the_representation_unusable(rep):
    """Nada é baixado para ser reprovado depois: a representação fora de faixa nem é escolhida."""
    assert outcome(mpd([video(**rep)], [audio()])) == "no_usable_video"
    mixed = mpd([video(**{**rep, "rep_id": "ruim"}), video(width=540, height=960, rep_id="ok")], [audio()])
    selection, parse = select_dash_streams(mixed)
    assert (selection.video.width, parse.video_representations) == (540, 1)


@pytest.mark.parametrize("rep", [dict(bandwidth="9" * 400), dict(content_length="1" + "0" * 14)])
def test_absurd_numbers_in_the_audio(rep):
    assert outcome(mpd([video()], [audio(**rep)])) == "no_usable_audio"


@pytest.mark.parametrize("text", ["PT" + "9" * 400 + "S", "PT1e400S", "PT9999999S", "PT1." + "9" * 400 + "S",
                                  "PT" + "9" * 400 + "H", "PT999H", "PT0S", "PT0.000S", "PT99999M1S"])
def test_absurd_or_zero_duration_refuses_the_manifest(text):
    """Antes, centenas de dígitos viravam `inf` com outcome ok (e JSON inválido adiante)."""
    assert outcome(mpd([video()], [audio()], duration=text)) == "unsupported_form"


def test_limits_accept_the_edge_values():
    selection, _ = select_dash_streams(mpd([video(width=16384, height=16384, bandwidth=10 ** 9, frame_rate="1000",
                                                  content_length=2 ** 40)], [audio()], duration="PT48H"))
    assert (selection.video.width, selection.video.content_length, selection.duration) == (16384, 2 ** 40, 172800.0)


def test_no_video_adaptation_set():
    assert outcome(mpd([], [audio()])) == "no_usable_video"


def test_two_base_urls_in_one_representation_make_it_unusable():
    extra = f"<BaseURL>{dash_url(CODE, 'v2').replace('&', '&amp;')}</BaseURL>"
    assert outcome(mpd([video(extra=extra)], [audio()])) == "no_usable_video"


def test_custom_url_validator_is_used_and_its_errors_never_escape():
    def refuse(url: str) -> str:
        raise RuntimeError("recusado: " + url)

    assert select_dash_streams(mpd([video()], [audio()]), url_validator=refuse)[1].outcome == "no_usable_video"
    seen: list[str] = []
    selection, _ = select_dash_streams(mpd([video()], [audio()]), url_validator=lambda url: seen.append(url) or url)
    assert seen == [V, A] and selection.video.url == V


# --------------------------------------------------------------------------- nada sensível no resultado seguro


def test_parse_result_and_stream_repr_carry_no_url():
    selection, parse = select_dash_streams(mpd([video()], [audio()]))
    for text in (repr(parse), str(parse), repr(selection), repr(selection.video), repr(selection.audio)):
        assert "https://" not in text and "fbcdn" not in text and CODE not in text and "FAKE_OH" not in text
    assert HEAAC in repr(selection.audio)
    for manifest in (mpd([video(url="https://evil.example.com/v.mp4")], [audio()]), "<MPD", mpd([video()], [])):
        text = repr(select_dash_streams(manifest)[1])
        assert "https://" not in text and "example" not in text
