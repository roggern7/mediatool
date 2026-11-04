"""Análise de Perfis em escala (10.000 perfis) e retomada. Tudo com fetcher falso e relógio simulado, sem rede."""

from __future__ import annotations

import asyncio
import csv
import gc
import io
import json
import re
import statistics
import tempfile
import threading
import time
import tracemalloc
import zipfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from xml.etree import ElementTree

import httpx
import pytest
from starlette.requests import ClientDisconnect

from app.api.limits import MAX_BODY_BYTES
from app.api.profile_analyses import ReportResponse
from app.core import logging as app_logging
from app.integrations.instagram.web.errors import InstagramWebError
from app.integrations.instagram.web.http import InstagramWebHttp
from app.jobs.profile_analysis_runner import (
    CANARY_EVERY,
    CANARY_PROFILE,
    REST_EVERY,
    ProfileAnalysisRunner,
    stamp,
)
from app.main import anonymous_profile_fetcher
from app.schemas.profile_analyses import MAX_TEXT_CHARS
from app.services import profile_analysis_service, profile_report
from app.services.instagram_gate import InstagramRequestGate
from app.services.profile_analysis_service import MAX_PROFILES, report_zip
from profile_fakes import (
    ScriptedFetcher,
    SimClock,
    analysis_of,
    create,
    install_runner,
    items_of,
    profile_html,
    snapshot,
)

API = "/api/profile-analyses"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
START = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
MIB = 1024 * 1024


def names_of(size: int, prefix: str = "perfil") -> list[str]:
    return [f"{prefix}{index:05d}" for index in range(size)]


def expected_row(position: int) -> dict:
    """Linha sintética da posição: mistura estável de status, com horário próprio (9 s entre perfis)."""
    moment = START + timedelta(seconds=9 * position)
    return {"status": ("active", "private", "unavailable", "unverified", "active")[position % 5],
            "checked_at": stamp(moment), "date": moment.strftime("%d/%m/%Y"), "time": moment.strftime("%H:%M:%S"),
            "stories": "yes" if position % 2 else "no", "highlights": "no" if position % 3 else "yes"}


def seed_finished(ctx, size: int, *, status: str = "completed", last_done: int | None = None) -> int:
    """Análise terminal direto no banco, pelo `finish` do executor. Depois de `last_done`, itens não consultados."""
    repo = ctx.profile_analyses
    last_done = size if last_done is None else last_done
    analysis_id = repo.create(names_of(size), invalid_count=0, duplicate_count=0, invalid_lines=[])["id"]
    repo.claim_next(stamp(START))

    def params():
        for position in range(1, last_done + 1):
            row = expected_row(position)
            known = row["status"] in ("active", "private")
            yield (row["status"], f"Nome {position}" if known else None, "known" if known else "unverified",
                   position if known else None, 1 if known else None, "known" if known else "unverified",
                   position * 10 if known else None, 2 if known else None, row["stories"] if known else "unverified",
                   row["highlights"] if known else "unverified", row["checked_at"], analysis_id, position)

    with ctx.db.transaction() as conn:
        conn.executemany(
            "UPDATE profile_analysis_items SET state = 'done', attempts = 1, account_status = ?, display_name = ?,"
            " posts_state = ?, posts_count = ?, posts_exact = ?, views_state = ?, views_total = ?, views_reels = ?,"
            " stories = ?, highlights = ?, checked_at = ? WHERE analysis_id = ? AND position = ?", params())
    repo.finish(analysis_id, "completed", stamp(START))
    if status == "cancelled":
        ctx.db.execute("UPDATE profile_analyses SET status = 'running', final_stats = NULL WHERE id = ?",
                       (analysis_id,))
        repo.finish_cancelled(analysis_id, stamp(START))
    return analysis_id


# ---- limites ------------------------------------------------------------------------------------------------------
def test_limit_constants():
    assert MAX_PROFILES == 10_000 and MAX_TEXT_CHARS == 1_048_576


def test_ten_thousand_valid_with_duplicates_and_invalid_lines_are_accepted(harness):
    names = names_of(MAX_PROFILES)
    text = "\n".join(names + names[:500] + [f"#ruim{i}" for i in range(300)])
    response = harness.client.post(API, json={"text": text})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["total"] == MAX_PROFILES and body["pending"] == MAX_PROFILES
    assert body["duplicate_count"] == 500 and body["invalid_count"] == 300 and len(body["invalid_lines"]) == 50


def test_ten_thousand_and_one_is_refused(harness):
    response = harness.client.post(API, json={"text": "\n".join(names_of(MAX_PROFILES + 1))})
    assert response.status_code == 400 and response.json()["code"] == "too_many_profiles"
    assert harness.ctx.profile_analyses.count() == 0


def test_ten_thousand_full_links_fit_the_body(harness):
    text = "\n".join(f"https://www.instagram.com/u{index:029d}/" for index in range(MAX_PROFILES))
    payload = json.dumps({"text": text}).encode("utf-8")
    assert len(text) < MAX_TEXT_CHARS and len(payload) < MAX_BODY_BYTES
    response = harness.client.post(API, content=payload, headers={"content-type": "application/json"})
    assert response.status_code == 202 and response.json()["total"] == MAX_PROFILES


# ---- register_secret ----------------------------------------------------------------------------------------------
@pytest.fixture
def clean_secrets(monkeypatch):
    monkeypatch.setattr(app_logging, "_secrets", set(app_logging._secrets))
    monkeypatch.setattr(app_logging, "_secrets_sorted", app_logging._secrets_sorted)


def cookie_transport() -> httpx.MockTransport:
    served = iter(range(10**9))

    def handler(_request):
        serial = next(served)
        headers = [("content-type", "text/html"), ("set-cookie", f"csrftoken=token{serial:012d}; Path=/"),
                   ("set-cookie", f"mid=midvalue{serial:012d}; Path=/"),
                   ("set-cookie", f"ig_did=did{serial:012d}; Path=/")]
        return httpx.Response(200, text=profile_html(), headers=headers)

    return httpx.MockTransport(handler)


def test_anonymous_fetcher_does_not_grow_the_secrets(clean_secrets):
    before = set(app_logging._secrets)
    ordered = app_logging._secrets_sorted
    fetch = anonymous_profile_fetcher(InstagramRequestGate(interval=(0.0, 0.0)), transport=cookie_transport())
    for _ in range(1000):
        assert fetch("perfil", None).account_status == "active"
    assert app_logging._secrets == before and app_logging._secrets_sorted is ordered


def test_client_default_still_registers_received_cookies(clean_secrets):
    http = InstagramWebHttp.anonymous(transport=cookie_transport())
    http.get_document("/perfil/reels/")
    http.close()
    assert "token000000000000" in app_logging._secrets


def test_redaction_still_covers_anonymous_cookie_without_registering(clean_secrets):
    assert "valorsecreto" not in app_logging.redact("csrftoken=valorsecreto; mid=outrovalor")


# ---- plano das consultas ------------------------------------------------------------------------------------------
def plan_of(ctx, call) -> str:
    seen: list[str] = []
    ctx.db.conn.set_trace_callback(seen.append)
    try:
        call()
    finally:
        ctx.db.conn.set_trace_callback(None)
    query = next(sql for sql in seen if sql.startswith("SELECT"))
    return " | ".join(row["detail"] for row in ctx.db.fetchall(f"EXPLAIN QUERY PLAN {query}"))


def test_query_plans_do_not_sort_in_memory(harness):
    ctx = harness.ctx
    repo = ctx.profile_analyses
    analysis_id = seed_finished(ctx, 2_000, status="cancelled", last_done=1_000)
    ctx.db.execute("UPDATE profile_analyses SET status = 'running' WHERE id = ?", (analysis_id,))
    ctx.db.execute("UPDATE profile_analysis_items SET state = 'pending' WHERE state = 'cancelled'")
    ctx.db.execute("ANALYZE")

    items = plan_of(ctx, lambda: repo.items(analysis_id, limit=50, offset=500))
    keyset = plan_of(ctx, lambda: next(repo.iter_items(analysis_id)))
    following = plan_of(ctx, lambda: repo.next_item(analysis_id))
    assert "USE TEMP B-TREE" not in items and "ix_profile_items_position" in items
    assert "USE TEMP B-TREE" not in keyset and "ix_profile_items_position" in keyset
    assert "USE TEMP B-TREE" not in following and "ix_profile_items_open" in following


def test_next_item_stays_cheap_with_a_long_queue(harness):
    ctx = harness.ctx
    repo = ctx.profile_analyses
    analysis_id = repo.create(names_of(MAX_PROFILES), invalid_count=0, duplicate_count=0, invalid_lines=[])["id"]
    moments = []
    for _ in range(50):
        started = time.perf_counter()
        repo.next_item(analysis_id)
        moments.append(time.perf_counter() - started)
    assert statistics.median(moments) < 0.001


# ---- tempos -------------------------------------------------------------------------------------------------------
def median_ms(call, runs: int = 7) -> float:
    moments = []
    for _ in range(runs):
        started = time.perf_counter()
        call()
        moments.append((time.perf_counter() - started) * 1000)
    return statistics.median(moments)


def test_read_times_with_twenty_analyses_of_ten_thousand(harness):
    ctx = harness.ctx
    ids = [seed_finished(ctx, MAX_PROFILES) for _ in range(19)]
    open_id = ctx.profile_analyses.create(names_of(MAX_PROFILES), invalid_count=0, duplicate_count=0,
                                          invalid_lines=[])["id"]
    ctx.db.execute("UPDATE profile_analyses SET status = 'running' WHERE id = ?", (open_id,))

    assert analysis_of(harness, ids[0])["summary"]["active"] == 4_000
    assert median_ms(lambda: harness.client.get(f"{API}/{ids[0]}")) < 30
    assert median_ms(lambda: harness.client.get(f"{API}/{open_id}")) < 30     # aberta: varre os itens
    assert median_ms(lambda: harness.client.get(API)) < 200
    assert median_ms(lambda: harness.client.get(f"{API}/{ids[0]}/items", params={"limit": 50, "offset": 9_950})) < 30


def test_stored_stats_match_a_scan_of_the_items(harness):
    ctx = harness.ctx
    repo = ctx.profile_analyses
    analysis_id = seed_finished(ctx, 500, status="cancelled", last_done=300)
    stored = repo.stats([analysis_id])[analysis_id]
    ctx.db.execute("UPDATE profile_analyses SET final_stats = NULL WHERE id = ?", (analysis_id,))
    assert repo.stats([analysis_id])[analysis_id] == stored
    assert stored["done"] == 300 and stored["cancelled"] == 200 and stored["pending"] == 0


def test_every_way_of_ending_stores_the_stats(harness):
    ctx = harness.ctx
    repo = ctx.profile_analyses
    clock = SimClock()
    limited = InstagramWebError("rate_limited", "limite")
    fetcher = ScriptedFetcher(clock, {"b1": [limited]})
    runner = install_runner(harness, fetcher, clock)
    completed = create(harness, "a1")["id"]
    runner.run_next()
    interrupted = create(harness, "b1", "b2")["id"]
    runner.run_next()
    queued = create(harness, "c1")["id"]
    harness.client.post(f"{API}/{queued}/cancel")
    running = create(harness, "d1", "d2")["id"]
    cancelled_after(harness, fetcher, runner, running, "d1")

    for analysis_id, done, cancelled in ((completed, 1, 0), (interrupted, 0, 2), (queued, 0, 1), (running, 1, 1)):
        stored = json.loads(repo.get(analysis_id)["final_stats"])
        assert (stored["done"] or 0) == done and stored["cancelled"] == cancelled
    # O histórico lê o gravado: uma mudança nos itens que não passe por uma retomada não aparece.
    ctx.db.execute("UPDATE profile_analysis_items SET account_status = 'private' WHERE analysis_id = ?", (completed,))
    assert analysis_of(harness, completed)["summary"]["active"] == 1


def test_item_total_comes_from_the_analysis(harness):
    analysis_id = seed_finished(harness.ctx, 300)
    harness.ctx.db.execute("UPDATE profile_analyses SET total = 299 WHERE id = ?", (analysis_id,))
    assert harness.client.get(f"{API}/{analysis_id}/items", params={"limit": 2}).json()["total"] == 299


# ---- laço completo ------------------------------------------------------------------------------------------------
def test_full_loop_of_ten_thousand_queries_each_profile_once_and_persists_before_the_next(harness):
    ctx = harness.ctx
    clock = SimClock()
    fetcher = ScriptedFetcher(clock)
    runner = install_runner(harness, fetcher, clock)
    analysis_id = harness.client.post(API, json={"text": "\n".join(names_of(MAX_PROFILES))}).json()["id"]
    sequence = {"position": 0}

    def previous_is_saved(username):
        position = sequence["position"]
        if position:
            state, checked = ctx.db.fetchone(
                "SELECT state, checked_at FROM profile_analysis_items WHERE analysis_id = ? AND position = ?",
                (analysis_id, position))
            assert state == "done" and checked
        sequence["position"] = position + 1

    fetcher.before = previous_is_saved
    assert runner.run_next() is True
    assert Counter(fetcher.calls) == Counter(names_of(MAX_PROFILES)) and fetcher.calls == names_of(MAX_PROFILES)
    done = analysis_of(harness, analysis_id)
    assert done["status"] == "completed" and done["done"] == MAX_PROFILES and done["pending"] == 0
    assert done["summary"]["active"] == MAX_PROFILES
    assert ctx.db.scalar("SELECT COUNT(DISTINCT checked_at) FROM profile_analysis_items") == MAX_PROFILES
    # O ritmo não mudou: mesmos descansos, mesmo intervalo mínimo entre consultas.
    assert sum(1 for wait in clock.waits if wait == 45.0) == (MAX_PROFILES - 1) // REST_EVERY
    assert len(clock.waits) == MAX_PROFILES - 1 and min(clock.waits) == 3.0


class LeanClock:
    """Sem histórico: o consumo medido é o do executor, não o da lista de esperas de um relógio de teste."""

    def __init__(self) -> None:
        self.moment = START

    def now(self) -> datetime:
        return self.moment

    def wait(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)


def loop_peak(harness_factory, size: int) -> int:
    harness = harness_factory()
    ctx = harness.ctx
    clock = LeanClock()
    ctx.profile_fetcher = lambda username, _cancel: snapshot(username)
    runner = ProfileAnalysisRunner(ctx, clock=clock.now, wait=clock.wait, rng=lambda low, _high: low)
    analysis_id = ctx.profile_analyses.create(names_of(size), invalid_count=0, duplicate_count=0,
                                              invalid_lines=[])["id"]
    gc.collect()
    tracemalloc.start()
    try:
        assert runner.run_next() is True
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert ctx.profile_analyses.get(analysis_id)["status"] == "completed"
    return peak


def test_loop_memory_does_not_grow_with_the_number_of_profiles(harness_factory):
    small = loop_peak(harness_factory, 2_000)
    large = loop_peak(harness_factory, MAX_PROFILES)
    print(f"pico do laço: 2.000 = {small / 1024:.0f} KiB, 10.000 = {large / 1024:.0f} KiB")
    assert large < small * 1.25 + 128 * 1024


# ---- exportação ---------------------------------------------------------------------------------------------------
@pytest.fixture
def no_temp_left(tmp_path, monkeypatch):
    folder = tmp_path / "temporarios"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    yield folder
    assert list(folder.iterdir()) == []


def test_export_peak_of_ten_thousand_stays_under_50_mb(harness, no_temp_left):
    analysis_id = seed_finished(harness.ctx, MAX_PROFILES)
    gc.collect()
    tracemalloc.start()
    try:
        _name, file, size = report_zip(harness.ctx, analysis_id)
        with file:
            read = 0
            while block := file.read(64 * 1024):
                read += len(block)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    print(f"pico da exportação de 10.000: {peak / MIB:.2f} MiB; ZIP de {size / MIB:.2f} MiB")
    assert read == size and peak < 50 * MIB


def read_sheet(content: bytes) -> tuple[list[list[str]], dict[str, str]]:
    rows: list[list[str]] = []
    refs: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(content)) as package, package.open("xl/worksheets/sheet1.xml") as sheet:
        for _event, element in ElementTree.iterparse(sheet):
            if element.tag == f"{NS}row":
                rows.append(["".join(cell.itertext()) for cell in element])
                element.clear()
            elif element.tag in (f"{NS}dimension", f"{NS}autoFilter"):
                refs[element.tag.removeprefix(NS)] = element.get("ref")
    return rows, refs


def test_zip_of_ten_thousand_reads_back_whole(harness, no_temp_left):
    analysis_id = seed_finished(harness.ctx, MAX_PROFILES, status="cancelled", last_done=9_000)
    response = harness.client.get(f"{API}/{analysis_id}/report")
    assert response.status_code == 200
    assert int(response.headers["content-length"]) == len(response.content)
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.testzip() is None
        names = {name.rsplit(".", 1)[1]: name for name in archive.namelist()}
        sheet, refs = read_sheet(archive.read(names["xlsx"]))
        with io.TextIOWrapper(archive.open(names["csv"]), encoding="utf-8-sig", newline="") as text:
            table = list(csv.reader(text, delimiter=";"))
        page = archive.read(names["html"]).decode("utf-8")

    assert len(sheet) == MAX_PROFILES + 1 and len(table) == MAX_PROFILES + 1
    assert refs == {"dimension": f"A1:N{MAX_PROFILES + 1}", "autoFilter": f"A1:N{MAX_PROFILES + 1}"}
    body = page[page.index("<tbody>"):page.index("</tbody>")]
    html_rows = re.findall(r"<tr>.*?</tr>", body)
    assert len(html_rows) == MAX_PROFILES and "—" not in page
    assert [row[0] for row in table[1:]] == names_of(MAX_PROFILES)
    assert [row[0] for row in sheet[1:]] == [f"@{name}" for name in names_of(MAX_PROFILES)]

    labels = {"active": "Ativa", "private": "Privada", "unavailable": "Indisponível", "unverified": "Não verificado"}
    for position in range(1, 9_001):
        row = expected_row(position)
        for found in (table[position], sheet[position]):
            assert found[10:13] == [labels[row["status"]], row["date"], row["time"]], position
        cells = html_rows[position - 1]
        assert f">{row['date']}</td>" in cells and f">{row['time']}</td>" in cells
        if row["status"] in ("active", "private"):
            expected = [{"yes": "Sim", "no": "Não"}[row[key]] for key in ("stories", "highlights")]
            assert table[position][8:10] == expected and sheet[position][8:10] == expected
        else:
            assert table[position][2:10] == sheet[position][2:10] == ["Não verificado"] * 8
            assert "Não verificado" in cells
    for position in range(9_001, MAX_PROFILES + 1):
        assert table[position][2:10] == ["Não consultado"] * 8 and table[position][10] == "Cancelado"
        assert sheet[position][10] == "Cancelado"
        assert 'colspan="5"' in html_rows[position - 1] and 'colspan="2"' in html_rows[position - 1]


def test_iter_items_reads_in_blocks_without_repeating(harness):
    ctx = harness.ctx
    analysis_id = seed_finished(ctx, 1_203)
    seen: list[str] = []
    ctx.db.conn.set_trace_callback(seen.append)
    positions = [item["position"] for item in ctx.profile_analyses.iter_items(analysis_id, page=500)]
    ctx.db.conn.set_trace_callback(None)
    assert positions == list(range(1, 1_204)) and len(seen) == 4


def test_items_has_no_unlimited_form(harness):
    with pytest.raises(TypeError):
        harness.ctx.profile_analyses.items(1)


def test_report_on_disk_leaves_no_temporary(harness, no_temp_left, monkeypatch):
    monkeypatch.setattr(profile_analysis_service, "REPORT_SPOOL_BYTES", 1024)
    analysis_id = seed_finished(harness.ctx, 2_000)
    _name, file, size = report_zip(harness.ctx, analysis_id)
    assert file._rolled and size > 1024 and len(list(no_temp_left.iterdir())) == 1
    file.close()
    assert harness.client.get(f"{API}/{analysis_id}/report").status_code == 200


def test_report_failure_leaves_no_temporary(harness, no_temp_left, monkeypatch):
    def broken(*_args):
        raise OSError("disco cheio")

    real = tempfile.SpooledTemporaryFile
    created = []

    def spy(**kwargs):
        created.append(real(**kwargs))
        return created[-1]

    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", spy)
    monkeypatch.setattr(profile_analysis_service, "REPORT_SPOOL_BYTES", 1024)
    monkeypatch.setattr(profile_report, "write_html", broken)
    analysis_id = seed_finished(harness.ctx, 2_000)
    with pytest.raises(OSError, match="disco cheio"):
        report_zip(harness.ctx, analysis_id)
    assert len(created) == 1 and created[0].closed


def payload_file():
    file = tempfile.TemporaryFile()
    file.write(b"x" * (300 * 1024))
    file.seek(0)
    return file


def report_response(file) -> ReportResponse:
    return ReportResponse(file, media_type="application/zip", headers={"content-length": str(300 * 1024)})


def test_report_response_delivers_in_blocks_and_closes(no_temp_left):
    file = payload_file()
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        await asyncio.sleep(3600)

    asyncio.run(report_response(file)({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
    blocks = [message["body"] for message in sent[1:] if message["body"]]
    assert file.closed and len(blocks) == 5 and sum(map(len, blocks)) == 300 * 1024


def test_report_response_closes_when_the_client_disconnects(no_temp_left):
    file = payload_file()
    sent: list[dict] = []

    async def send(message):
        sent.append(message)
        if len(sent) == 2:
            raise OSError("cliente saiu")

    async def receive():
        await asyncio.sleep(3600)

    with pytest.raises((ClientDisconnect, OSError)):
        asyncio.run(report_response(file)({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
    assert file.closed


def test_report_response_closes_on_disconnect_notice_of_older_servers(no_temp_left):
    file = payload_file()

    async def send(_message):
        await asyncio.sleep(0)

    async def receive():
        return {"type": "http.disconnect"}

    asyncio.run(report_response(file)({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send))
    assert file.closed


def test_report_response_closes_before_the_first_byte_is_sent(no_temp_left):
    file = payload_file()

    async def send(_message):
        raise RuntimeError("falha antes de enviar")

    async def receive():
        await asyncio.sleep(3600)

    with pytest.raises(RuntimeError):
        asyncio.run(report_response(file)({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
    assert file.closed


# ---- retomada -----------------------------------------------------------------------------------------------------
def resume(harness, analysis_id: int) -> httpx.Response:
    return harness.client.post(f"{API}/{analysis_id}/resume")


def done_rows(harness, analysis_id: int) -> dict[str, dict]:
    return {item["username"]: item for item in items_of(harness, analysis_id) if item["state"] == "done"}


def cancelled_after(harness, fetcher, runner, analysis_id: int, last: str) -> None:
    def cancel(username):
        if username == last:
            harness.client.post(f"{API}/{analysis_id}/cancel")

    fetcher.before = cancel
    runner.run_next()
    fetcher.before = None


def test_resume_cancelled_never_repeats_done_and_finishes_whole(harness):
    clock = SimClock()
    results = {"p00001": [snapshot("p00001", "unverified", error_code="x", error_message="Falhou.")]}
    fetcher = ScriptedFetcher(clock, results)
    runner = install_runner(harness, fetcher, clock)
    names = names_of(20, "p")
    created = create(harness, *names)
    analysis_id = created["id"]
    cancelled_after(harness, fetcher, runner, analysis_id, "p00004")
    first = analysis_of(harness, analysis_id)
    assert first["status"] == "cancelled" and first["resumable"] is True and first["done"] == 5
    kept = done_rows(harness, analysis_id)
    calls = list(fetcher.calls)

    response = resume(harness, analysis_id)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending" and body["resumable"] is False and body["report_ready"] is False
    assert body["done"] == 5 and body["pending"] == 15 and body["summary"]["cancelled"] == 0
    assert body["finished_at"] is None and body["started_at"] == first["started_at"]
    assert body["created_at"] == first["created_at"]

    clock.tick(3600)
    runner.run_next()
    assert fetcher.calls[len(calls):] == names[5:]
    assert Counter(fetcher.calls) == Counter(names)
    done = analysis_of(harness, analysis_id)
    assert done["status"] == "completed" and done["done"] == 20 and done["started_at"] == first["started_at"]
    assert done["finished_at"] and done["resumable"] is False and done["summary"]["cancelled"] == 0
    assert {name: row for name, row in done_rows(harness, analysis_id).items() if name in kept} == kept

    report = harness.client.get(f"{API}/{analysis_id}/report")
    with zipfile.ZipFile(io.BytesIO(report.content)) as archive:
        csv_name = next(name for name in archive.namelist() if name.endswith(".csv"))
        rows = list(csv.reader(io.StringIO(archive.read(csv_name).decode("utf-8-sig"), newline=""), delimiter=";"))
    assert [row[0] for row in rows[1:]] == names
    assert rows[2][8] == "Não verificado" and "Não consultado" not in {row[8] for row in rows[1:]}


def test_resume_interrupted_clears_the_failure_and_keeps_results(harness):
    clock = SimClock()
    limited = InstagramWebError("rate_limited", "limite")
    fetcher = ScriptedFetcher(clock, {"a2": [limited, limited, limited, limited, snapshot("a2")]})
    runner = install_runner(harness, fetcher, clock)
    analysis_id = create(harness, "a1", "a2", "a3")["id"]
    runner.run_next()
    first = analysis_of(harness, analysis_id)
    assert first["status"] == "interrupted" and first["resumable"] is True and first["error"]
    kept = done_rows(harness, analysis_id)
    assert list(kept) == ["a1"]

    body = resume(harness, analysis_id).json()
    assert body["status"] == "pending" and body["error"] is None and body["waiting_until"] is None
    row = harness.ctx.profile_analyses.get(analysis_id)
    assert row["wait_level"] == 0 and row["cancel_requested"] == 0 and row["final_stats"] is None
    reopened = items_of(harness, analysis_id)[1:]
    assert all(item["state"] == "pending" and item["error_code"] is None and item["error_message"] is None
               and item["attempts"] == 0 for item in reopened)

    calls = len(fetcher.calls)
    runner.run_next()
    assert fetcher.calls[calls:] == ["a2", "a3"]
    done = analysis_of(harness, analysis_id)
    assert done["status"] == "completed" and done["error"] is None and done["summary"]["active"] == 3
    assert done_rows(harness, analysis_id)["a1"] == kept["a1"]


def test_resume_wakes_the_runner(harness):
    analysis_id = create(harness, "a1")["id"]
    harness.client.post(f"{API}/{analysis_id}/cancel")
    harness.ctx.profile_runner.wake = MagicMock()
    assert resume(harness, analysis_id).status_code == 200
    harness.ctx.profile_runner.wake.assert_called_once_with()


def test_queued_analysis_cancelled_gets_started_at_when_resumed(harness):
    clock = SimClock()
    runner = install_runner(harness, ScriptedFetcher(clock), clock)
    analysis_id = create(harness, "a1")["id"]
    harness.client.post(f"{API}/{analysis_id}/cancel")
    assert analysis_of(harness, analysis_id)["started_at"] is None
    resume(harness, analysis_id)
    runner.run_next()
    assert analysis_of(harness, analysis_id)["started_at"] == stamp(clock.moment - timedelta(seconds=1.5))


@pytest.mark.parametrize("status", ["pending", "running", "waiting", "completed"])
def test_resume_is_refused_outside_interrupted_and_cancelled(harness, status):
    analysis_id = create(harness, "a1")["id"]
    harness.ctx.db.execute("UPDATE profile_analyses SET status = ? WHERE id = ?", (status, analysis_id))
    # Com item `cancelled` de propósito: quem recusa é o status, não a falta do que retomar.
    harness.ctx.db.execute("UPDATE profile_analysis_items SET state = 'cancelled' WHERE analysis_id = ?",
                           (analysis_id,))
    response = resume(harness, analysis_id)
    assert response.status_code == 409 and response.json()["code"] == "analysis_not_resumable"
    assert harness.ctx.profile_analyses.get(analysis_id)["status"] == status


def test_cancelled_without_profiles_left_is_not_resumable(harness):
    clock = SimClock()
    fetcher = ScriptedFetcher(clock)
    runner = install_runner(harness, fetcher, clock)
    analysis_id = create(harness, "a1")["id"]
    cancelled_after(harness, fetcher, runner, analysis_id, "a1")
    body = analysis_of(harness, analysis_id)
    assert body["status"] == "cancelled" and body["resumable"] is False
    assert resume(harness, analysis_id).status_code == 409
    assert analysis_of(harness, analysis_id)["status"] == "cancelled"


def test_double_click_second_resume_has_no_effect(harness):
    analysis_id = create(harness, "a1", "a2")["id"]
    harness.client.post(f"{API}/{analysis_id}/cancel")
    assert resume(harness, analysis_id).status_code == 200
    harness.ctx.db.execute("UPDATE profile_analysis_items SET attempts = 7 WHERE analysis_id = ?", (analysis_id,))
    response = resume(harness, analysis_id)
    assert response.status_code == 409 and response.json()["code"] == "analysis_not_resumable"
    assert {item["attempts"] for item in items_of(harness, analysis_id)} == {7}


def test_concurrent_resume_has_one_winner(harness):
    analysis_id = create(harness, "a1", "a2")["id"]
    harness.client.post(f"{API}/{analysis_id}/cancel")
    repo = harness.ctx.profile_analyses
    barrier = threading.Barrier(4)
    results: list[bool] = []

    def attempt():
        barrier.wait()
        results.append(repo.resume_analysis(analysis_id))

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, False, False, True]


def test_canary_after_resume_never_reopens_older_results(harness):
    clock = SimClock()
    names = [f"u{index:02d}" for index in range(CANARY_EVERY + 4)]
    answers = {name: [snapshot(name, "unavailable", error_code="profile_unavailable")] for name in names}
    fetcher = ScriptedFetcher(clock, answers)
    runner = install_runner(harness, fetcher, clock)
    analysis_id = create(harness, *names)["id"]
    cancelled_after(harness, fetcher, runner, analysis_id, names[CANARY_EVERY - 2])
    kept = done_rows(harness, analysis_id)
    assert len(kept) == CANARY_EVERY - 1 and CANARY_PROFILE not in fetcher.calls
    assert resume(harness, analysis_id).status_code == 200
    resumed_at = len(fetcher.calls)

    # Bloqueio depois da retomada: o canário reprova, mas só o que veio depois dela poderia voltar para a fila.
    fetcher.results[CANARY_PROFILE] = [snapshot(CANARY_PROFILE, "unavailable")]
    runner.run_next()
    assert CANARY_PROFILE not in fetcher.calls[resumed_at:]    # menos de 10 novos: o canário nem foi chamado
    assert Counter(fetcher.calls) == Counter(names)
    assert {name: row for name, row in done_rows(harness, analysis_id).items() if name in kept} == kept


def test_restart_after_resume_keeps_the_canary_boundary(harness_factory):
    first = harness_factory()
    clock = SimClock()
    names = [f"u{index:02d}" for index in range(CANARY_EVERY + 1)]
    answers = {name: [snapshot(name, "unavailable")] for name in names}
    fetcher = ScriptedFetcher(clock, answers)
    runner = install_runner(first, fetcher, clock)
    analysis_id = create(first, *names)["id"]
    cancelled_after(first, fetcher, runner, analysis_id, names[3])
    assert resume(first, analysis_id).status_code == 200
    assert first.ctx.profile_analyses.get(analysis_id)["resume_after"] == 4
    root = first.root
    first.__exit__(None, None, None)

    second = harness_factory(root=root)
    assert analysis_of(second, analysis_id)["status"] == "pending"
    answers = {name: [snapshot(name, "unavailable")] for name in names}
    after = ScriptedFetcher(clock, answers)
    install_runner(second, after, clock).run_next()
    assert after.calls == names[4:]      # 11 Indisponíveis seguidos, mas só 7 depois da retomada: sem canário
