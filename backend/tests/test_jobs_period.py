"""Filtro de período da Fila: `GET /api/jobs?created_from=&created_to=` sobre `jobs.created_at` (UTC)."""

from __future__ import annotations

from media_fakes import remote

# created_at (UTC, formato de utc_now) por ordem de criação; o job mais novo tem o maior id.
STAMPS = [
    "2026-10-27T10:00:00.000Z",
    "2026-10-28T02:59:59.999Z",  # 27/10 23:59:59.999 em UTC-3
    "2026-10-28T03:00:00.000Z",  # 28/10 00:00 em UTC-3 (borda inicial inclusiva)
    "2026-10-28T15:30:00.000Z",
    "2026-10-29T02:59:59.999Z",  # último milissegundo de 28/10 em UTC-3
    "2026-10-29T03:00:00.000Z",  # 29/10 00:00 em UTC-3 (borda final exclusiva)
]
# Dia 28/10 no fuso -03:00, como o frontend manda (toISOString do início do dia local e do dia seguinte).
DAY_FROM = "2026-10-28T03:00:00.000Z"
DAY_TO = "2026-10-29T03:00:00.000Z"


def _jobs_with_stamps(harness) -> tuple[dict, list[int]]:
    harness.downloader.items = [remote(f"P{i}") for i in range(len(STAMPS))]
    batch = harness.import_profile()
    harness.run_imports()
    ids = sorted(row["id"] for row in harness.ctx.db.fetchall("SELECT id FROM jobs"))
    assert len(ids) == len(STAMPS)
    for job_id, stamp in zip(ids, STAMPS, strict=True):
        harness.ctx.db.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (stamp, job_id))
    return batch, ids


def _get(harness, **params) -> dict:
    response = harness.client.get("/api/jobs", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_period_bounds_are_half_open(harness):
    batch, ids = _jobs_with_stamps(harness)
    day = _get(harness, created_from=DAY_FROM, created_to=DAY_TO)
    assert [j["id"] for j in day["items"]] == [ids[4], ids[3], ids[2]]
    assert day["total"] == 3 and day["counts"]["pending"] == 3 and sum(day["counts"].values()) == 3
    # Mesmo instante com outro fuso: o limite é convertido para UTC.
    same = _get(harness, created_from="2026-10-28T00:00:00-03:00", created_to="2026-10-29T00:00:00-03:00")
    assert [j["id"] for j in same["items"]] == [ids[4], ids[3], ids[2]]
    # Combinável com lote e status.
    assert _get(harness, created_from=DAY_FROM, created_to=DAY_TO, batch_id=batch["id"])["total"] == 3
    assert _get(harness, created_from=DAY_FROM, created_to=DAY_TO, status="completed")["total"] == 0
    # Só um dos limites.
    assert [j["id"] for j in _get(harness, created_from=DAY_TO)["items"]] == [ids[5]]
    assert [j["id"] for j in _get(harness, created_to=DAY_FROM)["items"]] == [ids[1], ids[0]]
    # Sem período: tudo, como antes.
    assert _get(harness)["total"] == len(STAMPS)
    # Período vazio (início = fim) é aceito e não traz nada.
    empty = _get(harness, created_from=DAY_FROM, created_to=DAY_FROM)
    assert empty["items"] == [] and empty["total"] == 0 and empty["next_before_id"] is None


def test_period_submillisecond_bounds(harness):
    _batch, ids = _jobs_with_stamps(harness)
    # 02:59:59.9995Z: o job em .999 é anterior ao início (fica fora) e anterior ao fim (fica dentro).
    after = _get(harness, created_from="2026-10-28T02:59:59.999500Z", created_to=DAY_TO)
    assert [j["id"] for j in after["items"]] == [ids[4], ids[3], ids[2]]
    before = _get(harness, created_from="2026-10-28T00:00:00Z", created_to="2026-10-28T02:59:59.999500Z")
    assert [j["id"] for j in before["items"]] == [ids[1]]


def test_counts_respect_period(harness):
    _batch, ids = _jobs_with_stamps(harness)
    harness.run_jobs()
    harness.ctx.db.execute("UPDATE jobs SET status = 'failed' WHERE id = ?", (ids[3],))
    day = _get(harness, created_from=DAY_FROM, created_to=DAY_TO)
    assert day["counts"]["completed"] == 2 and day["counts"]["failed"] == 1
    failed = _get(harness, created_from=DAY_FROM, created_to=DAY_TO, status="failed")
    assert [j["id"] for j in failed["items"]] == [ids[3]]
    assert failed["total"] == 1 and failed["counts"]["completed"] == 2
    everything = _get(harness)
    assert everything["counts"]["completed"] == len(STAMPS) - 1


def test_keyset_pagination_with_period(harness):
    _batch, ids = _jobs_with_stamps(harness)
    first = _get(harness, created_from=DAY_FROM, created_to=DAY_TO, limit=2)
    assert [j["id"] for j in first["items"]] == [ids[4], ids[3]]
    assert first["total"] == 3 and first["next_before_id"] == ids[3]
    second = _get(harness, created_from=DAY_FROM, created_to=DAY_TO, limit=2, before_id=first["next_before_id"])
    assert [j["id"] for j in second["items"]] == [ids[2]]
    assert second["total"] == 3 and second["next_before_id"] is None
    # Página que acaba exatamente no último job do período não aponta para jobs fora dele.
    exact = _get(harness, created_from=DAY_FROM, created_to=DAY_TO, limit=3)
    assert len(exact["items"]) == 3 and exact["next_before_id"] is None


def test_invalid_period_params(harness):
    for params in (
        {"created_from": "2026-10-28"},  # sem horário nem fuso
        {"created_from": "2026-10-28T03:00:00"},  # sem fuso
        {"created_to": "ontem"},
        {"created_to": "2026-13-01T00:00:00Z"},
        {"created_from": "2026-10-28T00:00:00 03:00"},  # "+" sem codificar vira espaço
        {"created_from": "0001-01-01T00:00:00+05:00"},  # fora do intervalo ao converter para UTC
        {"created_from": DAY_TO, "created_to": DAY_FROM},  # início depois do fim
        {"created_from": "x" * 65},
    ):
        response = harness.client.get("/api/jobs", params=params)
        assert response.status_code == 422, params
        assert response.json()["code"] == "validation_error", params
    # Vazio é o mesmo que ausente.
    assert harness.client.get("/api/jobs", params={"created_from": "", "created_to": ""}).status_code == 200
