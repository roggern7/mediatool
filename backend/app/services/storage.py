"""Layout do storage (contrato, seção 8) e operações de arquivo seguras.

- Originais: `originals/<yyyy>/<mm>/<sha[:2]>/<sha>.<ext>`, promovidos de temp por rename atômico, nunca sobrescritos.
- Processados: `processed/<yyyy>/<mm>/<media_id>_<job_id>_<sha8>.<ext>`.
- Thumbs: `thumbs/<media_id % 256>/<media_id>.jpg` (fatiado; os antigos `thumbs/<media_id>.jpg` são migrados no
  startup). Temporários por job: `temp/job-<id>/`.
Caminhos guardados no banco são relativos à raiz do storage, em formato POSIX.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

logger = logging.getLogger("media_tool.storage")

ORIGINAL_EXTENSIONS = frozenset(
    {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif", ".h264"}
)
MIME_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".gif": "image/gif",
    ".h264": "video/h264",
}
JOB_TEMP_PREFIX = "job-"
QUARANTINE_MARK = ".quarantine-"
# Nome gerado pelo sistema para processados: <media_id>_<job_id>_<sha8>[_n].<ext>
PROCESSED_NAME = re.compile(r"^\d+_\d+_[0-9a-f]{8}(?:_\d+)?\.[a-z0-9]{2,5}$")
_HASH_CHUNK = 1024 * 1024
THUMB_SHARDS = 256
_FLAT_THUMB = re.compile(r"^(\d+)\.jpg$")


class StorageError(Exception):
    pass


class FileBusyError(StorageError):
    """Arquivo em uso por outro processo (não pôde ser movido/removido)."""


def normalize_extension(suffix: str | None, media_type: str) -> str:
    ext = (suffix or "").lower()
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    if ext == ".jpeg":
        ext = ".jpg"
    if ext in ORIGINAL_EXTENSIONS:
        return ext
    return ".mp4" if media_type == "video" else ".jpg"


def mime_for(path: str | Path) -> str:
    return MIME_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def has_media_signature(path: Path) -> bool:
    """Magic bytes de mídia aceita: JPEG, PNG, GIF, WebP, MP4/MOV/HEIC (ISO BMFF) e WebM/Matroska."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return False
    if head.startswith(b"\xff\xd8\xff") or head.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if head.startswith((b"GIF87a", b"GIF89a")) or head.startswith(b"\x1a\x45\xdf\xa3"):
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    if head[4:8] not in (b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip"):
        return False
    # ISO BMFF: os 4 primeiros bytes são o tamanho da primeira caixa (0 = até o fim, 1 = tamanho estendido). Texto
    # que por acaso tenha "free"/"skip" nos bytes 4-8 não passa.
    box = int.from_bytes(head[:4], "big")
    try:
        size = path.stat().st_size
    except OSError:
        return False
    return box in (0, 1) or 8 <= box <= size


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _move_no_overwrite(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.rename(src, dst)  # no Windows falha com FileExistsError se o destino existe
    else:
        os.link(src, dst)  # falha se o destino existe; depois remove a origem
        os.unlink(src)


@dataclass
class StagedDeletion:
    """Remoção em duas fases: arquivos são renomeados (falha cedo se em uso) e só apagados após o commit do banco."""

    moved: list[tuple[Path, Path]] = field(default_factory=list)

    def commit(self) -> list[str]:
        problems: list[str] = []
        for _original, staged in self.moved:
            try:
                staged.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Não foi possível apagar %s: %s", staged.name, exc.strerror or exc)
                problems.append(staged.name)
        self.moved.clear()
        return problems

    def rollback(self) -> None:
        for original, staged in reversed(self.moved):
            try:
                os.rename(staged, original)
            except OSError as exc:
                logger.error("Falha ao restaurar %s: %s", original.name, exc.strerror or exc)
        self.moved.clear()


class Storage:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.originals = self.root / "originals"
        self.processed = self.root / "processed"
        self.temp = self.root / "temp"
        self.thumbs = self.root / "thumbs"

    def ensure_dirs(self) -> None:
        for directory in (self.originals, self.processed, self.temp, self.thumbs):
            directory.mkdir(parents=True, exist_ok=True)

    def dirs(self) -> dict[str, str]:
        return {
            "originals": str(self.originals),
            "processed": str(self.processed),
            "temp": str(self.temp),
            "thumbs": str(self.thumbs),
        }

    # ---- caminhos ----------------------------------------------------------------------------------------------
    def relative(self, path: Path) -> str:
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.root):
            raise StorageError("Caminho fora do storage")
        return resolved.relative_to(self.root).as_posix()

    def resolve(self, relative: str) -> Path:
        """Resolve um caminho relativo do banco garantindo que fica dentro do storage."""
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or not pure.parts:
            raise StorageError("Caminho relativo inválido")
        candidate = (self.root / Path(*pure.parts)).resolve()
        if not candidate.is_relative_to(self.root):
            raise StorageError("Caminho fora do storage")
        return candidate

    def job_temp_dir(self, job_id: int, *, fresh: bool = False) -> Path:
        directory = self.temp / f"{JOB_TEMP_PREFIX}{int(job_id)}"
        if fresh and directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def remove_job_temp(self, job_id: int) -> None:
        directory = self.temp / f"{JOB_TEMP_PREFIX}{int(job_id)}"
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)

    def thumb_path(self, media_id: int) -> Path:
        return self.thumbs / str(int(media_id) % THUMB_SHARDS) / f"{int(media_id)}.jpg"

    def migrate_flat_thumbs(self) -> int:
        """Move `thumbs/<id>.jpg` para `thumbs/<id % 256>/<id>.jpg` (idempotente; nada é sobrescrito). Cópia plana
        idêntica a uma já migrada (migração interrompida) é apagada. Retorna quantas foram movidas."""
        if not self.thumbs.is_dir():
            return 0
        moved = 0
        for entry in self.thumbs.iterdir():
            match = _FLAT_THUMB.match(entry.name)
            if match is None or entry.is_symlink() or not entry.is_file():
                continue
            target = self.thumb_path(int(match.group(1)))
            try:
                if os.path.lexists(target):
                    if target.is_file() and target.read_bytes() == entry.read_bytes():
                        entry.unlink()
                    continue
                _move_no_overwrite(entry, target)
                moved += 1
            except OSError as exc:
                logger.warning("Thumbnail %s não migrada: %s", entry.name, exc.strerror or exc)
        return moved

    # ---- promoção ----------------------------------------------------------------------------------------------
    def promote_original(self, src: Path, sha256: str, ext: str, when: datetime | None = None) -> Path:
        """Promove o temp para o caminho final. Se já existe um arquivo lá (órfão de um crash entre a promoção e o
        registro no banco): conteúdo idêntico é adotado e o temp descartado; conteúdo diferente vai para quarentena.
        Nada é sobrescrito."""
        moment = when or datetime.now(UTC)
        dst = self.originals / f"{moment:%Y}" / f"{moment:%m}" / sha256[:2] / f"{sha256}{ext}"
        if os.path.lexists(dst):
            if dst.is_file() and not dst.is_symlink() and file_sha256(dst) == sha256:
                logger.info("Original órfão com o mesmo conteúdo adotado (%s)", dst.name)
                Path(src).unlink(missing_ok=True)
                return dst
            quarantined = self.quarantine(dst)
            logger.warning("Original existente com conteúdo divergente movido para quarentena: %s", quarantined.name)
        _move_no_overwrite(src, dst)
        return dst

    def quarantine(self, path: Path) -> Path:
        """Renomeia o arquivo para `<nome>.quarantine-<timestamp>-<id>` na mesma pasta (nunca apaga)."""
        target = path.with_name(f"{path.name}{QUARANTINE_MARK}{int(time.time())}-{uuid.uuid4().hex[:6]}")
        os.rename(path, target)
        return target

    def promote_thumbnail(self, src: Path, media_id: int) -> Path:
        """Move a thumbnail gerada em temp para `thumbs/<media_id>.jpg`; se já existir, mantém a existente."""
        dst = self.thumb_path(media_id)
        try:
            _move_no_overwrite(src, dst)
        except FileExistsError:
            Path(src).unlink(missing_ok=True)
        return dst

    def promote_processed(
        self, src: Path, media_id: int, job_id: int, sha256: str, ext: str, when: datetime | None = None
    ) -> Path:
        moment = when or datetime.now(UTC)
        directory = self.processed / f"{moment:%Y}" / f"{moment:%m}"
        dst = directory / f"{int(media_id)}_{int(job_id)}_{sha256[:8]}{ext}"
        counter = 1
        while dst.exists():
            counter += 1
            dst = directory / f"{int(media_id)}_{int(job_id)}_{sha256[:8]}_{counter}{ext}"
        _move_no_overwrite(src, dst)
        return dst

    # ---- limpeza -----------------------------------------------------------------------------------------------
    def cleanup_orphan_temps(self, keep: set[str]) -> int:
        """Remove entradas de `temp/` que não pertencem a jobs ativos. Nunca sai de `temp/`."""
        if not self.temp.is_dir():
            return 0
        removed = 0
        for entry in self.temp.iterdir():
            if entry.name in keep or entry.name == ".gitkeep":
                continue
            try:
                if entry.is_junction():
                    os.rmdir(entry)  # remove só a junção, nunca o alvo
                elif entry.is_symlink():
                    entry.unlink()  # remove só o link, nunca o alvo
                elif entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                removed += 1
            except OSError as exc:
                logger.warning("Temporário órfão não removido (%s): %s", entry.name, exc.strerror or exc)
        return removed

    def remove_orphan_processed(self, referenced: set[str], *, older_than: float) -> int:
        """Remove de `processed/` arquivos com nome gerado pelo sistema que nenhuma rendition referencia e que
        são anteriores a `older_than` (timestamp). Arquivos com outros nomes nunca são tocados."""
        if not self.processed.is_dir():
            return 0
        removed = 0
        for path in self.processed.rglob("*"):
            if not path.is_file() or path.is_symlink() or not PROCESSED_NAME.match(path.name):
                continue
            if self.relative(path) in referenced:
                continue
            try:
                if path.stat().st_mtime >= older_than:
                    continue
                path.unlink()
                removed += 1
                logger.warning("Processado órfão removido: %s", path.name)
            except OSError as exc:
                logger.warning("Processado órfão não removido (%s): %s", path.name, exc.strerror or exc)
        return removed

    def stage_deletion(self, paths: list[Path]) -> StagedDeletion:
        """Renomeia os arquivos existentes para nomes de descarte. Se algum estiver em uso, desfaz e falha."""
        staged = StagedDeletion()
        allowed = [self.originals.resolve(), self.processed.resolve(), self.thumbs.resolve()]
        for path in paths:
            resolved = Path(path).resolve()
            if not any(resolved != base and resolved.is_relative_to(base) for base in allowed):
                staged.rollback()
                raise StorageError("Caminho fora das pastas de mídia do storage")
            if not os.path.lexists(resolved):
                continue
            if not resolved.is_file():
                staged.rollback()
                raise StorageError("Só arquivos podem ser removidos")
            target = resolved.with_name(f"{resolved.name}.deleting-{uuid.uuid4().hex[:8]}")
            try:
                os.rename(resolved, target)
            except OSError as exc:
                staged.rollback()
                raise FileBusyError(resolved.name) from exc
            staged.moved.append((resolved, target))
        return staged
