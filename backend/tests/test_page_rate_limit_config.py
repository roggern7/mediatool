"""INSTAGRAM_PAGE_RATE_LIMIT: teto de páginas de listagem por hora configurável (docs/BATCH_ROBUSTNESS.md, seção 9).

Padrão 600/h (antes 300); ``0`` = sem teto horário (só POC de escala). Muda SOMENTE o teto por hora: intervalo entre requisições,
1 em voo, backpressure, pausas, 429, ritmo lento, orçamento de páginas e checkpoint continuam iguais.
100% offline, valores FAKE, relógio simulado (sem rede, sem porta 8765, sem data\\secrets reais).
"""

from __future__ import annotations

import json
import logging
import time

import pytest

import app.integrations.instagram.web.provider as provider_mod
from app.core.config import load_settings
from app.integrations.instagram.downloader import ListingRef
from app.integrations.instagram.web.provider import ENV_PAGE_RATE_LIMIT, InstagramWebDownloader, page_rate_limit
from app.jobs.import_runner import MAX_PENDING_INGEST, RESUME_PENDING_INGEST
from app.db.migrate import apply_migrations
from app.main import build_context
from app.services.instagram_gate import PAGES_STATE_KEY, SLOW_PAGE_DELAY, InstagramPaused, InstagramRequestGate
from media_fakes import MemorySessionProvider
from test_instagram_gate_requests import FakeProvider, GatedInstagram, _run
from test_protection_robust import MonoClock, NoopBootstrap, PagedGraphQL, RecordingGate
from test_review_batch_robustness import Feed, FakeCdn, SessionWithCookie
from test_review_section9 import (  # noqa: F401 - `sim` é fixture
    Outage,
    V,
    drive,
    expire,
    internal_web_batch,
    lrow,
    ok_result,
    sim,
    web_harness,
)

ZERO = {ENV_PAGE_RATE_LIMIT: "0"}
LEGACY = "MEDIA_TOOL_IG_MAX_PAGES_PER_HOUR"


@pytest.fixture(autouse=True)
def _restore_disabled_flag(monkeypatch):
    """A flag de módulo do aviso único é restaurada: nenhum teste depende da ordem."""
    monkeypatch.setattr(provider_mod, "_rate_limit_disabled_logged", False)


def _calls(env, pages: int = 3) -> list[int]:
    """Teto passado a ``wait_listing_page`` antes de cada página (provider real, GraphQL falso)."""
    gate = RecordingGate()
    dl = InstagramWebDownloader(FakeProvider(), request_gate=gate, env=env, http_factory=lambda session: object(),
                                bootstrap_factory=lambda http: NoopBootstrap(),
                                graphql_factory=lambda http, bootstrap: PagedGraphQL(), max_pages=pages)
    assert list(dl.iter_profile_media("perfil_fake", "all", 10)) == []
    return gate.calls


# --------------------------------------------------------------------------- 1. padrão e valores


@pytest.mark.parametrize("env, expected", [
    (None, 600),
    ({}, 600),
    ({ENV_PAGE_RATE_LIMIT: "300"}, 300),
    ({ENV_PAGE_RATE_LIMIT: "5"}, 5),
    ({ENV_PAGE_RATE_LIMIT: " 7 "}, 7),
    ({ENV_PAGE_RATE_LIMIT: "100000"}, 100000),
    ({ENV_PAGE_RATE_LIMIT: "5", LEGACY: "9"}, 5),            # a nova prevalece
    ({ENV_PAGE_RATE_LIMIT: "0", LEGACY: "9"}, 0),            # a nova prevalece, inclusive o 0
    ({LEGACY: "9"}, 9),                                      # só a antiga: comportamento anterior
    ({ENV_PAGE_RATE_LIMIT: "", LEGACY: "9"}, 9),             # vazia = ausente
    ({ENV_PAGE_RATE_LIMIT: "abc"}, 600),
    ({ENV_PAGE_RATE_LIMIT: "-1"}, 600),
    ({ENV_PAGE_RATE_LIMIT: "1.5"}, 600),
    ({ENV_PAGE_RATE_LIMIT: ""}, 600),
    ({ENV_PAGE_RATE_LIMIT: "   "}, 600),
    ({ENV_PAGE_RATE_LIMIT: "100001"}, 600),
    ({ENV_PAGE_RATE_LIMIT: "0x10"}, 600),
    ({ENV_PAGE_RATE_LIMIT: "abc", LEGACY: "9"}, 600),        # inválida nunca desabilita nem herda por engano
])
def test_page_rate_limit_value(env, expected):
    assert page_rate_limit(env) == expected


@pytest.mark.parametrize("env, expected", [
    ({}, 600),
    ({ENV_PAGE_RATE_LIMIT: "300"}, 300),
    ({ENV_PAGE_RATE_LIMIT: "5"}, 5),
    ({ENV_PAGE_RATE_LIMIT: "5", LEGACY: "9"}, 5),
    ({ENV_PAGE_RATE_LIMIT: "abc"}, 600),
    ({ENV_PAGE_RATE_LIMIT: "-1"}, 600),
    ({ENV_PAGE_RATE_LIMIT: "1.5"}, 600),
    ({ENV_PAGE_RATE_LIMIT: ""}, 600),
    ({ENV_PAGE_RATE_LIMIT: "  "}, 600),
])
def test_provider_calls_gate_with_the_cap_before_each_page(env, expected):
    assert _calls(env) == [expected] * 3


def test_zero_never_calls_wait_listing_page_and_default_is_unchanged():
    assert _calls(ZERO, pages=4) == []
    assert _calls({}, pages=4) == [600] * 4


def test_variable_reaches_the_provider_through_settings_env(monkeypatch, tmp_path):
    """Ambiente do processo e .env chegam a ``settings.env`` e ao ``_env`` do provider do app real (sem filtro)."""
    monkeypatch.setenv(ENV_PAGE_RATE_LIMIT, "0")
    settings = load_settings(root=tmp_path)
    assert settings.env[ENV_PAGE_RATE_LIMIT] == "0" and page_rate_limit(settings.env) == 0
    ctx = build_context(settings, session_provider=MemorySessionProvider())
    apply_migrations(ctx.db)
    web = ctx.downloader_factory().primary
    assert isinstance(web, InstagramWebDownloader) and page_rate_limit(web._env) == 0

    monkeypatch.delenv(ENV_PAGE_RATE_LIMIT)
    (tmp_path / ".env").write_text(f"{ENV_PAGE_RATE_LIMIT}=0\n", encoding="utf-8")
    assert page_rate_limit(load_settings(root=tmp_path).env) == 0, ".env também vale"
    monkeypatch.setenv(ENV_PAGE_RATE_LIMIT, "5")
    assert page_rate_limit(load_settings(root=tmp_path).env) == 5, "ambiente do processo > .env"
    (tmp_path / ".env").unlink()
    monkeypatch.delenv(ENV_PAGE_RATE_LIMIT)
    assert page_rate_limit(load_settings(root=tmp_path).env) == 600


# --------------------------------------------------------------------------- 7. aviso uma vez só


def test_disabled_warning_is_logged_once_per_process(caplog):
    caplog.set_level(logging.WARNING)
    _calls(ZERO)
    _calls(ZERO)
    _calls({ENV_PAGE_RATE_LIMIT: "0", LEGACY: "9"})
    warnings = [r for r in caplog.records if "DESABILITADO" in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
    assert ENV_PAGE_RATE_LIMIT in warnings[0].getMessage()


def test_no_disabled_warning_for_default_positive_or_invalid(caplog):
    caplog.set_level(logging.WARNING)
    for env in ({}, {ENV_PAGE_RATE_LIMIT: "300"}, {ENV_PAGE_RATE_LIMIT: "5"}, {ENV_PAGE_RATE_LIMIT: "abc"},
                {ENV_PAGE_RATE_LIMIT: "-1"}):
        _calls(env)
    assert not [r for r in caplog.records if "DESABILITADO" in r.getMessage()]
    assert provider_mod._rate_limit_disabled_logged is False
    assert [r for r in caplog.records if "inválido" in r.getMessage()], "valor inválido avisa e usa 600"


# --------------------------------------------------------------------------- 2. com 0 não há espera nem reserva


def _web(mock, gate, *, env, pages: int = 6) -> InstagramWebDownloader:
    return InstagramWebDownloader(FakeProvider(), transport=mock.transport, page_delay=(0, 0), request_gate=gate,
                                  max_pages=pages, env=env)


def test_zero_lists_more_pages_than_a_full_window_allows_without_waiting_or_reserving():
    clk = MonoClock()
    wall = time.time()
    # Janela cheia persistida de uma execução anterior (600 páginas agora): com teto, a listagem esperaria ~1 h.
    full = json.dumps([wall] * 600)

    def run(env):
        saved: list[str] = []
        gate = InstagramRequestGate(interval=(0, 0), clock=clk, sleep=clk.sleep, save_pages=saved.append)
        assert gate.restore_listing_pages(full) == 600
        mock = GatedInstagram()
        assert len(list(_web(mock, gate, env=env).iter_profile_media("perfil_fake", "all", None))) == 6 * 12
        return gate, saved

    clk.slept.clear()
    gate, saved = run(ZERO)
    assert clk.slept == [] and saved == [], "sem espera e nada gravado em instagram_listing_pages"
    assert len(gate._pages) == 600, "nenhuma reserva nova na janela"

    gate, saved = run({})   # controle: o padrão (600/h) com a janela cheia espera e reserva
    assert clk.slept and clk.slept[0] > 3000
    assert saved, "o controle prova que a gravação funciona"


def test_zero_batch_never_waits_rate_limit_and_ignores_a_full_persisted_window(harness_factory, sim):
    saved: list[str] = []
    gate = InstagramRequestGate(interval=(0.0, 0.0), clock=sim.mono, sleep=sim.sleep, save_pages=saved.append)
    feed, cdn, outage = Feed(["img"] * 800), FakeCdn(), Outage()
    outage.sim = sim
    feed.override = outage.override
    h = web_harness(harness_factory, feed, cdn, sim, gate=gate, env=ZERO)
    assert gate.restore_listing_pages(json.dumps([time.time()] * 300)) == 300
    assert gate.listing_window(300)[1] is not None, "pré-condição: janela cheia"
    batch_id = internal_web_batch(h, 800)

    for _ in range(600):
        h.run_imports()
        h.run_jobs()
        api = h.client.get(f"/api/batches/{batch_id}").json()
        assert api["listing_state"] != "waiting_rate_limit"
        assert (api["rate_limit_resume_at"], api["rate_limit_pages_used"], api["rate_limit_pages_max"]) == (
            None, None, None)
        assert lrow(h, batch_id)["rate_limit_resume_at"] is None
        if api["status"] in ("completed", "failed", "cancelled"):
            break
    final = h.client.get(f"/api/batches/{batch_id}").json()
    assert final["status"] == "completed" and final["found"] == 800
    assert len(outage.stamps) >= 60, "mais páginas do que qualquer teto pequeno permitiria"
    assert sim.sleeps == [] and sim.offset == 0.0, "nenhuma espera, nem a do teto"
    assert saved == [] and h.ctx.app_settings.get_state(PAGES_STATE_KEY) is None
    assert len(gate._pages) == 300


def test_control_with_a_cap_the_same_batch_does_reserve_and_wait(harness_factory, sim):
    saved: list[str] = []
    gate = InstagramRequestGate(interval=(0.0, 0.0), clock=sim.mono, sleep=sim.sleep, save_pages=saved.append)
    feed, cdn, outage = Feed(["img"] * 100), FakeCdn(), Outage()
    outage.sim = sim
    feed.override = outage.override
    h = web_harness(harness_factory, feed, cdn, sim, gate=gate, env={ENV_PAGE_RATE_LIMIT: "3"})
    batch_id = internal_web_batch(h, 100)
    assert drive(h, batch_id)["status"] == "completed"
    assert saved and sim.sleeps and max(sim.sleeps) > 3000, "com teto 3 a listagem espera a janela"


def test_stale_wait_fields_are_cleared_at_startup_when_cap_is_disabled(harness_factory):
    h = harness_factory()
    batch_id = h.import_profile(limit=100)["id"]
    from app.db.connection import utc_now

    h.ctx.app_settings.set_state(PAGES_STATE_KEY, json.dumps([time.time()] * 600))   # janela cheia, de agora
    h.ctx.batches.update_listing(batch_id, listing_state="running", rate_limit_resume_at=utc_now(1800),
                                 rate_limit_pages_used=600, rate_limit_pages_max=600)

    def api(app):
        return app.client.get(f"/api/batches/{batch_id}").json()

    default = harness_factory(root=h.root)   # reinício com teto padrão: a janela cheia mantém a espera
    assert api(default)["listing_state"] == "waiting_rate_limit"

    off = harness_factory(root=h.root, env=dict(ZERO))   # reinício com teto desabilitado
    body = api(off)
    assert body["listing_state"] == "running", body
    assert (body["rate_limit_resume_at"], body["rate_limit_pages_used"], body["rate_limit_pages_max"]) == (
        None, None, None)
    row = dict(off.ctx.db.fetchone(
        "SELECT rate_limit_resume_at, rate_limit_pages_used, rate_limit_pages_max FROM batches WHERE id = ?",
        (batch_id,)))
    assert set(row.values()) == {None}
    off.run_imports()
    assert api(off)["listing_state"] == "done", "o lote lista normalmente, sem esperar"


# --------------------------------------------------------------------------- 3. intervalo entre requisições


def test_zero_keeps_the_normal_interval_between_requests_exactly_as_default():
    def run(env):
        clk = MonoClock()
        draws = iter(2.0 + 0.1 * i for i in range(1000))
        gate = InstagramRequestGate(interval=(2.0, 4.0), clock=clk, sleep=clk.sleep, rng=lambda lo, hi: next(draws))
        mock = GatedInstagram()
        assert len(list(_web(mock, gate, env=env).iter_profile_media("perfil_fake", "all", None))) == 6 * 12
        return clk.slept, len(mock.spans)

    default, requests = run({})
    zero, requests_zero = run(ZERO)
    assert requests == requests_zero == 1 + 6   # 1 GET do bootstrap + 6 POSTs
    assert len(default) == requests - 1, "uma espera de intervalo entre requisições consecutivas"
    assert default == pytest.approx([2.0 + 0.1 * i for i in range(requests - 1)]), "o intervalo sorteado inteiro"
    assert zero == default, "mesmas esperas, mesmos valores, com e sem a variável"


# --------------------------------------------------------------------------- 4. concorrência


def test_zero_keeps_at_most_one_request_in_flight_with_competing_threads():
    gate = InstagramRequestGate(interval=(0.003, 0.003))
    mock = GatedInstagram()
    results: dict = {}

    def listing(name):
        return lambda: len(list(_web(mock, gate, env=ZERO, pages=8).iter_profile_media("perfil_fake", "all", None)))

    def renewals():
        out = []
        for i in range(3):
            ref = ListingRef(page_index=10 + i, cursor=f"FAKE_CURSOR_{9 + i}", page_size=12)
            out.append(len(_web(mock, gate, env=ZERO).refresh_page("perfil_fake", ref)))
        return out

    threads = [_run("listing_a", listing("a"), results), _run("listing_b", listing("b"), results),
               _run("renew", renewals, results)]
    for thread in threads:
        thread.join(60)
        assert not thread.is_alive(), f"deadlock em {thread.name}"
    assert results["listing_a"] == results["listing_b"] == 8 * 12 and results["renew"] == [12, 12, 12]
    assert len(mock.spans) == 2 * (1 + 8) + 3 * 2
    assert mock.max_in_flight == 1
    assert len(gate._pages) == 0


# --------------------------------------------------------------------------- 5. backpressure


def test_zero_keeps_backpressure(harness_factory, sim):
    feed, cdn = Feed(["img"] * 300), FakeCdn()
    h = web_harness(harness_factory, feed, cdn, sim, env=ZERO)
    batch_id = internal_web_batch(h, 300)
    h.run_imports()
    assert lrow(h, batch_id)["listing_state"] == "waiting_backpressure"
    pending = h.ctx.jobs.pending_ingest_count(batch_id)
    assert RESUME_PENDING_INGEST < pending <= MAX_PENDING_INGEST
    stopped_at = len(feed.posts_vars)
    assert 0 < stopped_at < 300 // 12
    h.run_imports()
    assert len(feed.posts_vars) == stopped_at, "downloads parados: nenhuma página nova"

    h.ctx.worker.run_pending(max_jobs=pending - RESUME_PENDING_INGEST)   # pendentes == limite de retomada
    assert h.ctx.jobs.pending_ingest_count(batch_id) == RESUME_PENDING_INGEST
    h.run_imports()
    assert len(feed.posts_vars) == stopped_at, "só retoma ABAIXO do limite de retomada"
    h.ctx.worker.run_pending(max_jobs=1)
    h.run_imports()
    assert len(feed.posts_vars) > stopped_at, "abaixo do limite a listagem pede a página seguinte"
    peak = 0
    for _ in range(200):
        h.run_imports()
        peak = max(peak, h.ctx.jobs.pending_ingest_count(batch_id))
        h.ctx.worker.run_pending(max_jobs=25)
        if h.ctx.batches.get(batch_id)["status"] == "completed":
            break
    assert peak <= MAX_PENDING_INGEST and h.batch(batch_id)["found"] == 300
    assert sim.sleeps == []


# --------------------------------------------------------------------------- 6. as demais proteções


def test_zero_gate_still_refuses_the_next_page_when_instagram_is_paused():
    gate = InstagramRequestGate(interval=(0, 0))
    mock = GatedInstagram()
    dl = _web(mock, gate, env=ZERO, pages=10)
    stream = dl.iter_profile_media("perfil_fake", "all", None)
    first = next(stream)   # a página 1 foi pedida e entregue
    assert first is not None
    gate.publish_pause("instagram")
    with pytest.raises(InstagramPaused):
        list(stream)
    assert [s[2] for s in mock.spans].count("/graphql/query") == 1, "nenhuma página depois da pausa publicada"


def test_zero_pause_after_page_keeps_checkpoint_and_refuses_next_page(harness_factory, sim):
    feed, cdn = Feed(["img"] * 600), FakeCdn()
    h = web_harness(harness_factory, feed, cdn, sim, env=ZERO)
    batch_id = internal_web_batch(h, 600)
    seen_next_page: list[int | None] = []

    def override(index, variables, request):
        seen_next_page.append(lrow(h, batch_id)["listing_next_page"])   # checkpoint gravado ANTES de cada POST
        if index == 1:
            h.ctx.worker.pause_for("rate_limited")   # a pausa é publicada durante a 2a página
        return None

    feed.override = override
    h.run_imports()
    assert len(feed.posts_vars) == 2, "a 3a página foi recusada na admissão"
    assert seen_next_page == [None, 2], "checkpoint por página: a página 1 gravou next_page=2 antes da 2a"
    row = lrow(h, batch_id)
    assert row["listing_state"] == "paused" and row["status"] == "listing"
    assert row["listing_next_page"] == 3 and row["listing_cursor"] and row["listing_pages"] == 2, "checkpoint mantido"
    assert row["rate_limit_resume_at"] is None
    assert h.ctx.worker.pause_state()[1] == "rate_limited"
    h.run_imports()
    assert len(feed.posts_vars) == 2, "pausa vigente: nada vai ao Instagram"


def test_zero_429_pauses_and_slow_pace_still_applies_for_one_hour(harness_factory, sim):
    feed, cdn, outage = Feed(["img"] * 600), FakeCdn(), Outage()
    outage.sim, outage.mode, outage.ok_left = sim, "429", 4
    feed.override = outage.override
    h = web_harness(harness_factory, feed, cdn, sim, env=ZERO)
    worker = h.ctx.worker
    batch_id = internal_web_batch(h, 600)
    h.run_imports()
    assert sim.sleeps == [], "antes do 429 o ritmo é normal e não há espera de teto"
    assert worker.pause_state()[1] == "rate_limited" and lrow(h, batch_id)["listing_state"] == "paused"
    assert lrow(h, batch_id)["listing_next_page"] == 5, "checkpoint mantido: 4 páginas boas, a 5a recusada"
    assert h.ctx.instagram_gate.listing_slowed()
    posts = len(feed.posts_vars)
    h.run_imports()
    assert len(feed.posts_vars) == posts, "pausa vigente"

    worker.recovery.verify = V(ok_result())
    expire(h, sim)
    assert worker.recovery.tick() == "ok"
    outage.mode = "ok"
    h.run_imports()
    assert len(feed.posts_vars) > posts and sim.sleeps
    assert all(SLOW_PAGE_DELAY[0] <= s <= SLOW_PAGE_DELAY[1] for s in sim.sleeps), sim.sleeps
    sim.advance(3601)
    count = len(sim.sleeps)
    h.run_imports()
    assert len(sim.sleeps) == count, "passada 1 h o ritmo volta ao normal"
    final = drive(h, batch_id)
    assert final["status"] == "completed"


def test_zero_keeps_max_pages_and_lists_to_the_real_end_like_default():
    def posts(env, content, pages):
        mock = GatedInstagram()
        dl = _web(mock, InstagramRequestGate(interval=(0, 0)), env=env, pages=pages)
        list(dl.iter_profile_media("perfil_fake", content, None))
        return sum(1 for s in mock.spans if s[2] == "/graphql/query")

    assert posts(ZERO, "all", 4) == posts({}, "all", 4) == 4, "max_pages (teto do trecho, só testes) continua valendo"

    def unproductive(env):
        feed = Feed(["img"] * 3000)   # só imagens: pedir "videos" não rende nada
        dl = InstagramWebDownloader(SessionWithCookie(), transport=feed.transport(), page_delay=(0, 0),
                                    request_gate=InstagramRequestGate(interval=(0, 0)), env=env)
        list(dl.iter_profile_media("perfil_fake", "videos", None))
        return len(feed.posts_vars)

    # Não existe mais corte por baixo rendimento: com e sem a variável a listagem vai até o fim real do perfil.
    default, zero = unproductive({}), unproductive(ZERO)
    assert zero == default == 3000 // 12


def test_startup_uses_the_cap_in_force_not_the_one_stored_in_the_batch(harness_factory):
    """Atualização 300 -> 600 com um lote que estava esperando: a janela tem 300 páginas (cheia pelo teto antigo,
    com vaga pelo novo). A inicialização decide pelo teto VIGENTE: o lote não aparece `waiting_rate_limit`."""
    from app.db.connection import utc_now

    h = harness_factory()
    batch_id = h.import_profile(limit=100)["id"]
    h.ctx.app_settings.set_state(PAGES_STATE_KEY, json.dumps([time.time()] * 300))
    h.ctx.batches.update_listing(batch_id, listing_state="running", rate_limit_resume_at=utc_now(3000),
                                 rate_limit_pages_used=300, rate_limit_pages_max=300)
    assert h.client.get(f"/api/batches/{batch_id}").json()["listing_state"] == "waiting_rate_limit"

    upgraded = harness_factory(root=h.root)   # reinício com o padrão novo (600)
    body = upgraded.client.get(f"/api/batches/{batch_id}").json()
    assert body["listing_state"] == "running" and body["rate_limit_resume_at"] is None
    assert upgraded.ctx.batches.get(batch_id)["rate_limit_pages_max"] is None

    lower = harness_factory(root=h.root, env={ENV_PAGE_RATE_LIMIT: "200"})   # teto vigente menor: continua cheia
    h.ctx.batches.update_listing(batch_id, rate_limit_resume_at=utc_now(3000), rate_limit_pages_used=300,
                                 rate_limit_pages_max=300)
    lower.ctx.import_runner.restore_rate_limit_wait()
    body = lower.client.get(f"/api/batches/{batch_id}").json()
    assert (body["listing_state"], body["rate_limit_pages_used"], body["rate_limit_pages_max"]) == (
        "waiting_rate_limit", 300, 200)
