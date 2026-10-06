"""Respostas 200 do GraphQL com `data` e `errors` juntos: itens, cursor/checkpoint, contadores e a linha de log.

Offline (httpx.MockTransport, pilha real http + bootstrap + GraphQL + provider); só valores FAKE. Os valores
"sensíveis" plantados no payload (cursor, id longo, URL de CDN assinada, token, shortcode) nunca podem aparecer no log.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from urllib.parse import parse_qsl, quote

import httpx
import pytest

from app.core.logging import redact
from app.integrations.instagram.composite import CompositeInstagramDownloader
from app.integrations.instagram.downloader import InstagramError, ListingCheckpoint
from app.integrations.instagram.session import InstagramSession
from app.integrations.instagram.web import graphql as graphql_mod
from app.integrations.instagram.web.provider import InstagramWebDownloader
from media_fakes import FAKE_SESSION

FIXTURES = Path(__file__).parent / "fixtures" / "instagram_web"
ROOT = "xdt_api__v1__feed__user_timeline_graphql_connection"
GRAPHQL_LOGGER = "app.integrations.instagram.web.graphql"
CURSOR = "FAKE_CURSOR_SECRET_QWxhZGRpbjpvcGVu_"
LONG_ID = "3141592653589793238"                      # id FAKE de 19 dígitos (formato de pk)
PK_PREFIX = "31415926535"
SIGNED_URL = "https://scontent-fake.cdninstagram.com/v/t51.fake/FAKEPATH.jpg?oh=FAKE_SIGNED_OH_77&oe=FAKEOE66"
TOKEN = "FAKEtokenDTSG0001xyzQQ"
SITE_URL = "https://www.instagram.com/p/SYNP01N03/?igsh=FAKEshare123"
SECRET_MARKERS = (CURSOR, "FAKE_CURSOR", LONG_ID, PK_PREFIX, "cdninstagram", "FAKE_SIGNED", "FAKEOE", TOKEN,
                  "FAKEtoken", "SYNP", "FAKEshare", "instagram.com/p/")
# Message FAKE com tudo o que não pode vazar (o nome de campo citado pode ficar).
LEAKY_MESSAGE = (f"Field 'video_versions' failed for media {LONG_ID} code SYNP01N03\nsee {SIGNED_URL} "
                 f"fb_dtsg={TOKEN} bearer {TOKEN} at {SITE_URL} after={CURSOR}1")


class FakeProvider:
    def get(self):
        return InstagramSession({"sessionid": FAKE_SESSION})


def cursor(n: int) -> str:
    """Cursor FAKE do fim da página ``n`` (1-based); leva à página ``n + 1``."""
    return f"{CURSOR}{n}"


def image(page: int, i: int) -> dict:
    code = f"SYNP{page:02d}N{i:02d}"
    return {"code": code, "pk": f"{PK_PREFIX}{page:04d}{i:04d}", "media_type": 1, "product_type": "feed",
            "taken_at": 1700000000,
            "image_versions2": {"candidates": [{"url": f"{SIGNED_URL}&c={code}", "width": 640, "height": 640}]}}


def carousel(page: int, i: int, children: list) -> dict:
    node = image(page, i)
    del node["image_versions2"]
    node.update(media_type=8, carousel_media=children)
    return node


def child(page: int, i: int, k: int) -> dict:
    return {"pk": f"{PK_PREFIX}{page:03d}{i:03d}{k:02d}", "media_type": 1,
            "image_versions2": {"candidates": [{"url": f"{SIGNED_URL}&k={k}", "width": 64, "height": 64}]}}


def images(page: int, count: int = 12) -> list[dict]:
    return [image(page, i) for i in range(count)]


def body(page: int, nodes: list | None = None, *, edges: list | None = None, has_next: bool = True,
         end_cursor: object = "auto", errors: object = None) -> dict:
    """Corpo da página ``page``; ``edges`` (cru) substitui ``nodes`` quando é preciso plantar edge/nó nulo."""
    if edges is None:
        edges = [{"node": node} for node in (nodes or [])]
    if end_cursor == "auto":
        end_cursor = cursor(page) if has_next else None
    payload: dict = {"data": {ROOT: {"edges": edges, "page_info": {"end_cursor": end_cursor,
                                                                    "has_next_page": has_next}}}}
    if errors is not None:
        payload["errors"] = errors
    return payload


class Site:
    """GET / (bootstrap) + POST /graphql/query servindo corpos fixos: sem `after` -> 1a página; `after` = cursor
    da página n -> página n + 1."""

    def __init__(self, *pages: dict) -> None:
        self.pages = list(pages)
        self.afters: list[str | None] = []
        self.gets = 0
        self.bootstrap_html = (FIXTURES / "bootstrap_input.html").read_text(encoding="utf-8")
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/":
            self.gets += 1
            return httpx.Response(200, text=self.bootstrap_html, headers={"content-type": "text/html; charset=utf-8"})
        if request.method == "POST" and request.url.path == "/graphql/query":
            after = json.loads(dict(parse_qsl(request.content.decode()))["variables"]).get("after")
            self.afters.append(after)
            index = 0 if after is None else int(after[len(CURSOR):])
            return httpx.Response(200, text=json.dumps(self.pages[index]),
                                  headers={"content-type": "application/json"})
        return httpx.Response(404, text="{}", headers={"content-type": "application/json"})


def downloader(site: Site, **kwargs) -> InstagramWebDownloader:
    return InstagramWebDownloader(FakeProvider(), transport=site.transport, page_delay=(0, 0), **kwargs)


def anomalies(pages_with_errors: int = 0, errors: int = 0, dropped: dict | None = None,
              skipped: dict | None = None, cursor_missing: int = 0) -> dict:
    return {"pages_with_errors": pages_with_errors, "errors": errors,
            "cursor_missing_end_of_profile": cursor_missing, "dropped_unexpected": dropped or {},
            "skipped_expected": skipped or {}}


def codes(items) -> list[str]:
    return [item.source_post_id for item in items]


def page_lines(caplog) -> list[str]:
    """Linhas de aviso por página do GraphQL (errors parciais ou descartes inesperados)."""
    return [r.getMessage() for r in caplog.records
            if r.name == GRAPHQL_LOGGER and r.levelno == logging.WARNING and "Query: página " in r.getMessage()]


def assert_safe(text: str) -> None:
    for marker in SECRET_MARKERS:
        assert marker not in text, marker


@pytest.fixture(autouse=True)
def _nothing_sensitive_in_any_log(caplog):
    caplog.set_level(logging.DEBUG)
    yield
    assert_safe(caplog.text)


# --------------------------------------------------------------------------- 1 e 6: data completo + errors, cursor válido


def test_full_data_with_errors_delivers_everything_and_keeps_paginating(caplog):
    site = Site(body(1, images(1), errors=[{"message": LEAKY_MESSAGE}]), body(2, images(2), has_next=False))
    cps: list[ListingCheckpoint] = []
    dl = downloader(site)
    items = list(dl.iter_profile_media("perfil_teste", "all", 100, on_checkpoint=cps.append))
    assert codes(items) == [f"SYNP{p:02d}N{i:02d}" for p in (1, 2) for i in range(12)]
    assert site.afters == [None, cursor(1)] and site.gets == 1   # a 2a página é pedida com o cursor da 1a; sem retry
    assert [(cp.resume_cursor, cp.next_page_index, cp.pages, cp.posts_seen, cp.items_compatible,
             cp.unproductive_streak, cp.has_next) for cp in cps] == [
        (cursor(1), 2, 1, 12, 12, 0, True), (None, 3, 2, 24, 24, 0, False)]
    assert dl.last_listing_stats["stop_reason"] == "end_of_profile"
    assert dl.last_listing_anomalies == anomalies(1, 1)
    lines = page_lines(caplog)
    assert len(lines) == 1, lines   # UMA linha, só para a página com errors
    line = lines[0]
    assert line.startswith("GraphQL PolarisProfilePostsQuery: página 1 com errors parciais: errors=1 groups=[{")
    for part in ("count=1}]", "requested=12", "edges=12", "new_nodes=12", "media_normalized=12", "has_next_page=True",
                 "cursor_present=True", "dropped_unexpected={}", "skipped_expected={}", "keys=message",
                 "'video_versions'"):
        assert part in line, part
    assert "\n" not in line and "code=" not in line   # erro sem código, como no real
    assert_safe(line)
    assert redact(line) == line   # o redator do app não tem mais nada a tirar (nem estraga a linha)
    summary = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Listagem web (este trecho)")]
    assert summary == ["Listagem web (este trecho): pages_with_errors=1 errors=1 cursor_missing_end_of_profile=0 "
                       "dropped_unexpected={} skipped_expected={}"]


def test_clean_listing_logs_no_warning_line(caplog):
    site = Site(body(1, images(1)), body(2, images(2), has_next=False))
    dl = downloader(site)
    assert len(list(dl.iter_profile_media("perfil_teste", "all", 100))) == 24
    assert page_lines(caplog) == []
    assert dl.last_listing_anomalies == anomalies()
    assert not [r for r in caplog.records if r.getMessage().startswith("Listagem web (este trecho)")]


# --------------------------------------------------------------------------- 2 e 4: data parcial + errors com path


def partial_edges(page: int) -> list:
    """6 edges: 2 imagens válidas, nó nulo, edge nulo, nó sem `code`, carrossel com um filho nulo."""
    no_code = image(page, 4)
    del no_code["code"]
    return [{"node": image(page, 0)}, {"node": None}, {"node": image(page, 2)}, None, {"node": no_code},
            {"node": carousel(page, 5, [child(page, 5, 0), None, child(page, 5, 2)])}]


def test_partial_data_with_errors_counts_every_discard_and_checkpoint_moves_on(caplog):
    errors = [{"message": "Field failed", "path": [ROOT, "edges", 1, "node"]},
              {"message": LEAKY_MESSAGE, "path": [ROOT, "edges", 5, "node", "carousel_media", 1, "image_versions2"],
               "extensions": {"code": "FIELD_ERROR", "classification": "DataFetchingException", "severity": "CRITICAL"}}]
    site = Site(body(1, edges=partial_edges(1), errors=errors), body(2, images(2), has_next=False))
    cps: list[ListingCheckpoint] = []
    dl = downloader(site)
    items = list(dl.iter_profile_media("perfil_teste", "all", 100, on_checkpoint=cps.append))
    assert [(i.source_post_id, i.source_media_index) for i in items][:4] == [
        ("SYNP01N00", 0), ("SYNP01N02", 0), ("SYNP01N05", 0), ("SYNP01N05", 2)]
    assert len(items) == 4 + 12 and site.afters == [None, cursor(1)]
    # O checkpoint avança além da página com descartes (não é pedida de novo): posts_seen só conta nós entregues.
    first = cps[0]
    assert (first.resume_cursor, first.next_page_index, first.posts_seen, first.items_compatible,
            first.unproductive_streak, first.has_next) == (cursor(1), 2, 4, 4, 0, True)
    dropped = {"node_null": 1, "edge_null": 1, "node_no_code": 1, "carousel_child_invalid": 1}
    assert dl.last_listing_anomalies == anomalies(1, 2, dropped)
    lines = page_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    for part in ("página 1 com errors parciais: errors=2", "edges=6", "new_nodes=4", "media_normalized=4",
                 "dropped_unexpected={carousel_child_invalid:1,edge_null:1,node_no_code:1,node_null:1}",
                 "path=edges[].node ", "count=1 edges_at=1}",
                 "path=edges[].node.carousel_media[].image_versions2 ", "count=1 edges_at=5}",
                 "dropped_at={carousel_child_invalid:[5],edge_null:[3],node_no_code:[4],node_null:[1]}",
                 "extensions.code=FIELD_ERROR extensions.severity=CRITICAL",
                 "keys=extensions,message,path"):
        assert part in line, part
    assert "DataFetching" not in line   # código só em MAIÚSCULAS/`_` (enum) ou inteiro pequeno
    assert ROOT not in line and "edges[1]" not in line   # path do grupo: sem a raiz e sem índices
    assert_safe(line)


def test_unexpected_discards_without_errors_are_logged_once_per_page(caplog):
    site = Site(body(1, edges=partial_edges(1), has_next=False))
    dl = downloader(site)
    assert len(list(dl.iter_profile_media("perfil_teste", "all", 100))) == 4
    lines = page_lines(caplog)
    assert len(lines) == 1 and "página 1 com descartes inesperados: errors=0 requested=12 edges=6" in lines[0]
    assert dl.last_listing_anomalies["pages_with_errors"] == 0
    assert sum(dl.last_listing_anomalies["dropped_unexpected"].values()) == 4


def test_filtered_and_unsupported_are_expected_not_unexpected(caplog):
    unknown = image(1, 1)
    unknown["media_type"] = 99          # tipo que o Media Tool não baixa: de propósito
    typeless = image(1, 2)
    del typeless["media_type"]          # sem media_type: inesperado
    site = Site(body(1, [image(1, 0), unknown, typeless], has_next=False, errors=[{"message": "x"}]))
    dl = downloader(site)
    assert list(dl.iter_profile_media("perfil_teste", "videos", 100)) == []   # a imagem sai pelo filtro de conteúdo
    line = page_lines(caplog)[0]
    assert "dropped_unexpected={media_type_missing:1}" in line
    assert "skipped_expected={content_filter:1,unsupported_type:1}" in line and "media_normalized=1" in line
    assert dl.last_listing_anomalies["skipped_expected"] == {"unsupported_type": 1, "content_filter": 1}


# --------------------------------------------------------------------------- 3: página sem edges + errors


def test_empty_page_with_errors_and_no_next_page_ends_the_profile(caplog):
    site = Site(body(1, images(1)), body(2, [], has_next=False, errors=[{"message": "nothing"}]))
    cps: list[ListingCheckpoint] = []
    dl = downloader(site)
    assert len(list(dl.iter_profile_media("perfil_teste", "all", 100, on_checkpoint=cps.append))) == 12
    assert dl.last_listing_stats["stop_reason"] == "end_of_profile" and cps[-1].has_next is False
    lines = page_lines(caplog)
    assert len(lines) == 1 and "página 2 com errors parciais" in lines[0]
    for part in ("edges=0", "new_nodes=0", "media_normalized=0", "has_next_page=False", "cursor_present=False"):
        assert part in lines[0], part


def test_empty_page_with_errors_and_next_page_stops_as_today(caplog):
    # Comportamento atual preservado (documentado no relatório): página vazia com has_next_page=true encerra a
    # listagem (empty_page) sem pedir a página seguinte, mesmo com cursor válido; o checkpoint sai com has_next=False.
    site = Site(body(1, images(1)), body(2, [], errors=[{"message": "nothing"}]), body(3, images(3), has_next=False))
    cps: list[ListingCheckpoint] = []
    dl = downloader(site)
    assert len(list(dl.iter_profile_media("perfil_teste", "all", 100, on_checkpoint=cps.append))) == 12
    assert site.afters == [None, cursor(1)]
    assert dl.last_listing_stats["stop_reason"] == "empty_page"
    assert (cps[-1].next_page_index, cps[-1].has_next, cps[-1].unproductive_streak) == (3, False, 1)
    line = page_lines(caplog)[0]
    assert "página 2 com errors parciais" in line and "edges=0" in line
    assert "has_next_page=True" in line and "cursor_present=True" in line


# --------------------------------------------------------------------------- 5: errors sem código e com código


@pytest.mark.parametrize("error, expected, absent", [
    ({"message": "boom"}, "{keys=message message=[boom]}", "code="),
    ({"message": "boom", "code": 1357004, "severity": "WARNING"},
     "{code=1357004 severity=WARNING keys=code,message,severity message=[boom]}", None),
    ({"message": "boom", "api_error_code": -1, "extensions": {"code": "SERVER_ERROR", "severity": "CRITICAL"}},
     "{extensions.code=SERVER_ERROR extensions.severity=CRITICAL keys=api_error_code,extensions,message "
     "message=[boom]}", None),
    # Códigos que não parecem enum/inteiro pequeno (id, texto livre, URL) não entram.
    ({"message": "boom", "code": int(LONG_ID), "severity": "see " + SIGNED_URL, "classification": "SYNP01N03",
      "extensions": {"code": TOKEN}}, "{keys=classification,code,extensions,message,severity message=[boom]}",
     "code="),
])
def test_errors_with_and_without_code(caplog, error, expected, absent):
    site = Site(body(1, images(1), has_next=False, errors=[error]))
    assert len(list(downloader(site).iter_profile_media("perfil_teste", "all", 100))) == 12
    assert site.gets == 1 and site.afters == [None]   # código de token junto com dados não refaz o bootstrap
    line = page_lines(caplog)[0]
    assert f"errors=1 groups=[{expected[:-1]} count=1}}]" in line
    if absent:
        assert absent not in line.replace("extensions.code", "")


# --------------------------------------------------------------------------- 7: has_next_page=true sem cursor


@pytest.mark.parametrize("end_cursor", [None, ""])
def test_has_next_without_cursor_and_errors_is_contract_changed_after_delivering_the_page(caplog, end_cursor):
    site = Site(body(1, images(1)), body(2, images(2), end_cursor=end_cursor, errors=[{"message": "partial"}]))
    cps: list[ListingCheckpoint] = []
    dl = downloader(site)
    received = []
    with pytest.raises(InstagramError) as info:
        for item in dl.iter_profile_media("perfil_teste", "all", 100, on_checkpoint=cps.append):
            received.append(item)
    assert info.value.code == "contract_changed" and not info.value.retryable
    assert info.value.diagnostic == {"detail": "cursor_missing_with_errors", "page": 2}
    assert len(received) == 24 and site.afters == [None, cursor(1)]   # os dados utilizáveis foram entregues
    assert [cp.next_page_index for cp in cps] == [2]   # sem checkpoint da página quebrada: ela é pedida de novo
    assert dl.last_listing_stats["stop_reason"] == "error"
    line = page_lines(caplog)[0]
    assert "página 2 com errors parciais" in line and "has_next_page=True" in line and "cursor_present=False" in line


def test_has_next_without_cursor_and_without_errors_ends_as_before(caplog):
    site = Site(body(1, images(1)), body(2, images(2), end_cursor=None))
    cps: list[ListingCheckpoint] = []
    dl = downloader(site)
    assert len(list(dl.iter_profile_media("perfil_teste", "all", 100, on_checkpoint=cps.append))) == 24
    assert dl.last_listing_stats["stop_reason"] == "end_of_profile" and cps[-1].has_next is False
    # 2a: o encerramento não muda, mas agora há aviso explícito e contador (sem a linha de errors da página).
    assert page_lines(caplog) == []
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno == logging.WARNING and "cursor_missing_end_of_profile" in r.getMessage()]
    assert warnings == [
        "GraphQL: página 2 com has_next_page=true sem cursor; listagem encerrada como fim do perfil "
        "(cursor_missing_end_of_profile)",
        "Listagem web (este trecho): pages_with_errors=0 errors=0 cursor_missing_end_of_profile=1 "
        "dropped_unexpected={} skipped_expected={}"]
    assert dl.last_listing_anomalies == anomalies(cursor_missing=1)


def test_runner_keeps_checkpoint_and_asks_for_attention_when_pagination_breaks(harness_factory):
    site = Site(body(1, images(1)), body(2, images(2), end_cursor=None, errors=[{"message": "partial"}]))
    h = harness_factory(downloader=downloader(site))
    batch_id = h.ctx.batches.create(source="instagram_import", status="listing", source_profile="perfil_teste",
                                    content_type="all", requested_limit=100)
    h.run_imports()
    row = h.ctx.db.fetchone("SELECT status, listing_state, listing_cursor, listing_next_page, listing_has_next, "
                            "error_code, found FROM batches WHERE id = ?", (batch_id,))
    assert (row["status"], row["listing_state"], row["error_code"]) == ("listing", "needs_attention",
                                                                       "contract_changed")
    assert (row["listing_cursor"], row["listing_next_page"], row["listing_has_next"]) == (cursor(1), 2, 1)
    assert row["found"] == 24 and site.afters == [None, cursor(1)]   # nada perdido; nenhuma requisição a mais


# --------------------------------------------------------------------------- limite e retomada (itens d e e)


def test_limit_is_filled_with_older_media_after_a_dropped_node(caplog):
    # Documenta o item (d): o nó nulo da página 1 não conta no limite; a 6a mídia vem da página 2 (mais antiga) e o
    # lote "fecha" a quantidade pedida. Antes só o total de nós da página aparecia no log; agora há linha e contador.
    edges = [{"node": image(1, 0)}, {"node": None}, {"node": image(1, 2)}, {"node": image(1, 3)}]
    site = Site(body(1, edges=edges, errors=[{"message": "partial", "path": [ROOT, "edges", 1, "node"]}]),
                body(2, images(2, 4)), body(3, images(3, 4), has_next=False))
    dl = downloader(site)
    items = list(dl.iter_profile_media("perfil_teste", "all", 6))
    assert codes(items) == ["SYNP01N00", "SYNP01N02", "SYNP01N03", "SYNP02N00", "SYNP02N01", "SYNP02N02"]
    assert dl.last_listing_stats["stop_reason"] == "limit_reached" and site.afters == [None, cursor(1)]
    assert dl.last_listing_anomalies["dropped_unexpected"] == {"node_null": 1}
    lines = page_lines(caplog)
    assert len(lines) == 1 and "dropped_unexpected={node_null:1}" in lines[0] and "edges=4 new_nodes=3" in lines[0]


def test_line_is_logged_when_the_consumer_stops_inside_the_page(caplog):
    site = Site(body(1, images(1), errors=[{"message": "partial"}]))
    assert len(list(downloader(site).iter_profile_media("perfil_teste", "all", 5))) == 5
    lines = page_lines(caplog)
    assert len(lines) == 1 and "media_normalized=5" in lines[0] and "edges=12" in lines[0]


def test_resume_from_checkpoint_after_partial_error_page_does_not_request_it_again(caplog):
    edges = [{"node": image(1, 0)}, {"node": None}, {"node": image(1, 2)}, {"node": image(1, 3)}]
    site = Site(body(1, edges=edges, errors=[{"message": "partial"}]), body(2, images(2, 4)),
                body(3, images(3, 4), has_next=False))
    cps: list[ListingCheckpoint] = []
    dl = downloader(site)
    gen = dl.iter_profile_media("perfil_teste", "all", 100, on_checkpoint=cps.append)
    first = [next(gen) for _ in range(4)]   # página 1 inteira (3 mídias) + 1a da página 2
    gen.close()
    assert len(cps) == 1 and (cps[0].resume_cursor, cps[0].next_page_index, cps[0].posts_seen) == (cursor(1), 2, 3)
    site.afters.clear()
    more: list[ListingCheckpoint] = []
    rest = list(dl.iter_profile_media("perfil_teste", "all", 100, resume=cps[0], on_checkpoint=more.append))
    assert site.afters == [cursor(1), cursor(2)]   # retoma da página 2; a página 1 (com o nó perdido) não volta
    assert codes(first[:3]) + codes(rest) == ["SYNP01N00", "SYNP01N02", "SYNP01N03"] + [
        f"SYNP{p:02d}N{i:02d}" for p in (2, 3) for i in range(4)]
    assert [(cp.pages, cp.posts_seen, cp.items_compatible, cp.has_next) for cp in more] == [
        (2, 7, 7, True), (3, 11, 11, False)]
    assert dl.last_listing_anomalies["pages_with_errors"] == 0   # totais são do trecho, não do lote


def test_pages_with_every_node_dropped_count_as_unproductive(caplog):
    # Item (e): páginas com errors em que nada é aproveitado contam na sequência improdutiva e encerram a listagem.
    empty = [{"node": None}] * 3
    site = Site(body(1, images(1)), *[body(n, edges=empty, errors=[{"message": "partial"}]) for n in (2, 3, 4)],
                body(5, images(5), has_next=False))
    cps: list[ListingCheckpoint] = []
    dl = downloader(site)
    assert len(list(dl.iter_profile_media("perfil_teste", "all", 100, on_checkpoint=cps.append))) == 12
    # 2 páginas seguidas sem nó novo encerram (no_new_items) antes do limite de 3 páginas improdutivas.
    assert dl.last_listing_stats["stop_reason"] == "no_new_items" and len(site.afters) == 3
    assert [(cp.unproductive_streak, cp.has_next) for cp in cps] == [(0, True), (1, True), (2, False)]
    assert dl.last_listing_anomalies["dropped_unexpected"] == {"node_null": 6}
    assert len(page_lines(caplog)) == 2


# --------------------------------------------------------------------------- formatos estranhos de `errors`


HUGE = ("A" * 300 + f" {LONG_ID} {SIGNED_URL} {TOKEN}\n\r\t") * 400


@pytest.mark.parametrize("errors, count, expected", [
    (f"boom {LEAKY_MESSAGE}", 1, "groups=[{invalid=str count=1}]"),
    ({"message": "single object", "code": 7}, 1, "groups=[{code=7 keys=code,message message=[single object] count=1}]"),
    (7, 1, "groups=[{invalid=int count=1}]"),
    ([None, 7, "txt " + TOKEN, ["x"], {"message": None}, {"message": "sixth"}, {}], 7,
     "groups=[{invalid=null count=1} {invalid=int count=1} {invalid=str count=1} {invalid=list count=1} "
     "{keys=message count=1} {keys=message message=[sixth] count=1} {keys= count=1}] requested="),
    ([{"message": HUGE, "path": HUGE, "code": HUGE, "extensions": HUGE}], 1, "keys=code,extensions,message,path"),
    ([{"message": {"nested": LEAKY_MESSAGE}, "path": {"x": LONG_ID}, "extensions": [SIGNED_URL],
       LONG_ID: 1, 5: "x"}], 1, "groups=[{keys=extensions,message,path… count=1}]"),
    ([{"message": "p", "path": ["edges", LONG_ID, int(LONG_ID), "SYNP01N03", {"k": TOKEN}, True, -1, 1000, 0,
                               "node", "more"]}], 1, "path=edges.<?>[].<?>.<?>.<?>[][]… "),
    ([{"message": "m"}] * 9, 9, "groups=[{keys=message message=[m] count=9}] requested="),
])
def test_weird_error_shapes_neither_break_nor_leak(caplog, errors, count, expected):
    site = Site(body(1, images(1), has_next=False, errors=errors))
    dl = downloader(site)
    assert len(list(dl.iter_profile_media("perfil_teste", "all", 100))) == 12
    assert dl.last_listing_anomalies["errors"] == count and dl.last_listing_anomalies["pages_with_errors"] == 1
    lines = page_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert f"errors={count} " in line and expected in line, line
    assert "\n" not in line and "\r" not in line and len(line) < 2500
    assert_safe(line)


@pytest.mark.parametrize("errors", [None, [], "", {}, 0, False])
def test_absent_or_empty_errors_are_not_reported(caplog, errors):
    site = Site(body(1, images(1), has_next=False))
    site.pages[0]["errors"] = errors
    dl = downloader(site)
    assert len(list(dl.iter_profile_media("perfil_teste", "all", 100))) == 12
    assert page_lines(caplog) == [] and dl.last_listing_anomalies["pages_with_errors"] == 0


def test_message_is_one_short_line_without_values():
    safe = graphql_mod._safe_error_message(LEAKY_MESSAGE, (TOKEN,))
    assert "'video_versions'" in safe and "<url>" in safe and len(safe) <= 160 and "\n" not in safe
    assert_safe(safe)
    assert len(graphql_mod._safe_error_message(HUGE, ())) <= 160
    assert graphql_mod._safe_error_message('user "Fulano Secreto 99" failed', ()) == 'user "…" failed'
    assert graphql_mod._safe_error_message("A server error field_exception occured. Check server logs", ()) == (
        "A server error field_exception occured. Check server logs")
    assert graphql_mod._safe_error_message(None, ()) == "" and graphql_mod._safe_error_message(123, ()) == ""


def test_refreshed_page_with_errors_logs_safe_line(caplog):
    site = Site(body(1, edges=[{"node": image(1, 0)}, {"node": None}], errors=[{"message": LEAKY_MESSAGE}]))
    dl = downloader(site)
    item = next(iter(dl.iter_profile_media("perfil_teste", "all", 1)))
    caplog.clear()
    assert codes(dl.refresh_page("perfil_teste", item.listing_ref)) == ["SYNP01N00"]
    lines = [r.getMessage() for r in caplog.records if "página renovada com errors parciais" in r.getMessage()]
    assert len(lines) == 1 and "errors=1" in lines[0] and "edges=2 dropped_unexpected={node_null:1}" in lines[0]
    assert_safe(lines[0])


# --------------------------------------------------------------------------- revisão: sanitização adversarial


EMAIL = "fulano.secreto@example.com"
BASE64 = "aB3+xY9/Qz1+kL7/mN2+pQ4/rS6+tU8"
LOWER_TOKEN = "qwertyuiopasdfghjklzxcvb"
UPPER_TOKEN = "QWERTYUIOPASDFGHJKLZXCVB"
COOKIE = "55512345678:abCDfakeEFgh:7:AYfakeTailzz"
DOUBLE_ENCODED = quote(quote(SIGNED_URL, safe=""), safe="")
TRIPLE_ENCODED_SCHEMELESS = quote(quote(quote("scontent-fake.cdninstagram.com/v/t51.fake/FAKEPATH.jpg?oh=x", safe=""),
                                        safe=""), safe="")


@pytest.mark.parametrize("message, fragments", [
    (f"contact {EMAIL} now", ("fulano", "secreto", "example.com", "@")),
    (f"token {BASE64} rejected", (BASE64, "aB3", "Qz1", "tU8")),
    (f"token {LOWER_TOKEN} rejected", (LOWER_TOKEN, LOWER_TOKEN[:12])),
    (f"token {UPPER_TOKEN} rejected", (UPPER_TOKEN, UPPER_TOKEN[:12])),
    (f"bad sessionid {COOKIE} here", ("55512345678", "abCDfake", "AYfake", ":7:")),
    (f"fetch {DOUBLE_ENCODED} failed", ("scontent", "cdninstagram", "FAKEPATH", "%2F", "%252F", "t51")),
    (f"fetch {TRIPLE_ENCODED_SCHEMELESS} failed", ("scontent", "cdninstagram", "FAKEPATH", "%25", "t51")),
    ("media 3141.5926.5358 gone", ("3141", "5926", "5358")),
    ("media 3141 5926 5358 gone", ("3141", "5926", "5358")),
    ("media 3141-5926-53 gone and 77123 too", ("3141", "5926", "77123")),
    ("flip \u202eterces\u2066 \ufeffvalue\u2069 \u202a\u202b\u202c\u202d\u2067\u2068 done",
     ("\u202e", "\u2066", "\ufeff", "\u2069", "\u202a", "\u202b", "\u202c", "\u202d", "\u2067", "\u2068")),
    ("post DAbCdEfGhIj and user.name-x9 and a+b=c", ("DAbCdEfGhIj", "user.name", "x9", "a+b")),
])
def test_adversarial_message_content_never_reaches_any_log(caplog, message, fragments):
    site = Site(body(1, images(1), has_next=False, errors=[{"message": message}]))
    assert len(list(downloader(site).iter_profile_media("perfil_teste", "all", 100))) == 12
    lines = page_lines(caplog)
    assert len(lines) == 1 and "errors=1" in lines[0]
    detail = lines[0].split("message=[", 1)[1].split("] count=1}]", 1)[0]
    for fragment in fragments:
        assert fragment not in detail, (fragment, detail)
    for fragment in fragments:
        if len(fragment) >= 6:   # trechos longos não aparecem em NENHUM log (os curtos colidem com texto normal)
            assert fragment not in caplog.text, fragment
    assert message.split()[0] in detail   # a palavra comum do começo continua legível


def test_allowlist_keeps_plain_words_names_and_small_numbers():
    safe = graphql_mod._safe_error_message
    assert safe("Field 'video_versions' could not be resolved (INTERNAL_SERVER_ERROR), retry 3 of 12.", ()) == (
        "Field 'video_versions' could not be resolved (INTERNAL_SERVER_ERROR), retry 3 of 12.")
    assert safe("GraphQL can't resolve xdt_api__v1__feed__user_timeline_graphql_connection", ()) == (
        "GraphQL can't resolve xdt_api__v1__feed__user_timeline_graphql_connection")
    assert safe("id 12345 and abc123 and Mixed-Thing", ()) == "id <id> and <id> and <id>"


@pytest.mark.parametrize("code", ["DataFetchingException", "warning", "Abc", "ABC1234", "SYNP01N03", "A" * 41,
                                  12345678, -1, True, 1.5, None, ["X"]])
def test_codes_must_look_like_an_uppercase_enum_or_small_integer(code):
    assert graphql_mod._safe_error_code(code) is None


@pytest.mark.parametrize("code, expected", [("WARNING", "WARNING"), ("FIELD_ERROR", "FIELD_ERROR"),
                                            ("ERROR_404", "ERROR_404"), (1357004, "1357004"), ("7", "7")])
def test_enum_and_small_integer_codes_are_kept(code, expected):
    assert graphql_mod._safe_error_code(code) == expected


# --------------------------------------------------------------------------- revisão: observabilidade (2c, 2d, 2f)


def test_carousel_with_fewer_children_than_declared_is_counted_without_errors(caplog):
    node = carousel(1, 0, [child(1, 0, k) for k in range(3)])
    node["carousel_media_count"] = 5
    complete = carousel(1, 1, [child(1, 1, k) for k in range(2)])
    complete["carousel_media_count"] = 2
    site = Site(body(1, [node, complete], has_next=False))
    dl = downloader(site)
    assert len(list(dl.iter_profile_media("perfil_teste", "all", 100))) == 5
    lines = page_lines(caplog)
    assert len(lines) == 1 and "página 1 com descartes inesperados: errors=0 requested=12 edges=2" in lines[0]
    assert "dropped_unexpected={carousel_children_missing:2}" in lines[0] and "media_normalized=5" in lines[0]
    assert dl.last_listing_anomalies == anomalies(dropped={"carousel_children_missing": 2})


def test_smaller_rendition_used_because_larger_candidate_is_invalid_is_counted(caplog):
    node = image(1, 0)
    node["image_versions2"]["candidates"].append({"url": None, "width": 1080, "height": 1080})
    smaller_invalid = image(1, 1)   # candidato inválido MENOR que o escolhido: não conta
    smaller_invalid["image_versions2"]["candidates"].append({"url": None, "width": 64, "height": 64})
    site = Site(body(1, [node, smaller_invalid], has_next=False))
    dl = downloader(site)
    items = list(dl.iter_profile_media("perfil_teste", "all", 100))
    assert [(i.source_post_id, i.width) for i in items] == [("SYNP01N00", 640), ("SYNP01N01", 640)]
    assert dl.last_listing_anomalies == anomalies(dropped={"rendition_candidates_invalid": 1})
    assert "dropped_unexpected={rendition_candidates_invalid:1}" in page_lines(caplog)[0]


def test_page_line_is_logged_even_when_on_page_callback_raises(caplog):
    def boom(meta: dict) -> None:
        raise RuntimeError("callback FAKE falhou")

    site = Site(body(1, images(1), has_next=False, errors=[{"message": "partial"}]))
    dl = downloader(site, on_page=boom)
    with pytest.raises(RuntimeError):
        list(dl.iter_profile_media("perfil_teste", "all", 100))
    lines = page_lines(caplog)
    assert len(lines) == 1 and "página 1 com errors parciais: errors=1" in lines[0]
    assert "media_normalized=0" in lines[0]
    assert dl.last_listing_anomalies == anomalies(1, 1)


# --------------------------------------------------------------------------- revisão: cursor_missing_with_errors no runner


BATCH_COLUMNS = ("status, listing_state, listing_cursor, listing_next_page, listing_has_next, listing_retry_at, "
                 "listing_cursor_refusals, error_code, found")


def _batch(h, limit: int = 100) -> int:
    return h.ctx.batches.create(source="instagram_import", status="listing", source_profile="perfil_teste",
                                content_type="all", requested_limit=limit)


def _row(h, batch_id: int):
    return h.ctx.db.fetchone(f"SELECT {BATCH_COLUMNS} FROM batches WHERE id = ?", (batch_id,))


def test_runner_first_page_break_ends_listing_with_visible_error_and_keeps_items(harness_factory):
    # 1a página do lote (não há checkpoint a manter): os 12 itens viram jobs e a listagem termina (`done`) com o
    # erro visível no lote; nenhuma nova tentativa automática, nenhuma requisição a mais.
    site = Site(body(1, images(1), end_cursor=None, errors=[{"message": "partial"}]))
    h = harness_factory(downloader=downloader(site))
    batch_id = _batch(h)
    h.run_imports()
    row = _row(h, batch_id)
    assert (row["listing_state"], row["error_code"], row["found"]) == ("done", "contract_changed", 12)
    assert row["status"] == "running" and row["listing_retry_at"] is None and row["listing_cursor"] is None
    assert site.afters == [None]


def test_runner_break_on_first_page_of_resumed_chunk_retries_same_cursor_once_then_needs_attention(harness_factory):
    # Comportamento real (regra já existente do "cursor recusado" no início de um trecho retomado, não alterada):
    # `contract_changed` na 1a página do trecho -> listagem `paused` com nova tentativa em 60 s com o MESMO cursor;
    # repetindo, `needs_attention` com o checkpoint mantido (depois, 1 tentativa a cada 6 h, no máximo 4).
    site = Site(body(1, images(1)), body(2, images(2), end_cursor=None, errors=[{"message": "partial"}]))
    h = harness_factory(downloader=downloader(site))
    batch_id = _batch(h)
    h.ctx.batches.update_listing(batch_id, listing_cursor=cursor(1), listing_next_page=2, listing_pages=1,
                                 listing_posts_seen=12, listing_items_compatible=12, listing_unproductive_streak=0,
                                 listing_has_next=1)
    h.run_imports()
    row = _row(h, batch_id)
    assert (row["status"], row["listing_state"], row["error_code"]) == ("listing", "paused", "contract_changed")
    assert row["listing_retry_at"] is not None and row["listing_cursor_refusals"] == 1
    assert (row["listing_cursor"], row["listing_next_page"], row["found"]) == (cursor(1), 2, 12)
    assert site.afters == [cursor(1)]
    h.run_imports()
    assert site.afters == [cursor(1)], "antes do prazo não há nova requisição"
    h.ctx.db.execute("UPDATE batches SET listing_retry_at = NULL WHERE id = ?", (batch_id,))   # fim dos 60 s
    h.run_imports()
    row = _row(h, batch_id)
    assert (row["status"], row["listing_state"], row["error_code"]) == ("listing", "needs_attention",
                                                                       "contract_changed")
    assert (row["listing_cursor"], row["listing_next_page"], row["found"]) == (cursor(1), 2, 12)
    assert site.afters == [cursor(1), cursor(1)]   # exatamente uma nova tentativa, com o mesmo cursor


# --------------------------------------------------------------------------- revisão: estratégia auto sem fallback


class FakeGallery:
    """Provider de fallback FAKE: só registra se foi chamado."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def iter_profile_media(self, username, content, limit, *, cancel=None, **extra):
        self.calls.append("iter_profile_media")
        return iter(())


def _broken_first_page_without_items() -> Site:
    return Site(body(1, edges=[{"node": None}], end_cursor=None, errors=[{"message": "partial"}]))


def test_auto_strategy_does_not_fall_back_on_cursor_missing_with_errors():
    gallery = FakeGallery()
    dl = CompositeInstagramDownloader(downloader(_broken_first_page_without_items()), gallery, strategy="auto")
    with pytest.raises(InstagramError) as info:
        list(dl.iter_profile_media("perfil_teste", "all", 100))
    assert info.value.code == "contract_changed"
    assert info.value.diagnostic["detail"] == "cursor_missing_with_errors" and gallery.calls == []


def test_auto_strategy_still_falls_back_on_other_contract_changes():
    gallery = FakeGallery()
    site = Site({"data": {ROOT: {"edges": None, "page_info": {}}}, "errors": [{"message": "partial"}]})
    dl = CompositeInstagramDownloader(downloader(site), gallery, strategy="auto")
    assert list(dl.iter_profile_media("perfil_teste", "all", 100)) == []
    assert gallery.calls == ["iter_profile_media"]


def test_auto_strategy_batch_ends_with_visible_error_instead_of_gallery_dl(harness_factory):
    gallery = FakeGallery()
    site = _broken_first_page_without_items()
    h = harness_factory(downloader=CompositeInstagramDownloader(downloader(site), gallery, strategy="auto"))
    batch_id = _batch(h)
    h.run_imports()
    row = _row(h, batch_id)
    assert (row["status"], row["error_code"], row["found"]) == ("failed", "contract_changed", 0)
    assert gallery.calls == [] and site.afters == [None]


# --------------------------------------------------------------------------- erros agrupados (sem corte cego em 5)


def location_error(edge: int) -> dict:
    return {"message": "A server error field_exception occured. Check server logs for details.", "severity": "ERROR",
            "path": [ROOT, "edges", edge, "node", "location", "profile_pic_url"]}


def image_error(edge: int) -> dict:
    return {"message": "A server error field_exception occured. Check server logs for details.", "severity": "ERROR",
            "path": [ROOT, "edges", edge, "node", "image_versions2"]}


def no_url_image(page: int, i: int) -> dict:
    node = image(page, i)
    node["image_versions2"] = None
    return node


def run_page(caplog, errors, nodes: list | None = None) -> str:
    site = Site(body(1, nodes if nodes is not None else images(1), has_next=False, errors=errors))
    list(downloader(site).iter_profile_media("perfil_teste", "all", 100))
    lines = page_lines(caplog)
    assert len(lines) == 1, lines
    assert "\n" not in lines[0] and "\r" not in lines[0] and len(lines[0]) <= 3000
    assert_safe(lines[0])
    return lines[0]


def test_more_than_five_errors_of_one_type_are_one_group_with_the_right_count(caplog):
    line = run_page(caplog, [location_error(i) for i in range(9)])
    assert ("errors=9 groups=[{severity=ERROR keys=message,path,severity path=edges[].node.location.profile_pic_url "
            "message=[A server error field_exception occured. Check server logs for details.] count=9 "
            "edges_at=0-8}] requested=12") in line
    assert "omitted" not in line and "other_groups" not in line


def test_a_different_error_after_the_fifth_is_not_hidden_and_crosses_with_the_discard(caplog):
    # O caso da página real: 9 erros de um tipo e, DEPOIS do quinto, 1 de outro tipo no edge da mídia descartada.
    errors = [location_error(i) for i in range(9)]
    errors.insert(7, image_error(5))
    nodes = images(1, 10)
    nodes[5] = no_url_image(1, 5)
    line = run_page(caplog, errors, nodes)
    assert line == (
        # Os dois grupos citam o edge 5 (o da mídia descartada): saem na ordem de chegada.
        "GraphQL PolarisProfilePostsQuery: página 1 com errors parciais: errors=10 groups=["
        "{severity=ERROR keys=message,path,severity path=edges[].node.location.profile_pic_url "
        "message=[A server error field_exception occured. Check server logs for details.] count=9 edges_at=0-8} "
        "{severity=ERROR keys=message,path,severity path=edges[].node.image_versions2 "
        "message=[A server error field_exception occured. Check server logs for details.] count=1 edges_at=5}] "
        "requested=12 edges=10 new_nodes=10 media_normalized=9 has_next_page=False cursor_present=False "
        "dropped_unexpected={image_no_url:1} skipped_expected={} dropped_at={image_no_url:[5]}")


@pytest.mark.parametrize("total", [100, 10_000])
def test_many_errors_keep_the_line_small_and_cheap(caplog, total):
    errors = [location_error(i % 12) for i in range(total - 1)] + [image_error(3)]
    started = time.perf_counter()
    line = run_page(caplog, errors)
    assert time.perf_counter() - started < 5.0
    assert f"errors={total} groups=[{{" in line and f"count={total - 1} edges_at=0-11}}" in line
    assert "path=edges[].node.image_versions2 " in line and "count=1 edges_at=3}]" in line   # o último não some
    assert len(line) < 800


def test_group_ceiling_is_on_distinct_types_and_what_is_left_out_is_explicit(caplog):
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet", "kilo",
             "lima", "mike", "november", "oscar", "papa", "quebec", "romeo", "sierra", "tango"]
    errors = [{"message": f"{word} failed"} for word in words] + [{"message": "zulu failed"}] * 30
    line = run_page(caplog, errors)
    # 21 tipos: os 12 com mais ocorrências aparecem (o de 30, que chegou por último, vem primeiro); 9 ficam fora.
    assert "errors=50 groups=[{keys=message message=[zulu failed] count=30} {keys=message message=[alpha failed] " \
           "count=1}" in line
    assert line.count(" count=") == graphql_mod._MAX_ERROR_GROUPS == 12
    assert "] other_groups=9 other_errors=9 other_paths=[-] requested=12" in line
    assert "message=[kilo failed]" in line and "message=[lima failed]" not in line


def test_huge_distinct_groups_never_pass_the_line_limit(caplog):
    errors = [{"message": " ".join([word] * 30), "path": [ROOT] + ["field_name_long"] * 7}
              for word in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "kilo")]
    line = run_page(caplog, errors)
    shown = line.count(" count=1}")
    assert 1 <= shown < 10 and (f"other_groups={10 - shown} other_errors={10 - shown} "
                                 "other_paths=[field_name_long.field_name_long.") in line
    assert "dropped_unexpected={}" in line and "truncated" not in line   # o fim da linha continua inteiro


ADVERSARIAL_FRAGMENTS = ("scontent", "cdninstagram", "FAKEPATH", "%2F", "%252F", "fulano", "example.com",
                         "FAKEtoken", "SYNP", "31415926", "\u202e", "\u2066")


def adversarial_error(i: int) -> dict:
    """Erro FAKE cujos valores sensíveis mudam a cada ocorrência (o tipo do erro é sempre o mesmo)."""
    signed = f"{SIGNED_URL}&n={i}"
    schemeless = signed.split("://", 1)[1]
    message = (f"Field 'image_versions2' failed\r\nfor media {int(LONG_ID) + i} code SYNP01N{i:02d}\t see {signed} "
               f"or {schemeless} or {quote(signed, safe='')} or {quote(quote(signed, safe=''), safe='')} "
               f"cursor 31415926535{i:07d} token {TOKEN}{i} mail fulano{i}.secreto@example.com "
               f"\u202eflip{i}\u2066 retry 3 of 99")
    return {"message": message, "code": int(LONG_ID) + i, "severity": f"see {signed}",
            "path": [ROOT, "edges", i % 12, "node", f"SYNP01N{i:02d}", int(LONG_ID) + i, f"{TOKEN}{i}"],
            "extensions": {"code": f"{TOKEN}{i}", "classification": schemeless}}


def test_distinct_sensitive_values_neither_split_groups_nor_reach_any_log(caplog):
    line = run_page(caplog, [adversarial_error(i) for i in range(40)])
    # Um grupo só: a chave é calculada depois da sanitização (a cardinalidade dos valores não vaza).
    assert "errors=40 groups=[{keys=code,extensions,message,path,severity path=edges[].node.<?>[].<?> message=[" in line
    assert line.count(" count=") == 1 and "count=40 edges_at=0-11}] requested=12" in line
    assert "other_groups" not in line
    assert "'image_versions2'" in line
    for fragment in ADVERSARIAL_FRAGMENTS:
        assert fragment not in line, fragment
        assert fragment not in caplog.text, fragment


def test_group_key_helpers_are_safe_on_their_own():
    report = graphql_mod._errors_report({"errors": [adversarial_error(i) for i in range(3)] + [None, 7]}, (TOKEN,), ROOT)
    assert report["count"] == 5
    assert [(g[1], g[2], sorted(g[3])) for g in report["groups"]] == [
        ("edges[].node.<?>[].<?>", 3, [0, 1, 2]), ("-", 1, []), ("-", 1, [])]
    assert_safe(" ".join(g[0] for g in report["groups"]))
    assert graphql_mod._errors_text(report).startswith("errors=5 groups=[{keys=code,extensions,message,path,severity")
    assert graphql_mod._ranges({0, 1, 2, 3, 4, 7}) == "0-4,7"
    assert graphql_mod._ranges(set(range(0, 40, 2))) == "0,2,4,6,8,10,12,14,…"
    assert graphql_mod._safe_error_path([ROOT], ROOT) == "<root>"
    assert graphql_mod._error_edge_index([ROOT, "edges", int(LONG_ID), "node"]) is None
    assert graphql_mod._error_edge_index([ROOT, "edges", True]) is None


# --------------------------------------------------------------------------- revisão: quem aparece quando não cabe tudo


WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet", "kilo", "lima"]


def field_error(field: str, edge: int, message: str = "field failed") -> dict:
    return {"message": message, "path": [ROOT, "edges", edge, "node", field]}


def test_rare_errors_at_the_start_and_at_the_end_are_shown_before_repeated_ones(caplog):
    # 14 tipos para 12 vagas: os dois raros (1 ocorrência, no começo e no fim) aparecem; ficam fora dois dos
    # repetidos, contados e com o path listado.
    errors = [field_error("caption", 0, "rare first failed")]
    errors += [field_error(word, edge) for word in WORDS for edge in range(3)]
    errors.append(field_error("owner", 9, "rare last failed"))
    line = run_page(caplog, errors)
    assert "{keys=message,path path=edges[].node.caption message=[rare first failed] count=1 edges_at=0}" in line
    assert "{keys=message,path path=edges[].node.owner message=[rare last failed] count=1 edges_at=9}" in line
    assert line.count(" count=") == 12
    assert ("] other_groups=2 other_errors=6 other_paths=[edges[].node.kilo,edges[].node.lima] requested=12 "
            "edges=12") in line
    assert line.split("groups=[", 1)[1].startswith("{keys=message,path path=edges[].node.alpha ")   # mais ocorrências


def test_other_paths_is_bounded_and_says_how_many_more(caplog):
    names = [a + b for a in "abcdef" for b in "ghijk"]   # 30 paths distintos, 1 erro cada
    line = run_page(caplog, [field_error(name, 0) for name in names])
    assert line.count(" count=1 ") == 12
    assert " other_groups=18 other_errors=18 other_paths=[edges[].node.ci,edges[].node.cj," in line
    assert line.count("edges[].node.") == 12 + graphql_mod._MAX_OTHER_PATHS and ",+10] requested=12" in line


def test_error_that_crosses_with_a_discard_is_never_left_out(caplog):
    # O erro do edge 7 (onde a imagem foi descartada) é do path dominante, com 1 ocorrência, e chega por último
    # entre 15 tipos: pela contagem e pela diversidade de path ele ficaria fora; por cruzar com o descarte, é o 1o.
    errors = [field_error("location", edge % 5, "common failure") for edge in range(50)]
    names = WORDS + ["mike"]
    errors += [field_error(word, edge) for word in names for edge in range(3)]
    errors.append(field_error("location", 7, "other failure"))
    nodes = images(1, 10)
    nodes[7] = no_url_image(1, 7)
    line = run_page(caplog, errors, nodes)
    assert "errors=90 groups=[{keys=message,path path=edges[].node.location message=[other failure] count=1 " \
           "edges_at=7} {keys=message,path path=edges[].node.location message=[common failure] count=50 " \
           "edges_at=0-4} {" in line
    assert "dropped_at={image_no_url:[7]}" in line and " other_groups=3 other_errors=9 other_paths=[" in line
    # Sem o descarte no edge 7, o mesmo erro fica fora (mas contado, com o path em other_paths).
    caplog.clear()
    line = run_page(caplog, errors)
    assert "other failure" not in line and " other_groups=3 other_errors=7 other_paths=[" in line
    assert "edges[].node.location" in line.split("other_paths=[", 1)[1]


def test_short_numbers_keep_different_errors_apart(caplog):
    line = run_page(caplog, [{"message": "upstream returned 404"}, {"message": "upstream returned 500"},
                             {"message": "upstream returned 404"}])
    assert ("errors=3 groups=[{keys=message message=[upstream returned 404] count=2} "
            "{keys=message message=[upstream returned 500] count=1}] requested=12") in line


def test_quoted_names_that_change_per_error_do_not_create_one_group_each(caplog):
    line = run_page(caplog, [{"message": f"Field 'campo_{i}' failed"} for i in range(30)])
    # Na chave o trecho citado vira "…"; o texto exibido é o do primeiro erro do grupo.
    assert "errors=30 groups=[{keys=message message=[Field 'campo_0' failed] count=30}] requested=12" in line
    assert "campo_1" not in line
