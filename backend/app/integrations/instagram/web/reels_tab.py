"""Aba Reels do perfil SEM login -> visualizações (``play_count``) por código de publicação (filtro de visualizações
mínimas da importação por perfil; contrato INSTAGRAM_WEB_CONTRACT.md, web/reels_tab.py).

A listagem com a sessão (PolarisProfilePostsQuery) NÃO traz contagem de visualizações (verificado 2026-10-28). A aba
Reels aberta sem login traz: VERIFICADO no Instagram real em 2026-10-29 (só nomes de campos):

1. ``GET /<usuario>/reels/`` anônimo: o HTML embute num ``<script type="application/json" data-sjs>`` o envelope
   ``RelayPrefetchedStreamCache`` com ``data.xig_user_by_username`` (``id``, ``pk``, ``polaris_clips_connection``
   {``edges[].node``, ``page_info`` {``end_cursor``, ``has_next_page``}}) e o token ``LSD``.
2. Nó: ``code``, ``pk``, ``play_count`` (int), ``clips_tab_pinned_user_ids`` (fixado na aba), entre outros; SEM
   ``taken_at``/``media_type``/``video_versions``.
3. Próximas páginas: ``POST /api/graphql`` (REELS_TAB_CONNECTION em web/queries.py) com ``variables={"after", "first",
   "id"}``; resposta em ``data.node.polaris_clips_connection``.

As páginas são buscadas SOB DEMANDA (``lookup``): só quando um vídeo da listagem ainda não está no mapa, e só até achar
o código, até ``has_next_page`` false ou até o menor ``pk`` NÃO fixado já visto na aba ficar menor que o ``pk`` do item
(a aba vai do mais novo para o mais antigo: o item não está nela). Cada requisição passa por ``before_request`` (teto de
páginas por hora e cancelamento) e pelo portão global do cliente HTTP. Nada de cookie, token, cursor, código, id ou
contagem vai para log ou diagnóstico.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

from .bootstrap import _LSD_MARKERS, _first
from .errors import InstagramWebError, classify_public_document, classify_response, loads_json
from .post_document import MAX_DOCUMENT_BYTES, _load_scripts, iter_json_scripts
from .queries import REELS_TAB_CONNECTION, resolve_spec

log = logging.getLogger(__name__)

OBSERVED_AT = "2026-10-29"
USER_FIELD = "xig_user_by_username"
CONNECTION_FIELD = "polaris_clips_connection"
PINNED_FIELD = "clips_tab_pinned_user_ids"
GRAPHQL_PATH = "/api/graphql"
DOCUMENT_STAGE = "reels_tab"
GRAPHQL_STAGE = "reels_tab_graphql"
SAFE_PATH = "/reels/"   # token fixo para log/diagnóstico
# O navegador pede 4 por página; 12 economiza requisições. Se o servidor não devolver a conexão com 12, a mesma
# página é pedida UMA vez com 4 e o tamanho fica valendo para o resto da operação.
PAGE_SIZE = 12
FALLBACK_PAGE_SIZE = 4
# Respostas da requisição anônima: não há sessão nela, então nenhum código de sessão/autenticação faz sentido.
_ANONYMOUS_REMAP = frozenset({"session_expired", "session_invalid", "request_auth_failed"})
MSG_SHAPE = ("Não foi possível ler as visualizações do perfil: o Instagram respondeu a aba Reels num formato que o "
             "Media Tool não conhece. A importação parou para não baixar vídeos sem o filtro de visualizações.")
MSG_REFUSED = ("O Instagram recusou a leitura das visualizações do perfil (aba Reels sem login). Isso não indica "
               "sessão inválida.")


def _shape_error(detail: str, stage: str) -> InstagramWebError:
    return InstagramWebError("contract_changed", MSG_SHAPE, stage=stage,
                             diagnostic={"detail": detail, "path": SAFE_PATH, "stage": stage})


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _pk(value: object) -> int | None:
    text = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    return int(text) if text.isascii() and text.isdigit() else None


class ReelsViews:
    """Mapa código -> ``play_count`` da aba Reels anônima de UM perfil, preenchido sob demanda. Vive só durante um
    trecho de listagem (cursor, LSD e cookies anônimos ficam em memória e somem com ele)."""

    def __init__(self, http: Any, username: str, *, before_request: Callable[[], bool],
                 overrides: Mapping[str, str] | None = None, env: Mapping[str, str] | None = None) -> None:
        self._http = http
        self._username = username
        self._before = before_request
        self._spec = resolve_spec(REELS_TAB_CONNECTION, overrides or {}, env)
        self._views: dict[str, int | None] = {}
        self._user_id: str | None = None
        self._lsd: str | None = None
        self._cursor: str | None = None
        self._started = False
        self._has_next = True
        self._min_pk: int | None = None
        self._page_size = PAGE_SIZE
        self.pages = 0
        self.cancelled = False

    def __repr__(self) -> str:
        return "ReelsViews(<redacted>)"

    def lookup(self, code: str, pk: object) -> int | None:
        """Visualizações do Reel ``code`` (``pk`` = pk da publicação na listagem); None = sem contagem (fora da aba
        Reels, ou campo ausente/inválido). Cancelado antes de uma requisição -> None com ``cancelled = True``."""
        item_pk = _pk(pk)
        while True:
            if code in self._views:
                return self._views[code]
            if self._started and not self._has_next:
                return None
            if self._min_pk is not None and item_pk is not None and self._min_pk < item_pk:
                return None   # a aba já passou da posição do item: ele não é um Reel da aba
            if not self._before():
                self.cancelled = True
                return None
            if self._started:
                self._next_page()
            else:
                self._first_page()

    # ---------------------------------------------------------------- páginas

    def _first_page(self) -> None:
        resp = self._http.get_document(f"/{self._username}/reels/", max_bytes=MAX_DOCUMENT_BYTES,
                                       diagnostic_path=SAFE_PATH)
        error = classify_public_document(resp, stage=DOCUMENT_STAGE)
        if error is not None:
            error.diagnostic["path"] = SAFE_PATH
            if error.code == "content_unavailable":   # 404 da aba de um perfil que a listagem acabou de ler
                raise _shape_error("reels_tab_http_404", DOCUMENT_STAGE)
            if error.code == "contract_changed":
                raise _shape_error(str(error.diagnostic.get("detail") or "reels_tab_document"), DOCUMENT_STAGE)
            if error.code == "access_refused":
                error.message = MSG_REFUSED
            raise error
        html = resp.text or ""
        del resp
        _, _, envelopes = _load_scripts(iter_json_scripts(html), USER_FIELD)
        _, lsd = _first(html, _LSD_MARKERS)
        del html
        user = next((env for _, env in envelopes if isinstance(env.get(CONNECTION_FIELD), Mapping)), None)
        if user is None:
            raise _shape_error("reels_tab_envelope_missing", DOCUMENT_STAGE)
        user_id = user.get("id") or user.get("pk")
        self._user_id = str(user_id) if _pk(user_id) is not None else None
        self._lsd = lsd or None
        self._started = True
        self._ingest(user[CONNECTION_FIELD], DOCUMENT_STAGE)

    def _next_page(self) -> None:
        if not self._cursor or not self._user_id or not self._lsd:
            raise _shape_error("reels_tab_paging_tokens_missing", GRAPHQL_STAGE)
        while True:
            connection = self._post_page(self._page_size)
            if connection is not None:
                self._ingest(connection, GRAPHQL_STAGE)
                return
            if self._page_size == FALLBACK_PAGE_SIZE:
                raise _shape_error("reels_tab_connection_missing", GRAPHQL_STAGE)
            # Sem a conexão com 12 por página: UMA nova tentativa da mesma página com o tamanho do navegador.
            log.info("Aba Reels (sem login): página sem a conexão com %d itens; tentando com %d", self._page_size,
                     FALLBACK_PAGE_SIZE)
            self._page_size = FALLBACK_PAGE_SIZE
            if not self._before():
                self.cancelled = True
                return

    def _post_page(self, first: int) -> Mapping | None:
        spec = self._spec
        form = {
            "lsd": self._lsd,
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": spec.name,
            "variables": json.dumps({"after": self._cursor, "first": first, "id": self._user_id},
                                    separators=(",", ":")),
            "server_timestamps": "true",
            "doc_id": spec.doc_id,
        }
        referer = f"https://www.instagram.com/{self._username}/reels/"
        headers = {
            "X-FB-LSD": self._lsd,
            "X-FB-Friendly-Name": spec.name,
            "Origin": "https://www.instagram.com",
            "Referer": referer,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        resp = self._http.post_form(GRAPHQL_PATH, form, headers)
        error = classify_response(resp, stage=GRAPHQL_STAGE)
        if error is not None:
            if error.code in _ANONYMOUS_REMAP:
                error = InstagramWebError("access_refused", MSG_REFUSED, stage=GRAPHQL_STAGE,
                                          diagnostic={**error.diagnostic})
            elif error.code == "access_refused":
                error.message = MSG_REFUSED
            elif error.code == "contract_changed":
                raise _shape_error(str(error.diagnostic.get("detail") or "reels_tab_response"), GRAPHQL_STAGE)
            raise error
        try:
            payload = loads_json(resp.text or "")
        except ValueError:
            raise _shape_error("reels_tab_invalid_json", GRAPHQL_STAGE) from None
        del resp
        data = payload.get("data") if isinstance(payload, Mapping) else None
        node = data.get(spec.root_field) if isinstance(data, Mapping) else None
        connection = node.get(CONNECTION_FIELD) if isinstance(node, Mapping) else None
        return connection if isinstance(connection, Mapping) else None

    def _ingest(self, connection: Mapping, stage: str) -> None:
        edges = connection.get("edges")
        info = connection.get("page_info")
        if not isinstance(edges, list) or not isinstance(info, Mapping):
            raise _shape_error("reels_tab_connection_shape", stage)
        has_next = info.get("has_next_page")
        cursor = info.get("end_cursor")
        if not isinstance(has_next, bool):
            raise _shape_error("reels_tab_page_info_shape", stage)
        if has_next and not (isinstance(cursor, str) and cursor):
            raise _shape_error("reels_tab_cursor_missing", stage)
        added = 0
        for edge in edges:
            node = edge.get("node") if isinstance(edge, Mapping) else None
            code = node.get("code") if isinstance(node, Mapping) else None
            if not isinstance(code, str) or not code:
                continue
            self._views[code] = _count(node.get("play_count"))
            added += 1
            pinned = node.get(PINNED_FIELD)
            pk = _pk(node.get("pk"))
            if pk is not None and not (isinstance(pinned, list) and pinned):
                # Fixado na aba pode ser antigo: fora do menor pk (senão encerraria a busca cedo demais).
                self._min_pk = pk if self._min_pk is None else min(self._min_pk, pk)
        self._has_next = has_next
        self._cursor = cursor if has_next else None
        self.pages += 1
        log.info("Aba Reels (sem login): página %d lida, %d Reels, has_next=%s", self.pages, added, has_next)
