"""Sessão autenticada do Instagram fornecida localmente pelo usuário.

O ``sessionid`` é credencial: nunca aparece em repr/str, exceções, logs, banco ou respostas.
Armazenamento principal: ``<secrets_dir>/instagram-session.bin`` cifrado com Windows DPAPI (escopo do
usuário atual). Fora do Windows, o arquivo é gravado em texto JSON com permissão 0600.
Precedência: arquivo; senão variável de ambiente ``INSTAGRAM_SESSIONID``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets as _secrets_mod
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.core.logging import register_secret

SESSION_FILENAME = "instagram-session.bin"
ENV_SESSIONID = "INSTAGRAM_SESSIONID"
ALLOWED_EXTRA_KEYS: frozenset[str] = frozenset({"csrftoken", "ds_user_id", "mid", "ig_did", "rur"})
SESSIONID_MIN_LENGTH = 10
SESSIONID_MAX_LENGTH = 512
EXTRA_MAX_LENGTH = 512

_VALUE_RE = re.compile(r"[A-Za-z0-9%:_\-.]+")
_DPAPI_ENTROPY = b"media-tool/instagram-session/v1"
_HEADER_DPAPI = b"MTDPAPI1\n"
_HEADER_PLAIN = b"MTPLAIN1\n"
_PAYLOAD_VERSION = 1
_log = logging.getLogger("media_tool.session")


class SessionValidationError(ValueError):
    """Valor de sessão inválido. A mensagem nunca contém o valor recebido."""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message
        super().__init__(message)


class SessionStorageError(Exception):
    """Falha ao ler/gravar o arquivo de sessão. Mensagem segura (sem segredo)."""


class InstagramSession:
    """Contêiner opaco dos cookies da sessão. ``cookies()`` é o único acesso aos valores."""

    __slots__ = ("_cookies",)

    def __init__(self, cookies: Mapping[str, str]) -> None:
        object.__setattr__(self, "_cookies", dict(cookies))

    def cookies(self) -> dict[str, str]:
        return dict(self._cookies)

    def __repr__(self) -> str:
        return "InstagramSession(<redacted>)"

    __str__ = __repr__

    def __format__(self, format_spec: str) -> str:
        return repr(self)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("InstagramSession é imutável")

    def __getstate__(self) -> object:
        raise TypeError("InstagramSession não é serializável")

    def __reduce_ex__(self, protocol: object) -> object:
        raise TypeError("InstagramSession não é serializável")

    def __reduce__(self) -> object:
        raise TypeError("InstagramSession não é serializável")


class SessionStatus(BaseModel):
    configured: bool
    source: Literal["file", "env"] | None = None
    updated_at: str | None = None
    error: str | None = None  # motivo seguro quando há problema (ex.: arquivo corrompido)
    extra_keys: list[str] = Field(default_factory=list)  # só os NOMES dos cookies extras salvos, nunca valores


class InstagramSessionProvider(ABC):
    @abstractmethod
    def status(self) -> SessionStatus: ...

    @abstractmethod
    def get(self) -> InstagramSession | None: ...

    @abstractmethod
    def save(self, sessionid: str, extra: dict[str, str] | None = None) -> None: ...

    @abstractmethod
    def clear(self) -> None: ...

    def update_extra(self, extra: Mapping[str, str | None]) -> None:
        """Atualiza só os cookies extras da sessão salva. Padrão: não suportado."""
        raise SessionStorageError("Este provedor de sessão não permite editar cookies extras.")


# --------------------------------------------------------------------------- validação


def validate_sessionid(sessionid: object) -> str:
    if not isinstance(sessionid, str):
        raise SessionValidationError("sessionid", "sessionid ausente ou inválido")
    if not (SESSIONID_MIN_LENGTH <= len(sessionid) <= SESSIONID_MAX_LENGTH):
        raise SessionValidationError(
            "sessionid",
            f"sessionid deve ter entre {SESSIONID_MIN_LENGTH} e {SESSIONID_MAX_LENGTH} caracteres",
        )
    if not _VALUE_RE.fullmatch(sessionid):
        raise SessionValidationError(
            "sessionid", "sessionid contém caracteres inválidos (permitidos: letras, números e % : _ - .)"
        )
    return sessionid


def validate_extra(extra: Mapping[str, object] | None) -> dict[str, str]:
    """Valida chaves extras permitidas. Valores ``None`` ou vazios são ignorados."""
    result: dict[str, str] = {}
    if not extra:
        return result
    for key, value in extra.items():
        if key not in ALLOWED_EXTRA_KEYS:
            raise SessionValidationError(
                "extra", "chave extra não permitida (aceitas: " + ", ".join(sorted(ALLOWED_EXTRA_KEYS)) + ")"
            )
        if value is None or value == "":
            continue
        if not isinstance(value, str) or len(value) > EXTRA_MAX_LENGTH or not _VALUE_RE.fullmatch(value):
            raise SessionValidationError(
                key, f"{key} inválido (até {EXTRA_MAX_LENGTH} caracteres: letras, números e % : _ - .)"
            )
        result[key] = value
    return result


def _register_cookie_secrets(cookies: Mapping[str, str]) -> None:
    for value in cookies.values():
        register_secret(value)


# --------------------------------------------------------------------------- DPAPI


def dpapi_available() -> bool:
    return os.name == "nt"


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    def _make_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
        buffer = ctypes.create_string_buffer(data, len(data))
        blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        return blob, buffer

    def _dpapi_call(protect: bool, data: bytes) -> bytes:
        in_blob, in_buffer = _make_blob(data)
        entropy_blob, entropy_buffer = _make_blob(_DPAPI_ENTROPY)
        out_blob = _DataBlob()
        try:
            if protect:
                ok = _crypt32.CryptProtectData(
                    ctypes.byref(in_blob), "media-tool", ctypes.byref(entropy_blob),
                    None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
                )
            else:
                ok = _crypt32.CryptUnprotectData(
                    ctypes.byref(in_blob), None, ctypes.byref(entropy_blob),
                    None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
                )
            if not ok:
                code = ctypes.get_last_error()
                action = "cifrar" if protect else "decifrar"
                raise SessionStorageError(f"DPAPI falhou ao {action} (erro {code})")
            try:
                return ctypes.string_at(out_blob.pbData, out_blob.cbData)
            finally:
                ctypes.memset(out_blob.pbData, 0, out_blob.cbData)
                _kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))
        finally:
            ctypes.memset(in_buffer, 0, len(data))
            del entropy_buffer

    def dpapi_protect(data: bytes) -> bytes:
        """Cifra com CryptProtectData (escopo do usuário atual, entropia fixa do app)."""
        return _dpapi_call(True, data)

    def dpapi_unprotect(data: bytes) -> bytes:
        """Decifra com CryptUnprotectData. Levanta SessionStorageError se falhar."""
        return _dpapi_call(False, data)

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER_CLASS = 1

    def current_user_sid() -> str | None:
        """SID (texto) do usuário dono do token deste processo, ou None se não for possível obter."""
        token = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            size = wintypes.DWORD(0)
            _advapi32.GetTokenInformation(token, _TOKEN_USER_CLASS, None, 0, ctypes.byref(size))
            if size.value == 0:
                return None
            buffer = ctypes.create_string_buffer(size.value)
            if not _advapi32.GetTokenInformation(token, _TOKEN_USER_CLASS, buffer, size, ctypes.byref(size)):
                return None
            psid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_USER.User.Sid
            out = ctypes.c_void_p()
            if not _advapi32.ConvertSidToStringSidW(psid, ctypes.byref(out)):
                return None
            try:
                return ctypes.wstring_at(out.value)
            finally:
                _kernel32.LocalFree(out)
        finally:
            _kernel32.CloseHandle(token)

else:

    def dpapi_protect(data: bytes) -> bytes:
        raise SessionStorageError("DPAPI indisponível fora do Windows")

    def dpapi_unprotect(data: bytes) -> bytes:
        raise SessionStorageError("DPAPI indisponível fora do Windows")

    def current_user_sid() -> str | None:
        return None


_SID_RE = re.compile(r"S-1-\d+(-\d+)+")


def restrict_acl_to_current_user(path: Path, *, is_dir: bool) -> bool:
    """Windows: remove ACEs herdadas e deixa só controle total do usuário atual (via icacls, shell=False).

    Defesa extra (o conteúdo já é cifrado com DPAPI). Em falha, registra aviso sem segredo e retorna False.
    """
    if os.name != "nt":
        return False
    sid = current_user_sid()
    if not sid or not _SID_RE.fullmatch(sid):
        _log.warning("Não foi possível obter o SID do usuário atual; ACL de %s não foi restringida.", path)
        return False
    icacls = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "icacls.exe")
    grant = f"*{sid}:(OI)(CI)F" if is_dir else f"*{sid}:F"
    try:
        result = subprocess.run(
            [icacls, str(path), "/inheritance:r", "/grant:r", grant, "/q"],
            shell=False, capture_output=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("icacls falhou ao restringir a ACL de %s (%s).", path, type(exc).__name__)
        return False
    if result.returncode != 0:
        _log.warning("icacls retornou %s ao restringir a ACL de %s.", result.returncode, path)
        return False
    return True


# --------------------------------------------------------------------------- provider


class _CorruptSessionFile(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LocalInstagramSessionProvider(InstagramSessionProvider):
    def __init__(self, secrets_dir: Path, env: Mapping[str, str] = os.environ) -> None:
        self._secrets_dir = Path(secrets_dir)
        self._env = env

    @property
    def path(self) -> Path:
        return self._secrets_dir / SESSION_FILENAME

    # ----- leitura

    def _read_file(self) -> tuple[dict[str, str], str | None] | None:
        """Retorna (cookies, updated_at), None se o arquivo não existe, ou levanta _CorruptSessionFile."""
        path = self.path
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            raise _CorruptSessionFile("Não foi possível ler o arquivo de sessão.") from None

        if raw.startswith(_HEADER_DPAPI):
            if not dpapi_available():
                raise _CorruptSessionFile("Arquivo de sessão cifrado com DPAPI só pode ser lido no Windows.")
            try:
                plain = dpapi_unprotect(raw[len(_HEADER_DPAPI):])
            except SessionStorageError:
                raise _CorruptSessionFile(
                    "Não foi possível decifrar o arquivo de sessão (outro usuário/máquina ou arquivo "
                    "corrompido). Salve a sessão novamente."
                ) from None
        elif raw.startswith(_HEADER_PLAIN):
            plain = raw[len(_HEADER_PLAIN):]
        else:
            raise _CorruptSessionFile("Arquivo de sessão em formato desconhecido. Salve a sessão novamente.")

        try:
            payload = json.loads(plain.decode("utf-8"))
            cookies_raw = payload["cookies"]
            if not isinstance(cookies_raw, dict):
                raise TypeError
            sessionid = validate_sessionid(cookies_raw.get("sessionid"))
            extra = validate_extra({k: v for k, v in cookies_raw.items() if k != "sessionid"})
            updated_at = payload.get("updated_at")
            if updated_at is not None and not isinstance(updated_at, str):
                updated_at = None
        except Exception:
            raise _CorruptSessionFile("Arquivo de sessão corrompido. Salve a sessão novamente.") from None
        finally:
            plain = b""

        cookies = {"sessionid": sessionid, **extra}
        _register_cookie_secrets(cookies)
        return cookies, updated_at

    def _read_env(self) -> dict[str, str] | None:
        value = self._env.get(ENV_SESSIONID)
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        register_secret(value)
        validate_sessionid(value)
        return {"sessionid": value}

    def status(self) -> SessionStatus:
        try:
            from_file = self._read_file()
        except _CorruptSessionFile as exc:
            return SessionStatus(configured=False, source=None, updated_at=None, error=exc.reason)
        if from_file is not None:
            extra_keys = sorted(k for k in from_file[0] if k != "sessionid")
            return SessionStatus(configured=True, source="file", updated_at=from_file[1], extra_keys=extra_keys)
        try:
            from_env = self._read_env()
        except SessionValidationError:
            return SessionStatus(
                configured=False, error=f"Variável {ENV_SESSIONID} definida com valor inválido."
            )
        if from_env is not None:
            return SessionStatus(configured=True, source="env")
        return SessionStatus(configured=False)

    def get(self) -> InstagramSession | None:
        try:
            from_file = self._read_file()
        except _CorruptSessionFile:
            return None
        if from_file is not None:
            return InstagramSession(from_file[0])
        try:
            from_env = self._read_env()
        except SessionValidationError:
            return None
        return InstagramSession(from_env) if from_env is not None else None

    # ----- escrita

    def save(self, sessionid: str, extra: dict[str, str] | None = None) -> None:
        if isinstance(sessionid, str):
            register_secret(sessionid)
        if extra:
            for value in extra.values():
                if isinstance(value, str):
                    register_secret(value)
        cookies = {"sessionid": validate_sessionid(sessionid), **validate_extra(extra)}
        self._write_cookies(cookies)

    def update_extra(self, extra: Mapping[str, str | None]) -> None:
        """Acrescenta/substitui cookies extras da sessão salva em ARQUIVO, mantendo o sessionid.

        ``None`` ou ``""`` remove a chave. Sessão vinda só da variável de ambiente não é editável.
        """
        if not extra:
            raise SessionValidationError("extra", "Nenhum cookie extra informado.")
        for value in extra.values():
            if isinstance(value, str):
                register_secret(value)
        to_set = validate_extra(extra)
        to_remove = {key for key, value in extra.items() if value is None or value == ""}
        try:
            current = self._read_file()
        except _CorruptSessionFile as exc:
            raise SessionStorageError(exc.reason) from None
        if current is None:
            raise SessionStorageError(
                "Não há sessão salva em arquivo para atualizar. Salve o sessionid primeiro "
                "(sessão definida só pela variável de ambiente não pode ser editada)."
            )
        cookies = dict(current[0])
        for key in to_remove:
            cookies.pop(key, None)
        cookies.update(to_set)
        self._write_cookies(cookies)

    def _write_cookies(self, cookies: dict[str, str]) -> None:
        payload = json.dumps(
            {"version": _PAYLOAD_VERSION, "cookies": cookies, "updated_at": _utc_now_iso()},
            separators=(",", ":"),
        ).encode("utf-8")
        if dpapi_available():
            content = _HEADER_DPAPI + dpapi_protect(payload)
        else:
            content = _HEADER_PLAIN + payload
        payload = b""
        self._atomic_write(content)

    def _atomic_write(self, content: bytes) -> None:
        directory = self._secrets_dir
        tmp: Path | None = None
        try:
            created_dir = not directory.is_dir()
            directory.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                try:
                    os.chmod(directory, 0o700)
                except OSError:
                    pass
            elif created_dir:
                restrict_acl_to_current_user(directory, is_dir=True)
            tmp = directory / f".{SESSION_FILENAME}.{os.getpid()}.{_secrets_mod.token_hex(6)}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            os.close(os.open(tmp, flags, 0o600))
            # ACL restrita aplicada no temporário ainda vazio; os.replace preserva o descritor de segurança.
            if os.name == "nt":
                restrict_acl_to_current_user(tmp, is_dir=False)
            with open(tmp, "r+b") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            if os.name != "nt":
                os.chmod(self.path, 0o600)
        except (OSError, ValueError):
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            raise SessionStorageError("Não foi possível gravar o arquivo de sessão.") from None

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise SessionStorageError("Não foi possível remover o arquivo de sessão.") from None
