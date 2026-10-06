"""POC de paginação do provider web do Instagram: SÓ listagem (sem banco, sem download, sem processamento).

Uso: ``.venv\\Scripts\\python.exe backend\\tools\\poc_instagram_pagination.py --profile <perfil>
[--page-size 1] [--limit 2]``

Padrão page_size=1 e limit=2: força exatamente 2 páginas com o mínimo de tráfego (1 GET do documento + 2 POSTs
GraphQL). Usa a sessão salva (LocalInstagramSessionProvider) e o InstagramWebDownloader real com conteúdo ``all``,
retries 0 e sem re-bootstrap. Para no primeiro erro (rate_limited, checkpoint_required, action_blocked,
access_refused, contract_changed, session_invalid ou qualquer exceção), imprimindo só code/message/detail seguros.
Teto duro de 2 páginas (max_pages=2): nunca mais de 2 POSTs. Só 1 página -> ``inconclusive_single_page``; limite
não atingido em 2 páginas -> ``inconclusive_limit_not_reached`` (saída 3). Ctrl+C -> ``interrupted`` (saída 130).
Conta TODAS as requisições (método, path sem query, status) e confirma 0 requisições ao CDN. Recusa rodar se o app
estiver escutando na porta configurada. Relatório JSON sem ids, cookies, tokens, cursores ou URLs no stdout.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import Settings, load_settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.integrations.instagram.downloader import InstagramError, RemoteMedia  # noqa: E402
from app.integrations.instagram.session import LocalInstagramSessionProvider  # noqa: E402
from app.integrations.instagram.validation import parse_profile_input  # noqa: E402
from app.integrations.instagram.web.bootstrap import WebBootstrap  # noqa: E402
from app.integrations.instagram.web.errors import InstagramWebError  # noqa: E402
from app.integrations.instagram.web.provider import InstagramWebDownloader  # noqa: E402
from app.jobs.errors import safe_message  # noqa: E402
from app.services.cdn_downloader import ALLOWED_HOST_SUFFIXES, DownloadError, validate_cdn_url  # noqa: E402

CONTENT = "all"
MAX_PAGES = 2   # teto duro: nunca mais de 2 POSTs GraphQL, mesmo com posts sem URL/não suportados
INSTAGRAM_HOSTS = ("instagram.com", "www.instagram.com")
GRAPHQL_PATH = "/graphql/query"
_DETAIL_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def safe_error(exc: BaseException) -> dict[str, Any]:
    if not isinstance(exc, InstagramError):
        return {"code": "internal_error", "message": f"Erro inesperado ({type(exc).__name__}); veja o log do app.",
                "stage": None, "detail": None}
    diagnostic = getattr(exc, "diagnostic", None)
    detail = diagnostic.get("detail") if isinstance(diagnostic, dict) else None
    stage = getattr(exc, "stage", None)
    return {
        "code": str(exc.code or "internal_error"),
        "message": safe_message(exc.message or ""),
        "stage": stage if isinstance(stage, str) and _DETAIL_RE.match(stage) else None,
        "detail": detail if isinstance(detail, str) and _DETAIL_RE.match(detail) else None,
    }


def app_running(settings: Settings) -> bool:
    """True se o backend do Media Tool já escuta na porta configurada (mesma regra do poc_instagram_web)."""
    try:
        with socket.create_connection((settings.host, settings.port), timeout=0.5):
            return True
    except OSError:
        return False


def _cdn_suffix(host: str) -> str | None:
    return next((s for s in ALLOWED_HOST_SUFFIXES if host.endswith(s) and len(host) > len(s)), None)


class _CountingTransport(httpx.BaseTransport):
    """Registra método, host, path (sem query) e status de cada requisição; nunca a URL completa nem o corpo."""

    def __init__(self, inner: httpx.BaseTransport, log: list[dict[str, Any]]) -> None:
        self._inner = inner
        self._log = log

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        entry: dict[str, Any] = {"method": request.method, "host": (request.url.host or "").lower(),
                                 "path": request.url.path, "status": None}
        self._log.append(entry)
        response = self._inner.handle_request(request)
        entry["status"] = response.status_code
        return response

    def close(self) -> None:
        self._inner.close()


def _token_rejected() -> InstagramWebError:
    return InstagramWebError(
        "session_invalid", "O Instagram não aceitou a sessão salva para consultar publicações. A POC não "
        "refaz o bootstrap (sem nova tentativa).", stage="graphql", diagnostic={"detail": "web_token_rejected"})


class _NoRetryBootstrap:
    """WebBootstrap sem re-bootstrap: o pedido de refazer (token recusado) vira erro na hora, sem nova requisição
    e antes do aviso de re-bootstrap no log."""

    def __init__(self, inner: WebBootstrap) -> None:
        self._inner = inner

    def get(self, *, force: bool = False):
        if force:
            raise _token_rejected()
        return self._inner.get()

    def invalidate(self) -> None:
        raise _token_rejected()


def _item(item: RemoteMedia) -> dict[str, Any]:
    url = item.media_url
    try:
        validate_cdn_url(url)
        valid = True
    except DownloadError:
        valid = False
    try:
        host = (urlsplit(url).hostname or "").lower() if isinstance(url, str) else ""
    except ValueError:
        host = ""
    return {"shortcode": item.source_post_id, "media_type": item.media_type, "is_reel": item.is_reel,
            "source_media_index": item.source_media_index, "taken_at": item.taken_at,
            "cdn_url_valid": valid, "cdn_domain_suffix": _cdn_suffix(host)}


def run(
    args: argparse.Namespace,
    *,
    settings: Settings,
    session_provider: Any = None,
    transport: httpx.BaseTransport | None = None,
    page_delay: tuple[float, float] | None = None,
    is_app_running: Callable[[Settings], bool] = app_running,
) -> tuple[int, dict[str, Any]]:
    """Exit code 0 ok, 1 erro na listagem, 2 pré-condição (app aberto), 3 inconclusivo (só 1 página ou limite não
    atingido em MAX_PAGES páginas), 130 interrompido (Ctrl+C)."""
    try:
        profile: str | None = parse_profile_input(args.profile)
    except ValueError:
        profile = None   # o provider recusa com invalid_input; a entrada crua não vai para o relatório
    report: dict[str, Any] = {"profile": profile, "content": CONTENT, "page_size": args.page_size,
                              "limit": args.limit, "max_pages": MAX_PAGES}
    if is_app_running(settings):
        report["outcome"] = "app_running"
        report["error"] = {"code": "app_running", "message": (
            "O Media Tool está aberto. Feche o app (scripts\\stop.ps1) antes da POC.")}
        return 2, report

    raw: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    cdn_calls: list[int] = []

    def cdn_guard(*_args: Any, **_kwargs: Any) -> Any:
        cdn_calls.append(1)
        raise RuntimeError("download desativado na POC de paginação")

    def graphql_posts() -> int:
        return sum(1 for r in raw if r["method"] == "POST" and r["path"] == GRAPHQL_PATH)

    counting = _CountingTransport(transport or httpx.HTTPTransport(retries=0, trust_env=False), raw)
    downloader = InstagramWebDownloader(
        session_provider or LocalInstagramSessionProvider(settings.secrets_dir, env=settings.env),
        cdn_downloader=cdn_guard,
        env=settings.env,
        page_delay=page_delay,
        transport=counting,
        page_size=args.page_size,
        bootstrap_factory=lambda http: _NoRetryBootstrap(WebBootstrap(http)),
        on_page=pages.append,
        max_pages=MAX_PAGES,
    )
    error: dict[str, Any] | None = None
    posts_at_limit: int | None = None
    listing = None
    try:
        configure_logging(settings.log_dir)
        listing = downloader.iter_profile_media(args.profile, CONTENT, args.limit)
        for media in listing:
            items.append(_item(media))
            if len(items) >= args.limit and posts_at_limit is None:
                posts_at_limit = graphql_posts()
    except KeyboardInterrupt:
        error = {"code": "interrupted", "message": "POC interrompida pelo usuário (Ctrl+C).", "stage": None,
                 "detail": None}
    except Exception as exc:  # noqa: BLE001 - qualquer erro para a POC com dados seguros
        error = safe_error(exc)
    finally:
        close = getattr(listing, "close", None)
        if close is not None:
            try:
                close()
            except Exception:  # noqa: BLE001 - fechamento não muda o resultado
                pass

    instagram = [r for r in raw if r["host"] in INSTAGRAM_HOSTS]
    cdn = [r for r in raw if _cdn_suffix(r["host"])]
    report.update({
        "requests": [{"method": r["method"], "path": r["path"], "status": r["status"]} for r in instagram],
        "instagram_requests": len(instagram),
        "cdn_requests": len(cdn) + len(cdn_calls),
        "other_requests": len(raw) - len(instagram) - len(cdn),
        "pages": pages,
        "items": items,
        "duplicates_across_pages": sum(p["duplicate_nodes"] for p in pages if p["page_index"] > 1),
        "stopped_at_limit": posts_at_limit is not None and error is None and graphql_posts() == posts_at_limit,
        "account_confirmation": downloader.last_account_confirmation,
    })
    if error is not None:
        report["outcome"] = error["code"]
        report["error"] = error
        return (130 if error["code"] == "interrupted" else 1), report
    if len(pages) < 2:
        # A 1a página já bastou (ou o perfil só tem uma): a página 2 não foi testada; nenhuma requisição extra.
        report["outcome"] = "inconclusive_single_page"
        return 3, report
    if len(items) < args.limit:
        report["outcome"] = "inconclusive_limit_not_reached"
        return 3, report
    report["outcome"] = "ok"
    return 0, report


def _positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("deve ser maior que zero")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="POC de paginação do provider web do Instagram (só listagem).")
    parser.add_argument("--profile", required=True, help="perfil alvo (@usuario, usuario ou URL)")
    parser.add_argument("--page-size", type=_positive, default=1, help="itens por página (padrão 1)")
    parser.add_argument("--limit", type=_positive, default=2, help="mídias a listar (padrão 2)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        code, report = run(args, settings=load_settings())
    except KeyboardInterrupt:
        code, report = 130, {"outcome": "interrupted", "error": {
            "code": "interrupted", "message": "POC interrompida pelo usuário (Ctrl+C).", "stage": None, "detail": None}}
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
