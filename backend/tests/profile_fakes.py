"""Análise de Perfis, FAKE: a estrutura real da aba Reels anônima observada em 2026-10-31/11-01, só com valores
sintéticos, e um executor com relógio, espera e sorteio simulados. Nenhum teste aqui acessa a internet."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from app.integrations.instagram.web.profile_snapshot import ProfileSnapshot
from app.jobs.profile_analysis_runner import ProfileAnalysisRunner

MISSING = object()
NOW = 1_793_500_000
USER_PK = "424242"
OG = "1,234 seguidores, seguindo 56, 8,630 posts &#x2014; Veja as fotos e v&#xed;deos do Instagram"
ERROR_ROUTE = ('<script>{"canonicalRouteName":"comet.igweb.PolarisErrorRoute",'
               '"tracePolicy":"polaris.httpErrorPage"}</script>')


def clip(play_count: object = 100, code: str = "CODE") -> dict:
    node = {"code": code, "pk": "1", "like_and_view_counts_disabled": False, "clips_tab_pinned_user_ids": []}
    if play_count is not MISSING:
        node["play_count"] = play_count
    return {"node": node}


def clips_connection(edges: list, has_next: bool = False) -> dict:
    return {"edges": edges, "page_info": {"end_cursor": None, "has_next_page": has_next}}


def highlights_connection(count: int = 2) -> dict:
    edges = [{"node": {"__typename": "XDTReelDict", "id": f"h{i}", "title": "fake"}} for i in range(count)]
    return {"edges": edges, "page_info": {"end_cursor": None, "has_next_page": False}}


def user_envelope(username: str = "perfil", **overrides) -> dict:
    envelope = {
        "pk": USER_PK, "id": USER_PK, "username": username, "is_private": False, "is_unpublished": False,
        "latest_reel_media": NOW - 3600, "full_name": "Perfil Fake", "is_verified": False, "all_media_count": None,
        "lox_highlights_connection": highlights_connection(), "has_any_clips": True, "is_memorialized": False,
    }
    for key, value in overrides.items():
        if value is MISSING:
            envelope.pop(key, None)
        else:
            envelope[key] = value
    return envelope


def _script(envelope: dict) -> str:
    entry = ["RelayPrefetchedStreamCache", "next", [],
             ["adp_PolarisLoggedOutDesktopWWWProfileReelsTabContentQueryRelayPreloader_fake0",
              {"__bbox": {"result": {"data": {"xig_user_by_username": envelope}}}}]]
    payload = {"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"require": [entry]}}]]]}
    return f'<script type="application/json" data-sjs>{json.dumps(payload)}</script>'


def profile_html(user: object = MISSING, connection: object = MISSING, *, og: str | None = OG,
                 description: str | None = None, clips_pk: str = USER_PK, extra: str = "") -> str:
    """HTML com os dois envelopes. `connection=None` omite o envelope dos Reels; `user=None` omite o do usuário."""
    user = user_envelope() if user is MISSING else user
    connection = clips_connection([clip(100), clip(50)]) if connection is MISSING else connection
    head = ""
    if og is not None:
        head += f'<meta property="og:description" content="{og}" />'
    if description is not None:
        head += f'<meta content="{description}" name="description" />'
    body = _script(user) if user is not None else ""
    if connection is not None:
        body += _script({"pk": clips_pk, "polaris_clips_connection": connection})
    return f"<!DOCTYPE html><html><head>{head}</head><body>{body}{extra}</body></html>"


def snapshot(username: str, status: str = "active", **fields) -> ProfileSnapshot:
    if status in ("active", "private"):
        fields = {"display_name": "Nome", "posts_state": "known", "posts_count": 10, "posts_exact": 1,
                  "views_state": "known", "views_total": 150, "views_reels": 2, "stories": "yes", "highlights": "no",
                  **fields}
    return ProfileSnapshot(username, status, **fields)


MAX_SIM_WAITS = 20_000


class SimClockRunaway(BaseException):
    """BaseException: o laço do executor captura Exception, e o teste precisa falhar em vez de girar para sempre."""


class SimClock:
    """Relógio simulado: a espera do executor só avança o tempo (e guarda cada espera)."""

    def __init__(self) -> None:
        self.moment = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
        self.waits: list[float] = []
        self.on_wait = None

    def now(self) -> datetime:
        return self.moment

    def tick(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)
        if len(self.waits) > MAX_SIM_WAITS:
            raise SimClockRunaway(f"{len(self.waits)} esperas simuladas: laço sem saída")
        self.tick(seconds)
        if self.on_wait is not None:
            self.on_wait(self)


class ScriptedFetcher:
    """`results`: usuário -> lista de respostas, uma por chamada (exceção é levantada; a última se repete); `default`
    responde o resto. Cada resposta leva `step` segundos no relógio simulado."""

    def __init__(self, clock: SimClock | None = None, results: dict | None = None, *, default=None,
                 step: float = 1.5) -> None:
        self.clock = clock
        self.results = {name: list(answers) for name, answers in (results or {}).items()}
        self.default = default
        self.step = step
        self.calls: list[str] = []
        self.before = None

    def __call__(self, username: str, cancel) -> ProfileSnapshot:
        self.calls.append(username)
        if self.before is not None:
            self.before(username)
        if self.clock is not None:
            self.clock.tick(self.step)
        answers = self.results.get(username)
        answer = (answers.pop(0) if len(answers) > 1 else answers[0]) if answers else self.default
        if answer is None:
            answer = snapshot(username)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def install_runner(harness, fetcher, clock: SimClock, rng=lambda low, high: low, *,
                   start_fast: bool = False) -> ProfileAnalysisRunner:
    ctx = harness.ctx
    ctx.profile_fetcher = fetcher
    ctx.profile_runner = ProfileAnalysisRunner(ctx, clock=clock.now, wait=clock.wait, rng=rng, start_fast=start_fast)
    return ctx.profile_runner


def create(harness, *usernames: str) -> dict:
    response = harness.client.post("/api/profile-analyses", json={"text": "\n".join(usernames)})
    assert response.status_code == 202, response.text
    return response.json()


def items_of(harness, analysis_id: int) -> list[dict]:
    response = harness.client.get(f"/api/profile-analyses/{analysis_id}/items", params={"limit": 200})
    assert response.status_code == 200, response.text
    return response.json()["items"]


def analysis_of(harness, analysis_id: int) -> dict:
    response = harness.client.get(f"/api/profile-analyses/{analysis_id}")
    assert response.status_code == 200, response.text
    return response.json()
