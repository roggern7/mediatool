"""Destino da exportação para pasta: seletor nativo, validação e "destinos autorizados" por token.

Modelo de segurança: nenhuma rota recebe caminho do cliente. O destino só existe se (a) um humano o escolheu no
diálogo nativo aberto por ESTE processo (`pick_folder_native`), ou (b) o servidor montou a pasta padrão em Documentos.
O caminho escolhido fica guardado no servidor, ligado a um token aleatório de uso único e validade curta
(`DestinationTokens`); as rotas de exportação recebem só o token.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from app.core.config import Settings
from app.services.system_open import _child_env  # mesmo ambiente filtrado dos outros subprocessos de desktop

logger = logging.getLogger("media_tool.exports")

PICKER_MODULE = "app.services.folder_picker_child"
BACKEND_DIR = Path(__file__).resolve().parents[2]
# O usuário pode demorar para escolher: prazo longo. Estourou = tratado como cancelado.
PICKER_TIMEOUT_SECONDS = 600.0
TOKEN_TTL_SECONDS = 600.0
MAX_TOKENS = 16
EXPORTS_SUBDIR = ("Media Tool", "Exports")
DEFAULT_FOLDER_PREFIX = "Importacao"
LIBRARY_FOLDER_PREFIX = "Biblioteca"
_SYSTEM_ENV_KEYS = ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData",
                    "ALLUSERSPROFILE")


class DestinationError(Exception):
    """Destino recusado ou seletor indisponível. `code` estável, `message` em português, `status` HTTP."""

    def __init__(self, code: str, message: str, status: int = 422) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


# --------------------------------------------------------------------------- validação

_LONG_PREFIX = "\\\\?\\"
_DEVICE_PREFIX = "\\\\.\\"
# Acima disto o Windows só aceita o caminho com o prefixo de caminho longo.
_MAX_PLAIN_PATH = 259


def strip_long_prefix(text: str) -> str:
    """`\\\\?\\C:\\x` -> `C:\\x`; `\\\\?\\UNC\\srv\\share\\x` -> `\\\\srv\\share\\x`. Outros textos ficam como estão."""
    if text.startswith(_LONG_PREFIX):
        rest = text[len(_LONG_PREFIX):]
        if rest[:4].upper() == "UNC\\":
            return "\\\\" + rest[4:]
        return rest
    return text


def io_path(path: str | os.PathLike[str]) -> str:
    """Caminho para I/O. No Windows usa o prefixo `\\\\?\\` para aceitar caminhos longos (mais de 260 caracteres)."""
    text = os.fspath(path)
    if os.name != "nt" or text.startswith(_LONG_PREFIX):
        return text
    if text.startswith("\\\\"):
        return _LONG_PREFIX + "UNC\\" + text[2:]
    return _LONG_PREFIX + text


def file_identity(path: str | os.PathLike[str]) -> tuple[int, int] | None:
    """Identidade do arquivo/pasta no disco (volume, número do arquivo), seguindo links e junções. É a mesma para
    qualquer TEXTO de caminho que leve ao mesmo lugar (`C:\\x`, `\\\\?\\C:\\x`, `\\\\localhost\\C$\\x`, unidade
    mapeada, junção). None se não existe ou se o sistema de arquivos não informa a identidade."""
    try:
        info = os.stat(io_path(path))
    except (OSError, ValueError):
        return None
    if not info.st_ino:
        return None
    return (info.st_dev, info.st_ino)


def _resolved(path: Path) -> Path | None:
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError):
        return None


def managed_roots(settings: Settings) -> list[Path]:
    """Pastas gerenciadas pelo Media Tool: código do projeto, storage, data (banco, backups, secrets) e logs."""
    return [settings.root, settings.storage_dir, settings.data_dir, settings.secrets_dir, settings.log_dir]


def system_roots(env: Mapping[str, str] | None = None) -> list[Path]:
    """Pastas do sistema onde nunca se exporta (%SystemRoot%, Program Files, Program Files (x86), ProgramData)."""
    env = os.environ if env is None else env
    roots: list[Path] = []
    for key in _SYSTEM_ENV_KEYS:
        value = (env.get(key) or "").strip()
        if value and Path(value).is_absolute():
            roots.append(Path(value))
    return roots


def _identities(paths) -> set[tuple[int, int]]:
    return {identity for identity in map(file_identity, paths) if identity is not None}


def validate_destination(raw: str | os.PathLike[str], settings: Settings, *, check_writable: bool = True) -> Path:
    """Valida e devolve o destino RESOLVIDO (links e junções seguidos). Levanta `DestinationError`.

    Aceita unidade local, removível e caminho de rede (UNC/unidade mapeada): é um destino do usuário, escolhido no
    diálogo nativo. Recusa: caminho relativo ou com `..`, inexistente, que não é pasta, raiz de unidade (ou de
    compartilhamento), pasta dentro (ou pai) das pastas gerenciadas pelo Media Tool, pasta dentro das pastas do sistema
    e pasta sem permissão de escrita.

    "Dentro" e "pai" são decididos por IDENTIDADE de arquivo (`file_identity`), não só pelo texto: um caminho
    alternativo para a mesma pasta (`\\\\localhost\\C$\\...`, `\\\\?\\C:\\...`, unidade mapeada para um compartilhamento
    desta máquina, junção) é recusado do mesmo jeito. A comparação por texto continua valendo como segunda barreira."""
    try:
        text = os.fspath(raw)
    except TypeError:
        text = ""
    if not isinstance(text, str) or not text.strip() or "\x00" in text:
        raise DestinationError("export_dest_invalid", "Pasta de destino inválida.")
    if os.name == "nt":
        text = text.replace("/", "\\")
    if text.startswith(_DEVICE_PREFIX):
        raise DestinationError("export_dest_invalid", "Pasta de destino inválida.")
    text = strip_long_prefix(text)
    path = Path(text)
    if not path.is_absolute() or ".." in path.parts:
        raise DestinationError("export_dest_invalid", "A pasta de destino precisa ser um caminho absoluto.")
    too_long = DestinationError(
        "export_dest_too_long",
        "O caminho desta pasta é longo demais para o Windows neste computador. Escolha uma pasta com caminho mais"
        " curto.",
    )
    try:
        # Com o prefixo de caminho longo, a checagem de existência vale também acima de 260 caracteres.
        resolved = Path(strip_long_prefix(str(Path(io_path(path)).resolve(strict=True))))
        is_dir = os.path.isdir(io_path(resolved))
    except (OSError, RuntimeError, ValueError):
        if len(text) > _MAX_PLAIN_PATH:
            raise too_long from None
        raise DestinationError(
            "export_dest_missing", "A pasta de destino não existe mais. Escolha outra pasta.", 409
        ) from None
    if not is_dir:
        raise DestinationError("export_dest_missing", "O destino escolhido não é uma pasta.", 409)
    if resolved.parent == resolved:
        raise DestinationError(
            "export_dest_drive_root",
            "Não é possível exportar direto para a raiz de uma unidade. Escolha ou crie uma subpasta.",
        )
    managed = [root for root in map(_resolved, managed_roots(settings)) if root is not None]
    system = [root for root in map(_resolved, system_roots()) if root is not None]
    inside = DestinationError(
        "export_dest_forbidden",
        "Esta pasta é usada pelo Media Tool ou pelo Windows. Escolha uma pasta sua, fora delas.",
    )
    parent_of = DestinationError(
        "export_dest_forbidden",
        "Esta pasta contém as pastas internas do Media Tool. Escolha ou crie uma subpasta separada.",
    )
    # 1) Por texto (caminhos resolvidos).
    for root in [*managed, *system]:
        if resolved == root or resolved.is_relative_to(root):
            raise inside
    for root in managed:
        if root.is_relative_to(resolved):
            raise parent_of
    # 2) Por identidade: o destino ou qualquer ancestral dele É uma pasta gerenciada/do sistema (destino dentro
    # dela); ou o destino É um ancestral de uma pasta gerenciada (destino pai dela).
    own = file_identity(resolved)
    if own is not None:
        if _identities([resolved, *resolved.parents]) & _identities([*managed, *system]):
            raise inside
        if own in _identities(parent for root in managed for parent in root.parents):
            raise parent_of
    if check_writable:
        try:
            with tempfile.TemporaryFile(dir=io_path(resolved)):
                pass
        except OSError:
            if len(str(resolved)) > _MAX_PLAIN_PATH:
                raise too_long from None
            raise DestinationError(
                "export_dest_not_writable", "Sem permissão para gravar nesta pasta. Escolha outra pasta.", 409
            ) from None
    return resolved


def remove_if_empty(path: str | os.PathLike[str]) -> bool:
    """Remove a pasta SÓ se estiver vazia (`rmdir`, nunca recursivo). Usado para a pasta padrão criada pelo servidor
    numa exportação que não chegou a copiar nada."""
    try:
        os.rmdir(io_path(path))
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- pasta padrão


def user_documents_dir() -> Path:
    """Pasta Documentos do usuário atual (pasta conhecida do Windows; fora dele, `~/Documents`)."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class _Guid(ctypes.Structure):
                _fields_ = [("data1", wintypes.DWORD), ("data2", wintypes.WORD), ("data3", wintypes.WORD),
                            ("data4", ctypes.c_ubyte * 8)]

            # FOLDERID_Documents {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
            folder_id = _Guid(0xFDD39AD0, 0x238F, 0x46AF,
                              (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
            pointer = ctypes.c_void_p()
            get_path = ctypes.windll.shell32.SHGetKnownFolderPath
            get_path.argtypes = [ctypes.POINTER(_Guid), wintypes.DWORD, wintypes.HANDLE,
                                 ctypes.POINTER(ctypes.c_void_p)]
            get_path.restype = ctypes.c_long
            free = ctypes.windll.ole32.CoTaskMemFree
            free.argtypes = [ctypes.c_void_p]
            free.restype = None
            try:
                if get_path(ctypes.byref(folder_id), 0, None, ctypes.byref(pointer)) == 0 and pointer.value:
                    value = ctypes.wstring_at(pointer.value)
                    if value:
                        return Path(value)
            finally:
                free(pointer)
        except (OSError, AttributeError, ValueError):
            pass
    return Path.home() / "Documents"


def default_exports_dir(documents: Path) -> Path:
    return Path(documents).joinpath(*EXPORTS_SUBDIR)


def create_default_folder(documents: Path, *, now: datetime | None = None,
                          prefix: str = DEFAULT_FOLDER_PREFIX) -> Path:
    """Cria `<Documentos>\\Media Tool\\Exports\\Importacao-AAAA-MM-DD-HHMM` (sufixo `-2`, `-3`... se já existir;
    `Biblioteca-...` para seleção da Biblioteca). O caminho é montado só no servidor (`prefix` nunca vem do cliente)."""
    moment = now or datetime.now()
    base = default_exports_dir(documents)
    name = f"{prefix}-{moment:%Y-%m-%d-%H%M}"
    try:
        base.mkdir(parents=True, exist_ok=True)
        for attempt in range(1, 1000):
            candidate = base / (name if attempt == 1 else f"{name}-{attempt}")
            try:
                candidate.mkdir()
            except FileExistsError:
                continue
            return candidate
    except OSError:
        pass
    raise DestinationError(
        "export_dest_not_writable", "Não foi possível criar a pasta padrão de exportações em Documentos.", 409
    )


# --------------------------------------------------------------------------- destinos autorizados


class DestinationTokens:
    """Destinos escolhidos no diálogo nativo, guardados só no servidor: token aleatório, uso único, validade curta."""

    def __init__(self, *, ttl: float = TOKEN_TTL_SECONDS, clock: Callable[[], float] = time.monotonic,
                 max_entries: int = MAX_TOKENS) -> None:
        self.ttl = ttl
        self.clock = clock
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[Path, float]] = {}

    def issue(self, path: Path) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = self.clock()
            for key in [k for k, (_, expires) in self._entries.items() if expires <= now]:
                del self._entries[key]
            while len(self._entries) >= self.max_entries:
                del self._entries[next(iter(self._entries))]   # o mais antigo sai
            self._entries[token] = (Path(path), now + self.ttl)
        return token

    def peek(self, token: str) -> Path:
        """Devolve o destino SEM invalidar o token (para validar o pedido antes de gastá-lo)."""
        with self._lock:
            entry = self._entries.get(token) if isinstance(token, str) else None
            if entry is not None and entry[1] > self.clock():
                return entry[0]
        raise DestinationError(
            "export_token_invalid", "A pasta escolhida expirou ou já foi usada. Escolha a pasta de novo.", 409
        )

    def consume(self, token: str) -> Path:
        """Devolve o destino e invalida o token (uso único). Inexistente, expirado ou já usado: erro."""
        with self._lock:
            entry = self._entries.pop(token, None) if isinstance(token, str) else None
            if entry is not None and entry[1] > self.clock():
                return entry[0]
        raise DestinationError(
            "export_token_invalid", "A pasta escolhida expirou ou já foi usada. Escolha a pasta de novo.", 409
        )


# --------------------------------------------------------------------------- seletor nativo

PICKER_UNAVAILABLE_MESSAGE = (
    "Não foi possível abrir a janela de escolha de pasta neste computador. Use “Usar pasta padrão”:"
    " o Media Tool cria uma pasta em Documentos\\Media Tool\\Exports."
)
# Variável de ambiente (só do processo filho) com o diretório inicial do método PowerShell. O valor é montado no
# servidor (última pasta usada ou pasta padrão) e só é passado se for um diretório absoluto existente.
PICKER_INITIAL_ENV = "MEDIA_TOOL_PICKER_INITIAL"
# Script FIXO do segundo método (sem Tcl/Tk): nenhum dado entra no texto do comando. A resposta sai em Base64 de
# UTF-8 (só ASCII no stdout), então acentos e Unicode não dependem do codepage do console.
POWERSHELL_PICKER_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "Add-Type -AssemblyName System.Windows.Forms; "
    "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
    "$dialog.Description = 'Media Tool: escolher pasta de exportacao'; "
    "$dialog.ShowNewFolderButton = $true; "
    f"$initial = $env:{PICKER_INITIAL_ENV}; "
    "if ($initial -and (Test-Path -LiteralPath $initial -PathType Container)) { $dialog.SelectedPath = $initial }; "
    "$owner = New-Object System.Windows.Forms.Form; "
    "$owner.TopMost = $true; $owner.ShowInTaskbar = $false; $owner.Opacity = 0; "
    "$owner.StartPosition = 'CenterScreen'; $owner.Show(); $owner.Activate(); "
    "$result = $dialog.ShowDialog($owner); $owner.Close(); "
    "if ($result -eq [System.Windows.Forms.DialogResult]::OK) { "
    "[Console]::Out.Write('OK:' + [Convert]::ToBase64String("
    "[System.Text.Encoding]::UTF8.GetBytes($dialog.SelectedPath))) } "
    "else { [Console]::Out.Write('CANCEL') }"
)


class PickerMethodFailed(Exception):
    """Este método de diálogo não funcionou neste ambiente (tenta-se o próximo)."""


def _run_picker_child(args: list[str], *, stdin_data: bytes | None, env: dict[str, str], cwd: str | None,
                      timeout: float) -> bytes | None:
    """Roda um processo filho de diálogo: sem shell, lista de argumentos fixa, sem janela de console.
    Retorna o stdout (bytes) ou None se o prazo estourou (filho encerrado; tratado como cancelado)."""
    try:
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        raise PickerMethodFailed("start") from None
    try:
        output, _ = proc.communicate(stdin_data or b"", timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return None
    return output


def _pick_with_tk(initial: Path | None, timeout: float) -> Path | None:
    """Método 1: `python -m app.services.folder_picker_child` (tkinter). Pedido e resposta em JSON só com ASCII
    (`\\uXXXX`), lidos como UTF-8: o caminho chega íntegro com acentos, Unicode e espaços."""
    request = json.dumps({"initial": str(initial) if initial is not None else None}).encode("ascii") + b"\n"
    output = _run_picker_child([sys.executable, "-m", PICKER_MODULE], stdin_data=request, env=_child_env(),
                               cwd=str(BACKEND_DIR), timeout=timeout)
    if output is None:
        return None
    try:
        answer = json.loads(output.decode("utf-8"))
    except ValueError:
        answer = None
    if not isinstance(answer, dict) or "error" in answer or "path" not in answer:
        raise PickerMethodFailed("tk")   # tkinter/Tcl não inicializou (ImportError, TclError...) ou resposta inválida
    chosen = answer["path"]
    return Path(chosen) if isinstance(chosen, str) and chosen else None


def _powershell_executable() -> str:
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                        "powershell.exe")


def _pick_with_powershell(initial: Path | None, timeout: float) -> Path | None:
    """Método 2 (sem Tcl): PowerShell + System.Windows.Forms.FolderBrowserDialog, com o script fixo acima."""
    env = _child_env()
    env.pop(PICKER_INITIAL_ENV, None)
    if initial is not None and Path(initial).is_absolute() and Path(initial).is_dir():
        env[PICKER_INITIAL_ENV] = str(initial)
    args = [_powershell_executable(), "-NoProfile", "-NonInteractive", "-STA", "-Command", POWERSHELL_PICKER_SCRIPT]
    output = _run_picker_child(args, stdin_data=None, env=env, cwd=None, timeout=timeout)
    if output is None:
        return None
    text = output.decode("ascii", "replace").strip()
    if text == "CANCEL":
        return None
    if not text.startswith("OK:"):
        raise PickerMethodFailed("powershell")
    try:
        chosen = base64.b64decode(text[3:], validate=True).decode("utf-8")
    except ValueError:
        raise PickerMethodFailed("powershell") from None
    if not chosen:
        raise PickerMethodFailed("powershell")
    return Path(chosen)


# Ordem de tentativa. Só passa para o seguinte quando o método NÃO funcionou (cancelar não é falha).
PICKER_METHODS = (_pick_with_tk, _pick_with_powershell)


def pick_folder_native(initial: Path | None, *, timeout: float = PICKER_TIMEOUT_SECONDS) -> Path | None:
    """Abre o diálogo nativo "Selecionar pasta" num processo filho isolado e devolve a pasta (None = cancelado).

    Tenta o tkinter e, se ele não inicializar neste ambiente, o PowerShell. Nenhum funcionou (ou fora do Windows):
    501 `picker_unavailable`, orientando a usar a pasta padrão. Quem chama garante um diálogo por vez e valida o
    resultado com `validate_destination`."""
    if os.name == "nt":
        for method in PICKER_METHODS:
            try:
                return method(initial, timeout)
            except PickerMethodFailed as exc:
                logger.warning("Seletor de pasta: método %s indisponível", exc)
    raise DestinationError("picker_unavailable", PICKER_UNAVAILABLE_MESSAGE, 501)
