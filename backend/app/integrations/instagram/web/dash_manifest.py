"""`video_dash_manifest` (MPD) do nó público de uma publicação -> a maior representação de vídeo e o áudio.

VERIFICADO no Instagram real (2026-10-10, navegador anônimo, UM Reel público; só a LEITURA do manifesto): raiz
``MPD`` no namespace DASH, ``type="static"``, perfil on-demand, 1 ``Period``, 1 ``AdaptationSet`` de vídeo com 9
``Representation`` VP9 (``vp09.*``) em ``video/mp4`` e 1 de áudio com 1 ``Representation`` ``mp4a.40.5`` em
``audio/mp4``; cada ``Representation`` com UMA ``BaseURL`` https absoluta (arquivo fMP4 inteiro), ``SegmentBase``,
``bandwidth``, ``FBContentLength`` e, no vídeo, ``width``/``height``/``frameRate`` em fração.

NÃO VERIFICADO: o download de uma ``BaseURL`` e a junção vídeo+áudio NUNCA rodaram contra o CDN real (o navegador
usa ``Range``; não se sabe se o CDN entrega o arquivo inteiro num GET simples). H.264 (``avc1``/``avc3``) no
manifesto é aceito por inferência, nunca foi observado. Manifesto sem áudio, com vários ``Period``, com
``SegmentTemplate``/``SegmentList``, com ``BaseURL`` relativa ou fora de ``Representation``, ``type="dynamic"`` ou com
mais de um ``AdaptationSet`` do mesmo tipo: formas não observadas, recusadas aqui (quem chama usa o MP4 direto, sem
erro e sem requisição).

Nada do manifesto (URLs, assinaturas, ids) vai para log, exceção ou ``DashParse``.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass

from app.services.cdn_downloader import validate_cdn_url

MAX_MANIFEST_BYTES = 512 * 1024
DASH_NS = "urn:mpeg:dash:schema:mpd:2011"
ACCEPTED_VIDEO_CODEC_PREFIXES = ("vp09.", "avc1.", "avc3.")
ACCEPTED_AUDIO_CODEC_PREFIXES = ("mp4a.40.",)
# Faixas plausíveis: número fora delas é manifesto hostil ou corrompido. Representação fora da faixa é ignorada (nada
# é baixado para ser reprovado depois); duração fora da faixa recusa o manifesto inteiro.
MAX_DIMENSION = 16384
MAX_FPS = 1000.0
MAX_BANDWIDTH = 1_000_000_000
MAX_CONTENT_LENGTH = 2 ** 40
MAX_DURATION = 48 * 3600.0

_NS = f"{{{DASH_NS}}}"
# `&` só como uma das cinco entidades predefinidas ou referência numérica; `<!` (DOCTYPE, ENTITY, CDATA, comentário)
# é recusado antes de parsear: sem declaração de entidade não há expansão.
_UNSAFE_AMP_RE = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#[0-9]{1,7}|#x[0-9a-fA-F]{1,6});)")
# Dígitos limitados: um número de centenas de dígitos viraria `inf` (JSON inválido adiante).
_DURATION_RE = re.compile(r"PT(?:([0-9]{1,3})H)?(?:([0-9]{1,5})M)?(?:([0-9]{1,6}(?:\.[0-9]{1,9})?)S)?")
_INT_RE = re.compile(r"[0-9]{1,15}")
_DIGITS_RE = re.compile(r"[0-9]+")
_FORBIDDEN_TAGS = (f"{_NS}SegmentTemplate", f"{_NS}SegmentList")


@dataclass(frozen=True)
class DashStream:
    url: str
    codecs: str
    bandwidth: int
    content_length: int | None
    width: int | None = None
    height: int | None = None
    fps: float | None = None

    def __repr__(self) -> str:
        return (f"DashStream(codecs={self.codecs!r}, bandwidth={self.bandwidth}, width={self.width}, "
                f"height={self.height}, fps={self.fps}, url=<redacted>)")


@dataclass(frozen=True)
class DashSelection:
    video: DashStream
    audio: DashStream
    duration: float | None


@dataclass
class DashParse:
    """Resultado seguro para log (sem URL nem ids). ``outcome``: ok, absent, too_large, unsafe_xml, xml_error,
    unsupported_form, no_usable_video, no_usable_audio."""

    outcome: str = "absent"
    video_representations: int = 0
    audio_representations: int = 0
    chosen_height: int | None = None
    chosen_fps: int | None = None


class _Unsupported(Exception):
    pass


def _int(value: object) -> int | None:
    return int(value) if isinstance(value, str) and _INT_RE.fullmatch(value) else None


def _fps(value: str) -> float | None:
    """"n/d" ou "n" (inteiros positivos); qualquer outra coisa -> None."""
    num, sep, den = value.partition("/")
    n, d = _int(num), (_int(den) if sep else 1)
    if not n or not d:
        return None
    return n / d


def _duration(value: str | None) -> float | None:
    if value is None:
        return None
    match = _DURATION_RE.fullmatch(value)
    if match is None or not any(match.groups()):
        raise _Unsupported
    hours, minutes, seconds = match.groups()
    total = int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds or 0)
    if not math.isfinite(total) or not 0 < total <= MAX_DURATION:
        raise _Unsupported
    return total


def _content_length(value: str | None) -> tuple[int | None, bool]:
    """(bytes declarados ou None se ausente/não numérico, plausível?). Número absurdo -> não plausível."""
    if value is None or not _DIGITS_RE.fullmatch(value):
        return None, True
    if len(value) > 15 or int(value) > MAX_CONTENT_LENGTH:
        return None, False
    return int(value), True


def _kind(adaptation: ET.Element) -> str | None:
    content = adaptation.get("contentType")
    if content is None:
        content = (adaptation.get("mimeType") or "").partition("/")[0]
    return content if content in ("video", "audio") else None


def _streams(adaptation: ET.Element, kind: str, url_validator: Callable[[str], str]) -> list[DashStream]:
    """Representações utilizáveis do AdaptationSet (as demais são ignoradas). BaseURL relativa -> ``_Unsupported``."""
    prefixes = ACCEPTED_VIDEO_CODEC_PREFIXES if kind == "video" else ACCEPTED_AUDIO_CODEC_PREFIXES
    result: list[DashStream] = []
    for rep in adaptation.findall(f"{_NS}Representation"):
        urls = rep.findall(f"{_NS}BaseURL")
        text = (urls[0].text or "").strip() if len(urls) == 1 else ""
        if text and not text.lower().startswith(("https://", "http://")):
            raise _Unsupported
        mime = rep.get("mimeType") or adaptation.get("mimeType")
        codecs = rep.get("codecs") or adaptation.get("codecs") or ""
        bandwidth = _int(rep.get("bandwidth"))
        content_length, plausible = _content_length(rep.get("FBContentLength"))
        if (not text or mime != f"{kind}/mp4" or not codecs.startswith(prefixes) or bandwidth is None
                or not 0 < bandwidth <= MAX_BANDWIDTH or not plausible
                or rep.find(f"{_NS}SegmentBase") is None):
            continue
        try:
            url_validator(text)
        except Exception:  # noqa: BLE001 - URL recusada: representação não utilizável
            continue
        width = height = None
        fps = None
        if kind == "video":
            width, height = _int(rep.get("width")), _int(rep.get("height"))
            if not width or not height or width > MAX_DIMENSION or height > MAX_DIMENSION:
                continue
            rate = rep.get("frameRate")
            if rate is not None:
                fps = _fps(rate)
                if fps is None or fps > MAX_FPS:
                    continue
        result.append(DashStream(url=text, codecs=codecs, bandwidth=bandwidth, content_length=content_length,
                                 width=width, height=height, fps=fps))
    return result


def _select(manifest: str, url_validator: Callable[[str], str], parse: DashParse) -> DashSelection | None:
    root = ET.fromstring(manifest)
    if root.tag != f"{_NS}MPD" or root.get("type") not in (None, "static"):
        raise _Unsupported
    periods = root.findall(f"{_NS}Period")
    if len(periods) != 1 or any(element.tag in _FORBIDDEN_TAGS for element in root.iter()):
        raise _Unsupported
    duration = _duration(root.get("mediaPresentationDuration"))
    adaptations = periods[0].findall(f"{_NS}AdaptationSet")
    representations = [rep for adaptation in adaptations for rep in adaptation.findall(f"{_NS}Representation")]
    in_representations = sum(len(rep.findall(f"{_NS}BaseURL")) for rep in representations)
    if in_representations != sum(1 for _ in root.iter(f"{_NS}BaseURL")):
        raise _Unsupported   # BaseURL fora de Representation (MPD, Period, AdaptationSet)
    by_kind: dict[str, list[ET.Element]] = {"video": [], "audio": []}
    for adaptation in adaptations:
        kind = _kind(adaptation)
        if kind is None:
            raise _Unsupported
        by_kind[kind].append(adaptation)
    if len(by_kind["video"]) > 1 or len(by_kind["audio"]) > 1:
        raise _Unsupported
    videos = _streams(by_kind["video"][0], "video", url_validator) if by_kind["video"] else []
    audios = _streams(by_kind["audio"][0], "audio", url_validator) if by_kind["audio"] else []
    parse.video_representations, parse.audio_representations = len(videos), len(audios)
    if not videos:
        parse.outcome = "no_usable_video"
        return None
    if not audios:
        parse.outcome = "no_usable_audio"
        return None
    # Maior área, depois mais quadros por segundo, depois maior bitrate; empate total fica com a primeira.
    video = max(videos, key=lambda s: (s.width * s.height, s.fps or 0.0, s.bandwidth))
    audio = max(audios, key=lambda s: s.bandwidth)
    parse.outcome = "ok"
    parse.chosen_height = video.height
    parse.chosen_fps = round(video.fps) if video.fps else None
    return DashSelection(video=video, audio=audio, duration=duration)


def select_dash_streams(manifest: object, *, url_validator: Callable[[str], str] = validate_cdn_url
                        ) -> tuple[DashSelection | None, DashParse]:
    """Escolhe o vídeo de maior área (depois fps, depois bitrate) e o áudio de maior bitrate do manifesto.

    Nunca levanta exceção e nunca faz requisição: tudo o que não é a forma observada vira ``(None, DashParse)`` com
    o ``outcome`` do motivo. O download das URLs devolvidas e a junção NUNCA rodaram contra o CDN real."""
    parse = DashParse()
    if not isinstance(manifest, str) or not manifest.strip():
        return None, parse
    try:
        if len(manifest) > MAX_MANIFEST_BYTES or len(manifest.encode("utf-8", "replace")) > MAX_MANIFEST_BYTES:
            parse.outcome = "too_large"
            return None, parse
        if "<!" in manifest or _UNSAFE_AMP_RE.search(manifest):
            parse.outcome = "unsafe_xml"
            return None, parse
        return _select(manifest, url_validator, parse), parse
    except ET.ParseError:
        outcome = "xml_error"
    except Exception:  # noqa: BLE001 - forma não observada: quem chama usa o MP4 direto
        outcome = "unsupported_form"
    return None, DashParse(outcome=outcome)
