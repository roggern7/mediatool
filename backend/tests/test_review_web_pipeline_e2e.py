"""Revisão independente: pipeline download -> processamento com o provider Instagram Web REAL, 100% offline.

O app é montado por `create_app` (via Harness) com `InstagramWebDownloader` real sobre `httpx.MockTransport`
(bootstrap das fixtures + GraphQL sintético) e um `cdn_download` fake que grava mídia REAL gerada com ffmpeg.
Nenhuma requisição de rede; nenhum acesso a data/secrets; nenhuma porta aberta.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.parse
from pathlib import Path

import httpx
import pytest
from media_processor import MediaProcessor, sha256_file

from app.integrations.instagram.session import InstagramSession, InstagramSessionProvider, SessionStatus
from app.integrations.instagram.web.provider import InstagramWebDownloader
from app.services.cdn_downloader import DownloadError, DownloadResult, validate_cdn_url
from conftest import Harness

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg/ffprobe ausentes"
)

FIXTURES = Path(__file__).parent / "fixtures" / "instagram_web"
CDN_HOST = "https://scontent-fake.cdninstagram.com/v/t51.fake/"
PROFILE = "perfil_teste"
# Ordem da listagem: página 1 = [imagem, reel]; página 2 = [carrossel misto com 3 filhos].
EXPECTED_ORDER = [("IMGPOST001", 0), ("REELPOST01", 0), ("CAROUSEL01", 0), ("CAROUSEL01", 1), ("CAROUSEL01", 2)]
KEY_BY_ITEM = {
    ("IMGPOST001", 0): "img", ("REELPOST01", 0): "reel",
    ("CAROUSEL01", 0): "car0", ("CAROUSEL01", 1): "car1", ("CAROUSEL01", 2): "car2",
}
KIND = {"img": "image", "reel": "video", "car0": "image", "car1": "video", "car2": "image"}


# --------------------------------------------------------------------------------------------- mídia real (ffmpeg)


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


@pytest.fixture(scope="module")
def real_media(tmp_path_factory) -> dict[str, Path]:
    base = tmp_path_factory.mktemp("real_media")
    files: dict[str, Path] = {}
    for key, color in (("img", "red"), ("car0", "green"), ("car2", "blue")):
        out = base / f"{key}.jpg"
        _ffmpeg("-f", "lavfi", "-i", f"color=c={color}:s=320x240:d=1", "-frames:v", "1", str(out))
        files[key] = out
    for key, freq, pattern in (("reel", 440, "testsrc"), ("car1", 880, "testsrc2")):
        out = base / f"{key}.mp4"
        _ffmpeg("-f", "lavfi", "-i", f"{pattern}=duration=3:size=320x240:rate=25",
                "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=3",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out))
        files[key] = out
    return files


def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                          str(path)], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


# --------------------------------------------------------------------------------------------- servidor Instagram falso


def _node(code: str, pk: str, key: str, gen: int) -> dict:
    def url(name: str, ext: str) -> str:
        return f"{CDN_HOST}{name}.{ext}?oh=FAKE&oe=g{gen}"

    def media(k: str, mt: int, child_pk: str | None = None) -> dict:
        data: dict = {"media_type": mt, "pk": child_pk or pk, "id": f"{child_pk or pk}_9",
                      "image_versions2": {"candidates": [
                          {"url": url(f"{k}_small", "jpg"), "width": 100, "height": 100},
                          {"url": url(k, "jpg"), "width": 320, "height": 240}]}}
        if mt == 2:
            data["video_versions"] = [{"url": url(f"{k}_low", "mp4"), "width": 160, "height": 120},
                                      {"url": url(k, "mp4"), "width": 320, "height": 240}]
            data["video_duration"] = 3.0
        return data

    node = {"code": code, "pk": pk, "id": f"{pk}_9", "taken_at": 1700000000, "carousel_media": None,
            "product_type": "feed"}
    if key == "img":
        node.update(media("img", 1))
    elif key == "reel":
        node.update(media("reel", 2))
        node["product_type"] = "clips"
    else:
        node.update({"media_type": 8, "carousel_media_count": 3, "carousel_media": [
            media("car0", 1, pk + "01"), media("car1", 2, pk + "02"), media("car2", 1, pk + "03")]})
    return node


class FakeInstagram:
    """Atende www.instagram.com: GET / (bootstrap) e POST /graphql/query (PolarisProfilePostsQuery)."""

    def __init__(self) -> None:
        self.generation = 1
        self.gets: list[str] = []
        self.posts: list[dict] = []          # variáveis de cada POST /graphql/query
        self.form_checks: list[dict] = []
        self.unexpected: list[str] = []
        self.graphql_status: int | None = None   # força um status HTTP no GraphQL (ex.: 429)
        self.graphql_body: str | None = None     # força corpo do GraphQL
        self.bootstrap_html = (FIXTURES / "bootstrap_define.html").read_text(encoding="utf-8")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "www.instagram.com":
            self.unexpected.append(str(request.url.host))
            return httpx.Response(500)
        if request.method == "GET" and request.url.path == "/":
            self.gets.append("/")
            return httpx.Response(200, text=self.bootstrap_html, headers={"content-type": "text/html; charset=utf-8"})
        if request.method == "POST" and request.url.path == "/graphql/query":
            form = {k: v[0] for k, v in urllib.parse.parse_qs(request.content.decode()).items()}
            variables = json.loads(form["variables"])
            self.posts.append(variables)
            self.form_checks.append({
                "doc_id": form.get("doc_id"), "name": form.get("fb_api_req_friendly_name"),
                "fb_dtsg": form.get("fb_dtsg"), "lsd": form.get("lsd"), "header_lsd": request.headers.get("x-fb-lsd"),
                "cookie": request.headers.get("cookie", ""),
            })
            if self.graphql_status is not None:
                return httpx.Response(self.graphql_status, text="{}", headers={"content-type": "application/json"})
            if self.graphql_body is not None:
                return httpx.Response(200, text=self.graphql_body, headers={"content-type": "application/json"})
            return httpx.Response(200, json=self._page(variables), headers={"content-type": "application/json"})
        self.unexpected.append(f"{request.method} {request.url.path}")
        return httpx.Response(404)

    def _page(self, variables: dict) -> dict:
        g = self.generation
        if variables.get("after") is None:
            edges = [{"node": _node("IMGPOST001", "3100000000000000001", "img", g), "cursor": None},
                     {"node": _node("REELPOST01", "3100000000000000002", "reel", g), "cursor": None}]
            page_info = {"has_next_page": True, "end_cursor": "CURSOR_PAGE_2"}
        else:
            assert variables["after"] == "CURSOR_PAGE_2"
            edges = [{"node": _node("CAROUSEL01", "3100000000000000003", "carousel", g), "cursor": None}]
            page_info = {"has_next_page": False, "end_cursor": None}
        return {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {"edges": edges,
                                                                                    "page_info": page_info}}}


class FakeCdn:
    """cdn_download injetado: valida a URL como o real (sem DNS) e grava a mídia real do `real_media`."""

    def __init__(self, files: dict[str, Path], server: FakeInstagram) -> None:
        self.files = files
        self.server = server
        self.calls: list[str] = []
        self.expire: dict[str, int] = {}      # chave -> geração a partir da qual a URL é aceita
        self.always_forbidden: set[str] = set()
        self.status: dict[str, int] = {}      # chave -> HTTP fixo (ex.: 404)

    def __call__(self, url, dest_dir, *, max_bytes, cancel=None, progress=None, **_):
        validate_cdn_url(url)
        parts = urllib.parse.urlsplit(url)
        name = Path(parts.path).stem
        key = name
        self.calls.append(f"{key}:{urllib.parse.parse_qs(parts.query).get('oe', [''])[0]}")
        oe = int(urllib.parse.parse_qs(parts.query)["oe"][0][1:])
        if key in self.always_forbidden or (key in self.expire and oe < self.expire[key]):
            raise DownloadError("forbidden", "CDN recusou o acesso (HTTP 403).")
        if key not in self.files:
            raise DownloadError("not_found", "mídia inexistente")
        src = self.files[key]
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / f"cdn-{key}{src.suffix}"
        shutil.copyfile(src, target)
        size = target.stat().st_size
        if progress:
            progress(size, size)
        return DownloadResult(path=target, size=size, content_type="x", suffix=src.suffix)


class SessionWithCookie(InstagramSessionProvider):
    def status(self) -> SessionStatus:
        return SessionStatus(configured=True, source="file")

    def get(self):
        return InstagramSession({"sessionid": "FAKE-SESSION-E2E-0001", "csrftoken": "FAKECSRF"})

    def save(self, sessionid, extra=None) -> None: ...

    def clear(self) -> None: ...


@pytest.fixture(autouse=True)
def _grid_only(monkeypatch):
    """Estes cenários cobrem só a grade: o site falso não serve a aba Reels (etapa testada em
    test_post_times_flow.py e test_reels_timeline.py). Sem o método, o runner pula a etapa."""
    monkeypatch.delattr(InstagramWebDownloader, "iter_reels_post_times")


@pytest.fixture
def env(tmp_path, real_media):
    server = FakeInstagram()
    cdn = FakeCdn(real_media, server)
    provider = SessionWithCookie()
    web = InstagramWebDownloader(provider, cdn_downloader=cdn, transport=server.transport())
    root = tmp_path / "app"
    root.mkdir()
    harness = Harness(root, downloader=web, processor=MediaProcessor(), session_provider=provider).__enter__()
    try:
        yield harness, server, cdn
    finally:
        harness.__exit__(None, None, None)
        assert server.unexpected == [], f"requisições inesperadas: {server.unexpected}"


# --------------------------------------------------------------------------------------------- helpers


def anti_dup_preset(h: Harness) -> dict:
    return next(p for p in h.ctx.presets.list() if p["name"] == "Anti-duplicidade")


def start_batch(h: Harness, limit: int | None, *, process: bool = True) -> int:
    """Cria o lote como POST /api/imports; limites 1 e 3 e None ("Todos", 422 limit_all_disabled na API) não passam
    pela API (10/20/50/100)."""
    preset = anti_dup_preset(h)
    if limit in (10, 20, 50, 100):
        response = h.client.post("/api/imports", json={"profile": PROFILE, "content_type": "all", "limit": limit,
                                                       "preset_id": preset["id"], "process": process})
        assert response.status_code == 202, response.text
        return response.json()["id"]
    batch_id = h.ctx.batches.create(
        source="instagram_import", status="listing", source_profile=PROFILE, content_type="all",
        requested_limit=limit, preset_id=preset["id"], preset_name=preset["name"],
        params=preset["params"] if process else None, process=process)
    h.ctx.wake_import_runner()
    return batch_id


def run_everything(h: Harness) -> None:
    h.run_imports()
    h.run_jobs()


def jobs_of(h: Harness, batch_id: int) -> list[dict]:
    return sorted(h.batch(batch_id)["jobs"], key=lambda j: j["id"])


def media_rows(h: Harness) -> list:
    return h.ctx.db.fetchall("SELECT * FROM media ORDER BY id")


def n_graphql(server: FakeInstagram) -> int:
    return len(server.posts)


# --------------------------------------------------------------------------------------------- testes


def test_full_pipeline_limit_none(env, real_media):
    h, server, cdn = env
    batch_id = start_batch(h, 100)
    run_everything(h)

    batch = h.batch(batch_id)
    assert batch["status"] == "completed", batch
    assert batch["found"] == 5 and batch["error_code"] is None
    jobs = jobs_of(h, batch_id)
    assert len(jobs) == 5 and all(j["kind"] == "ingest" and j["status"] == "completed" for j in jobs)
    # ordem da listagem e source_media_index do carrossel 0..2
    assert [(j["source_post_id"], j["source_media_index"]) for j in jobs] == EXPECTED_ORDER
    assert [j["media_type"] for j in jobs] == [KIND[KEY_BY_ITEM[i]] for i in EXPECTED_ORDER]

    # paginação: exatamente 2 páginas GraphQL; 1 bootstrap
    assert n_graphql(server) == 2 and server.gets == ["/"]
    assert "after" not in server.posts[0] and server.posts[1]["after"] == "CURSOR_PAGE_2"
    assert all(c["doc_id"] == "28991540097136703" and c["name"] == "PolarisProfilePostsQuery"
               and c["fb_dtsg"] == "FAKE_DTSG_DEFINE_0001:17:1700000000" and c["lsd"] == c["header_lsd"] == "FAKE_LSD_DEFINE_0001"
               and "sessionid=FAKE-SESSION-E2E-0001" in c["cookie"] for c in server.form_checks)

    rows = media_rows(h)
    assert len(rows) == 5
    originals_root = h.ctx.storage.originals
    renditions = h.ctx.db.fetchall("SELECT * FROM renditions ORDER BY media_id")
    assert len(renditions) == 5
    for index, order in enumerate(EXPECTED_ORDER):
        key = KEY_BY_ITEM[order]
        row = next(r for r in rows if (r["source_post_id"], r["source_media_index"]) == order)
        assert row["media_type"] == KIND[key]
        expected_sha = sha256_file(real_media[key])
        # SHA-256 no SQLite e nome do original por sha256, conteúdo intacto
        assert row["original_sha256"] == expected_sha
        original = h.ctx.storage.resolve(row["original_path"])
        assert original.is_file()
        assert original.stem == expected_sha and original.parent.name == expected_sha[:2]
        assert original.resolve().is_relative_to(originals_root.resolve())
        assert sha256_file(original) == expected_sha
        assert original.read_bytes() == real_media[key].read_bytes()
        assert row["thumbnail_path"] and h.ctx.storage.resolve(row["thumbnail_path"]).is_file()
        # rendition
        rend = next(r for r in renditions if r["media_id"] == row["id"])
        processed = h.ctx.storage.resolve(rend["processed_path"])
        assert processed.is_file() and processed.resolve().is_relative_to(h.ctx.storage.processed.resolve())
        assert rend["processed_sha256"] == sha256_file(processed) != expected_sha
        assert rend["preset_name"] == "Anti-duplicidade"
        info = probe(processed)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        if KIND[key] == "video":
            a = next(s for s in info["streams"] if s["codec_type"] == "audio")
            assert (v["codec_name"], v["pix_fmt"], a["codec_name"]) == ("h264", "yuv420p", "aac")
            assert processed.suffix == ".mp4"
        else:
            assert v["codec_name"] == "mjpeg" and processed.suffix == ".jpg"
    # nenhum .part/temp sobrando em originals
    assert not list(originals_root.rglob("*.part"))
    assert not list(originals_root.rglob("cdn-*"))


def test_media_api_exposes_hashes_and_reel_flag(env):
    h, _server, _cdn = env
    start_batch(h, 100)
    run_everything(h)
    items = h.client.get("/api/media").json()["items"]
    assert len(items) == 5
    for item in items:
        assert len(item["original_sha256"]) == 64 and len(item["renditions"]) == 1
        assert item["renditions"][0]["processed_sha256"] != item["original_sha256"]
    reel = next(i for i in items if i["source_post_id"] == "REELPOST01")
    assert "/reel/REELPOST01/" in reel["source_url"] and reel["has_audio"] is True


@pytest.mark.parametrize("bad_limit", [0, -3, 1_000_001])
def test_api_rejects_limits_outside_the_valid_range(env, bad_limit):
    """A API aceita N inteiro de 1 a 1.000.000 (ou null = Todos); antes só a lista fechada 10/20/50/100..."""
    h, _s, _c = env
    response = h.client.post("/api/imports", json={"profile": PROFILE, "content_type": "all", "limit": bad_limit,
                                                   "preset_id": None, "process": True})
    assert response.status_code == 422


def test_limit_1_creates_one_job_and_does_not_request_page_2(env):
    h, server, _cdn = env
    batch_id = start_batch(h, 1)
    run_everything(h)
    jobs = jobs_of(h, batch_id)
    assert [(j["source_post_id"], j["source_media_index"], j["status"]) for j in jobs] == [("IMGPOST001", 0, "completed")]
    assert n_graphql(server) == 1 and server.gets == ["/"]
    assert server.posts[0]["data"]["count"] == 12     # 1a página sempre cheia (count pequeno volta vazio no real)
    assert h.batch(batch_id)["status"] == "completed"
    assert len(media_rows(h)) == 1


def test_limit_3_stops_in_the_middle_of_the_carousel(env):
    h, server, _cdn = env
    batch_id = start_batch(h, 3)
    run_everything(h)
    jobs = jobs_of(h, batch_id)
    assert [(j["source_post_id"], j["source_media_index"]) for j in jobs] == EXPECTED_ORDER[:3]
    assert all(j["status"] == "completed" for j in jobs)
    assert n_graphql(server) == 2      # precisou da página 2 para o 3o item
    assert len(media_rows(h)) == 3


def test_reimport_dedups_by_source_id_without_downloading_or_reprocessing(env):
    h, server, cdn = env
    first = start_batch(h, 100)
    run_everything(h)
    downloads_before = len(cdn.calls)
    media_before = [(r["id"], r["original_sha256"]) for r in media_rows(h)]
    renditions_before = h.ctx.db.scalar("SELECT COUNT(*) FROM renditions")

    second = start_batch(h, 100)
    run_everything(h)
    jobs = jobs_of(h, second)
    assert len(jobs) == 5
    assert all(j["is_duplicate"] and j["duplicate_reason"] == "source_id" and j["status"] == "completed"
               and j["stage"] == "skipped" for j in jobs), jobs
    assert len(cdn.calls) == downloads_before                      # nenhum download novo
    assert [(r["id"], r["original_sha256"]) for r in media_rows(h)] == media_before
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM renditions") == renditions_before
    assert h.batch(first)["status"] == h.batch(second)["status"] == "completed"
    assert len(list(h.ctx.storage.originals.rglob("*.*"))) == 5


def test_expired_cdn_url_triggers_exactly_one_limited_refresh(env):
    h, server, cdn = env
    batch_id = start_batch(h, 100)
    h.run_imports()                      # listagem com URLs da geração 1
    assert n_graphql(server) == 2
    server.generation = 2                # a CDN passa a recusar as URLs antigas do reel
    cdn.expire["reel"] = 2
    h.run_jobs()
    jobs = jobs_of(h, batch_id)
    assert all(j["status"] == "completed" for j in jobs), [(j["source_post_id"], j["status"], j["error_code"]) for j in jobs]
    reel_job = next(j for j in jobs if j["source_post_id"] == "REELPOST01")
    assert reel_job["attempts"] == 0
    assert [c for c in cdn.calls if c.startswith("reel:")] == ["reel:g1", "reel:g2"]
    # 1 refresh = 1 bootstrap + páginas até achar o post (o reel está na página 1) => exatamente +1 POST e +1 GET
    assert n_graphql(server) == 3 and len(server.gets) == 2
    assert len(media_rows(h)) == 5


def test_refresh_is_bounded_when_cdn_keeps_refusing(env):
    h, server, cdn = env
    batch_id = start_batch(h, 100)
    h.run_imports()
    posts_after_listing = n_graphql(server)
    cdn.always_forbidden.add("car2")     # carrossel (página 2): refresh precisa paginar até achar
    h.run_jobs()
    jobs = jobs_of(h, batch_id)
    bad = next(j for j in jobs if j["source_media_index"] == 2)
    # Seção 2 (BATCH_ROBUSTNESS): recusado de novo após a renovação da página -> content_unavailable definitivo.
    assert bad["status"] == "failed" and bad["error_code"] == "content_unavailable"
    assert bad["attempts"] == 1 and bad["next_attempt_at"] is None           # não retentável
    assert [c.split(":")[0] for c in cdn.calls].count("car2") == 2           # 1 tentativa + 1 depois do refresh
    assert n_graphql(server) - posts_after_listing <= 3                      # REFRESH_MAX_PAGES
    assert sum(j["status"] == "completed" for j in jobs) == 4
    assert h.batch(batch_id)["status"] in ("completed", "failed")
    assert h.ctx.worker.pause_state() == (None, None)


def test_contract_changed_in_listing_fails_batch_with_clear_message_and_no_pause(env):
    h, server, _cdn = env
    server.graphql_body = json.dumps({"data": {"something_else": {}}})
    batch_id = start_batch(h, 100)
    run_everything(h)
    batch = h.batch(batch_id)
    assert batch["status"] == "failed" and batch["error_code"] == "contract_changed"
    assert "mudou a forma de responder" in batch["error_message"] and "atualizado" in batch["error_message"]
    assert batch["found"] == 0 and batch["jobs"] == []
    assert h.ctx.worker.pause_state() == (None, None)
    assert n_graphql(server) == 1                                           # sem laço de repetição
    assert media_rows(h) == []


def test_rate_limited_429_fails_batch_and_pauses_10_minutes(env):
    from datetime import UTC, datetime

    h, server, _cdn = env
    server.graphql_status = 429
    batch_id = start_batch(h, 100)
    run_everything(h)
    batch = h.batch(batch_id)
    # Seção 0: erro de proteção na listagem deixa o lote em `listing` pausado, retomável do checkpoint.
    assert batch["status"] == "listing" and batch["error_code"] == "rate_limited"
    assert h.ctx.db.scalar("SELECT listing_state FROM batches WHERE id = ?", (batch_id,)) == "paused"
    until, reason = h.ctx.worker.pause_state()
    assert reason == "rate_limited"
    remaining = (datetime.fromisoformat(until.replace("Z", "+00:00")) - datetime.now(UTC)).total_seconds()
    assert 890 <= remaining <= 901  # seção 8: 1o degrau de rate_limited é 15 min
    assert n_graphql(server) == 1
    # novo import é recusado enquanto pausado
    response = h.client.post("/api/imports", json={"profile": PROFILE, "content_type": "all", "limit": 10,
                                                   "preset_id": None, "process": True})
    assert response.status_code in (409, 429), response.text
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 1       # a tentativa recusada não criou lote


def test_no_tokens_or_session_leak_into_database_or_logs(env, caplog):
    h, server, _cdn = env
    with caplog.at_level("DEBUG"):
        batch_id = start_batch(h, 100)
        run_everything(h)
    blob = "\n".join(r.getMessage() for r in caplog.records)
    for secret in ("FAKE-SESSION-E2E-0001", "FAKE_DTSG_DEFINE_0001", "FAKE_LSD_DEFINE_0001"):
        assert secret not in blob
    dump = json.dumps(h.batch(batch_id), default=str)
    assert "FAKE-SESSION" not in dump and "FAKE_DTSG" not in dump
    # media_url com assinatura (oh/oe) não aparece na API pública de jobs
    assert "oh=FAKE" not in dump


def test_processed_media_preserved_original_when_processing_runs_twice(env, real_media):
    """Reprocessar com o mesmo preset não cria rendition duplicada nem mexe no original."""
    h, _s, _c = env
    start_batch(h, 1)
    run_everything(h)
    row = media_rows(h)[0]
    original = h.ctx.storage.resolve(row["original_path"])
    before = (sha256_file(original), original.stat().st_mtime_ns)
    preset = anti_dup_preset(h)
    response = h.client.post(f"/api/media/{row['id']}/process", json={"preset_id": preset["id"]})
    assert response.status_code == 202
    h.run_jobs()
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM renditions") == 1
    assert (sha256_file(original), original.stat().st_mtime_ns) == before


def test_429_on_page_2_keeps_partial_batch_and_pauses(env):
    """Erro após itens entregues: lote segue `running` com error_code, itens já listados ficam pendentes (pausa)."""
    h, server, _cdn = env
    original = server.handle

    def handle(request):
        if request.url.path == "/graphql/query" and b"CURSOR_PAGE_2" in request.content:
            server.posts.append({"after": "CURSOR_PAGE_2"})
            return httpx.Response(429, text="{}", headers={"content-type": "application/json"})
        return original(request)

    from app.integrations.instagram.web.http import InstagramWebHttp, WebHttpConfig

    h.downloader._http_factory = lambda session: InstagramWebHttp(
        session, WebHttpConfig(), transport=httpx.MockTransport(handle))
    batch_id = start_batch(h, 100)
    run_everything(h)
    batch = h.batch(batch_id)
    assert batch["error_code"] == "rate_limited" and batch["found"] == 2
    assert batch["status"] == "listing"                                       # pausado; retoma do checkpoint
    assert {j["status"] for j in batch["jobs"]} == {"pending"}               # downloads pausados por 10 min
    assert h.ctx.worker.pause_state()[1] == "rate_limited"
