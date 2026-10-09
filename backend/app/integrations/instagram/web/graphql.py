"""POST /graphql/query com doc_id persistido (contrato INSTAGRAM_WEB_CONTRACT.md, web/graphql.py).

Nada de corpo de resposta, token, cursor ou variável vai para log, exceção ou diagnóstico: só status, chaves,
códigos e mensagens curtas (sem trechos entre aspas) de `errors`. Na linha por página de `errors` parciais, a
`message` passa por lista de permissão (_safe_error_message): só palavras comuns e nomes de campo; valores nunca.
Os erros da página são agrupados (_errors_report) pela chave já sanitizada (códigos, chaves, path sem índices,
message sem trechos citados) e cada grupo sai uma vez com a contagem: o teto é de grupos distintos, nunca de
ocorrências, e a escolha (_errors_text) põe na frente o erro que cruza com um descarte da página e um grupo por
path distinto (os raros primeiro); o que fica fora sai contado e com os paths listados (other_paths).
"""

from __future__ import annotations

import itertools
import json
import logging
import random
import re
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping
from typing import TYPE_CHECKING

from app.core.logging import redact, register_secret

from .errors import InstagramWebError, classify_response
from .queries import (
    POST_BY_SHORTCODE,
    PROFILE_POSTS,
    QuerySpec,
    post_variables,
    profile_posts_variables,
    resolve_spec,
)

if TYPE_CHECKING:
    from .bootstrap import WebBootstrap, WebState
    from .http import InstagramWebHttp, WebResponse

log = logging.getLogger(__name__)

BASE = "https://www.instagram.com"
GRAPHQL_PATH = "/graphql/query"
STAGE = "graphql"

# Códigos de erro do frontend (formato `{"error": <código>, ...}` ou `errors[].code`) que indicam token
# fb_dtsg/lsd vencido ou sessão web não reconhecida: 1357001 = não logado, 1357004 = recarregue a página.
TOKEN_ERROR_CODES = frozenset({1357001, 1357004})
# Trechos de mensagem (minúsculas) de `errors` que indicam token/sessão web inválida.
TOKEN_MESSAGE_MARKERS = ("fb_dtsg", "csrf", "lsd", "login", "log in", "logged in")
# Trechos (minúsculas) dos campos de erro de um 403 em JSON que indicam token recusado (nunca o corpo HTML).
TOKEN_403_MARKERS = ("csrf", "fb_dtsg", "lsd", "token")
# Trechos de mensagem (minúsculas) de `errors` que indicam doc_id/consulta persistida inexistente.
MISSING_DOC_MARKERS = ("persisted", "doc_id", "document", "query_not_found", "querynotfound", "unknown query",
                       "missing_required_variable")
_REBOOTSTRAP_CODES = ("session_invalid", "request_auth_failed", "contract_changed")
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")
_MAX_ERRORS = 5
_MAX_MESSAGE = 120
_MAX_KEYS = 30
DEFAULT_PAGE_DELAY = (2.0, 4.0)   # segundos entre páginas (a partir da 2a), sorteado no intervalo
NO_NEW_PAGES_LIMIT = 2            # páginas consecutivas só com nós já vistos antes de parar

_CONTRACT_MESSAGE = "O Instagram mudou a forma de responder e o Media Tool precisa ser atualizado."
# Fim anômalo da paginação (has_next_page=true sem como continuar): não é fim do perfil nem conclui a importação.
_PAGINATION_MESSAGE = ("O Instagram interrompeu a paginação do perfil antes do fim (resposta sem continuação "
                       "utilizável). A importação não foi dada como concluída e o ponto de retomada foi mantido.")
# Recusa de autenticação explícita no JSON de erro (minúsculas), avaliada após a renovação: request_auth_failed.
EXPLICIT_LOGIN_MARKERS = ("login_required", "require_login", "not logged in", "requires login", "checkpoint",
                          "challenge_required")
NOT_LOGGED_IN_CODE = 1357001

# Respostas 200 com `errors` (docs/INSTAGRAM_WEB_CONTRACT.md, "Respostas 200 com `errors`"): resumo seguro por página.
_MAX_ERROR_GROUPS = 12            # GRUPOS distintos detalhados por página; o resto sai em other_groups/other_errors
_MAX_ERRORS_TEXT = 2400           # tamanho de `groups=[...]`; grupo que não couber também vai para other_*
_MAX_GROUP_KEY = 700
_MAX_PAGE_LINE = 3000             # corte duro da linha por página (com marcador)
_MAX_INDEX_RANGES = 8             # faixas de posições de edge por grupo/motivo (`0-4,7`)
_MAX_MESSAGE_CACHE = 256
_MAX_OTHER_PATHS = 8              # paths distintos dos grupos omitidos listados em `other_paths` (o resto: `+N`)
_MAX_OTHER_PATHS_TEXT = 300
_TRUNCATED = " …[truncated]"
_MAX_ERROR_MESSAGE = 160
_MAX_ERROR_INPUT = 1000           # só o começo de uma message enorme é examinado
_MAX_PATH_SEGMENTS = 8
_MAX_PATH = 200
_MAX_PATH_INDEX = 999
_MAX_ERROR_KEYS = 8
# Nome de campo: minúsculas e `_`, com no máximo 3 trechos de 1-2 dígitos (`image_versions2`, `xdt_api__v1__feed`).
_SAFE_NAME = re.compile(r"[a-z][a-z_]{0,63}(?:\d{1,2}[a-z_]{0,63}){0,3}")
# Código tipo enum (MAIÚSCULAS e `_`, com até 3 dígitos só no fim) ou inteiro de até 7 dígitos; nada de texto livre
# nem id.
_SAFE_CODE = re.compile(r"[A-Z][A-Z_]{1,36}\d{0,3}|\d{1,7}")
_ERROR_CODE_KEYS = ("code", "api_error_code", "error_code", "severity", "classification")
_ANY_URL = re.compile(r"(?i)(?:\bhttps?:)?//\S+|\bwww\.\S+")
# Controle, largura zero, direção de texto (U+202A-202E, U+2066-2069) e BOM.
_INVISIBLE = re.compile("[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
# Dígitos em grupos (`3141.5926.5358`, `3141 5926 5358`) que somam 9+ dígitos.
_DIGIT_GROUPS = re.compile(r"\d(?:[\s.\-_:/,]*\d){8,}")
_CHUNK = re.compile(r"\S+")
_CHUNK_SHAPE = re.compile(r"([(\[\"']*)(.*?)([)\]\"'.,;:!?]*)", re.DOTALL)
_UPPER_ENUM = re.compile(r"[A-Z]+(?:_+[A-Z]+)*")
_CONTRACTION = re.compile(r"[A-Za-z]{1,19}'[a-z]{1,2}")
_PLACEHOLDERS = frozenset({"<redacted>", "<url>", "<url-midia>", "<id>", "<num>", "…", "-"})
_MAX_WORD = 19                    # palavra/trecho de nome maior que isso é tratado como token
_MAX_MIXED_WORD = 8               # palavra com maiúsculas no meio (GraphQL) só até este tamanho; shortcode tem 11
_MAX_NUMBER_DIGITS = 4
_MAX_UNQUOTE_PASSES = 3


class PageReport:
    """Contadores seguros de uma página da listagem (sem ids, cursores, URLs nem valores da resposta).

    ``dropped``: descartes inesperados (inválido/nulo), motivo -> quantidade. ``skipped``: descartes de propósito
    (duplicado, tipo não suportado, filtro de conteúdo). ``media``: mídias normalizadas da página (antes do filtro
    de conteúdo); quem normaliza (provider) informa por ``add``. ``fresh_edges``: posição (índice do edge na página)
    de cada nó novo, na ordem de entrega. ``events``: cada descarte inesperado como (posição do edge, motivo,
    quantidade); posição -1 = desconhecida. Posições não são ids: só dizem onde na página."""

    __slots__ = ("errors", "edges", "new_nodes", "has_next", "cursor_present", "dropped", "skipped", "media",
                 "requested", "fresh_edges", "events")

    def __init__(self, errors: dict | None, edges: int, new_nodes: int, has_next: bool, cursor_present: bool,
                 dropped: dict[str, int], duplicates: int, requested: int = 0,
                 fresh_edges: list[int] | None = None, events: list[tuple[int, str, int]] | None = None) -> None:
        self.errors = errors            # resumo de _errors_report (None = resposta sem `errors`)
        self.requested = requested      # count pedido (informativo: páginas menores que o pedido são normais)
        self.edges = edges
        self.new_nodes = new_nodes
        self.has_next = has_next        # has_next_page da resposta
        self.cursor_present = cursor_present
        self.dropped = dropped
        self.skipped: dict[str, int] = {"duplicate": duplicates} if duplicates else {}
        self.media = 0
        self.fresh_edges = fresh_edges or []
        self.events = events or []

    def add(self, media: int = 0, dropped: Mapping[str, int] | None = None,
            skipped: Mapping[str, int] | None = None, edge: int | None = None) -> None:
        self.media += media
        for reason, amount in (dropped or {}).items():
            if amount:
                self.events.append((-1 if edge is None else edge, reason, amount))
        for target, extra in ((self.dropped, dropped), (self.skipped, skipped)):
            for reason, amount in (extra or {}).items():
                if amount:
                    target[reason] = target.get(reason, 0) + amount


class PageState:
    """Página corrente da listagem (para ListingRef/checkpoint do provider). Cursores nunca logados nem expostos."""

    __slots__ = ("index", "after", "count", "end_cursor", "has_next", "duplicates", "report")

    def __init__(self, index: int, after: str | None, count: int, end_cursor: str | None, has_next: bool,
                 duplicates: int, report: PageReport | None = None) -> None:
        self.index = index              # 1-based
        self.after = after              # cursor usado para pedir esta página (None = 1a página)
        self.count = count              # count pedido
        self.end_cursor = end_cursor    # cursor da próxima página (None se ausente)
        self.has_next = has_next
        self.duplicates = duplicates    # nós já vistos neste trecho
        self.report = report            # contadores seguros da página (None em fakes de teste)

    def __repr__(self) -> str:
        return f"PageState(index={self.index}, count={self.count}, has_next={self.has_next}, cursors=<redacted>)"


def _explicit_login_evidence(payload: object) -> bool:
    """``require_login: true``, erro 1357001 ou marcador login_required/"Not Logged In"/"requires login" explícito
    nos campos de erro do JSON."""
    if not isinstance(payload, Mapping):
        return False
    if payload.get("require_login") is True or payload.get("error") == NOT_LOGGED_IN_CODE:
        return True
    if any(entry.get("code") == NOT_LOGGED_IN_CODE for entry in _error_entries(payload)):
        return True
    texts = [payload.get(k) for k in ("message", "errorSummary", "errorDescription", "error_type", "status")]
    for entry in _error_entries(payload):
        texts.extend(entry.get(k) for k in ("message", "summary", "code", "error_type"))
    return any(isinstance(t, str) and any(m in t.lower() for m in EXPLICIT_LOGIN_MARKERS) for t in texts)


def _load_json(text: str) -> object:
    body = text.lstrip()
    if body.startswith("for (;;);"):   # prefixo anti-JSON-hijacking do frontend
        body = body[len("for (;;);"):]
    return json.loads(body)


def _error_entries(payload: object) -> list[Mapping]:
    if not isinstance(payload, Mapping):
        return []
    errors = payload.get("errors")
    return [e for e in errors if isinstance(e, Mapping)] if isinstance(errors, list) else []


def _short_message(value: object, secrets: tuple[str, ...]) -> str:
    text = value if isinstance(value, str) else ""
    text = _QUOTED.sub('"…"', text)   # remove valores citados (variáveis, usernames)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return redact(text)[:_MAX_MESSAGE]


def _is_field_name(token: str) -> bool:
    """Nome de campo (minúsculas, `_`) cujos trechos entre `_` são curtos como palavras; dígitos (no máximo 3 no
    total) só em nome com `_` (`image_versions2`, `xdt_api__v1__feed`), para `abc123` não passar por nome."""
    if _SAFE_NAME.fullmatch(token) is None or any(len(part) > _MAX_WORD for part in token.split("_")):
        return False
    digits = sum(c.isdigit() for c in token)
    return digits == 0 or ("_" in token and digits <= 3)


def _is_plain_word(core: str) -> bool:
    """Lista de permissão da ``message``: marcador de redação, número curto, nome de campo, enum em maiúsculas ou
    palavra alfabética curta (minúscula, maiúscula, Capitalizada; com maiúsculas no meio só se bem curta)."""
    if core in _PLACEHOLDERS:
        return True
    if core.isascii() and core.isdigit():
        return len(core) <= _MAX_NUMBER_DIGITS
    if _is_field_name(core) or _CONTRACTION.fullmatch(core):
        return True
    if _UPPER_ENUM.fullmatch(core):
        return all(len(part) <= _MAX_WORD for part in core.split("_"))
    if not core.isalpha() or len(core) > _MAX_WORD:
        return False
    return core.islower() or core.isupper() or core[1:].islower() or len(core) <= _MAX_MIXED_WORD


def _scrub_chunk(match: re.Match[str]) -> str:
    """Trecho entre espaços: fica só se for pontuação básica + algo da lista de permissão; senão vira ``<id>``
    (e-mail, token, base64, cookie com `:`, URL sem esquema, percent-encoding, id, shortcode...)."""
    lead, core, trail = _CHUNK_SHAPE.fullmatch(match.group(0)).groups()
    if not core:
        return lead + trail
    return lead + (core if _is_plain_word(core) else "<id>") + trail


def _safe_error_message(value: object, secrets: tuple[str, ...]) -> str:
    """``message`` sanitizada (ver _scrub_error_message), cortada em ``_MAX_ERROR_MESSAGE`` caracteres."""
    return _scrub_error_message(value, secrets)[:_MAX_ERROR_MESSAGE]


def _scrub_error_message(value: object, secrets: tuple[str, ...]) -> str:
    """``message`` de um item de ``errors`` para log, por lista de permissão: uma linha com palavras curtas, nomes
    de campo, números de até 4 dígitos e pontuação básica; todo o resto vira marcador (``<url>``, ``<num>``,
    ``<id>``, ``"…"``). Até ``_MAX_ERROR_MESSAGE`` caracteres."""
    if not isinstance(value, str):
        return ""
    text = value[:_MAX_ERROR_INPUT]
    for _ in range(_MAX_UNQUOTE_PASSES):   # URL percent-codificada mais de uma vez
        decoded = urllib.parse.unquote(text)
        if decoded == text:
            break
        text = decoded
    text = " ".join(_INVISIBLE.sub(" ", text).split())
    text = _ANY_URL.sub("<url>", text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    text = redact(text)
    text = _QUOTED.sub(lambda m: m.group(0) if _is_field_name(m.group(0)[1:-1]) else '"…"', text)
    text = _DIGIT_GROUPS.sub("<num>", text)
    return _CHUNK.sub(_scrub_chunk, text)


def _safe_error_path(value: object, root: str | None = None) -> str | None:
    """``path`` de um erro GraphQL para a chave do grupo: só nomes de campo, todo índice de lista como ``[]`` e sem
    o campo raiz da consulta (``edges[].node.location.profile_pic_url``); nunca valores."""
    if not isinstance(value, list) or not value:
        return None
    if root is not None and value[0] == root:
        value = value[1:]
    out = ""
    for segment in value[:_MAX_PATH_SEGMENTS]:
        if isinstance(segment, int) and not isinstance(segment, bool):
            out += "[]"
        else:
            name = segment if isinstance(segment, str) and _is_field_name(segment) else "<?>"
            out += ("." if out else "") + name
    if len(value) > _MAX_PATH_SEGMENTS:
        out += "…"
    return out[:_MAX_PATH] or "<root>"


def _error_edge_index(path: object) -> int | None:
    """Posição do edge citado no ``path`` (o inteiro logo depois de ``edges``); é só a posição na página."""
    if not isinstance(path, list):
        return None
    for position in range(min(len(path), _MAX_PATH_SEGMENTS + 1) - 1):
        if path[position] == "edges":
            index = path[position + 1]
            if isinstance(index, int) and not isinstance(index, bool) and 0 <= index <= _MAX_PATH_INDEX:
                return index
            return None
    return None


def _ranges(indices: set[int] | list[int]) -> str:
    """Posições em faixas compactas (``0-4,7``), no máximo ``_MAX_INDEX_RANGES`` faixas."""
    parts: list[str] = []
    start = last = None
    for index in sorted(set(indices)):
        if last is not None and index == last + 1:
            last = index
            continue
        if start is not None:
            parts.append(str(start) if start == last else f"{start}-{last}")
        start = last = index
    if start is not None:
        parts.append(str(start) if start == last else f"{start}-{last}")
    if len(parts) > _MAX_INDEX_RANGES:
        parts = parts[:_MAX_INDEX_RANGES] + ["…"]
    return ",".join(parts)


def _safe_error_code(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else None


def _error_group_key(entry: object, secrets: tuple[str, ...], root: str | None,
                     cache: dict[str, tuple[str, str]]) -> tuple[str, str, str]:
    """(chave do grupo, texto exibido, path) de um item de ``errors``, só com partes JÁ sanitizadas (códigos curtos,
    chaves presentes, path sem índices nem raiz, message): valores que somem na sanitização caem no mesmo grupo.
    Na CHAVE todo trecho citado da message vira ``"…"`` (um nome citado diferente por erro não cria um grupo por
    valor); números curtos ficam (``404`` e ``500`` são erros diferentes). O texto exibido é o do primeiro erro do
    grupo, com a message sanitizada de sempre. ``cache``: message crua -> (da chave, exibida), só durante a página."""
    if not isinstance(entry, Mapping):
        text = "invalid=%s" % ("null" if entry is None else type(entry).__name__)
        return text, text, "-"
    parts: list[str] = []
    extensions = entry.get("extensions")
    for prefix, source in (("", entry), ("extensions.", extensions if isinstance(extensions, Mapping) else {})):
        for key in _ERROR_CODE_KEYS:
            code = _safe_error_code(source.get(key))
            if code is not None:
                parts.append(f"{prefix}{key}={code}")
    keys = sorted(k for k in entry if isinstance(k, str) and _is_field_name(k))[:_MAX_ERROR_KEYS]
    parts.append("keys=" + ",".join(keys) + ("…" if len(entry) > len(keys) else ""))
    path = _safe_error_path(entry.get("path"), root)
    if path is not None:
        parts.append(f"path={path}")
    raw = entry.get("message") or entry.get("summary")
    messages = cache.get(raw) if isinstance(raw, str) else ("", "")
    if messages is None:
        full = _scrub_error_message(raw, secrets)   # sem corte: o tamanho de um valor não muda onde a chave corta
        messages = (_QUOTED.sub('"…"', full)[:_MAX_ERROR_MESSAGE].rstrip(), full[:_MAX_ERROR_MESSAGE])
        if len(cache) < _MAX_MESSAGE_CACHE:
            cache[raw] = messages
    head = " ".join(parts)
    key, shown = (head + (f" message=[{message}]" if message else "") for message in messages)
    if len(shown) > _MAX_GROUP_KEY:
        shown = shown[:_MAX_GROUP_KEY] + "…"
    return key[:_MAX_GROUP_KEY], shown, path or "-"


def _errors_report(payload: object, secrets: tuple[str, ...], root: str | None = None) -> dict | None:
    """Resumo seguro de ``errors`` de uma resposta com dados, agrupado por tipo: ``count`` (ocorrências) e ``groups``
    (na ordem de chegada; cada um ``[texto, path, ocorrências, posições de edge]``). Todas as ocorrências são
    examinadas (custo linear); quem escolhe o que cabe na linha é ``_errors_text``. None quando ``errors`` está
    ausente/vazio. Formatos estranhos (não-lista, itens não-objeto) contam."""
    errors = payload.get("errors") if isinstance(payload, Mapping) else None
    if not errors:
        return None
    entries = errors if isinstance(errors, list) else [errors]
    groups: dict[str, list] = {}   # chave sanitizada -> [texto exibido, path, ocorrências, posições de edge]
    cache: dict[str, tuple[str, str]] = {}
    for entry in entries:
        key, shown, path = _error_group_key(entry, secrets, root, cache)
        group = groups.get(key)
        if group is None:
            group = groups[key] = [shown, path, 0, set()]
        group[2] += 1
        edge = _error_edge_index(entry.get("path")) if isinstance(entry, Mapping) else None
        if edge is not None:
            group[3].add(edge)
    return {"count": len(entries), "groups": list(groups.values())}


def _counts_text(counts: Mapping[str, int]) -> str:
    return "{" + ",".join(f"{reason}:{counts[reason]}" for reason in sorted(counts)) + "}"


def _other_paths_text(groups: list[list]) -> str:
    """Paths sanitizados distintos dos grupos omitidos (``-`` = erro sem path), com teto e ``+N`` para o resto."""
    paths = list(dict.fromkeys(group[1] for group in groups))
    shown: list[str] = []
    size = 0
    for path in paths[:_MAX_OTHER_PATHS]:
        if shown and size + len(path) + 1 > _MAX_OTHER_PATHS_TEXT:
            break
        shown.append(path)
        size += len(path) + 1
    if len(paths) > len(shown):
        shown.append(f"+{len(paths) - len(shown)}")
    return "[" + ",".join(shown) + "]"


def _errors_text(errors: dict | None, dropped_edges: set[int] | frozenset[int] = frozenset()) -> str:
    """``errors=N groups=[...]`` com no máximo ``_MAX_ERROR_GROUPS`` grupos / ``_MAX_ERRORS_TEXT`` caracteres.

    Prioridade: (1) grupo cujas posições de edge cruzam com um descarte inesperado da página (``dropped_edges``) e o
    grupo com mais ocorrências: nunca são omitidos, e saem na frente; (2) um grupo por path distinto, do path mais
    raro para o dominante (o campo que mais falha não esconde os outros); (3) o resto por ocorrências; a lista sai
    ordenada por ocorrências depois dos de (1). O que ficar fora sai explícito:
    ``other_groups=K other_errors=M other_paths=[...]``."""
    if not errors:
        return "errors=0"
    groups: list[list] = errors["groups"]
    crossing = [g for g in groups if dropped_edges and not dropped_edges.isdisjoint(g[3])]
    top = max(groups, key=lambda g: g[2])   # o grupo com mais ocorrências (o primeiro, no empate) sempre entra
    if not any(g is top for g in crossing):
        crossing.append(top)
    taken = {id(g) for g in crossing}
    totals: dict[str, int] = {}
    best: dict[str, list] = {}
    for group in groups:
        totals[group[1]] = totals.get(group[1], 0) + group[2]
        if id(group) not in taken and (group[1] not in best or group[2] > best[group[1]][2]):
            best[group[1]] = group
    diverse = [best[path] for path in sorted(best, key=totals.__getitem__)]
    taken.update(id(g) for g in diverse)
    rest = sorted((g for g in groups if id(g) not in taken), key=lambda g: -g[2])
    shown: list[tuple[int, int, int, str]] = []
    omitted: list[list] = []
    size = 0
    for position, group in enumerate(crossing + diverse + rest):
        text = "{%s count=%d%s}" % (group[0], group[2], " edges_at=" + _ranges(group[3]) if group[3] else "")
        must = position < len(crossing)
        if not must and shown and (len(shown) >= _MAX_ERROR_GROUPS or size + len(text) + 1 > _MAX_ERRORS_TEXT):
            omitted.append(group)
            continue
        shown.append((0 if must else 1, 0 if must else -group[2], position, text))
        size += len(text) + 1
    text = "errors=%d groups=[%s]" % (errors["count"], " ".join(item[-1] for item in sorted(shown)))
    if omitted:
        # Nada some em silêncio: quantos tipos e ocorrências ficaram fora, e de quais paths.
        text += " other_groups=%d other_errors=%d other_paths=%s" % (
            len(omitted), sum(g[2] for g in omitted), _other_paths_text(omitted))
    return text


def _dropped_at_text(events: list[tuple[int, str, int]]) -> str:
    """``{motivo:[posições de edge]}`` dos descartes inesperados da página (para cruzar com ``edges_at`` dos erros)."""
    at: dict[str, set[int]] = {}
    for edge, reason, _amount in events:
        if edge >= 0:
            at.setdefault(reason, set()).add(edge)
    return "{" + ",".join(f"{reason}:[{_ranges(at[reason])}]" for reason in sorted(at)) + "}"


def _new_totals() -> dict:
    return {"pages_with_errors": 0, "errors": 0, "dropped": {}}


def _split_edges(edges: list) -> tuple[list[dict], dict[str, int], list[int], list[tuple[int, str, int]]]:
    """Nós utilizáveis de ``edges``, os descartes inesperados (motivo -> quantidade), a posição do edge de cada nó
    utilizável e cada descarte como (posição do edge, motivo, 1)."""
    nodes: list[dict] = []
    dropped: dict[str, int] = {}
    positions: list[int] = []
    events: list[tuple[int, str, int]] = []
    for index, edge in enumerate(edges):
        if not isinstance(edge, Mapping):
            reason = "edge_null" if edge is None else "edge_not_object"
        elif isinstance(edge.get("node"), dict):
            nodes.append(edge["node"])
            positions.append(index)
            continue
        else:
            reason = "node_null" if edge.get("node") is None else "node_not_object"
        dropped[reason] = dropped.get(reason, 0) + 1
        events.append((index, reason, 1))
    return nodes, dropped, positions, events


def _error_summaries(payload: object, secrets: tuple[str, ...]) -> list[dict]:
    out: list[dict] = []
    if isinstance(payload, Mapping) and isinstance(payload.get("error"), int):
        out.append({"code": payload["error"], "message": _short_message(payload.get("errorSummary"), secrets)})
    for entry in _error_entries(payload):
        code = entry.get("code")
        out.append({"code": code if isinstance(code, (int, str)) else None,
                    "message": _short_message(entry.get("message") or entry.get("summary"), secrets)})
    return out[:_MAX_ERRORS]


def _diagnostic(resp: WebResponse, payload: object, detail: str, secrets: tuple[str, ...]) -> dict:
    diag: dict = {"detail": detail, "status": resp.status, "path": GRAPHQL_PATH,
                  "content_type": resp.content_type, "size": len(resp.text or "")}
    if isinstance(payload, Mapping):
        diag["top_keys"] = sorted(str(k) for k in payload)[:_MAX_KEYS]
        data = payload.get("data")
        if isinstance(data, Mapping):
            diag["data_keys"] = sorted(str(k) for k in data)[:_MAX_KEYS]
        errors = _error_summaries(payload, secrets)
        if errors:
            # Listas planas: InstagramWebError.diagnostic só preserva escalares e listas de escalares.
            diag["error_codes"] = [e["code"] for e in errors]
            diag["error_messages"] = [e["message"] for e in errors]
    return diag


def _token_issue(resp: WebResponse, payload: object) -> str | None:
    """Detalhe seguro quando a resposta indica fb_dtsg/lsd vencido ou sessão web inválida; senão None."""
    if isinstance(payload, Mapping) and payload.get("error") in TOKEN_ERROR_CODES:
        return f"error_{payload['error']}"
    for entry in _error_entries(payload):
        if entry.get("code") in TOKEN_ERROR_CODES:
            return f"error_{entry['code']}"
        message = entry.get("message")
        if isinstance(message, str) and any(m in message.lower() for m in TOKEN_MESSAGE_MARKERS):
            return "token_message"
    # 403 só conta com JSON estruturado (campos de erro); corpo HTML/texto nunca (JS embutido dá falso positivo) e
    # segue a classificação de errors.py (access_refused, sem re-bootstrap).
    if resp.status == 403 and isinstance(payload, Mapping):
        fields = (payload.get(k) for k in ("message", "errorSummary", "errorDescription"))
        if any(isinstance(v, str) and any(m in v.lower() for m in TOKEN_403_MARKERS) for v in fields):
            return "http_403_token"
    return None


def _has_root(payload: object, root_field: str) -> bool:
    data = payload.get("data") if isinstance(payload, Mapping) else None
    return isinstance(data, Mapping) and isinstance(data.get(root_field), dict)


def _root_is_null(payload: object, root_field: str) -> bool:
    """``data`` presente com o campo raiz presente e ``null`` (consulta de post: a publicação não veio)."""
    data = payload.get("data") if isinstance(payload, Mapping) else None
    return isinstance(data, Mapping) and root_field in data and data[root_field] is None


def _viewer_id(data: Mapping) -> str | None:
    """``data.xdt_viewer.user.id`` (prova de quem o Instagram acha que é o viewer); tolera ausente/null."""
    viewer = data.get("xdt_viewer")
    user = viewer.get("user") if isinstance(viewer, Mapping) else None
    value = user.get("id") if isinstance(user, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    if text in ("", "0"):
        return None
    register_secret(text)
    return text


CONFIRMED = "confirmed"
DIFFERENT_ACCOUNT = "different_account"
NOT_CONFIRMED = "not_confirmed"


def _account_text(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return None if text in ("", "0") else text


def account_confirmation(viewer_id: object, account_id: object) -> str:
    """Estado da conta: ``xdt_viewer.user.id`` (``viewer_id``) x ``ds_user_id`` da sessão (``account_id``).

    ``confirmed`` (iguais), ``different_account`` (ambos presentes e diferentes) ou ``not_confirmed`` (algum
    ausente/null/vazio/``"0"``). Só o estado pode ir para log/saída; os ids nunca."""
    viewer = _account_text(viewer_id)
    account = _account_text(account_id)
    if viewer is None or account is None:
        return NOT_CONFIRMED
    return CONFIRMED if viewer == account else DIFFERENT_ACCOUNT


def _missing_doc(payload: object) -> bool:
    for entry in _error_entries(payload):
        message = entry.get("message")
        if isinstance(message, str) and any(m in message.lower() for m in MISSING_DOC_MARKERS):
            return True
    return False


class InstagramWebGraphQL:
    def __init__(self, http: InstagramWebHttp, bootstrap: WebBootstrap, *, overrides: Mapping[str, str] = {},
                 env: Mapping[str, str] | None = None, page_delay: tuple[float, float] = DEFAULT_PAGE_DELAY,
                 sleep: Callable[[float], None] | None = None):
        """``env``: mapeamento de ambiente (Settings.env) para MEDIA_TOOL_IG_DOC_<NAME>. ``page_delay``: (mín, máx)
        em segundos entre páginas. ``sleep``: substitui a espera (testes); sem ele, espera em ``cancel.wait``."""
        self._http = http
        self._bootstrap = bootstrap
        self._overrides = dict(overrides or {})
        self._env = dict(env or {})
        low, high = sorted(max(0.0, float(v)) for v in page_delay)
        self._page_delay = (low, high)
        self._sleep = sleep
        self._resolved: dict[str, QuerySpec] = {}
        # data.xdt_viewer.user.id da última resposta com dados (None se ausente/null). Nunca logado.
        self.last_viewer_id: str | None = None
        # account_confirmation() da 1a resposta da última listagem (None antes dela). Só o estado, sem ids.
        self.listing_account_confirmation: str | None = None
        self._last_status: int | None = None   # status HTTP da última resposta (metadado de on_page)
        # Página corrente da listagem (atualizada antes de entregar os nós dela). Nunca logada.
        self.current_page: PageState | None = None
        # Resumo seguro de `errors` da última resposta com dados (None = sem `errors`); ver _errors_report.
        self._last_errors: dict | None = None
        # Totais seguros da última listagem (páginas com `errors`, erros, descartes inesperados por motivo).
        self.listing_totals: dict = _new_totals()

    def _spec(self, spec: QuerySpec) -> QuerySpec:
        if spec.name not in self._resolved:
            self._resolved[spec.name] = resolve_spec(spec, self._overrides, self._env)
        return self._resolved[spec.name]

    def _pause(self, cancel: threading.Event | None) -> None:
        delay = random.uniform(*self._page_delay)
        if delay <= 0:
            return
        if self._sleep is not None:
            self._sleep(delay)
        elif cancel is not None:
            cancel.wait(delay)   # acorda na hora se cancelarem
        else:
            time.sleep(delay)

    def _av(self, state: WebState) -> str:
        # `av` aceito no real (POCs de 2026-10-03, conta confirmada pelo xdt_viewer); o valor exato que o navegador
        # logado envia não foi capturado. Ordem:
        # viewer do HTML quando real (!= "0"); senão o ds_user_id dos cookies da sessão; senão "0".
        if state.viewer_id and state.viewer_id != "0":
            return state.viewer_id
        return self._http.account_id() or "0"

    def _form(self, spec: QuerySpec, variables: Mapping[str, object], state: WebState) -> dict[str, str]:
        return {
            "av": self._av(state),                      # id da conta; o navegador sempre envia
            "__user": "0",                              # fixo 0 no Instagram Web (conta FB não usada)
            "__a": "1",                                 # pede resposta de API (JSON); aceito no real, necessidade não isolada
            "__comet_req": "7",                         # frontend Polaris/Comet; aceito no real, necessidade não isolada
            "fb_dtsg": state.fb_dtsg,                   # token anti-CSRF do documento
            "jazoest": state.jazoest,                   # checksum do fb_dtsg, validado junto
            "lsd": state.lsd,                           # token anti-CSRF secundário
            "fb_api_caller_class": "RelayModern",       # cliente Relay que emite consultas persistidas
            "fb_api_req_friendly_name": spec.name,      # nome da consulta (casa com o doc_id)
            "variables": json.dumps(variables, separators=(",", ":")),   # JSON compacto como o navegador
            "server_timestamps": "true",                # o navegador sempre envia
            "doc_id": spec.doc_id,                      # consulta persistida
        }

    def _headers(self, spec: QuerySpec, state: WebState, referer_path: str) -> dict[str, str]:
        # Mesma grafia do InstagramWebHttp.post_form, para estes valores substituírem os dele sem duplicar.
        # X-ASBD-ID (WebHttpConfig, valor aceito no real; necessidade não isolada) e User-Agent/Accept-Language vêm
        # do post_form.
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",   # corpo é formulário
            "X-FB-LSD": state.lsd,                                 # repete o lsd do formulário
            "X-FB-Friendly-Name": spec.name,                       # nome da consulta, como o navegador
            "X-Root-Field-Name": spec.root_field,                  # campo raiz; aceito no real, necessidade não isolada
            "Origin": BASE,                                        # POST same-origin
            "Referer": BASE + referer_path,                        # página que originou a consulta
            "Sec-Fetch-Dest": "empty",                             # fetch() de cors same-origin
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        if state.app_id:
            headers["X-IG-App-ID"] = state.app_id                  # app do documento (senão o do WebHttpConfig)
        csrftoken = self._http.csrftoken()
        if csrftoken:
            headers["X-CSRFToken"] = csrftoken                     # cookie csrftoken (CSRF do Django)
        return headers

    def query(self, spec: QuerySpec, variables: Mapping[str, object], *, referer_path: str = "/") -> dict:
        """Retorna data[spec.root_field]; no máximo 1 re-bootstrap se o token/sessão web for recusado."""
        root = self._query(spec, variables, referer_path, {"rebootstrapped": False})
        if self._last_errors:
            log.warning("GraphQL %s: resposta com dados e errors parciais: %s", spec.name,
                        _errors_text(self._last_errors))
        return root

    def _query(self, spec: QuerySpec, variables: Mapping[str, object], referer_path: str, op: dict, *,
               post: bool = False) -> dict:
        """``op["rebootstrapped"]`` é compartilhado pela operação inteira (1 re-bootstrap por operação).

        ``post``: consulta de post isolado. Raiz presente e ``null`` (sem evidência de token recusado) é erro DO
        LINK (``content_unavailable``/``profile_private`` com mensagem de publicação), não de contrato; a listagem
        de perfil (``post=False``) não muda."""
        spec = self._spec(spec)
        force = False
        while True:
            state = self._bootstrap.get(force=force)
            self.last_viewer_id = None
            self._last_errors = None
            secrets =(state.fb_dtsg, state.lsd, state.jazoest)
            resp = self._http.post_form(GRAPHQL_PATH, self._form(spec, variables, state),
                                        self._headers(spec, state, referer_path))
            self._last_status = resp.status
            err = classify_response(resp, stage=STAGE)
            try:
                payload = _load_json(resp.text or "")
            except ValueError:
                payload = None
            # Com data[root_field] útil, os dados valem: errors parciais (mesmo citando login) só geram aviso no
            # _extract; token/sessão recusado só é avaliado quando data/raiz estão ausentes.
            has_root = _has_root(payload, spec.root_field)
            if not has_root and resp.status == 200 and not resp.sessionid_deleted \
                    and self._authenticated_viewer(payload):
                # xdt_viewer.user.id == ds_user_id: sessão comprovadamente autenticada. Raiz null/ausente aqui é
                # problema do conteúdo, não token/sessão recusada (sem re-bootstrap, mesmo com errors citando login).
                if not post:
                    raise self._content_error(resp, payload, secrets)
                if _root_is_null(payload, spec.root_field):
                    raise self._post_error(resp, payload, secrets)
                # Post com a raiz AUSENTE: segue a classificação normal (contrato), abaixo.
            issue = None if has_root else _token_issue(resp, payload)
            if issue is None and not has_root and err is not None and err.code == "request_auth_failed":
                # 401/require_login sem marcador de token também passam pela única renovação (seção 7).
                issue = "auth_refused"
            # 403 com corpo de token vem classificado como access_refused; só ele (e não redirects) re-bootstrapa.
            if issue and (err is None or err.code in _REBOOTSTRAP_CODES
                          or (err.code == "access_refused" and resp.status == 403)):
                if not op["rebootstrapped"]:
                    # invalidate antes do aviso: um bootstrap que recusa refazer (POC sem nova tentativa) levanta
                    # aqui e o log não anuncia um re-bootstrap que não acontece.
                    self._bootstrap.invalidate()
                    log.warning("GraphQL %s: token/sessão web recusado (%s, HTTP %s); refazendo o bootstrap uma vez",
                                spec.name, issue, resp.status)
                    op["rebootstrapped"] = True
                    force = True
                    continue
                raise self._after_renewal_error(spec, resp, payload, err, secrets)
            if err is not None:
                raise err
            if post and _root_is_null(payload, spec.root_field):
                raise self._post_error(resp, payload, secrets)
            return self._extract(spec, resp, payload, secrets)

    def _after_renewal_error(self, spec: QuerySpec, resp: WebResponse, payload: object,
                             err: InstagramWebError | None, secrets: tuple[str, ...]) -> InstagramWebError:
        """Token ainda recusado após a única renovação (seção 7 de docs/BATCH_ROBUSTNESS.md).

        ``session_invalid`` só com o Set-Cookie apagando o ``sessionid``. Recusa de autenticação deste request (401,
        require_login, 1357001, "Not Logged In"/"requires login"/login_required no JSON) -> ``request_auth_failed``
        (pausa de verificação; a escalada para ``session_invalid`` é do worker). Sem essa evidência: 403 ->
        ``access_refused``; demais (ex.: 1357004 persistente) -> ``contract_changed``."""
        auth_refused = (err is not None and err.code in ("request_auth_failed", "session_expired")) \
            or resp.status == 401 or _explicit_login_evidence(payload)
        if resp.sessionid_deleted or (err is not None and err.code == "session_invalid"):
            code, detail = "session_invalid", "sessionid_deleted"
            message = ("O Instagram encerrou a sessão salva. Entre no Instagram pelo navegador e salve a sessão de "
                       "novo em Configurações, ou teste a sessão.")
        elif auth_refused:
            code, detail = "request_auth_failed", "web_token_rejected"
            message = ("O Instagram recusou a consulta de publicações mesmo depois de renovar o acesso. O Media Tool "
                       "aguarda 15 minutos e verifica de novo; a sessão salva continua guardada.")
        elif resp.status == 403:
            code, detail = "access_refused", "web_token_rejected_403"
            message = ("O Instagram recusou a consulta de publicações mesmo depois de renovar o acesso. Isso não "
                       "indica sessão inválida nem restrição da conta.")
        else:
            code, detail = "contract_changed", "web_token_rejected_unexplained"
            message = _CONTRACT_MESSAGE
        log.warning("GraphQL %s: token/sessão web ainda recusado após a renovação (HTTP %s); classificado como %s",
                    spec.name, resp.status, code)
        return InstagramWebError(code, message, retryable=code == "request_auth_failed", stage=STAGE,
                                 diagnostic=_diagnostic(resp, payload, detail, secrets))

    def _authenticated_viewer(self, payload: object) -> bool:
        data = payload.get("data") if isinstance(payload, Mapping) else None
        viewer = _viewer_id(data) if isinstance(data, Mapping) else None
        if viewer is None:
            return False
        self.last_viewer_id = viewer
        account = (self._http.account_id() or "").strip()
        return bool(account) and viewer == account

    def _content_error(self, resp: WebResponse, payload: object, secrets: tuple[str, ...]) -> InstagramWebError:
        messages = " ".join(str(e.get("message") or e.get("summary") or "").lower() for e in _error_entries(payload))
        if "private" in messages or "privad" in messages:
            code, detail = "profile_private", "root_null_private"
            message = "O perfil é privado e a conta da sessão não o segue."
        else:
            code, detail = "content_unavailable", "root_null_authenticated"
            message = "O Instagram não entregou as publicações deste perfil para a conta da sessão."
        log.warning("GraphQL: sessão confirmada, mas sem publicações na resposta (%s, HTTP %s)", detail, resp.status)
        return InstagramWebError(code, message, stage=STAGE, diagnostic=_diagnostic(resp, payload, detail, secrets))

    def _post_error(self, resp: WebResponse, payload: object, secrets: tuple[str, ...]) -> InstagramWebError:
        """Consulta de post com a raiz ``null``: a publicação não existe mais ou a conta da sessão não a vê."""
        messages = " ".join(str(e.get("message") or e.get("summary") or "").lower() for e in _error_entries(payload))
        if "private" in messages or "privad" in messages:
            code, detail = "profile_private", "post_null_private"
            message = "A publicação é de um perfil privado que a conta da sessão não segue."
        else:
            code, detail = "content_unavailable", "post_null"
            message = "A publicação não existe mais ou não está disponível para a conta da sessão."
        log.warning("GraphQL: publicação não veio na resposta (%s, HTTP %s)", detail, resp.status)
        return InstagramWebError(code, message, stage=STAGE, diagnostic=_diagnostic(resp, payload, detail, secrets))

    def _contract(self, resp: WebResponse, payload: object, detail: str, secrets: tuple[str, ...]):
        diag = _diagnostic(resp, payload, detail, secrets)
        log.warning("GraphQL: contrato inesperado (%s, HTTP %s)", detail, resp.status)
        return InstagramWebError("contract_changed", _CONTRACT_MESSAGE, stage=STAGE, diagnostic=diag)

    def _extract(self, spec: QuerySpec, resp: WebResponse, payload: object, secrets: tuple[str, ...]) -> dict:
        if payload is None:
            raise self._contract(resp, payload, "invalid_json", secrets)
        if not isinstance(payload, Mapping):
            raise self._contract(resp, payload, "not_object", secrets)
        data = payload.get("data")
        if not isinstance(data, Mapping) or spec.root_field not in data:
            # Marcadores de doc_id inexistente só contam sem dados úteis (errors parcial com dados não quebra).
            if _missing_doc(payload):
                raise self._contract(resp, payload, "persisted_query_error", secrets)
            detail = "missing_data" if not isinstance(data, Mapping) else "missing_root_field"
            raise self._contract(resp, payload, detail, secrets)
        self.last_viewer_id = _viewer_id(data)
        root = data[spec.root_field]
        if not isinstance(root, dict):
            raise self._contract(resp, payload, "root_not_object", secrets)
        # `errors` junto com dados não é falha: quem chamou registra UMA linha com o resumo seguro (_errors_text).
        self._last_errors = _errors_report(payload, secrets, spec.root_field)
        return root

    def _finish_page(self, spec: QuerySpec, page: int, report: PageReport) -> None:
        """Fim de uma página (nós consumidos ou listagem encerrada nela): com `errors` ou descarte inesperado,
        registra UMA linha segura (sem ids, cursores, URLs nem valores)."""
        if not report.errors and not report.dropped:
            return
        line = (
            "GraphQL %s: página %d com %s: %s requested=%d edges=%d new_nodes=%d media_normalized=%d "
            "has_next_page=%s cursor_present=%s dropped_unexpected=%s skipped_expected=%s dropped_at=%s"
        ) % (
            spec.name, page, "errors parciais" if report.errors else "descartes inesperados",
            _errors_text(report.errors, {e for e, _r, _a in report.events if e >= 0}), report.requested,
            report.edges, report.new_nodes, report.media,
            report.has_next,
            report.cursor_present, _counts_text(report.dropped), _counts_text(report.skipped),
            _dropped_at_text(report.events),
        )
        if len(line) > _MAX_PAGE_LINE:
            line = line[:_MAX_PAGE_LINE - len(_TRUNCATED)] + _TRUNCATED
        log.warning("%s", line)

    def iter_profile_posts(self, username: str, *, max_media: int | None, page_size: int = 12,
                           max_pages: int | None = 500, cancel: threading.Event | None = None,
                           on_page: Callable[[dict], None] | None = None, start_cursor: str | None = None,
                           start_page: int = 1,
                           on_page_end: Callable[[str | None], bool] | None = None,
                           before_page: Callable[[threading.Event | None], None] | None = None) -> Iterator[dict]:
        """Gera os nós dos posts, página a página, só quando o consumidor pede o próximo.

        `max_media` só reduz o `count` da 1a página (ex.: teste com count=1); quem conta mídias e para é o
        chamador, porque um nó pode render 0..N mídias depois de normalizar e filtrar.

        `on_page(meta)`: chamado após cada resposta válida, antes dos nós da página serem entregues, com metadados
        seguros (sem cursor, ids nem valores de variáveis): page_index, http_status, edges_count, has_next_page,
        end_cursor_present, cursor_changed, variables_keys, new_nodes, duplicate_nodes, account_confirmation.

        Retomada: `start_cursor` (cursor da próxima página de um checkpoint) e `start_page` (índice dela); `max_pages`
        conta as páginas deste trecho (None = sem teto: só o fim real do perfil ou o consumidor encerram).
        `current_page` descreve a página cujos nós estão sendo entregues.

        `on_page_end(reason)`: chamado depois que todos os nós de uma página foram consumidos, antes de pedir a
        próxima, com o motivo de parada já decidido aqui (None = continuaria; "end_of_profile" só com
        `has_next_page=false`; "budget_exhausted" na última página permitida por `max_pages`). Retornar False encerra
        a listagem.

        Fim anômalo NÃO é fim do perfil: `has_next_page=true` com página vazia, sem cursor, com cursor repetido ou
        depois de `NO_NEW_PAGES_LIMIT` páginas seguidas sem nó novo (e `has_next_page` que não é booleano) levanta
        `contract_changed` com `diagnostic["detail"]` em `LISTING_ANOMALY_DETAILS`, SEM `on_page_end` da página (o
        checkpoint anterior continua valendo; os nós dela já foram entregues).

        `before_page(cancel)`: chamado antes do POST de cada página (inclusive a 1a do trecho); o provider o usa para
        o teto global de páginas por hora (pode esperar).
        """
        spec = PROFILE_POSTS
        referer_path = f"/{username}/"
        seen_ids: set[str] = set()       # só neste trecho
        seen_cursors: set[str] = set()
        after: str | None = start_cursor
        op = {"rebootstrapped": False}   # 1 re-bootstrap por operação, não por página
        pages_without_new = 0
        self.listing_account_confirmation = None
        self.current_page = None
        self.listing_totals = _new_totals()
        last_page = None if max_pages is None else start_page + max_pages - 1
        for page in itertools.count(start_page):
            if last_page is not None and page > last_page:
                return
            first = page == start_page
            if not first and not (cancel is not None and cancel.is_set()):
                self._pause(cancel)   # ritmo entre páginas; nunca antes da 1a do trecho
            if cancel is not None and cancel.is_set():
                log.info("Listagem GraphQL cancelada antes da página %d", page)
                return
            if before_page is not None:
                before_page(cancel)
            count = page_size
            if page == 1 and after is None and max_media is not None:
                count = max(1, min(page_size, max_media))
            variables = profile_posts_variables(self._spec(spec), username, count, after)
            conn = self._query(spec, variables, referer_path, op)
            errors = self._last_errors
            if first:
                self.listing_account_confirmation = account_confirmation(self.last_viewer_id,
                                                                         self._http.account_id())
            edges = conn.get("edges")
            page_info = conn.get("page_info")
            if not isinstance(edges, list):
                raise self._contract_conn(conn, "edges_not_list")
            if not isinstance(page_info, Mapping):
                raise self._contract_conn(conn, "page_info_missing")
            fresh: list[dict] = []
            fresh_edges: list[int] = []
            duplicates = 0
            # edge/nó nulo ou inválido: descartado e contado por motivo
            nodes, dropped, positions, events = _split_edges(edges)
            keyed_new = 0   # nós novos COM chave estável: só eles contam como "post novo" (no_new_items)
            for position, node in zip(positions, nodes):
                key = node.get("pk") or node.get("id") or node.get("code")
                key = str(key) if key is not None else None
                if key is not None:
                    if key in seen_ids:
                        duplicates += 1
                        continue
                    seen_ids.add(key)
                    keyed_new += 1
                # Nó sem pk/id/code segue adiante (a normalização o descarta e conta), mas não prova página nova.
                fresh.append(node)
                fresh_edges.append(position)
            new = len(fresh)
            edges_count = len(edges)
            del conn, edges, nodes   # nada da resposta fica vivo além dos nós desta página
            has_next_raw = page_info.get("has_next_page")
            has_next = has_next_raw is True
            cursor = page_info.get("end_cursor")
            cursor_present = isinstance(cursor, str) and bool(cursor)
            totals = self.listing_totals   # só o que esta camada vê: `errors` e edges/nós descartados
            if errors:
                totals["pages_with_errors"] += 1
                totals["errors"] += errors["count"]
            for drop_reason, amount in dropped.items():
                totals["dropped"][drop_reason] = totals["dropped"].get(drop_reason, 0) + amount
            report = PageReport(errors, edges_count, new, has_next, cursor_present, dropped, duplicates, count,
                                fresh_edges, events)
            self.current_page = PageState(page, after, count, cursor if cursor_present else None,
                                          has_next and cursor_present, duplicates, report)
            try:
                if on_page is not None:
                    on_page({
                        "page_index": page,
                        "http_status": self._last_status,
                        "edges_count": edges_count,
                        "has_next_page": has_next,
                        "end_cursor_present": cursor_present,
                        "cursor_changed": cursor_present and cursor != after,
                        "variables_keys": list(variables),
                        "new_nodes": new,
                        "duplicate_nodes": duplicates,
                        "account_confirmation": account_confirmation(self.last_viewer_id, self._http.account_id()),
                    })
                # Antes de entregar os nós: a última página também é registrada quando o consumidor para no limite.
                log.info("GraphQL página %d: %d nós (%d novos), has_next_page=%s", page, edges_count, new, has_next)
                yield from fresh
            finally:
                # Também quando o consumidor para no meio da página (limite, cancelamento, erro) ou o `on_page`
                # levanta.
                self._finish_page(spec, page, report)
            del fresh   # nada de acumular nós entre páginas
            reason: str | None = None
            anomaly: str | None = None
            if has_next_raw is False:
                reason = "end_of_profile"   # ÚNICO fim real do perfil: resposta válida com has_next_page=false
            elif not has_next:
                anomaly = "has_next_invalid"
            elif not cursor_present:
                # has_next_page=true sem cursor (com ou sem `errors`): a paginação quebrou no meio. Encerrar como fim
                # do perfil concluiria o lote sem as publicações seguintes.
                anomaly = "cursor_missing_with_errors" if errors else "cursor_missing"
            elif not edges_count:
                anomaly = "empty_page"
            elif cursor in seen_cursors or cursor == after:
                anomaly = "repeated_cursor"
            else:
                pages_without_new = pages_without_new + 1 if keyed_new == 0 else 0
                if pages_without_new >= NO_NEW_PAGES_LIMIT:
                    anomaly = "no_new_items"
                elif page == last_page:
                    reason = "budget_exhausted"
            if anomaly is not None:
                # Sem checkpoint desta página (os nós dela já foram entregues); quem lista mantém o checkpoint
                # anterior e NÃO conclui a importação.
                log.warning("GraphQL: página %d com fim anômalo da paginação (%s); não é fim do perfil", page, anomaly)
                raise InstagramWebError(
                    "contract_changed",
                    _CONTRACT_MESSAGE if anomaly in ("cursor_missing_with_errors", "repeated_cursor")
                    else _PAGINATION_MESSAGE,
                    stage=STAGE, diagnostic={"detail": anomaly, "page": page})
            if on_page_end is not None and not on_page_end(reason):
                return
            if reason is not None:
                if reason == "budget_exhausted":
                    log.warning("GraphQL: limite de %d páginas atingido; encerrando (max_pages)", max_pages)
                return
            seen_cursors.add(cursor)
            after = cursor

    def profile_page(self, username: str, *, after: str | None, count: int) -> list[dict]:
        """UMA página da listagem (1 POST): ``after`` = cursor usado para pedi-la (None = 1a página)."""
        spec = PROFILE_POSTS
        variables = profile_posts_variables(self._spec(spec), username, count, after)
        conn = self._query(spec, variables, f"/{username}/", {"rebootstrapped": False})
        edges = conn.get("edges")
        if not isinstance(edges, list):
            raise self._contract_conn(conn, "edges_not_list")
        nodes, dropped, _positions, events = _split_edges(edges)
        if self._last_errors or dropped:
            log.warning("GraphQL %s: página renovada com %s: %s edges=%d dropped_unexpected=%s", spec.name,
                        "errors parciais" if self._last_errors else "descartes inesperados",
                        _errors_text(self._last_errors, {e for e, _r, _a in events}), len(edges),
                        _counts_text(dropped))
        log.info("GraphQL página renovada: %d nós", len(nodes))
        return nodes

    def post_by_shortcode(self, shortcode: str) -> dict:
        """Raiz da consulta de UM post (1 POST; no máximo 1 re-bootstrap). NÃO VERIFICADO no Instagram real
        (queries.POST_BY_SHORTCODE). Raiz ``null`` levanta ``content_unavailable``/``profile_private``; quem chama
        normaliza o objeto devolvido (normalize.normalize_post). O shortcode nunca é logado."""
        spec = POST_BY_SHORTCODE
        variables = post_variables(self._spec(spec), shortcode)
        root = self._query(spec, variables, f"/p/{shortcode}/", {"rebootstrapped": False}, post=True)
        if self._last_errors:
            log.warning("GraphQL %s: resposta com dados e errors parciais: %s", spec.name,
                        _errors_text(self._last_errors))
        return root

    @staticmethod
    def _contract_conn(conn: Mapping, detail: str) -> InstagramWebError:
        log.warning("GraphQL: contrato inesperado (%s)", detail)
        return InstagramWebError("contract_changed", _CONTRACT_MESSAGE, stage=STAGE,
                                 diagnostic={"detail": detail, "root_keys": sorted(str(k) for k in conn)[:_MAX_KEYS]})
