"""Fixtures do `video_dash_manifest` (MPD) do documento público. Só valores FAKE; nenhuma rede.

``mpd`` gera XML fiel ao observado no real em 2026-10-10 (namespace DASH, perfil on-demand, ``type="static"``, um
``Period``, ``AdaptationSet`` por ``contentType``, ``Representation`` com ``BaseURL`` + ``SegmentBase``). O manifesto
real saneado (URLs, ids e identificador trocados) está em ``fixtures/instagram_web/dash/observed_sanitized.mpd``.
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from post_document_fixtures import media_node

OBSERVED_MPD = Path(__file__).parent / "fixtures" / "instagram_web" / "dash" / "observed_sanitized.mpd"
DASH_HOST = "https://video-fake.fbcdn.net/v/dash"
VP9 = "vp09.00.40.08.00.01.01.01.00"
HEAAC = "mp4a.40.5"


def dash_url(code: str, name: str) -> str:
    """URL FAKE de um fluxo separado; ``name`` = "v" (vídeo) ou "a" (áudio). ``FAKE_OH`` troca na renovação."""
    return f"{DASH_HOST}/{code}/{name}.mp4?efg=FAKE&oh=FAKE_OH&oe=00000000"


def _attrs(values: dict) -> str:
    return "".join(f" {key}={quoteattr(str(value))}" for key, value in values.items() if value is not None)


def representation(*, url: str | None, kind: str = "video", codecs: str | None = None, bandwidth: object = 1000,
                   width: object = 1080, height: object = 1920, frame_rate: str | None = "15360/512",
                   content_length: object = None, mime: str | None = None, segment_base: bool = True,
                   rep_id: str = "1v", raw_url: str | None = None, extra: str = "") -> str:
    """Uma ``Representation``. ``url=None`` omite a ``BaseURL``; ``raw_url`` entra sem escapar (XML inseguro)."""
    attrs = {"id": rep_id, "bandwidth": bandwidth,
             "codecs": codecs if codecs is not None else (VP9 if kind == "video" else HEAAC),
             "mimeType": mime if mime is not None else f"{kind}/mp4", "FBContentLength": content_length}
    if kind == "video":
        attrs.update(width=width, height=height, frameRate=frame_rate, FBQualityLabel="1080p")
    else:
        attrs.update(audioSamplingRate="44100")
    body = ""
    if raw_url is not None:
        body += f"<BaseURL>{raw_url}</BaseURL>"
    elif url is not None:
        body += f"<BaseURL>{escape(url)}</BaseURL>"
    if segment_base:
        body += '<SegmentBase indexRange="818-1017" timescale="15360"><Initialization range="0-817"/></SegmentBase>'
    return f"<Representation{_attrs(attrs)}>{body}{extra}</Representation>"


def adaptation(kind: str, representations: list[str], *, content_type: bool = True, extra: str = "") -> str:
    attrs = {"id": "0" if kind == "video" else "1", "contentType": kind if content_type else None,
             "subsegmentAlignment": "true"}
    return f"<AdaptationSet{_attrs(attrs)}>{extra}{''.join(representations)}</AdaptationSet>"


def mpd(video_reps: list[str], audio_reps: list[str], *, duration: str | None = "PT2.000000S", period_count: int = 1,
        mpd_type: str | None = "static", extra_xml: str = "", period_extra: str = "",
        extra_adaptations: str = "", root: str = "MPD", namespace: str = "urn:mpeg:dash:schema:mpd:2011") -> str:
    """Manifesto com um AdaptationSet de vídeo (se houver representações) e um de áudio (idem)."""
    sets = (adaptation("video", video_reps) if video_reps else "") + (adaptation("audio", audio_reps) if audio_reps
                                                                       else "") + extra_adaptations
    period = f'<Period id="0">{period_extra}{sets}</Period>'
    attrs = {"xmlns": namespace, "profiles": "urn:mpeg:dash:profile:isoff-on-demand:2011", "minBufferTime": "PT2S",
             "type": mpd_type, "mediaPresentationDuration": duration}
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<{root}{_attrs(attrs)}>{extra_xml}{period * period_count}'
            f"</{root}>")


def simple_mpd(code: str, *, width: int = 1080, height: int = 1920, video_bytes: int | None = None,
               audio_bytes: int | None = None, duration: str | None = "PT2.000000S", codecs: str = VP9,
               frame_rate: str | None = "30") -> str:
    """Um vídeo + um áudio com as URLs de ``dash_url`` (o que os testes ponta a ponta servem pelo CDN simulado)."""
    return mpd(
        [representation(url=dash_url(code, "v"), width=width, height=height, content_length=video_bytes,
                        codecs=codecs, frame_rate=frame_rate, bandwidth=3279228)],
        [representation(url=dash_url(code, "a"), kind="audio", content_length=audio_bytes, bandwidth=87372,
                        rep_id="1a")],
        duration=duration)


def media_node_with_dash(code: str, manifest: object, *, original: tuple[int, int] | None = (1080, 1920),
                         owner: str = "Dono_Fake") -> dict:
    """Nó de Reel fiel ao observado (``post_document_fixtures.media_node``) + ``video_dash_manifest``."""
    node = media_node("video", code, owner)
    node["video_dash_manifest"] = manifest
    node["is_dash_eligible"] = 1
    node["number_of_qualities"] = 9
    if original is None:
        node.pop("original_width", None)
        node.pop("original_height", None)
    else:
        node["original_width"], node["original_height"] = original
    return node
