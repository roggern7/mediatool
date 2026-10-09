"""Auditoria da importação por link (QA): pilha REAL do Instagram web (provider -> GraphQL -> normalize_post ->
cdn_download) com transportes simulados e mídias sintéticas reais (JPEG/MP4 do ffmpeg). Nenhuma rede: o cliente HTTP
do GraphQL é um roteiro em memória, o cliente HTTP do CDN é um ``httpx.MockTransport`` e conexões não-loopback são
bloqueadas. Só valores FAKE."""

from __future__ import annotations

import json
import socket
import subprocess
import threading
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from media_processor import sha256_file

from app.integrations.instagram.web.graphql import InstagramWebGraphQL
from app.integrations.instagram.web.provider import InstagramWebDownloader
from app.services import cdn_downloader
from media_fakes import FakeInstagramDownloader
from test_web_graphql import FakeBootstrap as GqlBootstrap
from test_web_graphql import resp
from test_web_provider import FakeProvider

FIXTURES = Path(__file__).parent / "fixtures" / "instagram_web" / "graphql"
POST_ROOT = "xdt_shortcode_media"
OWNER = "dono_fake"

_KINDS = {  # tipo -> (fixture, shortcode da fixture)
    "image": ("post_image.json", "FAKEPOSTIMG1"),
    "reel": ("post_video_reel.json", "FAKEPOSTREEL"),
    "carousel": ("post_carousel_mixed.json", "FAKEPOSTCAR1"),
    "v1": ("post_v1_items.json", "FAKEPOSTV1A1"),
}


def make_root(kind: str, code: str, owner: str = "Dono_Fake") -> dict:
    """Raiz de publicação a partir das fixtures, com shortcode, dono e URLs de mídia ÚNICOS (o código vai no caminho
    da URL, para dois posts nunca compartilharem arquivo)."""
    if kind == "video":   # vídeo que não é Reel
        root = make_root("reel", code, owner)
        root["product_type"] = "feed"
        return root
    name, original = _KINDS[kind]
    text = (FIXTURES / name).read_text(encoding="utf-8")
    text = text.replace(original, code).replace("Dono_Fake", owner)
    text = text.replace("/v/t51.fake/", f"/v/t51.fake/{code}/").replace("/v/t50.fake/", f"/v/t50.fake/{code}/")
    return json.loads(text)[ "data"][POST_ROOT]


def payload(root, *, field: str = POST_ROOT) -> str:
    return json.dumps({"data": {field: root}})


# --------------------------------------------------------------------------- mídias sintéticas


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, timeout=120)


@pytest.fixture(scope="session")
def base_media(tmp_path_factory) -> dict[str, bytes]:
    """Um JPEG e um MP4 (com áudio) reais e pequenos, gerados uma vez por sessão."""
    directory = tmp_path_factory.mktemp("audit_samples")
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=160x120", "-frames:v", "1", str(directory / "a.jpg"))
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(directory / "a.mp4"))
    return {"image": (directory / "a.jpg").read_bytes(), "video": (directory / "a.mp4").read_bytes()}


def variant(base: dict[str, bytes], kind: str, tag: str) -> bytes:
    """Cópia válida do arquivo base com sha256 próprio: JPEG ganha bytes após o EOI; MP4 ganha um átomo ``free``."""
    data = base[kind]
    blob = tag.encode()
    if kind == "image":
        return data + b"\x00" + blob
    size = 8 + len(blob)
    return data + size.to_bytes(4, "big") + b"free" + blob


# --------------------------------------------------------------------------- CDN simulado


class CdnSim:
    """Servidor de CDN em memória (``httpx.MockTransport`` sob o ``cdn_download`` REAL).

    Por padrão serve ``variant(base, tipo, caminho-da-URL)``. ``rules`` = lista de (trecho-da-URL, ação) avaliada na
    ordem: ação ``int`` = status HTTP; ``bytes`` = corpo; ``callable(request)`` = ``httpx.Response``. ``expired`` =
    trechos de URL que respondem 403 (URL temporária vencida)."""

    def __init__(self, base: dict[str, bytes]):
        self.base = base
        self.rules: list[tuple[str, object]] = []
        self.expired: set[str] = set()
        self.hits: list[str] = []
        self.on_hit: Callable[[str], None] | None = None
        self.bodies: dict[str, bytes] = {}   # URL sem query -> bytes servidos (para conferir sha256 depois)
        self._lock = threading.Lock()

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        with self._lock:
            self.hits.append(url)
        if self.on_hit is not None:
            self.on_hit(url)
        if any(token in url for token in self.expired):
            return httpx.Response(403)
        for token, action in self.rules:
            if token in url:
                if isinstance(action, int):
                    return httpx.Response(action)
                if isinstance(action, bytes):
                    return httpx.Response(200, content=action)
                return action(request)
        kind = "video" if request.url.path.endswith(".mp4") else "image"
        body = variant(self.base, kind, request.url.path)
        self.bodies[url.split("?")[0]] = body
        return httpx.Response(200, content=body)

    def hit_paths(self) -> list[str]:
        return [h.split("?")[0] for h in self.hits]


# --------------------------------------------------------------------------- GraphQL simulado


class ScriptedHttp:
    """Cliente HTTP do GraphQL em memória: responde a consulta de post pelo shortcode das ``variables``.

    ``roots[shortcode]`` = raiz (dict), ``None`` (raiz nula) ou ``str`` (corpo cru). ``script[shortcode]`` = fila de
    respostas/erros consumida antes (``WebResponse`` | ``Exception`` | callable(shortcode)). A partir da 2a consulta
    do mesmo shortcode as URLs de mídia trocam ``FAKE_OH`` por ``FRESH<n>`` (renovação)."""

    def __init__(self):
        self.roots: dict[str, object] = {}
        self.script: dict[str, list] = {}
        self.calls: list[str] = []
        self.counts: Counter[str] = Counter()
        self.in_flight = 0
        self.max_in_flight = 0
        self.on_call: Callable[[str], None] | None = None
        self.answer_with: dict[str, str] = {}   # shortcode pedido -> shortcode da raiz a devolver (resposta trocada)
        self._lock = threading.Lock()

    def csrftoken(self):
        return "FAKE_CSRF_TOKEN_0001"

    def account_id(self):
        return None

    def close(self):
        pass

    def post_form(self, path, form, headers):
        shortcode = json.loads(form["variables"])["shortcode"]
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.calls.append(shortcode)
            self.counts[shortcode] += 1
            n = self.counts[shortcode]
        try:
            if self.on_call is not None:
                self.on_call(shortcode)
            queue = self.script.get(shortcode)
            if queue:
                item = queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item(shortcode) if callable(item) else item
            key = self.answer_with.get(shortcode, shortcode)
            root = self.roots.get(key)
            if isinstance(root, str):
                return resp(200, root)
            text = payload(root)
            if n > 1:
                text = text.replace("FAKE_OH", f"FRESH{n}")
            return resp(200, text)
        finally:
            with self._lock:
                self.in_flight -= 1


class HybridWebDownloader(InstagramWebDownloader):
    """Downloader web REAL para publicação (``resolve_post``) e download (``cdn_download``); a listagem de perfil é
    delegada a um ``FakeInstagramDownloader`` (``profile_fake``), para misturar lotes por perfil e por link."""

    profile_fake: FakeInstagramDownloader

    def get_profile(self, username):
        return self.profile_fake.get_profile(username)

    def iter_profile_media(self, username, content, limit, **kwargs):
        return self.profile_fake.iter_profile_media(username, content, limit, **kwargs)

    def refresh_page(self, profile, ref, *, cancel=None):
        return self.profile_fake.refresh_page(profile, ref, cancel=cancel)


class WebSim:
    """Downloader real (``InstagramWebDownloader``) sobre ``ScriptedHttp`` + ``CdnSim``."""

    def __init__(self, base: dict[str, bytes], *, max_download_bytes: int = 50 * 1024 * 1024):
        self.http = ScriptedHttp()
        self.cdn = CdnSim(base)
        self.bootstrap = GqlBootstrap()
        self.downloader = HybridWebDownloader(
            FakeProvider(True), max_download_bytes=max_download_bytes, http_factory=lambda session: self.http,
            bootstrap_factory=lambda http: self.bootstrap,
            graphql_factory=lambda http, bootstrap: InstagramWebGraphQL(self.http, bootstrap, page_delay=(0, 0)),
        )
        self.profile = FakeInstagramDownloader(refreshable=True)
        self.downloader.profile_fake = self.profile

    def add(self, kind: str, code: str, owner: str = "Dono_Fake") -> dict:
        root = make_root(kind, code, owner)
        self.http.roots[code] = root
        return root

    @property
    def calls(self) -> list[str]:
        return self.http.calls


@pytest.fixture
def audit_env(monkeypatch):
    """Sem rede: CDN por MockTransport, DNS sem consulta e conexões não-loopback proibidas."""
    holder: dict[str, CdnSim] = {}

    def install(sim: CdnSim) -> None:
        holder["sim"] = sim
        monkeypatch.setattr(
            cdn_downloader, "_build_client",
            lambda timeout: httpx.Client(transport=httpx.MockTransport(sim.handler), follow_redirects=False,
                                         timeout=httpx.Timeout(timeout), trust_env=False))
        monkeypatch.setattr(cdn_downloader, "_check_dns", lambda host: None)

    real_connect = socket.socket.connect

    def guarded(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"Teste tentou abrir conexão de rede: {host}")
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    return install


def make_app(harness_factory, base, audit_env, *, processor=None, max_download_bytes: int = 50 * 1024 * 1024,
             root: Path | None = None, sim: WebSim | None = None, **kwargs):
    """Harness com o downloader web real. Devolve (harness, sim). ``processor=None`` = FakeProcessor padrão do
    Harness (os testes com download puro não chamam o processor)."""
    sim = sim or WebSim(base, max_download_bytes=max_download_bytes)
    audit_env(sim.cdn)
    extra = {"processor": processor} if processor is not None else {}
    h = harness_factory(root, downloader=sim.downloader, **extra, **kwargs)
    return h, sim


# --------------------------------------------------------------------------- invariantes


def check_invariants(h, *, expect_original_files: bool = True) -> None:
    """Invariantes de integridade do banco e do storage (item 9 da auditoria)."""
    db = h.ctx.db
    assert db.fetchall("PRAGMA foreign_key_check") == []
    assert db.scalar("PRAGMA integrity_check") == "ok"
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE batch_id IS NULL AND kind = 'ingest'") == 0
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE batch_id IS NOT NULL AND batch_id NOT IN (SELECT id FROM batches)") == 0
    storage = h.ctx.storage
    referenced = set()
    for media in db.fetchall("SELECT * FROM media"):
        path = storage.resolve(media["original_path"])
        assert path.is_file(), media["original_path"]
        assert sha256_file(path) == media["original_sha256"]
        referenced.add(path.resolve())
    originals = Path(h.settings.storage_dir) / "originals"
    on_disk = {p.resolve() for p in originals.rglob("*") if p.is_file()} if originals.exists() else set()
    assert on_disk == referenced, f"arquivos em originals sem linha em media (ou o inverso): {on_disk ^ referenced}"
    # media duplicada por origem não pode existir
    dup = db.fetchall("SELECT source_post_id, source_media_index, COUNT(*) c FROM media WHERE source_post_id IS NOT NULL"
                      " GROUP BY 1, 2 HAVING c > 1")
    assert dup == []
    for batch in db.fetchall("SELECT id, import_mode FROM batches"):
        jobs = db.scalar("SELECT COUNT(*) FROM jobs WHERE batch_id = ?", (batch["id"],))
        body = h.client.get(f"/api/batches/{batch['id']}").json()
        progress = body["progress"]
        if batch["import_mode"] == "links":
            resolved = db.scalar("SELECT COALESCE(SUM(items_found), 0) FROM batch_links WHERE batch_id = ?",
                                 (batch["id"],))
            assert resolved == jobs, f"lote {batch['id']}: soma de items_found {resolved} != jobs {jobs}"
        assert progress["found"] == jobs
        counts = body.get("counts")
        if isinstance(counts, dict):
            assert sum(int(v) for v in counts.values()) == jobs, f"lote {batch['id']}: contadores {counts} != {jobs} jobs"


def storage_leftovers(h) -> list[Path]:
    temp = h.ctx.storage.temp
    return [p for p in temp.rglob("*") if p.is_file()] if temp.exists() else []
