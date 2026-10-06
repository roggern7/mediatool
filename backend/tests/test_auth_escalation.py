"""Falha de autenticação de um request x sessão inválida (docs/BATCH_ROBUSTNESS.md, seção 7). Offline, valores FAKE.

Lotes simulados de 1k, 5k e 10k itens com um downloader fake que gera as páginas sob demanda; o erro de autenticação
aparece uma vez na página N. Nada aqui toca C:\\media-tool\\data\\secrets: a sessão DPAPI fica num secrets_dir
temporário.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.integrations.instagram.downloader import (
    InstagramDownloader,
    InstagramError,
    ListingCheckpoint,
    ListingRef,
    ProfileInfo,
    SessionTestResult,
)
from app.integrations.instagram.session import LocalInstagramSessionProvider
from app.integrations.instagram.web.bootstrap import WebBootstrap
from app.integrations.instagram.web.errors import InstagramWebError, classify_response
from app.integrations.instagram.web.http import WebResponse
from app.services.instagram_recovery import AUTH_FAIL_STREAK_KEY
from media_fakes import FAKE_SESSION, MAGIC, remote

PAGE_SIZE = 50
FAKE_EXTRA = {"csrftoken": "FAKECSRF0000", "ds_user_id": "1234567890"}


class LazyPagedDownloader(InstagramDownloader):
    """Listagem que gera cada página só quando é pedida (sem lista de itens em memória). ``fail_pages``: página ->
    quantas vezes ela falha com ``fail_code`` antes de responder."""

    def __init__(self, total: int, *, page_size: int = PAGE_SIZE, fail_pages: dict[int, int] | None = None,
                 fail_code: str = "request_auth_failed", prefix: str = "Z") -> None:
        self.total = total
        self.prefix = prefix
        self.page_size = page_size
        self.fail_pages = dict(fail_pages or {})
        self.fail_code = fail_code
        self.page_requests: list[int] = []
        self.downloaded: list[str] = []
        self.session_result: SessionTestResult | None = None
        self._lock = threading.Lock()

    def _page_items(self, page: int):
        start = (page - 1) * self.page_size
        cursor = None if page == 1 else f"FAKE-CURSOR-{page}"
        ref = ListingRef(page_index=page, cursor=cursor, page_size=self.page_size)
        for i in range(start, min(start + self.page_size, self.total)):
            yield replace(remote(f"{self.prefix}{i:05d}"), listing_ref=ref)

    def test_session(self) -> SessionTestResult:
        return self.session_result or SessionTestResult(ok=True, code="ok", message="Sessão válida.", username="c")

    def get_profile(self, username: str) -> ProfileInfo:
        return ProfileInfo(username=username, post_count=self.total)

    def iter_profile_media(self, username, content, limit, *, cancel=None, resume=None, on_checkpoint=None):
        last_page = max(1, -(-self.total // self.page_size))
        page = resume.next_page_index if resume is not None else 1
        pages = resume.pages if resume is not None else 0
        seen = resume.posts_seen if resume is not None else 0
        produced = 0
        while page <= last_page:
            if cancel is not None and cancel.is_set():
                return
            if limit is not None and produced >= limit:
                return
            self.page_requests.append(page)
            if self.fail_pages.get(page, 0) > 0:
                self.fail_pages[page] -= 1
                raise InstagramError(self.fail_code)
            count = 0
            for item in self._page_items(page):
                if cancel is not None and cancel.is_set():
                    return
                if limit is not None and produced >= limit:
                    return
                produced += 1
                count += 1
                yield item
            pages += 1
            seen += count
            has_next = page < last_page
            if on_checkpoint is not None:
                on_checkpoint(ListingCheckpoint(
                    resume_cursor=f"FAKE-CURSOR-{page + 1}" if has_next else None, next_page_index=page + 1,
                    pages=pages, posts_seen=seen, items_compatible=seen, unproductive_streak=0, has_next=has_next))
            page += 1

    def resolve_post(self, shortcode: str):
        return []

    def download(self, item, dest_dir: Path, *, cancel=None, progress=None) -> Path:
        key = f"{item.source_post_id}:{item.source_media_index}"
        with self._lock:
            self.downloaded.append(key)
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / "download.jpg"
        target.write_bytes(MAGIC["image"] + f"FAKE-image-{key}".encode())
        return target


class SpySessionProvider(LocalInstagramSessionProvider):
    """Sessão DPAPI real num diretório temporário; registra qualquer chamada que altere a sessão."""

    def __init__(self, secrets_dir: Path) -> None:
        super().__init__(secrets_dir, env={})
        self.calls: list[str] = []

    def save(self, sessionid, extra=None):
        self.calls.append("save")
        return super().save(sessionid, extra)

    def update_extra(self, extra):
        self.calls.append("update_extra")
        return super().update_extra(extra)

    def clear(self):
        self.calls.append("clear")
        return super().clear()


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def session_fixture(tmp_path: Path) -> tuple[SpySessionProvider, str]:
    provider = SpySessionProvider(tmp_path / "secrets-temp")
    provider.save(FAKE_SESSION, dict(FAKE_EXTRA))
    provider.calls.clear()
    return provider, file_hash(provider)


def file_hash(provider: LocalInstagramSessionProvider) -> str:
    return hashlib.sha256(provider.path.read_bytes()).hexdigest()


def assert_session_intact(provider: SpySessionProvider, digest: str) -> None:
    assert provider.path.is_file()
    assert file_hash(provider) == digest, "o arquivo DPAPI da sessão não pode mudar"
    assert provider.calls == [], f"nenhuma chamada que altere a sessão: {provider.calls}"
    session = provider.get()
    assert session is not None
    cookies = session.cookies()
    assert cookies["sessionid"] == FAKE_SESSION
    assert all(cookies.get(k) == v for k, v in FAKE_EXTRA.items()), "cookies extras intactos"
    assert provider.status().configured is True


def make_harness(harness_factory, tmp_path: Path, downloader) -> tuple:
    provider, digest = session_fixture(tmp_path)
    h = harness_factory(downloader=downloader, session_provider=provider)
    clock = FakeClock()
    h.ctx.worker.clock = clock
    return h, provider, digest, clock


def internal_batch(h, limit: int) -> int:
    """Lote com limite grande pelo caminho interno (a API só aceita volumes menores na interface)."""
    return h.ctx.batches.create(source="instagram_import", status="listing", source_profile="perfil_teste",
                                content_type="all", requested_limit=limit)


def batch_row(h, batch_id: int) -> dict:
    return dict(h.ctx.db.fetchone("SELECT status, listing_state, error_code FROM batches WHERE id = ?",
                                   (batch_id,)))


def drive(h, batch_id: int, *, stop_when_paused: bool = False, max_rounds: int = 5000) -> None:
    """Alterna listagem e jobs até o lote concluir (ou até a pausa, ou até não haver mais o que fazer)."""
    worker = h.ctx.worker
    for _ in range(max_rounds):
        h.run_imports()
        if stop_when_paused and worker.downloads_paused():
            return
        ran = worker.run_pending(max_jobs=500)
        if batch_row(h, batch_id)["status"] == "completed":
            return
        if ran == 0 and worker.downloads_paused():
            return
    raise AssertionError("o lote não terminou")


def valid_result() -> SessionTestResult:
    return SessionTestResult(ok=True, code="ok", message="Sessão válida.", username="conta")


def auth_failed_result() -> SessionTestResult:
    return SessionTestResult(ok=False, code="request_auth_failed", message="Recusado.", username=None)


# --------------------------------------------------------------------------- lotes de 1k, 5k e 10k


# 5k e 10k concluem todos os downloads (~2 e ~4,5 min): marcados slow para quem quiser pular com -m "not slow".
@pytest.mark.parametrize("total,fail_page", [
    (1000, 7),
    pytest.param(5000, 41, marks=pytest.mark.slow),
    pytest.param(10000, 133, marks=pytest.mark.slow),
])
def test_isolated_auth_error_pauses_and_resumes_from_checkpoint(harness_factory, tmp_path, total, fail_page):
    dl = LazyPagedDownloader(total, fail_pages={fail_page: 1})
    h, provider, digest, clock = make_harness(harness_factory, tmp_path, dl)
    worker = h.ctx.worker
    batch_id = internal_batch(h, total)

    drive(h, batch_id, stop_when_paused=True)
    row = batch_row(h, batch_id)
    assert row["status"] == "listing" and row["listing_state"] == "paused", row
    assert row["error_code"] == "request_auth_failed"
    until, reason = worker.pause_state()
    assert reason == "auth_check" and worker.current_pause_scope() == "session"
    remaining = (datetime.fromisoformat(until.replace("Z", "+00:00")) - clock.now).total_seconds()
    assert 14 * 60 < remaining <= 15 * 60
    assert h.ctx.app_settings.get_state(AUTH_FAIL_STREAK_KEY) == "1"
    status = h.client.get("/api/settings").json()["instagram"]
    assert status["pause_reason"] == "auth_check" and status["pause_scope"] == "session"
    assert status["action_required"] is False and status["session_configured"] is True

    # Durante a pausa: nenhuma página nova; downloads com URL válida continuam (escopo "session").
    requested = list(dl.page_requests)
    before = len(dl.downloaded)
    drive(h, batch_id)
    assert dl.page_requests == requested, "nenhuma consulta ao Instagram durante a pausa"
    assert len(dl.downloaded) > before or before == (fail_page - 1) * PAGE_SIZE
    assert_session_intact(provider, digest)

    # Vencida, a pausa continua até a verificação do controlador (nenhuma listagem antes dela).
    clock.advance(minutes=16)
    assert worker.downloads_paused()
    h.run_imports()
    assert dl.page_requests == requested
    worker.recovery.verify = valid_result
    assert worker.recovery.tick() == "ok"
    assert worker.pause_state() == (None, None)
    assert h.ctx.app_settings.get_state(AUTH_FAIL_STREAK_KEY) is None

    drive(h, batch_id)
    row = batch_row(h, batch_id)
    assert row["status"] == "completed", row
    found = h.ctx.db.scalar("SELECT COUNT(*) FROM jobs WHERE batch_id = ? AND status = 'completed'", (batch_id,))
    assert found == total
    assert len(set(dl.downloaded)) == total
    # Retomou do checkpoint: só a página que falhou foi pedida de novo.
    last_page = -(-total // PAGE_SIZE)
    expected = list(range(1, fail_page + 1)) + list(range(fail_page, last_page + 1))
    assert dl.page_requests == expected
    assert_session_intact(provider, digest)


# --------------------------------------------------------------------------- escalada (seção 7 c)


def test_second_consecutive_auth_failure_after_pause_is_session_invalid_but_session_intact(harness_factory, tmp_path):
    dl = LazyPagedDownloader(300, fail_pages={3: 1})
    h, provider, digest, clock = make_harness(harness_factory, tmp_path, dl)
    worker = h.ctx.worker
    batch_id = internal_batch(h, 300)
    drive(h, batch_id, stop_when_paused=True)
    assert worker.pause_state()[1] == "auth_check"

    clock.advance(minutes=16)
    worker.recovery.verify = auth_failed_result   # 2a verificação, sem sucesso entre elas
    assert worker.recovery.tick() == "failed"
    until, reason = worker.pause_state()
    assert reason == "session_invalid" and worker.current_pause_scope() == "session"
    remaining = (datetime.fromisoformat(until.replace("Z", "+00:00")) - clock.now).total_seconds()
    assert 29 * 60 < remaining <= 30 * 60, "sobe um degrau (30 min)"
    status = h.client.get("/api/settings").json()["instagram"]
    assert status["pause_reason"] == "session_invalid" and status["session_configured"] is True
    assert batch_row(h, batch_id)["status"] == "listing"
    assert_session_intact(provider, digest)

    # Pausa liberável: um teste de sessão válido libera e zera o contador; o lote conclui.
    dl.session_result = valid_result()
    tested = h.client.post("/api/settings/instagram/session/test")
    assert tested.status_code == 200 and tested.json()["pause_reason"] is None
    assert h.ctx.app_settings.get_state(AUTH_FAIL_STREAK_KEY) is None
    drive(h, batch_id)
    assert batch_row(h, batch_id)["status"] == "completed"
    assert_session_intact(provider, digest)


def test_manual_release_of_session_invalid_keeps_session(harness_factory, tmp_path):
    dl = LazyPagedDownloader(120, fail_pages={2: 1})
    h, provider, digest, clock = make_harness(harness_factory, tmp_path, dl)
    worker = h.ctx.worker
    batch_id = internal_batch(h, 120)
    drive(h, batch_id, stop_when_paused=True)
    clock.advance(minutes=16)
    worker.recovery.verify = auth_failed_result
    worker.recovery.tick()
    assert worker.pause_state()[1] == "session_invalid"
    assert h.client.delete("/api/settings/instagram/pause").status_code == 200
    assert worker.pause_state() == (None, None)
    drive(h, batch_id)
    assert batch_row(h, batch_id)["status"] == "completed"
    assert_session_intact(provider, digest)


def test_success_between_failures_resets_counter(harness_factory, tmp_path):
    dl = LazyPagedDownloader(600, fail_pages={3: 1, 9: 1})
    h, provider, digest, clock = make_harness(harness_factory, tmp_path, dl)
    worker = h.ctx.worker
    batch_id = internal_batch(h, 600)

    drive(h, batch_id, stop_when_paused=True)
    assert worker.pause_state()[1] == "auth_check"
    clock.advance(minutes=16)
    worker.recovery.verify = valid_result
    assert worker.recovery.tick() == "ok"

    # Páginas 3..8 entregues com sucesso; a página 9 falha: de novo só auth_check (não escala).
    drive(h, batch_id, stop_when_paused=True)
    assert worker.pause_state()[1] == "auth_check"
    assert h.ctx.app_settings.get_state(AUTH_FAIL_STREAK_KEY) == "1"
    clock.advance(minutes=31)   # seção 9 (A2): dentro da janela de prova o backoff sobe (30 min), sem escalar
    assert worker.recovery.tick() == "ok"
    drive(h, batch_id)
    assert batch_row(h, batch_id)["status"] == "completed"
    assert_session_intact(provider, digest)


def test_success_without_pause_resets_counter_left_by_manual_release(harness_factory, tmp_path):
    dl = LazyPagedDownloader(300, fail_pages={2: 1, 5: 1})
    h, provider, digest, clock = make_harness(harness_factory, tmp_path, dl)
    worker = h.ctx.worker
    batch_id = internal_batch(h, 300)
    drive(h, batch_id, stop_when_paused=True)
    # Liberação manual não é sucesso autenticado: o contador continua em 1 até uma página dar certo.
    worker.clear_pause()
    assert h.ctx.app_settings.get_state(AUTH_FAIL_STREAK_KEY) == "1"
    drive(h, batch_id, stop_when_paused=True)   # páginas 2..4 ok, 5 falha
    assert worker.pause_state()[1] == "auth_check", "sucesso entre as falhas zerou o contador"
    assert_session_intact(provider, digest)


# --------------------------------------------------------------------------- evidência forte


def _resp(status: int, *, location: str | None = None, sessionid_deleted: bool = False, text: str = "{}",
          content_type: str = "application/json") -> WebResponse:
    names = ["sessionid"] if sessionid_deleted else []
    return WebResponse(status, location, content_type, 5, names, sessionid_deleted, text, "/graphql/query")


def test_set_cookie_deleting_sessionid_is_session_invalid_immediately(harness_factory, tmp_path):
    err = classify_response(_resp(200, sessionid_deleted=True, text='{"data": {}}'), stage="graphql")
    assert err is not None and err.code == "session_invalid"

    dl = LazyPagedDownloader(200)
    h, provider, digest, clock = make_harness(harness_factory, tmp_path, dl)
    worker = h.ctx.worker
    dl.fail_pages = {2: 1}
    dl.fail_code = "session_invalid"
    batch_id = internal_batch(h, 200)
    drive(h, batch_id, stop_when_paused=True)
    assert worker.pause_state()[1] == "session_invalid"
    status = h.client.get("/api/settings").json()["instagram"]
    assert status["action_required"] is True and status["pause_scope"] == "session"
    assert batch_row(h, batch_id)["listing_state"] == "paused"
    assert_session_intact(provider, digest)
    # Liberável: salvar a sessão encerra a pausa (o save é ação do dono, não do Media Tool).
    saved = h.client.put("/api/settings/instagram/session", json={"sessionid": FAKE_SESSION})
    assert saved.status_code == 200 and saved.json()["pause_reason"] is None


class _DocHttp:
    def __init__(self, response: WebResponse) -> None:
        self.response = response

    def get_document(self, path: str) -> WebResponse:
        return self.response


def test_bootstrap_redirect_to_login_is_session_invalid(harness_factory, tmp_path):
    boot = WebBootstrap(_DocHttp(_resp(302, location="/accounts/login/", content_type="text/html", text="")))
    with pytest.raises(InstagramWebError) as info:
        boot.get()
    assert info.value.code == "session_expired"

    h, provider, digest, clock = make_harness(harness_factory, tmp_path, LazyPagedDownloader(10))
    assert h.ctx.worker.pause_for(info.value.code) is True
    assert h.ctx.worker.pause_state()[1] == "session_invalid"
    assert h.client.get("/api/settings").json()["instagram"]["action_required"] is True
    assert_session_intact(provider, digest)


def test_graphql_redirect_to_login_is_only_request_auth_failed():
    err = classify_response(_resp(302, location="/accounts/login/", content_type="text/html", text=""),
                            stage="graphql")
    assert err is not None and err.code == "request_auth_failed" and err.retryable is True
    for body in ('{"require_login": true}', '{"message": "login_required", "status": "fail"}'):
        assert classify_response(_resp(400, text=body), stage="graphql").code == "request_auth_failed"
    assert classify_response(_resp(401), stage="graphql").code == "request_auth_failed"
