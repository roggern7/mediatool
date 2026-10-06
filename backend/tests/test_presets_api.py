from __future__ import annotations

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
