"""Aba Reels do perfil COM a sessão -> linha do tempo dos Reels (consulta de padrões de postagem).

Algumas contas publicam Reels que só aparecem na aba Reels, não na grade (PolarisProfilePostsQuery). Observado no
navegador logado do dono em 2026-11-04 (só nomes de campos):

1. id numérico do perfil: o HTML logado de ``GET /<usuario>/reels/`` traz ``"profile_id":"<id>"`` e
   ``"page_id":"profilePage_<id>"``.
2. 1a página: ``POST /graphql/query`` REELS_TAB_LOGGED (web/queries.py) com ``variables={"data", "user_id"}``; páginas
   seguintes: REELS_TAB_LOGGED_CONNECTION com ``variables={"after", "data", "first", "id"}`` (o navegador usa
   ``first=3``). Resposta em ``data.fetch__XDTUserDict.clips_connection`` {``edges[].node``, ``page_info``}.
3. Nó: ``pk``, ``code``, ``media_type``, ``product_type``, ``clips_tab_pinned_user_ids``, ``is_in_profile_grid``...
   SEM ``taken_at``: o horário sai do ``pk`` (reel_taken_at_from_pk).

Nada de cookie, token, cursor, id ou HTML vai para log ou diagnóstico.
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timezone
from typing import Any

from .errors import InstagramWebError, classify_session_document
from .profile_snapshot import ERROR_ROUTE_MARKERS
from .queries import REELS_TAB_LOGGED, REELS_TAB_LOGGED_CONNECTION

log = logging.getLogger(__name__)

OBSERVED_AT = "2026-11-04"
STAGE = "reels_timeline"
DOCUMENT_STAGE = "reels_timeline_document"
SAFE_PATH = "/reels/"   # token fixo para log/diagnóstico
CONNECTION_FIELD = "clips_connection"
PINNED_FIELD = "clips_tab_pinned_user_ids"
# O HTML logado é maior que o anônimo (~750 KB observado); tamanho do logado não medido.
MAX_PROFILE_DOCUMENT_BYTES = 8 * 1024 * 1024
# O navegador pede first=3 nas páginas seguintes; 12 economiza requisições. Sem conexão útil com 12, a mesma página é
# pedida UMA vez com 3 e o tamanho fica valendo para o resto da operação.
FALLBACK_FIRST = 3

# pk do Instagram: os bits acima do 23o são milissegundos desde esta época. Comparado no real (2026-11-04, 32 posts
# da grade): o instante derivado fica 15-47 s ANTES do taken_at (mediana ~23 s); compensa com +25 s.
PK_EPOCH_MS = 1314220021721
PK_TAKEN_AT_OFFSET_MS = 25_000
_MIN_TS = datetime(2010, 1, 1, tzinfo=timezone.utc).timestamp()
_MAX_TS = datetime(2101, 1, 1, tzinfo=timezone.utc).timestamp()

_PROFILE_ID_RE = re.compile(r'"profile_id":"([0-9]{1,25})"')
_PAGE_ID_RE = re.compile(r'"page_id":"profilePage_([0-9]{1,25})"')
# Respostas da conexão sem a conexão útil (candidatas à nova tentativa com first=3).
_RETRY_DETAILS = frozenset({"missing_root_field", "root_not_object", "missing_data", "root_null_authenticated"})

MSG_NOT_FOUND = "Perfil não encontrado ou indisponível para a conta da sessão."
MSG_SHAPE = ("O Instagram respondeu a aba Reels do perfil num formato que o Media Tool não conhece. O Media Tool "
             "precisa ser atualizado.")
MSG_PAGINATION = ("O Instagram interrompeu a paginação da aba Reels antes do fim (resposta sem continuação "
                  "utilizável).")

_KINDS = {1: "photo", 2: "video", 8: "carousel"}


def _shape_error(detail: str, stage: str = STAGE, message: str = MSG_SHAPE) -> InstagramWebError:
    log.warning("Aba Reels (com sessão): contrato inesperado (%s)", detail)
    return InstagramWebError("contract_changed", message, stage=stage,
                             diagnostic={"detail": detail, "path": SAFE_PATH, "stage": stage})


def reel_taken_at_from_pk(pk: object) -> str | None:
    """Instante aproximado de publicação (ISO UTC ``YYYY-MM-DDTHH:MM:SSZ``) derivado do ``pk``; None se o ``pk`` for
    inválido ou cair fora de 2010..2100."""
    if isinstance(pk, bool):
        return None
    text = str(pk) if isinstance(pk, (str, int)) else ""
    if not (text.isascii() and text.isdigit()) or len(text) > 25:
        return None
    ms = (int(text) >> 23) + PK_EPOCH_MS + PK_TAKEN_AT_OFFSET_MS
    seconds = ms // 1000
    if not _MIN_TS <= seconds < _MAX_TS:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def profile_user_id(http: Any, username: str) -> str:
    """id numérico do perfil a partir do HTML logado de ``/<username>/reels/``. 404 ou página de erro sem id ->
    ``profile_not_found``; HTML sem id (ou com ids diferentes) -> ``contract_changed``."""
    resp = http.get_document(f"/{username}/reels/", max_bytes=MAX_PROFILE_DOCUMENT_BYTES, diagnostic_path=SAFE_PATH)
    error = classify_session_document(resp, stage=DOCUMENT_STAGE)
    if error is not None:
        error.diagnostic["path"] = SAFE_PATH
        if error.code == "content_unavailable":
            raise InstagramWebError("profile_not_found", MSG_NOT_FOUND, stage=DOCUMENT_STAGE,
                                    diagnostic={**error.diagnostic, "detail": "reels_http_404"})
        raise error
    html = resp.text or ""
    del resp
    ids = set(_PROFILE_ID_RE.findall(html)) | set(_PAGE_ID_RE.findall(html))
    error_route = any(marker in html for marker in ERROR_ROUTE_MARKERS)
    del html
    if len(ids) == 1:
        return ids.pop()
    if not ids and error_route:
        raise InstagramWebError("profile_not_found", MSG_NOT_FOUND, stage=DOCUMENT_STAGE,
                                diagnostic={"detail": "reels_error_route", "path": SAFE_PATH, "stage": DOCUMENT_STAGE})
    raise _shape_error("reels_profile_id_missing" if not ids else "reels_profile_id_ambiguous", DOCUMENT_STAGE)


def _kind(node: Mapping) -> str:
    if node.get("product_type") == "clips":
        return "reel"
    media_type = node.get("media_type")
    return _KINDS.get(media_type, "reel") if isinstance(media_type, int) and not isinstance(media_type, bool) \
        else "reel"


def _item(node: Mapping, code: str, user_id: str | None = None) -> dict:
    pinned = node.get(PINNED_FIELD)
    in_grid = node.get("is_in_profile_grid")
    pk = node.get("pk")
    pk_text = str(pk) if isinstance(pk, (str, int)) and not isinstance(pk, bool) else ""
    return {
        "code": code,
        "taken_at": reel_taken_at_from_pk(pk),
        "kind": _kind(node),
        # Fixado pelo DONO do perfil: em colaboração a lista traz também o id de quem colaborou.
        "pinned": ((user_id in {str(x) for x in pinned}) if user_id else bool(pinned))
        if isinstance(pinned, list) else None,
        "in_grid": in_grid if isinstance(in_grid, bool) else None,
        "source": "reels_tab",
        "pk": pk_text if pk_text.isascii() and pk_text.isdigit() else None,
        "play_count": _int_or_none(node.get("play_count")),
        "media_type": _int_or_none(node.get("media_type")),
    }


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _useful(connection: object) -> bool:
    return isinstance(connection, Mapping) and isinstance(connection.get("edges"), list) \
        and isinstance(connection.get("page_info"), Mapping)


def _cancelled(cancel: threading.Event | None) -> bool:
    return cancel is not None and cancel.is_set()


def _pause(graphql: Any, cancel: threading.Event | None, page_delay: tuple[float, float] | None) -> None:
    if page_delay is None:
        # Mesmo ritmo da listagem da grade (intervalo configurado no cliente GraphQL).
        pause = getattr(graphql, "_pause", None)
        if pause is not None:
            pause(cancel)
        return
    low, high = sorted(max(0.0, float(v)) for v in page_delay)
    delay = random.uniform(low, high)
    if delay <= 0:
        return
    if cancel is not None:
        cancel.wait(delay)
    else:
        time.sleep(delay)


def iter_reels_tab(graphql: Any, http: Any, username: str, *, cancel: threading.Event | None = None,
                   before_page: Callable[[threading.Event | None], None] | None = None, page_size: int = 12,
                   max_pages: int | None = None,
                   page_delay: tuple[float, float] | None = None) -> Iterator[dict]:
    """Reels da aba Reels (com a sessão), do mais novo para o mais antigo, um dict por código (sem repetir).

    ``before_page(cancel)`` antes de cada POST; ``cancel`` encerra sem erro antes da próxima requisição; pausa entre
    páginas no ritmo do cliente GraphQL (``page_delay`` = (mín, máx) em segundos substitui). Fim só com
    ``has_next_page`` false (ou ``max_pages``); cursor ausente/repetido com ``has_next_page`` true -> ``contract_changed``.
    """
    if _cancelled(cancel):
        return
    user_id = profile_user_id(http, username)
    referer_path = f"/{username}/reels/"
    data = {"include_feed_video": True, "page_size": page_size, "target_user_id": user_id}
    first = page_size
    after: str | None = None
    seen_cursors: set[str] = set()
    seen_codes: set[str] = set()
    page = 0
    while max_pages is None or page < max_pages:
        if page and not _cancelled(cancel):
            _pause(graphql, cancel, page_delay)
        connection: Mapping | None = None
        while connection is None:
            if _cancelled(cancel):
                log.info("Aba Reels (com sessão): cancelada antes da página %d", page + 1)
                return
            if before_page is not None:
                before_page(cancel)
            if after is None:
                spec = REELS_TAB_LOGGED
                variables = {"data": data, "user_id": user_id, **spec.relay_providers}
            else:
                spec = REELS_TAB_LOGGED_CONNECTION
                variables = {"after": after, "data": data, "first": first, "id": user_id, **spec.relay_providers}
            try:
                root = graphql.query(spec, variables, referer_path=referer_path)
                found = root.get(CONNECTION_FIELD)
            except InstagramWebError as exc:
                if after is None or first == FALLBACK_FIRST or exc.code not in ("contract_changed",
                                                                                 "content_unavailable") \
                        or exc.diagnostic.get("detail") not in _RETRY_DETAILS:
                    raise
                found = None
            if _useful(found):
                connection = found
            elif after is not None and first != FALLBACK_FIRST:
                # Sem a conexão com first=12: UMA nova tentativa da mesma página com o tamanho do navegador.
                log.info("Aba Reels (com sessão): página sem a conexão com %d itens; tentando com %d", first,
                         FALLBACK_FIRST)
                first = FALLBACK_FIRST
            else:
                raise _shape_error("reels_connection_missing")
        page += 1
        info = connection["page_info"]
        has_next = info.get("has_next_page")
        cursor = info.get("end_cursor")
        if not isinstance(has_next, bool):
            raise _shape_error("has_next_invalid", message=MSG_PAGINATION)
        items: list[dict] = []
        for edge in connection["edges"]:
            node = edge.get("node") if isinstance(edge, Mapping) else None
            # Visto no real (2026-11-04): o nó vem embrulhado em `node.media` {pk, code, ...}; aceita os dois formatos.
            if isinstance(node, Mapping) and isinstance(node.get("media"), Mapping):
                node = node["media"]
            code = node.get("code") if isinstance(node, Mapping) else None
            if not isinstance(code, str) or not code or code in seen_codes:
                continue
            seen_codes.add(code)
            items.append(_item(node, code, user_id))
        del connection
        log.info("Aba Reels (com sessão): página %d lida, %d Reels, has_next=%s", page, len(items), has_next)
        yield from items
        if not has_next:
            return
        if not (isinstance(cursor, str) and cursor):
            raise _shape_error("cursor_missing", message=MSG_PAGINATION)
        if cursor in seen_cursors or cursor == after:
            raise _shape_error("repeated_cursor", message=MSG_PAGINATION)
        seen_cursors.add(cursor)
        after = cursor
