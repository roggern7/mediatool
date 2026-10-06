"""Revisão independente (caixa-preta) do contrato docs/BATCH_ROBUSTNESS.md. 100% offline, valores FAKE.

App real (`create_app` via Harness) + `InstagramWebDownloader` real sobre `httpx.MockTransport` (bootstrap das fixtures e
páginas da PolarisProfilePostsQuery com cursores) + `cdn_download` fake controlável. Nenhuma rede, nenhum data/secrets.
Os números do contrato (3/6 páginas improdutivas, 20 páginas/0,25 de rendimento, 100/40 de backpressure) são fixados
aqui de propósito: o teste valida o comportamento da especificação, não as constantes do código.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.integrations.instagram.downloader import InstagramError, ListingRef, RemoteMedia
from app.integrations.instagram.session import InstagramSession, InstagramSessionProvider, SessionStatus
from app.integrations.instagram.web.provider import InstagramWebDownloader
from app.services.cdn_downloader import DownloadError, DownloadResult, validate_cdn_url
from conftest import Harness
from media_fakes import MAGIC, FakeProcessor

FIXTURES = Path(__file__).parent / "fixtures" / "instagram_web"
CDN_HOST = "https://scontent-fake.cdninstagram.com/v/t51.fake/"
PROFILE = "perfil_teste"
PAGE = 12
SECRET_MARKERS = ("FAKE_SIGNED", "FAKE_CURSOR")
PK_PREFIX = "7770"      # prefixo dos ids (pk/id) sintéticos
ALLOWED_API_LIMITS = (10, 20, 50, 100)
MAX_PENDING, RESUME_PENDING = 100, 40
INTERNAL_COLUMNS = ("listing_page_index", "listing_cursor", "listing_page_size", "media_url_refreshed_at")


# --------------------------------------------------------------------------------------------- servidor falso


def _node(n: int, code: str, kind: str, gen: int) -> dict:
    pk = f"{PK_PREFIX}{n:08d}"

    def url(ext: str) -> str:
        return f"{CDN_HOST}{code}.{ext}?oh=FAKE_SIGNED_{code}&oe=g{gen}"

    node = {"code": code, "pk": pk, "id": f"{pk}_9", "taken_at": 1700000000, "carousel_media": None,
            "product_type": "clips" if kind == "reel" else "feed",
            "media_type": 2 if kind == "reel" else 1,
            "image_versions2": {"candidates": [{"url": url("jpg"), "width": 320, "height": 240}]}}
    if kind == "reel":
        node["video_versions"] = [{"url": url("mp4"), "width": 320, "height": 240}]
        node["video_duration"] = 3.0
    return node


class Feed:
    """Perfil falso: lista de (code, kind). Cursor `FAKE_CURSOR_<offset da próxima página>`."""

    def __init__(self, kinds: list[str]) -> None:
        self.posts = [(f"CODE{i:05d}", kind) for i, kind in enumerate(kinds)]
        self.generation = 1
        self.gets = 0
        self.posts_vars: list[dict] = []
        self.unexpected: list[str] = []
        self.override = None                 # callable(index, variables, request) -> Response | None
        self.fixed_cursor: str | None = None  # força o mesmo end_cursor sempre (cursor repetido)
        self.removed: set[str] = set()
        self.delay = 0.0
        self.bootstrap_html = (FIXTURES / "bootstrap_define.html").read_text(encoding="utf-8")

    def code(self, i: int) -> str:
        return self.posts[i][0]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "www.instagram.com":
            self.unexpected.append(str(request.url.host))
            return httpx.Response(500)
        if request.method == "GET" and request.url.path == "/":
            self.gets += 1
            return httpx.Response(200, text=self.bootstrap_html, headers={"content-type": "text/html; charset=utf-8"})
        if request.method == "POST" and request.url.path == "/graphql/query":
            form = {k: v[0] for k, v in urllib.parse.parse_qs(request.content.decode()).items()}
            variables = json.loads(form["variables"])
            index = len(self.posts_vars)
            self.posts_vars.append(variables)
            if self.delay:
                time.sleep(self.delay)
            if self.override is not None:
                forced = self.override(index, variables, request)
                if forced is not None:
                    return forced
            return httpx.Response(200, json=self._page(variables), headers={"content-type": "application/json"})
        self.unexpected.append(f"{request.method} {request.url.path}")
        return httpx.Response(404)

    def _page(self, variables: dict) -> dict:
        after = variables.get("after")
        offset = int(after.rsplit("_", 1)[1]) if after and self.fixed_cursor is None else 0
        if after and self.fixed_cursor is not None:
            offset = len(self.posts_vars) * PAGE - PAGE   # avança sempre (nós novos)
        count = variables["data"]["count"]
        chunk = range(offset, min(offset + count, len(self.posts)))
        edges = [{"node": _node(i, *self.posts[i][:1], self.posts[i][1], self.generation), "cursor": None}
                 for i in chunk if self.posts[i][0] not in self.removed]
        end = offset + count
        has_next = end < len(self.posts)
        cursor = (self.fixed_cursor or f"FAKE_CURSOR_{end}") if has_next else None
        if self.fixed_cursor is not None:
            has_next, cursor = True, self.fixed_cursor
        return {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {
            "edges": edges, "page_info": {"has_next_page": has_next, "end_cursor": cursor}}}}

    def after_values(self) -> list[str | None]:
        return [v.get("after") for v in self.posts_vars]


class FakeCdn:
    def __init__(self) -> None:
        self.calls: list[str] = []            # "<code>:g<gen>"
        self.expire: dict[str, int] = {}      # code -> geração mínima aceita
        self.always_forbidden: set[str] = set()
        self.on_call = None
        self.lock = threading.Lock()

    def __call__(self, url, dest_dir, *, max_bytes, cancel=None, progress=None, **_):
        validate_cdn_url(url)
        parts = urllib.parse.urlsplit(url)
        code = Path(parts.path).stem
        gen = int(urllib.parse.parse_qs(parts.query)["oe"][0][1:])
        with self.lock:
            self.calls.append(f"{code}:g{gen}")
        if self.on_call is not None:
            self.on_call(code, gen)
        if code in self.always_forbidden or (code in self.expire and gen < self.expire[code]):
            raise DownloadError("forbidden", "CDN recusou o acesso (HTTP 403).")
        video = parts.path.endswith(".mp4")
        data = (MAGIC["video"] + f"FAKE-video-{code}".encode()) if video else (MAGIC["image"] + f"FAKE-image-{code}".encode())
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / f"cdn-{code}{'.mp4' if video else '.jpg'}"
        target.write_bytes(data)
        if progress:
            progress(len(data), len(data))
        return DownloadResult(path=target, size=len(data), content_type="x", suffix=target.suffix)

    def count(self, code: str) -> int:
        return sum(1 for c in self.calls if c.split(":")[0] == code)


class SessionWithCookie(InstagramSessionProvider):
    def status(self) -> SessionStatus:
        return SessionStatus(configured=True, source="file")

    def get(self):
        return InstagramSession({"sessionid": "FAKE-SESSION-BR-0001", "csrftoken": "FAKECSRF"})

    def save(self, sessionid, extra=None) -> None: ...

    def update_extra(self, extra) -> None: ...

    def clear(self) -> None: ...


class Env:
    def __init__(self, root: Path, feed: Feed, cdn: FakeCdn, provider) -> None:
        self.feed, self.cdn, self.provider, self.root = feed, cdn, provider, root
        self.h: Harness | None = None
        self.start()

    def start(self) -> Harness:
        web = InstagramWebDownloader(self.provider, cdn_downloader=self.cdn, transport=self.feed.transport(),
                                     page_delay=(0, 0))
        self.h = Harness(self.root, downloader=web, processor=FakeProcessor(), session_provider=self.provider).__enter__()
        return self.h

    def stop(self) -> None:
        if self.h is not None:
            self.h.__exit__(None, None, None)
            self.h = None


@pytest.fixture
def make_env(tmp_path):
    envs: list[Env] = []

    def factory(kinds: list[str] | None = None) -> Env:
        root = tmp_path / f"app{len(envs)}"
        root.mkdir()
        env = Env(root, Feed(kinds if kinds is not None else ["img"] * 60), FakeCdn(), SessionWithCookie())
        envs.append(env)
        return env

    yield factory
    for env in envs:
        env.stop()
        assert env.feed.unexpected == [], env.feed.unexpected


# --------------------------------------------------------------------------------------------- helpers


def start(h: Harness, limit: int | None, content: str = "all") -> int:
    if limit in ALLOWED_API_LIMITS:
        r = h.client.post("/api/imports", json={"profile": PROFILE, "content_type": content, "limit": limit,
                                                "preset_id": None, "process": False})
        assert r.status_code == 202, r.text
        return r.json()["id"]
    batch_id = h.ctx.batches.create(source="instagram_import", status="listing", source_profile=PROFILE,
                                    content_type=content, requested_limit=limit, preset_id=None, preset_name=None,
                                    params=None, process=False)
    h.ctx.wake_import_runner()
    return batch_id


def terminal(batch: dict) -> bool:
    return batch["status"] in ("completed", "failed", "cancelled")


def drain(h: Harness, batch_id: int, rounds: int = 400) -> dict:
    for _ in range(rounds):
        h.run_imports()
        ran = h.run_jobs()
        batch = h.batch(batch_id)
        if terminal(batch):
            return batch
        if ran == 0 and h.ctx.worker.downloads_paused():
            break
    return h.batch(batch_id)


def jobs_of(h: Harness, batch_id: int) -> list[dict]:
    return sorted(h.batch(batch_id)["jobs"], key=lambda j: j["id"])


def job_of(h: Harness, batch_id: int, code: str) -> dict:
    return next(j for j in jobs_of(h, batch_id) if j["source_post_id"] == code)


def batch_message(batch: dict) -> str:
    return batch.get("error_message") or batch.get("message") or batch.get("stop_message") or ""


def pending_ingest(h: Harness, batch_id: int) -> int:
    return h.ctx.db.scalar("SELECT COUNT(*) FROM jobs WHERE batch_id = ? AND kind = 'ingest' AND status = 'pending'",
                           (batch_id,))


def assert_listing_stopped_by(h: Harness, batch: dict, code: str) -> None:
    """Erro que pausa: o lote fica `listing` pausado (retomável do checkpoint) ou falha; nunca perde o código."""
    assert batch["error_code"] == code, batch
    assert batch["status"] in ("listing", "failed"), batch
    if batch["status"] == "listing":
        state = h.ctx.db.scalar("SELECT listing_state FROM batches WHERE id = ?", (batch["id"],))
        assert state == "paused", state


def pause_seconds(h: Harness) -> tuple[str | None, float]:
    until, reason = h.ctx.worker.pause_state()
    if until is None:
        return reason, 0.0
    return reason, (datetime.fromisoformat(until.replace("Z", "+00:00")) - datetime.now(UTC)).total_seconds()


def refresh_posts(feed: Feed, since: int) -> list[dict]:
    return feed.posts_vars[since:]


def max_pages_safety(limit: int, content: str) -> int:
    factor = {"all": 2, "posts": 2, "images": 4, "reels": 8, "videos": 8}[content]
    return math.ceil(limit / PAGE) * factor + 2


def prepare_expired(env: Env, *codes: str) -> int:
    """Lista tudo com URLs g1; depois a CDN passa a recusar g1 dessas mídias. Retorna nº de POSTs até aqui."""
    env.h.run_imports()
    since = len(env.feed.posts_vars)
    env.feed.generation = 2
    for code in codes:
        env.cdn.expire[code] = 2
    return since


# --------------------------------------------------------------------------------------------- budget (unidade)


def test_budget_formula_and_no_small_absolute_cap():
    from app.integrations.instagram.web import budget

    assert budget.PAGE_SIZE == 12
    f = budget.max_pages_for
    assert f(10, "all") == max_pages_safety(10, "all")
    assert f(100, "reels") == max_pages_safety(100, "reels")
    assert f(100, "images") == max_pages_safety(100, "images")
    assert 1660 <= f(10000, "all") <= 1680
    assert 6600 <= f(10000, "reels") <= 6700
    assert f(10000, "all") > 40, "não existe mais teto absoluto de 40 páginas"
    for content in ("all", "posts", "images", "reels", "videos"):
        assert f(300, content) >= math.ceil(300 / PAGE), content


# --------------------------------------------------------------------------------------------- 1, 2, 3 paginação


def test_01a_few_reels_in_photo_profile_stops_by_safety_cap_or_yield_and_completes(make_env, caplog):
    kinds = ["reel" if i % 20 == 19 else "img" for i in range(600)]
    env = make_env(kinds)
    h = env.h
    with caplog.at_level("DEBUG"):
        batch_id = start(h, 10, "reels")
        batch = drain(h, batch_id)
    pages = len(env.feed.posts_vars)
    assert pages <= max_pages_safety(10, "reels"), pages
    assert batch["status"] == "completed", batch
    assert 1 <= batch["found"] < 10
    assert all(j["status"] == "completed" for j in batch["jobs"])
    assert batch_message(batch), "o lote deve explicar por que parou antes da quantidade pedida"
    assert env.cdn.calls and all(c.split(":")[0] in {env.feed.code(i) for i in range(19, 600, 20)} for c in env.cdn.calls)


def test_01b_sparse_reels_stop_by_unproductive_streak_of_6_pages(make_env):
    kinds = ["reel" if i == 400 else "img" for i in range(600)]
    env = make_env(kinds)
    batch_id = start(env.h, 10, "reels")
    batch = drain(env.h, batch_id)
    assert len(env.feed.posts_vars) == 6
    assert batch["status"] == "completed" and batch["found"] == 0 and batch["error_code"] is None
    assert batch_message(batch)


def test_02_no_compatible_media_stops_after_unproductive_pages_filter(make_env, caplog):
    env = make_env(["img"] * 300)
    with caplog.at_level("DEBUG"):
        batch_id = start(env.h, 20, "videos")
        batch = drain(env.h, batch_id)
    assert len(env.feed.posts_vars) == 6
    assert batch["status"] == "completed" and batch["found"] == 0 and batch["error_code"] is None
    assert "filter_exhausted" in "\n".join(r.getMessage() for r in caplog.records)
    assert env.h.ctx.worker.pause_state() == (None, None)


def test_03_unproductive_counter_resets_on_productive_page(make_env):
    # 1 reel no início de cada 3a página: sequência improdutiva máxima = 2 (< 6); rendimento 0,33 >= 0,25
    kinds = ["reel" if (i % (PAGE * 3) == PAGE * 2) else "img" for i in range(PAGE * 40)]
    env = make_env(kinds)
    batch_id = start(env.h, 100, "reels")
    batch = drain(env.h, batch_id)
    assert len(env.feed.posts_vars) == 40, "o perfil inteiro deveria ser percorrido"
    assert batch["found"] == 13 and batch["status"] == "completed"


def test_03b_low_yield_after_20_pages(make_env, caplog):
    # 1 reel a cada 6 páginas: sequência improdutiva 5 (< 6) mas rendimento 0,167 < 0,25 -> low_yield em ~20 páginas
    kinds = ["reel" if (i % (PAGE * 6) == PAGE * 5) else "img" for i in range(PAGE * 120)]
    env = make_env(kinds)
    with caplog.at_level("DEBUG"):
        batch_id = start(env.h, 100, "reels")
        batch = drain(env.h, batch_id)
    assert 20 <= len(env.feed.posts_vars) <= 21, len(env.feed.posts_vars)
    assert batch["found"] == 3 and batch["status"] == "completed" and batch["error_code"] is None
    assert "low_yield" in "\n".join(r.getMessage() for r in caplog.records)


def test_04_repeated_cursor_is_contract_changed_without_pause(make_env):
    env = make_env(["img"] * 300)
    env.feed.fixed_cursor = "FAKE_CURSOR_SAME"
    batch_id = start(env.h, 100)
    env.h.run_imports()
    batch = env.h.batch(batch_id)
    assert batch["error_code"] == "contract_changed", batch
    # Seção 9 (C1): contract_changed no meio do lote não encerra; aguarda ação com o checkpoint mantido.
    assert batch["status"] == "listing" and batch["listing_state"] == "needs_attention"
    assert len(env.feed.posts_vars) <= 3
    assert env.h.ctx.worker.pause_state() == (None, None)


def test_05_requested_quantity_reached_requests_no_extra_page(make_env):
    env = make_env(["img"] * 100)
    h = env.h
    b10 = start(h, 10)
    drain(h, b10)
    assert len(env.feed.posts_vars) == 1 and h.batch(b10)["found"] == 10      # count reduzido, has_next verdadeiro
    before = len(env.feed.posts_vars)
    b20 = start(h, 20)
    drain(h, b20)
    assert len(env.feed.posts_vars) - before == 2 and h.batch(b20)["found"] == 20


# --------------------------------------------------------------------------------------------- 6 cancelamento


def test_06_cancel_during_listing(make_env):
    env = make_env(["img"] * 200)
    h = env.h
    holder: dict = {}

    def override(index, variables, request):
        if index == 1:
            r = h.client.post(f"/api/batches/{holder['id']}/cancel")
            assert r.status_code == 200, r.text
        return None

    env.feed.override = override
    holder["id"] = start(h, 100)
    h.run_imports()
    h.run_jobs()
    assert len(env.feed.posts_vars) == 2
    batch = h.batch(holder["id"])
    assert batch["status"] == "cancelled"
    assert all(j["status"] == "cancelled" for j in batch["jobs"])
    assert env.cdn.calls == []


# --------------------------------------------------------------------------------------------- 7, 8, 9, 11 renovação


def _two_page_env(make_env) -> tuple[Env, int]:
    env = make_env(["img"] * 60)
    batch_id = start(env.h, 20)         # 12 itens na pág. 1 e 8 na pág. 2 (cursor FAKE_CURSOR_12)
    return env, batch_id


def test_07_expired_url_does_one_refresh_with_stored_cursor_and_download_completes(make_env):
    env, batch_id = _two_page_env(make_env)
    code = env.feed.code(15)          # página 2
    since = prepare_expired(env, code)
    env.h.run_jobs()
    jobs = jobs_of(env.h, batch_id)
    assert all(j["status"] == "completed" for j in jobs), [(j["source_post_id"], j["status"], j["error_code"]) for j in jobs]
    new = refresh_posts(env.feed, since)
    assert len(new) == 1, "exatamente 1 refresh_page"
    assert new[0].get("after") == "FAKE_CURSOR_12" and new[0]["data"]["count"] == PAGE
    assert [c for c in env.cdn.calls if c.startswith(code)] == [f"{code}:g1", f"{code}:g2"]
    assert job_of(env.h, batch_id, code)["attempts"] == 0


def test_07b_expired_url_on_first_page_refreshes_without_cursor(make_env):
    env, batch_id = _two_page_env(make_env)
    code = env.feed.code(3)
    since = prepare_expired(env, code)
    env.h.run_jobs()
    new = refresh_posts(env.feed, since)
    assert len(new) == 1 and new[0].get("after") is None and new[0]["data"]["count"] == PAGE
    assert job_of(env.h, batch_id, code)["status"] == "completed"


def test_07c_refresh_still_refused_is_content_unavailable_and_not_retried(make_env):
    env, batch_id = _two_page_env(make_env)
    code = env.feed.code(15)
    since = prepare_expired(env, code)
    env.cdn.always_forbidden.add(code)
    env.h.run_jobs()
    job = job_of(env.h, batch_id, code)
    assert job["status"] == "failed" and job["error_code"] == "content_unavailable"
    assert job["next_attempt_at"] is None
    assert len(refresh_posts(env.feed, since)) == 1
    assert env.cdn.count(code) == 2
    assert env.h.ctx.worker.pause_state() == (None, None)


def test_07d_item_missing_on_refreshed_page_is_content_unavailable(make_env):
    env, batch_id = _two_page_env(make_env)
    code = env.feed.code(15)
    prepare_expired(env, code)
    env.feed.removed.add(code)
    env.h.run_jobs()
    job = job_of(env.h, batch_id, code)
    assert job["status"] == "failed" and job["error_code"] == "content_unavailable"
    assert env.cdn.count(code) == 1


def test_08_successful_refresh_updates_other_jobs_of_same_page(make_env):
    env, batch_id = _two_page_env(make_env)
    first, second = env.feed.code(13), env.feed.code(14)      # mesma página 2
    since = prepare_expired(env, first, second)
    env.h.run_jobs()
    jobs = jobs_of(env.h, batch_id)
    assert all(j["status"] == "completed" for j in jobs)
    assert len(refresh_posts(env.feed, since)) == 1, "o 2o job não renova: a URL já veio atualizada"
    assert env.cdn.count(first) == 2 and env.cdn.calls.count(f"{second}:g1") == 0 and env.cdn.count(second) == 1


def test_08b_page_refreshed_recently_and_url_still_refused_fails_without_second_refresh(make_env):
    env, batch_id = _two_page_env(make_env)
    first, second = env.feed.code(13), env.feed.code(14)
    since = prepare_expired(env, first)
    env.cdn.always_forbidden.add(second)    # a URL nova do 2o continua recusada
    env.h.run_jobs()
    assert job_of(env.h, batch_id, first)["status"] == "completed"
    bad = job_of(env.h, batch_id, second)
    assert bad["status"] == "failed" and bad["error_code"] == "content_unavailable"
    assert len(refresh_posts(env.feed, since)) == 1
    assert env.cdn.count(second) == 1


def test_09_concurrent_expiry_on_same_page_does_exactly_one_refresh(make_env):
    env, batch_id = _two_page_env(make_env)
    a, b = env.feed.code(13), env.feed.code(14)
    barrier = threading.Barrier(2, timeout=10)

    def on_call(code, gen):
        if gen == 1 and code in (a, b):
            barrier.wait()

    since = prepare_expired(env, a, b)
    env.cdn.on_call = on_call
    env.feed.delay = 0.3          # refresh lento: o outro job chega à trava enquanto o primeiro renova
    worker = env.h.ctx.worker
    claimed: list = []
    errors: list = []

    def run_one():
        try:
            item = worker._claim()
            claimed.append(item)
            worker._execute(*item)
        except BaseException as exc:      # noqa: BLE001
            errors.append(exc)

    # os 2 primeiros jobs reivindicados precisam ser a e b: remove os anteriores da fila
    for j in jobs_of(env.h, batch_id):
        if j["source_post_id"] not in (a, b):
            env.h.ctx.jobs.update(j["id"], status="cancelled")
    threads = [threading.Thread(target=run_one) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    assert len(refresh_posts(env.feed, since)) == 1
    assert job_of(env.h, batch_id, a)["status"] == "completed" and job_of(env.h, batch_id, b)["status"] == "completed"


def test_11_old_job_waiting_in_queue_uses_stored_cursor(make_env):
    env, batch_id = _two_page_env(make_env)
    h = env.h
    h.run_imports()
    h.ctx.worker.pause_downloads(600)
    assert h.run_jobs() == 0 and env.cdn.calls == []
    code = env.feed.code(15)
    since = len(env.feed.posts_vars)
    env.feed.generation = 2
    env.cdn.expire[code] = 2
    h.ctx.worker.clear_pause()
    h.run_jobs()
    assert job_of(h, batch_id, code)["status"] == "completed"
    new = refresh_posts(env.feed, since)
    assert len(new) == 1 and new[0].get("after") == "FAKE_CURSOR_12"


# --------------------------------------------------------------------------------------------- 10 renovação que falha


@pytest.mark.parametrize("name,response,code,lo,hi", [
    ("429", lambda: httpx.Response(429, json={}), "rate_limited", 890, 901),  # seção 8: 1o degrau 15 min
    ("checkpoint", lambda: httpx.Response(400, json={"message": "checkpoint_required",
                                                      "checkpoint_url": "https://www.instagram.com/challenge/FAKE/"}),
     "checkpoint_required", 5.9 * 3600, 6 * 3600 + 5),  # seção 8: verificação a cada 6 h
])
def test_10_refresh_failure_pauses(make_env, name, response, code, lo, hi):
    env, batch_id = _two_page_env(make_env)
    target = env.feed.code(15)
    since = prepare_expired(env, target)
    env.feed.override = lambda i, v, r: response()
    env.h.run_jobs()
    job = job_of(env.h, batch_id, target)
    assert job["error_code"] == code
    # FORK (ver relatório): 429 é retryable na classificação normal -> pode voltar a `pending` (waiting_retry);
    # checkpoint não é retryable -> failed.
    assert job["status"] in ("failed", "pending")
    reason, remaining = pause_seconds(env.h)
    assert reason == code and lo <= remaining <= hi
    assert len(refresh_posts(env.feed, since)) == 1, "sem retry extra"
    assert env.cdn.count(target) == 1


def test_10b_refresh_contract_changed_fails_without_pause(make_env):
    env, batch_id = _two_page_env(make_env)
    target = env.feed.code(15)
    since = prepare_expired(env, target)
    env.feed.override = lambda i, v, r: httpx.Response(200, json={"data": {"something_else": {}}})
    env.h.run_jobs()
    job = job_of(env.h, batch_id, target)
    assert job["status"] == "failed" and job["error_code"] == "contract_changed"
    assert env.h.ctx.worker.pause_state() == (None, None)
    assert len(refresh_posts(env.feed, since)) == 1


# --------------------------------------------------------------------------------------------- 12-16 pausas e classificação


def test_12_429_on_listing_pauses_10_min_and_blocks_new_imports(make_env):
    env = make_env()
    env.feed.override = lambda i, v, r: httpx.Response(429, json={})
    batch_id = start(env.h, 10)
    env.h.run_imports()
    batch = env.h.batch(batch_id)
    assert_listing_stopped_by(env.h, batch, "rate_limited")
    reason, remaining = pause_seconds(env.h)
    assert reason == "rate_limited" and 890 <= remaining <= 901  # seção 8: 1o degrau 15 min
    env.h.run_imports()
    assert len(env.feed.posts_vars) == 1, "nenhum retry automático"
    r = env.h.client.post("/api/imports", json={"profile": PROFILE, "content_type": "all", "limit": 10,
                                                "preset_id": None, "process": False})
    assert r.status_code == 409 and r.json()["code"] == "instagram_paused"


def test_13_checkpoint_pauses_24h_blocks_imports_until_released(make_env):
    env = make_env()
    env.feed.override = lambda i, v, r: httpx.Response(
        400, json={"message": "checkpoint_required", "checkpoint_url": "https://www.instagram.com/challenge/FAKE/"})
    batch_id = start(env.h, 10)
    env.h.run_imports()
    batch = env.h.batch(batch_id)
    assert_listing_stopped_by(env.h, batch, "checkpoint_required")
    reason, remaining = pause_seconds(env.h)
    assert reason == "checkpoint_required" and 5.9 * 3600 <= remaining <= 6 * 3600 + 5  # seção 8
    body = {"profile": PROFILE, "content_type": "all", "limit": 10, "preset_id": None, "process": False}
    r = env.h.client.post("/api/imports", json=body)
    assert r.status_code == 409 and r.json()["code"] == "instagram_paused"
    assert env.h.client.post("/api/instagram/profile",
                             json={"profile": PROFILE}).status_code == 409
    assert env.h.client.delete("/api/settings/instagram/pause").status_code == 200
    assert env.h.ctx.worker.pause_state() == (None, None)
    env.feed.override = None
    # Outro perfil: o lote pausado continua com a listagem aberta (mesmo perfil daria 409 profile_already_importing).
    assert env.h.client.post("/api/imports", json={**body, "profile": "outro_perfil"}).status_code == 202


def _login_required():
    return httpx.Response(401, json={"message": "login_required", "require_login": True, "status": "fail"})


@pytest.mark.parametrize("via", ["session", "cookies"])
def test_14_session_invalid_pauses_until_session_saved(make_env, via):
    env = make_env()
    env.feed.override = lambda i, v, r: _login_required()
    batch_id = start(env.h, 10)
    env.h.run_imports()
    batch = env.h.batch(batch_id)
    # Seção 7: um request recusado por autenticação pausa auth_check (15 min); salvar a sessão também libera.
    assert_listing_stopped_by(env.h, batch, "request_auth_failed")
    reason, remaining = pause_seconds(env.h)
    assert reason == "auth_check" and 0 < remaining <= 15 * 60 + 5
    body = {"profile": PROFILE, "content_type": "all", "limit": 10, "preset_id": None, "process": False}
    assert env.h.client.post("/api/imports", json=body).status_code == 409
    if via == "session":
        r = env.h.client.put("/api/settings/instagram/session", json={"sessionid": "FAKE-SESSION-NEW-0002"})
    else:
        r = env.h.client.patch("/api/settings/instagram/session/cookies", json={"csrftoken": "FAKECSRFNEW0002"})
    assert r.status_code == 200, r.text
    assert env.h.ctx.worker.pause_state() == (None, None), "salvar sessão/cookies limpa a pausa"
    env.feed.override = None
    # Outro perfil: o lote pausado continua com a listagem aberta (mesmo perfil daria 409 profile_already_importing).
    assert env.h.client.post("/api/imports", json={**body, "profile": "outro_perfil"}).status_code == 202


def test_15_access_refused_after_token_renewal_is_not_session_invalid(make_env):
    env = make_env()
    env.feed.override = lambda i, v, r: httpx.Response(403, json={"message": "csrf token mismatch"})
    batch_id = start(env.h, 10)
    env.h.run_imports()
    batch = env.h.batch(batch_id)
    assert batch["status"] == "failed" and batch["error_code"] == "access_refused", batch
    assert len(env.feed.posts_vars) == 2 and env.feed.gets == 2      # 1 renovação de token, nada além disso
    assert env.h.ctx.worker.pause_state() == (None, None)


def test_15b_persistent_1357004_after_renewal_is_contract_changed(make_env):
    env = make_env()
    env.feed.override = lambda i, v, r: httpx.Response(200, json={"error": 1357004, "errorSummary": "x"})
    batch_id = start(env.h, 10)
    env.h.run_imports()
    batch = env.h.batch(batch_id)
    assert batch["status"] == "failed" and batch["error_code"] == "contract_changed", batch
    assert len(env.feed.posts_vars) == 2
    assert env.h.ctx.worker.pause_state() == (None, None)


def test_16_contract_changed_listing_no_pause_no_loop(make_env):
    env = make_env()
    env.feed.override = lambda i, v, r: httpx.Response(200, json={"data": {"something_else": {}}})
    batch_id = start(env.h, 10)
    env.h.run_imports()
    batch = env.h.batch(batch_id)
    assert batch["status"] == "failed" and batch["error_code"] == "contract_changed"
    assert "atualizado" in batch["error_message"]
    assert len(env.feed.posts_vars) == 1 and env.h.ctx.worker.pause_state() == (None, None)


# --------------------------------------------------------------------------------------------- 17 "Todos"


def test_17_limit_all_is_422(make_env):
    env = make_env()
    r = env.h.client.post("/api/imports", json={"profile": PROFILE, "content_type": "all", "limit": None,
                                                "preset_id": None, "process": False})
    assert r.status_code == 422 and r.json()["code"] == "limit_all_disabled" and r.json()["detail"]
    assert env.h.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0
    for limit in ALLOWED_API_LIMITS:  # perfis distintos (mesmo perfil com listagem aberta: 409)
        assert env.h.client.post("/api/imports", json={"profile": f"{PROFILE}_{limit}", "content_type": "all",
                                                       "limit": limit,
                                                       "preset_id": None, "process": False}).status_code == 202


# --------------------------------------------------------------------------------------------- 18 segredos


def test_18_no_cursor_signed_url_or_id_leaks_and_internal_columns_cleaned(make_env, caplog):
    env, batch_id = _two_page_env(make_env)
    h = env.h
    c_ok, c_fail, c_cancel = env.feed.code(15), env.feed.code(16), env.feed.code(17)
    with caplog.at_level("DEBUG"):
        since = prepare_expired(env, c_ok, c_fail)
        env.cdn.always_forbidden.add(c_fail)
        # enquanto pendentes, nada sensível nas respostas públicas
        for path in (f"/api/batches/{batch_id}", "/api/batches", "/api/jobs"):
            text = h.client.get(path).text
            for needle in (*SECRET_MARKERS, PK_PREFIX, "listing_cursor", "listing_page_index", "media_url"):
                assert needle not in text, (path, needle)
        cancelled = job_of(h, batch_id, c_cancel)
        assert h.client.post(f"/api/jobs/{cancelled['id']}/cancel").status_code in (200, 202, 204)
        h.run_jobs()
    assert len(refresh_posts(env.feed, since)) >= 1
    blob = "\n".join(r.getMessage() + " " + str(r.args) for r in caplog.records)
    blob += "\n".join(str(r.exc_text) for r in caplog.records)
    for needle in (*SECRET_MARKERS, PK_PREFIX, "FAKE-SESSION-BR"):
        assert needle not in blob, needle
    for path in (f"/api/batches/{batch_id}", "/api/batches", "/api/jobs"):
        text = h.client.get(path).text
        for needle in (*SECRET_MARKERS, PK_PREFIX, "listing_cursor", "media_url"):
            assert needle not in text, (path, needle)
    assert job_of(h, batch_id, c_fail)["status"] == "failed"
    assert job_of(h, batch_id, c_cancel)["status"] == "cancelled"
    # URLs e cursor ficam em job_remote_refs (seção 6): job terminado não tem URL; concluído não tem linha.
    rows = h.ctx.db.fetchall(
        "SELECT j.status, r.job_id, r.media_url, r.thumbnail_url FROM jobs j"
        " LEFT JOIN job_remote_refs r ON r.job_id = j.id WHERE j.batch_id = ?", (batch_id,))
    done = [r for r in rows if r["status"] in ("completed", "failed", "cancelled")]
    assert done and all(not r["media_url"] and not r["thumbnail_url"] for r in done)
    assert all(r["job_id"] is None for r in done if r["status"] == "completed")
    cols = {r["name"] for r in h.ctx.db.fetchall("PRAGMA table_info(jobs)")}
    cols |= {r["name"] for r in h.ctx.db.fetchall("PRAGMA table_info(job_remote_refs)")}
    assert set(INTERNAL_COLUMNS) <= cols


def test_18b_repr_of_listing_objects_hides_cursor():
    ref = ListingRef(page_index=2, cursor="FAKE_CURSOR_12", page_size=12)
    assert (ref.page_index, ref.cursor, ref.page_size) == (2, "FAKE_CURSOR_12", 12)
    with pytest.raises(Exception):
        ref.page_index = 3          # frozen


# --------------------------------------------------------------------------------------------- provider (unidade)


def _web(feed: Feed, cdn=None) -> InstagramWebDownloader:
    return InstagramWebDownloader(SessionWithCookie(), cdn_downloader=cdn or FakeCdn(), transport=feed.transport(),
                                  page_delay=(0, 0))


def test_provider_items_carry_listing_ref_and_download_does_not_refresh(tmp_path):
    feed = Feed(["img"] * 30)
    web = _web(feed)
    items = list(web.iter_profile_media(PROFILE, "all", 20))
    assert items[0].listing_ref == ListingRef(1, None, PAGE)
    assert items[PAGE].listing_ref == ListingRef(2, "FAKE_CURSOR_12", PAGE)
    cdn = FakeCdn()
    cdn.always_forbidden.add(items[0].source_post_id)
    web = _web(feed, cdn)
    before = len(feed.posts_vars)
    with pytest.raises(InstagramError) as info:
        web.download(items[0], Path(tmp_path))
    assert info.value.code == "media_url_expired" and info.value.retryable is False
    assert getattr(info.value, "diagnostic", {}).get("detail") == "http_403"
    assert len(feed.posts_vars) == before, "download não renova mais internamente"


def test_provider_refresh_page_single_post_with_cursor_and_count():
    feed = Feed(["img"] * 30)
    web = _web(feed)
    fresh = web.refresh_page(PROFILE, ListingRef(2, "FAKE_CURSOR_12", PAGE))
    assert len(feed.posts_vars) == 1
    variables = feed.posts_vars[0]
    assert variables["after"] == "FAKE_CURSOR_12" and variables["data"]["count"] == PAGE
    assert [m.source_post_id for m in fresh] == [feed.code(i) for i in range(12, 24)]
    assert all(isinstance(m, RemoteMedia) for m in fresh)


def test_default_downloader_refresh_page_is_content_unavailable():
    from media_fakes import FakeInstagramDownloader

    with pytest.raises(InstagramError) as info:
        FakeInstagramDownloader().refresh_page(PROFILE, ListingRef(1, None, PAGE))
    assert info.value.code == "content_unavailable"


# --------------------------------------------------------------------------------------------- 19, 20, 21 escala


def test_19_large_legitimate_request_is_not_cut_by_budget(make_env):
    env = make_env(["img"] * 420)
    batch_id = start(env.h, 300)
    batch = drain(env.h, batch_id)
    assert batch["status"] == "completed" and batch["found"] == 300, batch
    assert len(env.feed.posts_vars) == 300 // PAGE + (1 if 300 % PAGE else 0)
    assert all(j["status"] == "completed" for j in batch["jobs"])
    assert len({j["source_post_id"] for j in batch["jobs"]}) == 300
    after = [a for a in env.feed.after_values() if a]
    assert len(after) == len(set(after)), "nenhuma página pedida duas vezes"


def test_20_backpressure_never_more_than_100_pending_listing_pauses_and_resumes(make_env):
    env = make_env(["img"] * 420)
    h = env.h
    batch_id = start(h, 300)
    peak = 0
    h.ctx.import_runner.run_next()
    peak = max(peak, pending_ingest(h, batch_id))
    assert 0 < pending_ingest(h, batch_id) <= MAX_PENDING
    state = h.ctx.db.scalar("SELECT listing_state FROM batches WHERE id = ?", (batch_id,))
    assert state == "waiting_backpressure", state
    assert h.batch(batch_id)["status"] in ("listing", "running")
    posts_at_stop = len(env.feed.posts_vars)
    # ainda acima de RESUME: não lista mais
    h.ctx.worker.run_pending(max_jobs=pending_ingest(h, batch_id) - (RESUME_PENDING + 10))
    assert pending_ingest(h, batch_id) > RESUME_PENDING
    h.ctx.import_runner.run_next()
    assert len(env.feed.posts_vars) == posts_at_stop, "só retoma abaixo de 40 pendentes"
    # drena passo a passo medindo o pico
    for _ in range(400):
        h.ctx.import_runner.run_next()
        peak = max(peak, pending_ingest(h, batch_id))
        if h.ctx.worker.run_pending(max_jobs=20) == 0 and terminal(h.batch(batch_id)):
            break
        if terminal(h.batch(batch_id)):
            break
    batch = h.batch(batch_id)
    assert peak <= MAX_PENDING, peak
    assert batch["status"] == "completed" and batch["found"] == 300
    after = [a for a in env.feed.after_values() if a]
    assert len(after) == len(set(after)), "retomada continua do checkpoint, sem repetir páginas"
    assert h.ctx.db.scalar("SELECT listing_state FROM batches WHERE id = ?", (batch_id,)) == "done"


def test_21_restart_resumes_from_checkpoint_without_redoing_pages_or_downloads(make_env):
    env = make_env(["img"] * 420)
    h = env.h
    batch_id = start(h, 300)
    h.ctx.import_runner.run_next()                       # para por backpressure
    h.ctx.worker.run_pending(max_jobs=70)                # 70 concluídos
    done_before = {j["source_post_id"] for j in h.batch(batch_id)["jobs"] if j["status"] == "completed"}
    assert len(done_before) == 70
    posts_before = len(env.feed.posts_vars)
    pages_before = list(env.feed.after_values())
    cdn_before = len(env.cdn.calls)
    env.stop()
    h2 = env.start()                                      # novo app/novo downloader sobre o mesmo DB
    batch = drain(h2, batch_id)
    assert batch["status"] == "completed" and batch["found"] == 300, batch
    assert len({j["source_post_id"] for j in batch["jobs"]}) == 300 == len(batch["jobs"])
    resumed = env.feed.after_values()[posts_before:]
    assert resumed and resumed[0] is not None, "não recomeçou da 1a página"
    assert len([a for a in resumed if a in pages_before[1:]]) <= 1, "no máximo a página em voo é repetida"
    for code in done_before:
        assert env.cdn.count(code) == 1, "o que terminou não é baixado de novo"
    assert len(env.cdn.calls) - cdn_before == 300 - 70
