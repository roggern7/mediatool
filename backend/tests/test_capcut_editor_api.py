"""Editor visual das Legendas CapCut (API): modelos com `design` (criar, listar, alterar), catálogo, arquivo de fonte
(lista branca), prévias (criadas, processadas por um processador falso, com prioridade sobre os jobs comuns, fora da
lista de jobs) e migração 0030. Núcleo real com instalação falsa do CapCut; processador falso; sem rede."""

from __future__ import annotations

import pytest
from capcut_editor_fakes import FADE_IN, FADE_OUT, LATO, MONTSERRAT, PRO_FONT, design, install_fakes
from capcut_jobs_fakes import FakeCapCutError, FakeCapCutProcessor, add_media

from app.db.connection import Database
from app.db.migrate import apply_migrations, revert_migration, schema_version
from app.integrations.capcut import catalog
from app.services import capcut_service


@pytest.fixture
def env(monkeypatch, tmp_path):
    data = install_fakes(monkeypatch, tmp_path)
    yield data
    catalog.reset_cache()


@pytest.fixture
def runner(harness):
    processor = FakeCapCutProcessor()
    harness.ctx.capcut_processor = processor
    return harness.ctx.capcut_runner, processor


def test_styles_with_design_crud(harness, env):
    styles = harness.client.get("/api/capcut/styles").json()
    builtin = styles[0]
    assert builtin["id"] == 0 and builtin["design"]["font"]["path"].endswith("SystemFont/en.ttf")
    assert builtin["design"]["size"] == 7.0 and builtin["design"]["stroke"]["width"] == 30

    body = design(uppercase=True, animation={"in": {"id": FADE_IN, "name": "Fade-in", "duration_ms": 400},
                                             "out": None, "loop": None})
    created = harness.client.post("/api/capcut/styles", json={"name": "Editor 1", "design": body})
    assert created.status_code == 201, created.text
    style = created.json()
    assert style["source_draft"] is None and style["design"]["uppercase"] is True
    assert style["design"]["animation"]["in"]["id"] == FADE_IN and style["summary"]["font"] == "Montserrat"
    assert style["summary"]["has_animation"] is True
    stored = capcut_service.repo(harness.ctx).get_style(style["id"])
    assert stored["style"]["uppercase"] is True and stored["style"]["extras"][0]["array"] == "material_animations"

    dup = harness.client.post("/api/capcut/styles", json={"name": "editor 1", "design": body})
    assert dup.status_code == 409 and dup.json()["code"] == "style_name_taken"

    changed = harness.client.put(f"/api/capcut/styles/{style['id']}",
                                 json={"name": "Editor 2", "design": design(color="#FF0000")})
    assert changed.status_code == 200, changed.text
    assert changed.json()["name"] == "Editor 2" and changed.json()["design"]["color"] == "#FF0000"
    assert changed.json()["summary"]["color"] == "#ff0000"
    keep_name = harness.client.put(f"/api/capcut/styles/{style['id']}", json={"design": design()})
    assert keep_name.json()["name"] == "Editor 2"
    assert harness.client.put("/api/capcut/styles/999", json={"design": design()}).status_code == 404

    # Modelo copiado de rascunho (sem design): 409 no PUT; listado com design null.
    repo = capcut_service.repo(harness.ctx)
    extracted = repo.create_style(name="Do rascunho", source_draft="Meu", style={"material": {}, "segment": {}},
                                  summary=None)
    response = harness.client.put(f"/api/capcut/styles/{extracted}", json={"design": design()})
    assert response.status_code == 409 and response.json()["code"] == "style_not_editable"
    listed = {s["id"]: s for s in harness.client.get("/api/capcut/styles").json()}
    assert listed[extracted]["design"] is None and listed[style["id"]]["design"]["font"]["id"] == MONTSERRAT
    other = harness.client.post("/api/capcut/styles", json={"name": "Outro", "design": design()}).json()
    clash = harness.client.put(f"/api/capcut/styles/{other['id']}", json={"name": "EDITOR 2", "design": design()})
    assert clash.status_code == 409 and clash.json()["code"] == "style_name_taken"


def test_style_create_validation(harness, env):
    win_outside = str(env["outside"] / "segredo.ttf")
    cases = [
        ({"name": "X", "design": design(color="branco")}, 422, "validation_error"),
        ({"name": "X", "design": design(), "draft_name": "Meu"}, 422, "validation_error"),
        ({"name": "X"}, 422, "validation_error"),
        ({"name": "X", "design": design(font={"source": "windows", "id": None, "path": win_outside, "name": "x"})},
         422, "invalid_design"),
        ({"name": "X", "design": design(font={"source": "capcut", "id": "123", "path": None, "name": "x"})},
         422, "invalid_design"),
        ({"name": "X", "design": design(animation={"in": {"id": FADE_OUT, "duration_ms": 400}})}, 422,
         "invalid_design"),
    ]
    for body, status, code in cases:
        response = harness.client.post("/api/capcut/styles", json=body)
        assert response.status_code == status, (body, response.text)
        assert response.json()["code"] == code, response.text
    assert harness.client.get("/api/capcut/styles").json()[1:] == []


def test_catalog_endpoints(harness, env):
    fonts = harness.client.get("/api/capcut/catalog/fonts", params={"source": "capcut"}).json()
    assert {f["id"] for f in fonts["items"]} == {MONTSERRAT, LATO} and fonts["total"] == 2
    pro = harness.client.get("/api/capcut/catalog/fonts", params={"source": "capcut", "include_pro": 1}).json()
    assert PRO_FONT in {f["id"] for f in pro["items"]}
    page = harness.client.get("/api/capcut/catalog/fonts", params={"limit": 2, "offset": 1}).json()
    assert len(page["items"]) == 2 and page["total"] > 2
    found = harness.client.get("/api/capcut/catalog/fonts", params={"q": "impact"}).json()
    assert [f["key"] for f in found["items"]] == ["windows:impact.ttf"]
    assert harness.client.get("/api/capcut/catalog/fonts", params={"source": "x"}).status_code == 422
    assert harness.client.get("/api/capcut/catalog/fonts", params={"limit": 0}).status_code == 422

    anims = harness.client.get("/api/capcut/catalog/animations", params={"type": "in"}).json()
    assert anims == {"items": [{"id": FADE_IN, "name": "Fade-in", "type": "in", "is_pro": False, "downloaded": True,
                                "default_duration_ms": 500}], "total": 1}
    assert harness.client.get("/api/capcut/catalog/animations", params={"type": "x"}).status_code == 422


def test_font_file_endpoint(harness, env):
    ok = harness.client.get("/api/capcut/fonts/file", params={"key": "windows:impact.ttf"})
    assert ok.status_code == 200 and ok.content == b"TTF-impact" and ok.headers["content-type"] == "font/ttf"
    otf = harness.client.get("/api/capcut/fonts/file", params={"key": "bundled:CapCutSansText-Bold.otf"})
    assert otf.status_code == 200 and otf.headers["content-type"] == "font/otf"
    assert harness.client.get("/api/capcut/fonts/file",
                              params={"key": f"capcut:{MONTSERRAT}"}).content == b"TTF-montserrat"
    for key in ("windows:../fora/segredo.ttf", "windows:..%5Cfora%5Csegredo.ttf", f"capcut:{LATO}", "bundled:x.ttf",
                "windows:C:\\fora\\segredo.ttf"):
        response = harness.client.get("/api/capcut/fonts/file", params={"key": key})
        assert response.status_code == 404 and response.json()["code"] == "font_not_found", key
        assert b"SEGREDO" not in response.content


def test_preview_runs_before_common_job_and_is_not_a_job(harness, env, runner):
    run, processor = runner
    video = add_media(harness, name="v1")
    newest = add_media(harness, name="v2")
    job = harness.client.post("/api/capcut/jobs", json={"media_ids": [video]}).json()["created"][0]

    created = harness.client.post("/api/capcut/previews", json={"design": design(), "start_s": 2, "seconds": 4})
    assert created.status_code == 202, created.text
    preview = created.json()
    assert preview["status"] == "pending" and len(preview["id"]) == 12
    view = harness.client.get(f"/api/capcut/previews/{preview['id']}").json()
    assert view["media_id"] == newest and view["file_url"] is None, "mídia padrão: o vídeo mais recente"

    assert run.run_next() is True
    [spec] = processor.specs
    assert spec.draft_name == f"MT_preview_{preview['id']}" and spec.output_name == preview["id"]
    assert spec.output_dir == harness.settings.data_dir / "capcut" / "previews"
    assert (spec.resolution, spec.keep_original_audio, spec.clip_start, spec.clip_duration) == ("720P", False, 2.0, 4.0)
    assert spec.sample_text == "Exemplo de legenda no seu estilo" and spec.transcript is None
    assert spec.style["material"]["font_id"] == MONTSERRAT and spec.highlight is True
    assert spec.highlight_color == "#FFD400"
    assert capcut_service.repo(harness.ctx).get_job(job["id"])["status"] == "pending", "o job comum espera"

    done = harness.client.get(f"/api/capcut/previews/{preview['id']}").json()
    assert done["status"] == "completed" and done["progress"] == 100
    assert done["file_url"] == f"/api/capcut/previews/{preview['id']}/file"
    file = harness.client.get(done["file_url"])
    assert file.status_code == 200 and file.content == b"FAKE-video-legendado"
    assert file.headers["content-type"] == "video/mp4"

    listing = harness.client.get("/api/capcut/jobs").json()
    assert [item["id"] for item in listing["items"]] == [job["id"]] and listing["total"] == 1
    assert sum(listing["counts"].values()) == 1

    assert run.run_next() is True
    finished = capcut_service.repo(harness.ctx).get_job(job["id"])
    assert finished["status"] == "completed" and finished["attempts"] == 1, "a prévia não gasta tentativa do job"
    assert run.run_next() is False


def test_preview_with_style_id_and_paused_queue(harness, env, runner):
    run, processor = runner
    video = add_media(harness, name="v1")
    harness.client.post("/api/capcut/queue/pause")
    response = harness.client.post("/api/capcut/previews", json={"style_id": 0, "media_id": video})
    assert response.status_code == 202, response.text
    assert run.run_next() is True, "a prévia roda mesmo com a fila de jobs pausada"
    assert processor.specs[0].style is None and processor.specs[0].clip_duration == 6.0


def test_preview_validation(harness, env, runner):
    image = add_media(harness, media_type="image", name="i1")
    assert harness.client.post("/api/capcut/previews", json={"style_id": 0}).json()["code"] == "preview_no_media"
    video = add_media(harness, name="v1")
    cases = [({}, 422), ({"design": design(), "style_id": 0}, 422), ({"style_id": 0, "seconds": 2}, 422),
             ({"style_id": 0, "seconds": 13}, 422), ({"style_id": 0, "start_s": -1}, 422),
             ({"style_id": 0, "media_id": image}, 422), ({"style_id": 0, "media_id": 999}, 404),
             ({"style_id": 77}, 404), ({"design": design(color="x")}, 422),
             ({"design": design(font={"source": "capcut", "id": "1", "path": None, "name": "x"})}, 422)]
    for body, status in cases:
        response = harness.client.post("/api/capcut/previews", json=body)
        assert response.status_code == status, (body, response.text)
    assert harness.client.get("/api/capcut/previews/abcdefabcdef").status_code == 404
    assert harness.client.get("/api/capcut/previews/..%2Fx").status_code in (404, 422)
    assert harness.client.get("/api/capcut/previews/ZZZ").status_code == 422
    assert video


def test_preview_cancel_wait_fail_and_replacement(harness, env, runner):
    run, processor = runner
    add_media(harness, name="v1")
    first = harness.client.post("/api/capcut/previews", json={"style_id": 0}).json()
    second = harness.client.post("/api/capcut/previews", json={"style_id": 0}).json()
    assert harness.client.get(f"/api/capcut/previews/{first['id']}").json()["status"] == "cancelled", \
        "uma prévia nova substitui a que ainda estava na fila"
    cancelled = harness.client.post(f"/api/capcut/previews/{second['id']}/cancel").json()
    assert cancelled["status"] == "cancelled" and run.run_next() is False

    processor.errors = [FakeCapCutError("desktop_busy", "PC em uso.", transient=True)]
    waiting = harness.client.post("/api/capcut/previews", json={"style_id": 0}).json()
    assert run.run_next() is True
    view = harness.client.get(f"/api/capcut/previews/{waiting['id']}").json()
    assert view["status"] == "waiting" and "PC está em uso" in view["detail"]
    assert run.run_next() is False, "espera ainda não venceu"
    harness.client.post(f"/api/capcut/previews/{waiting['id']}/cancel")

    processor.errors = [FakeCapCutError("ui_timeout", "Demorou.", transient=True)]
    failing = harness.client.post("/api/capcut/previews", json={"style_id": 0}).json()
    run.run_next()
    view = harness.client.get(f"/api/capcut/previews/{failing['id']}").json()
    assert view["status"] == "failed" and view["error_code"] == "ui_timeout" and view["error_message"] == "Demorou."
    assert harness.client.get(f"/api/capcut/previews/{failing['id']}/file").status_code == 404

    def cancel_while_running(spec, cancel):
        harness.client.post(f"/api/capcut/previews/{spec.output_name}/cancel")
        assert cancel.is_set()
        raise FakeCapCutError("cancelled", "Cancelado.")

    processor.during = cancel_while_running
    running = harness.client.post("/api/capcut/previews", json={"style_id": 0}).json()
    run.run_next()
    assert harness.client.get(f"/api/capcut/previews/{running['id']}").json()["status"] == "cancelled"


def test_preview_keeps_the_ten_most_recent_files(harness, env, runner):
    run, _ = runner
    add_media(harness, name="v1")
    folder = harness.settings.data_dir / "capcut" / "previews"
    folder.mkdir(parents=True)
    import os
    for i in range(12):
        old = folder / f"old{i:02d}.mp4"
        old.write_bytes(b"x")
        os.utime(old, (1_000_000 + i, 1_000_000 + i))
    harness.client.post("/api/capcut/previews", json={"style_id": 0})
    run.run_next()
    names = sorted(p.name for p in folder.glob("*.mp4"))
    assert len(names) == 10 and "old00.mp4" not in names and "old11.mp4" in names


def test_migration_0030_up_down(tmp_path):
    db = Database(tmp_path / "m.db")
    try:
        apply_migrations(db)
        assert schema_version(db) == 30
        columns = {row["name"] for row in db.fetchall("PRAGMA table_info(capcut_styles)")}
        assert "design" in columns
        db.execute("INSERT INTO capcut_styles (name, source_draft, style, created_at) VALUES ('a', 'r', '{}', 'x')")
        assert db.scalar("SELECT design FROM capcut_styles") is None, "existentes ficam NULL"
        revert_migration(db, 30)
        assert schema_version(db) == 29
        columns = {row["name"] for row in db.fetchall("PRAGMA table_info(capcut_styles)")}
        assert "design" not in columns and db.scalar("SELECT name FROM capcut_styles") == "a", "dados mantidos"
        assert apply_migrations(db) == [30]
    finally:
        db.close_all()
