"""Consultas GraphQL persistidas do Instagram Web (contrato INSTAGRAM_WEB_CONTRACT.md, web/queries.py).

Único lugar do código com `doc_id`. O valor embutido é o observado no Chrome do dono; pode ser trocado sem
nova versão por env `MEDIA_TOOL_IG_DOC_<NAME>` ou pelo setting `instagram_web_doc_ids` (nome -> doc_id).

ATENÇÃO: a consulta de post isolado (POST_BY_SHORTCODE) NÃO foi observada no Chrome do dono nem verificada no
Instagram real; nome, doc_id, campo raiz e variáveis vêm de conhecimento público e são trocáveis sem nova versão
(doc_id por setting/env; campo raiz por env `MEDIA_TOOL_IG_ROOT_<NAME>`).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace

log = logging.getLogger(__name__)

# Só dígitos ASCII (\d aceitaria dígitos Unicode); usar com fullmatch.
DOC_ID_RE = re.compile(r"[0-9]{5,25}")
ENV_PREFIX = "MEDIA_TOOL_IG_DOC_"
# Override do campo raiz (`data.<campo>`) por env, para quando o doc_id trocado responder em outra raiz.
ROOT_ENV_PREFIX = "MEDIA_TOOL_IG_ROOT_"
# Nome de campo seguro: minúsculas ASCII, dígitos e `_` (vai para o header X-Root-Field-Name); usar com fullmatch.
ROOT_FIELD_RE = re.compile(r"[a-z][a-z0-9_]{2,119}")


@dataclass(frozen=True)
class QuerySpec:
    name: str              # fb_api_req_friendly_name
    doc_id: str
    root_field: str        # chave esperada dentro de data
    observed_at: str       # data da evidência
    relay_providers: Mapping[str, bool]


PROFILE_POSTS = QuerySpec(
    "PolarisProfilePostsQuery", "28991540097136703",
    "xdt_api__v1__feed__user_timeline_graphql_connection", "2026-10-03",
    {"__relay_internal__pv__PolarisMultiCaptionCarouselEnabledrelayprovider": False,
     "__relay_internal__pv__PolarisShortDramaEnabledrelayprovider": False,
     "__relay_internal__pv__PolarisReelsRecoDebugOverlayEnabledrelayprovider": False},
)

# Post isolado pelo shortcode. NÃO VERIFICADO: sem evidência do navegador do dono e sem requisição real (por isso
# `observed_at` é "unverified"). Nome/doc_id/raiz do conhecimento público; a resposta esperada é o formato GraphQL
# "legado" (ver normalize.normalize_post). Se o Instagram recusar, troque o doc_id (setting/env) e, se a consulta
# nova responder em outra raiz (ex.: `PolarisPostRootQuery`, raiz `xdt_api__v1__media__shortcode__web_info`), o
# campo raiz por env `MEDIA_TOOL_IG_ROOT_POLARISPOSTACTIONLOADPOSTQUERYQUERY`.
POST_BY_SHORTCODE = QuerySpec(
    "PolarisPostActionLoadPostQueryQuery", "8845758582119845", "xdt_shortcode_media", "unverified", {},
)

# Nomes aceitos em `instagram_web_doc_ids` (o schema valida contra esta lista).
KNOWN_QUERIES: tuple[str, ...] = (PROFILE_POSTS.name, POST_BY_SHORTCODE.name)


def _valid_override(value: object, *, name: str, source: str) -> str | None:
    if isinstance(value, str) and DOC_ID_RE.fullmatch(value.strip()):
        return value.strip()
    # Valor nunca é registrado (pode ser qualquer coisa colada pelo usuário); só nome e origem.
    log.warning("doc_id inválido ignorado para %s (origem: %s)", name, source)
    return None


def resolve_spec(spec: QuerySpec, overrides: Mapping[str, str],
                 env: Mapping[str, str] | None = None) -> QuerySpec:
    """Aplica override de doc_id por nome (sem diferenciar maiúsculas).

    Precedência: setting `instagram_web_doc_ids` (``overrides``) > env `MEDIA_TOOL_IG_DOC_<NAME>` (``env``, ex.:
    Settings.env, que inclui o .env; ``os.environ`` não é lido aqui) > valor embutido. Override inválido (fora de
    ``[0-9]{5,25}``) cai para a próxima fonte, com aviso que não mostra o valor. Nomes fora de KNOWN_QUERIES são
    ignorados com aviso que não mostra nome nem valor.

    Campo raiz: env `MEDIA_TOOL_IG_ROOT_<NAME>` (só env; nome de campo seguro, ROOT_FIELD_RE) > valor embutido.
    Valor inválido é ignorado com aviso que não o mostra.
    """
    spec = _resolve_root(spec, env)
    known = {name.upper() for name in KNOWN_QUERIES}
    unknown = sum(1 for key in (overrides or {}) if not isinstance(key, str) or key.upper() not in known)
    if unknown:
        log.warning("%d override(s) de doc_id com nome de consulta desconhecido ignorado(s)", unknown)
    for key, value in (overrides or {}).items():
        if isinstance(key, str) and key.upper() == spec.name.upper():
            doc_id = _valid_override(value, name=spec.name, source="setting")
            if doc_id:
                return replace(spec, doc_id=doc_id)
    env_value = (env or {}).get(ENV_PREFIX + spec.name.upper())
    if env_value:
        doc_id = _valid_override(env_value, name=spec.name, source="env")
        if doc_id:
            return replace(spec, doc_id=doc_id)
    return spec


def _resolve_root(spec: QuerySpec, env: Mapping[str, str] | None) -> QuerySpec:
    value = (env or {}).get(ROOT_ENV_PREFIX + spec.name.upper())
    if not value:
        return spec
    if isinstance(value, str) and ROOT_FIELD_RE.fullmatch(value.strip()):
        return replace(spec, root_field=value.strip())
    log.warning("campo raiz inválido ignorado para %s (origem: env)", spec.name)
    return spec


def post_variables(spec: QuerySpec, shortcode: str) -> dict:
    """Variáveis da consulta de post isolado. NÃO VERIFICADO no Instagram real (conhecimento público)."""
    variables: dict = {"shortcode": shortcode, "fetch_tagged_user_count": None, "hoisted_comment_id": None,
                       "hoisted_reply_id": None}
    variables.update(spec.relay_providers)
    return variables


def profile_posts_variables(spec: QuerySpec, username: str, count: int, after: str | None) -> dict:
    """Variáveis do PolarisProfilePostsQuery.

    1a página (after=None): exatamente como observado no navegador em 2026-10-03.
    """
    variables: dict = {}
    if after is not None:
        # Verificado no Instagram real em 2026-10-03 (POC de paginação: página 2 HTTP 200, itens novos, sem
        # duplicatas): páginas seguintes no padrão Relay de conexão.
        variables.update({"after": after, "before": None})
    variables["data"] = {
        "count": count,
        "include_reel_media_seen_timestamp": True,
        "include_relationship_info": True,
        "latest_besties_reel_media": True,
        "latest_reel_media": True,
    }
    if after is not None:
        # Verificado no real (idem): first/last do padrão Relay.
        variables.update({"first": count, "last": None})
    variables["username"] = username
    variables.update(spec.relay_providers)
    return variables
