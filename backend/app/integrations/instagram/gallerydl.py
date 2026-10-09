"""GalleryDlInstagramDownloader: implementa InstagramDownloader falando com o runner via subprocess."""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.integrations.instagram.downloader import (
    CONTENT_TYPES,
    ContentType,
    InstagramDownloader,
    LISTING_API_STATES,
    InstagramError,
    ProfileInfo,
    RemoteMedia,
    SESSION_RESULT_CODES,
    SESSION_TEST_RESULTS,
    SessionTestResult,
)
from app.integrations.instagram.gallerydl_runner import content_matches, redact
from app.integrations.instagram.validation import parse_profile_input, validate_shortcode

if TYPE_CHECKING:
    from app.integrations.instagram.session import InstagramSessionProvider

log = logging.getLogger(__name__)

# Teto do gallery-dl (o runner também limita a 5000). O provider web não tem teto de quantidade; "Todos"
# (limit None) e `photos_carousels` nunca chegam aqui (composite.web_only; a API recusa na criação).
GALLERY_DL_MAX_ITEMS = 5000

RUNNER_MODULE = "app.integrations.instagram.gallerydl_runner"
BACKEND_DIR = Path(__file__).resolve().parents[3]
DEFAULT_MAX_DOWNLOAD_BYTES = 2147483648
# Operações curtas (teste de sessão, perfil, post) têm este teto total, além do timeout de inatividade.
SHORT_OPERATION_TIMEOUT = 600.0
# Teto total do teste de sessão e da prévia de perfil (usados por endpoints síncronos).
SESSION_TEST_TIMEOUT = 30.0
PROFILE_PREVIEW_TIMEOUT = 45.0
# Linha do stdout do runner acima disso é descartada (teto de memória).
MAX_LINE_BYTES = 2 * 1024 * 1024
MAX_STDERR_LINE_BYTES = 64 * 1024
_STDERR_TAIL = 40
_EXPIRED_CODES = ("forbidden", "not_found")
_PROFILE_FIELDS = ("username", "user_id", "full_name", "avatar_url", "post_count", "is_private", "is_verified")
_MEDIA_FIELDS = (
    "source_platform", "source_profile", "source_post_id", "post_id", "media_id", "source_media_index",
    "media_type", "is_reel", "source_url", "media_url", "thumbnail_url", "width", "height", "duration",
    "taken_at", "extension",
)


class _TimeLimitExceeded(InstagramError):
    """Teto total ou de inatividade estourado (o filho já foi encerrado)."""

    def __init__(self, message: str):
        super().__init__("network_error", message, retryable=True)


def _profile_from_event(event: dict[str, Any]) -> ProfileInfo:
    return ProfileInfo(**{key: event.get(key) for key in _PROFILE_FIELDS})


def _media_from_event(event: dict[str, Any]) -> RemoteMedia:
    try:
        return RemoteMedia(**{key: event[key] for key in _MEDIA_FIELDS})
    except KeyError as exc:
        raise InstagramError("internal_error", f"Item do runner sem o campo {exc.args[0]}") from None


class GalleryDlInstagramDownloader(InstagramDownloader):
    def __init__(
        self,
        session_provider: InstagramSessionProvider,
        *,
        python_executable: str = sys.executable,
        temp_dir: Path,
        cdn_downloader: Callable[..., Any] | None = None,
        timeout: float = 300.0,
        max_total_time: float | None = None,
        sleep_request: tuple[float, float] | None = None,
        max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        backend_dir: Path = BACKEND_DIR,
        test_timeout: float = SESSION_TEST_TIMEOUT,
        profile_timeout: float = PROFILE_PREVIEW_TIMEOUT,
        request_gate: Any = None,
    ):
        self._provider = session_provider
        # Portão global (seção 8): cada operação do runner (um subprocess) ocupa UMA vaga enquanto roda.
        self._gate = request_gate
        self._python = python_executable
        self._temp_dir = Path(temp_dir)
        self._cdn_downloader = cdn_downloader
        # timeout = inatividade do filho (segundos sem nenhuma linha no stdout/stderr). O gallery-dl dorme
        # 6-12 s entre requisições e espera 60 s por tentativa em 429, por isso o padrão é generoso.
        self._timeout = float(timeout)
        self._max_total_time = float(max_total_time) if max_total_time is not None else None
        self._sleep_request = list(sleep_request) if sleep_request is not None else None
        self._max_download_bytes = int(max_download_bytes)
        self._backend_dir = Path(backend_dir)
        self._test_timeout = float(test_timeout)
        self._profile_timeout = float(profile_timeout)

    # ------------------------------------------------------------------ API pública

    def test_session(self) -> SessionTestResult:
        """Testa a sessão em no máximo ``test_timeout`` segundos; nunca lança InstagramError."""
        started = time.monotonic()

        def finish(result: str, message: str, username: str | None = None, http_status: int | None = None,
                   detail: str | None = None, recognized: bool | None = None, listing: str = "unknown",
                   code: str | None = None):
            return SessionTestResult(
                ok=result == "valid", code=code or SESSION_RESULT_CODES[result], message=message, username=username,
                result=result, http_status=http_status, duration_ms=int((time.monotonic() - started) * 1000),
                detail=detail, session_recognized=recognized, listing_api=listing,
            )

        try:
            events = list(self._run({"op": "test_session"}, timeout=self._test_timeout))
        except _TimeLimitExceeded:
            return finish(
                "timeout",
                f"O teste de sessão passou de {self._test_timeout:.0f} s e foi interrompido. Verifique a conexão "
                "e tente de novo em alguns minutos.",
            )
        except InstagramError as exc:
            result = SessionTestResult(ok=False, code=exc.code, message=exc.message, username=None).result
            if result == "session_missing":
                return finish(result, "Nenhuma sessão do Instagram configurada. Salve um sessionid em "
                                      "Configurações e teste de novo.")
            return finish(result, exc.message)
        event = next((e for e in events if e.get("type") == "session_test"), None)
        result = event.get("result") if event is not None else None
        if result not in SESSION_TEST_RESULTS or result == "session_missing":
            return finish("instagram_error", "O teste de sessão terminou sem um resultado reconhecível.")
        username = event.get("username") if result == "valid" else None
        if result == "valid" and not (isinstance(username, str) and username):
            return finish("instagram_error", "O Instagram não retornou o usuário da sessão.")
        status = event.get("http_status")
        status = status if isinstance(status, int) and not isinstance(status, bool) else None
        message = str(event.get("message") or "") or "O teste de sessão terminou sem mensagem."
        detail = event.get("detail")
        recognized = event.get("session_recognized")
        listing = event.get("listing_api")
        # Seção 7: recusa de autenticação sem evidência forte chega como instagram_error/request_auth_failed.
        auth_failed = result == "instagram_error" and event.get("code") == "request_auth_failed"
        return finish(result, message, username, status, detail if isinstance(detail, str) else None,
                      recognized if isinstance(recognized, bool) else None,
                      listing if listing in LISTING_API_STATES else "unknown",
                      "request_auth_failed" if auth_failed else None)

    def get_profile(self, username: str) -> ProfileInfo:
        username = self._username(username)
        cap = min(self._profile_timeout, self._short_timeout())
        try:
            for event in self._run({"op": "profile", "username": username}, timeout=cap):
                if event.get("type") == "profile":
                    return _profile_from_event(event)
        except _TimeLimitExceeded as exc:
            raise InstagramError("timeout", exc.message) from None
        raise InstagramError("internal_error", "O runner não retornou o perfil.")

    def iter_profile_media(
        self,
        username: str,
        content: ContentType,
        limit: int | None,
        *,
        cancel: threading.Event | None = None,
        resume: Any = None,
        on_checkpoint: Any = None,
    ) -> Iterator[RemoteMedia]:
        # gallery-dl não retoma nem emite checkpoint: `resume`/`on_checkpoint` são ignorados (a listagem recomeça
        # do início; o índice único da pipeline descarta repetidos).
        username = self._username(username)
        if content not in CONTENT_TYPES:
            raise InstagramError("invalid_input", "Tipo de conteúdo inválido.")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0):
            raise InstagramError("invalid_input", "Limite inválido.")
        effective = min(limit, GALLERY_DL_MAX_ITEMS) if limit is not None else GALLERY_DL_MAX_ITEMS
        return self._iter_media(username, content, effective, cancel)

    # ------------------------------------------------------------------ diagnóstico (fora do contrato)

    def inspect_request(self, username: str = "instagram") -> list[dict[str, Any]]:
        """Descreve, sem enviar, as requisições do teste de sessão e da 1a chamada da listagem.

        Só nomes de cookies (present/absent), domínio/path do jar e headers não sensíveis; nunca valores.
        """
        username = self._username(username)
        events = self._run({"op": "inspect_request", "username": username}, timeout=self._test_timeout)
        return [e for e in events if e.get("type") == "inspect"]

    def probe(self, endpoint: str, *, method: str = "GET", params: dict[str, str] | None = None,
              data: dict[str, str] | None = None) -> dict[str, Any]:
        """UMA requisição real ao endpoint (/api/v1/...), sem seguir redirect, sem nova tentativa e sem dormir
        em 429. Devolve status, Location sem query, tipo de redirect, tamanho, content-type e chaves do JSON."""
        request = {"op": "probe", "method": method, "endpoint": endpoint, "params": params or {}, "data": data or {}}
        events = list(self._run(request, timeout=self._test_timeout))
        return next((e for e in events if e.get("type") == "probe"), {})

    def resolve_post(self, shortcode: str, *, cancel: threading.Event | None = None) -> list[RemoteMedia]:
        try:
            shortcode = validate_shortcode(shortcode)
        except ValueError as exc:
            raise InstagramError("invalid_input", str(exc)) from None
        events = self._run({"op": "resolve_post", "shortcode": shortcode}, timeout=self._short_timeout(),
                           cancel=cancel)
        return [_media_from_event(e) for e in events if e.get("type") == "item"]

    def download(
        self,
        item: RemoteMedia,
        dest_dir: Path,
        *,
        cancel: threading.Event | None = None,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> Path:
        from app.services.cdn_downloader import DownloadError

        cdn = self._cdn_downloader
        if cdn is None:
            from app.services.cdn_downloader import cdn_download as cdn

        def fetch(url: str) -> Path:
            result = cdn(url, Path(dest_dir), max_bytes=self._max_download_bytes, cancel=cancel, progress=progress)
            return Path(result.path)

        try:
            return fetch(item.media_url)
        except DownloadError as exc:
            if exc.code not in _EXPIRED_CODES:
                raise self._download_error(exc) from None
            if cancel is not None and cancel.is_set():
                raise
            log.info("URL de mídia expirada para %s#%s; renovando", item.source_post_id, item.source_media_index)

        fresh = self._match_fresh(item, self.resolve_post(item.source_post_id))
        if fresh is None:
            raise InstagramError("content_unavailable", "A mídia não está mais disponível no post.")
        try:
            return fetch(fresh.media_url)
        except DownloadError as exc:
            raise self._download_error(exc) from None

    # ------------------------------------------------------------------ internos

    def _short_timeout(self) -> float:
        if self._max_total_time is None:
            return SHORT_OPERATION_TIMEOUT
        return min(self._max_total_time, SHORT_OPERATION_TIMEOUT)

    @staticmethod
    def _username(username: str) -> str:
        try:
            return parse_profile_input(username)
        except ValueError as exc:
            raise InstagramError("invalid_input", str(exc)) from None

    @staticmethod
    def _match_fresh(item: RemoteMedia, candidates: list[RemoteMedia]) -> RemoteMedia | None:
        if item.media_id:
            for candidate in candidates:
                if candidate.media_id == item.media_id:
                    return candidate
        for candidate in candidates:
            if candidate.source_media_index == item.source_media_index and candidate.media_type == item.media_type:
                return candidate
        return None

    @staticmethod
    def _download_error(exc: Exception) -> Exception:
        code = getattr(exc, "code", None)
        if code == "cancelled":
            return exc
        if code == "network_error":
            return InstagramError("download_failed", "Falha de rede ao baixar a mídia.", retryable=True)
        if code in _EXPIRED_CODES:
            return InstagramError("download_failed", "A CDN recusou a URL da mídia mesmo após renovar.", retryable=False)
        if code == "too_large":
            return InstagramError("download_failed", "Arquivo de mídia maior que o limite permitido.", retryable=False)
        if code == "invalid_media":
            return InstagramError("download_failed", "O arquivo baixado não é uma mídia válida.", retryable=False)
        if code == "invalid_url":
            return InstagramError("download_failed", "URL de mídia recusada pela validação de CDN.", retryable=False)
        return InstagramError("download_failed", retryable=False)

    def _iter_media(
        self, username: str, content: str, limit: int, cancel: threading.Event | None
    ) -> Iterator[RemoteMedia]:
        request = {"op": "list", "username": username, "content": content, "limit": limit}
        count = 0
        for event in self._run(request, timeout=self._max_total_time, cancel=cancel):
            if event.get("type") != "item":
                continue
            media = _media_from_event(event)
            if not content_matches(content, media.media_type):
                continue
            yield media
            count += 1
            if count >= limit:
                return

    def _child_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k.upper() != "INSTAGRAM_SESSIONID"}
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        if self._temp_dir.is_dir():
            env["TEMP"] = env["TMP"] = env["TMPDIR"] = str(self._temp_dir)
        return env

    def _run(
        self, request: dict[str, Any], *, timeout: float | None = None, cancel: threading.Event | None = None
    ) -> Iterator[dict[str, Any]]:
        """Executa o runner e produz os eventos profile/item. Lança InstagramError em erro.

        ``timeout`` é o teto total opcional (inclui o tempo do consumidor); a inatividade do filho é
        limitada por ``self._timeout``.
        """
        session = self._provider.get()
        if session is None:
            raise InstagramError("session_missing")
        return self._stream(session, request, timeout, cancel)

    def _stream(self, session, request, timeout, cancel) -> Iterator[dict[str, Any]]:
        from app.services.instagram_gate import GateCancelled

        try:
            with self._gate.slot(cancel) if self._gate is not None else nullcontext():
                yield from self._stream_locked(session, request, timeout, cancel)
        except GateCancelled:   # só a espera pela vaga levanta isto (o cancelamento já está pedido)
            log.info("Operação do Instagram cancelada enquanto aguardava a vez no portão")

    def _stream_locked(self, session, request, timeout, cancel) -> Iterator[dict[str, Any]]:
        cookies = session.cookies()
        if not cookies.get("sessionid"):
            raise InstagramError("session_missing")
        secrets = [v for v in cookies.values() if v]
        payload = dict(request, cookies=cookies)
        if self._sleep_request is not None:
            payload["sleep_request"] = self._sleep_request
        data = json.dumps(payload).encode("utf-8")
        del payload, cookies

        args = [self._python, "-m", RUNNER_MODULE]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            proc = subprocess.Popen(
                args,
                cwd=str(self._backend_dir),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env=self._child_env(),
                creationflags=creationflags,
            )
        except OSError as exc:
            raise InstagramError("internal_error", f"Não foi possível iniciar o runner ({type(exc).__name__}).") from None

        lines: queue.Queue[bytes | None] = queue.Queue()
        stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL)

        last_activity = [time.monotonic()]

        def read_stdout() -> None:
            discarding = False
            while True:
                raw = proc.stdout.readline(MAX_LINE_BYTES + 1)
                if not raw:
                    break
                last_activity[0] = time.monotonic()
                complete = raw.endswith(b"\n")
                if discarding or len(raw) > MAX_LINE_BYTES:
                    if not discarding:
                        log.warning("Linha do runner maior que %d bytes descartada", MAX_LINE_BYTES)
                    discarding = not complete
                    continue
                lines.put(raw)
            lines.put(None)

        def read_stderr() -> None:
            discarding = False
            while True:
                raw = proc.stderr.readline(MAX_STDERR_LINE_BYTES)
                if not raw:
                    break
                last_activity[0] = time.monotonic()
                if not discarding:
                    stderr_tail.append(redact(raw.decode("utf-8", "replace").rstrip(), secrets))
                discarding = not raw.endswith(b"\n")

        threads = [threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=read_stderr, daemon=True)]
        for thread in threads:
            thread.start()

        finished = False
        try:
            try:
                proc.stdin.write(data)
                proc.stdin.close()
            except OSError:
                pass
            del data
            deadline = time.monotonic() + timeout if timeout is not None else None
            while True:
                if cancel is not None and cancel.is_set():
                    log.info("Listagem do Instagram cancelada")
                    return
                if deadline is not None and time.monotonic() > deadline:
                    raise _TimeLimitExceeded(
                        f"O Instagram não respondeu dentro do limite de {timeout:.0f} s. "
                        "Tente de novo em alguns minutos."
                    )
                try:
                    raw = lines.get(timeout=0.2)
                except queue.Empty:
                    if time.monotonic() - last_activity[0] > self._timeout:
                        raise _TimeLimitExceeded(
                            "O runner ficou sem responder além do tempo limite de inatividade."
                        ) from None
                    continue
                if raw is None:
                    break
                try:
                    event = json.loads(raw.decode("utf-8"))
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")
                if kind == "error":
                    message = redact(str(event.get("message") or ""), secrets) or None
                    raise InstagramError(str(event.get("code") or "internal_error"), message)
                if kind == "done":
                    finished = True
                    continue
                if kind in ("inspect", "probe"):
                    # Defesa extra: o runner não emite valores, mas redige qualquer coisa que pareça segredo.
                    yield json.loads(redact(json.dumps(event), secrets))
                if kind == "session_test":
                    event["message"] = redact(str(event.get("message") or ""), secrets)
                    yield event
                if kind in ("profile", "item"):
                    yield event
            try:
                wait = 10.0 if deadline is None else max(0.1, min(10.0, deadline - time.monotonic()))
                proc.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                # O filho fechou o stdout mas não terminou; o finally o encerra.
                raise _TimeLimitExceeded("O runner não terminou depois de concluir a saída.") from None
            if not finished:
                raise InstagramError("internal_error", f"O runner terminou sem concluir (código {proc.returncode}).")
        finally:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
            for thread in threads:
                thread.join(timeout=2)
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
            if stderr_tail:
                log.debug("gallery-dl (redigido):\n%s", "\n".join(stderr_tail))
