"""Análise de 10.000 perfis com o backend como PROCESSO real (uvicorn + profile_restart_app, fetcher falso, sem rede) e
kill -9 (taskkill /F /T). Rodar: `pytest -m slow tests/test_profile_restart_real.py`. Porta livre, dados em tmp_path."""

from __future__ import annotations

import csv
import io
import os
import socket
import sqlite3
import subprocess
import sys
import time
import zipfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from xml.etree import ElementTree

import httpx
import pytest

from app.jobs.profile_analysis_runner import REREAD_CODES

BACKEND = Path(__file__).resolve().parents[1]
HEADERS = {"X-Media-Tool-Client": "pytest"}
API = "/profile-analyses"
SIZE = 10_000
CANARY = "instagram"
ROW_TAG = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"
pytestmark = pytest.mark.slow


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait(predicate: Callable[[], object], timeout: float = 240.0, interval: float = 0.02, what: str = ""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timeout esperando: {what}")


class Server:
    def __init__(self, root: Path):
        self.root = root
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None
        self.calls = root / "calls.log"
        self.temp = root / "tmp"
        self.temp.mkdir()
        self.db_path = root / "data" / "media-tool.db"
        self.runs = 0

    def start(self) -> None:
        paths = [str(BACKEND), str(BACKEND / "tests"), os.environ.get("PYTHONPATH", "")]
        env = {**os.environ, "RT_ROOT": str(self.root), "RT_PORT": str(self.port), "PROFILE_ZERO_INTERVALS": "1",
               "PROFILE_FAKE_DELAY": "0.001", "PROFILE_CALL_LOG": str(self.calls), "PYTHONPATH": os.pathsep.join(paths),
               "TMP": str(self.temp), "TEMP": str(self.temp), "TMPDIR": str(self.temp)}
        self.runs += 1
        log = open(self.root / f"server-{self.runs}.log", "wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "profile_restart_app:app", "--host", "127.0.0.1",
             "--port", str(self.port), "--log-level", "warning"],
            cwd=BACKEND, env=env, stdout=log, stderr=subprocess.STDOUT)

        def healthy() -> bool:
            if self.proc.poll() is not None:
                raise AssertionError((self.root / f"server-{self.runs}.log").read_text(errors="replace")[-2000:])
            try:
                return httpx.get(f"{self.base}/api/health", timeout=1).status_code == 200
            except httpx.HTTPError:
                return False

        _wait(healthy, 30, 0.1, "servidor saudável")

    def kill(self) -> None:
        assert self.proc is not None
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)], capture_output=True, check=False)
        self.proc.wait(timeout=20)
        self.proc = None
        time.sleep(0.3)

    def api(self, method: str, path: str, **kwargs) -> httpx.Response:
        return httpx.request(method, f"{self.base}/api{path}", headers=HEADERS, timeout=60, **kwargs)

    def sql(self, query: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(query, params).fetchall()
        finally:
            conn.close()

    def done_count(self, analysis_id: int) -> int:
        return self.sql("SELECT COUNT(*) AS n FROM profile_analysis_items WHERE analysis_id = ? AND state = 'done'",
                        (analysis_id,))[0]["n"]

    def checked(self, analysis_id: int) -> dict[int, str]:
        return {row["position"]: row["checked_at"] for row in self.sql(
            "SELECT position, checked_at FROM profile_analysis_items WHERE analysis_id = ? AND state = 'done'",
            (analysis_id,))}

    def status(self, analysis_id: int) -> str:
        return self.sql("SELECT status FROM profile_analyses WHERE id = ?", (analysis_id,))[0]["status"]

    def fetched(self) -> Counter:
        calls = Counter(self.calls.read_text(encoding="utf-8").split())
        calls.pop(CANARY, None)
        return calls


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "rt"
    root.mkdir()
    instance = Server(root)
    yield instance
    if instance.proc is not None:
        instance.kill()


def start_analysis(srv: Server, prefix: str) -> tuple[int, list[str]]:
    names = [f"{prefix}{index:05d}" for index in range(SIZE)]
    response = srv.api("POST", API, json={"text": "\n".join(names)})
    assert response.status_code == 202, response.text
    assert response.json()["total"] == SIZE
    return response.json()["id"], names


def assert_one_row_per_profile(srv: Server, analysis_id: int, names: list[str]) -> None:
    rows = srv.sql(
        "SELECT position, username, state FROM profile_analysis_items WHERE analysis_id = ? ORDER BY position",
        (analysis_id,))
    assert [row["username"] for row in rows] == names
    assert [row["position"] for row in rows] == list(range(1, SIZE + 1))
    assert {row["state"] for row in rows} == {"done"}


def assert_report_is_whole(srv: Server, analysis_id: int, names: list[str]) -> None:
    response = srv.api("GET", f"{API}/{analysis_id}/report")
    assert response.status_code == 200
    assert int(response.headers["content-length"]) == len(response.content)
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        by_kind = {name.rsplit(".", 1)[1]: name for name in archive.namelist()}
        page = archive.read(by_kind["html"]).decode("utf-8")
        with io.TextIOWrapper(archive.open(by_kind["csv"]), encoding="utf-8-sig", newline="") as text:
            rows = list(csv.reader(text, delimiter=";"))
        with zipfile.ZipFile(io.BytesIO(archive.read(by_kind["xlsx"]))) as package, \
                package.open("xl/worksheets/sheet1.xml") as sheet:
            sheet_rows = sum(1 for _event, element in ElementTree.iterparse(sheet) if element.tag == ROW_TAG)
    assert [row[0] for row in rows[1:]] == names and sheet_rows == SIZE + 1
    assert page.count('<a href="https://www.instagram.com/') == SIZE and "—" not in page
    assert list(srv.temp.iterdir()) == []


def reread_names(srv, analysis_id: int) -> set[str]:
    """Resposta duvidosa tem UMA releitura de propósito (`REREAD_CODES`); qualquer outra repetição é defeito."""
    marks = ", ".join("?" for _ in REREAD_CODES)
    return {row["username"] for row in srv.sql(
        f"SELECT username FROM profile_analysis_items WHERE analysis_id = ? AND error_code IN ({marks})",
        (analysis_id, *sorted(REREAD_CODES)))}


def test_kill_restart_cancel_restart_and_resume_with_ten_thousand_profiles(server):
    srv = server
    srv.start()
    analysis_id, names = start_analysis(srv, "perfil")

    # ---- queda no meio da análise
    _wait(lambda: srv.done_count(analysis_id) >= 2500, what="análise em andamento")
    srv.kill()
    assert srv.status(analysis_id) == "running"
    before = srv.checked(analysis_id)
    assert 2500 <= len(before) < SIZE
    in_flight = {row["username"] for row in srv.sql(
        "SELECT username FROM profile_analysis_items WHERE analysis_id = ? AND state = 'running'", (analysis_id,))}
    assert len(in_flight) <= 1

    srv.start()
    _wait(lambda: srv.status(analysis_id) == "completed", what="análise concluída depois do reinício")
    calls = srv.fetched()
    assert set(calls) == set(names)
    repeated = {name for name, count in calls.items() if count > 1}
    print(f"queda com {len(before)} concluídos; em voo: {sorted(in_flight)}; repetidos: {sorted(repeated)}")
    rereads = reread_names(srv, analysis_id)
    assert rereads                                               # o fetcher falso devolve ~3% de respostas ilegíveis
    assert repeated == rereads | in_flight or repeated == rereads    # nada além da releitura e do que estava em voo
    wrong = {name: calls[name] for name in names if name not in in_flight and calls[name] != 1 + (name in rereads)}
    assert not wrong, f"1a análise: {wrong}, releituras {len(rereads)}"
    assert all(calls[name] <= 3 for name in in_flight)
    assert_one_row_per_profile(srv, analysis_id, names)
    after = srv.checked(analysis_id)
    assert {position: after[position] for position in before} == before
    assert srv.api("GET", f"{API}/{analysis_id}").json()["done"] == SIZE
    assert_report_is_whole(srv, analysis_id, names)

    # ---- cancelamento, novo reinício e retomada
    second_id, second_names = start_analysis(srv, "outro")
    _wait(lambda: srv.done_count(second_id) >= 1000, what="segunda análise em andamento")
    assert srv.api("POST", f"{API}/{second_id}/cancel").status_code == 200
    _wait(lambda: srv.status(second_id) == "cancelled", what="análise cancelada")
    cancelled = srv.checked(second_id)
    assert 1000 <= len(cancelled) < SIZE
    srv.kill()

    srv.start()
    body = srv.api("GET", f"{API}/{second_id}").json()
    assert body["status"] == "cancelled" and body["resumable"] is True and body["done"] == len(cancelled)
    resumed = srv.api("POST", f"{API}/{second_id}/resume")
    assert resumed.status_code == 200 and resumed.json()["resumable"] is False
    assert srv.api("POST", f"{API}/{second_id}/resume").status_code == 409
    _wait(lambda: srv.status(second_id) == "completed", what="análise retomada concluída")
    calls = srv.fetched()
    rereads = reread_names(srv, second_id)
    wrong = {name: calls[name] for name in second_names if calls[name] != 1 + (name in rereads)}
    # Só um perfil duvidoso apanhado pelo cancelamento entre a leitura e a releitura é lido mais uma vez na retomada.
    assert len(wrong) <= 1 and all(name in rereads and count == 3 for name, count in wrong.items()), wrong
    assert_one_row_per_profile(srv, second_id, second_names)
    after = srv.checked(second_id)
    assert {position: after[position] for position in cancelled} == cancelled
    assert_report_is_whole(srv, second_id, second_names)
    assert srv.sql("SELECT resume_after FROM profile_analyses WHERE id = ?", (second_id,))[0][0] == len(cancelled)
