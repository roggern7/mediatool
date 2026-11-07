"""Legendas CapCut: modo de exportação (configuração, status, registro no job/prévia, falha `isolated_unavailable`
sem fallback) e avisos do design (função pura + POST/PUT de modelos + prévia). Sem CapCut real."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from capcut_editor_fakes import FADE_IN, FADE_OUT, design, install_fakes
from capcut_jobs_fakes import FakeCapCutError, FakeCapCutProcessor, add_media

from app.integrations.capcut import catalog
from app.integrations.capcut.design import design_warnings
from app.jobs.capcut_runner import CapCutRunner
from app.services import capcut_service

FADE = {"id": FADE_IN, "name": "Fade-in", "duration_ms": 400}
FADE_OUT_ANIM = {"id": FADE_OUT, "name": "Fade-out", "duration_ms": 500}
BG_ON = {"enabled": True, "color": "#7C3AED", "opacity": 0.75, "radius": 0.3}


@pytest.fixture
def setup(harness, tmp_path):
    processor = FakeCapCutProcessor()
    harness.ctx.capcut_processor = processor
    runner = CapCutRunner(harness.ctx, clock=lambda: datetime(2026, 11, 7, 12, 0, tzinfo=UTC))
    harness.ctx.capcut_runner = runner

    def make_job() -> dict:
        media_id = add_media(harness, name=f"v{len(processor.specs)}")
        response = harness.client.post("/api/capcut/jobs", json={
            "media_ids": [media_id], "overrides": {"output_dir": str(tmp_path / "saida")}})
        assert response.status_code == 202, response.text
        return response.json()["created"][0]

    return harness, processor, runner, make_job


@pytest.fixture
def env(monkeypatch, tmp_path):
    data = install_fakes(monkeypatch, tmp_path)
    yield data
    catalog.reset_cache()


def test_export_mode_setting_default_update_and_validation(harness):
    settings = harness.client.get("/api/capcut/settings").json()
    assert settings["export_mode"] == "isolated", "padrão = isolado"
    changed = harness.client.put("/api/capcut/settings", json={"export_mode": "interactive"})
    assert changed.status_code == 200 and changed.json()["export_mode"] == "interactive"
    assert harness.client.get("/api/capcut/settings").json()["export_mode"] == "interactive"
    bad = harness.client.put("/api/capcut/settings", json={"export_mode": "fantasma"})
    assert bad.status_code == 422
    back = harness.client.put("/api/capcut/settings", json={"export_mode": None})
    assert back.json()["export_mode"] == "isolated", "null volta ao padrão"
    # valor estranho gravado no banco: lido como o padrão (sem 500)
    harness.ctx.app_settings.set(capcut_service.SETTINGS_KEY, {"export_mode": "velho"})
    assert harness.client.get("/api/capcut/settings").json()["export_mode"] == "isolated"


def test_status_reports_export_mode_and_isolated_availability(harness, monkeypatch):
    def missing():
        raise ImportError("sem núcleo")

    monkeypatch.setattr(capcut_service, "load_core", missing)
    body = harness.client.get("/api/capcut/status").json()
    assert body["export_mode"] == "isolated" and body["settings"]["export_mode"] == "isolated"
    assert body["isolated"] == {"available": False, "reason": None}

    from capcut_jobs_fakes import fake_core

    fake = fake_core()
    base = fake.status
    fake.status = lambda d: {**base(d), "isolated": {"available": True, "reason": None}}
    monkeypatch.setattr(capcut_service, "load_core", lambda: fake)
    harness.client.put("/api/capcut/settings", json={"export_mode": "interactive"})
    body = harness.client.get("/api/capcut/status").json()
    assert body["isolated"] == {"available": True, "reason": None} and body["export_mode"] == "interactive"


def test_job_uses_and_records_the_mode_configured_when_it_runs(setup):
    harness, processor, runner, make_job = setup
    first = make_job()
    assert first["export_mode"] is None, "ainda não rodou"
    harness.client.put("/api/capcut/settings", json={"export_mode": "interactive"})  # vale para o job já na fila
    assert runner.run_next() is True
    assert processor.specs[-1].export_mode == "interactive"
    view = harness.client.get(f"/api/capcut/jobs/{first['id']}").json()
    assert view["export_mode"] == "interactive" and view["options"]["export_mode"] == "interactive"
    harness.client.put("/api/capcut/settings", json={"export_mode": "isolated"})
    second = make_job()
    assert runner.run_next() is True
    assert processor.specs[-1].export_mode == "isolated"
    assert harness.client.get(f"/api/capcut/jobs/{second['id']}").json()["export_mode"] == "isolated"


def test_isolated_unavailable_fails_the_job_without_retry_or_wait(setup):
    harness, processor, runner, make_job = setup
    created = make_job()
    processor.errors.append(FakeCapCutError("isolated_unavailable", "Troque o modo para Interativo.", False))
    assert runner.run_next() is True
    job = capcut_service.repo(harness.ctx).get_job(created["id"])
    assert job["status"] == "failed" and job["error_code"] == "isolated_unavailable"
    assert job["attempts"] == 1 and job["wait_reason"] is None and job["next_attempt_at"] is None
    assert len(processor.specs) == 1 and processor.specs[0].export_mode == "isolated", "sem fallback"
    assert runner.run_next() is False


def test_design_warnings_rule():
    base = design()
    assert design_warnings(base) == []
    assert design_warnings(design(background=BG_ON)) == []
    assert design_warnings(design(animation={"in": FADE, "out": None, "loop": None})) == [], "sem fundo: ok"
    assert design_warnings(design(background=BG_ON, animation={"in": None, "out": None, "loop": FADE})) == [], \
        "loop não tem o problema"
    [only_in] = design_warnings(design(background=BG_ON, animation={"in": FADE, "out": None, "loop": None}))
    # mesmo texto do frontend (frontend/src/lib/captionDesign.test.ts)
    assert only_in == ("Com a caixa de fundo ligada, a animação de entrada do CapCut atua só no texto: a caixa de "
                       "fundo aparece vazia por alguns instantes. Para evitar, desligue a caixa de fundo ou tire essa "
                       "animação.")
    [only_out] = design_warnings(design(background=BG_ON, animation={"in": None, "out": FADE, "loop": None}))
    assert "animação de saída" in only_out
    [both] = design_warnings(design(background=BG_ON, animation={"in": FADE, "out": FADE, "loop": None}))
    assert "animação de entrada e de saída" in both
    assert design_warnings({}) == []


def test_styles_and_preview_return_design_warnings(harness, env):
    risky = design(background=BG_ON, animation={"in": FADE, "out": None, "loop": None})
    created = harness.client.post("/api/capcut/styles", json={"name": "Com fundo", "design": risky})
    assert created.status_code == 201, created.text
    assert len(created.json()["warnings"]) == 1, "avisa sem bloquear"
    ok = harness.client.put(f"/api/capcut/styles/{created.json()['id']}", json={"design": design()})
    assert ok.status_code == 200 and ok.json()["warnings"] == []
    again = harness.client.put(f"/api/capcut/styles/{created.json()['id']}", json={
        "design": design(background=BG_ON, animation={"in": None, "out": FADE_OUT_ANIM, "loop": None})})
    assert again.status_code == 200 and "saída" in again.json()["warnings"][0]
    listed = harness.client.get("/api/capcut/styles").json()
    assert all(isinstance(s["warnings"], list) for s in listed)

    processor = FakeCapCutProcessor()
    harness.ctx.capcut_processor = processor
    add_media(harness, name="v1")
    preview = harness.client.post("/api/capcut/previews", json={"design": risky})
    assert preview.status_code == 202, preview.text
    assert len(preview.json()["warnings"]) == 1
    pending = harness.client.get(f"/api/capcut/previews/{preview.json()['id']}").json()
    assert pending["export_mode"] is None and len(pending["warnings"]) == 1
    assert harness.ctx.capcut_runner.run_next() is True
    done = harness.client.get(f"/api/capcut/previews/{preview.json()['id']}").json()
    assert done["status"] == "completed" and done["export_mode"] == "isolated"
    assert processor.specs[0].export_mode == "isolated", "a prévia usa o modo configurado"
    assert done["warnings"][0] == preview.json()["warnings"][0] and "aviso falso" in done["warnings"]
    by_style = harness.client.post("/api/capcut/previews", json={"style_id": again.json()["id"]})
    assert by_style.status_code == 202 and len(by_style.json()["warnings"]) == 1
