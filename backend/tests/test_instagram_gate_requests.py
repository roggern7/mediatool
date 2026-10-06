"""Portão global por requisição HTTP (docs/BATCH_ROBUSTNESS.md, seções 7 e 8). Tudo offline, valores FAKE.

- Provider web: cada requisição ao instagram.com (documento, API, POST GraphQL) ocupa UMA vaga do portão; ninguém
  segura o portão por fora (sem deadlock, portão não reentrante). Downloads de CDN não passam por ele.
- Provider gallery-dl: uma operação (subprocess) = uma vaga.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest

from app.integrations.instagram.downloader import InstagramError, ListingRef
from app.integrations.instagram.gallerydl import GalleryDlInstagramDownloader
from app.integrations.instagram.session import InstagramSession
from app.integrations.instagram.web.provider import InstagramWebDownloader
from app.services.instagram_gate import GateCancelled, InstagramRequestGate
from media_fakes import FAKE_SESSION

FIXTURES = Path(__file__).parent / "fixtures" / "instagram_web"
PAGES = 50
NODES_PER_PAGE = 12
INTERVAL = 0.01
JOIN_TIMEOUT = 60.0


class FakeProvider:
    def get(self):
        return InstagramSession({"sessionid": FAKE_SESSION})


def _template_node() -> dict:
    data = json.loads((FIXTURES / "graphql" / "page2_last.json").read_text(encoding="utf-8"))
    return data["data"]["xdt_api__v1__feed__user_timeline_graphql_connection"]["edges"][0]["node"]


def _page(index: int, template: dict) -> str:
    edges = []
    for n in range(NODES_PER_PAGE):
        node = copy.deepcopy(template)
        serial = f"{index:03d}{n:02d}"
        node["code"] = f"FAKEG{serial}"
        node["pk"] = f"31000000000{serial}"
        node["id"] = f"31000000000{serial}_900000001"
        edges.append({"node": node, "cursor": None})
    has_next = index < PAGES
    body = {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {
        "edges": edges,
        "page_info": {"end_cursor": f"FAKE_CURSOR_{index}" if has_next else None, "has_next_page": has_next,
                      "has_previous_page": False, "start_cursor": None},
    }}, "extensions": {"is_final": True}, "status": "ok"}
    return json.dumps(body)


class GatedInstagram:
    """Transporte em memória que mede requisições em voo e o intervalo entre elas (relógio real)."""

    def __init__(self, graphql_status: int = 200) -> None:
        template = _template_node()
        self.pages = {i: _page(i, template) for i in range(1, PAGES + 1)}
        self.html = (FIXTURES / "bootstrap_input.html").read_text(encoding="utf-8")
        self.graphql_status = graphql_status
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.spans: list[tuple[float, float, str, str]] = []   # (início, fim, path, thread)
        self.on_listing_page: dict[int, threading.Event] = {}
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        start = time.monotonic()
        try:
            time.sleep(0.002)   # duração simulada da requisição
            return self._respond(request)
        finally:
            end = time.monotonic()
            with self.lock:
                self.in_flight -= 1
                self.spans.append((start, end, request.url.path, threading.current_thread().name))

    def _respond(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/" and request.method == "GET":
            return httpx.Response(200, text=self.html, headers={"content-type": "text/html; charset=utf-8"})
        if request.url.path == "/graphql/query" and request.method == "POST":
            if self.graphql_status == 401:
                return httpx.Response(401, text="", headers={"content-type": "text/html"})
            variables = json.loads(dict(parse_qsl(request.content.decode()))["variables"])
            after = variables.get("after")
            index = 1 if after is None else int(str(after).rsplit("_", 1)[1]) + 1
            event = self.on_listing_page.get(index)
            if event is not None and threading.current_thread().name == "listing":
                event.set()
            return httpx.Response(200, text=self.pages[index], headers={"content-type": "application/json"})
        return httpx.Response(404, text="{}", headers={"content-type": "application/json"})


def downloader(mock: GatedInstagram, gate: InstagramRequestGate | None, **extra) -> InstagramWebDownloader:
    return InstagramWebDownloader(FakeProvider(), transport=mock.transport, page_delay=(0, 0), request_gate=gate,
                                  max_pages=PAGES + 5, **extra)


def _run(name: str, target, results: dict) -> threading.Thread:
    def body() -> None:
        try:
            results[name] = target()
        except BaseException as exc:  # noqa: BLE001 - o teste inspeciona
            results[name] = exc
        results[name + "_end"] = time.monotonic()

    thread = threading.Thread(target=body, name=name, daemon=True)
    thread.start()
    return thread


def test_long_listing_interleaved_with_concurrent_renewals_one_in_flight_without_deadlock():
    mock = GatedInstagram()
    gate = InstagramRequestGate(interval=(INTERVAL, INTERVAL))
    page_5 = threading.Event()
    mock.on_listing_page[5] = page_5
    results: dict = {}

    listing = _run("listing", lambda: len(list(downloader(mock, gate).iter_profile_media(
        "perfil_fake", "all", None))), results)
    assert page_5.wait(JOIN_TIMEOUT), "a listagem não chegou à página 5"

    def renew(i: int):
        def go():
            pages = []
            for j in range(2):   # cada renovação é uma operação nova (bootstrap + 1 POST)
                ref = ListingRef(page_index=10 + i + j, cursor=f"FAKE_CURSOR_{9 + i + j}", page_size=12)
                pages.append(len(downloader(mock, gate).refresh_page("perfil_fake", ref)))
            return pages
        return go

    renewals = [_run(f"renew{i}", renew(i), results) for i in range(3)]
    for thread in [listing, *renewals]:
        thread.join(JOIN_TIMEOUT)
        assert not thread.is_alive(), f"deadlock: {thread.name} não terminou"

    assert results["listing"] == PAGES * NODES_PER_PAGE, results["listing"]
    for i in range(3):
        assert results[f"renew{i}"] == [NODES_PER_PAGE, NODES_PER_PAGE], results[f"renew{i}"]
    assert mock.max_in_flight == 1, "no máximo 1 requisição ao Instagram em voo"
    spans = sorted(mock.spans)
    assert len(spans) == (1 + PAGES) + 3 * 2 * 2
    gaps = [b[0] - a[1] for a, b in zip(spans, spans[1:], strict=False)]
    assert min(gaps) >= INTERVAL - 0.002, f"intervalo mínimo desrespeitado: {min(gaps):.4f}s"
    # As renovações passam ENTRE as páginas: terminam antes do fim da listagem e há páginas depois delas.
    listing_posts = [s for s in spans if s[3] == "listing" and s[2] == "/graphql/query"]
    last_renewal = max(s[1] for s in spans if s[3].startswith("renew"))
    assert last_renewal < listing_posts[-1][0], "renovações ficaram bloqueadas até o fim da listagem"
    assert all(results[f"renew{i}_end"] < results["listing_end"] for i in range(3))


def test_cdn_download_does_not_take_the_gate():
    gate = InstagramRequestGate(interval=(0, 0))
    calls = []

    def cdn(url, dest, **kwargs):
        calls.append(url)
        return type("R", (), {"path": str(Path(dest) / "x.jpg")})()

    held = threading.Event()
    release = threading.Event()

    def holder():
        with gate.slot():
            held.set()
            release.wait(10)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    held.wait(5)
    try:
        dl = InstagramWebDownloader(FakeProvider(), cdn_downloader=cdn, request_gate=gate)
        item = type("I", (), {"media_url": "https://scontent-fake.cdninstagram.com/x.jpg"})()
        done = []
        worker = threading.Thread(target=lambda: done.append(dl.download(item, Path("."))), daemon=True)
        worker.start()
        worker.join(5)
        assert done and calls, "download de CDN esperou o portão"
    finally:
        release.set()
        t.join(5)


@pytest.mark.parametrize("operation", ["listing", "refresh"])
def test_cancel_while_waiting_for_the_gate(operation):
    mock = GatedInstagram()
    gate = InstagramRequestGate(interval=(0, 0))
    held = threading.Event()
    release = threading.Event()

    def holder():
        with gate.slot():
            held.set()
            release.wait(10)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert held.wait(5)
    cancel = threading.Event()
    results: dict = {}
    dl = downloader(mock, gate)
    if operation == "listing":
        target = lambda: list(dl.iter_profile_media("perfil_fake", "all", None, cancel=cancel))  # noqa: E731
    else:
        ref = ListingRef(page_index=2, cursor="FAKE_CURSOR_1", page_size=12)
        target = lambda: dl.refresh_page("perfil_fake", ref, cancel=cancel)  # noqa: E731
    waiter = _run("waiter", target, results)
    time.sleep(0.2)
    assert waiter.is_alive(), "deveria estar esperando a vez no portão"
    cancel.set()
    waiter.join(5)
    try:
        assert not waiter.is_alive(), "o cancelamento não interrompeu a espera pelo portão"
        assert isinstance(results["waiter"], GateCancelled) and results["waiter"].code == "cancelled"
        assert mock.spans == [], "nenhuma requisição ao Instagram depois do cancelamento"
    finally:
        release.set()
        t.join(5)


def test_401_renews_once_then_request_auth_failed_through_the_gate():
    mock = GatedInstagram(graphql_status=401)
    gate = InstagramRequestGate(interval=(0, 0))
    with pytest.raises(InstagramError) as info:
        list(downloader(mock, gate).iter_profile_media("perfil_fake", "all", None))
    assert info.value.code == "request_auth_failed" and info.value.retryable is True
    assert [s[2] for s in sorted(mock.spans)] == ["/", "/graphql/query", "/", "/graphql/query"]
    assert mock.max_in_flight == 1


# --------------------------------------------------------------------------- gallery-dl: 1 operação = 1 vaga


def _gallery(gate, tmp_path) -> GalleryDlInstagramDownloader:
    return GalleryDlInstagramDownloader(FakeProvider(), temp_dir=tmp_path, request_gate=gate)


def test_gallerydl_operation_takes_one_slot_and_cancel_while_waiting(tmp_path, monkeypatch):
    gate = InstagramRequestGate(interval=(0, 0))
    dl = _gallery(gate, tmp_path)
    seen = []

    def fake_locked(session, request, timeout, cancel):
        # Dentro da operação o portão está ocupado: outra vaga não é obtida.
        seen.append(gate._lock.locked())
        yield {"type": "item"}

    monkeypatch.setattr(dl, "_stream_locked", fake_locked)
    assert list(dl._run({"op": "list"})) == [{"type": "item"}]
    assert seen == [True] and not gate._lock.locked()

    held = threading.Event()
    release = threading.Event()

    def holder():
        with gate.slot():
            held.set()
            release.wait(10)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert held.wait(5)
    cancel = threading.Event()
    results: dict = {}
    waiter = _run("waiter", lambda: list(dl._run({"op": "list"}, cancel=cancel)), results)
    time.sleep(0.2)
    assert waiter.is_alive()
    cancel.set()
    waiter.join(5)
    try:
        assert not waiter.is_alive() and results["waiter"] == []
        assert seen == [True], "o runner não pode iniciar depois do cancelamento"
    finally:
        release.set()
        t.join(5)


def test_gallerydl_session_test_maps_request_auth_failed(tmp_path, monkeypatch):
    dl = _gallery(None, tmp_path)
    event = {"type": "session_test", "result": "instagram_error", "code": "request_auth_failed",
             "message": "recusado", "http_status": 401}
    monkeypatch.setattr(dl, "_run", lambda request, timeout=None, cancel=None: iter([event]))
    result = dl.test_session()
    assert (result.ok, result.result, result.code) == (False, "instagram_error", "request_auth_failed")
