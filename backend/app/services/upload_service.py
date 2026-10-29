"""Upload de vídeos locais para a biblioteca (POST /api/library/uploads).

Um arquivo por requisição, em multipart/form-data no campo `file`. O corpo é lido em streaming (sem
python-multipart: parser mínimo abaixo) e gravado em `temp/upload-<uuid>/upload.bin`, contando bytes até
`settings.max_download_bytes`. Depois, em threadpool: sha256 -> duplicata? -> ffprobe (só vídeo) -> promoção do
original sob `ctx.ingest_lock` -> registro em `media` com `source_platform='local'` -> thumbnail (best-effort).
A pasta temporária é apagada em qualquer desfecho; restos de uma queda são removidos na partida
(`cleanup_orphan_temps`, que só preserva `job-<id>` de jobs ativos).

O nome enviado pelo cliente nunca vira caminho: vai só para `original_filename`, saneado.
"""

from __future__ import annotations

import errno
import logging
import re
import shutil
import subprocess
import threading
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from media_processor import FFmpegNotFoundError, InvalidMediaError, MediaProcessor, ProbeResult, sha256_file
from media_processor.probe import video_stream
from media_processor.tools import popen_kwargs
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect, Request

from app.core.errors import AppError
from app.services import disk
from app.services.context import AppContext
from app.services.storage import mime_for

logger = logging.getLogger("media_tool.uploads")

UPLOAD_PATH = "/api/library/uploads"
UPLOAD_TEMP_PREFIX = "upload-"
MAX_CONCURRENT_UPLOADS = 2
FIELD_NAME = "file"
# Cabeçalhos de cada parte e campos que não são o arquivo: pequenos por definição.
MAX_PART_HEADERS = 16 * 1024
MAX_OTHER_FIELDS = 64 * 1024
# Folga para boundary, cabeçalhos, outras partes e epílogo: o corpo inteiro (Content-Length declarado ou lido em
# streaming) não passa de max_download_bytes + MULTIPART_OVERHEAD.
MULTIPART_OVERHEAD = 256 * 1024
WRITE_CHUNK = 1024 * 1024
MAX_FILENAME = 255
# Removidos do nome: controle (Cc), formatação/bidi como U+202E (Cf), substitutos, uso privado, não atribuídos e
# separadores de linha/parágrafo.
_DROP_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_DISK_FULL_ERRNOS = frozenset({errno.ENOSPC})
_DISK_FULL_WINERRORS = frozenset({39, 112})  # ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL
# Integridade: decodifica só os últimos segundos (arquivo truncado ou com o fim zerado falha ali).
INTEGRITY_TAIL_SECONDS = 3.0
INTEGRITY_TIMEOUT = 120.0
UNSUPPORTED_FORMAT = "Formato não suportado. Envie vídeos MP4, MOV, WebM ou MKV."


def payload_too_large(max_bytes: int) -> AppError:
    limit = f"{max_bytes / (1024 ** 3):.1f} GB".replace(".", ",") if max_bytes >= 1024 ** 3 else f"{max_bytes} bytes"
    return AppError(413, "payload_too_large", f"Arquivo maior que o limite de {limit}.")


def invalid_upload(detail: str = "Envie um arquivo no campo file (multipart/form-data).") -> AppError:
    return AppError(422, "invalid_upload", detail)


def invalid_media() -> AppError:
    return AppError(422, "invalid_media", "O arquivo enviado não é um vídeo válido.")


def integrity_timeout() -> AppError:
    return AppError(422, "integrity_timeout",
                    "Não foi possível verificar o arquivo a tempo. Tente um arquivo menor ou com mais quadros-chave.")


def unsupported_media() -> AppError:
    return AppError(422, "unsupported_media", "Só vídeos podem ser editados.")


def ffmpeg_missing() -> AppError:
    return AppError(503, "ffmpeg_missing", "FFmpeg/ffprobe não encontrado; configure o caminho em Configurações.")


def disk_full(free: int | None = None, required: int | None = None) -> AppError:
    extra = {"free_bytes": free, "required_bytes": required} if free is not None else None
    return AppError(507, "disk_full",
                    "Pouco espaço livre em disco para receber o arquivo. Libere espaço e tente de novo.", extra=extra)


def _is_disk_full(exc: OSError) -> bool:
    return exc.errno in _DISK_FULL_ERRNOS or getattr(exc, "winerror", None) in _DISK_FULL_WINERRORS


class UploadSlots:
    """No máximo `limit` uploads simultâneos; sem fila (o excedente recebe 429)."""

    def __init__(self, limit: int = MAX_CONCURRENT_UPLOADS) -> None:
        self.limit = limit
        self._active = 0
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            if self._active >= self.limit:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)


def sanitize_filename(name: str | None) -> str:
    """Só o último componente, sem caracteres de controle, até 255 caracteres. Nunca usado como caminho."""
    base = re.split(r"[\\/]", name or "")[-1]
    base = unicodedata.normalize("NFC", base)
    base = "".join(ch for ch in base if unicodedata.category(ch) not in _DROP_CATEGORIES)
    base = base.strip().strip(".").strip()
    return base[:MAX_FILENAME] or "video"


def _boundary(content_type: str | None) -> bytes:
    if not content_type:
        raise invalid_upload()
    kind, _, params = content_type.partition(";")
    if kind.strip().lower() != "multipart/form-data":
        raise invalid_upload()
    match = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', params, re.IGNORECASE)
    if match is None:
        raise invalid_upload()
    boundary = (match.group(1) or match.group(2)).encode("latin-1", "replace")
    if not 1 <= len(boundary) <= 200:
        raise invalid_upload()
    return boundary


def _disposition(headers: bytes) -> tuple[str | None, str | None]:
    """(name, filename) do Content-Disposition de uma parte."""
    text = headers.decode("utf-8", "replace")
    for line in text.split("\r\n"):
        key, _, value = line.partition(":")
        if key.strip().lower() != "content-disposition":
            continue
        name = re.search(r'(?:^|;)\s*name="((?:[^"\\]|\\.)*)"', value)
        filename = re.search(r'(?:^|;)\s*filename="((?:[^"\\]|\\.)*)"', value)
        if filename is None:
            filename = re.search(r"(?:^|;)\s*filename=([^;]+)", value)
        unescape = lambda s: re.sub(r"\\(.)", r"\1", s)  # noqa: E731
        return (unescape(name.group(1)) if name else None,
                unescape(filename.group(1)).strip() if filename else None)
    return None, None


class _MultipartFileReader:
    """Parser incremental de multipart/form-data que grava a parte `file` em `target`.

    Exige exatamente uma parte `file` com nome de arquivo; outras partes são descartadas (até MAX_OTHER_FIELDS).
    """

    def __init__(self, boundary: bytes, target: Path, max_bytes: int) -> None:
        self.delimiter = b"--" + boundary
        self.body_delimiter = b"\r\n" + self.delimiter
        self.target = target
        self.max_bytes = max_bytes
        self.buffer = b""
        self.state = "preamble"
        self.in_file = False
        self.file_seen = False
        self.filename: str | None = None
        self.written = 0
        self.other_bytes = 0
        self.pending = bytearray()
        self.handle = None

    async def _write(self, data: bytes, *, flush: bool = False) -> None:
        if data:
            self.written += len(data)
            if self.written > self.max_bytes:
                raise payload_too_large(self.max_bytes)
            self.pending += data
        if self.pending and (flush or len(self.pending) >= WRITE_CHUNK):
            chunk, self.pending = bytes(self.pending), bytearray()
            await run_in_threadpool(self.handle.write, chunk)

    async def feed(self, chunk: bytes) -> None:
        self.buffer += chunk
        while True:
            if self.state == "preamble":
                index = self.buffer.find(self.delimiter)
                if index < 0:
                    if len(self.buffer) > MAX_PART_HEADERS:
                        raise invalid_upload()
                    return
                after = self.buffer[index + len(self.delimiter):]
                if len(after) < 2:
                    return
                if after[:2] != b"\r\n":
                    raise invalid_upload()
                self.buffer = after[2:]
                self.state = "headers"
            elif self.state == "headers":
                index = self.buffer.find(b"\r\n\r\n")
                if index < 0:
                    if len(self.buffer) > MAX_PART_HEADERS:
                        raise invalid_upload()
                    return
                name, filename = _disposition(self.buffer[:index])
                self.buffer = self.buffer[index + 4:]
                self.in_file = name == FIELD_NAME and filename is not None
                if self.in_file:
                    if self.file_seen:
                        raise invalid_upload("Envie um arquivo por requisição.")
                    self.file_seen = True
                    self.filename = filename
                    self.handle = await run_in_threadpool(open, self.target, "wb")
                self.state = "body"
            elif self.state == "body":
                index = self.buffer.find(self.body_delimiter)
                if index < 0:
                    keep = len(self.body_delimiter) + 1
                    if len(self.buffer) > keep:
                        await self._consume(self.buffer[:-keep])
                        self.buffer = self.buffer[-keep:]
                    return
                after = self.buffer[index + len(self.body_delimiter):]
                if len(after) < 2:
                    return
                await self._consume(self.buffer[:index])
                if self.in_file:
                    await self._write(b"", flush=True)
                    await run_in_threadpool(self.handle.close)
                    self.handle = None
                    self.in_file = False
                if after[:2] == b"--":
                    self.buffer = b""
                    self.state = "done"
                    return
                if after[:2] != b"\r\n":
                    raise invalid_upload()
                self.buffer = after[2:]
                self.state = "headers"
            else:  # done: epílogo ignorado
                self.buffer = b""
                return

    async def _consume(self, data: bytes) -> None:
        if self.in_file:
            await self._write(data)
        else:
            self.other_bytes += len(data)
            if self.other_bytes > MAX_OTHER_FIELDS:
                raise invalid_upload()

    def close(self) -> None:
        if self.handle is not None:
            try:
                self.handle.close()
            except OSError:
                pass
            self.handle = None


def _check_ready(ctx: AppContext) -> None:
    """Antes de ler o corpo: FFmpeg/ffprobe disponíveis e disco suficiente (mesma verificação dos downloads)."""
    processors = ctx.processors
    if not (processors.ffprobe_info().found and processors.ffmpeg_info().found):
        raise ffmpeg_missing()
    check = getattr(ctx.worker, "disk_check", None) or disk.disk_ok
    ok, free, required = check()
    if not ok:
        raise disk_full(free, required)


async def receive_upload(ctx: AppContext, request: Request, slots: UploadSlots) -> tuple[dict[str, Any], bool]:
    """Recebe o corpo em streaming e registra o vídeo. Devolve (mídia, duplicada)."""
    max_bytes = int(ctx.settings.max_download_bytes)
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > max_bytes + MULTIPART_OVERHEAD:
                raise payload_too_large(max_bytes)
        except ValueError:
            raise invalid_upload() from None
    boundary = _boundary(request.headers.get("content-type"))
    if not slots.try_acquire():
        raise AppError(429, "too_many_uploads", "Já há 2 envios em andamento; aguarde um terminar.")
    temp_dir = ctx.storage.temp / f"{UPLOAD_TEMP_PREFIX}{uuid.uuid4().hex}"
    try:
        await run_in_threadpool(_check_ready, ctx)
        await run_in_threadpool(temp_dir.mkdir, parents=True, exist_ok=False)
        target = temp_dir / "upload.bin"
        reader = _MultipartFileReader(boundary, target, max_bytes)
        total = 0
        try:
            async for chunk in request.stream():
                # Teto do corpo inteiro (também sem Content-Length): partes extras e epílogo contam.
                total += len(chunk)
                if total > max_bytes + MULTIPART_OVERHEAD:
                    raise payload_too_large(max_bytes)
                await reader.feed(chunk)
        except ClientDisconnect:
            # Cliente cancelou ou fechou a aba: resposta para ninguém, sem erro no log.
            logger.info("Upload interrompido pelo cliente; temporário descartado")
            raise AppError(400, "client_disconnected", "Envio interrompido.") from None
        finally:
            await run_in_threadpool(reader.close)
        if reader.state != "done" or not reader.file_seen:
            raise invalid_upload()
        if reader.written == 0:
            raise invalid_media()
        return await run_in_threadpool(ingest_file, ctx, target, sanitize_filename(reader.filename))
    except OSError as exc:
        if _is_disk_full(exc):
            logger.warning("Upload recusado: disco cheio durante a gravação; temporário descartado")
            raise disk_full() from None
        raise
    finally:
        await run_in_threadpool(shutil.rmtree, temp_dir, True)
        slots.release()


def container_extension(probe: ProbeResult) -> str:
    """Extensão pelo container real (nunca pelo nome enviado). Só MP4/MOV e Matroska/WebM são aceitos: GIF e
    outros formatos de imagem animada ou de vídeo legado (AVI, TS, FLV...) são recusados como `unsupported_media`."""
    names = set((probe.format_name or "").lower().split(","))
    if names & {"mov", "mp4"}:
        brand = str((probe.tags or {}).get("major_brand", "")).strip().lower()
        return ".mov" if brand == "qt" else ".mp4"
    if names & {"matroska", "webm"}:
        webm = (probe.video_codec or "").lower() in {"vp8", "vp9", "av1"} and (
            (probe.audio_codec or "").lower() in {"", "opus", "vorbis"})
        return ".webm" if webm else ".mkv"
    raise AppError(422, "unsupported_media", UNSUPPORTED_FORMAT)


def _video_duration(probe: ProbeResult) -> float | None:
    """Duração do stream de vídeo (o fim do vídeo pode vir antes do fim do áudio); senão a do container."""
    stream = video_stream(probe.raw or {}) or {}
    try:
        value = float(stream.get("duration"))
    except (TypeError, ValueError):
        return probe.duration
    return value if value > 0 else probe.duration


# Avisos de referência que o decodificador imprime logo depois do salto quando o ponto de entrada não é um IDR
# (open-GOP, intra-refresh, quadros RASL do HEVC): o vídeo inteiro decodifica sem erro, então não reprovam.
_SEEK_REFERENCE_WARNINGS = re.compile(
    r"mmco: unref short failure"
    r"|number of reference frames \(\d+\+\d+\) exceeds max"
    r"|co located POCs unavailable"
    r"|reference picture missing during reorder"
    r"|missing reference picture"
    r"|illegal short term buffer state detected"
    r"|could not find ref with poc"
    r"|error constructing the frame rps",
    re.IGNORECASE,
)
_LOG_PREFIX = re.compile(r"^(?:\[[^\]]*\]\s*)+")
_REPEATED = re.compile(r"^last message repeated \d+ times?$", re.IGNORECASE)


def integrity_errors(stderr: str) -> list[str]:
    """Linhas de erro do FFmpeg que reprovam o arquivo: tudo, menos os avisos de referência depois do salto (e as
    repetições deles). Erros de leitura/demux ("partial file", "Invalid data found"...) e de decodificação de dados
    ("Invalid NAL unit size", "missing picture in access unit", erros do AAC...) continuam reprovando."""
    errors: list[str] = []
    previous_benign = False
    for raw in stderr.splitlines():
        line = _LOG_PREFIX.sub("", raw.strip()).strip()
        if not line:
            continue
        if _REPEATED.match(line):
            if not previous_benign:
                errors.append(line)
            continue
        previous_benign = bool(_SEEK_REFERENCE_WARNINGS.search(line))
        if not previous_benign:
            errors.append(line)
    return errors


def check_decodable(ffmpeg: str, path: Path, duration: float | None) -> None:
    """Integridade barata e determinística: decodifica (vídeo e o primeiro áudio) do quadro-chave anterior aos
    últimos INTEGRITY_TAIL_SECONDS da duração do vídeo até o fim, com `-v error`. Reprova (422 invalid_media) com
    código de saída diferente de 0, nenhum quadro decodificado ou qualquer erro que não seja aviso de referência
    logo depois do salto (`integrity_errors`). Pega arquivo truncado (o índice aponta para dados que não existem) e
    dados zerados/corrompidos no trecho decodificado; dano só no meio do arquivo, antes desse trecho, não é
    detectado (decodificar tudo custaria minutos em arquivos de 2 GB). Prazo estourado -> 422 integrity_timeout."""
    seek = ["-ss", f"{duration - INTEGRITY_TAIL_SECONDS:.3f}"] if duration and duration > INTEGRITY_TAIL_SECONDS else []
    command = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", *seek, "-i", str(path), "-map", "0:v:0",
               "-map", "0:a:0?", "-f", "null", "-progress", "pipe:1", "-nostats", "-"]
    try:
        result = subprocess.run(command, capture_output=True, timeout=INTEGRITY_TIMEOUT, **popen_kwargs())
    except FileNotFoundError:
        raise ffmpeg_missing() from None
    except subprocess.TimeoutExpired:
        # Não é prova de arquivo inválido (ex.: GOP único muito longo obriga a decodificar quase tudo).
        logger.warning("Upload recusado: verificação de integridade passou de %ss", INTEGRITY_TIMEOUT)
        raise integrity_timeout() from None
    frames = 0
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "frame" and value.strip().isdigit():
            frames = int(value.strip())
    errors = integrity_errors(result.stderr.decode("utf-8", "replace"))
    if result.returncode != 0 or errors or frames == 0:
        logger.info("Upload recusado na verificação de integridade (rc %s, %s quadros): %s",
                    result.returncode, frames, " | ".join(errors)[:300])
        raise invalid_media()


_EXTENSION_FAMILY = {".mp4": "MP4", ".m4v": "MP4", ".mov": "MP4", ".webm": "MKV", ".mkv": "MKV"}


def check_name_matches(filename: str, ext: str) -> None:
    """Nome com extensão de vídeo conhecida precisa ser da mesma família do container real (MP4/MOV ou
    Matroska/WebM): um arquivo disfarçado (ex.: MKV renomeado para .mp4) é recusado. Sem extensão ou com extensão
    desconhecida, vale o container."""
    declared = Path(filename).suffix.lower()
    family = _EXTENSION_FAMILY.get(declared)
    if family is not None and family != _EXTENSION_FAMILY[ext]:
        raise AppError(422, "unsupported_media",
                       f"O conteúdo do arquivo não corresponde à extensão {declared}. "
                       "Renomeie com a extensão certa ou converta para MP4.")


def ingest_file(ctx: AppContext, path: Path, filename: str) -> tuple[dict[str, Any], bool]:
    """Registra `path` (dentro de temp/upload-*) como original local. Síncrono (threadpool)."""
    sha256 = sha256_file(path)
    size = path.stat().st_size
    existing = ctx.media.find_by_sha(sha256)
    if existing is not None:
        if existing["media_type"] != "video":
            raise unsupported_media()
        logger.info("Upload idêntico à media %s (sha256); temporário descartado", existing["id"])
        return existing, True

    try:
        probe = ctx.processors.get().probe(path)
    except FFmpegNotFoundError:
        raise ffmpeg_missing() from None
    except InvalidMediaError as exc:
        if isinstance(exc.__cause__, OSError):
            raise ffmpeg_missing() from None
        raise invalid_media() from None
    if probe.media_type != "video":
        raise unsupported_media()
    if not probe.has_video or not probe.width or not probe.height:
        raise invalid_media()
    ext = container_extension(probe)
    check_name_matches(filename, ext)
    processor = ctx.processors.get()
    # Só com o MediaProcessor real (um processor injetado nos testes não tem binário para decodificar).
    if isinstance(processor, MediaProcessor):
        ffmpeg = ctx.processors.ffmpeg_info().path
        if not ffmpeg:
            raise ffmpeg_missing()
        check_decodable(ffmpeg, path, _video_duration(probe))

    storage = ctx.storage
    with ctx.ingest_lock:
        existing = ctx.media.find_by_sha(sha256)
        if existing is not None:
            return existing, True
        original = storage.promote_original(path, sha256, ext)
        try:
            media_id = ctx.media.insert(
                {
                    "source_platform": "local",
                    "source_profile": None,
                    "source_url": None,
                    "source_post_id": None,
                    "source_media_index": 0,
                    "post_id": None,
                    "media_id_remote": None,
                    "media_type": "video",
                    "original_filename": filename,
                    "original_path": storage.relative(original),
                    "original_sha256": sha256,
                    "file_size": size,
                    "duration": probe.duration,
                    "width": probe.width,
                    "height": probe.height,
                    "fps": probe.fps,
                    "has_audio": int(probe.has_audio),
                    "video_codec": probe.video_codec,
                    "mime_type": mime_for(original),
                    "taken_at": None,
                }
            )
        except BaseException:
            original.unlink(missing_ok=True)
            raise
    logger.info("Upload local registrado (media %s, sha %s)", media_id, sha256[:12])
    _make_thumbnail(ctx, media_id, original, path.parent)
    return ctx.media.get(media_id), False


def _make_thumbnail(ctx: AppContext, media_id: int, original: Path, temp_dir: Path) -> None:
    storage = ctx.storage
    temp = temp_dir / f"thumb-{media_id}.jpg"
    try:
        ctx.processors.get().make_thumbnail(original, temp)
        target = storage.promote_thumbnail(temp, media_id)
    except Exception as exc:  # noqa: BLE001 - falha de thumbnail não falha o upload
        logger.warning("Thumbnail não gerada para media %s: %s", media_id,
                       getattr(exc, "code", exc.__class__.__name__))
        return
    if target.is_file():
        ctx.media.update(media_id, thumbnail_path=storage.relative(target))
