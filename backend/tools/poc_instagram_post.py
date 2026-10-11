"""POC do documento público de UMA publicação do Instagram: SÓ leitura (sem banco, sem download, SEM sessão).

Uso: ``.venv\\Scripts\\python.exe backend\\tools\\poc_instagram_post.py --url <link da publicação>``

A importação por link lê a publicação do documento público (web/post_document.py), aberto por um cliente ANÔNIMO.
Isso foi verificado no Instagram real só para UM Reel público (2026-10-10): esta POC existe para o dono conferir os
casos NÃO verificados (foto, carrossel, caminho ``/p/``, publicação restrita, redirect), e só deve ser rodada com
autorização dele. Tráfego: UM GET do documento da publicação, nada mais: sem cookies, sem a sessão salva, retries 0,
sem seguir redirect e sem nenhuma requisição ao CDN. Usa o InstagramWebDownloader real (mesma classificação de erros
do app). Para no primeiro erro. Conta TODAS as requisições (método, tipo do caminho, status) e recusa rodar se o app
estiver escutando na porta configurada. Relatório JSON no stdout só com status, tipo de redirect, content-type,
tamanho, o resumo da leitura (contagens) e o resumo das mídias (quantidade, tipos, maior largura/altura, se as
versões de vídeo trazem tamanho): nunca URL, HTML, shortcode, ids, cookies ou tokens.
"""

from __future__ import annotations

import argparse
import dataclasses
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
from app.integrations.instagram.validation import parse_post_link, post_path_kind  # noqa: E402
from app.integrations.instagram.web.errors import InstagramWebError, redirect_kind  # noqa: E402
from app.integrations.instagram.web.http import InstagramWebHttp  # noqa: E402
from app.integrations.instagram.web.post_document import OBSERVED_AT, parse_post_document  # noqa: E402
from app.integrations.instagram.web.provider import InstagramWebDownloader  # noqa: E402
from app.jobs.errors import safe_diagnostic, safe_message  # noqa: E402
from app.services.cdn_downloader import ALLOWED_HOST_SUFFIXES, DownloadError, validate_cdn_url  # noqa: E402

INSTAGRAM_HOSTS = ("instagram.com", "www.instagram.com")
_DETAIL_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CONTENT_TYPE_RE = re.compile(r"^[a-z]{1,20}/[a-z0-9.+-]{1,40}$")
_PRODUCT_TYPE_RE = re.compile(r"^[a-z_]{1,40}$")
_MAX_LISTED = 20


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
                                 "path": request.url.path, "status": None,
                                 "cookie_sent": "cookie" in request.headers}
        self._log.append(entry)
        response = self._inner.handle_request(request)
        entry["status"] = response.status_code
        return response

    def close(self) -> None:
        self._inner.close()


class _NoSession:
    """A POC não usa a sessão salva: qualquer leitura dela é erro de programação."""

    def get(self) -> Any:
        raise RuntimeError("a POC do documento público não lê a sessão salva")


class _RecordingHttp:
    """Cliente anônimo real que guarda a última resposta do documento para o relatório (só em memória)."""

    def __init__(self, inner: InstagramWebHttp) -> None:
        self._inner = inner
        self.cancel = None
        self.last: Any = None

    def get_document(self, path: str, **kwargs: Any) -> Any:
        self.last = self._inner.get_document(path, **kwargs)
        return self.last

    def close(self) -> None:
        self._inner.close()


def document_summary(resp: Any) -> dict[str, Any]:
    """Só status, tipo de redirect, content-type (sem parâmetros) e tamanho; nunca `Location` nem corpo."""
    content_type = (resp.content_type or "").split(";", 1)[0].strip().lower()
    return {"status": resp.status,
            "redirect_kind": redirect_kind(resp.location_path) if 300 <= resp.status < 400 else None,
            "content_type": content_type if _CONTENT_TYPE_RE.match(content_type) else None,
            "size": len(resp.text or "")}


def _dimension(entries: object, key: str) -> int | None:
    values = [e.get(key) for e in entries if isinstance(e, Mapping)] if isinstance(entries, list) else []
    values = [v for v in values if isinstance(v, int) and not isinstance(v, bool) and 0 < v < 100000]
    return max(values) if values else None


def _media_shape(item: Mapping) -> dict[str, Any]:
    versions = item.get("image_versions2")
    candidates = versions.get("candidates") if isinstance(versions, Mapping) else None
    videos = item.get("video_versions")
    video_list = [v for v in videos if isinstance(v, Mapping)] if isinstance(videos, list) else []
    types = [v.get("type") for v in video_list]
    media_type = item.get("media_type")
    return {
        "media_type": media_type if isinstance(media_type, int) and not isinstance(media_type, bool) else None,
        "image_candidates": len(candidates) if isinstance(candidates, list) else 0,
        "max_image_width": _dimension(candidates, "width"),
        "max_image_height": _dimension(candidates, "height"),
        "video_versions": len(video_list),
        "video_versions_have_size": bool(video_list) and all(
            _dimension([v], "width") and _dimension([v], "height") for v in video_list),
        "video_version_types": [t for t in types if isinstance(t, int) and not isinstance(t, bool)
                                and 0 <= t < 10000][:_MAX_LISTED],
    }


def node_summary(node: Mapping) -> dict[str, Any]:
    """Forma do nó da mídia: só tipos numéricos, contagens e dimensões; nunca valores de URL, id ou texto."""
    product_type = node.get("product_type")
    summary = _media_shape(node)
    summary["product_type"] = product_type if isinstance(product_type, str) and _PRODUCT_TYPE_RE.match(
        product_type) else None
    for key in ("original_width", "original_height"):
        summary[key] = _dimension([node], key)
    children = node.get("carousel_media")
    if isinstance(children, list):
        summary["carousel_children"] = len(children)
        summary["children"] = [_media_shape(c) for c in children if isinstance(c, Mapping)][:_MAX_LISTED]
    return summary


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
            "width": item.width, "height": item.height, "taken_at": item.taken_at, "cdn_url_valid": valid,
            "cdn_domain_suffix": _cdn_suffix(host), "shortcode_matches": item.source_post_id == shortcode,
            "owner_present": bool(item.source_profile), "thumbnail_present": bool(item.thumbnail_url)}


def run(
    args: argparse.Namespace,
    *,
    settings: Settings,
    transport: httpx.BaseTransport | None = None,
    is_app_running: Callable[[Settings], bool] = app_running,
) -> tuple[int, dict[str, Any]]:
    """Exit code 0 ok, 1 erro (link inválido ou falha na leitura), 2 pré-condição (app aberto), 130 interrompido
    (Ctrl+C)."""
    report: dict[str, Any] = {"source": "public_document", "verified_at": OBSERVED_AT, "session_used": False}
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
    kind = post_path_kind(normalized)
    report["link_kind"] = kind
    document_path = f"/{kind}/{shortcode}/"

    raw: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    cdn_calls: list[int] = []

    def cdn_guard(*_args: Any, **_kwargs: Any) -> Any:
        cdn_calls.append(1)
        raise RuntimeError("download desativado na POC de post")

    counting = _CountingTransport(transport or httpx.HTTPTransport(retries=0, trust_env=False), raw)
    recorder = _RecordingHttp(InstagramWebHttp.anonymous(transport=counting))
    downloader = InstagramWebDownloader(_NoSession(), cdn_downloader=cdn_guard, env=settings.env,
                                        public_http_factory=lambda: recorder)
    error: dict[str, Any] | None = None
    try:
        configure_logging(settings.log_dir)
        items = [_item(media, shortcode) for media in downloader.resolve_post(shortcode, path_kind=kind)]
    except KeyboardInterrupt:
        error = {"code": "interrupted", "message": "POC interrompida pelo usuário (Ctrl+C).", "stage": None,
                 "detail": None}
    except Exception as exc:  # noqa: BLE001 - qualquer erro para a POC com dados seguros
        error = safe_error(exc)

    resp = recorder.last
    parse_report: dict[str, Any] | None = None
    node_report: dict[str, Any] | None = None
    if resp is not None and 200 <= resp.status < 300:
        node, parse = parse_post_document(resp.text, shortcode)
        parse_report = dataclasses.asdict(parse)
        node_report = node_summary(node) if node is not None else None
    instagram = [r for r in raw if r["host"] in INSTAGRAM_HOSTS]
    cdn = [r for r in raw if _cdn_suffix(r["host"])]
    report.update({
        # O path real carrega o shortcode: no relatório vai só o tipo do caminho.
        "requests": [{"method": r["method"], "path": f"/{kind}/" if r["path"] == document_path else "<outro>",
                      "status": r["status"]} for r in instagram],
        "instagram_requests": len(instagram),
        "cdn_requests": len(cdn) + len(cdn_calls),
        "other_requests": len(raw) - len(instagram) - len(cdn),
        "cookies_sent": sum(1 for r in raw if r["cookie_sent"]),
        "document": document_summary(resp) if resp is not None else None,
        "parse": parse_report,
        "node": node_report,
        "media_count": len(items),
        "media_types": sorted({item["media_type"] for item in items}),
        "items": items,
    })
    if error is not None:
        report["outcome"] = error["code"]
        report["error"] = error
        return (130 if error["code"] == "interrupted" else 1), report
    report["outcome"] = "ok"
    return 0, report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="POC do documento público de uma publicação do Instagram.")
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
