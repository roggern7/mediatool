"""Baixar ZIP: onde fica o arquivo temporário, checagem de espaço, nomes e limpeza.

O ZIP é uma variante da exportação (`exports.mode = 'zip'`): mesma origem, mesma consulta de itens e mesmo executor.
O arquivo é gerado em `<data>\\exports-tmp\\` (pasta EXCLUSIVA do servidor, fora de `storage`, ignorada pelo git):
`export-<16 hex>.zip.part` durante a preparação, renomeado para `.zip` ao ficar pronto. Os originais só são lidos.

Segurança da pasta: só se opera numa pasta REAL dentro de `<data>` (nunca junção, symlink ou outro reparse point), só
em arquivos regulares cujo nome casa exatamente com o padrão gerado pelo servidor e cujo caminho resolvido fica
dentro dela. Qualquer outro arquivo que apareça ali não é tocado.

Limpeza: o ZIP pronto vale por `ZIP_READY_TTL_SECONDS` (24 h) e, depois do primeiro download completo, por no
máximo `ZIP_AFTER_DOWNLOAD_TTL_SECONDS` (1 h, contada do fim da transmissão). Cancelamento e falha removem o `.part`
na hora. Vencido: o arquivo é removido e SÓ ENTÃO o registro vira `expired`; se a remoção falhar (arquivo em uso por
um download em andamento), o registro continua elegível e a próxima varredura tenta de novo. A varredura (a cada
10 min e no startup) também remove arquivos com o padrão do servidor que não pertencem a nenhum registro vivo e, com
o disco apertado para os downloads, antecipa a remoção de ZIPs que JÁ foram baixados.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import stat
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.db.connection import utc_now
from app.services import disk

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.services.context import AppContext

logger = logging.getLogger("media_tool.exports")

ZIP_DIR_NAME = "exports-tmp"
ZIP_READY_TTL_SECONDS = 24 * 3600.0
ZIP_AFTER_DOWNLOAD_TTL_SECONDS = 3600.0
SWEEP_INTERVAL_SECONDS = 600.0
# Margem sobre a soma dos originais: cabeçalhos por entrada e diretório central do ZIP.
SPACE_MARGIN_RATIO = 0.01
SPACE_MARGIN_BYTES = 64 * 1024 * 1024
PART_SUFFIX = ".part"
_ZIP_FILE = re.compile(r"^export-[0-9a-f]{16}\.zip$")
_ZIP_OR_PART = re.compile(r"^export-[0-9a-f]{16}\.zip(\.part)?$")
_NAME_PREFIX = {"library": "biblioteca"}
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def zip_dir(settings: Settings) -> Path:
    return Path(settings.data_dir) / ZIP_DIR_NAME


def _is_real_dir(path: Path) -> bool:
    """Diretório de verdade: não é symlink, junção nem outro reparse point."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
        return False
    return stat.S_ISDIR(info.st_mode) and not path.is_symlink() and not path.is_junction()


def safe_zip_dir(settings: Settings, *, create: bool = False) -> Path | None:
    """A pasta de temporários, só se for um diretório REAL dentro de `<data>`. None: não existe ou não é confiável
    (junção/symlink/arquivo, ou resolve para fora de `<data>`): nesse caso nada é criado, servido nem apagado."""
    folder = zip_dir(settings)
    if create and not os.path.lexists(folder):
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
    if not _is_real_dir(folder):
        return None
    try:
        if folder.resolve() != Path(settings.data_dir).resolve() / ZIP_DIR_NAME:
            return None
    except (OSError, RuntimeError):
        return None
    return folder


def new_zip_fields(kind: str, *, now: datetime | None = None, content: str = "originals",
                   edit_batch: bool = False) -> dict[str, str]:
    """Nome interno aleatório, nome de download (ASCII) e token de download da exportação. Só versões editadas:
    `editados-...`; lote de Edição com originais (ou os dois): `edicao-...`."""
    moment = now or datetime.now()
    prefix = ("editados" if content == "edited" else "edicao" if edit_batch
              else _NAME_PREFIX.get(kind, "importacao"))
    return {
        "zip_path": f"export-{secrets.token_hex(8)}.zip",
        "zip_name": f"{prefix}-{moment:%Y-%m-%d-%H%M}.zip",
        "zip_token": secrets.token_urlsafe(32),
    }


def zip_file(settings: Settings, export: dict[str, Any], *, part: bool = False, create_dir: bool = False) -> Path | None:
    """Caminho do ZIP daquele registro. None se o nome gravado não é EXATAMENTE um nome gerado pelo servidor, se a
    pasta de temporários não é confiável, ou se o caminho resolvido não fica dentro dela."""
    name = export.get("zip_path") or ""
    if not isinstance(name, str) or not _ZIP_FILE.match(name):
        return None
    folder = safe_zip_dir(settings, create=create_dir)
    if folder is None:
        return None
    path = folder / (name + PART_SUFFIX if part else name)
    try:
        if path.resolve().parent != folder.resolve():
            return None
    except (OSError, RuntimeError):
        return None
    return path


def _is_regular_file(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def servable_zip(settings: Settings, export: dict[str, Any]) -> Path | None:
    """O ZIP pronto daquele registro, se existe como arquivo regular dentro da pasta de temporários."""
    path = zip_file(settings, export)
    return path if path is not None and _is_regular_file(path) else None


def remove_zip_files(settings: Settings, export: dict[str, Any]) -> bool:
    """Remove o ZIP e o `.part` DAQUELE registro. Retorna True se, ao final, nenhum dos dois existe (False: a
    remoção falhou, por exemplo com o arquivo em uso por um download, ou a pasta não é confiável)."""
    name = export.get("zip_path") or ""
    if not isinstance(name, str) or not _ZIP_FILE.match(name):
        return True   # o registro não aponta para um arquivo do servidor: não há o que remover
    if not os.path.lexists(zip_dir(settings)):
        return True
    gone = True
    for part in (True, False):
        path = zip_file(settings, export, part=part)
        if path is None:
            return False   # pasta não confiável: nada é apagado e o registro continua elegível
        if not os.path.lexists(path):
            continue
        if not _is_regular_file(path):
            gone = False
            continue
        try:
            os.unlink(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.info("ZIP temporário ainda em uso, fica para a próxima varredura (%s): %s", path.name,
                        exc.strerror or type(exc).__name__)
            gone = False
    return gone


def free_bytes(path: Path) -> int:
    """Espaço livre do volume da pasta (sobe até um ancestral existente); -1 se não foi possível medir."""
    target = Path(path)
    while not target.exists() and target.parent != target:
        target = target.parent
    try:
        return int(shutil.disk_usage(target).free)
    except OSError:
        return -1


def required_bytes(total_size: int) -> int:
    return int(total_size) + int(total_size * SPACE_MARGIN_RATIO) + SPACE_MARGIN_BYTES


def reserve_bytes(settings: Settings) -> int:
    """Reserva que o app já exige para os downloads (lida de `services.disk`, sem alterar): vale quando o temporário
    do ZIP fica no mesmo volume do storage, para a criação do ZIP não levar o disco abaixo do mínimo dos downloads."""
    try:
        same_volume = os.stat(Path(settings.storage_dir).anchor or settings.storage_dir).st_dev == os.stat(
            Path(settings.data_dir).anchor or settings.data_dir).st_dev
    except OSError:
        same_volume = True
    return disk.disk_ok(download_only=True)[2] if same_volume else 0


def format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            text = f"{amount:.0f}" if unit in ("B", "KB") else f"{amount:.1f}".replace(".", ",")
            return f"{text} {unit}"
        amount /= 1024
    return f"{value} B"


def queued_bytes(ctx: AppContext, *, exclude_id: int | None = None) -> int:
    """Reserva lógica da fila: bytes que os ZIPs `pending`/`running` ainda vão escrever (o que o `.part` em
    preparação já ocupa não conta de novo: já saiu do espaço livre)."""
    total = 0
    for export in ctx.exports.open_zips():
        if export["id"] == exclude_id:
            continue
        needed = required_bytes(ctx.exports.sum_bytes(export))
        part = zip_file(ctx.settings, export, part=True)
        try:
            written = os.path.getsize(part) if part is not None else 0
        except OSError:
            written = 0
        total += max(0, needed - written)
    return total


def space_problem(ctx: AppContext, export: dict[str, Any], *, include_queue: bool = True) -> str | None:
    """Mensagem de erro se não há espaço para o ZIP temporário desta exportação (None = há espaço, ou não foi
    possível medir). Conta: soma agregada de `file_size` + margem, contra o espaço livre menos a reserva dos
    downloads e (na criação) menos o que os ZIPs já na fila ainda vão escrever."""
    free = free_bytes(zip_dir(ctx.settings))
    if free < 0:
        return None
    needed = required_bytes(ctx.exports.sum_bytes(export))
    reserve = reserve_bytes(ctx.settings)
    queued = queued_bytes(ctx, exclude_id=export["id"]) if include_queue else 0
    available = max(0, free - reserve - queued)
    if available >= needed:
        return None
    notes = []
    if reserve:
        notes.append(f"o Media Tool mantém {format_bytes(reserve)} livres para os downloads")
    if queued:
        notes.append(f"{format_bytes(queued)} já estão reservados para ZIPs na fila")
    kept = f" ({'; '.join(notes)})" if notes else ""
    return (
        f"Sem espaço em disco para preparar o ZIP: são necessários {format_bytes(needed)} e há"
        f" {format_bytes(available)} disponíveis{kept}. Libere espaço ou use “Exportar para pasta” em outro disco."
    )


def sweep(ctx: AppContext, *, startup: bool = False) -> int:
    """Varredura da pasta de temporários (startup e a cada 10 min). Retorna quantos arquivos/registros limpou.

    1. ZIPs vencidos: remove o arquivo e só então marca `expired` (remoção que falha fica para a próxima vez).
    2. Disco apertado para os downloads: antecipa a remoção de ZIPs prontos que JÁ foram baixados (nunca os ainda
       não baixados).
    3. Órfãos: arquivos regulares com o padrão do servidor (`export-<16 hex>.zip[.part]`) sem registro vivo.
    Nunca toca em outro arquivo, nunca segue junção/symlink e nunca sai de `<data>\\exports-tmp`."""
    del startup   # mesma varredura nos dois momentos
    removed = 0
    folder_exists = os.path.lexists(zip_dir(ctx.settings))
    folder = safe_zip_dir(ctx.settings)
    if folder_exists and folder is None:
        logger.warning("Pasta de ZIPs temporários não é um diretório real dentro de data: nada foi limpo")
        return 0
    candidates = list(ctx.exports.expired_zips(utc_now()))
    if not disk.disk_ok(download_only=True)[0]:
        seen = {export["id"] for export in candidates}
        candidates += [export for export in ctx.exports.downloaded_zips() if export["id"] not in seen]
    for export in candidates:
        if remove_zip_files(ctx.settings, export):
            ctx.exports.set_expired(export["id"])
            removed += 1
    if folder is not None:
        alive = ctx.exports.alive_zip_names()
        keep = alive | {name + PART_SUFFIX for name in alive}
        try:
            entries = list(os.scandir(folder))
        except OSError:
            entries = []
        for entry in entries:
            if entry.name in keep or not _ZIP_OR_PART.match(entry.name):
                continue
            path = folder / entry.name
            if not _is_regular_file(path):
                continue
            try:
                os.unlink(path)
                removed += 1
            except OSError:
                pass
    if removed:
        logger.info("ZIPs temporários limpos: %s", removed)
    return removed
