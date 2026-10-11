"""Regressões extras do QA para a importação por link com fluxos separados (DASH): lote misto, retry manual depois de
falha, exportação do original juntado, preset sobre original VP9 e superfície da API. Só valores FAKE; nenhuma rede."""

from __future__ import annotations

import json

import pytest
from media_processor import MediaProcessor

from app.jobs import pipeline
from audit_links_helpers import (  # noqa: F401 - fixtures
    audit_env,
    base_media,
    check_invariants,
    make_app,
    storage_leftovers,
)
from export_helpers import export_to, prepare, sha256_of
from import_links_fakes import create_links, link
from post_document_fixtures import media_node
from synthetic_streams import requires_ffmpeg, requires_vp9
from test_import_links_dash import (  # noqa: F401 - fixtures
    A,
    B,
    C,
    dash_app,
    dash_media,
    kinds,
    only_job,
    only_media,
    refs,
)

pytestmark = requires_ffmpeg


def test_mixed_batch_photo_reel_with_streams_and_carousel_all_complete(harness_factory, base_media, audit_env,
                                                                      dash_media):
    """Foto + Reel com fluxos separados + carrossel (foto/vídeo/foto/vídeo): só o Reel usa a junção."""
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media, codes=(A,))
    sim.http.roots[B] = media_node("photo", B)
    sim.http.roots[C] = media_node("carousel", C)
    batch = create_links(h, [link(B, "p"), link(A, "reel"), link(C, "p")])
    h.run_all()
    h.run_jobs()
    body = h.batch(batch["id"])
    jobs = h.ctx.jobs.for_batch(batch["id"])
    assert len(jobs) == 6, [j["source_post_id"] for j in jobs]
    assert {j["status"] for j in jobs} == {"completed"}, [(j["source_post_id"], j["status"], j["error_code"]) for j in jobs]
    assert body["status"] == "completed" and body["progress"]["failed"] == 0
    assert sorted(kinds(sim)) == sorted(["v", "a"] + ["p"] * 5), kinds(sim)
    reel = h.ctx.db.fetchall("SELECT * FROM media WHERE source_post_id = ?", (A,))
    assert len(reel) == 1 and reel[0]["video_codec"] == "h264" and reel[0]["has_audio"] == 1
    assert refs(h) == [] and storage_leftovers(h) == []
    check_invariants(h)


@pytest.mark.parametrize("route", ["job", "batch"])
def test_manual_retry_after_streams_refused_downloads_streams_again(harness_factory, base_media, audit_env,
                                                                    dash_media, route, monkeypatch):
    """Job falhado depois de baixar os fluxos (recusados de novo após a renovação): o retry manual recupera as URLs
    pela publicação e conclui com a junção, sem restos."""
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media)
    sim.cdn.expired = {f"/{A}/"}   # tudo recusado (fluxos e MP4 direto): só assim o job falha
    batch = create_links(h, [link(A, "reel")])
    h.run_all()
    h.run_all()
    job = only_job(h, batch)
    assert (job["status"], job["error_code"]) == ("failed", "content_unavailable")
    assert all(r["dash_streams"] is None and r["media_url"] is None for r in refs(h))

    sim.cdn.expired = set()
    monkeypatch.setattr(pipeline, "REFRESH_MIN_INTERVAL", 0.0)  # passada a janela de 10 min entre renovações
    if route == "job":
        assert h.client.post(f"/api/jobs/{job['id']}/retry").status_code == 200
    else:
        assert h.client.post(f"/api/batches/{batch['id']}/retry-failed").status_code == 200
    h.run_all()
    h.run_all()
    job = only_job(h, batch)
    assert (job["status"], job["error_code"]) == ("completed", None), (job["status"], job["error_code"],
                                                                      job["error_message"], kinds(sim))
    assert kinds(sim)[-2:] == ["v", "a"], kinds(sim)
    assert only_media(h)["video_codec"] == "h264"
    assert refs(h) == [] and storage_leftovers(h) == []
    check_invariants(h)


def test_manual_retry_inside_the_refresh_window_asks_nothing_and_fails_with_a_safe_message(
        harness_factory, base_media, audit_env, dash_media):
    """Dentro dos 10 min da última renovação a publicação NÃO é pedida de novo (poupa o Instagram): o retry manual
    falha de novo, sem requisição nenhuma e sem vazar URL."""
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media)
    sim.cdn.expired = {f"/{A}/"}   # tudo recusado (fluxos e MP4 direto): só assim o job falha
    batch = create_links(h, [link(A, "reel")])
    h.run_all()
    h.run_all()
    job = only_job(h, batch)
    calls, hits = list(sim.calls), len(sim.cdn.hits)
    sim.cdn.expired = set()
    assert h.client.post(f"/api/jobs/{job['id']}/retry").status_code == 200
    h.run_all()
    h.run_all()
    job = only_job(h, batch)
    assert (job["status"], job["error_code"]) == ("failed", "content_unavailable")
    assert sim.calls == calls and len(sim.cdn.hits) == hits
    assert "fbcdn" not in (job["error_message"] or "") and "/dash/" not in (job["error_message"] or "")
    assert refs(h) == [] or all(r["dash_streams"] is None for r in refs(h))
    assert storage_leftovers(h) == []


def test_export_of_a_muxed_original_is_byte_identical_mp4(harness_factory, base_media, audit_env, dash_media,
                                                         tmp_path):
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media)
    batch = create_links(h, [link(A, "reel")])
    h.run_all()
    media = only_media(h)
    dest = prepare(h, tmp_path)
    export = export_to(h, {"kind": "batch", "id": batch["id"]}, dest)
    assert (export["status"], export["total"], export["copied"], export["failed"]) == ("completed", 1, 1, 0)
    [exported] = [p for p in dest.iterdir() if p.is_file()]
    assert exported.suffix == ".mp4"
    assert sha256_of(exported) == media["original_sha256"]
    probe = MediaProcessor().probe(exported)
    assert probe.has_video and probe.has_audio
    assert "dash-muxed" not in exported.name, exported.name


@requires_vp9
def test_preset_processing_of_a_vp9_muxed_original(harness_factory, base_media, audit_env, dash_media):
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media, codec="vp9")
    preset = h.client.get("/api/presets").json()[0]
    batch = create_links(h, [link(A, "reel")], process=True, preset_id=preset["id"])
    h.run_all()
    h.run_jobs()
    job = only_job(h, batch)
    assert (job["status"], job["error_code"]) == ("completed", None), job["error_message"]
    media = only_media(h)
    assert media["video_codec"] == "vp9" and media["thumbnail_path"]
    rendition = h.ctx.db.fetchall("SELECT * FROM renditions")
    assert len(rendition) == 1
    out = MediaProcessor().probe(h.ctx.storage.resolve(rendition[0]["processed_path"]))
    assert out.has_video and out.has_audio
    assert storage_leftovers(h) == []
    check_invariants(h)


def test_dash_streams_never_appear_in_any_api_response_while_the_reference_exists(harness_factory, base_media,
                                                                                  audit_env, dash_media):
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media)
    batch = create_links(h, [link(A, "reel")])
    h.run_imports()
    [ref] = refs(h)
    assert json.loads(ref["dash_streams"])  # a referência existe no banco
    job = only_job(h, batch)
    paths = ["/api/jobs", f"/api/jobs/{job['id']}", "/api/batches", f"/api/batches/{batch['id']}",
             f"/api/batches/{batch['id']}/links", "/api/media", "/openapi.json"]
    for path in paths:
        response = h.client.get(path, params={"include_jobs": "true"})
        text = response.text
        for token in ("dash_streams", "dash_video_url", "dash_audio_url", "/dash/", "fbcdn", "FAKE_OH"):
            if path == "/openapi.json" and token == "/dash/":
                continue
            assert token not in text, (path, token)
