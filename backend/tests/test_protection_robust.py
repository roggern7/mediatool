"""Proteção do Instagram (docs/BATCH_ROBUSTNESS.md, seção 9: A2, M2, M5). Offline, valores FAKE, relógio simulado.

- A2: janela de prova do backoff, ritmo lento da listagem por 1 h depois de um 429 (persistido) e teto de páginas/h.
- M2: WebState do bootstrap reaproveitado entre trechos (1 GET em vez de 3) e invalidado após recusa de token.
- M5: composite sem fallback quando há ``resume``.
"""

from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest

from app.integrations.instagram.composite import CompositeInstagramDownloader
from app.integrations.instagram.downloader import InstagramError, ListingCheckpoint, ListingRef
from app.integrations.instagram.web.graphql import DEFAULT_PAGE_DELAY
from app.integrations.instagram.web.provider import InstagramWebDownloader, WebStateCache
from app.services.instagram_gate import SLOW_PAGE_DELAY, GateCancelled, InstagramRequestGate
from app.services.instagram_recovery import (
    AUTH_FAIL_STREAK_KEY,
    PROBATION_KEY,
    SLOW_PACE_KEY,
    InstagramRecoveryController,
)
from test_auth_escalation import LazyPagedDownloader, assert_session_intact, make_harness
from test_instagram_gate_requests import FakeProvider, GatedInstagram
from test_recovery_controller import CountingVerify, expire, fail, ok, remaining

MIN = 60.0
HOUR = 3600.0


class MonoClock:
    """Relógio monotônico simulado do portão; ``sleep`` avança o relógio."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


@pytest.fixture
def env(harness_factory, tmp_path):
    h, provider, digest, clock = make_harness(harness_factory, tmp_path, LazyPagedDownloader(50))
    yield h, clock
    assert_session_intact(provider, digest)


# --------------------------------------------------------------------------- A2: janela de prova


def test_429_inside_probation_window_climbs_a_step(env):
    h, clock = env
    worker = h.ctx.worker
    worker.recovery.verify = CountingVerify(ok())
    assert worker.pause_for("rate_limited") is True
    assert worker.recovery.state().level == 0 and round(remaining(h, clock)) == 15 * MIN
    expire(h, clock)
    assert worker.recovery.tick() == "ok"
    assert worker.pause_state() == (None, None) and worker.recovery.state() is None
    assert h.ctx.app_settings.get_state(PROBATION_KEY) is not None, "janela de prova guardada"

    clock.advance(minutes=20)   # dentro da janela (2 x 15 min)
    worker.pause_for("rate_limited")
    state = worker.recovery.state()
    assert state.level == 1 and round(remaining(h, clock)) == 30 * MIN, "subiu um degrau, não voltou a 15 min"

    expire(h, clock)
    assert worker.recovery.tick() == "ok"
    clock.advance(minutes=59)   # janela agora é 2 x 30 min
    worker.pause_for("rate_limited")
    assert worker.recovery.state().level == 2 and round(remaining(h, clock)) == HOUR


def test_429_after_probation_window_starts_over(env):
    h, clock = env
    worker = h.ctx.worker
    worker.recovery.verify = CountingVerify(fail("rate_limited"), ok())
    worker.pause_for("rate_limited")
    expire(h, clock)
    assert worker.recovery.tick() == "failed"
    assert worker.recovery.state().level == 1
    expire(h, clock)
    assert worker.recovery.tick() == "ok"
    clock.advance(minutes=61)   # fora da janela (2 x 30 min) sem falhas
    worker.pause_for("rate_limited")
    state = worker.recovery.state()
    assert state.level == 0 and round(remaining(h, clock)) == 15 * MIN
    assert h.ctx.app_settings.get_state(PROBATION_KEY) is None


def test_auth_failure_cycles_inside_window_climb_without_session_invalid(env):
    h, clock = env
    worker = h.ctx.worker
    worker.recovery.verify = CountingVerify(ok())
    seen = []
    for _ in range(4):   # falha -> verificação ok -> nova falha dentro da janela
        assert worker.pause_for("request_auth_failed") is True
        seen.append((round(remaining(h, clock)), worker.pause_state()[1]))
        expire(h, clock)
        assert worker.recovery.tick() == "ok"
        assert h.ctx.app_settings.get_state(AUTH_FAIL_STREAK_KEY) is None, "sucesso zera o contador da seção 7"
        clock.advance(minutes=5)
    assert seen == [(15 * MIN, "auth_check"), (30 * MIN, "auth_check"), (HOUR, "auth_check"),
                    (2 * HOUR, "auth_check")], seen

    # Fora da janela (2 x 2 h) sem falhas: volta a 15 min.
    clock.advance(hours=4)
    worker.pause_for("request_auth_failed")
    assert round(remaining(h, clock)) == 15 * MIN and worker.pause_state()[1] == "auth_check"


def test_auth_escalation_to_session_invalid_still_follows_section_7(env):
    h, clock = env
    worker = h.ctx.worker
    worker.recovery.verify = CountingVerify(ok(), fail("request_auth_failed"))
    worker.pause_for("request_auth_failed")
    expire(h, clock)
    assert worker.recovery.tick() == "ok"
    worker.pause_for("request_auth_failed")   # dentro da janela: degrau sobe, mas ainda auth_check
    assert worker.pause_state()[1] == "auth_check" and round(remaining(h, clock)) == 30 * MIN
    expire(h, clock)
    assert worker.recovery.tick() == "failed"   # 2a recusa seguida sem sucesso entre elas
    assert worker.pause_state()[1] == "session_invalid" and round(remaining(h, clock)) == HOUR
    state = InstagramRecoveryController(h.ctx, worker, clock=clock).reload()   # sobrevive a reinício
    assert state.pause_reason == "session_invalid" and state.level == 2


def test_manual_release_forgets_probation(env):
    h, clock = env
    worker = h.ctx.worker
    worker.recovery.verify = CountingVerify(ok())
    worker.pause_for("rate_limited")
    expire(h, clock)
    assert worker.recovery.tick() == "ok"
    assert h.ctx.app_settings.get_state(PROBATION_KEY) is not None
    worker.clear_pause()   # liberação manual esquece o backoff, inclusive a janela de prova
    assert h.ctx.app_settings.get_state(PROBATION_KEY) is None
    worker.pause_for("rate_limited")
    assert worker.recovery.state().level == 0


# --------------------------------------------------------------------------- A2: ritmo lento por 1 h


def test_slow_pace_after_429_for_one_hour_and_survives_restart(env):
    h, clock = env
    worker = h.ctx.worker
    mono = MonoClock()
    gate = InstagramRequestGate(interval=(0, 0), clock=mono, sleep=mono.sleep)
    h.ctx.instagram_gate = gate
    assert gate.listing_page_delay(DEFAULT_PAGE_DELAY) == DEFAULT_PAGE_DELAY
    worker.pause_for("rate_limited")
    assert gate.listing_page_delay(DEFAULT_PAGE_DELAY) == SLOW_PAGE_DELAY
    persisted = h.ctx.app_settings.get_state(SLOW_PACE_KEY)
    assert persisted is not None
    until = datetime.fromisoformat(persisted.replace("Z", "+00:00"))
    assert round((until - clock.now).total_seconds()) == HOUR

    # O provider monta o GraphQL da operação com o ritmo lento.
    dl = InstagramWebDownloader(FakeProvider(), request_gate=gate)
    assert dl._graphql_factory(object(), object())._page_delay == SLOW_PAGE_DELAY

    mono.t += HOUR + 1
    assert gate.listing_page_delay(DEFAULT_PAGE_DELAY) == DEFAULT_PAGE_DELAY, "volta ao normal depois de 1 h"
    assert dl._graphql_factory(object(), object())._page_delay == DEFAULT_PAGE_DELAY

    # Reinício 30 min depois do 429: o restante (30 min) é reaplicado num portão novo.
    clock.advance(minutes=30)
    mono2 = MonoClock()
    gate2 = InstagramRequestGate(interval=(0, 0), clock=mono2, sleep=mono2.sleep)
    h.ctx.instagram_gate = gate2
    InstagramRecoveryController(h.ctx, worker, clock=clock).reload()
    assert gate2.listing_slowed()
    mono2.t += 29 * MIN
    assert gate2.listing_slowed()
    mono2.t += 2 * MIN
    assert not gate2.listing_slowed()

    # Depois do prazo, um novo reload não reaplica e limpa o estado.
    clock.advance(minutes=31)
    gate3 = InstagramRequestGate(interval=(0, 0), clock=MonoClock())
    h.ctx.instagram_gate = gate3
    InstagramRecoveryController(h.ctx, worker, clock=clock).reload()
    assert not gate3.listing_slowed() and h.ctx.app_settings.get_state(SLOW_PACE_KEY) is None


# --------------------------------------------------------------------------- A2: teto de páginas por hora


def test_page_cap_per_hour_waits_for_window():
    mono = MonoClock()
    gate = InstagramRequestGate(interval=(0, 0), clock=mono, sleep=mono.sleep)
    for _ in range(3):
        gate.wait_listing_page(max_per_hour=3)
        mono.t += 10
    assert mono.slept == []
    gate.wait_listing_page(max_per_hour=3)   # 4a página na mesma hora: espera a 1a sair da janela
    assert mono.slept == [HOUR - 30]
    assert mono.t == 1000.0 + HOUR


def test_page_cap_default_and_cancel():
    import threading

    mono = MonoClock()
    gate = InstagramRequestGate(interval=(0, 0), clock=mono, sleep=mono.sleep)
    for _ in range(600):   # padrão: 600 páginas por hora (antes 300)
        gate.wait_listing_page()
    assert mono.slept == []
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(GateCancelled):
        gate.wait_listing_page(cancel)


class RecordingGate:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def listing_page_delay(self, default):
        return default

    def wait_listing_page(self, cancel=None, *, max_per_hour):
        self.calls.append(max_per_hour)


class PagedGraphQL:
    current_page = None
    last_viewer_id = None
    listing_account_confirmation = None

    def iter_profile_posts(self, username, *, max_pages, before_page=None, cancel=None, **kwargs):
        for _ in range(max_pages):
            if before_page is not None:
                before_page(cancel)
        return iter(())


class NoopBootstrap:
    def get(self, *, force=False):
        return object()

    def invalidate(self):
        pass


def test_provider_applies_configurable_page_cap_before_each_page():
    gate = RecordingGate()
    dl = InstagramWebDownloader(FakeProvider(), request_gate=gate, env={"MEDIA_TOOL_IG_MAX_PAGES_PER_HOUR": "5"},
                                http_factory=lambda session: object(), bootstrap_factory=lambda http: NoopBootstrap(),
                                graphql_factory=lambda http, bootstrap: PagedGraphQL(), max_pages=4)
    assert list(dl.iter_profile_media("perfil_fake", "all", 10)) == []
    assert gate.calls == [5, 5, 5, 5]


# --------------------------------------------------------------------------- M2: bootstrap reaproveitado


class RefusingInstagram(GatedInstagram):
    """Igual ao GatedInstagram; com ``refuse_next`` o próximo POST recusa o token (1357004)."""

    refuse_next = False

    def _respond(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/graphql/query" and self.refuse_next:
            self.refuse_next = False
            return httpx.Response(200, text=json.dumps({"error": 1357004, "errorSummary": "Recarregue"}),
                                  headers={"content-type": "application/json"})
        return super()._respond(request)


def _gets(mock: GatedInstagram) -> int:
    return sum(1 for span in mock.spans if span[2] == "/")


def _posts(mock: GatedInstagram) -> int:
    return sum(1 for span in mock.spans if span[2] == "/graphql/query")


def test_bootstrap_reused_across_chunks_and_invalidated_on_refusal():
    mock = RefusingInstagram()
    gate = InstagramRequestGate(interval=(0, 0))
    cache = WebStateCache()

    def new_downloader() -> InstagramWebDownloader:   # um downloader por trecho, como o downloader_factory do app
        return InstagramWebDownloader(FakeProvider(), transport=mock.transport, page_delay=(0, 0),
                                      request_gate=gate, bootstrap_cache=cache, max_pages=1)

    assert len(list(new_downloader().iter_profile_media("perfil_fake", "all", 12))) == 12
    for index in (2, 3):
        ref = ListingRef(page_index=index, cursor=f"FAKE_CURSOR_{index - 1}", page_size=12)
        assert len(new_downloader().refresh_page("perfil_fake", ref)) == 12
    assert (_gets(mock), _posts(mock)) == (1, 3), "1 GET do documento para 3 trechos"

    mock.refuse_next = True
    ref = ListingRef(page_index=4, cursor="FAKE_CURSOR_3", page_size=12)
    assert len(new_downloader().refresh_page("perfil_fake", ref)) == 12
    assert (_gets(mock), _posts(mock)) == (2, 5), "recusa invalida o cache e refaz o bootstrap uma vez"
    assert len(new_downloader().refresh_page("perfil_fake", ref)) == 12
    assert _gets(mock) == 2, "o estado renovado volta a ser reaproveitado"


def test_bootstrap_cache_ttl_and_session_key():
    mono = MonoClock()
    cache = WebStateCache(ttl=600, clock=mono)
    state = object()
    cache.put("a", state)
    assert cache.get("a") is state and cache.get("b") is None
    mono.t += 599
    assert cache.get("a") is state
    mono.t += 1
    assert cache.get("a") is None, "no máximo 10 min"
    cache.put("a", state)
    cache.invalidate("b")
    assert cache.get("a") is state
    cache.invalidate("a")
    assert cache.get("a") is None


def test_session_test_does_not_use_the_shared_cache():
    mock = GatedInstagram()
    cache = WebStateCache()
    dl = InstagramWebDownloader(FakeProvider(), transport=mock.transport, page_delay=(0, 0),
                                request_gate=InstagramRequestGate(interval=(0, 0)), bootstrap_cache=cache)
    ref = ListingRef(page_index=2, cursor="FAKE_CURSOR_1", page_size=12)
    dl.refresh_page("perfil_fake", ref)
    dl.test_session()
    assert _gets(mock) == 2, "o teste de sessão faz o próprio bootstrap"


# --------------------------------------------------------------------------- M5: composite com resume


class FailingPrimary:
    def __init__(self) -> None:
        self.calls = 0

    def iter_profile_media(self, username, content, limit, *, cancel=None, **extra):
        self.calls += 1

        def gen():
            raise InstagramError("contract_changed")
            yield  # pragma: no cover

        return gen()


class RecordingFallback:
    def __init__(self) -> None:
        self.calls = 0

    def iter_profile_media(self, username, content, limit, *, cancel=None, **extra):
        self.calls += 1
        return iter(())


def _checkpoint() -> ListingCheckpoint:
    return ListingCheckpoint(resume_cursor="FAKE_CURSOR_7", next_page_index=8, pages=7, posts_seen=84,
                             items_compatible=84, unproductive_streak=0, has_next=True)


def test_composite_auto_with_resume_does_not_fall_back():
    primary, fallback = FailingPrimary(), RecordingFallback()
    composite = CompositeInstagramDownloader(primary, fallback, strategy="auto")
    with pytest.raises(InstagramError) as info:
        list(composite.iter_profile_media("perfil_fake", "all", 10, resume=_checkpoint()))
    assert info.value.code == "contract_changed"
    assert fallback.calls == 0

    # Sem resume, o fallback único de `auto` continua valendo.
    assert list(composite.iter_profile_media("perfil_fake", "all", 10)) == []
    assert fallback.calls == 1
