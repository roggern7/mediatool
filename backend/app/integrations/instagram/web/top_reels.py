"""Top vídeos de UM perfil: os Reels com mais visualizações entre os últimos N da aba Reels SEM login (contrato
INSTAGRAM_WEB_CONTRACT.md, web/top_reels.py).

Mesma fonte e mesmas requisições do filtro de visualizações mínimas (web/reels_tab.py): ``GET /<usuario>/reels/``
anônimo (1a página, até 12 Reels) e ``POST /api/graphql`` REELS_TAB_CONNECTION para as seguintes (12 por página; 4 se o
servidor recusar 12). A aba vai do mais novo para o mais antigo; a leitura para ao juntar ``scan_limit`` Reels ou no fim
da aba. Cada requisição passa por ``before_request`` (teto de páginas por hora e cancelamento) e pelo portão global do
cliente HTTP.

Do nó usa ``code``, ``pk`` (horário aproximado, ``reels_timeline.reel_taken_at_from_pk``), ``play_count``,
``like_and_view_counts_disabled`` (contagem oculta pelo dono: fora do ranking, como na Análise de Perfis),
``clips_tab_pinned_user_ids`` (fixado) e ``display_uri`` (miniatura na CDN; opcional).

A aba com a sessão (web/reels_timeline.py) não é usada: a resposta dela não foi verificada com contagem de
visualizações, e esta consulta não precisa da conta do dono. Nada de cookie, token, cursor, código, id, URL ou contagem
vai para log ou diagnóstico.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.integrations.instagram.validation import validate_shortcode

from .errors import InstagramWebError, classify_public_document
from .post_document import MAX_DOCUMENT_BYTES
from .profile_snapshot import MSG_RESTRICTED, MSG_UNAVAILABLE, parse_profile_document
from .reels_tab import DOCUMENT_STAGE, MSG_REFUSED, PINNED_FIELD, SAFE_PATH, ReelsViews, _count
from .reels_timeline import reel_taken_at_from_pk

THUMB_FIELD = "display_uri"
MAX_THUMB_URL = 8192
MSG_SHAPE = ("O Instagram respondeu a aba Reels do perfil num formato que o Media Tool não conhece. O Media Tool "
             "precisa ser atualizado para listar os vídeos com mais visualizações.")
MSG_PRIVATE = "O perfil é privado: a aba Reels não aparece para quem não está logado."
MSG_RESTRICTED_TOP = MSG_RESTRICTED + " Os Reels do perfil não podem ser listados sem login."


@dataclass(frozen=True)
class TopReel:
    code: str
    views: int | None          # None = sem contagem (campo ausente, inválido ou oculto pelo dono)
    posted_at: str | None      # ISO UTC estimado pelo pk
    pinned: bool
    thumbnail: str | None = field(default=None, repr=False)   # URL assinada da CDN: nunca em log
    index: int = 0             # posição na aba (0 = mais novo, fixados primeiro)


@dataclass
class ScanResult:
    items: list[TopReel]       # os ``top`` maiores, por visualizações decrescentes
    scanned: int
    pages: int
    cancelled: bool


def rank(reels: list[TopReel], top: int) -> list[TopReel]:
    """Os ``top`` com mais visualizações (empate: o mais novo na aba primeiro); sem contagem fica de fora."""
    counted = [reel for reel in reels if reel.views is not None]
    return sorted(counted, key=lambda reel: (-(reel.views or 0), reel.index))[:max(0, top)]


def _reel(node: Mapping, index: int) -> TopReel:
    views = None if node.get("like_and_view_counts_disabled") else _count(node.get("play_count"))
    pinned = node.get(PINNED_FIELD)
    thumb = node.get(THUMB_FIELD)
    thumb = thumb if isinstance(thumb, str) and thumb.startswith("https://") and len(thumb) <= MAX_THUMB_URL else None
    return TopReel(code=node["code"], views=views, posted_at=reel_taken_at_from_pk(node.get("pk")),
                   pinned=isinstance(pinned, list) and bool(pinned), thumbnail=thumb, index=index)


def _profile_problem(html: str, username: str) -> InstagramWebError | None:
    """Documento sem a conexão de Reels: perfil inexistente, privado ou escondido de visitantes (mesma leitura da
    Análise de Perfis). None = não dá para afirmar; fica o erro de formato."""
    status = parse_profile_document(html, username, 0.0).account_status
    diagnostic = {"path": SAFE_PATH, "stage": DOCUMENT_STAGE}
    if status == "unavailable":
        return InstagramWebError("profile_not_found", MSG_UNAVAILABLE, stage=DOCUMENT_STAGE, diagnostic=diagnostic)
    if status == "private":
        return InstagramWebError("profile_private", MSG_PRIVATE, stage=DOCUMENT_STAGE, diagnostic=diagnostic)
    if status == "restricted":
        return InstagramWebError("profile_private", MSG_RESTRICTED_TOP, stage=DOCUMENT_STAGE, diagnostic=diagnostic)
    return None


class _ProfileReelsTab(ReelsViews):
    """A aba Reels lida como fonte principal: aqui o perfil pode não existir (404 ou rota de erro com 200)."""

    def _first_page(self) -> None:
        resp = self._http.get_document(f"/{self._username}/reels/", max_bytes=MAX_DOCUMENT_BYTES,
                                       diagnostic_path=SAFE_PATH)
        error = classify_public_document(resp, stage=DOCUMENT_STAGE)
        if error is not None:
            error.diagnostic["path"] = SAFE_PATH
            if error.code == "content_unavailable":
                raise InstagramWebError("profile_not_found", MSG_UNAVAILABLE, stage=DOCUMENT_STAGE,
                                        diagnostic=error.diagnostic)
            if error.code == "access_refused":
                error.message = MSG_REFUSED
            raise error
        html = resp.text or ""
        del resp
        try:
            self._read_document(html)
        except InstagramWebError as exc:
            if exc.code != "contract_changed":
                raise
            problem = _profile_problem(html, self._username)
            if problem is not None:
                raise problem from None
            raise


def scan_top_reels(http: Any, username: str, *, top: int, scan_limit: int, before_request: Callable[[], bool],
                   overrides: Mapping[str, str] | None = None, env: Mapping[str, str] | None = None,
                   on_progress: Callable[[int, int], None] | None = None,
                   cancel: threading.Event | None = None) -> ScanResult:
    """Lê a aba até ``scan_limit`` Reels (ou o fim) e devolve os ``top`` com mais visualizações. ``on_progress``
    recebe (Reels lidos, páginas) depois de cada página. Erros sobem como ``InstagramWebError`` (formato desconhecido
    com mensagem própria desta consulta) e levam em ``partial`` o ``ScanResult`` do que já foi lido. Cancelado (antes de uma requisição, ou ``cancel`` acionado durante uma que
    falhou por isso) -> ``cancelled`` com o que já foi lido."""
    reels: list[TopReel] = []
    seen: set[str] = set()

    def on_node(node: Mapping) -> None:
        code = node["code"]
        if len(reels) >= scan_limit or code in seen:
            return
        try:
            validate_shortcode(code)
        except ValueError:
            return   # código fora do formato de publicação: não vira link nem rota de miniatura
        seen.add(code)
        reels.append(_reel(node, len(reels)))

    tab = _ProfileReelsTab(http, username, before_request=before_request, overrides=overrides, env=env,
                           on_node=on_node)
    try:
        while len(reels) < scan_limit and tab.fetch_next():
            if on_progress is not None:
                on_progress(len(reels), tab.pages)
    except Exception as exc:
        if cancel is not None and cancel.is_set():
            return ScanResult(items=rank(reels, top), scanned=len(reels), pages=tab.pages, cancelled=True)
        if isinstance(exc, InstagramWebError) and exc.code == "contract_changed":
            exc.message = MSG_SHAPE
        # Falha no meio (429, pausa, página com erro): o ranking do que já foi lido vai junto com o erro.
        exc.partial = ScanResult(items=rank(reels, top), scanned=len(reels), pages=tab.pages,  # type: ignore[attr-defined]
                                 cancelled=False)
        raise
    finally:
        if on_progress is not None:
            on_progress(len(reels), tab.pages)
    return ScanResult(items=rank(reels, top), scanned=len(reels), pages=tab.pages, cancelled=tab.cancelled)
