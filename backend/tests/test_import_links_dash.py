"""Importação por link na maior qualidade: fluxos separados (DASH) + junção sem recodificar, ponta a ponta.

Pilha REAL (provider web -> documento público -> normalize_post -> cdn_download -> pipeline -> MediaProcessor) sobre
transportes simulados (``audit_links_helpers``) e um par fMP4 SINTÉTICO do ffmpeg local (``synthetic_streams``).
NENHUMA rede: nada aqui prova o comportamento do CDN real (o download de ``BaseURL`` e a junção nunca rodaram contra
ele). Só valores FAKE."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

import httpx
import pytest
from media_processor import MediaProcessor, sha256_file

from app.integrations.instagram.composite import CompositeInstagramDownloader
from app.integrations.instagram.downloader import InstagramError
from app.jobs import pipeline
from app.jobs.dash_ref import DASH_KEYS, dash_fields_from_json, dash_json
from app.services import cdn_downloader
from app.services.cdn_downloader import DownloadError
from app.services.import_service import cancel_job_ids
from audit_links_helpers import (  # noqa: F401 - fixtures
    audit_env,
    base_media,
    check_invariants,
    make_app,
    storage_leftovers,
)
from dash_fixtures import dash_url, media_node_with_dash, simple_mpd
from import_links_fakes import create_links, link, links_app, reel
from media_fakes import FakeInstagramDownloader, FakeProcessor
from post_document_fixtures import media_node, video_url
from synthetic_streams import SIZE, has_encoder, make_audio, make_video, requires_ffmpeg, requires_vp9
from test_web_provider import FakeCdn, build, video_node

pytestmark = requires_ffmpeg

A, B, C = "DASHAaaaa11", "DASHBbbbb22", "DASHCcccc33"
CODECS = {"vp9": "vp09.00.40.08.00.01.01.01.00", "h264": "avc1.64001f"}
SENSITIVE = ("fbcdn", "cdninstagram", "https://", "oh=", "FAKE_OH", "FRESH", "/dash/", "BaseURL", "<MPD", A, B, C)


@pytest.fixture(scope="session")
def dash_media(tmp_path_factory) -> dict[str, bytes]:
    """fMP4 só-vídeo (H.264 e, se o ffmpeg tiver, VP9) e fMP4 só-áudio AAC de 2 s; um áudio de 10 s para a junção
    reprovada."""
    directory = tmp_path_factory.mktemp("dash_samples")
    media = {"h264": make_video(directory / "v_h264.mp4", codec="libx264"),
             "audio": make_audio(directory / "a.mp4"),
             "audio_long": make_audio(directory / "a_long.mp4", seconds=10)}
    if has_encoder("libvpx-vp9"):
        media["vp9"] = make_video(directory / "v_vp9.mp4", codec="libvpx-vp9")
    return media


def node_for(code: str, dash_media, *, codec: str = "h264", declare: tuple[bool, bool] = (True, True),
             duration: str | None = "PT2.000000S", video_bytes: int | None = None) -> dict:
    """Reel com manifesto do par sintético (dimensões reais do par, para a regra de seleção e a verificação)."""
    sizes = (len(dash_media[codec]), len(dash_media["audio"]))
    manifest = simple_mpd(code, width=SIZE[0], height=SIZE[1], codecs=CODECS[codec], duration=duration,
                          video_bytes=video_bytes if video_bytes is not None else (sizes[0] if declare[0] else None),
                          audio_bytes=sizes[1] if declare[1] else None)
    return media_node_with_dash(code, manifest, original=SIZE)


def dash_app(harness_factory, base_media, audit_env, dash_media, *, codec: str = "h264", codes=(A,), processor=None,
             audio: str = "audio", **node_kwargs):
    h, sim = make_app(harness_factory, base_media, audit_env,
                      processor=processor if processor is not None else MediaProcessor())
    for code in codes:
        sim.http.roots[code] = node_for(code, dash_media, codec=codec, **node_kwargs)
    sim.cdn.rules = [("/v.mp4", dash_media[codec]), ("/a.mp4", dash_media[audio])]
    return h, sim


def kinds(sim) -> list[str]:
    """Cada acesso ao CDN simulado: "v"/"a" = fluxo separado de vídeo/áudio; "p" = MP4 direto."""
    return ["v" if path.endswith("/v.mp4") else "a" if path.endswith("/a.mp4") else "p"
            for path in sim.cdn.hit_paths()]


def only_job(h, batch) -> dict:
    [job] = h.ctx.jobs.for_batch(batch["id"])
    return job


def only_media(h) -> dict:
    [media] = h.ctx.db.fetchall("SELECT * FROM media")
    return dict(media)


def refs(h) -> list[dict]:
    return [dict(row) for row in h.ctx.db.fetchall("SELECT * FROM job_remote_refs")]


def progressive_sha(sim, code: str) -> str:
    import hashlib

    return hashlib.sha256(sim.cdn.bodies[video_url(code, "preel1", 101).split("?")[0]]).hexdigest()


def assert_progressive_original(h, sim, code: str = A) -> dict:
    media = only_media(h)
    assert media["original_sha256"] == progressive_sha(sim, code), "o original é o MP4 direto, byte a byte"
    assert media["video_codec"] is None and media["width"] is None, "download puro do MP4 direto: sem ffprobe"
    return media


def cancel_batch(h, batch_id: int) -> None:
    assert h.client.post(f"/api/batches/{batch_id}/cancel").status_code == 200


# --------------------------------------------------------------------------- caminho feliz


def _happy_path(harness_factory, base_media, audit_env, dash_media, tmp_path, codec: str) -> None:
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media, codec=codec)
    batch = create_links(h, [link(A, "reel")])
    h.run_imports()
    [ref] = refs(h)
    stored = json.loads(ref["dash_streams"])
    assert sorted(stored) == sorted(DASH_KEYS) and stored["dash_video_url"] == dash_url(A, "v")
    assert ref["media_url"] == video_url(A, "preel1", 101), "media_url continua sendo o MP4 direto"
    assert only_job(h, batch)["params"] is None, "download puro (padrão)"
    h.run_jobs()

    assert kinds(sim) == ["v", "a"], "exatamente 2 acessos ao CDN; nenhum ao MP4 direto"
    done = h.batch(batch["id"])
    assert (done["status"], done["progress"]["downloaded"], done["progress"]["failed"]) == ("completed", 1, 0)
    job = only_job(h, batch)
    assert (job["status"], job["stage"], job["is_duplicate"], job["error_code"]) == ("completed", "done", False, None)
    assert refs(h) == [], "job concluído: a referência remota (URLs e dash_streams) some"

    media = only_media(h)
    assert (media["width"], media["height"], media["video_codec"], media["has_audio"]) == (*SIZE, codec, 1)
    assert media["fps"] == pytest.approx(30.0) and media["duration"] == pytest.approx(2.0, abs=0.2)
    assert (media["media_type"], media["mime_type"]) == ("video", "video/mp4")
    original = h.ctx.storage.resolve(media["original_path"])
    assert original.suffix == ".mp4" and original.stat().st_size == media["file_size"]
    assert re.fullmatch(r"cdn-[0-9a-f]{32}\.mp4", media["original_filename"]), "mesmo padrão dos downloads diretos"
    probe = MediaProcessor().probe(original)
    assert (probe.video_codec, probe.audio_codec, probe.has_video, probe.has_audio) == (codec, "aac", True, True)
    assert (probe.width, probe.height) == SIZE

    # Determinismo: a junção do mesmo par, feita de novo fora do app, dá o mesmo SHA-256 do original registrado.
    video, audio = tmp_path / "v.mp4", tmp_path / "a.mp4"
    video.write_bytes(dash_media[codec])
    audio.write_bytes(dash_media["audio"])
    MediaProcessor().mux_streams(video, audio, tmp_path / "again.mp4")
    assert sha256_file(tmp_path / "again.mp4") == media["original_sha256"]

    listed = h.client.get("/api/media").json()["items"]
    assert [item["video_codec"] for item in listed] == [codec]
    assert storage_leftovers(h) == []
    check_invariants(h)


@requires_vp9
def test_vp9_and_aac_streams_become_one_mp4_original(harness_factory, base_media, audit_env, dash_media, tmp_path):
    _happy_path(harness_factory, base_media, audit_env, dash_media, tmp_path, "vp9")


def test_h264_and_aac_streams_become_one_mp4_original(harness_factory, base_media, audit_env, dash_media, tmp_path):
    """H.264 no manifesto NÃO foi observado no real; coberto por inferência (e quando o ffmpeg não tem libvpx-vp9)."""
    _happy_path(harness_factory, base_media, audit_env, dash_media, tmp_path, "h264")


def test_stage_muxing_is_reported_and_progress_never_goes_back(harness_factory, base_media, audit_env, dash_media,
                                                               monkeypatch):
    monkeypatch.setattr(pipeline, "PROGRESS_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(pipeline, "PROGRESS_MIN_DELTA", 0.0)
    monkeypatch.setattr(cdn_downloader, "CHUNK_SIZE", 2048)
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media)
    batch = create_links(h, [link(A, "reel")])
    h.run_imports()
    writes: list[dict] = []
    real_update = h.ctx.jobs.update

    def spy(job_id, **fields):
        writes.append(fields)
        return real_update(job_id, **fields)

    monkeypatch.setattr(h.ctx.jobs, "update", spy)
    h.run_jobs()
    stages = [w["stage"] for w in writes if "stage" in w]
    assert stages == ["downloading", "downloading", "muxing", "hashing", "probing", "finalizing", "done"]
    start = next(i for i, w in enumerate(writes) if w.get("stage") == "downloading" and w.get("progress") == 0.0)
    end = next(i for i, w in enumerate(writes) if w.get("stage") == "muxing")
    progress = [w["progress"] for w in writes[start + 1:end]]
    assert len(progress) > 5 and progress == sorted(progress), progress
    video_share = len(dash_media["h264"]) / (len(dash_media["h264"]) + len(dash_media["audio"]))
    assert progress[-1] == pytest.approx(1.0) and any(p == pytest.approx(video_share) for p in progress), \
        "progresso somado: o fim do vídeo é a fração dele no total declarado"
    assert h.client.get(f"/api/jobs/{only_job(h, batch)['id']}").json()["stage"] == "done"


def test_job_api_accepts_the_muxing_stage(harness_factory, base_media, audit_env, dash_media):
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media)
    batch = create_links(h, [link(A, "reel")])
    h.run_imports()
    job = only_job(h, batch)
    h.ctx.jobs.update(job["id"], status="downloading", stage="muxing")
    body = h.client.get(f"/api/jobs/{job['id']}")
    assert body.status_code == 200 and body.json()["stage"] == "muxing"
    text = body.text + h.client.get(f"/api/batches/{batch['id']}", params={"include_jobs": "true"}).text
    assert "/dash/" not in text and "dash_streams" not in text and "dash_video_url" not in text


def test_streams_without_declared_sizes_still_work(harness_factory, base_media, audit_env, dash_media):
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media, declare=(False, False), duration=None)
    batch = create_links(h, [link(A, "reel")])
    h.run_all()
    assert kinds(sim) == ["v", "a"] and only_job(h, batch)["status"] == "completed"
    assert only_media(h)["video_codec"] == "h264"


def test_processing_batch_makes_thumbnail_and_rendition_from_the_muxed_original(harness_factory, base_media,
                                                                                audit_env, dash_media):
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media)
    preset = h.client.get("/api/presets").json()[0]
    batch = create_links(h, [link(A, "reel")], process=True, preset_id=preset["id"])
    h.run_all()
    h.run_jobs()
    job = only_job(h, batch)
    assert (job["status"], job["error_code"]) == ("completed", None) and job["rendition_id"]
    media = only_media(h)
    assert media["video_codec"] == "h264" and media["thumbnail_path"]
    assert kinds(sim) == ["v", "a"] and storage_leftovers(h) == []
    check_invariants(h)


# --------------------------------------------------------------------------- fallback para o MP4 direto


@pytest.mark.parametrize("make_processor", [
    lambda tmp: FakeProcessor(ffmpeg_found=False),
    lambda tmp: MediaProcessor(ffmpeg_path=str(tmp / "nao-existe" / "ffmpeg.exe")),
    lambda tmp: MediaProcessor(ffprobe_path=str(tmp / "nao-existe" / "ffprobe.exe")),
], ids=["fake_sem_ffmpeg", "ffmpeg_ausente", "ffprobe_ausente"])
def test_without_ffmpeg_or_ffprobe_the_direct_mp4_is_downloaded(harness_factory, base_media, audit_env, dash_media,
                                                                tmp_path, caplog, make_processor):
    caplog.set_level(logging.INFO)
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media, processor=make_processor(tmp_path))
    batch = create_links(h, [link(A, "reel")])
    h.run_all()
    assert kinds(sim) == ["p"], "1 acesso: o MP4 direto; nenhum fluxo separado é pedido"
    assert only_job(h, batch)["status"] == "completed"
    assert_progressive_original(h, sim)
    assert "dash_fallback=tools_missing" in caplog.text
    assert storage_leftovers(h) == []
    check_invariants(h)


def test_declared_sizes_above_the_limit_skip_the_streams_without_any_request(harness_factory, base_media, audit_env,
                                                                             dash_media, caplog):
    caplog.set_level(logging.INFO)
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media, video_bytes=3 * 1024 ** 3)
    assert h.settings.max_download_bytes < 3 * 1024 ** 3
    batch = create_links(h, [link(A, "reel")])
    h.run_all()
    assert kinds(sim) == ["p"] and only_job(h, batch)["status"] == "completed"
    assert_progressive_original(h, sim)
    assert "dash_fallback=too_large" in caplog.text


def test_audio_larger_than_the_download_limit_falls_back_with_three_requests(harness_factory, base_media, audit_env,
                                                                             dash_media, caplog):
    caplog.set_level(logging.INFO)
    limit = 1024 * 1024
    h, sim = make_app(harness_factory, base_media, audit_env, processor=MediaProcessor(), max_download_bytes=limit)
    sim.http.roots[A] = node_for(A, dash_media, declare=(True, False))
    padding = (limit + 8).to_bytes(4, "big") + b"free" + b"\x00" * limit
    sim.cdn.rules = [("/v.mp4", dash_media["h264"]), ("/a.mp4", dash_media["audio"] + padding)]
    batch = create_links(h, [link(A, "reel")])
    h.run_all()
    assert kinds(sim) == ["v", "a", "p"], "vídeo, áudio (grande demais) e o MP4 direto: 3 acessos, sem laço"
    assert only_job(h, batch)["status"] == "completed"
    assert_progressive_original(h, sim)
    assert "dash_fallback=download_failed" in caplog.text
    assert storage_leftovers(h) == []
    check_invariants(h)


@pytest.mark.parametrize("which", ["video", "audio"])
def test_size_different_from_the_manifest_falls_back(harness_factory, base_media, audit_env, dash_media, caplog,
                                                     which):
    caplog.set_level(logging.INFO)
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media)
    extra = (16).to_bytes(4, "big") + b"free" + b"\x00" * 8   # arquivo válido, 16 bytes maior que o declarado
    name = "/v.mp4" if which == "video" else "/a.mp4"
    sim.cdn.rules = [(token, body + extra if token == name else body) for token, body in sim.cdn.rules]
    batch = create_links(h, [link(A, "reel")])
    h.run_all()
    assert kinds(sim) == ["v", "a", "p"] and only_job(h, batch)["status"] == "completed"
    assert_progressive_original(h, sim)
    assert "dash_fallback=size_mismatch" in caplog.text and storage_leftovers(h) == []


@pytest.mark.parametrize("status", [206, 416, 500, 503])
def test_unexpected_http_status_on_a_stream_falls_back_instead_of_retrying(harness_factory, base_media, audit_env,
                                                                           dash_media, status):
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media)
    sim.cdn.rules.insert(0, ("/v.mp4", lambda request: httpx.Response(status, content=b"parcial")))
    batch = create_links(h, [link(A, "reel")])
    h.run_all()
    job = only_job(h, batch)
    assert kinds(sim) == ["v", "p"] and (job["status"], job["attempts"]) == ("completed", 0)
    assert_progressive_original(h, sim)


@pytest.mark.parametrize("body", [b"<html>erro</html>", b""], ids=["html", "vazio"])
def test_stream_that_is_not_media_falls_back(harness_factory, base_media, audit_env, dash_media, body):
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media, declare=(True, False))
    sim.cdn.rules.insert(0, ("/a.mp4", body))
    batch = create_links(h, [link(A, "reel")])
    h.run_all()
    assert kinds(sim) == ["v", "a", "p"] and only_job(h, batch)["status"] == "completed"
    assert_progressive_original(h, sim)
    assert storage_leftovers(h) == []


def test_mux_rejected_by_the_duration_check_falls_back_and_deletes_the_output(harness_factory, base_media, audit_env,
                                                                              dash_media, caplog, monkeypatch):
    """Áudio de 10 s para um manifesto de 2 s: a junção funciona, a verificação reprova."""
    caplog.set_level(logging.INFO)
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media, audio="audio_long", declare=(True, False))
    seen: list[list[str]] = []
    real_fallback = pipeline.JobExecutor._dash_fallback

    def spy(self, reason):
        temp = h.ctx.storage.temp
        seen.append(sorted(p.name for p in temp.rglob("*") if p.is_file()))
        return real_fallback(self, reason)

    monkeypatch.setattr(pipeline.JobExecutor, "_dash_fallback", spy)
    batch = create_links(h, [link(A, "reel")])
    h.run_all()
    assert kinds(sim) == ["v", "a", "p"] and only_job(h, batch)["status"] == "completed"
    assert seen == [[]], "saída juntada e fluxos intermediários apagados ANTES de baixar o MP4 direto"
    assert_progressive_original(h, sim)
    assert "dash_fallback=duration_mismatch" in caplog.text and storage_leftovers(h) == []
    check_invariants(h)


def test_mux_failure_falls_back(harness_factory, base_media, audit_env, dash_media, caplog):
    """O "áudio" servido é um fMP4 só de vídeo: o ffmpeg não acha a stream de áudio e a junção falha."""
    caplog.set_level(logging.INFO)
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media, declare=(True, False))
    sim.cdn.rules.insert(0, ("/a.mp4", dash_media["h264"]))
    batch = create_links(h, [link(A, "reel")])
    h.run_all()
    assert kinds(sim) == ["v", "a", "p"] and only_job(h, batch)["status"] == "completed"
    assert_progressive_original(h, sim)
    assert "dash_fallback=processing_failed" in caplog.text and storage_leftovers(h) == []
    for record in caplog.records:
        assert "cdn-" not in record.getMessage() or "dash_fallback" not in record.getMessage()


def test_dimensions_different_from_the_manifest_fall_back(harness_factory, base_media, audit_env, dash_media, caplog):
    caplog.set_level(logging.INFO)
    h, sim = make_app(harness_factory, base_media, audit_env, processor=MediaProcessor())
    manifest = simple_mpd(A, width=216, height=384, codecs=CODECS["h264"])
    sim.http.roots[A] = media_node_with_dash(A, manifest, original=(216, 384))
    sim.cdn.rules = [("/v.mp4", dash_media["h264"]), ("/a.mp4", dash_media["audio"])]
    batch = create_links(h, [link(A, "reel")])
    h.run_all()
    assert kinds(sim) == ["v", "a", "p"] and only_job(h, batch)["status"] == "completed"
    assert "dash_fallback=dimensions_mismatch" in caplog.text


def test_network_failure_on_a_stream_is_retried_like_today_without_the_third_request(harness_factory, base_media,
                                                                                     audit_env, dash_media):
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media)

    def broken(request):
        raise httpx.ConnectError("FAKE")

    sim.cdn.rules.insert(0, ("/a.mp4", broken))
    batch = create_links(h, [link(A, "reel")])
    h.run_all()
    job = only_job(h, batch)
    assert kinds(sim) == ["v", "a"], "falha de rede: sem MP4 direto; o retry controlado do job cuida"
    assert (job["status"], job["stage"], job["error_code"], job["attempts"]) == (
        "pending", "waiting_retry", "download_failed", 1)
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM media") == 0 and storage_leftovers(h) == []
    assert json.loads(refs(h)[0]["dash_streams"])["dash_audio_url"] == dash_url(A, "a"), "retry mantém a referência"


# --------------------------------------------------------------------------- fluxo recusado / URL expirada

ORIGINAL_URLS = "oh=FAKE_OH"   # toda URL da 1a consulta (as da renovação trocam para FRESH<n>)


@pytest.mark.parametrize("expired, expected", [("/a.mp4", ["v", "a", "p"]), ("/v.mp4", ["v", "p"])],
                         ids=["audio", "video"])
def test_refused_stream_goes_straight_to_the_direct_mp4_without_any_new_query(harness_factory, base_media, audit_env,
                                                                             dash_media, caplog, expired, expected):
    """403 num fluxo é falha de conteúdo: MP4 direto, SEM renovação e sem consulta nova da publicação."""
    caplog.set_level(logging.INFO)
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media)
    sim.cdn.expired = {expired}
    batch = create_links(h, [link(A, "reel")])
    h.run_all()
    h.run_all()
    assert sim.calls == [A], "ZERO consultas novas à publicação"
    assert kinds(sim) == expected, "exatamente estes acessos ao CDN, sem laço"
    assert all("FAKE_OH" in url for url in sim.cdn.hits), "nenhuma URL renovada foi usada"
    job = only_job(h, batch)
    assert (job["status"], job["error_code"], job["attempts"]) == ("completed", None, 0)
    assert job["media_url_refreshed_at"] is None
    assert_progressive_original(h, sim)
    assert "dash_fallback=media_url_expired" in caplog.text
    assert refs(h) == [] and storage_leftovers(h) == []
    check_invariants(h)


def test_refused_streams_and_refused_direct_mp4_renew_once_and_then_use_the_streams(harness_factory, base_media,
                                                                                    audit_env, dash_media):
    """Só o MP4 direto vencido leva à renovação única; depois dela o download roda de novo do começo (fluxos com as
    URLs novas)."""
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media)
    sim.cdn.expired = {ORIGINAL_URLS}
    batch = create_links(h, [link(A, "reel")])
    h.run_all()
    h.run_all()
    assert sim.calls == [A, A], "UMA renovação"
    assert kinds(sim) == ["v", "p", "v", "a"]
    assert all("FRESH2" in url for url in sim.cdn.hits[-2:]), "os DOIS fluxos voltam com URL nova"
    job = only_job(h, batch)
    assert (job["status"], job["error_code"]) == ("completed", None)
    media = only_media(h)
    assert (media["video_codec"], media["width"], media["height"]) == ("h264", *SIZE)
    assert refs(h) == [] and storage_leftovers(h) == []
    check_invariants(h)


def test_streams_refused_again_after_the_renewal_use_the_renewed_direct_mp4(harness_factory, base_media, audit_env,
                                                                            dash_media):
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media)
    sim.cdn.expired = {ORIGINAL_URLS, "/v.mp4", "/a.mp4"}
    batch = create_links(h, [link(A, "reel")])
    h.run_all()
    h.run_all()
    assert sim.calls == [A, A] and kinds(sim) == ["v", "p", "v", "p"]
    assert "FRESH2" in sim.cdn.hits[-1], "o MP4 direto baixado é o da renovação"
    assert only_job(h, batch)["status"] == "completed"
    assert_progressive_original(h, sim)
    assert refs(h) == [] and storage_leftovers(h) == []


def test_renewal_without_manifest_downloads_the_direct_mp4(harness_factory, base_media, audit_env, dash_media):
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media)
    sim.cdn.expired = {ORIGINAL_URLS}

    def drop_manifest(code: str) -> None:
        if sim.http.counts[code] == 2:
            sim.http.roots[code] = media_node("video", code)

    sim.http.on_call = drop_manifest
    batch = create_links(h, [link(A, "reel")])
    h.run_imports()
    assert refs(h)[0]["dash_streams"]
    h.run_jobs()
    assert sim.calls == [A, A] and kinds(sim) == ["v", "p", "p"]
    assert "FRESH2" in sim.cdn.hits[-1], "o MP4 direto baixado é o da renovação"
    assert only_job(h, batch)["status"] == "completed"
    assert_progressive_original(h, sim)


def test_everything_refused_even_after_the_renewal_fails_cleanly_without_a_loop(harness_factory, base_media,
                                                                                audit_env, dash_media):
    """Teto por tentativa do job: 3 acessos ao CDN antes da renovação + 3 depois, e 1 consulta. Aqui: vídeo recusado
    (o áudio nem é pedido) + MP4 direto recusado, duas vezes."""
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media)
    sim.cdn.expired = {".mp4"}
    batch = create_links(h, [link(A, "reel")])
    h.run_all()
    h.run_all()
    h.run_all()
    job = only_job(h, batch)
    assert sim.calls == [A, A] and kinds(sim) == ["v", "p", "v", "p"], "sem laço e sem nova consulta"
    assert (job["status"], job["error_code"]) == ("failed", "content_unavailable")
    assert all(token not in (job["error_message"] or "") for token in SENSITIVE)
    assert all(r["media_url"] is None and r["dash_streams"] is None for r in refs(h))
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM media") == 0 and storage_leftovers(h) == []
    check_invariants(h)


def test_worst_case_is_six_cdn_requests_and_one_query_per_job_attempt(harness_factory, base_media, audit_env,
                                                                      dash_media):
    """Pior caso: vídeo ok, áudio recusado, MP4 direto recusado -> renovação -> vídeo ok, áudio recusado, MP4 direto
    renovado ok. 6 acessos ao CDN, 1 renovação, nada além disso."""
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media)
    sim.cdn.expired = {"/a.mp4", "preel1_101.mp4?efg=FAKE&oh=FAKE_OH"}
    batch = create_links(h, [link(A, "reel")])
    h.run_all()
    h.run_all()
    assert sim.calls == [A, A] and kinds(sim) == ["v", "a", "p", "v", "a", "p"]
    assert only_job(h, batch)["status"] == "completed"
    assert_progressive_original(h, sim)
    assert storage_leftovers(h) == []


# --------------------------------------------------------------------------- manifesto hostil


HOSTILE = {
    "duration_400_digits": dict(duration="PT" + "9" * 400 + "S"),
    "duration_days": dict(duration="PT999H"),
    "width_15_digits": dict(width=10 ** 14),
    "height_400_digits": dict(height=int("9" * 400)),
    "video_bytes_400_digits": dict(video_bytes=int("9" * 400)),
    "audio_bytes_15_digits": dict(audio_bytes=10 ** 14),
    "frame_rate_huge": dict(frame_rate="999999999999999"),
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_hostile_manifest_never_blocks_the_batch(harness_factory, base_media, audit_env, dash_media, name):
    """Um link com números absurdos no manifesto + um link normal: os DOIS concluem (o hostil pelo MP4 direto, sem
    pedir nenhum fluxo); o lote não fica preso em `listing`."""
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media, codes=(B,))
    kwargs = {"width": SIZE[0], "height": SIZE[1], "codecs": CODECS["h264"], **HOSTILE[name]}
    sim.http.roots[A] = media_node_with_dash(A, simple_mpd(A, **kwargs), original=SIZE)
    batch = create_links(h, [link(A, "reel"), link(B, "reel")])
    h.run_all()
    h.run_all()
    done = h.batch(batch["id"])
    assert (done["status"], done["progress"]["downloaded"], done["progress"]["failed"]) == ("completed", 2, 0), done
    assert sim.calls == [A, B] and kinds(sim) == ["p", "v", "a"]
    hostile, normal = h.ctx.jobs.for_batch(batch["id"])
    assert (hostile["status"], hostile["error_code"], normal["status"]) == ("completed", None, "completed")
    codecs = [row["video_codec"] for row in h.ctx.db.fetchall("SELECT video_codec FROM media ORDER BY id")]
    assert codecs == [None, "h264"]
    assert storage_leftovers(h) == []
    check_invariants(h)


def test_non_finite_value_past_the_parser_still_cannot_break_the_insert(harness_factory, base_media, audit_env,
                                                                        dash_media, monkeypatch):
    """Defesa em profundidade: mesmo que o parser deixasse passar uma duração infinita, `dash_json` devolve None e o
    job nasce sem fluxos separados (nenhum JSON inválido chega ao CHECK da coluna)."""
    from app.integrations.instagram.web import normalize

    real = normalize._dash_fields

    def poisoned(item, video, stats):
        fields = real(item, video, stats)
        return {**fields, "dash_duration": float("inf")} if fields else fields

    monkeypatch.setattr(normalize, "_dash_fields", poisoned)
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media)
    batch = create_links(h, [link(A, "reel")])
    h.run_imports()
    assert h.batch(batch["id"])["status"] != "listing" and [r["dash_streams"] for r in refs(h)] == [None]
    h.run_jobs()
    assert kinds(sim) == ["p"] and only_job(h, batch)["status"] == "completed"


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_terminal_job_loses_the_stream_urls_at_the_same_points_as_media_url(harness_factory, base_media, audit_env,
                                                                           dash_media, status):
    """Falha/cancelamento apaga `dash_streams` junto com `media_url` (a linha fica para o retry com renovação)."""
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media, codes=(A, B))
    batch = create_links(h, [link(A, "reel"), link(B, "reel")])
    h.run_imports()
    first, second = h.ctx.jobs.for_batch(batch["id"])
    h.ctx.jobs.finish(first["id"], status=status)
    by_job = {r["job_id"]: r for r in refs(h)}
    assert by_job[first["id"]]["media_url"] is None and by_job[first["id"]]["dash_streams"] is None
    assert by_job[second["id"]]["media_url"] and by_job[second["id"]]["dash_streams"]
    h.ctx.jobs.clear_remote_urls(second["id"])
    assert all(r["dash_streams"] is None and r["media_url"] is None for r in refs(h))
    assert sim.cdn.hits == []


# --------------------------------------------------------------------------- cancelamento


def _assert_cancelled(h, sim, batch, expected_hits: list[str]) -> None:
    job = only_job(h, batch)
    assert job["status"] == "cancelled" and job["media_id"] is None, job
    assert h.batch(batch["id"])["status"] == "cancelled"
    assert kinds(sim) == expected_hits, "nenhum acesso ao CDN depois do cancelamento"
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM media") == 0
    assert all(not r["media_url"] and not r["dash_streams"] for r in refs(h))
    assert storage_leftovers(h) == []
    h.run_all()
    assert kinds(sim) == expected_hits and sim.calls == [A]
    check_invariants(h)


@pytest.mark.parametrize("during, expected", [("/v.mp4", ["v"]), ("/a.mp4", ["v", "a"])], ids=["video", "audio"])
def test_cancel_during_a_stream_download(harness_factory, base_media, audit_env, dash_media, during, expected):
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media)
    batch = create_links(h, [link(A, "reel")])
    h.run_imports()
    sim.cdn.on_hit = lambda url: cancel_batch(h, batch["id"]) if during in url else None
    h.run_jobs()
    _assert_cancelled(h, sim, batch, expected)


def test_cancel_between_the_video_and_the_audio(harness_factory, base_media, audit_env, dash_media, monkeypatch):
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media)
    batch = create_links(h, [link(A, "reel")])
    h.run_imports()
    real = sim.downloader.download_url
    delivered: list[Path] = []

    def then_cancel(url, dest_dir, **kwargs):
        path = real(url, dest_dir, **kwargs)
        delivered.append(Path(path))
        cancel_batch(h, batch["id"])   # o vídeo já está inteiro no disco; o áudio ainda não foi pedido
        return path

    monkeypatch.setattr(sim.downloader, "download_url", then_cancel)
    h.run_jobs()
    assert len(delivered) == 1 and not delivered[0].exists(), "o vídeo já baixado é apagado"
    _assert_cancelled(h, sim, batch, ["v"])


def test_cancel_right_before_the_mux_never_starts_ffmpeg(harness_factory, base_media, audit_env, dash_media):
    calls: list[str] = []

    class Processor(MediaProcessor):
        def mux_streams(self, video, audio, dst, **kwargs):
            calls.append("mux")
            return super().mux_streams(video, audio, dst, **kwargs)

    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media, processor=Processor())
    batch = create_links(h, [link(A, "reel")])
    h.run_imports()
    real_stage = pipeline.JobExecutor._stage

    def stage(self, name, **kwargs):
        if name == "muxing":
            cancel_batch(h, batch["id"])
        return real_stage(self, name, **kwargs)

    pipeline.JobExecutor._stage = stage
    try:
        h.run_jobs()
    finally:
        pipeline.JobExecutor._stage = real_stage
    assert calls == []
    _assert_cancelled(h, sim, batch, ["v", "a"])


def test_cancel_during_the_mux_kills_ffmpeg_and_leaves_no_temp(harness_factory, base_media, audit_env, dash_media,
                                                              monkeypatch):
    """O ffmpeg REAL lê as entradas em tempo real (`-re`, só neste teste) para a junção durar ~2 s; o cancelamento
    chega com o `.part` já sendo escrito."""
    import media_processor.processor as processor_module

    real_run = processor_module.run_ffmpeg

    def paced(cmd, **kwargs):
        at = cmd.index("-i")
        return real_run([*cmd[:at], "-re", *cmd[at:]], **kwargs)

    monkeypatch.setattr(processor_module, "run_ffmpeg", paced)
    seen: dict[str, object] = {}

    class Processor(MediaProcessor):
        def mux_streams(self, video, audio, dst, **kwargs):
            part = Path(str(dst) + ".part")

            def watch() -> None:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if part.exists():
                        seen["part"] = True
                        cancel_job_ids(h.ctx, [seen["job_id"]])
                        return
                    time.sleep(0.002)

            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
            started = time.monotonic()
            try:
                return super().mux_streams(video, audio, dst, **kwargs)
            finally:
                seen["elapsed"] = time.monotonic() - started
                watcher.join(timeout=5)

    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media, processor=Processor())
    batch = create_links(h, [link(A, "reel")])
    h.run_imports()
    seen["job_id"] = only_job(h, batch)["id"]
    h.run_jobs()
    assert seen.get("part") is True and seen["elapsed"] < 1.9, "o ffmpeg foi morto no meio da junção"
    job = only_job(h, batch)
    assert job["status"] == "cancelled" and job["media_id"] is None
    assert kinds(sim) == ["v", "a"] and h.ctx.db.scalar("SELECT COUNT(*) FROM media") == 0
    assert all(not r["media_url"] and not r["dash_streams"] for r in refs(h))
    assert storage_leftovers(h) == []
    check_invariants(h)


# --------------------------------------------------------------------------- duplicatas


def test_reimporting_the_same_link_is_a_duplicate_by_origin_without_download(harness_factory, base_media, audit_env,
                                                                            dash_media):
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media)
    create_links(h, [link(A, "reel")])
    h.run_all()
    before = only_media(h)
    hits = list(sim.cdn.hits)
    again = create_links(h, [link(A, "reel")])
    h.run_all()
    job = only_job(h, again)
    assert (job["status"], job["stage"], job["is_duplicate"], job["duplicate_reason"], job["media_id"]) == (
        "completed", "skipped", True, "source_id", before["id"])
    assert sim.cdn.hits == hits, "zero acessos ao CDN na reimportação"
    assert only_media(h) == before and refs(h) == []
    check_invariants(h)


def test_library_original_downloaded_as_direct_mp4_is_never_replaced_by_the_streams(harness_factory, base_media,
                                                                                    audit_env, dash_media):
    """Mídia já na biblioteca pelo MP4 direto (como a importada antes desta versão): reimportar o link, agora com
    manifesto, é duplicata por origem. Nada é baixado, rebaixado ou substituído."""
    h, sim = make_app(harness_factory, base_media, audit_env, processor=MediaProcessor())
    sim.add("reel", A)
    create_links(h, [link(A, "reel")])
    h.run_all()
    before = assert_progressive_original(h, sim)
    original = h.ctx.storage.resolve(before["original_path"])
    stat = (original.stat().st_size, original.stat().st_mtime_ns)
    assert kinds(sim) == ["p"]

    sim.http.roots[A] = node_for(A, dash_media)
    sim.cdn.rules = [("/v.mp4", dash_media["h264"]), ("/a.mp4", dash_media["audio"])]
    again = create_links(h, [link(A, "reel")])
    h.run_all()
    job = only_job(h, again)
    assert (job["status"], job["is_duplicate"], job["duplicate_reason"]) == ("completed", True, "source_id")
    assert kinds(sim) == ["p"], "nenhum fluxo separado é baixado para uma mídia que já existe"
    assert only_media(h) == before and (original.stat().st_size, original.stat().st_mtime_ns) == stat
    assert sha256_file(original) == before["original_sha256"]
    check_invariants(h)


def test_two_posts_with_the_same_streams_are_one_media_by_sha256(harness_factory, base_media, audit_env, dash_media):
    """A junção é determinística: o segundo post dá os mesmos bytes e vira duplicata por SHA-256, como hoje."""
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media, codes=(A, B))
    batch = create_links(h, [link(A, "reel"), link(B, "reel")])
    h.run_all()
    first, second = h.ctx.jobs.for_batch(batch["id"])
    assert kinds(sim) == ["v", "a", "v", "a"]
    assert (first["status"], first["is_duplicate"]) == ("completed", False)
    assert (second["status"], second["is_duplicate"], second["duplicate_reason"], second["media_id"]) == (
        "completed", True, "sha256", first["media_id"])
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM media") == 1 and storage_leftovers(h) == []
    check_invariants(h)


def test_same_link_in_two_open_batches_downloads_the_streams_once(harness_factory, base_media, audit_env, dash_media):
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media)
    one = create_links(h, [link(A, "reel")])
    two = create_links(h, [link(A, "reel")])
    h.run_imports()
    waiting = only_job(h, two)
    assert (waiting["duplicate_reason"], waiting["status"]) == ("duplicate_in_flight", "pending")
    h.run_all()
    h.run_all()
    assert kinds(sim) == ["v", "a"]
    assert only_job(h, one)["status"] == "completed" and only_job(h, two)["status"] == "completed"
    assert only_job(h, two)["media_id"] == only_job(h, one)["media_id"]
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM media") == 1 and refs(h) == []
    check_invariants(h)


# --------------------------------------------------------------------------- queda no meio


def test_crash_in_the_middle_leaves_no_temp_and_the_job_finishes_once(harness_factory, base_media, audit_env,
                                                                      dash_media):
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media)
    batch = create_links(h, [link(A, "reel")])
    h.run_imports()
    job = only_job(h, batch)
    # "app caiu no meio da junção": job em `downloading`/`muxing`, fluxos e `.part` deixados no temp do job.
    h.ctx.db.execute("UPDATE jobs SET status = 'downloading', stage = 'muxing', started_at = ?, claimed_by = 'morto'"
                     " WHERE id = ?", ("2026-01-01T00:00:00Z", job["id"]))
    temp = h.ctx.storage.job_temp_dir(job["id"], fresh=True)
    (temp / "cdn-aaaa.mp4").write_bytes(dash_media["h264"])
    (temp / "cdn-bbbb.mp4").write_bytes(dash_media["audio"])
    (temp / "cdn-cccc.part").write_bytes(b"parcial")
    (temp / "dash-muxed.mp4.part").write_bytes(b"parcial")
    (temp / "dash-muxed.mp4").write_bytes(b"parcial")
    h.ctx.db.execute("UPDATE jobs SET temp_path = ? WHERE id = ?", (h.ctx.storage.relative(temp), job["id"]))

    h2, _ = make_app(harness_factory, base_media, audit_env, root=h.root, sim=sim, processor=MediaProcessor())
    assert storage_leftovers(h2) == [], "a recuperação do startup apaga o temp do job interrompido"
    recovered = only_job(h2, batch)
    assert (recovered["status"], recovered["stage"], recovered["temp_path"], recovered["error_code"]) == (
        "pending", "queued", None, "interrupted")
    h2.run_all()
    assert kinds(sim) == ["v", "a"], "o job recomeça do zero (sem retomada parcial) e baixa os dois fluxos uma vez"
    assert sim.calls == [A], "sem nova consulta: a referência do job não terminal continua válida"
    done = only_job(h2, batch)
    assert (done["status"], done["error_code"]) == ("completed", None)
    assert only_media(h2)["video_codec"] == "h264" and refs(h2) == [] and storage_leftovers(h2) == []
    check_invariants(h2)


# --------------------------------------------------------------------------- nada sensível em log/API


def test_nothing_sensitive_reaches_logs_errors_or_the_api(harness_factory, base_media, audit_env, dash_media, caplog):
    """Três links no mesmo lote: junção ok (A), fallback por junção reprovada (B) e fluxos recusados até o fim (C)."""
    caplog.set_level(logging.DEBUG)
    h, sim = make_app(harness_factory, base_media, audit_env, processor=MediaProcessor())
    sim.http.roots[A] = node_for(A, dash_media)
    sim.http.roots[B] = node_for(B, dash_media, declare=(True, False), duration="PT9.000000S")
    sim.http.roots[C] = node_for(C, dash_media)
    sim.cdn.rules = [("/v.mp4", dash_media["h264"]), ("/a.mp4", dash_media["audio"])]
    sim.cdn.expired = {f"/{C}/"}   # tudo de C recusado, antes e depois da renovação
    batch = create_links(h, [link(A, "reel"), link(B, "reel"), link(C, "reel")])
    h.run_imports()
    pending = (h.client.get("/api/jobs", params={"batch_id": batch["id"]}).text
               + h.client.get(f"/api/batches/{batch['id']}", params={"include_jobs": "true"}).text
               + h.client.get(f"/api/batches/{batch['id']}/links").text)
    h.run_jobs()
    h.run_all()
    statuses = [j["status"] for j in h.ctx.jobs.for_batch(batch["id"])]
    assert statuses == ["completed", "completed", "failed"], statuses
    assert "dash_fallback=duration_mismatch" in caplog.text and "etapa muxing" in caplog.text

    for record in caplog.records:
        if record.name.startswith(("httpx", "httpcore", "asyncio")):
            continue
        message = record.getMessage()
        for token in SENSITIVE:
            if token in (A, B, C) and record.name not in ("media_tool.jobs", "app.integrations.instagram.web.provider",
                                                           "app.integrations.instagram.web.normalize"):
                continue   # o shortcode faz parte do link colado pelo dono (lote por link); fora dos caminhos novos
            assert token not in message, (record.name, token)
    finished = (h.client.get("/api/jobs", params={"batch_id": batch["id"]}).text
                + h.client.get(f"/api/batches/{batch['id']}", params={"include_jobs": "true"}).text
                + h.client.get("/api/media").text + h.client.get("/api/diagnostics").text)
    for text in (pending, finished):
        for token in ("/dash/", "dash_streams", "dash_video_url", "dash_audio_url", "FAKE_OH", "FRESH", "<MPD",
                      "fbcdn.net/v/dash"):
            assert token not in text, token
    for job in h.ctx.jobs.for_batch(batch["id"]):
        assert all(token not in (job["error_message"] or "") for token in SENSITIVE)


def test_dash_path_logs_no_url_even_with_the_redaction_filter_off(harness_factory, base_media, audit_env, dash_media,
                                                                 caplog, monkeypatch):
    """O filtro de redação mascararia uma URL logada por engano; aqui ele fica DESLIGADO, então qualquer log novo com
    URL, assinatura ou caminho de fluxo aparece cru e reprova o teste."""
    from app.core import logging as app_logging

    monkeypatch.setattr(app_logging.RedactingFilter, "filter", lambda self, record: True)
    caplog.set_level(logging.DEBUG)
    h, sim = make_app(harness_factory, base_media, audit_env, processor=MediaProcessor())
    sim.http.roots[A] = node_for(A, dash_media)
    sim.http.roots[B] = node_for(B, dash_media, declare=(True, False), duration="PT9.000000S")
    sim.http.roots[C] = node_for(C, dash_media)
    sim.cdn.rules = [("/v.mp4", dash_media["h264"]), ("/a.mp4", dash_media["audio"])]
    sim.cdn.expired = {f"/{C}/"}
    batch = create_links(h, [link(A, "reel"), link(B, "reel"), link(C, "reel")])
    h.run_all()
    h.run_all()
    assert [j["status"] for j in h.ctx.jobs.for_batch(batch["id"])] == ["completed", "completed", "failed"]
    checked = [r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore", "asyncio"))]
    assert any("dash_fallback=duration_mismatch" in m for m in checked)
    assert any("dash_fallback=media_url_expired" in m for m in checked)
    sentinel = dash_url(A, "v")
    logging.getLogger("media_tool.jobs").info("sentinela %s", sentinel)
    assert caplog.records[-1].getMessage().endswith(sentinel), "o filtro está mesmo desligado neste teste"
    for message in checked:
        for token in ("fbcdn", "cdninstagram", "oh=", "FAKE_OH", "FRESH", "/dash/", "BaseURL", "<MPD", ".mp4?"):
            assert token not in message, (token, message[:80])


# --------------------------------------------------------------------------- fluxo por perfil inalterado


def test_profile_listing_of_the_real_provider_ignores_a_valid_manifest():
    """Listagem autenticada (perfil): mesmo com um manifesto VÁLIDO no nó, nenhuma mídia sai com fluxos separados."""
    node = video_node("PERFILaaa01")
    node["video_dash_manifest"] = simple_mpd("PERFILaaa01", width=2160, height=3840)
    downloader, _, _, _ = build([[node]])
    [item] = list(downloader.iter_profile_media("perfil_teste", "all", 10))
    assert item.dash_video_url is None and item.dash_audio_url is None and item.dash_width is None
    assert (item.width, item.height) == (720, 1280) and dash_json(item) is None


def test_profile_batch_keeps_downloading_the_direct_file(harness):
    """Lote por perfil: o item não tem fluxos separados; `download_url` nunca é chamado e nada vira `dash_streams`."""
    from media_fakes import remote

    called: list[str] = []
    harness.downloader.items = [remote("PERFILbbb02", media_type="video")]
    harness.downloader.download_url = lambda *args, **kwargs: called.append("download_url")
    batch = harness.import_profile()
    harness.run_imports()
    assert [row["dash_streams"] for row in refs(harness)] == [None]
    harness.run_jobs()
    assert called == [] and harness.batch(batch["id"])["status"] == "completed"
    assert only_media(harness)["video_codec"] is None


# --------------------------------------------------------------------------- providers


def test_provider_without_separate_streams_uses_the_direct_file_without_extra_requests(harness_factory, caplog):
    """Provider que entrega `dash_*` mas não implementa `download_url` (padrão da interface): MP4 direto, 1 download."""
    from dataclasses import replace

    caplog.set_level(logging.INFO)
    [item] = reel(A)
    item = replace(item, dash_video_url=dash_url(A, "v"), dash_audio_url=dash_url(A, "a"), dash_width=108,
                   dash_height=192)
    h, downloader = links_app(harness_factory, {A: [item]})
    batch = create_links(h, [link(A, "reel")])
    h.run_all()
    assert only_job(h, batch)["status"] == "completed" and downloader.download_urls == {f"{A}:0": [item.media_url]}
    assert "dash_fallback=content_unavailable" in caplog.text


def test_base_downloader_refuses_download_url(tmp_path):
    with pytest.raises(InstagramError) as excinfo:
        FakeInstagramDownloader().download_url(dash_url(A, "v"), tmp_path)
    assert (excinfo.value.code, excinfo.value.retryable) == ("content_unavailable", False)
    assert A not in str(excinfo.value)


@pytest.mark.parametrize("strategy, expected", [("web", "primary"), ("auto", "primary"), ("gallery_dl", "fallback")])
def test_composite_sends_download_url_to_the_provider_that_resolves_posts(tmp_path, strategy, expected):
    class Recorder(FakeInstagramDownloader):
        def __init__(self, name: str):
            super().__init__()
            self.name = name

        def download_url(self, url, dest_dir, *, cancel=None, progress=None):
            return Path(dest_dir) / self.name

    composite = CompositeInstagramDownloader(Recorder("primary"), Recorder("fallback"), strategy=strategy)
    assert composite.download_url(dash_url(A, "v"), tmp_path).name == expected


@pytest.mark.parametrize("error, code, retryable", [
    (DownloadError("forbidden", "FAKE"), "media_url_expired", False),
    (DownloadError("not_found", "FAKE"), "media_url_expired", False),
    (DownloadError("network_error", "FAKE"), "download_failed", True),
    (DownloadError("network_error", "FAKE", http_status=206), "download_failed", False),
    (DownloadError("too_large", "FAKE"), "download_failed", False),
    (DownloadError("invalid_media", "FAKE"), "download_failed", False),
    (DownloadError("invalid_url", "FAKE"), "download_failed", False),
])
def test_web_provider_download_url_error_mapping(tmp_path, error, code, retryable):
    url = dash_url(A, "v")

    def cdn(target, dest_dir, *, max_bytes, cancel=None, progress=None):
        assert target == url
        raise error

    downloader, _, bootstrap, graphql = build(cdn=cdn)
    with pytest.raises(InstagramError) as excinfo:
        downloader.download_url(url, tmp_path)
    assert (excinfo.value.code, excinfo.value.retryable) == (code, retryable)
    assert all(token not in str(excinfo.value) for token in SENSITIVE)
    assert bootstrap.calls == 0 and graphql.requests == []


def test_web_provider_download_keeps_retrying_on_unexpected_status_of_the_direct_file(tmp_path):
    """Só os fluxos separados mudam: no MP4 direto, status inesperado continua `download_failed` retryable."""
    from test_web_provider import _stale

    def cdn(target, dest_dir, *, max_bytes, cancel=None, progress=None):
        raise DownloadError("network_error", "FAKE", http_status=500)

    downloader, *_ = build(cdn=cdn)
    with pytest.raises(InstagramError) as excinfo:
        downloader.download(_stale("IMG1"), tmp_path)
    assert (excinfo.value.code, excinfo.value.retryable) == ("download_failed", True)


def test_web_provider_download_url_uses_the_cdn_downloader(tmp_path):
    cdn = FakeCdn()
    downloader, *_ = build(cdn=cdn)
    path = downloader.download_url(dash_url(A, "v"), tmp_path)
    assert path.is_file() and cdn.urls == [dash_url(A, "v")]


def test_cdn_download_error_carries_the_unexpected_status():
    with pytest.raises(DownloadError) as excinfo:
        cdn_downloader._raise_for_status(206)
    assert (excinfo.value.code, excinfo.value.http_status) == ("network_error", 206)
    with pytest.raises(DownloadError) as excinfo:
        cdn_downloader._raise_for_status(403)
    assert (excinfo.value.code, excinfo.value.http_status) == ("forbidden", None)


# --------------------------------------------------------------------------- job_remote_refs.dash_streams (JSON)


def _fields() -> dict:
    return {"dash_video_url": dash_url(A, "v"), "dash_audio_url": dash_url(A, "a"), "dash_video_bytes": 10,
            "dash_audio_bytes": 5, "dash_width": 108, "dash_height": 192, "dash_duration": 2.5}


def test_dash_json_round_trip_and_absence():
    from dataclasses import replace

    [plain] = reel(A)
    assert dash_json(plain) is None
    assert dash_json(replace(plain, dash_video_url=dash_url(A, "v"))) is None, "sem o áudio não há fluxos separados"
    full = replace(plain, **_fields())
    assert dash_fields_from_json(dash_json(full)) == _fields()
    optional = {**_fields(), "dash_video_bytes": None, "dash_audio_bytes": None, "dash_duration": None}
    assert dash_fields_from_json(json.dumps(optional)) == optional
    assert dash_fields_from_json(json.dumps({**_fields(), "dash_duration": 3})) == {**_fields(), "dash_duration": 3.0}


@pytest.mark.parametrize("change", [
    {"dash_video_url": None}, {"dash_audio_url": ""}, {"dash_video_url": "http://video-fake.fbcdn.net/v.mp4"},
    {"dash_audio_url": "https://evil.example.com/a.mp4"}, {"dash_video_url": 7}, {"dash_width": None},
    {"dash_height": 0}, {"dash_width": "108"}, {"dash_width": True}, {"dash_video_bytes": -1},
    {"dash_audio_bytes": "5"}, {"dash_audio_bytes": 1.5}, {"dash_duration": -1}, {"dash_duration": "2"},
    {"dash_duration": True}, {"dash_duration": float("inf")},
])
def test_malformed_dash_reference_is_ignored(change):
    assert dash_fields_from_json(json.dumps({**_fields(), **change})) is None


def test_dash_json_never_emits_invalid_json_or_out_of_range_values():
    from dataclasses import replace

    [plain] = reel(A)
    full = replace(plain, **_fields())
    for change in ({"dash_duration": float("inf")}, {"dash_duration": float("nan")}, {"dash_duration": -1.0},
                   {"dash_duration": 0.0}, {"dash_duration": 1e12}, {"dash_width": 10 ** 14}, {"dash_height": 0},
                   {"dash_video_bytes": 10 ** 14}, {"dash_audio_bytes": int("9" * 400)}, {"dash_width": int("9" * 400)},
                   {"dash_duration": int("9" * 400)}, {"dash_video_url": "https://evil.example.com/v.mp4"}):
        assert dash_json(replace(full, **change)) is None, change
    text = dash_json(full)
    assert "Infinity" not in text and "NaN" not in text and json.loads(text) == _fields()


@pytest.mark.parametrize("raw", [
    '"dash_duration": 1' + "0" * 400, '"dash_duration": Infinity', '"dash_duration": -Infinity',
    '"dash_duration": NaN', '"dash_duration": 1e999', '"dash_duration": 0', '"dash_width": 1' + "0" * 400,
    '"dash_width": 100000000000000', '"dash_video_bytes": 1' + "0" * 400, '"dash_audio_bytes": 100000000000000',
])
def test_gigantic_or_non_finite_numbers_in_the_database_are_ignored_without_exception(raw):
    """Inclui `OverflowError` (inteiro de 400 dígitos como duração): malformado -> None, nunca exceção."""
    text = json.dumps(_fields())
    key = raw.split(":")[0]
    start = text.index(key)
    end = text.find(",", start)
    end = len(text) - 1 if end == -1 else end
    assert dash_fields_from_json(text[:start] + raw + text[end:]) is None


@pytest.mark.parametrize("text", [None, "", 5, b"{}", "não é json", "[]", "null", '"texto"', "{}"])
def test_dash_reference_that_is_not_an_object_is_ignored(text):
    assert dash_fields_from_json(text) is None


def test_malformed_reference_in_the_database_downloads_the_direct_file(harness_factory, base_media, audit_env,
                                                                      dash_media):
    h, sim = dash_app(harness_factory, base_media, audit_env, dash_media)
    batch = create_links(h, [link(A, "reel")])
    h.run_imports()
    h.ctx.db.execute("UPDATE job_remote_refs SET dash_streams = ?",
                     (json.dumps({**_fields(), "dash_video_url": "https://evil.example.com/v.mp4"}),))
    h.run_jobs()
    assert kinds(sim) == ["p"] and only_job(h, batch)["status"] == "completed"
    assert_progressive_original(h, sim)
