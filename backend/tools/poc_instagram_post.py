"""POC da consulta de post isolado do provider web do Instagram: SÓ resolução (sem banco, sem download).

Uso: ``.venv\\Scripts\\python.exe backend\\tools\\poc_instagram_post.py --url <link da publicação>``

A consulta de post (queries.POST_BY_SHORTCODE) NÃO foi verificada no Instagram real: esta POC existe para o dono
verificá-la, e só deve ser rodada com autorização dele. Tráfego: 1 GET do documento + 1 POST GraphQL, nada mais.
Usa a sessão salva (LocalInstagramSessionProvider) e o InstagramWebDownloader real, retries 0 e sem re-bootstrap.
Para no primeiro erro, imprimindo só code/message/stage/detail seguros, o diagnóstico HTTP seguro quando houver
(status, redirect, content_type, size) e os NOMES das chaves da resposta (para ajustar doc_id/campo raiz). Conta TODAS as requisições (método, path sem query, status) e confirma 0 requisições ao
CDN. Recusa rodar se o app estiver escutando na porta configurada. Relatório JSON no stdout sem ids, cookies,
tokens, shortcode ou URLs. O override de doc_id gravado no banco (setting) não é lido; vale o env/.env
(MEDIA_TOOL_IG_DOC_<NAME>, MEDIA_TOOL_IG_ROOT_<NAME>) ou o valor embutido.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
from collections.abc import Callable, Mapping
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
from app.integrations.instagram.validation import parse_post_link  # noqa: E402
from app.integrations.instagram.web.bootstrap import WebBootstrap  # noqa: E402
from app.integrations.instagram.web.errors import InstagramWebError  # noqa: E402
from app.integrations.instagram.web.graphql import InstagramWebGraphQL  # noqa: E402
from app.integrations.instagram.web.provider import InstagramWebDownloader  # noqa: E402
from app.integrations.instagram.web.queries import POST_BY_SHORTCODE, resolve_spec  # noqa: E402
from app.jobs.errors import safe_diagnostic, safe_message  # noqa: E402
from app.services.cdn_downloader import ALLOWED_HOST_SUFFIXES, DownloadError, validate_cdn_url  # noqa: E402

INSTAGRAM_HOSTS = ("instagram.com", "www.instagram.com")
_DETAIL_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# Nome de chave/campo/tipo seguro para o relatório (nunca valores da resposta).
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
_MAX_KEYS = 60


def _keys(value: object) -> list[str] | None:
    if not isinstance(value, (Mapping, list)):
        return None
    return sorted(k for k in value if isinstance(k, str) and _KEY_RE.match(k))[:_MAX_KEYS]


def safe_error(exc: BaseException) -> dict[str, Any]:
    if not isinstance(exc, InstagramError):
        return {"code": "internal_error", "message": f"Erro inesperado ({type(exc).__name__}); veja o log do app.",
                "stage": None, "detail": None}
    diagnostic = getattr(exc, "diagnostic", None)
    diagnostic = diagnostic if isinstance(diagnostic, dict) else {}
    detail = diagnostic.get("detail")
    stage = getattr(exc, "stage", None)
    error = {
        "code": str(exc.code or "internal_error"),
        "message": safe_message(exc.message or ""),
        "stage": stage if isinstance(stage, str) and _DETAIL_RE.match(stage) else None,
        "detail": detail if isinstance(detail, str) and _DETAIL_RE.match(detail) else None,
    }
    # Campos seguros do diagnóstico HTTP (403 ou redirect? de que tipo?), pela MESMA lista de permissão do log
    # (jobs.errors.safe_diagnostic). O `path` fica de fora: já está em `requests`.
    for part in safe_diagnostic(exc).split():
        key, _, value = part.partition("=")
        if key in ("status", "size") and value.isdigit():
            error[key] = int(value)
        elif key in ("redirect", "content_type") and value:
            error[key] = value
    # Nomes das chaves da resposta que falhou (ex.: `data_keys` mostra o campo raiz que o Instagram devolveu).
    for name in ("top_keys", "data_keys"):
        keys = _keys(diagnostic.get(name))
        if keys is not None:
            error[name] = keys
    return error


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
        "session_invalid", "O Instagram não aceitou a sessão salva para consultar a publicação. A POC não "
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


def response_shape(root: Mapping) -> dict[str, Any]:
    """Forma da raiz da resposta: só NOMES de chaves e de tipo, e contagens; nunca valores."""
    shape: dict[str, Any] = {"root_keys": _keys(root)}
    items = root.get("items")
    if isinstance(items, list):
        shape["format"] = "v1_items"
        shape["items_count"] = len(items)
        first = next((i for i in items if isinstance(i, Mapping)), None)
        shape["item_keys"] = _keys(first)
        return shape
    media_type = root.get("media_type")
    if isinstance(media_type, int) and not isinstance(media_type, bool):
        shape["format"] = "v1_node"
        return shape
    shape["format"] = "legacy"
    typename = root.get("__typename")
    shape["typename"] = typename if isinstance(typename, str) and _KEY_RE.match(typename) else None
    sidecar = root.get("edge_sidecar_to_children")
    edges = sidecar.get("edges") if isinstance(sidecar, Mapping) else None
    if isinstance(edges, list):
        shape["children_count"] = len(edges)
        child = next((e.get("node") for e in edges if isinstance(e, Mapping) and isinstance(e.get("node"), Mapping)),
                     None)
        shape["child_keys"] = _keys(child)
    return shape


class _ShapeGraphQL(InstagramWebGraphQL):
    """GraphQL real que só anota a forma (nomes de chaves) da raiz do post para o relatório."""

    shape_sink: dict[str, Any]

    def post_by_shortcode(self, shortcode: str) -> dict:
        root = super().post_by_shortcode(shortcode)
        self.shape_sink["shape"] = response_shape(root)
        return root


def _item(item: RemoteMedia, shortcode: str) -> dict[str, Any]:
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
    return {"media_type": item.media_type, "is_reel": item.is_reel, "source_media_index": item.source_media_index,
            "taken_at": item.taken_at, "cdn_url_valid": valid, "cdn_domain_suffix": _cdn_suffix(host),
            "shortcode_matches": item.source_post_id == shortcode, "owner_present": bool(item.source_profile)}


def run(
    args: argparse.Namespace,
    *,
    settings: Settings,
    session_provider: Any = None,
    transport: httpx.BaseTransport | None = None,
    is_app_running: Callable[[Settings], bool] = app_running,
) -> tuple[int, dict[str, Any]]:
    """Exit code 0 ok, 1 erro (link inválido ou falha na resolução), 2 pré-condição (app aberto), 130 interrompido
    (Ctrl+C)."""
    spec = resolve_spec(POST_BY_SHORTCODE, {}, settings.env)
    # O doc_id não é segredo; nome/raiz efetivos ajudam a conferir qual consulta foi testada.
    report: dict[str, Any] = {"query": {"name": spec.name, "doc_id": spec.doc_id, "root_field": spec.root_field,
                                        "observed_at": spec.observed_at}}
    if is_app_running(settings):
        report["outcome"] = "app_running"
        report["error"] = {"code": "app_running", "message": (
            "O Media Tool está aberto. Feche o app (scripts\\stop.ps1) antes da POC.")}
        return 2, report
    try:
        shortcode, normalized = parse_post_link(args.url)
    except ValueError as exc:
        # A entrada crua não vai para o relatório; a mensagem de validação é fixa (não repete a entrada).
        report["outcome"] = "invalid_input"
        report["error"] = {"code": "invalid_input", "message": str(exc), "stage": None, "detail": None}
        report["requests"] = []
        return 1, report
    report["link_kind"] = "reel" if "/reel/" in normalized else "p"

    raw: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    cdn_calls: list[int] = []
    sink: dict[str, Any] = {}

    def cdn_guard(*_args: Any, **_kwargs: Any) -> Any:
        cdn_calls.append(1)
        raise RuntimeError("download desativado na POC de post")

    def graphql_factory(http: Any, bootstrap: Any) -> InstagramWebGraphQL:
        graphql = _ShapeGraphQL(http, bootstrap, env=settings.env)
        graphql.shape_sink = sink
        return graphql

    counting = _CountingTransport(transport or httpx.HTTPTransport(retries=0, trust_env=False), raw)
    downloader = InstagramWebDownloader(
        session_provider or LocalInstagramSessionProvider(settings.secrets_dir, env=settings.env),
        cdn_downloader=cdn_guard,
        env=settings.env,
        transport=counting,
        bootstrap_factory=lambda http: _NoRetryBootstrap(WebBootstrap(http)),
        graphql_factory=graphql_factory,
    )
    error: dict[str, Any] | None = None
    try:
        configure_logging(settings.log_dir)
        items = [_item(media, shortcode) for media in downloader.resolve_post(shortcode)]
    except KeyboardInterrupt:
        error = {"code": "interrupted", "message": "POC interrompida pelo usuário (Ctrl+C).", "stage": None,
                 "detail": None}
    except Exception as exc:  # noqa: BLE001 - qualquer erro para a POC com dados seguros
        error = safe_error(exc)

    instagram = [r for r in raw if r["host"] in INSTAGRAM_HOSTS]
    cdn = [r for r in raw if _cdn_suffix(r["host"])]
    report.update({
        "requests": [{"method": r["method"], "path": r["path"], "status": r["status"]} for r in instagram],
        "instagram_requests": len(instagram),
        "cdn_requests": len(cdn) + len(cdn_calls),
        "other_requests": len(raw) - len(instagram) - len(cdn),
        "response_shape": sink.get("shape"),
        "media_count": len(items),
        "items": items,
    })
    if error is not None:
        report["outcome"] = error["code"]
        report["error"] = error
        return (130 if error["code"] == "interrupted" else 1), report
    report["outcome"] = "ok"
    return 0, report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="POC da consulta de post isolado do Instagram (só resolução).")
    parser.add_argument("--url", required=True, help="link da publicação (/p/, /reel/ ou /reels/)")
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
