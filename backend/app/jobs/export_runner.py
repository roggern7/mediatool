"""Executor das exportações para pasta: uma thread própria, UMA exportação por vez (as demais esperam `pending`).

Não usa o pool de download/FFmpeg. Lê as mídias em páginas (keyset por id), copia cada original em blocos para um
temporário `.<nome>.<id da exportação>.<aleatório>.mtpart` na pasta de destino e renomeia ao final, sem nunca
sobrescrever. Exportar é COPIAR: o original no storage só é aberto para leitura. O progresso vai para o banco em lotes
(não a cada arquivo).

Regra dos temporários: a exportação só remove arquivos que ESTA execução criou. O temporário é criado com abertura
exclusiva (se o nome existir, outro nome é sorteado; nada é apagado) e fica registrado em memória; só os registrados
são removidos. Depois de um reinício nada é apagado por padrão de nome: um `.mtpart` pode sobrar na pasta de destino
(arquivo oculto por começar com ponto, terminado em `.mtpart`) e pode ser apagado à mão.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import secrets
import struct
import threading
import time
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.db.connection import utc_now
from app.db.repositories.exports import COUNT_COLUMNS
from app.services import export_time, export_zip
from app.services.export_destination import (
    DestinationError,
    file_identity,
    io_path,
    remove_if_empty,
    validate_destination,
)
from app.services.export_names import export_extension, export_stem, file_name
from app.services.storage import StorageError

if TYPE_CHECKING:
    from app.services.context import AppContext

logger = logging.getLogger("media_tool.exports")

PAGE_SIZE = 500
CHUNK_SIZE = 1024 * 1024
# Progresso gravado a cada N arquivos ou a cada intervalo, o que vier primeiro.
PROGRESS_EVERY_FILES = 200
PROGRESS_EVERY_SECONDS = 1.0
MAX_NAME_ATTEMPTS = 1000
MAX_TEMP_ATTEMPTS = 20
TEMP_SUFFIX = ".mtpart"
# Muitas falhas seguidas indicam destino com problema: a exportação para em vez de falhar milhares de arquivos.
MAX_CONSECUTIVE_FAILURES = 25
ZIP_MIN_YEAR, ZIP_MAX_YEAR = 1980, 2107
IDLE_WAIT_SECONDS = 2.0
_DISK_FULL_WINERRORS = (39, 112)  # ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL

CANCELLED_MESSAGE = (
    "Exportação cancelada. Os arquivos já copiados continuam na pasta e a Biblioteca não foi alterada."
)
INTERRUPTED_MESSAGE = (
    "A exportação foi interrompida porque o Media Tool foi encerrado. Os arquivos já copiados continuam na pasta;"
    " exporte de novo para a mesma pasta para completar."
)
FOLDER_GONE_MESSAGE = (
    "A pasta de destino deixou de existir (ou foi trocada) durante a exportação. A Biblioteca não foi alterada."
)


class ExportCancelled(Exception):
    pass


class ExportAborted(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _open_part(path: Path):
    """Cria o temporário com abertura EXCLUSIVA (falha com FileExistsError se o nome já existir; nada é apagado)."""
    return open(io_path(path), "xb")


def _open_zip_part(path: Path):
    """Cria o `.zip.part` na pasta exclusiva do servidor (abertura exclusiva)."""
    return open(path, "xb")


def _zip_time(media: dict[str, Any], src: Path,
              zone: export_time.Zone | None = None) -> tuple[int, int, int, int, int, int]:
    """Data de modificação da entrada: a da publicação (`taken_at`) quando houver e for válida; senão a do arquivo.
    O ZIP guarda hora LOCAL sem fuso: é a hora no mesmo fuso usado na data do nome (`zone`; None = UTC). O formato só
    representa de 1980-01-01 a 2107-12-31: fora disso a data é limitada ao intervalo."""
    try:
        moment = export_time.publication_moment(media.get("taken_at"))
        if moment is None:
            moment = datetime.fromtimestamp(src.stat().st_mtime, UTC)
        moment = (zone or export_time.utc_zone)(moment)
    except Exception:  # noqa: BLE001 - data nos limites do calendário: cai para a do arquivo, senão 1980
        try:
            moment = datetime.fromtimestamp(src.stat().st_mtime, UTC).replace(tzinfo=None)
        except Exception:  # noqa: BLE001
            return (ZIP_MIN_YEAR, 1, 1, 0, 0, 0)
    if moment.year < ZIP_MIN_YEAR:
        return (ZIP_MIN_YEAR, 1, 1, 0, 0, 0)
    if moment.year > ZIP_MAX_YEAR:
        return (ZIP_MAX_YEAR, 12, 31, 23, 59, 58)
    return (moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second - moment.second % 2)


def _rename_no_overwrite(src: Path, dst: Path) -> None:
    if os.name == "nt":
        os.rename(io_path(src), io_path(dst))  # no Windows falha com FileExistsError se o destino existe
    else:
        os.link(src, dst)  # falha se o destino existe; depois remove o temporário (os dois ficam na pasta de destino)
        os.unlink(src)


def _is_disk_full(exc: OSError) -> bool:
    return exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)) or \
        getattr(exc, "winerror", None) in _DISK_FULL_WINERRORS


def cleanup_default_folder(export: dict[str, Any]) -> bool:
    """Exportação cancelada ou falha para a pasta padrão criada pelo servidor NESTA exportação: remove a pasta se
    estiver vazia (só `rmdir`)."""
    return bool(export.get("dest_created")) and remove_if_empty(export["dest_path"])


def recover_interrupted_exports(ctx: AppContext) -> int:
    """Startup: exportação que ficou `running` vira `failed`/`interrupted` (não é retomada sozinha). As `pending`
    continuam na fila. Nenhum arquivo da pasta de destino é apagado por padrão de nome (um `.mtpart` pode sobrar);
    só a pasta padrão criada pelo servidor é removida, e apenas se estiver vazia."""
    rows = ctx.exports.running()
    for export in rows:
        ctx.exports.finish(export["id"], "failed", error_code="interrupted", error_message=INTERRUPTED_MESSAGE)
        cleanup_default_folder(export)
        if export.get("mode") == "zip":
            export_zip.remove_zip_files(ctx.settings, export)   # o .part DAQUELE registro (pasta do servidor)
    export_zip.sweep(ctx, startup=True)
    if rows:
        logger.info("Recovery: %s exportação(ões) interrompida(s) marcada(s) como falha", len(rows))
    return len(rows)


class ExportRunner:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Protege (exportação atual, evento de cancelamento) contra a corrida com `cancel()`.
        self._lock = threading.Lock()
        self._current_id: int | None = None
        self._cancel = threading.Event()
        # Temporários criados por ESTA execução (os únicos que ela pode remover).
        self._temps: set[Path] = set()
        self._dest_identity: tuple[int, int] | None = None
        self.progress_writes = 0
        self._failures: Counter = Counter()
        self._zone: export_time.Zone | None = None
        self._last_sweep = time.monotonic()

    # ---- ciclo de vida -----------------------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="export-runner", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def wake(self) -> None:
        self._wake.set()

    def cancel(self, export_id: int) -> None:
        """Sinaliza a exportação em execução (o pedido já está gravado no banco por quem chama)."""
        with self._lock:
            if self._current_id == export_id:
                self._cancel.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_next()
            except Exception:  # noqa: BLE001 - a thread nunca morre por causa de uma exportação
                logger.exception("Falha inesperada no executor de exportações")
                worked = False
            if time.monotonic() - self._last_sweep >= export_zip.SWEEP_INTERVAL_SECONDS:
                self._last_sweep = time.monotonic()
                try:
                    export_zip.sweep(self.ctx)   # ZIPs vencidos
                except Exception:  # noqa: BLE001
                    logger.exception("Falha na limpeza de ZIPs temporários")
            if not worked:
                self._wake.wait(IDLE_WAIT_SECONDS)
                self._wake.clear()

    # ---- execução ----------------------------------------------------------------------------------------------
    def run_next(self) -> bool:
        """Executa a próxima exportação `pending` até o fim. Retorna False se a fila está vazia."""
        with self._lock:
            export = self.ctx.exports.claim_next()
            if export is None:
                return False
            self._current_id = export["id"]
            self._cancel = threading.Event()
        try:
            self._run(export)
        finally:
            with self._lock:
                self._current_id = None
        return True

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise ExportCancelled()
        if self._stop.is_set():
            raise ExportAborted("interrupted", INTERRUPTED_MESSAGE)

    def _run(self, export: dict[str, Any]) -> None:
        counts = dict.fromkeys(COUNT_COLUMNS, 0)
        status, code, message = "completed", None, None
        total = None
        zip_bytes = None
        self._temps = set()
        self._failures = Counter()
        # Fuso da data nos nomes (e do mtime das entradas do ZIP): resolvido uma vez por valor configurado.
        self._zone = export_time.resolve_zone(self.ctx.settings.timezone)
        try:
            if export.get("mode") == "zip":
                zip_bytes = self._run_zip(export, counts)
            else:
                # Revalidação no início da cópia (inclusive por identidade): a pasta pode ter sumido ou mudado.
                dest = validate_destination(export["dest_path"], self.ctx.settings)
                self._dest_identity = file_identity(dest)
                self._copy_all(export, counts,
                               lambda media, carousel: self._export_one(export["id"], dest, media, carousel))
            # A seleção pode mudar durante a cópia (mídia excluída da Biblioteca, ou job concluído depois do pedido):
            # ao concluir, tratadas == total. O que sumiu conta como original ausente.
            processed = counts["copied"] + counts["skipped_existing"] + counts["missing"] + counts["failed"]
            if processed < export["total"]:
                counts["missing"] += export["total"] - processed
            elif processed > export["total"]:
                total = processed
            status, code, message = self._failure_summary(export, counts)
        except ExportCancelled:
            status, message = "cancelled", CANCELLED_MESSAGE
        except ExportAborted as exc:
            status, code, message = "failed", exc.code, exc.message
        except DestinationError as exc:
            status, code, message = "failed", exc.code, exc.message
        except Exception:  # noqa: BLE001 - erro inesperado encerra só esta exportação
            logger.exception("Exportação %s falhou", export["id"])
            status, code = "failed", "internal_error"
            message = "Erro interno durante a exportação. A Biblioteca não foi alterada."
        for leftover in list(self._temps):
            self._discard(leftover)
        if status != "completed" and export.get("mode") == "zip":
            export_zip.remove_zip_files(self.ctx.settings, export)
        self.ctx.exports.finish(
            export["id"], status, counts=counts, error_code=code, error_message=message, total=total,
            zip_bytes=zip_bytes if status == "completed" else None,
            zip_expires_at=utc_now(export_zip.ZIP_READY_TTL_SECONDS) if zip_bytes is not None else None,
        )
        if status != "completed":
            cleanup_default_folder(export)
        logger.info(
            "Exportação %s %s: %s copiadas, %s já existentes, %s renomeadas, %s ausentes, %s com falha",
            export["id"], status, counts["copied"], counts["skipped_existing"], counts["renamed"],
            counts["missing"], counts["failed"],
        )

    def _note_failure(self, exc: OSError, src: Path) -> None:
        """Guarda o motivo de uma mídia não copiada (leitura do original x gravação no destino + causa)."""
        reading = getattr(exc, "reading_original", False) or getattr(exc, "filename", None) == str(src)
        cause = ("acesso negado" if isinstance(exc, PermissionError)
                 else "arquivo não encontrado" if isinstance(exc, FileNotFoundError) else "erro de leitura/gravação")
        self._failures[("read" if reading else "write", cause)] += 1

    def _failure_summary(self, export: dict[str, Any], counts: dict[str, int]) -> tuple[str, str | None, str | None]:
        """Desfecho quando houve mídias com falha: nada gravado -> `failed` com código e mensagem; parte gravada ->
        `completed` com código/mensagem informativos do motivo predominante."""
        if counts["failed"] == 0 or not self._failures:
            return "completed", None, None
        (kind, cause), _ = self._failures.most_common(1)[0]
        code = "export_read_failed" if kind == "read" else "export_write_failed"
        one, many = ("lido do original", "lidos do original") if kind == "read" else (
            ("incluído no ZIP", "incluídos no ZIP") if export.get("mode") == "zip"
            else ("gravado na pasta de destino", "gravados na pasta de destino"))
        if counts["copied"] + counts["skipped_existing"] == 0:
            return "failed", code, f"Nenhum arquivo pôde ser {one}: {cause}. A Biblioteca não foi alterada."
        failed = counts["failed"]
        amount = "1 arquivo não pôde ser" if failed == 1 else f"{failed} arquivos não puderam ser"
        ending = one if failed == 1 else many
        return "completed", code, (
            f"{amount} {ending}: {cause}. Exporte de novo para o mesmo destino para copiar só o que faltou.")

    def _flush(self, export_id: int, counts: dict[str, int]) -> None:
        self.ctx.exports.update_progress(export_id, counts)
        self.progress_writes += 1

    def _copy_all(self, export: dict[str, Any], counts: dict[str, int], handle) -> None:
        """Percorre as mídias em páginas (keyset) e chama `handle(media, carousel)` para cada uma."""
        export_id = export["id"]
        after_id = 0
        pending_files = 0
        consecutive_failures = 0
        last_flush = time.monotonic()
        while True:
            self._check_cancel()
            rows = self.ctx.exports.page(export, after_id=after_id, limit=PAGE_SIZE)
            if not rows:
                return
            carousels = self.ctx.exports.carousel_posts(row["source_post_id"] for row in rows)
            for media in rows:
                self._check_cancel()
                outcome = handle(media, media["source_post_id"] in carousels)
                if outcome == "renamed":
                    counts["copied"] += 1
                counts[outcome] += 1
                consecutive_failures = consecutive_failures + 1 if outcome == "failed" else 0
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    raise ExportAborted(
                        "export_write_failed",
                        "Muitas falhas seguidas ao gravar na pasta de destino. Verifique a pasta e exporte de novo."
                        " A Biblioteca não foi alterada.",
                    )
                pending_files += 1
                now = time.monotonic()
                if pending_files >= PROGRESS_EVERY_FILES or now - last_flush >= PROGRESS_EVERY_SECONDS:
                    self._flush(export_id, counts)
                    pending_files, last_flush = 0, now
            after_id = rows[-1]["id"]

    def _discard(self, part: Path) -> None:
        """Remove um temporário, SÓ se foi criado por esta execução."""
        if part not in self._temps:
            return
        self._temps.discard(part)
        try:
            os.unlink(io_path(part))
        except OSError:
            pass

    def _check_dest(self, dest: Path) -> None:
        """A pasta de destino continua sendo a MESMA pasta validada (identidade): se foi trocada por outra (por
        exemplo, por uma junção para dentro do storage), a exportação para."""
        if self._dest_identity is not None and file_identity(dest) != self._dest_identity:
            raise ExportAborted("export_folder_missing", FOLDER_GONE_MESSAGE)

    def _export_one(self, export_id: int, dest: Path, media: dict[str, Any], carousel: bool) -> str:
        """Copia UM original. Retorna `copied`, `renamed`, `skipped_existing`, `missing` ou `failed`."""
        try:
            src = self.ctx.storage.resolve(media["original_path"])
            if src.is_symlink() or not src.is_file():
                return "missing"
            size = src.stat().st_size
        except (StorageError, OSError, TypeError):
            return "missing"
        try:
            stem = export_stem(media, carousel=carousel, zone=self._zone)
            ext = export_extension(media["original_path"])
            file_name(stem, ext)
        except Exception:  # noqa: BLE001 - dado inesperado numa mídia não derruba a exportação inteira
            logger.warning("Exportação %s: mídia %s com dados inválidos para o nome", export_id, media.get("id"))
            return "failed"
        self._check_dest(dest)
        for attempt in range(1, MAX_NAME_ATTEMPTS + 1):
            name = file_name(stem, ext, attempt)
            final = dest / name
            if final.parent != dest or os.path.basename(name) != name:   # o arquivo final fica NA pasta de destino
                return "failed"
            part: Path | None = None
            try:
                if os.path.lexists(io_path(final)):
                    if self._identical(final, size, media["original_sha256"]):
                        return "skipped_existing"
                    continue   # arquivo DIFERENTE com o mesmo nome: nunca sobrescreve; tenta " (2)", " (3)"...
                part = self._copy(src, dest, name, export_id, size)
                try:
                    _rename_no_overwrite(part, final)
                except FileExistsError:
                    # Outro processo criou o arquivo final entre a cópia e o rename: ele fica intacto.
                    self._discard(part)
                    continue
                self._temps.discard(part)
                self._set_mtime(final, media, src)
            except (ExportCancelled, ExportAborted):
                raise
            except OSError as exc:
                if part is not None:
                    self._discard(part)
                if _is_disk_full(exc):
                    raise ExportAborted(
                        "export_disk_full",
                        "O disco de destino ficou sem espaço. Libere espaço e exporte de novo para a mesma pasta:"
                        " o que já foi copiado não é copiado outra vez. A Biblioteca não foi alterada.",
                    ) from None
                if not os.path.isdir(io_path(dest)):
                    raise ExportAborted("export_folder_missing", FOLDER_GONE_MESSAGE) from None
                logger.warning("Exportação %s: mídia %s não copiada (%s)", export_id, media["id"],
                               type(exc).__name__)
                self._note_failure(exc, src)
                return "failed"
            return "copied" if attempt == 1 else "renamed"
        return "failed"

    @staticmethod
    def _set_mtime(final: Path, media: dict[str, Any], src: Path) -> None:
        """Data de modificação do arquivo exportado = horário da publicação (instante absoluto de `taken_at`); sem
        `taken_at`, a do original. Falha aqui nunca falha a cópia."""
        try:
            moment = export_time.publication_moment(media.get("taken_at"))
            stamp = moment.timestamp() if moment is not None else src.stat().st_mtime
            os.utime(io_path(final), (stamp, stamp))
        except Exception:  # noqa: BLE001
            pass

    def _identical(self, path: Path, size: int, sha256: str) -> bool:
        """Mesmo conteúdo do original: mesmo tamanho e mesmo SHA-256 (o hash só é lido se o tamanho bate)."""
        target = io_path(path)
        if not os.path.isfile(target) or os.path.getsize(target) != size:
            return False
        digest = hashlib.sha256()
        with open(target, "rb") as handle:
            while True:
                self._check_cancel()
                chunk = handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest() == sha256

    def _create_temp(self, dest: Path, name: str, export_id: int):
        """Cria um temporário novo desta execução. Nome já existente (arquivo de outra pessoa/execução): sorteia
        outro; nunca apaga nem reaproveita."""
        for _ in range(MAX_TEMP_ATTEMPTS):
            part = dest / f".{name}.{export_id}.{secrets.token_hex(4)}{TEMP_SUFFIX}"
            try:
                handle = _open_part(part)
            except FileExistsError:
                continue
            self._temps.add(part)
            return part, handle
        raise OSError(errno.EEXIST, "Não foi possível criar um arquivo temporário na pasta de destino")

    def _copy(self, src: Path, dest: Path, name: str, export_id: int, size: int) -> Path:
        """Copia o original em blocos para um temporário novo e devolve o caminho dele. Em qualquer falha (ou
        cancelamento) o temporário criado aqui é removido."""
        part, target = self._create_temp(dest, name, export_id)
        try:
            written = 0
            with target, open(src, "rb") as source:
                while True:
                    self._check_cancel()
                    try:
                        chunk = source.read(CHUNK_SIZE)
                    except OSError as exc:
                        exc.reading_original = True   # erro ao LER o original (não ao gravar no destino)
                        raise
                    if not chunk:
                        break
                    target.write(chunk)
                    written += len(chunk)
            if written != size or os.path.getsize(io_path(part)) != size:
                raise OSError(errno.EIO, "Tamanho copiado diferente do original")
        except BaseException:
            self._discard(part)
            raise
        return part

    # ---- ZIP ---------------------------------------------------------------------------------------------------
    def _run_zip(self, export: dict[str, Any], counts: dict[str, int]) -> int:
        """Gera o ZIP plano (STORED, ZIP64) no temporário do servidor, entrada por entrada, em blocos. Retorna o
        tamanho final. Em qualquer falha ou cancelamento o `.part` é removido por quem chama."""
        # Rechecagem no INÍCIO da preparação (o disco pode ter enchido enquanto a exportação esperava na fila):
        # mesma conta da criação, com a reserva dos downloads; falha limpa, sem criar o `.part`.
        problem = export_zip.space_problem(self.ctx, export, include_queue=False)
        if problem is not None:
            raise ExportAborted("export_no_space", f"{problem} A Biblioteca não foi alterada.")
        final = export_zip.zip_file(self.ctx.settings, export, create_dir=True)
        part = export_zip.zip_file(self.ctx.settings, export, part=True)
        if final is None or part is None:
            raise ExportAborted(
                "internal_error", "A pasta de ZIPs temporários não pôde ser usada. A Biblioteca não foi alterada.")
        try:
            entries = self._write_zip(part, export, counts)   # o objeto de escrita já foi liberado ao retornar
            if entries == 0:
                raise ExportAborted(
                    "export_empty",
                    "Nenhum arquivo original disponível para o ZIP: os originais desta seleção não estão no"
                    " storage. A Biblioteca não foi alterada.",
                )
            # Conferência barata (sem reler o conteúdo e sem manter duas listas): o ZIP abre e o diretório central
            # tem exatamente as entradas gravadas.
            with zipfile.ZipFile(part) as check:
                stored = len(check.filelist)
            if stored != entries or entries != counts["copied"]:
                raise ExportAborted("export_write_failed", "O ZIP gerado não passou na conferência. Tente de novo.")
            os.rename(part, final)
            return os.path.getsize(final)
        except (ExportCancelled, ExportAborted):
            raise
        except OSError as exc:
            if _is_disk_full(exc):
                raise ExportAborted(
                    "export_disk_full",
                    "O disco ficou sem espaço enquanto o ZIP era preparado. Libere espaço e tente de novo."
                    " A Biblioteca não foi alterada.",
                ) from None
            raise ExportAborted(
                "export_write_failed", "Falha ao gravar o ZIP temporário. A Biblioteca não foi alterada."
            ) from None

    def _write_zip(self, part: Path, export: dict[str, Any], counts: dict[str, int]) -> int:
        used: set[str] = set()   # nomes já usados (sem diferenciar maiúsculas/minúsculas)
        with _open_zip_part(part) as raw, \
                zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            self._copy_all(export, counts, lambda media, carousel: self._zip_one(archive, used, media, carousel))
            return len(archive.filelist)

    def _zip_one(self, archive: zipfile.ZipFile, used: set[str], media: dict[str, Any], carousel: bool) -> str:
        """Grava UM original como entrada na raiz do ZIP. Retorna `copied`, `renamed`, `missing` ou `failed`."""
        try:
            src = self.ctx.storage.resolve(media["original_path"])
            if src.is_symlink() or not src.is_file():
                return "missing"
            size = src.stat().st_size
        except (StorageError, OSError, TypeError):
            return "missing"
        try:
            stem = export_stem(media, carousel=carousel, zone=self._zone)
            ext = export_extension(media["original_path"])
            stamp = _zip_time(media, src, self._zone)
            # Nome único no arquivo, sem diferenciar maiúsculas/minúsculas (o ZIP extrai bem no Windows).
            name, attempt = None, 0
            for attempt in range(1, MAX_NAME_ATTEMPTS + 1):
                candidate = file_name(stem, ext, attempt)
                if candidate.casefold() not in used:
                    name = candidate
                    break
            if name is None or "/" in name or "\\" in name:
                return "failed"
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o644 << 16
        except Exception:  # noqa: BLE001 - dado inesperado numa mídia não derruba o ZIP inteiro
            return "failed"
        try:
            source = open(src, "rb")
        except OSError as exc:
            self._note_failure(exc, src)
            return "failed"
        with source:
            try:
                # O cabeçalho é montado antes de qualquer byte ir para o arquivo: se ele não puder ser montado
                # (data ou campo fora do formato), a entrada nem começa e o ZIP segue com as outras mídias.
                target = archive.open(info, "w", force_zip64=True)
            except (ValueError, OverflowError, struct.error):
                return "failed"
            written = 0
            with target:
                while True:
                    self._check_cancel()
                    try:
                        chunk = source.read(CHUNK_SIZE)
                    except OSError:
                        # A entrada já começou e não pode ser retirada do arquivo: o ZIP inteiro é refeito.
                        raise ExportAborted(
                            "export_read_failed",
                            "Um arquivo original não pôde ser lido durante a preparação do ZIP. Tente de novo:"
                            " o ZIP é preparado do zero. A Biblioteca não foi alterada.",
                        ) from None
                    if not chunk:
                        break
                    target.write(chunk)
                    written += len(chunk)
        if written != size:
            raise ExportAborted(
                "export_read_failed",
                "Um arquivo original mudou de tamanho durante a preparação do ZIP. Tente de novo. A Biblioteca não"
                " foi alterada.",
            )
        used.add(name.casefold())
        return "copied" if attempt == 1 else "renamed"
