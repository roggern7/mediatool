"""Retrato público de UM perfil (Análise de Perfis; contrato INSTAGRAM_WEB_CONTRACT.md, web/profile_snapshot.py).

Um único GET ANÔNIMO de ``/<usuario>/reels/`` traz tudo; nenhuma sessão é lida e nenhuma requisição extra é feita. O
HTML embute dois envelopes ``xig_user_by_username`` (web/reels_tab.py): o do usuário (``username``, ``is_private``,
``is_unpublished``, ``latest_reel_media``, ``lox_highlights_connection``, ``has_any_clips``...) e o da conexão de Reels
(``polaris_clips_connection``). Posts vêm da meta tag de descrição: ``all_media_count`` é sempre nulo sem login.

Regra central: falha nunca vira negativa. Campo ausente ou com outra forma é ``unverified``, jamais ``no``; só a rota de
erro exata (ou 404, ou ``is_unpublished``) vira ``unavailable``. Um envelope válido do usuário tem prioridade sobre as
marcas de rota de erro.

VERIFICADO no real (2026-10-31/11-01): perfil público com e sem Stories, com e sem Destaques, perfil inexistente com
200. NÃO VERIFICADO (falha segura): perfil privado, restrição de idade, redirect para login anônimo.

O corpo só é varrido pelas duas marcas exatas da rota de erro. Nada do HTML ou de cookies vai para log ou exceção.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from .errors import classify_public_document
from .post_document import MAX_DOCUMENT_BYTES, _load_scripts, iter_json_scripts
from .reels_tab import CONNECTION_FIELD, SAFE_PATH, USER_FIELD, _count

OBSERVED_AT = "2026-11-01"
STAGE = "profile_snapshot"
ERROR_ROUTE_MARKERS = ('"canonicalRouteName":"comet.igweb.PolarisErrorRoute"', '"tracePolicy":"polaris.httpErrorPage"')
HIGHLIGHTS_FIELD = "lox_highlights_connection"
STORY_SECONDS = 24 * 3600
MAX_DISPLAY_NAME = 200
# Maior contagem aceita por campo. Acima disso o valor não é confiável, e o teto garante que a soma de um lote inteiro
# nunca passe do que o SQLite guarda em INTEGER.
MAX_COUNT = 10**13

# Perfil restrito (idade ou região), VERIFICADO em 2026-11-01 com @ambev: rota normal da aba Reels, meta tag com a
# contagem de posts e usuário nulo. Como o usuário nulo também pode ser passageiro, o executor relê uma vez.
RESTRICTED_ROUTE = '"canonicalRouteName":"comet.igweb.PolarisLoggedOutDesktopWWWProfileReelsTabRoute"'
NULL_USER = f'"{USER_FIELD}":null'
MSG_RESTRICTED = "Perfil restrito por idade ou região: sem login, só a contagem de posts fica visível."
MSG_UNAVAILABLE = "Perfil não encontrado, removido ou restrito para visitantes sem login."
MSG_MISMATCH = "O Instagram devolveu outro perfil nesta consulta; o resultado não foi aceito."
MSG_UNREADABLE = "A página do perfil não trouxe os dados esperados; o resultado não pôde ser confirmado."

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")
# Número + sufixo + palavra: "8,630 posts", "33K posts", "1 post", "1,2K publicações". O texto vem em pt-BR com número
# em formato inglês; "." também é aceito como milhar. Lido só antes do travessão: depois dele há texto livre do perfil.
_POSTS_RE = re.compile(
    r"(?<![\w.,])(?<!\d\s)(\d[\d.,]*)\s?([KkMm]?)\s+(?:posts?|publica[çc](?:ão|ao|ões|oes))\b", re.IGNORECASE)
_DESCRIPTION_END_RE = re.compile(r"\s[—–-]\s")
_PLAIN_NUMBER_RE = re.compile(r"\d+|\d{1,3}(?:[.,]\d{3})+")
_SHORT_NUMBER_RE = re.compile(r"\d+(?:[.,]\d{1,2})?")   # "1,2K"; "1,234K" é ambíguo (1.234 ou 1,234?)
_MULTIPLIERS = {"k": 1_000, "m": 1_000_000}


@dataclass(frozen=True)
class ProfileSnapshot:
    """Resultado da consulta de um perfil, sem HTML. Em ``unavailable``/``unverified`` os campos ficam ``unverified``
    e nulos."""

    username: str
    account_status: str                  # active | private | unavailable | unverified
    display_name: str | None = None
    posts_state: str = "unverified"      # known | unverified
    posts_count: int | None = None
    posts_exact: int | None = None       # 1 = número inteiro; 0 = aproximado ("33K" -> 33000)
    posts_text: str | None = None        # texto original do aproximado
    views_state: str = "unverified"      # known | none (sem Reels) | unverified
    views_total: int | None = None
    views_reels: int | None = None       # quantos Reels entraram na soma
    stories: str = "unverified"          # yes | no | unverified
    highlights: str = "unverified"       # yes | no | unverified
    error_code: str | None = None
    error_message: str | None = None


class _MetaCollector(HTMLParser):
    """Conteúdo de ``og:description`` e ``description`` (o parser já decodifica as entidades dos atributos)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.og: str | None = None
        self.description: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        names = dict(attrs)
        content = names.get("content")
        if not content:
            return
        if names.get("property") == "og:description" and self.og is None:
            self.og = content
        elif names.get("name") == "description" and self.description is None:
            self.description = content


def _read_meta(html: str) -> _MetaCollector:
    collector = _MetaCollector()
    collector.feed(html)
    collector.close()
    return collector


def parse_posts_text(text: str | None) -> tuple[int, int, str | None] | None:
    """Descrição do perfil -> (quantidade, exato 1/0, texto do aproximado); None se não houver número legível."""
    if not text:
        return None
    match = _POSTS_RE.search(_DESCRIPTION_END_RE.split(text, maxsplit=1)[0])
    if match is None:
        return None
    number, suffix = match.group(1), match.group(2)
    if suffix:
        if not _SHORT_NUMBER_RE.fullmatch(number):
            return None
        amount = float(number.replace(",", ".")) * _MULTIPLIERS[suffix.lower()]
        return (round(amount), 0, number + suffix) if amount <= MAX_COUNT else None
    if not _PLAIN_NUMBER_RE.fullmatch(number):
        return None
    digits = re.sub(r"[.,]", "", number)
    if len(digits) > len(str(MAX_COUNT)) or int(digits) > MAX_COUNT:
        return None
    return int(digits), 1, None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _stories(value: object, *, private: bool, now_epoch: float) -> str:
    if not _is_int(value) or not 0 <= value <= MAX_COUNT:
        return "unverified"
    if value == 0:
        return "unverified" if private else "no"
    return "yes" if now_epoch - value < STORY_SECONDS else "no"   # timestamp no futuro: idade negativa, "yes"


def _highlights(connection: object, *, private: bool) -> str:
    edges = connection.get("edges") if isinstance(connection, Mapping) else None
    if not isinstance(edges, list):
        return "unverified"
    if edges:
        return "yes"
    return "unverified" if private else "no"


def _views(connection: object, has_any_clips: object, *, private: bool) -> tuple[str, int | None, int | None]:
    """(estado, soma, Reels somados). A soma é só da 1a página lida (até 12), não o total da vida da conta. `none` só
    com a conexão ausente ou `edges` lista vazia: conexão malformada não prova que faltam Reels."""
    if private:
        return "unverified", None, None
    edges = connection.get("edges") if isinstance(connection, Mapping) else None
    if isinstance(edges, list):
        # Contagem oculta pelo dono do Reel: o valor que vier não é uma contagem real e fica fora da soma.
        counts = [_count(edge["node"].get("play_count")) for edge in edges
                  if isinstance(edge, Mapping) and isinstance(edge.get("node"), Mapping)
                  and not edge["node"].get("like_and_view_counts_disabled")]
        counted = [count for count in counts if count is not None]
        if counted and sum(counted) <= MAX_COUNT:
            return "known", sum(counted), len(counted)
    if has_any_clips is False and (connection is None or edges == []):
        return "none", None, None
    return "unverified", None, None


def _clips_connection(envelopes: list[tuple[str | None, dict]], user: Mapping) -> object:
    """Conexão de Reels DESTE perfil como veio (a forma é conferida em ``_views``); None = ausente. Envelope com ``pk``
    diferente do usuário é ignorado."""
    pk = user.get("pk")
    for _name, envelope in envelopes:
        connection = envelope.get(CONNECTION_FIELD)
        if connection is None:
            continue
        other = envelope.get("pk")
        if envelope is user or other is None or pk is None or str(other) == str(pk):
            return connection
    return None


def _display_name(value: object) -> str | None:
    """Nome de exibição é dado não confiável: sem caracteres de controle nem surrogates soltos (quebrariam o banco)."""
    if not isinstance(value, str):
        return None
    return _CONTROL_RE.sub("", value).strip()[:MAX_DISPLAY_NAME] or None


def _posts(html: str, user: Mapping) -> dict[str, Any]:
    meta = _read_meta(html)
    posts = parse_posts_text(meta.og) or parse_posts_text(meta.description)
    media_count = user.get("all_media_count")   # nulo sem login; se um dia vier inteiro, é exato
    if (posts is None or not posts[1]) and _is_int(media_count) and 0 < media_count <= MAX_COUNT:
        return {"posts_state": "known", "posts_count": media_count, "posts_exact": 1}
    if posts is None:
        return {}
    return {"posts_state": "known", "posts_count": posts[0], "posts_exact": posts[1], "posts_text": posts[2]}


def _unavailable(username: str) -> ProfileSnapshot:
    return ProfileSnapshot(username, "unavailable", error_code="profile_unavailable", error_message=MSG_UNAVAILABLE)


def _unverified(username: str, code: str, message: str) -> ProfileSnapshot:
    return ProfileSnapshot(username, "unverified", error_code=code, error_message=message)


def parse_profile_document(html: str, username: str, now_epoch: float) -> ProfileSnapshot:
    """HTML da aba Reels anônima -> retrato do perfil (função pura)."""
    html = html or ""
    total, valid, envelopes = _load_scripts(iter_json_scripts(html), USER_FIELD)
    if not envelopes:
        # Script com JSON quebrado pode ser justamente o que traria o envelope: a marca só vale com a leitura íntegra.
        if valid == total and any(marker in html for marker in ERROR_ROUTE_MARKERS):
            return _unavailable(username)
        if valid == total and RESTRICTED_ROUTE in html and NULL_USER in html:
            posts = _posts(html, {})
            if posts.get("posts_state") == "known":
                return ProfileSnapshot(username, "restricted", **posts, error_code="profile_restricted",
                                       error_message=MSG_RESTRICTED)
        return _unverified(username, "profile_unreadable", MSG_UNREADABLE)
    named = [envelope for _name, envelope in envelopes if isinstance(envelope.get("username"), str)]
    if not named:
        return _unverified(username, "profile_unreadable", MSG_UNREADABLE)
    wanted = username.casefold()
    user = next((envelope for envelope in named if envelope["username"].casefold() == wanted), None)
    if user is None:
        return _unverified(username, "username_mismatch", MSG_MISMATCH)
    if user.get("is_unpublished") is True:
        return _unavailable(username)
    private = user.get("is_private")
    if private is False and user.get("is_unpublished") is False:
        status = "active"
    elif private is True:
        status = "private"
    else:
        return _unverified(username, "profile_unreadable", MSG_UNREADABLE)
    is_private = status == "private"
    views_state, views_total, views_reels = _views(
        _clips_connection(envelopes, user), user.get("has_any_clips"), private=is_private)
    return ProfileSnapshot(
        username, status, display_name=_display_name(user.get("full_name")), **_posts(html, user),
        views_state=views_state, views_total=views_total, views_reels=views_reels,
        stories=_stories(user.get("latest_reel_media"), private=is_private, now_epoch=now_epoch),
        highlights=_highlights(user.get(HIGHLIGHTS_FIELD), private=is_private),
    )


def fetch_profile_snapshot(http: Any, username: str, *, now: Callable[[], float] = time.time) -> ProfileSnapshot:
    """Um GET anônimo da aba Reels. 404 -> ``unavailable``; demais recusas sobem como ``InstagramWebError`` (429 ->
    ``rate_limited``). ``now``: relógio de parede em epoch, lido depois da resposta."""
    resp = http.get_document(f"/{username}/reels/", max_bytes=MAX_DOCUMENT_BYTES, diagnostic_path=SAFE_PATH)
    error = classify_public_document(resp, stage=STAGE)
    if error is not None:
        if error.code == "content_unavailable":
            return _unavailable(username)
        error.diagnostic["path"] = SAFE_PATH
        raise error
    html = resp.text or ""
    del resp
    return parse_profile_document(html, username, now())
