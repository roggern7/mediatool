"""Documento público de UMA publicação -> nó da mídia (contrato INSTAGRAM_WEB_CONTRACT.md, web/post_document.py).

A publicação vem EMBUTIDA no HTML de ``GET /reel/<código>/`` aberto SEM login, num
``<script type="application/json" data-sjs>``; nenhuma consulta de API a traz. Caminho dentro do JSON do script:
``[..., ["RelayPrefetchedStreamCache", "next", [], [<preloader>, {"__bbox": {"result": {"data":
{"xig_polaris_media": {...}}}}}]], ...]``.

VERIFICADO no Instagram real (2026-10-10, navegador anônimo, UM Reel público): o envelope acima, ``gating_ruling``
nulo e ``if_not_gated_logged_out`` com a mídia no formato v1 (``code``, ``pk``, ``media_type`` 2, ``product_type``
"clips", ``user.username``, ``image_versions2.candidates``, ``video_versions[{type, url}]`` sem largura/altura).

NÃO VERIFICADO (o código aceita, mas falha de forma segura se vier diferente): foto e carrossel neste envelope, o
caminho ``/p/``, a forma de ``gating_ruling`` quando não é nulo e o que vem no lugar da mídia numa publicação
restrita, e uma tela de login respondida com 200.

Sem regex sobre o HTML: ``html.parser`` trata o conteúdo de ``<script>`` como texto cru. Nada do HTML, das URLs ou
do shortcode vai para log, exceção ou ``PostDocumentParse``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser

OBSERVED_AT = "2026-10-10"                      # Chrome anônimo, Reel público
CACHE_MARKER = "RelayPrefetchedStreamCache"     # observado
ROOT_FIELD = "xig_polaris_media"                # observado
UNGATED_FIELD = "if_not_gated_logged_out"       # observado
GATING_FIELD = "gating_ruling"                  # observado (valor null)
# Nome do preloader visto na página (com um sufixo `_<hash>`). Só sinal para o log; nunca exigência.
OBSERVED_PRELOADER_PREFIX = "adp_PolarisLoggedOutDesktopWWWPostRootContentQuery"
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024            # observado ~750 KB
MAX_SCRIPTS = 200                               # observado ~40
MAX_SCRIPT_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 64

_TYPENAME_RE = re.compile(r"[A-Za-z]{1,40}")


@dataclass
class PostDocumentParse:
    """Resumo da leitura do documento, seguro para log: só contagens, booleanos e tokens fixos."""

    scripts: int = 0                 # scripts `application/json` com `data-sjs`
    json_scripts: int = 0            # deles, os que são JSON válido
    envelopes: int = 0               # objetos `xig_polaris_media` achados
    preloader_observed: bool = False
    outcome: str = "no_scripts"      # "ok" | "no_scripts" | "envelope_missing" | "mismatch" | "gated"
    typename: str | None = None      # `__typename` do envelope escolhido; só se for um nome de tipo simples


class _ScriptCollector(HTMLParser):
    """Junta o texto de cada ``<script type="application/json" data-sjs>`` (dentro dos tetos)."""

    def __init__(self, max_scripts: int, max_script_bytes: int) -> None:
        super().__init__(convert_charrefs=False)
        self._max_scripts = max_scripts
        self._max_script_bytes = max_script_bytes
        self._parts: list[str] | None = None   # None = fora de um script de interesse
        self._size = 0
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script" or len(self.scripts) >= self._max_scripts:
            return
        names = dict(attrs)
        if (names.get("type") or "").strip().lower() == "application/json" and "data-sjs" in names:
            self._parts = []
            self._size = 0

    def handle_data(self, data: str) -> None:
        if self._parts is None:
            return
        self._size += len(data)
        if self._size > self._max_script_bytes:
            self._parts = None   # script grande demais: descartado inteiro
            return
        self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._parts is not None:
            self.scripts.append("".join(self._parts))
            self._parts = None


def iter_json_scripts(html: str, *, max_scripts: int = MAX_SCRIPTS,
                      max_script_bytes: int = MAX_SCRIPT_BYTES) -> Iterator[str]:
    """Texto de cada ``<script type="application/json" data-sjs>`` do HTML, na ordem (no máximo ``max_scripts``;
    script acima de ``max_script_bytes`` é ignorado)."""
    collector = _ScriptCollector(max_scripts, max_script_bytes)
    collector.feed(html or "")
    collector.close()
    yield from collector.scripts


def _envelope_of(entry: object) -> tuple[str | None, dict] | None:
    """``["RelayPrefetchedStreamCache", <ação>, <args>, [<preloader>, {"__bbox": {"result": {"data": {...}}}}]]``
    -> (nome do preloader, ``data.xig_polaris_media``); None se a lista não for isso."""
    if not isinstance(entry, list) or not entry or entry[0] != CACHE_MARKER:
        return None
    payload = entry[-1]
    if not isinstance(payload, list):
        return None
    preloader = next((item for item in payload if isinstance(item, str)), None)
    for item in payload:
        bbox = item.get("__bbox") if isinstance(item, dict) else None
        result = bbox.get("result") if isinstance(bbox, dict) else None
        data = result.get("data") if isinstance(result, dict) else None
        envelope = data.get(ROOT_FIELD) if isinstance(data, dict) else None
        if isinstance(envelope, dict):
            return preloader, envelope
    return None


def _walk(value: object, depth: int, found: list[tuple[str | None, dict]]) -> None:
    if depth > MAX_DEPTH:
        return
    if isinstance(value, list):
        envelope = _envelope_of(value)
        if envelope is not None:
            found.append(envelope)
            return
        for item in value:
            if isinstance(item, (list, dict)):
                _walk(item, depth + 1, found)
    elif isinstance(value, dict):
        for item in value.values():
            if isinstance(item, (list, dict)):
                _walk(item, depth + 1, found)


def _load_scripts(scripts: Iterable[str]) -> tuple[int, int, list[tuple[str | None, dict]]]:
    total = valid = 0
    found: list[tuple[str | None, dict]] = []
    for text in scripts:
        total += 1
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            continue   # script que não é JSON válido: contado e ignorado
        valid += 1
        _walk(data, 0, found)
    return total, valid, found


def find_media_envelopes(scripts: Iterable[str]) -> list[tuple[str | None, dict]]:
    """(nome do preloader ou None, objeto ``xig_polaris_media``) de cada envelope achado nos scripts, na ordem.
    Script com JSON inválido é ignorado; a caminhada não desce além de ``MAX_DEPTH`` níveis."""
    return _load_scripts(scripts)[2]


def select_media_node(envelopes: Iterable[tuple[str | None, Mapping]], shortcode: str) -> tuple[str, Mapping | None]:
    """Escolhe o nó da mídia do ``shortcode`` pedido (comparação EXATA, diferenciando maiúsculas).

    ``("ok", nó)``: envelope com ``code == shortcode``, ``gating_ruling`` nulo e ``if_not_gated_logged_out`` objeto
    que não declara outro ``code``. ``("gated", None)``: o envelope do shortcode existe, mas ``gating_ruling`` não é
    nulo ou ``if_not_gated_logged_out`` não é objeto (forma NÃO verificada no real: tratado como publicação
    restrita). ``("mismatch", None)``: só há envelope de outra publicação. ``("envelope_missing", None)``: nenhum."""
    outcome = "envelope_missing"
    for _preloader, envelope in envelopes:
        if envelope.get("code") != shortcode:
            outcome = "mismatch" if outcome == "envelope_missing" else outcome
            continue
        node = envelope.get(UNGATED_FIELD)
        if envelope.get(GATING_FIELD) is not None or not isinstance(node, Mapping):
            outcome = "gated"
            continue
        inner = node.get("code")
        if isinstance(inner, str) and inner and inner != shortcode:
            outcome = "mismatch" if outcome == "envelope_missing" else outcome
            continue
        # Nó sem `code` segue adiante: a normalização não devolve mídia e quem chama responde `post_shape_unknown`.
        return "ok", node
    return outcome, None


def parse_post_document(html: str, shortcode: str) -> tuple[Mapping | None, PostDocumentParse]:
    """HTML do documento público -> (nó da mídia no formato v1 ou None, resumo seguro da leitura)."""
    parse = PostDocumentParse()
    parse.scripts, parse.json_scripts, envelopes = _load_scripts(iter_json_scripts(html))
    parse.envelopes = len(envelopes)
    parse.preloader_observed = any(
        isinstance(name, str) and name.startswith(OBSERVED_PRELOADER_PREFIX) for name, _ in envelopes)
    if not parse.scripts:
        return None, parse
    parse.outcome, node = select_media_node(envelopes, shortcode)
    if parse.outcome in ("ok", "gated"):
        chosen = next((env for _name, env in envelopes if env.get("code") == shortcode), None)
        typename = chosen.get("__typename") if chosen is not None else None
        if isinstance(typename, str) and _TYPENAME_RE.fullmatch(typename) and typename != shortcode:
            parse.typename = typename
    return node, parse
