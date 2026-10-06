"""Visibilidade da espera pelo teto de páginas por hora (docs/BATCH_ROBUSTNESS.md, seção 9, "Espera pelo teto").

Portão: o teto continua exatamente o configurado (600/h) antes e depois de um reinício, a janela persistida é
reconstruída (entradas velhas, futuras ou ilegíveis) e a espera avisa uma vez na entrada e uma na saída.
Lote: estado exibido pela API, uma linha de log em cada ponta, nenhuma página durante a espera, reinício,
cancelamento e pausa do Instagram durante a espera. Tudo offline, relógio simulado, valores FAKE.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import pytest

from app.db.connection import parse_utc, utc_now
from app.integrations.instagram.downloader import ListingCheckpoint, ListingRef, RemoteMedia
from app.services.instagram_gate import (
    DEFAULT_MAX_PAGES_PER_HOUR,
    PAGE_WINDOW_SECONDS,
    PAGES_STATE_KEY,
    GateCancelled,
    InstagramRequestGate,
)
from conftest import wait_until
from media_fakes import FakeInstagramDownloader, remote

JOIN_TIMEOUT = 20.0
PER_HOUR = 3   # teto do fake dos testes de lote (o do portão é 600 por padrão)


# --------------------------------------------------------------------------- portão (unidade, relógio simulado)


class FakeClock:
    """Relógio monotônico e de parede andando juntos; `sleep` avança os dois."""

    def __init__(self, mono: float = 1000.0, wall: float = 1_800_000_000.0) -> None:
        self.mono = mono
        self.wall = wall
        self.sleeps: list[float] = []

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)

    def gate(self, store: dict[str, str], **extra) -> InstagramRequestGate:
        return InstagramRequestGate(interval=(0, 0), clock=lambda: self.mono, wall_clock=lambda: self.wall,
                                    sleep=self.sleep, save_pages=lambda value: store.__setitem__("pages", value),
                                    **extra)


def test_cap_stays_exactly_600_per_hour_before_and_after_restart():
    assert DEFAULT_MAX_PAGES_PER_HOUR == 600 and PAGE_WINDOW_SECONDS == 3600.0
    store: dict[str, str] = {}
    first = FakeClock()
    gate = first.gate(store)
    reserved: list[float] = []   # relógio de parede de cada página reservada
    for _ in range(600):
        gate.wait_listing_page()
        reserved.append(first.wall)
        first.advance(1.0)
    assert first.sleeps == [], "as 600 primeiras páginas da hora não esperam"
    for _ in range(20):          # da 601a em diante cada página espera a mais antiga sair da janela
        gate.wait_listing_page()
        reserved.append(first.wall)
    assert len(first.sleeps) == 20 and first.sleeps[0] == pytest.approx(3000.0)

    # "Reinício": portão novo, relógio monotônico recomeçado, 0,5 s de relógio de parede depois.
    second = FakeClock(mono=5.0, wall=first.wall + 0.5)
    restarted = second.gate(store)
    assert restarted.restore_listing_pages(store["pages"]) == 600
    used, seconds = restarted.listing_window()
    assert used == 600 and seconds == pytest.approx(0.5, abs=0.01)
    for _ in range(620):
        restarted.wait_listing_page()
        reserved.append(second.wall)
    assert second.sleeps[0] == pytest.approx(0.5, abs=0.01), "nenhuma página antes da hora depois do reinício"

    # Nem mais: em qualquer janela de 1 h cabem no máximo 600 páginas. Nem menos: a 601a sai assim que a 1a completa 1 h.
    gaps = [reserved[i + 600] - reserved[i] for i in range(len(reserved) - 600)]
    assert min(gaps) >= PAGE_WINDOW_SECONDS - 0.01
    assert max(gaps) == pytest.approx(PAGE_WINDOW_SECONDS, abs=0.6)


def test_restored_window_drops_old_entries_and_counts_future_or_corrupted_as_used_now():
    clock = FakeClock()
    now = clock.wall
    assert clock.gate({}).restore_listing_pages(None) == 0
    assert clock.gate({}).restore_listing_pages("") == 0

    gate = clock.gate({})
    raw = f'[{now - 4000}, {now - 100}, {now + 10000}, "x", null, true]'
    assert gate.restore_listing_pages(raw) == 5, "a entrada com mais de 1 h sai; as duvidosas contam como usadas agora"
    assert gate.listing_window(6) == (5, None)
    used, seconds = gate.listing_window(5)
    assert used == 5 and seconds == pytest.approx(3500.0), "a vaga abre quando a entrada válida completa 1 h"
    used, seconds = gate.listing_window(1)
    assert seconds == pytest.approx(PAGE_WINDOW_SECONDS), "entrada no futuro nunca espera mais que 1 h"

    for raw in ("isto não é JSON", '{"a": 1}'):
        gate = clock.gate({})
        assert gate.restore_listing_pages(raw) == DEFAULT_MAX_PAGES_PER_HOUR
        used, seconds = gate.listing_window()
        assert used == 600 and seconds == pytest.approx(PAGE_WINDOW_SECONDS), "ilegível: cheia, por no máximo 1 h"
        clock.advance(0.0)
        gate.wait_listing_page()
        assert clock.sleeps[-1] == pytest.approx(PAGE_WINDOW_SECONDS)
    # O fail-safe usa o teto VIGENTE (variável de ambiente), não o padrão.
    gate = clock.gate({})
    assert gate.restore_listing_pages("isto não é JSON", max_per_hour=7) == 7
    assert gate.listing_window(7) == (7, pytest.approx(PAGE_WINDOW_SECONDS))


def test_gate_notifies_wait_once_on_enter_and_once_on_exit():
    clock = FakeClock()
    events: list[tuple[str, dict[str, Any]]] = []

    def short_sleep(seconds: float) -> None:   # acorda antes da hora várias vezes: o portão volta a esperar
        clock.sleeps.append(seconds)
        clock.advance(min(seconds, 600.0))

    gate = InstagramRequestGate(interval=(0, 0), clock=lambda: clock.mono, wall_clock=lambda: clock.wall,
                                sleep=short_sleep)
    gate.listing_wait_observer = lambda event, info: events.append((event, dict(info)))
    for _ in range(3):
        gate.wait_listing_page(max_per_hour=3)
    assert events == [], "sem espera não há aviso"
    clock.advance(60.0)
    gate.wait_listing_page(max_per_hour=3)
    assert len(clock.sleeps) == 6, "a espera foi retomada várias vezes"
    assert [event for event, _ in events] == ["enter", "exit"]
    assert events[0][1] == {"seconds": pytest.approx(3540.0), "used": 3, "max": 3}
    assert events[1][1] == {"waited": pytest.approx(3540.0), "reason": "released"}

    # Cancelamento durante a espera (caminho real, sem `sleep` injetado) e observador que falha: nada quebra.
    cancel = threading.Event()
    cancel.set()
    real = InstagramRequestGate(interval=(0, 0))
    seen: list[str] = []

    def failing(event: str, info: dict[str, Any]) -> None:
        seen.append(f"{event}:{info.get('reason')}")
        raise RuntimeError("falha FAKE do observador")

    real.listing_wait_observer = failing
    real.wait_listing_page(max_per_hour=1)
    with pytest.raises(GateCancelled):
        real.wait_listing_page(cancel, max_per_hour=1)
    assert seen == ["enter:None", "exit:cancelled"]


def test_short_waits_at_the_cap_are_not_announced_but_still_wait():
    """Em regime no teto cada página espera alguns segundos: isso é ritmo, não estado nem log."""
    clock = FakeClock()
    events: list[str] = []
    gate = clock.gate({})
    gate.listing_wait_observer = lambda event, info: events.append(event)
    for _ in range(600):
        gate.wait_listing_page()
        clock.advance(5.5)           # 600 páginas em 3300 s: a janela enche e a 601a espera 300 s (avisada)
    gate.wait_listing_page()
    assert events == ["enter", "exit"] and clock.sleeps == [pytest.approx(300.0)]
    events.clear()
    for _ in range(40):              # daqui em diante cada página espera 5,5 s: abaixo do limiar, sem aviso
        gate.wait_listing_page()
    assert events == [] and len(clock.sleeps) == 41
    assert all(0 < s < 30.0 for s in clock.sleeps[1:]), "a espera em si não mudou"


# --------------------------------------------------------------------------- lote (import runner + API)


@dataclass
class GatedFakeDownloader(FakeInstagramDownloader):
    """Listagem falsa que passa pelo portão como o provider web: reserva a página no teto por hora e ocupa uma vaga
    (admissão) antes de "pedir" cada página."""

    gate: Any = None

    def iter_profile_media(self, username, content, limit, *, cancel=None, resume=None,
                           on_checkpoint=None) -> Iterator[RemoteMedia]:
        self.listings += 1
        size = self.page_size
        last_page = max(1, -(-len(self.items) // size))
        page = resume.next_page_index if resume is not None else 1
        pages = resume.pages if resume is not None else 0
        seen = resume.posts_seen if resume is not None else 0
        while page <= last_page:
            if cancel is not None and cancel.is_set():
                return
            self.gate.wait_listing_page(cancel, max_per_hour=PER_HOUR)
            with self.gate.slot(cancel):
                self.page_requests.append(page)
            cursor = None if page == 1 else f"FAKE-CURSOR-{page}"
            chunk = self.items[(page - 1) * size: page * size]
            for item in chunk:
                yield replace(item, listing_ref=ListingRef(page_index=page, cursor=cursor, page_size=size))
            pages += 1
            seen += len(chunk)
            has_next = page < last_page
            if on_checkpoint is not None:
                on_checkpoint(ListingCheckpoint(
                    resume_cursor=f"FAKE-CURSOR-{page + 1}" if has_next else None, next_page_index=page + 1,
                    pages=pages, posts_seen=seen, items_compatible=seen, unproductive_streak=0, has_next=has_next,
                ))
            page += 1


class GateTime:
    """Controla o relógio do portão do app de teste: `advance` adianta o relógio monotônico e acorda a espera."""

    def __init__(self, gate: InstagramRequestGate) -> None:
        self.skew = 0.0
        self.sleeps = 0
        gate._clock = lambda: time.monotonic() + self.skew
        gate._sleep = self._sleep

    def _sleep(self, _seconds: float) -> None:
        skew = self.skew
        self.sleeps += 1
        deadline = time.monotonic() + JOIN_TIMEOUT
        while self.skew == skew and time.monotonic() < deadline:
            time.sleep(0.005)

    def advance(self, seconds: float) -> None:
        self.skew += seconds


class LogLines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    def waiting(self) -> list[str]:
        return [line for line in self.lines if "limite de requisições" in line]


@pytest.fixture
def log_lines() -> Iterator[LogLines]:
    handler = LogLines()
    target = logging.getLogger("media_tool.imports")
    level = target.level
    target.addHandler(handler)
    target.setLevel(logging.INFO)
    yield handler
    target.removeHandler(handler)
    target.setLevel(level)


def _app(harness_factory, root=None, items: int = 60):
    dl = GatedFakeDownloader(items=[remote(f"R{i:04d}") for i in range(items)])
    # O teto dos testes de lote vem da configuração: a reconciliação da inicialização usa o teto vigente.
    h = harness_factory(root=root, downloader=dl, env={"INSTAGRAM_PAGE_RATE_LIMIT": str(PER_HOUR)})
    dl.gate = h.ctx.instagram_gate
    return h, dl


def _start(h) -> threading.Thread:
    thread = threading.Thread(target=h.ctx.import_runner.run_next, name="listing", daemon=True)
    thread.start()
    return thread


def _api(h, batch_id: int) -> dict:
    response = h.client.get(f"/api/batches/{batch_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _row(h, batch_id: int) -> dict:
    return dict(h.ctx.db.fetchone(
        "SELECT status, listing_state, rate_limit_resume_at, rate_limit_pages_used, rate_limit_pages_max"
        " FROM batches WHERE id = ?", (batch_id,)))


def _waiting(h, batch_id: int) -> bool:
    return _row(h, batch_id)["rate_limit_resume_at"] is not None


def _remaining(iso: str) -> float:
    return (parse_utc(iso) - datetime.now(UTC)).total_seconds()


def test_wait_is_visible_in_batch_state_and_logged_once_on_enter_and_exit(harness_factory, log_lines):
    h, dl = _app(harness_factory)
    clock = GateTime(h.ctx.instagram_gate)
    batch_id = h.import_profile(limit=100)["id"]
    thread = _start(h)
    assert wait_until(lambda: clock.sleeps == 1), "a listagem deveria esperar o teto depois de 3 páginas"

    # Entrada: estado do lote, campos da API e UMA linha de log.
    assert dl.page_requests == [1, 2, 3]
    row = _row(h, batch_id)
    assert (row["status"], row["listing_state"]) == ("listing", "running"), "internamente o trecho continua ativo"
    api = _api(h, batch_id)
    assert (api["status"], api["listing_state"]) == ("listing", "waiting_rate_limit")
    assert (api["rate_limit_pages_used"], api["rate_limit_pages_max"]) == (3, 3)
    assert 3590 < _remaining(api["rate_limit_resume_at"]) <= 3600
    assert api["error_code"] is None and api["paused_reason"] is None and api["paused_until"] is None
    listed = next(b for b in h.client.get("/api/batches").json()["items"] if b["id"] == batch_id)
    assert listed["listing_state"] == "waiting_rate_limit"
    assert listed["rate_limit_resume_at"] == api["rate_limit_resume_at"]
    assert h.ctx.batches.has_active_work(), "o lote em espera continua ativo (mantém o PC acordado)"
    assert h.client.post(f"/api/batches/{batch_id}/resume-listing").status_code == 409, "não é pausa: nada a retomar"
    entered = log_lines.waiting()
    assert len(entered) == 1, entered
    assert entered[0].startswith(
        f"Lote {batch_id}: aguardando limite de requisições (teto de 3 páginas por hora atingido: 3/3); "
        "espera prevista de 60 min 00 s, retomada automática às "), entered[0]
    assert entered[0].endswith(api["rate_limit_resume_at"])

    # Durante a espera (o relógio anda várias vezes): nenhuma página, nenhum log novo, mesmo horário de retomada.
    for step in range(3):
        clock.advance(600.0)
        assert wait_until(lambda: clock.sleeps == step + 2)
        assert dl.page_requests == [1, 2, 3]
        assert log_lines.waiting() == entered
        assert _api(h, batch_id)["rate_limit_resume_at"] == api["rate_limit_resume_at"]

    # Saída: a janela libera, as páginas seguintes são pedidas e há UMA linha de log de saída.
    clock.advance(1801.0)
    thread.join(JOIN_TIMEOUT)
    assert not thread.is_alive()
    assert dl.page_requests == [1, 2, 3, 4, 5]
    lines = log_lines.waiting()
    assert len(lines) == 2, lines
    assert lines[1] == (f"Lote {batch_id}: fim da espera pelo limite de requisições depois de 60 min 01 s "
                        "(janela liberada)"), lines[1]
    api = _api(h, batch_id)
    assert api["listing_state"] == "done" and api["found"] == 60
    assert api["rate_limit_resume_at"] is None and api["rate_limit_pages_used"] is None
    assert api["rate_limit_pages_max"] is None
    assert not _waiting(h, batch_id)


def test_restart_during_wait_rebuilds_state_and_keeps_the_resume_time(harness_factory, log_lines):
    h, dl = _app(harness_factory)
    batch_id = h.import_profile(limit=100)["id"]
    thread = _start(h)   # relógio real: a espera de ~1 h só termina pelo encerramento
    assert wait_until(lambda: _waiting(h, batch_id))
    before = _api(h, batch_id)
    assert before["listing_state"] == "waiting_rate_limit" and dl.page_requests == [1, 2, 3]

    h.ctx.import_runner.stop()   # encerramento do app durante a espera
    thread.join(JOIN_TIMEOUT)
    assert not thread.is_alive() and dl.page_requests == [1, 2, 3]
    assert log_lines.waiting()[-1].endswith("(encerramento do aplicativo)")
    assert _waiting(h, batch_id), "o estado da espera fica gravado para a próxima execução"

    h2, dl2 = _app(harness_factory, root=h.root)   # "reinício": novo app sobre o mesmo banco
    after = _api(h2, batch_id)
    assert after["listing_state"] == "waiting_rate_limit"
    assert (after["rate_limit_pages_used"], after["rate_limit_pages_max"]) == (3, 3)
    drift = (parse_utc(after["rate_limit_resume_at"]) - parse_utc(before["rate_limit_resume_at"])).total_seconds()
    assert abs(drift) < 2.0, f"mesma hora de retomada depois do reinício (diferença de {drift:.3f}s)"

    clock = GateTime(h2.ctx.instagram_gate)
    log_lines.lines.clear()
    thread = _start(h2)
    assert wait_until(lambda: clock.sleeps == 1), "depois do reinício a listagem volta a esperar a mesma janela"
    assert dl2.page_requests == [], "nenhuma página antes da hora"
    assert _api(h2, batch_id)["listing_state"] == "waiting_rate_limit"
    assert len(log_lines.waiting()) == 1
    clock.advance(1800.0)
    assert wait_until(lambda: clock.sleeps == 2) and dl2.page_requests == []
    clock.advance(1801.0)
    thread.join(JOIN_TIMEOUT)
    assert not thread.is_alive()
    assert dl2.page_requests == [4, 5], "retoma do checkpoint quando a janela libera"
    api = _api(h2, batch_id)
    assert api["listing_state"] == "done" and api["found"] == 60 and api["rate_limit_resume_at"] is None
    assert len(log_lines.waiting()) == 2 and log_lines.waiting()[1].endswith("(janela liberada)")


def test_orphan_wait_state_is_reconciled_on_startup_and_never_shown_after_its_time(harness_factory):
    h, _dl = _app(harness_factory)
    batch_id = h.import_profile(limit=100)["id"]
    # Estado deixado por uma execução anterior, mas a janela persistida está livre (nenhuma página na última hora).
    h.ctx.batches.update_listing(batch_id, listing_state="running", rate_limit_resume_at=utc_now(1800),
                                 rate_limit_pages_used=300, rate_limit_pages_max=300)
    assert _api(h, batch_id)["listing_state"] == "waiting_rate_limit"
    h.ctx.batches.update_listing(batch_id, rate_limit_resume_at=utc_now(-5))
    api = _api(h, batch_id)
    assert api["listing_state"] == "running" and api["rate_limit_resume_at"] is None, "horário vencido não aparece"
    h.ctx.batches.update_listing(batch_id, rate_limit_resume_at=utc_now(1800))

    h2, dl2 = _app(harness_factory, root=h.root, items=36)
    assert not _waiting(h2, batch_id), "janela livre: o estado órfão é limpo na inicialização"
    api = _api(h2, batch_id)
    assert api["listing_state"] == "running" and api["rate_limit_pages_used"] is None
    h2.run_imports()
    assert dl2.page_requests == [1, 2, 3], "o lote volta ao fluxo normal de listagem"
    assert _api(h2, batch_id)["listing_state"] == "done"


def test_corrupted_persisted_window_is_fail_safe_and_keeps_the_wait_state(harness_factory):
    h, _dl = _app(harness_factory)
    batch_id = h.import_profile(limit=100)["id"]
    # Janela persistida ilegível: considerada cheia por no máximo 1 h; o estado gravado continua, recalculado.
    h.ctx.app_settings.set_state(PAGES_STATE_KEY, "{corrompido")
    h.ctx.batches.update_listing(batch_id, listing_state="running", rate_limit_resume_at=utc_now(60),
                                 rate_limit_pages_used=PER_HOUR, rate_limit_pages_max=PER_HOUR)
    h2, _dl2 = _app(harness_factory, root=h.root)
    api = _api(h2, batch_id)
    # Ilegível = cheia pelo teto VIGENTE (o da configuração do app de teste).
    assert api["listing_state"] == "waiting_rate_limit" and api["rate_limit_pages_used"] == PER_HOUR
    assert api["rate_limit_pages_max"] == PER_HOUR
    assert 3590 < _remaining(api["rate_limit_resume_at"]) <= 3600


def test_cancel_during_wait_takes_effect_immediately(harness_factory, log_lines):
    h, dl = _app(harness_factory)
    batch_id = h.import_profile(limit=100)["id"]
    thread = _start(h)   # relógio real: sem cancelamento a espera duraria ~1 h
    assert wait_until(lambda: _waiting(h, batch_id))
    assert _api(h, batch_id)["listing_state"] == "waiting_rate_limit"
    body = h.client.post(f"/api/batches/{batch_id}/cancel").json()
    assert (body["status"], body["listing_state"], body["rate_limit_resume_at"]) == ("cancelled", "cancelled", None)
    thread.join(5.0)
    assert not thread.is_alive(), "o cancelamento não interrompeu a espera"
    assert dl.page_requests == [1, 2, 3]
    row = _row(h, batch_id)
    assert (row["status"], row["listing_state"], row["rate_limit_resume_at"]) == ("cancelled", "cancelled", None)
    lines = log_lines.waiting()
    assert len(lines) == 2 and lines[1].endswith("(lote cancelado)"), lines


def test_instagram_pause_during_wait_keeps_precedence(harness_factory, log_lines):
    h, dl = _app(harness_factory)
    clock = GateTime(h.ctx.instagram_gate)
    batch_id = h.import_profile(limit=100)["id"]
    thread = _start(h)
    assert wait_until(lambda: clock.sleeps == 1)
    publisher = threading.Thread(target=h.ctx.worker.pause_for, args=("action_blocked",), name="publisher")
    publisher.start()
    publisher.join(JOIN_TIMEOUT)
    assert h.ctx.instagram_gate.paused_scope() is not None
    clock.advance(3601.0)
    thread.join(JOIN_TIMEOUT)
    assert not thread.is_alive()
    assert dl.page_requests == [1, 2, 3], "com a pausa publicada nenhuma página é pedida quando a janela libera"
    api = _api(h, batch_id)
    assert api["listing_state"] == "paused" and api["rate_limit_resume_at"] is None
    lines = log_lines.waiting()
    assert len(lines) == 2 and "janela liberada; Instagram em pausa" in lines[1], lines
    assert not h.ctx.import_runner.run_next(), "nenhum trecho durante a pausa"
    h.ctx.worker.clear_pause()
    h.run_imports()
    assert dl.page_requests == [1, 2, 3, 4, 5]
    assert _api(h, batch_id)["listing_state"] == "done"
