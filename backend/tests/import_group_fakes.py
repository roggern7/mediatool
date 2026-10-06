"""Fakes dos testes de "Todos", filtro por tipo e grupos de importação (100% offline; só valores FAKE).

- ``MultiProfileSite``: Instagram Web falso (httpx.MockTransport) com VÁRIOS perfis; as páginas são geradas na hora a
  partir do cursor (nada guardado), mede requisições em voo e a ordem dos perfis pedidos, e aceita anomalias de
  paginação por página.
- ``FakeCdn``: "baixa" escrevendo bytes sintéticos; mede downloads simultâneos.
- ``ProfilesDownloader``: provider falso por perfil (itens, erro, erro depois de N itens), para os testes de fila do
  grupo que não dependem do GraphQL.
"""

from __future__ import annotations

import json
import threading
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx

from app.integrations.instagram.session import InstagramSession
from app.integrations.instagram.web.provider import InstagramWebDownloader
from app.services.cdn_downloader import DownloadResult
from media_fakes import FAKE_SESSION, MAGIC, FakeInstagramDownloader, remote

FIXTURES = Path(__file__).parent / "fixtures" / "instagram_web"
ROOT = "xdt_api__v1__feed__user_timeline_graphql_connection"
CDN = "https://scontent-fake.cdninstagram.com/v/t51.fake"
CURSOR = "FAKE_CUR_"


class CookieSession:
    def get(self):
        return InstagramSession({"sessionid": FAKE_SESSION})


def _image(code: str, pk: str) -> dict:
    return {"code": code, "pk": pk, "media_type": 1, "product_type": "feed", "taken_at": 1700000000,
            "image_versions2": {"candidates": [{"url": f"{CDN}/{code}.jpg", "width": 640, "height": 640}]}}


def _reel(code: str, pk: str) -> dict:
    return {"code": code, "pk": pk, "media_type": 2, "product_type": "clips", "taken_at": 1700000000,
            "video_versions": [{"url": f"{CDN}/{code}.mp4", "width": 720, "height": 1280}],
            "image_versions2": {"candidates": [{"url": f"{CDN}/{code}-t.jpg", "width": 64, "height": 64}]}}


def _feed_video(code: str, pk: str) -> dict:
    node = _reel(code, pk)
    node["product_type"] = "feed"   # vídeo avulso que NÃO é Reel
    return node


def _carousel(code: str, pk: str) -> dict:
    """Carrossel de 3 itens: foto, VÍDEO, foto."""
    children = [
        {"pk": f"{pk}1", "media_type": 1,
         "image_versions2": {"candidates": [{"url": f"{CDN}/{code}-0.jpg", "width": 640, "height": 640}]}},
        {"pk": f"{pk}2", "media_type": 2,
         "video_versions": [{"url": f"{CDN}/{code}-1.mp4", "width": 720, "height": 1280}],
         "image_versions2": {"candidates": [{"url": f"{CDN}/{code}-1t.jpg", "width": 64, "height": 64}]}},
        {"pk": f"{pk}3", "media_type": 1,
         "image_versions2": {"candidates": [{"url": f"{CDN}/{code}-2.jpg", "width": 640, "height": 640}]}},
    ]
    return {"code": code, "pk": pk, "media_type": 8, "product_type": "carousel_container", "taken_at": 1700000000,
            "carousel_media": children, "carousel_media_count": 3}


def _no_url(code: str, pk: str) -> dict:
    node = _image(code, pk)
    node["image_versions2"] = {"candidates": [{"url": None, "width": 640, "height": 640}]}   # descarte inesperado
    return node


KINDS = {"image": _image, "reel": _reel, "carousel": _carousel, "feed_video": _feed_video, "no_url": _no_url}


@dataclass
class Profile:
    """Perfil falso: ``pages`` páginas de ``per_page`` posts; ``kind(page, i)`` diz o tipo de cada post.
    ``breaks[page]``: anomalia da paginação naquela página (todas com has_next_page=true):
    "empty" (sem edges), "no_cursor", "no_cursor_errors", "repeat_cursor", "dup" (só nós já vistos, da página 1)."""

    pages: int
    kind: Callable[[int, int], str] = lambda page, i: "image"
    per_page: int = 12
    breaks: dict[int, str] = field(default_factory=dict)
    code_prefix: str | None = None   # perfis com o mesmo prefixo "republicam" as mesmas mídias


class MultiProfileSite:
    def __init__(self, profiles: dict[str, Profile], *, delay: float = 0.0, record: bool = True) -> None:
        self.profiles = profiles
        self.delay = delay
        self.record = record
        self.gets = 0
        self.posts = 0
        self.posts_by_profile: dict[str, int] = {}
        self.order: list[tuple[str, int]] = []      # (perfil, página) de cada POST, em ordem
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.on_post: Callable[[str, int], None] | None = None
        self.bootstrap_html = (FIXTURES / "bootstrap_input.html").read_text(encoding="utf-8")
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                time.sleep(self.delay)
            return self._respond(request)
        finally:
            with self.lock:
                self.in_flight -= 1

    def _respond(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/":
            self.gets += 1
            return httpx.Response(200, text=self.bootstrap_html, headers={"content-type": "text/html; charset=utf-8"})
        if request.method == "POST" and request.url.path == "/graphql/query":
            variables = json.loads(dict(parse_qsl(request.content.decode()))["variables"])
            username = variables["username"]
            after = variables.get("after")
            page = 1 if after is None else int(after[len(CURSOR):]) + 1
            with self.lock:
                self.posts += 1
                self.posts_by_profile[username] = self.posts_by_profile.get(username, 0) + 1
                if self.record:
                    self.order.append((username, page))
            if self.on_post is not None:
                self.on_post(username, page)
            body = self.page_json(username, page, variables["data"]["count"])
            return httpx.Response(200, text=body, headers={"content-type": "application/json"})
        return httpx.Response(404, text="{}", headers={"content-type": "application/json"})

    def nodes(self, username: str, page: int, count: int) -> list[dict]:
        profile = self.profiles[username]
        prefix = profile.code_prefix or "".join(ch for ch in username.upper() if ch.isalnum())
        serial = zlib.crc32(prefix.encode()) % 900000 + 100000
        nodes = []
        if page <= profile.pages:
            for i in range(min(count, profile.per_page)):
                code, pk = f"{prefix}P{page:05d}N{i:02d}", f"{serial}{page:06d}{i:02d}"
                nodes.append(KINDS[profile.kind(page, i)](code, pk))
        return nodes

    def page_json(self, username: str, page: int, count: int) -> str:
        profile = self.profiles[username]
        anomaly = profile.breaks.get(page)
        has_next: object = page < profile.pages
        cursor: str | None = f"{CURSOR}{page}" if has_next else None
        nodes = self.nodes(username, page, count)
        payload: dict = {}
        if anomaly == "empty":
            nodes, has_next = [], True
        elif anomaly in ("no_cursor", "no_cursor_errors"):
            has_next, cursor = True, None
            if anomaly == "no_cursor_errors":
                payload["errors"] = [{"message": "partial FAKE"}]
        elif anomaly == "repeat_cursor":
            has_next, cursor = True, f"{CURSOR}{page - 1}"
        elif anomaly == "dup":
            nodes, has_next = self.nodes(username, 1, count), True
        payload["data"] = {ROOT: {"edges": [{"node": node} for node in nodes],
                                  "page_info": {"end_cursor": cursor, "has_next_page": has_next}}}
        return json.dumps(payload)


class FakeCdn:
    """CDN falso: escreve bytes sintéticos distintos por URL; mede downloads simultâneos."""

    def __init__(self, *, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls: list[str] = []
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0

    def __call__(self, url, dest_dir, *, max_bytes, cancel=None, progress=None, **_):
        name = Path(urlsplit(url).path).name
        with self.lock:
            self.calls.append(name)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                time.sleep(self.delay)
            video = name.endswith(".mp4")
            data = MAGIC["video" if video else "image"] + f"FAKE-{'video' if video else 'image'}-{name}".encode()
            dest = Path(dest_dir)
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / f"cdn-{name}"
            target.write_bytes(data)
            if progress:
                progress(len(data), len(data))
            return DownloadResult(path=target, size=len(data), content_type="x", suffix=target.suffix)
        finally:
            with self.lock:
                self.in_flight -= 1


def web_app(harness_factory, site: MultiProfileSite, *, cdn: FakeCdn | None = None, env: dict | None = None,
            gate=None, root=None, **kwargs):
    """App de teste com o ``InstagramWebDownloader`` REAL sobre o site falso e o portão do app (ou ``gate``)."""
    if env is not None:
        kwargs.setdefault("env", dict(env))   # a mesma configuração no app (reconciliação do teto na inicialização)
    h = harness_factory(root=root, **kwargs)
    if gate is not None:
        h.ctx.instagram_gate = gate
        gate.listing_wait_observer = h.ctx.import_runner._on_rate_limit_wait
    h.cdn = cdn or FakeCdn()
    h.downloader = InstagramWebDownloader(
        CookieSession(), cdn_downloader=h.cdn, transport=site.transport, page_delay=(0, 0),
        request_gate=h.ctx.instagram_gate, env=env)
    return h


def drive(h, done: Callable[[], bool], *, rounds: int = 3000, per_round: int = 500) -> None:
    """Alterna listagem e downloads na thread do teste até ``done()``."""
    for _ in range(rounds):
        h.run_imports()
        h.ctx.worker.run_pending(max_jobs=per_round)
        if done():
            return
    raise AssertionError("o lote/grupo não chegou ao estado esperado")


def closed(h, *batch_ids: int) -> Callable[[], bool]:
    return lambda: all(h.ctx.batches.get(b)["status"] in ("completed", "failed", "cancelled") for b in batch_ids)


def items_for(profile: str, count: int) -> list:
    prefix = "".join(ch for ch in profile.upper() if ch.isalnum())
    return [remote(f"{prefix}{i:04d}", profile=profile) for i in range(count)]


@dataclass
class ProfilesDownloader(FakeInstagramDownloader):
    """Um perfil falso por nome de usuário: ``profiles[nome]`` = itens; ``errors[nome]`` = erro da listagem
    (``error_after[nome]`` = depois de N itens; sem isso, o erro vem antes de qualquer item)."""

    profiles: dict[str, list] = field(default_factory=dict)
    errors: dict[str, Exception] = field(default_factory=dict)
    error_after: dict[str, int] = field(default_factory=dict)
    limits: list[tuple[str, int | None]] = field(default_factory=list)   # (perfil, limite) de cada trecho
    order: list[str] = field(default_factory=list)                       # perfil de cada trecho, em ordem
    listing_now: int = 0
    max_listing: int = 0

    def iter_profile_media(self, username, content, limit, **kwargs):
        self.order.append(username)
        self.limits.append((username, limit))
        error = self.errors.get(username)
        if error is not None and username not in self.error_after:
            raise error
        self.items = self.profiles.get(username, [])
        self.listing_error = error
        self.listing_error_after = self.error_after.get(username)
        self.listing_now += 1
        self.max_listing = max(self.max_listing, self.listing_now)
        try:
            yield from super().iter_profile_media(username, content, limit, **kwargs)
        finally:
            self.listing_now -= 1
