"""QA: Padrão x Anti-duplicidade com FFmpeg real, pelo app inteiro (API, fila, worker, banco e disco)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from media_processor import MediaProcessor, ProcessingOptions, sha256_file

from media_fakes import FakeInstagramDownloader, remote

requires_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="FFmpeg/ffprobe não estão no PATH"
)

SIX = {"metadata_cleanup": True, "trim_start": 0.5, "mirror": True, "speed": 1.05, "auto_adjust": True,
       "reencode": True}
ANTI = {**SIX, "zoom": 3.0, "brightness": 0.01, "contrast": 1.02, "saturation": 1.03, "trim_end": 0.3}


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, timeout=120)


def _clip(path: Path, secs: float, size: str = "321x241") -> None:
    _ff("-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25:duration={secs}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={secs}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-metadata", "title=origem", str(path))


def _samples(directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    video, short, image = directory / "v.mp4", directory / "short.mp4", directory / "i.jpg"
    _clip(video, 3)
    _clip(short, 1.2)
    _ff("-f", "lavfi", "-i", "testsrc2=size=321x241", "-frames:v", "1", "-update", "1", str(image))
    return {"V": video, "S": short, "I": image}


@pytest.fixture
def app(harness_factory, tmp_path):
    samples = _samples(tmp_path / "samples")
    downloader = FakeInstagramDownloader(
        items=[remote("V", media_type="video"), remote("S", media_type="video"), remote("I", media_type="image")],
        source_files={"V:0": samples["V"], "S:0": samples["S"], "I:0": samples["I"]},
    )
    harness = harness_factory(downloader=downloader, processor=MediaProcessor())
    batch = harness.import_profile(process=False)
    harness.run_all()
    jobs = harness.batch(batch["id"])["jobs"]
    media = {j["source_post_id"]: j["media_id"] for j in jobs}
    assert len(media) == 3
    return harness, media


def _process(harness, media_id, preset_id) -> dict:
    response = harness.client.post(f"/api/media/{media_id}/process", json={"preset_id": preset_id})
    assert response.status_code == 202, response.text
    harness.run_jobs()
    return harness.client.get(f"/api/jobs/{response.json()['id']}").json()


def _presets(harness) -> dict[str, dict]:
    return {p["name"]: p for p in harness.client.get("/api/presets").json()}


def _file(harness, rendition):
    return harness.ctx.storage.resolve(rendition["processed_path"])


def _probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                          str(path)], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


@requires_ffmpeg
def test_default_and_anti_duplicate_produce_distinct_renditions(app):
    harness, media = app
    presets = _presets(harness)
    original_paths = {k: harness.ctx.storage.resolve(harness.ctx.media.get(v)["original_path"])
                      for k, v in media.items()}
    originals_before = {k: sha256_file(p) for k, p in original_paths.items()}
    for key in ("V", "I"):
        padrao = _process(harness, media[key], presets["Padrão"]["id"])
        anti = _process(harness, media[key], presets["Anti-duplicidade"]["id"])
        assert padrao["status"] == anti["status"] == "completed", (padrao, anti)
        assert padrao["stage"] == anti["stage"] == "done"
        rp = harness.ctx.media.get_rendition(padrao["rendition_id"])
        ra = harness.ctx.media.get_rendition(anti["rendition_id"])
        assert rp["id"] != ra["id"] and rp["processed_path"] != ra["processed_path"]
        assert rp["processed_sha256"] != ra["processed_sha256"]
        assert set(rp["params"]) == set(SIX) and rp["params"] == SIX  # Padrão: exatamente as 6 chaves
        assert ra["params"] == ANTI
        assert rp["preset_name"] == "Padrão" and ra["preset_name"] == "Anti-duplicidade"
        fp, fa = _file(harness, rp), _file(harness, ra)
        assert sha256_file(fp) == rp["processed_sha256"] and sha256_file(fa) == ra["processed_sha256"]
        assert (rp["width"], rp["height"]) == (ra["width"], ra["height"])
        if key == "V":
            pa, pp = _probe(fa), _probe(fp)
            assert set(pa["format"].get("tags", {})) <= {"major_brand", "minor_version", "compatible_brands"}
            dv = float(next(s for s in pa["streams"] if s["codec_type"] == "video")["duration"])
            da = float(next(s for s in pa["streams"] if s["codec_type"] == "audio")["duration"])
            assert abs(dv - da) < 0.1
            assert float(pa["format"]["duration"]) == pytest.approx(
                float(pp["format"]["duration"]) - 0.3 / 1.05, abs=0.12)
        again = _process(harness, media[key], presets["Anti-duplicidade"]["id"])
        assert again["stage"] == "skipped" and again["rendition_id"] == ra["id"]
    for key, sha in originals_before.items():
        assert sha256_file(original_paths[key]) == sha == harness.ctx.media.get(media[key])["original_sha256"]


@requires_ffmpeg
def test_old_six_key_anti_duplicate_rendition_survives_reprocessing(app):
    harness, media = app
    ctx = harness.ctx
    anti_id = _presets(harness)["Anti-duplicidade"]["id"]
    new_params = ctx.db.scalar("SELECT params FROM presets WHERE id = ?", (anti_id,))
    ctx.db.execute("UPDATE presets SET params = ? WHERE id = ?",
                   (json.dumps(SIX, sort_keys=True, separators=(",", ":")), anti_id))
    old_job = _process(harness, media["V"], anti_id)
    old = ctx.media.get_rendition(old_job["rendition_id"])
    assert old["params"] == SIX
    old_sha, old_file = old["processed_sha256"], _file(harness, old)

    ctx.db.execute("UPDATE presets SET params = ? WHERE id = ?", (new_params, anti_id))
    new_job = _process(harness, media["V"], anti_id)
    assert new_job["status"] == "completed" and new_job["stage"] == "done"
    new = ctx.media.get_rendition(new_job["rendition_id"])
    assert new["id"] != old["id"] and new["params"] == ANTI and new["processed_sha256"] != old_sha

    still = ctx.media.get_rendition(old["id"])
    assert still is not None and still["params"] == SIX and still["processed_sha256"] == old_sha
    assert old_file.is_file() and sha256_file(old_file) == old_sha
    assert {r["id"] for r in ctx.media.renditions_for([media["V"]])[media["V"]]} == {old["id"], new["id"]}


@requires_ffmpeg
def test_preexisting_custom_six_key_preset_is_readable_editable_and_processes_like_before(app):
    harness, media = app
    ctx, client = harness.ctx, harness.client
    custom = {"metadata_cleanup": True, "trim_start": 0.25, "mirror": False, "speed": 1.1, "auto_adjust": True,
              "reencode": True}
    ctx.db.execute(
        "INSERT INTO presets (name, is_builtin, params, created_at, updated_at) VALUES (?, 0, ?, 'x', 'x')",
        ("Legado", json.dumps(custom)))  # formato do seed: com espaços, só 6 chaves
    got = _presets(harness)["Legado"]
    assert got["params"] == custom
    job = _process(harness, media["V"], got["id"])
    rend = ctx.media.get_rendition(job["rendition_id"])
    assert rend["params"] == custom
    original = ctx.storage.resolve(ctx.media.get(media["V"])["original_path"])
    reference = MediaProcessor().process(original, harness.root / "ref.mp4", ProcessingOptions.from_params(custom))
    assert reference.sha256 == rend["processed_sha256"]  # mesmos bytes que o processor puro com os 6 params
    edited = client.put(f"/api/presets/{got['id']}", json={"params": {**custom, "zoom": 2.0}})
    assert edited.status_code == 200 and edited.json()["params"] == {**custom, "zoom": 2.0}
    back = client.put(f"/api/presets/{got['id']}", json={"params": custom})
    assert back.status_code == 200 and back.json()["params"] == custom


@requires_ffmpeg
def test_short_video_with_anti_duplicate_ignores_trim_end(app):
    harness, media = app
    anti_id = _presets(harness)["Anti-duplicidade"]["id"]
    job = _process(harness, media["S"], anti_id)  # 1,2 s: 1,2 - 0,5 - 0,3 < 1 -> corte final ignorado
    assert job["status"] == "completed", job
    rend = harness.ctx.media.get_rendition(job["rendition_id"])
    assert rend["params"] == ANTI  # o snapshot mantém o preset; só a execução ignora o corte
    dur = float(_probe(_file(harness, rend))["format"]["duration"])
    assert dur == pytest.approx((1.2 - 0.5) / 1.05, abs=0.15)


@requires_ffmpeg
def test_too_short_video_fails_with_trim_start_error(harness_factory, tmp_path):
    short = tmp_path / "s.mp4"
    _clip(short, 0.55, "320x240")
    downloader = FakeInstagramDownloader(items=[remote("T", media_type="video")], source_files={"T:0": short})
    harness = harness_factory(downloader=downloader, processor=MediaProcessor())
    batch = harness.import_profile(process=False)
    harness.run_all()
    media_id = harness.batch(batch["id"])["jobs"][0]["media_id"]
    for name in ("Padrão", "Anti-duplicidade"):
        job = _process(harness, media_id, _presets(harness)[name]["id"])
        assert job["status"] == "failed", job
        assert "corte inicial" in json.dumps(job, ensure_ascii=False).lower(), job
    assert harness.ctx.media.renditions_for([media_id])[media_id] == []


def test_preset_api_rejects_invalid_unknown_and_wrong_types(harness):
    client = harness.client
    cases = [{"zoom": 11}, {"zoom": -0.01}, {"brightness": 0.1001}, {"contrast": 0.8999}, {"saturation": 1.2001},
             {"trim_end": 30.01}, {"pitch_shift": -3.01}, {"grain": 4.01}, {"zoom": "3"}, {"zoom": [3]},
             {"zoom": {"a": 1}}, {"grain": False}, {"trim_end": None}, {"unknown_key": 1}, {"Zoom": 3}]
    for extra in cases:
        response = client.post("/api/presets", json={"name": "x", "params": {**SIX, **extra}})
        assert response.status_code == 422, (extra, response.status_code, response.text)
    for raw in ('{"zoom": NaN}', '{"zoom": Infinity}', '{"zoom": 1e999}'):
        body = '{"name": "x", "params": {"metadata_cleanup": true, "trim_start": 0.5, "mirror": true, ' \
               '"speed": 1.05, "auto_adjust": true, "reencode": true, ' + raw[1:-1] + "}}"
        response = client.post("/api/presets", content=body, headers={"content-type": "application/json"})
        assert response.status_code == 422, (raw, response.status_code, response.text)
    created = client.post("/api/presets", json={"name": "ok", "params": SIX}).json()
    for extra in cases[:6]:
        assert client.put(f"/api/presets/{created['id']}", json={"params": {**SIX, **extra}}).status_code == 422
    assert _presets(harness)["ok"]["params"] == SIX
    edge = {"zoom": 0, "brightness": 0.1, "contrast": 0.9, "saturation": 1.2, "trim_end": 30, "pitch_shift": 3,
            "grain": 0}
    ok = client.put(f"/api/presets/{created['id']}", json={"params": {**SIX, **edge}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["params"] == {**SIX, "brightness": 0.1, "contrast": 0.9, "saturation": 1.2, "trim_end": 30,
                                   "pitch_shift": 3}
