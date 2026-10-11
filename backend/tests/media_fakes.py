"""Fakes para os testes do backend-core (sem rede real; processor falso para testes rápidos de fila)."""

from __future__ import annotations

import hashlib
import shutil
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

from media_processor import (
    FFmpegNotFoundError,
    InvalidMediaError,
    ProbeResult,
    ProcessingCancelledError,
    ProcessResult,
    ToolInfo,
    sha256_file,
)

from app.integrations.instagram.downloader import (
    InstagramDownloader,
    InstagramError,
    ListingCheckpoint,
    ListingRef,
    ProfileInfo,
    RemoteMedia,
    SessionTestResult,
)
from app.integrations.instagram.session import InstagramSessionProvider, SessionStatus

FAKE_SESSION = "FAKE-SESSION-0000000000"
# Prefixos com magic bytes reais (JPEG / MP4) para os bytes sintéticos passarem na checagem de assinatura.
MAGIC = {"image": b"\xff\xd8\xff\xe0", "video": b"\x00\x00\x00\x18ftypisom"}


def remote(shortcode: str, index: int = 0, media_type: str = "image", profile: str = "perfil_teste") -> RemoteMedia:
    ext = "mp4" if media_type == "video" else "jpg"
    return RemoteMedia(
        source_profile=profile,
        source_post_id=shortcode,
        post_id=f"post-{shortcode}",
        media_id=f"m-{shortcode}-{index}",
        source_media_index=index,
        media_type=media_type,
        is_reel=False,
        source_url=f"https://www.instagram.com/p/{shortcode}/",
        media_url=f"https://scontent.cdninstagram.com/v/{shortcode}_{index}.{ext}",
        thumbnail_url=None,
        width=None,
        height=None,
        duration=None,
        taken_at="2026-01-01T00:00:00Z",
        extension=ext,
    )


class MemorySessionProvider(InstagramSessionProvider):
    def __init__(self, configured: bool = True):
        self._configured = configured

    def status(self) -> SessionStatus:
        return SessionStatus(configured=self._configured, source="file" if self._configured else None)

    def get(self):
        return None

    def save(self, sessionid: str, extra: dict[str, str] | None = None) -> None:
        self._configured = True

    def clear(self) -> None:
        self._configured = False


@dataclass
class FakeInstagramDownloader(InstagramDownloader):
    """Lista itens configurados e "baixa" escrevendo bytes sintéticos (ou copiando arquivos reais) no destino."""

    items: list[RemoteMedia] = field(default_factory=list)
    listing_error: InstagramError | None = None
    listing_error_after: int | None = None  # levanta o erro depois de N itens
    download_errors: dict[str, list[Exception]] = field(default_factory=dict)  # chave "shortcode:index"
    content: Callable[[RemoteMedia], bytes] | None = None
    source_files: dict[str, Path] = field(default_factory=dict)  # chave "shortcode:index" -> arquivo real
    block: threading.Event | None = None  # se definido, download espera o evento (ou cancelamento)
    downloads: list[str] = field(default_factory=list)
    listings: int = 0
    page_size: int = 12  # itens por página da listagem falsa (checkpoint ao fim de cada página)
    page_requests: list[int] = field(default_factory=list)  # páginas "pedidas" (1-based), em ordem
    refreshes: list[int] = field(default_factory=list)  # páginas renovadas via refresh_page
    refreshable: bool = False  # False: comportamento padrão do contrato (content_unavailable)
    session_result: SessionTestResult | None = None

    def test_session(self) -> SessionTestResult:
        return self.session_result or SessionTestResult(ok=True, code="ok", message="Sessão válida.", username="conta")

    def get_profile(self, username: str) -> ProfileInfo:
        if self.listing_error is not None and self.listing_error_after is None:
            raise self.listing_error
        return ProfileInfo(username=username, user_id="1", full_name="Perfil Teste", avatar_url=None,
                           post_count=len(self.items), is_private=False, is_verified=False)

    def iter_profile_media(self, username, content, limit, *, cancel=None, resume=None,
                           on_checkpoint=None) -> Iterator[RemoteMedia]:
        """Lista em páginas de `page_size`; retoma de `resume.next_page_index`; checkpoint ao fim de cada página."""
        self.listings += 1
        produced = 0
        size = self.page_size
        last_page = max(1, -(-len(self.items) // size))
        page = resume.next_page_index if resume is not None else 1
        pages = resume.pages if resume is not None else 0
        seen = resume.posts_seen if resume is not None else 0
        while page <= last_page:
            if cancel is not None and cancel.is_set():
                return
            if limit is not None and produced >= limit:
                return
            self.page_requests.append(page)
            cursor = None if page == 1 else f"FAKE-CURSOR-{page}"
            chunk = self.items[(page - 1) * size: page * size]
            for item in chunk:
                if self.listing_error is not None and self.listing_error_after is not None \
                        and produced >= self.listing_error_after:
                    raise self.listing_error
                if cancel is not None and cancel.is_set():
                    return
                if limit is not None and produced >= limit:
                    return
                produced += 1
                yield replace(item, listing_ref=ListingRef(page_index=page, cursor=cursor, page_size=size))
            pages += 1
            seen += len(chunk)
            has_next = page < last_page
            if on_checkpoint is not None:
                on_checkpoint(ListingCheckpoint(
                    resume_cursor=f"FAKE-CURSOR-{page + 1}" if has_next else None, next_page_index=page + 1,
                    pages=pages, posts_seen=seen, items_compatible=seen, unproductive_streak=0, has_next=has_next,
                ))
            page += 1
        if self.listing_error is not None:
            if self.listing_error_after is None or produced >= self.listing_error_after:
                raise self.listing_error

    def refresh_page(self, profile, ref, *, cancel=None) -> list[RemoteMedia]:
        """Devolve de novo os itens da página `ref` (mesmas URLs falsas) quando `refreshable`."""
        if not self.refreshable:
            return super().refresh_page(profile, ref, cancel=cancel)
        self.refreshes.append(ref.page_index)
        size = ref.page_size
        return [replace(item, listing_ref=ref) for item in self.items[(ref.page_index - 1) * size: ref.page_index * size]]

    def resolve_post(self, shortcode: str, *, cancel=None, path_kind=None) -> list[RemoteMedia]:
        return [item for item in self.items if item.source_post_id == shortcode]

    def download(self, item: RemoteMedia, dest_dir: Path, *, cancel=None, progress=None) -> Path:
        key = f"{item.source_post_id}:{item.source_media_index}"
        self.downloads.append(key)
        errors = self.download_errors.get(key)
        if errors:
            raise errors.pop(0)
        if self.block is not None:
            while not self.block.wait(0.02):
                if cancel is not None and cancel.is_set():
                    raise InstagramError("download_failed", "cancelado", retryable=False)
        if cancel is not None and cancel.is_set():
            raise ProcessingCancelledError("cancelado")
        dest_dir.mkdir(parents=True, exist_ok=True)
        if key in self.source_files:
            src = self.source_files[key]
            target = dest_dir / f"download{src.suffix}"
            shutil.copyfile(src, target)
        else:
            ext = ".mp4" if item.media_type == "video" else ".jpg"
            data = self.content(item) if self.content else MAGIC[item.media_type] + f"FAKE-{item.media_type}-{key}".encode()
            target = dest_dir / f"download{ext}"
            target.write_bytes(data)
        if progress is not None:
            size = target.stat().st_size
            progress(size, size)
        return target


class FakeProcessor:
    """Imita o MediaProcessor sem FFmpeg: arquivos com b"FAKE-video" no início são vídeo, b"FAKE-image" imagem."""

    def __init__(self, *, ffmpeg_found: bool = True, fail_process: Exception | None = None,
                 block: threading.Event | None = None):
        self.ffmpeg_found = ffmpeg_found
        self.fail_process = fail_process
        self.block = block
        self.processed: list[Path] = []
        self.thumbnails: list[Path] = []

    def _tool(self, name: str) -> ToolInfo:
        if self.ffmpeg_found:
            return ToolInfo(found=True, path=f"C:/fake/{name}.exe", version="fake", error=None)
        return ToolInfo(found=False, path=None, version=None, error="não encontrado")

    def ffmpeg_info(self) -> ToolInfo:
        return self._tool("ffmpeg")

    def ffprobe_info(self) -> ToolInfo:
        return self._tool("ffprobe")

    def probe(self, path: Path) -> ProbeResult:
        if not self.ffmpeg_found:
            raise FFmpegNotFoundError("ffprobe não encontrado")
        head = Path(path).read_bytes()[:32]
        if b"FAKE-video" in head or b"PROC-video" in head:
            kind = "video"
        elif b"FAKE-image" in head or b"PROC-image" in head:
            kind = "image"
        else:
            raise InvalidMediaError("arquivo inválido")
        return ProbeResult(
            media_type=kind, format_name="fake", duration=2.0 if kind == "video" else None, width=64, height=48,
            fps=25.0 if kind == "video" else None, has_audio=kind == "video", has_video=True, video_codec="fake",
            audio_codec="fake" if kind == "video" else None, rotation=0, tags={}, stream_tags=[], raw={},
        )

    def output_suffix(self, probe: ProbeResult, opts) -> str:
        return ".mp4" if probe.media_type == "video" else ".jpg"

    def process(self, src, dst, opts, *, progress=None, cancel=None, timeout=None) -> ProcessResult:
        if not self.ffmpeg_found:
            raise FFmpegNotFoundError("ffmpeg não encontrado")
        if self.fail_process is not None:
            raise self.fail_process
        if self.block is not None:
            while not self.block.wait(0.02):
                if cancel is not None and cancel.is_set():
                    raise ProcessingCancelledError("cancelado")
        if cancel is not None and cancel.is_set():
            raise ProcessingCancelledError("cancelado")
        data = Path(src).read_bytes()
        out = data.replace(b"FAKE-", b"PROC-", 1) + hashlib.sha256(repr(opts).encode()).digest()
        Path(dst).write_bytes(out)
        if progress is not None:
            progress(0.5)
            progress(1.0)
        self.processed.append(Path(src))
        return ProcessResult(output_path=Path(dst), probe=self.probe(dst), sha256=sha256_file(Path(dst)),
                             size=len(out), command=["fake"], forced_reencode=False, elapsed=0.0)

    def make_thumbnail(self, src: Path, dst: Path, *, width: int = 320) -> Path:
        if not self.ffmpeg_found:
            raise FFmpegNotFoundError("ffmpeg não encontrado")
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
        self.thumbnails.append(Path(dst))
        return Path(dst)
