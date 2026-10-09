from __future__ import annotations

from media_processor import ProcessingOptions

from app.db.repositories.common import canonical_json

PARAMS = {"metadata_cleanup": True, "trim_start": 1.0, "mirror": False, "speed": 1.1, "auto_adjust": True,
          "reencode": True}


def test_list_seeded_presets(harness):
    response = harness.client.get("/api/presets")
    assert response.status_code == 200
    names = [p["name"] for p in response.json()]
    assert names == ["Padrão", "Anti-duplicidade"]
    assert all(p["is_builtin"] for p in response.json())


def test_crud_flow(harness):
    client = harness.client
    created = client.post("/api/presets", json={"name": "  Meu preset  ", "params": PARAMS})
    assert created.status_code == 201, created.text
    preset = created.json()
    assert preset["name"] == "Meu preset" and preset["is_builtin"] is False and preset["params"] == PARAMS

    assert client.get(f"/api/presets/{preset['id']}").json()["name"] == "Meu preset"

    updated = client.put(f"/api/presets/{preset['id']}", json={"params": {**PARAMS, "speed": 1.5}})
    assert updated.status_code == 200
    assert updated.json()["params"]["speed"] == 1.5 and updated.json()["name"] == "Meu preset"

    renamed = client.put(f"/api/presets/{preset['id']}", json={"name": "Outro"})
    assert renamed.json()["name"] == "Outro"

    assert client.delete(f"/api/presets/{preset['id']}").status_code == 204
    assert client.get(f"/api/presets/{preset['id']}").status_code == 404


def test_builtin_is_editable_but_not_deletable(harness):
    client = harness.client
    builtin = client.get("/api/presets").json()[0]
    response = client.put(f"/api/presets/{builtin['id']}", json={"params": PARAMS})
    assert response.status_code == 200 and response.json()["params"] == PARAMS
    deleted = client.delete(f"/api/presets/{builtin['id']}")
    assert deleted.status_code == 409
    assert deleted.json()["code"] == "preset_builtin"


def test_duplicate_preset(harness):
    client = harness.client
    builtin = client.get("/api/presets").json()[0]
    first = client.post(f"/api/presets/{builtin['id']}/duplicate", json={})
    assert first.status_code == 201
    assert first.json()["name"] == "Padrão (cópia)" and first.json()["is_builtin"] is False
    assert first.json()["params"] == builtin["params"]
    second = client.post(f"/api/presets/{builtin['id']}/duplicate")
    assert second.json()["name"] == "Padrão (cópia 2)"
    named = client.post(f"/api/presets/{builtin['id']}/duplicate", json={"name": "Nome livre"})
    assert named.json()["name"] == "Nome livre"
    clash = client.post(f"/api/presets/{builtin['id']}/duplicate", json={"name": "nome LIVRE"})
    assert clash.status_code == 409 and clash.json()["code"] == "preset_name_taken"


def test_validation_rules(harness):
    client = harness.client
    cases = [
        {"name": "", "params": PARAMS},
        {"name": "x" * 61, "params": PARAMS},
        {"name": "com\ncontrole", "params": PARAMS},
        {"name": "ok", "params": {**PARAMS, "extra": 1}},
        {"name": "ok", "params": {**PARAMS, "speed": 2.5}},
        {"name": "ok", "params": {**PARAMS, "speed": 0.4}},
        {"name": "ok", "params": {**PARAMS, "trim_start": -1}},
        {"name": "ok", "params": {**PARAMS, "trim_start": 31}},
        {"name": "ok", "params": {**PARAMS, "mirror": "sim"}},
        {"name": "ok", "params": {k: v for k, v in PARAMS.items() if k != "mirror"}},
        {"name": "ok", "params": PARAMS, "is_builtin": True},
    ]
    for body in cases:
        response = client.post("/api/presets", json=body)
        assert response.status_code == 422, body
        assert response.json()["code"] == "validation_error"
    assert client.post("/api/presets", json={"name": "Padrão", "params": PARAMS}).status_code == 409
    assert client.get("/api/presets/0").status_code == 422
    assert client.get("/api/presets/999").status_code == 404


def test_deleting_preset_keeps_pending_job_snapshot(harness):
    client = harness.client
    preset = client.post("/api/presets", json={"name": "Temporário", "params": PARAMS}).json()
    ctx = harness.ctx
    media_id = ctx.media.insert({"media_type": "image", "original_path": "originals/x.jpg",
                                 "original_sha256": "c" * 64, "file_size": 1})
    job = client.post(f"/api/media/{media_id}/process", json={"preset_id": preset["id"]}).json()
    assert client.delete(f"/api/presets/{preset['id']}").status_code == 204
    stored = ctx.jobs.get(job["id"])
    assert stored["params"] == PARAMS and stored["preset_name"] == "Temporário" and stored["preset_id"] is None


def test_builtin_cannot_be_renamed(harness):
    client = harness.client
    for builtin in client.get("/api/presets").json():
        renamed = client.put(f"/api/presets/{builtin['id']}", json={"name": "Outro nome"})
        assert renamed.status_code == 409 and renamed.json()["code"] == "preset_builtin_name"
        same = client.put(f"/api/presets/{builtin['id']}", json={"name": builtin["name"], "params": PARAMS})
        assert same.status_code == 200 and same.json()["name"] == builtin["name"]
        assert same.json()["params"] == PARAMS
    assert [p["name"] for p in client.get("/api/presets").json()] == ["Padrão", "Anti-duplicidade"]
    custom = client.post("/api/presets", json={"name": "Livre", "params": PARAMS}).json()
    assert client.put(f"/api/presets/{custom['id']}", json={"name": "Renomeado"}).status_code == 200


# ---- ajustes opcionais (zoom, cor, corte final, tom, granulação) ---------------------------------------------------

NEUTRAL = {"zoom": 0, "brightness": 0, "contrast": 1, "saturation": 1, "trim_end": 0, "pitch_shift": 0, "grain": 0}
BUILTIN_SIX = {"metadata_cleanup": True, "trim_start": 0.5, "mirror": True, "speed": 1.05, "auto_adjust": True,
               "reencode": True}
ANTI_DUPLICATE = {**BUILTIN_SIX, "zoom": 3.0, "brightness": 0.01, "contrast": 1.02, "saturation": 1.03,
                  "trim_end": 0.3}


def _stored(harness, preset_id):
    return harness.ctx.db.scalar("SELECT params FROM presets WHERE id = ?", (preset_id,))


def test_builtin_presets_differ_after_migrations(harness):
    padrao, anti = harness.client.get("/api/presets").json()
    assert padrao["params"] == BUILTIN_SIX and anti["params"] == ANTI_DUPLICATE


def test_optional_params_are_stored_only_when_active(harness):
    client = harness.client
    created = client.post("/api/presets", json={"name": "Neutro", "params": {**PARAMS, **NEUTRAL}})
    assert created.status_code == 201, created.text
    preset_id = created.json()["id"]
    assert created.json()["params"] == PARAMS  # neutros não são gravados nem devolvidos
    assert _stored(harness, preset_id) == canonical_json(PARAMS)

    active = {**PARAMS, "zoom": 2.5, "grain": 1.0, "pitch_shift": -1.5}
    updated = client.put(f"/api/presets/{preset_id}", json={"params": {**NEUTRAL, **active}})
    assert updated.status_code == 200 and updated.json()["params"] == active
    assert _stored(harness, preset_id) == canonical_json(active)
    copy = client.post(f"/api/presets/{preset_id}/duplicate", json={}).json()
    assert copy["params"] == active and _stored(harness, copy["id"]) == canonical_json(active)

    back = client.put(f"/api/presets/{preset_id}", json={"params": {**PARAMS, **NEUTRAL}})
    assert back.json()["params"] == PARAMS and _stored(harness, preset_id) == canonical_json(PARAMS)


def test_resaving_default_preset_keeps_the_six_keys_and_the_rendition_dedupe(harness):
    """Salvar o Padrão de novo pela interface (com os ajustes novos neutros) não muda os params: a mídia já
    processada com ele continua casando com a rendition existente em vez de ser reprocessada."""
    client, ctx = harness.client, harness.ctx
    padrao, anti = client.get("/api/presets").json()
    media_id = ctx.media.insert({"media_type": "image", "original_path": "originals/x.jpg",
                                 "original_sha256": "d" * 64, "file_size": 1})
    ctx.media.insert_rendition({"media_id": media_id, "preset_id": padrao["id"], "preset_name": "Padrão",
                                "params": BUILTIN_SIX, "processed_path": "processed/x.jpg",
                                "processed_sha256": "e" * 64, "file_size": 1})
    saved = client.put(f"/api/presets/{padrao['id']}", json={"params": {**BUILTIN_SIX, **NEUTRAL}})
    assert saved.status_code == 200 and saved.json()["params"] == BUILTIN_SIX
    assert _stored(harness, padrao["id"]) == canonical_json(BUILTIN_SIX)
    job = client.post(f"/api/media/{media_id}/process", json={"preset_id": padrao["id"]}).json()
    stored = ctx.jobs.get(job["id"])
    assert stored["params"] == BUILTIN_SIX
    assert ctx.media.find_rendition(media_id, padrao["id"], stored["params"]) is not None
    # o Anti-duplicidade tem params próprios, que vão inteiros para o snapshot do job
    job = client.post(f"/api/media/{media_id}/process", json={"preset_id": anti["id"]}).json()
    assert ctx.jobs.get(job["id"])["params"] == ANTI_DUPLICATE
    assert ctx.media.find_rendition(media_id, anti["id"], ANTI_DUPLICATE) is None


def test_optional_params_validation(harness):
    client = harness.client
    bad = [{"zoom": -0.1}, {"zoom": 10.1}, {"brightness": 0.11}, {"brightness": -0.11}, {"contrast": 0.89},
           {"contrast": 1.11}, {"saturation": 0.79}, {"saturation": 1.21}, {"trim_end": -1}, {"trim_end": 30.5},
           {"pitch_shift": 3.1}, {"pitch_shift": -3.1}, {"grain": -0.5}, {"grain": 4.1}, {"zoom": "3"},
           {"grain": True}, {"zoom": None}]
    for extra in bad:
        response = client.post("/api/presets", json={"name": "ruim", "params": {**PARAMS, **extra}})
        assert response.status_code == 422 and response.json()["code"] == "validation_error", extra
    limits = {"zoom": 10.0, "brightness": -0.1, "contrast": 1.1, "saturation": 0.8, "trim_end": 30.0,
              "pitch_shift": -3.0, "grain": 4.0}
    ok = client.post("/api/presets", json={"name": "limites", "params": {**PARAMS, **limits}})
    assert ok.status_code == 201 and ok.json()["params"] == {**PARAMS, **limits}, ok.text
    # o que a API aceita e devolve, o processor aceita
    ProcessingOptions.from_params(ok.json()["params"])
    ProcessingOptions.from_params(client.get("/api/presets").json()[1]["params"])
