"""Aba Reels do perfil SEM login, FAKE (web/reels_tab.py): a estrutura verificada no Instagram real em 2026-10-29,
só com valores sintéticos. Nenhum teste aqui acessa a internet.

- ``ReelsTab``: serve ``GET /<usuario>/reels/`` (HTML com o envelope ``xig_user_by_username`` e o token ``LSD``, e um
  ``Set-Cookie`` anônimo) e ``POST /api/graphql`` (``data.node.polaris_clips_connection``), registrando o que chegou.
- ``attach``: junta a aba a um ``test_graphql_partial_errors.Site`` (a listagem com a sessão) no mesmo transporte.
- ``MirroredSite``: um ``Site`` cuja aba Reels espelha os Reels da listagem (``play_count`` do nó da listagem vira o
  da aba; a listagem real não traz contagem e o provider ignora o campo nela), com ``pk`` decrescente como no real.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qsl

import httpx

import test_graphql_partial_errors as gp

LSD = "FAKElsdREELStoken77"
CSRF = "FAKEcsrfANONcookie55"
USER_ID = "8080808080"
REELS_CURSOR = "FAKE_REELS_CURSOR_"
FRIENDLY = "PolarisLoggedOutDesktopWWWProfileReelsTabContentQuery_connection"
DOC_ID = "27420265870989234"
MISSING = object()
PK_START = 9_000_000_000_000


def tab_node(code: str, pk: int | str, play_count: object = MISSING, *, pinned: bool = False) -> dict:
    node = {"__typename": "XIGPolarisVideoMedia", "code": code, "pk": str(pk), "id": f"{pk}_{USER_ID}",
            "like_count": 3, "comment_count": 1, "product_type": "clips", "like_and_view_counts_disabled": False,
            "clips_tab_pinned_user_ids": [USER_ID] if pinned else [],
            "display_uri": "https://scontent-fake.cdninstagram.com/v/fake-thumb.jpg",
            "user": {"pk": USER_ID, "id": USER_ID}, "seo_canonical_url": None}
    if play_count is not MISSING:
        node["play_count"] = play_count
    return node


def connection(nodes: list, page: int, has_next: bool) -> dict:
    return {"edges": [{"node": node} for node in nodes],
            "page_info": {"end_cursor": f"{REELS_CURSOR}{page}" if has_next else None, "has_next_page": has_next}}


def reels_html(nodes: list, has_next: bool, *, lsd: str | None = LSD, user: object = MISSING) -> str:
    data_user = {"id": USER_ID, "pk": USER_ID, "polaris_clips_connection": connection(nodes, 1, has_next)}
    if user is not MISSING:
        data_user = user
    entry = ["RelayPrefetchedStreamCache", "next", [],
             ["adp_PolarisLoggedOutDesktopWWWProfileReelsTabContentQueryRelayPreloader_fake0",
              {"__bbox": {"result": {"data": {"xig_user_by_username": data_user}}}}]]
    payload = {"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"require": [entry]}}]]]}
    lsd_js = f'<script>requireLazy([],function(){{}});["LSD",[],{{"token":"{lsd}"}},323]</script>' if lsd else ""
    return (f'<!DOCTYPE html><html><head>{lsd_js}<script type="application/json" data-sjs>{json.dumps(payload)}'
            "</script></head><body></body></html>")


class ReelsTab:
    """``pages``: lista de páginas (cada uma, lista de nós da aba). ``responses``: índice da requisição (0 = GET da
    aba, 1 = 1o POST...) -> ``httpx.Response`` no lugar da resposta normal."""

    def __init__(self, pages: list[list[dict]], *, responses: dict[int, httpx.Response] | None = None,
                 html: str | None = None) -> None:
        self.pages = pages
        self.responses = dict(responses or {})
        self.html = html
        self.gets = 0
        self.posts: list[dict] = []          # formulários recebidos
        self.headers: list[httpx.Headers] = []
        self.requests = 0

    def handle(self, request: httpx.Request) -> httpx.Response | None:
        path = request.url.path
        if request.method == "GET" and path.endswith("/reels/"):
            self.gets += 1
            self.headers.append(request.headers)
            index = self.requests
            self.requests += 1
            if index in self.responses:
                return self.responses[index]
            body = self.html if self.html is not None else reels_html(self.pages[0] if self.pages else [],
                                                                      len(self.pages) > 1)
            return httpx.Response(200, text=body, headers=[("content-type", "text/html; charset=utf-8"),
                                                           ("set-cookie", f"csrftoken={CSRF}; Path=/; Secure")])
        if request.method == "POST" and path == "/api/graphql":
            form = dict(parse_qsl(request.content.decode()))
            self.posts.append(form)
            self.headers.append(request.headers)
            index = self.requests
            self.requests += 1
            if index in self.responses:
                return self.responses[index]
            after = json.loads(form["variables"])["after"]
            page = int(after[len(REELS_CURSOR):])        # cursor da página n leva à página n + 1
            has_next = page + 1 < len(self.pages)
            body = {"data": {"node": {"__typename": "XIGUser", "id": USER_ID,
                                      "polaris_clips_connection": connection(self.pages[page], page + 1, has_next)}},
                    "extensions": {"is_final": True}}
            return httpx.Response(200, text=json.dumps(body), headers={"content-type": "application/json"})
        return None


def attach(site: gp.Site, tab: ReelsTab) -> gp.Site:
    listing = site.handle

    def handle(request: httpx.Request) -> httpx.Response:
        answer = tab.handle(request)
        return answer if answer is not None else listing(request)

    site.transport = httpx.MockTransport(handle)
    site.tab = tab
    return site


def listing_nodes(site: gp.Site):
    for page in site.pages:
        for edge in page["data"][gp.ROOT]["edges"]:
            node = edge.get("node") if isinstance(edge, dict) else None
            if isinstance(node, dict):
                yield node


class MirroredSite(gp.Site):
    """``gp.Site`` + aba Reels com os Reels da listagem, na mesma ordem, 12 por página."""

    def __init__(self, *pages: dict, per_page: int = 12) -> None:
        super().__init__(*pages)
        nodes = []
        pk = PK_START
        for node in listing_nodes(self):
            pk -= 1
            node["pk"] = str(pk)   # do mais novo para o mais antigo, como no real
            if node.get("media_type") == 2 and node.get("product_type") == "clips":
                nodes.append(tab_node(node["code"], pk, node.get("play_count", MISSING)))
        tab_pages = [nodes[i:i + per_page] for i in range(0, len(nodes), per_page)] or [[]]
        attach(self, ReelsTab(tab_pages))
