"""Admissão no portão x publicação da pausa (docs/BATCH_ROBUSTNESS.md, seção 8; revisão de concorrência).

Garantia: depois que o controlador PUBLICA a pausa (escopo "instagram" ou "session"), nenhuma requisição nova ao
instagram.com é admitida; a que já estava em voo termina e é contabilizada; só a verificação do controlador passa
durante a pausa vencida; depois de liberada, a admissão volta. Offline, valores FAKE, sem a porta 8765.
"""

from __future__ import annotations

import itertools
import json
import random
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime
from urllib.parse import parse_qsl

import httpx
import pytest

from app.integrations.instagram.downloader import ListingRef
from app.integrations.instagram.web.provider import ENV_MAX_PAGES_PER_HOUR, InstagramWebDownloader
from app.services.cdn_downloader import DownloadError
from app.services.instagram_gate import InstagramPaused, InstagramRequestGate
from conftest import wait_until
from test_auth_escalation import FakeClock, internal_batch
from test_instagram_gate_requests import PAGES, FakeProvider, GatedInstagram

ITERATIONS = 200
SEED = 20261003
JOIN_TIMEOUT = 30.0
VERIFY_THREAD = "instagram-recovery-check"   # thread de run_with_deadline da verificação do controlador
MANUAL_THREAD = "instagram-session-test"     # thread de run_with_deadline do "Testar sessão" manual
SESSION_TEST = "/api/settings/instagram/session/test"


class RecordingGate(InstagramRequestGate):
    """Portão real que registra, sob a MESMA trava da admissão, a publicação/liberação e cada admissão/recusa num
    contador monotônico (ordem total coerente com a trava)."""

    def __init__(self) -> None:
        super().__init__(interval=(0.0, 0.0))
        self._seq = itertools.count(1)
        self.events: list[tuple[int, str, str, object]] = []

    def record(self, kind: str, extra: object = None) -> int:
        with self._pause_lock:
            n = next(self._seq)
            self.events.append((n, kind, threading.current_thread().name, extra))
            return n

    def reset_events(self) -> None:
        with self._pause_lock:
            self.events = []

    def _admit(self) -> None:
        with self._pause_lock:
            try:
                super()._admit()
            except InstagramPaused:
                self.record("refused")
                raise
            self.record("admit")

    def publish_pause(self, scope):
        with self._pause_lock:
            epoch = super().publish_pause(scope)
            self.record("publish", scope)
            return epoch

    def clear_pause(self) -> None:
        with self._pause_lock:
            was = self._pause_scope
            super().clear_pause()
            if was is not None:
                self.record("clear")


class RecordingInstagram(GatedInstagram):
    """Transporte em memória: registra o fim de cada requisição (no contador do portão) e o cursor de cada POST."""

    def __init__(self, gate: RecordingGate) -> None:
        super().__init__()
        self.gate = gate
        self.afters: list[str | None] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        response = super().handle(request)
        if request.url.path == "/graphql/query" and request.method == "POST":
            variables = json.loads(dict(parse_qsl(request.content.decode()))["variables"])
            with self.lock:
                self.afters.append(variables.get("after"))
        self.gate.record("done", request.url.path)
        return response


def web(mock: RecordingInstagram, gate: RecordingGate, **extra) -> InstagramWebDownloader:
    # Teto por hora alto: as centenas de rodadas somam milhares de páginas (o teto continua sendo aplicado).
    return InstagramWebDownloader(FakeProvider(), transport=mock.transport, page_delay=(0, 0), request_gate=gate,
                                  max_pages=PAGES + 5, env={ENV_MAX_PAGES_PER_HOUR: "100000"}, **extra)


def setup(harness_factory, **extra):
    h = harness_factory()
    gate = RecordingGate()
    h.ctx.instagram_gate = gate
    mock = RecordingInstagram(gate)
    h.downloader = web(mock, gate, **extra)   # downloader_factory do harness devolve h.downloader
    clock = FakeClock()
    h.ctx.worker.clock = clock
    return h, gate, mock, clock


def expire(h, clock: FakeClock) -> None:
    state = h.ctx.worker.recovery.state()
    assert state is not None
    due = datetime.fromisoformat(state.next_attempt_at.replace("Z", "+00:00"))
    clock.advance(seconds=(due - clock.now).total_seconds() + 1)


def publish_from_other_thread(h, code: str) -> None:
    """Outra operação publica a pausa; quem chamou só segue depois da publicação (síncrona)."""
    thread = threading.Thread(target=h.ctx.worker.pause_for, args=(code,), name="publisher")
    thread.start()
    thread.join(JOIN_TIMEOUT)
    assert not thread.is_alive()


def admits(gate: RecordingGate) -> list[tuple[int, str, str, object]]:
    return [e for e in gate.events if e[1] == "admit"]


def first(gate: RecordingGate, kind: str) -> int | None:
    return next((e[0] for e in gate.events if e[1] == kind), None)


# --------------------------------------------------------------------------- portão (unidade)


def test_publication_blocks_admission_and_verification_pass_is_bound_to_epoch():
    gate = RecordingGate()
    with gate.slot():
        pass
    epoch = gate.publish_pause("session")
    with pytest.raises(InstagramPaused) as info:
        with gate.slot():
            pytest.fail("admitida durante a pausa")
    assert info.value.scope == "session" and info.value.code == "instagram_paused"

    with gate.verification_pass(epoch):
        with gate.slot():
            pass   # passagem própria da verificação
        other: list[BaseException] = []

        def elsewhere() -> None:
            try:
                with gate.slot():
                    pass
            except InstagramPaused as exc:
                other.append(exc)

        thread = threading.Thread(target=elsewhere)
        thread.start()
        thread.join(JOIN_TIMEOUT)
        assert len(other) == 1, "a passagem não vale para outra thread"
        gate.publish_pause("instagram")   # nova pausa: a passagem antiga não vale mais
        with pytest.raises(InstagramPaused):
            with gate.slot():
                pass
    with pytest.raises(InstagramPaused):
        with gate.verification_pass(epoch - 1):
            with gate.slot():
                pass
    gate.clear_pause()
    with gate.slot():
        pass
    assert [e[1] for e in gate.events].count("admit") == 3


def test_refused_admission_releases_the_slot_without_deadlock():
    gate = RecordingGate()
    gate.publish_pause("instagram")
    for _ in range(50):
        with pytest.raises(InstagramPaused):
            with gate.slot():
                pass
    gate.clear_pause()
    done = threading.Event()

    def go() -> None:
        with gate.slot():
            done.set()

    threading.Thread(target=go, daemon=True).start()
    assert done.wait(JOIN_TIMEOUT), "a vaga ficou presa depois das recusas"


# --------------------------------------------------------------------------- corrida (centenas de rodadas)


def test_no_request_admitted_after_publication_under_random_races(harness_factory):
    h, gate, mock, clock = setup(harness_factory)
    gate.slow_listing = lambda seconds=0.0: None   # ritmo lento pós-429 (6-12 s/página) fora deste teste
    worker = h.ctx.worker
    rng = random.Random(SEED)
    inflight_at_publication = 0
    verified = 0
    readmitted = 0
    for iteration in range(ITERATIONS):
        worker.clear_pause()
        gate.reset_events()
        stop = threading.Event()
        results: dict[str, object] = {}
        checkpoints: list[int] = []
        due_mode = rng.random() < 0.5
        delay = rng.uniform(0.0, 0.02)
        code = rng.choice(("action_blocked", "request_auth_failed", "rate_limited"))

        def listing() -> None:
            items = web(mock, gate).iter_profile_media(
                "perfil_fake", "all", None, cancel=threading.Event(),
                on_checkpoint=lambda cp: checkpoints.append(cp.next_page_index))
            try:
                results["listing"] = sum(1 for _ in items)
            except InstagramPaused as exc:
                results["listing"] = exc

        def renew(i: int) -> None:
            refused = 0
            while not stop.is_set():
                page = 2 + (i + refused) % 10
                try:
                    web(mock, gate).refresh_page("perfil_fake", ListingRef(page_index=page,
                                                                           cursor=f"FAKE_CURSOR_{page - 1}",
                                                                           page_size=12))
                except InstagramPaused:
                    refused += 1
                time.sleep(0.0005)
            results[f"renew-{i}"] = refused

        def verifier() -> None:
            while not stop.is_set():
                outcome = worker.recovery.tick()
                if outcome is not None:
                    results.setdefault("ticks", []).append(outcome)
                time.sleep(0.0005)

        def publisher() -> None:
            time.sleep(delay)
            worker.pause_for(code)
            if due_mode:
                with gate._pause_lock:   # vencimento registrado na mesma ordem total das admissões
                    expire(h, clock)
                    gate.record("due")

        def guarded(name: str, target, *args) -> threading.Thread:
            def body() -> None:
                try:
                    target(*args)
                except BaseException as exc:  # noqa: BLE001 - a asserção abaixo inspeciona
                    results[name + "_error"] = exc

            thread = threading.Thread(target=body, name=name, daemon=True)
            thread.start()
            return thread

        threads = [guarded("listing", listing)]
        threads += [guarded(f"renew-{i}", renew, i) for i in range(3)]
        threads += [guarded("verifier", verifier), guarded("publisher", publisher)]
        threads[-1].join(JOIN_TIMEOUT)
        assert not threads[-1].is_alive(), f"rodada {iteration}: publicação travada"
        if due_mode:
            assert wait_until(lambda: gate.paused_scope() is None, timeout=JOIN_TIMEOUT), \
                f"rodada {iteration}: a verificação do controlador não liberou a pausa vencida"
            clear_at = first(gate, "clear")
            assert wait_until(lambda: any(e[0] > clear_at and e[2].startswith("renew") for e in admits(gate)),
                              timeout=JOIN_TIMEOUT), f"rodada {iteration}: a admissão não voltou após a liberação"
        else:
            time.sleep(0.003)
        stop.set()
        for thread in threads:
            thread.join(JOIN_TIMEOUT)
            assert not thread.is_alive(), f"rodada {iteration}: deadlock em {thread.name}"
        errors = {k: v for k, v in results.items() if k.endswith("_error")}
        assert not errors, f"rodada {iteration}: {errors!r}"

        published = first(gate, "publish")
        cleared = first(gate, "clear")
        due = first(gate, "due")
        assert published is not None
        for n, _kind, thread_name, _ in admits(gate):
            if thread_name == VERIFY_THREAD:
                assert due is not None and published < due < n and (cleared is None or n < cleared), \
                    f"rodada {iteration}: verificação fora da pausa vencida ({n})"
            elif n > published:
                assert cleared is not None and n > cleared, \
                    f"rodada {iteration}: {thread_name} admitida em {n} depois da publicação em {published}"
        # Toda requisição admitida terminou e foi contabilizada (inclusive a que estava em voo na publicação).
        per_thread: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for n, kind, thread_name, _ in gate.events:
            if kind in ("admit", "done"):
                per_thread[thread_name].append((kind, n))
        for thread_name, seq in per_thread.items():
            kinds = [k for k, _ in seq]
            assert kinds == ["admit", "done"] * (len(kinds) // 2), f"rodada {iteration}: {thread_name} {kinds}"
            pairs = list(zip(seq[0::2], seq[1::2]))
            inflight_at_publication += sum(1 for (_, a), (_, d) in pairs if a < published < d)
        # A página em voo da listagem foi processada (checkpoint) antes de a próxima ser recusada.
        listing_posts = sum(1 for e in gate.events if e[1] == "done" and e[2] == "listing"
                            and e[3] == "/graphql/query")
        assert len(checkpoints) == listing_posts, f"rodada {iteration}: {checkpoints} x {listing_posts} POSTs"
        assert isinstance(results["listing"], (int, InstagramPaused))
        if due_mode:
            assert "ok" in results.get("ticks", []), f"rodada {iteration}: {results.get('ticks')}"
            verified += 1
            readmitted += 1
        else:
            assert gate.paused_scope() is not None
    worker.clear_pause()
    assert verified > 0 and readmitted > 0
    assert inflight_at_publication > 0, "nenhuma publicação caiu com requisição em voo (teste sem corrida)"


# --------------------------------------------------------------------------- quem recebe a recusa


def test_listing_refused_between_pages_goes_paused_keeping_checkpoint(harness_factory):
    h, gate, mock, _clock = setup(harness_factory)
    batch_id = internal_batch(h, 1000)
    original = gate.wait_listing_page
    calls = {"n": 0}

    def wait_listing_page(cancel=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 4:   # entre a página 3 (checkpoint gravado) e o POST da página 4
            publish_from_other_thread(h, "action_blocked")
        return original(cancel, **kwargs)

    gate.wait_listing_page = wait_listing_page
    assert h.ctx.import_runner.run_next()
    published = first(gate, "publish")
    assert published is not None
    assert not [e for e in admits(gate) if e[0] > published], "requisição admitida depois da publicação"
    assert mock.afters == [None, "FAKE_CURSOR_1", "FAKE_CURSOR_2"]
    row = dict(h.ctx.db.fetchone(
        "SELECT status, listing_state, listing_next_page, listing_cursor, error_code FROM batches WHERE id = ?",
        (batch_id,)))
    assert row == {"status": "listing", "listing_state": "paused", "listing_next_page": 4,
                   "listing_cursor": "FAKE_CURSOR_3", "error_code": None}
    assert h.ctx.jobs.count_for_batch(batch_id) == 36

    assert not h.ctx.import_runner.run_next(), "nenhum trecho durante a pausa"
    h.ctx.worker.clear_pause()
    assert h.ctx.import_runner.run_next()
    assert mock.afters[3] == "FAKE_CURSOR_3", "retomada do checkpoint, sem recomeçar da página 1"


def test_renewal_refused_by_gate_returns_job_to_pending_without_attempt(harness_factory):
    def expired_cdn(url, dest_dir, *, max_bytes, cancel=None, progress=None, **_):
        raise DownloadError("forbidden", "FAKE: link recusado")

    h, gate, mock, _clock = setup(harness_factory, cdn_downloader=expired_cdn)
    batch_id = internal_batch(h, 12)
    while h.ctx.import_runner.run_next():
        pass
    assert h.ctx.jobs.count_for_batch(batch_id) == 12
    sent = len(mock.spans)
    original = gate.slot
    armed = {"on": True}

    def slot(cancel=None, **kwargs):
        if armed["on"]:   # a pausa é publicada logo antes da admissão da renovação
            armed["on"] = False
            publish_from_other_thread(h, "request_auth_failed")
        return original(cancel, **kwargs)

    gate.slot = slot
    assert h.ctx.worker.run_pending(max_jobs=1) == 1
    assert len(mock.spans) == sent, "nenhuma requisição saiu depois da publicação"
    job = dict(h.ctx.db.fetchone(
        "SELECT id, status, stage, attempts FROM jobs WHERE batch_id = ? AND stage = 'waiting_retry'", (batch_id,)))
    assert (job["status"], job["attempts"]) == ("pending", 0)
    assert h.ctx.jobs.listing_page(batch_id, 1) is None, "recusa do portão não conta como falha de renovação"
    assert h.ctx.worker.pause_state()[1] == "auth_check"

    h.ctx.worker.clear_pause()
    posts = len(mock.afters)
    h.ctx.worker.run_pending(max_jobs=1)
    assert len(mock.afters) == posts + 1, "depois da liberação a renovação é admitida"


def test_only_controller_verification_passes_during_expired_pause(harness_factory):
    h, gate, mock, clock = setup(harness_factory)
    worker = h.ctx.worker
    worker.pause_for("request_auth_failed")
    assert gate.paused_scope() == "session"
    ref = ListingRef(page_index=2, cursor="FAKE_CURSOR_1", page_size=12)

    assert worker.recovery.tick() is None   # ainda no prazo: nem a verificação
    with pytest.raises(InstagramPaused):
        web(mock, gate).refresh_page("perfil_fake", ref)

    expire(h, clock)
    assert worker.pause_state()[0] is not None, "pausa vencida continua vigente até a verificação"
    with pytest.raises(InstagramPaused):
        web(mock, gate).refresh_page("perfil_fake", ref)
    with pytest.raises(InstagramPaused):
        list(web(mock, gate).iter_profile_media("perfil_fake", "all", None))
    assert admits(gate) == []

    assert worker.recovery.tick() == "ok"
    names = Counter(e[2] for e in admits(gate))
    assert set(names) == {VERIFY_THREAD} and names[VERIFY_THREAD] >= 2   # bootstrap + 1 consulta count=1
    assert gate.paused_scope() is None and worker.pause_state() == (None, None)
    assert len(web(mock, gate).refresh_page("perfil_fake", ref)) > 0


# --------------------------------------------------------------------------- "Testar sessão" manual


@pytest.mark.parametrize("code, released", [("action_blocked", False), ("request_auth_failed", True)])
def test_manual_session_test_passes_during_pause_and_others_stay_refused(harness_factory, code, released):
    h, gate, mock, _clock = setup(harness_factory)
    worker = h.ctx.worker
    worker.pause_for(code)
    published = first(gate, "publish")
    ref = ListingRef(page_index=2, cursor="FAKE_CURSOR_1", page_size=12)
    stop = threading.Event()
    refused = {"n": 0}

    def other() -> None:   # outra operação tentando durante o teste manual
        while not stop.is_set():
            try:
                web(mock, gate).refresh_page("perfil_fake", ref)
            except InstagramPaused:
                refused["n"] += 1
            time.sleep(0.0005)

    thread = threading.Thread(target=other, name="renew", daemon=True)
    thread.start()
    try:
        response = h.client.post(SESSION_TEST)
    finally:
        stop.set()
        thread.join(JOIN_TIMEOUT)
    assert not thread.is_alive()
    assert response.status_code == 200, response.text
    assert response.json()["last_test"]["result"] == "valid"
    cleared = first(gate, "clear")
    names = Counter(e[2] for e in admits(gate) if e[0] > published and (cleared is None or e[0] < cleared))
    assert set(names) == {MANUAL_THREAD} and names[MANUAL_THREAD] >= 2   # bootstrap + 1 consulta count=1
    assert refused["n"] > 0
    # Sucesso libera só as pausas de sessão (como antes); rate_limited/action_blocked continuam.
    assert (gate.paused_scope() is None) is released
    assert (worker.pause_state()[0] is None) is released
    if not released:
        with pytest.raises(InstagramPaused):
            web(mock, gate).refresh_page("perfil_fake", ref)


def test_new_pause_during_manual_session_test_refuses_its_next_requests(harness_factory):
    h, gate, mock, _clock = setup(harness_factory)
    worker = h.ctx.worker
    worker.pause_for("action_blocked")
    original = gate.slot
    calls = {"n": 0}

    def slot(cancel=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:   # depois do bootstrap do teste, antes da consulta: nova pausa (mais grave) publicada
            publish_from_other_thread(h, "checkpoint_required")
        return original(cancel, **kwargs)

    gate.slot = slot
    response = h.client.post(SESSION_TEST)
    assert response.status_code == 409 and response.json()["code"] == "instagram_paused", response.text
    republished = [e[0] for e in gate.events if e[1] == "publish"][-1]
    test_admits = [e[0] for e in admits(gate) if e[2] == MANUAL_THREAD]
    assert len(test_admits) == 1 and test_admits[0] < republished, "só o bootstrap, antes da nova pausa"
    assert [e for e in gate.events if e[1] == "refused" and e[2] == MANUAL_THREAD]
    assert gate.paused_scope() == "session" and worker.pause_state()[1] == "checkpoint_required"


# --------------------------------------------------------------------------- verificação recusada (republicação)


@pytest.mark.parametrize("outcome", [("failed", "rate_limited"), ("network", None)])
def test_failed_verification_never_opens_the_gate_while_republishing(harness_factory, monkeypatch, outcome):
    """Entre gravar o novo prazo e republicar a pausa, quem consulta ``pause_state`` de outra thread continua vendo
    pausa vigente: o portão não é liberado nem por um instante."""
    from app.services import instagram_recovery as recovery_mod

    h, gate, _mock, clock = setup(harness_factory)
    worker = h.ctx.worker
    worker.pause_for("rate_limited")
    expire(h, clock)
    monkeypatch.setattr(worker.recovery, "_verify", lambda: outcome)
    seen: list[tuple[object, object]] = []
    settings = h.ctx.app_settings
    original = settings.set_state

    def probe() -> None:
        seen.append((worker.pause_state()[0], gate.paused_scope()))
        try:
            with gate.slot():
                seen.append(("admitted", None))
        except InstagramPaused:
            pass

    def set_state(key, value):
        result = original(key, value)
        if key == recovery_mod.STATE_KEY and value is not None:
            thread = threading.Thread(target=probe, name="intruder")
            thread.start()
            thread.join(JOIN_TIMEOUT)
            assert not thread.is_alive()
        return result

    monkeypatch.setattr(settings, "set_state", set_state)
    gate.reset_events()
    assert worker.recovery.tick() == outcome[0]
    assert seen and all(until is not None and scope == "instagram" for until, scope in seen), seen
    assert admits(gate) == []
    assert worker.pause_state()[0] is not None and gate.paused_scope() == "instagram"
