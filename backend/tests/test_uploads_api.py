"""Upload de vídeos locais (POST /api/library/uploads): streaming, teto próprio, ffprobe, dedup e temporários."""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from media_processor import MediaProcessor

from app.jobs.recovery import run_recovery
from app.services.upload_service import UploadSlots
from conftest import BASE_URL
from media_fakes import FakeProcessor
from synthetic_streams import requires_ffmpeg

URL = "/api/library/uploads"
FAKE_VIDEO = b"FAKE-video" + b"\x00" * 2048


def _upload(h, data: bytes, name: str = "clip.mp4", **kwargs):
    return h.client.post(URL, files={"file": (name, data, "video/mp4")}, **kwargs)


def _upload_temps(h) -> list[str]:
    temp = h.ctx.storage.temp
    return sorted(p.name for p in temp.iterdir() if p.name.startswith("upload-")) if temp.is_dir() else []


def _media_count(h) -> int:
    return int(h.ctx.db.scalar("SELECT COUNT(*) FROM media"))


class Mp4FakeProcessor(FakeProcessor):
    """FakeProcessor cujo probe informa container MP4 (o upload deriva a extensão do container real)."""

    def probe(self, path: Path):
        return replace(super().probe(path), format_name="mov,mp4,m4a,3gp,3g2,mj2", tags={"major_brand": "isom"})


@pytest.fixture
def make(harness_factory):
    def factory(**kwargs):
        kwargs.setdefault("processor", Mp4FakeProcessor())
        h = harness_factory(**kwargs)
        h.ctx.worker.disk_check = lambda: (True, 1 << 40, 0)
        return h

    return factory


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args], check=True,
                   timeout=120)


def _real_mp4(path: Path) -> bytes:
    """MP4 H.264 + AAC sem faststart (moov no fim): truncado, o ffprobe não o reconhece."""
    _ff("-f", "lavfi", "-i", "testsrc=size=160x96:rate=25:duration=2", "-f", "lavfi", "-i",
        "sine=frequency=440:sample_rate=48000:duration=2", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path))
    return path.read_bytes()


# --------------------------------------------------------------------------- vídeo real (FFmpeg)


@requires_ffmpeg
def test_upload_real_video_creates_local_media_and_duplicate(make, tmp_path):
    h = make(processor=MediaProcessor())
    data = _real_mp4(tmp_path / "src.mp4")

    first = _upload(h, data, name="Meu vídeo.mp4")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["duplicate"] is False
    media = body["media"]
    assert media["source_platform"] == "local"
    assert media["media_type"] == "video"
    assert media["original_filename"] == "Meu vídeo.mp4"
    assert media["source_url"] is None and media["source_profile"] is None
    assert media["width"] == 160 and media["height"] == 96
    assert media["duration"] == pytest.approx(2.0, abs=0.1)
    row = h.ctx.media.get(media["id"])
    original = h.ctx.storage.resolve(row["original_path"])
    assert original.is_file() and original.read_bytes() == data
    assert row["original_path"].startswith("originals/")
    assert row["thumbnail_path"] and h.ctx.storage.resolve(row["thumbnail_path"]).is_file()
    assert _upload_temps(h) == []

    second = _upload(h, data, name="outro nome.mp4")
    assert second.status_code == 200, second.text
    assert second.json()["duplicate"] is True
    assert second.json()["media"]["id"] == media["id"]
    assert _media_count(h) == 1
    assert _upload_temps(h) == []


@requires_ffmpeg
@pytest.mark.parametrize("payload", [b"just some text\n" * 100,
                                     b"<!doctype html><html><body>erro</body></html>" * 50])
def test_upload_text_or_html_is_invalid(make, payload):
    h = make(processor=MediaProcessor())
    response = _upload(h, payload, name="pagina.mp4")
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_media"
    assert _media_count(h) == 0
    assert _upload_temps(h) == []


@requires_ffmpeg
def test_upload_image_is_unsupported(make, tmp_path):
    h = make(processor=MediaProcessor())
    png = tmp_path / "img.png"
    _ff("-f", "lavfi", "-i", "testsrc=size=64x48:duration=1", "-frames:v", "1", str(png))
    response = _upload(h, png.read_bytes(), name="img.png")
    assert response.status_code == 422
    assert response.json() == {"detail": "Só vídeos podem ser editados.", "code": "unsupported_media"}
    assert _media_count(h) == 0
    assert _upload_temps(h) == []


@requires_ffmpeg
def test_upload_truncated_video_is_invalid(make, tmp_path):
    h = make(processor=MediaProcessor())
    data = _real_mp4(tmp_path / "src.mp4")
    response = _upload(h, data[: len(data) // 2], name="cortado.mp4")
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_media"
    assert _media_count(h) == 0
    assert list(h.ctx.storage.originals.rglob("*.*")) == []
    assert _upload_temps(h) == []


# --------------------------------------------------------------------------- limites de tamanho


def test_upload_above_ceiling_is_413_while_streaming(make):
    h = make(max_download_bytes=1000)
    # Content-Length dentro da folga do multipart: a recusa acontece durante a leitura do corpo.
    response = _upload(h, FAKE_VIDEO + b"\x00" * 30_000)
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"
    assert _media_count(h) == 0
    assert _upload_temps(h) == []


def test_upload_declared_length_above_ceiling_is_413_before_reading(make):
    h = make(max_download_bytes=1000)
    response = _upload(h, FAKE_VIDEO + b"\x00" * 200_000)
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"
    assert _upload_temps(h) == []


def test_upload_route_is_exempt_from_1mb_limit_but_others_are_not(make):
    h = make()
    big = FAKE_VIDEO + b"\x01" * (1024 * 1024 + 4096)
    response = _upload(h, big, name="grande.mp4")
    assert response.status_code == 200, response.text
    assert response.json()["media"]["file_size"] == len(big)

    other = h.client.post("/api/media/process", content=b"{" + b" " * (1024 * 1024 + 10) + b"}",
                          headers={"Content-Type": "application/json"})
    assert other.status_code == 413
    assert other.json()["detail"] == "Corpo da requisição maior que o limite de 1 MB."
    # O prefixo isento não se estende a caminhos parecidos.
    sibling = h.client.post(URL + "-x", content=b"x" * (1024 * 1024 + 10))
    assert sibling.status_code == 413


# --------------------------------------------------------------------------- segurança e pré-condições


def test_upload_requires_client_header_and_allowed_origin(make):
    h = make()
    from fastapi.testclient import TestClient

    bare = TestClient(h.app, base_url=BASE_URL)
    response = bare.post(URL, files={"file": ("a.mp4", FAKE_VIDEO, "video/mp4")})
    assert response.status_code == 403
    assert response.json()["code"] == "client_header_required"

    evil = _upload(h, FAKE_VIDEO, headers={"Origin": "http://evil.example"})
    assert evil.status_code == 403
    assert evil.json()["code"] == "origin_not_allowed"

    wrong_host = _upload(h, FAKE_VIDEO, headers={"Host": "evil.example:8765"})
    assert wrong_host.status_code == 400
    assert _media_count(h) == 0
    assert _upload_temps(h) == []


def test_upload_refused_when_disk_is_low(make):
    h = make()
    h.ctx.worker.disk_check = lambda: (False, 10, 1000)
    response = _upload(h, FAKE_VIDEO)
    assert response.status_code == 507
    assert response.json()["code"] == "disk_full"
    assert _media_count(h) == 0
    assert _upload_temps(h) == []


def test_upload_requires_ffmpeg(make):
    h = make(processor=Mp4FakeProcessor(ffmpeg_found=False))
    response = _upload(h, FAKE_VIDEO)
    assert response.status_code == 503
    assert response.json()["code"] == "ffmpeg_missing"
    assert _upload_temps(h) == []


def test_upload_limits_concurrency(make):
    h = make()
    slots = h.app.state.upload_slots = UploadSlots(2)
    assert slots.try_acquire() and slots.try_acquire()
    response = _upload(h, FAKE_VIDEO)
    assert response.status_code == 429
    assert response.json()["code"] == "too_many_uploads"
    slots.release()
    assert _upload(h, FAKE_VIDEO).status_code == 200
    assert _upload_temps(h) == []


def test_upload_requires_multipart_file_field(make):
    h = make()
    raw = h.client.post(URL, content=FAKE_VIDEO, headers={"Content-Type": "application/octet-stream"})
    assert raw.status_code == 422 and raw.json()["code"] == "invalid_upload"
    other_field = h.client.post(URL, files={"outro": ("a.mp4", FAKE_VIDEO, "video/mp4")})
    assert other_field.status_code == 422 and other_field.json()["code"] == "invalid_upload"
    assert _media_count(h) == 0
    assert _upload_temps(h) == []


def test_upload_filename_never_becomes_a_path(make):
    h = make()
    response = _upload(h, FAKE_VIDEO, name="..\\..\\pasta/../evil.mov")
    assert response.status_code == 200, response.text
    media = response.json()["media"]
    assert media["original_filename"] == "evil.mov"
    row = h.ctx.media.get(media["id"])
    # A extensão vem do container (MP4), nunca do nome enviado.
    assert row["original_path"].startswith("originals/") and row["original_path"].endswith(".mp4")
    assert h.ctx.storage.resolve(row["original_path"]).is_file()


def test_fake_image_upload_is_unsupported(make):
    h = make()
    response = _upload(h, b"FAKE-image" + b"\x00" * 64, name="foto.mp4")
    assert response.status_code == 422 and response.json()["code"] == "unsupported_media"
    assert _upload_temps(h) == []


def test_startup_recovery_removes_upload_temps(make):
    h = make()
    leftover = h.ctx.storage.temp / "upload-0123456789abcdef"
    leftover.mkdir(parents=True)
    (leftover / "upload.bin").write_bytes(b"parcial")
    report = run_recovery(h.ctx)
    assert report.temp_removed >= 1
    assert not leftover.exists()


# --------------------------------------------------------------------------- parser em pedaços pequenos


@pytest.mark.parametrize("chunk", [1, 7, 64, 4096])
def test_multipart_reader_handles_any_chunk_boundary(tmp_path, chunk):
    import asyncio

    from app.services.upload_service import _MultipartFileReader

    boundary = b"----bnd123"
    # Conteúdo com sequências parecidas com o delimitador, que não podem encerrar a parte antes da hora.
    content = b"FAKE-video\r\n------bnd12\r\n--" + bytes(range(256)) * 40 + b"\r\n----bnd"
    body = (b"preambulo ignorado\r\n--" + boundary + b"\r\n"
            b'Content-Disposition: form-data; name="nota"\r\n\r\nvalor\r\n--' + boundary + b"\r\n"
            b'Content-Disposition: form-data; name="file"; filename="a\\"b.mp4"\r\n'
            b"Content-Type: video/mp4\r\n\r\n" + content + b"\r\n--" + boundary + b"--\r\nepilogo")
    target = tmp_path / "upload.bin"
    reader = _MultipartFileReader(boundary, target, max_bytes=len(content))

    async def run():
        for start in range(0, len(body), chunk):
            await reader.feed(body[start:start + chunk])

    asyncio.run(run())
    reader.close()
    assert reader.state == "done" and reader.file_seen
    assert reader.filename == 'a"b.mp4'
    assert target.read_bytes() == content


# --------------------------------------------------------------------------- revisão: integridade e container


def _originals(h) -> list[Path]:
    root = h.ctx.storage.originals
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def _faststart_mp4(path: Path, seconds: int = 6) -> bytes:
    _ff("-f", "lavfi", "-i", f"testsrc=size=320x240:rate=25:duration={seconds}", "-f", "lavfi", "-i",
        f"sine=frequency=440:sample_rate=48000:duration={seconds}", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(path))
    return path.read_bytes()


def _assert_rejected(h, response, code: str) -> None:
    assert response.status_code == 422, response.text
    assert response.json()["code"] == code
    assert _media_count(h) == 0
    assert _originals(h) == []
    assert _upload_temps(h) == []


@requires_ffmpeg
@pytest.mark.parametrize("fraction", [1 / 3, 0.9])
def test_upload_truncated_faststart_is_invalid(make, tmp_path, fraction):
    h = make(processor=MediaProcessor())
    data = _faststart_mp4(tmp_path / "fs.mp4")
    response = _upload(h, data[: int(len(data) * fraction)], name="cortado.mp4")
    _assert_rejected(h, response, "invalid_media")


@requires_ffmpeg
def test_upload_truncated_without_faststart_is_invalid(make, tmp_path):
    h = make(processor=MediaProcessor())
    data = _real_mp4(tmp_path / "src.mp4")
    _assert_rejected(h, _upload(h, data[: len(data) // 3], name="cortado.mp4"), "invalid_media")


@requires_ffmpeg
def test_upload_mp4_with_zeroed_tail_is_invalid(make, tmp_path):
    h = make(processor=MediaProcessor())
    data = bytearray(_faststart_mp4(tmp_path / "z.mp4"))
    cut = len(data) // 4
    data[cut:] = bytes(len(data) - cut)  # 75% do arquivo em zeros, índice intacto
    _assert_rejected(h, _upload(h, bytes(data), name="zeros.mp4"), "invalid_media")


@requires_ffmpeg
def test_upload_valid_faststart_long_video_passes_integrity(make, tmp_path):
    h = make(processor=MediaProcessor())
    response = _upload(h, _faststart_mp4(tmp_path / "ok.mp4", seconds=8), name="ok.mp4")
    assert response.status_code == 200, response.text
    assert response.json()["media"]["duration"] == pytest.approx(8.0, abs=0.1)


@requires_ffmpeg
def test_upload_gif_is_refused(make, tmp_path):
    h = make(processor=MediaProcessor())
    gif = tmp_path / "a.gif"
    _ff("-f", "lavfi", "-i", "testsrc=size=64x48:rate=10:duration=2", str(gif))
    response = _upload(h, gif.read_bytes(), name="anim.mp4")
    _assert_rejected(h, response, "unsupported_media")
    assert "MP4, MOV, WebM ou MKV" in response.json()["detail"]


@requires_ffmpeg
@pytest.mark.parametrize(("suffix", "extra", "ext", "mime", "sent_name"), [
    (".mkv", [], ".mkv", "video/x-matroska", "nome.webm"),
    (".mov", [], ".mov", "video/quicktime", "nome.mp4"),
    (".mp4", [], ".mp4", "video/mp4", "nome.m4v"),
    (".mp4", [], ".mp4", "video/mp4", "sem_extensao"),
])
def test_upload_extension_follows_real_container(make, tmp_path, suffix, extra, ext, mime, sent_name):
    h = make(processor=MediaProcessor())
    src = tmp_path / f"v{suffix}"
    _ff("-f", "lavfi", "-i", "testsrc=size=64x48:rate=10:duration=2", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", *extra, str(src))
    # Extensão do nome da mesma família (ou nenhuma): o arquivo gravado segue o container real.
    response = _upload(h, src.read_bytes(), name=sent_name)
    assert response.status_code == 200, response.text
    row = h.ctx.media.get(response.json()["media"]["id"])
    assert row["original_path"].endswith(ext)
    assert row["mime_type"] == mime


def test_container_extension_rules():
    from app.core.errors import AppError
    from app.services.upload_service import container_extension

    probe = _fake_probe()
    assert container_extension(replace(probe, format_name="mov,mp4,m4a,3gp,3g2,mj2", tags={})) == ".mp4"
    assert container_extension(replace(probe, format_name="mov,mp4,m4a", tags={"major_brand": "qt  "})) == ".mov"
    assert container_extension(replace(probe, format_name="matroska,webm", video_codec="vp9",
                                       audio_codec="opus")) == ".webm"
    assert container_extension(replace(probe, format_name="matroska,webm", video_codec="h264",
                                       audio_codec="aac")) == ".mkv"
    for fmt in ("gif", "avi", "mpegts", "image2"):
        with pytest.raises(AppError) as info:
            container_extension(replace(probe, format_name=fmt))
        assert info.value.code == "unsupported_media"


def _fake_probe():
    from media_processor import ProbeResult

    return ProbeResult(media_type="video", format_name="fake", duration=2.0, width=64, height=48, fps=25.0,
                       has_audio=False, has_video=True, video_codec="h264", audio_codec=None, rotation=0, tags={},
                       stream_tags=[], raw={})


# --------------------------------------------------------------------------- revisão: disco, desconexão, nome, teto


def test_disk_full_while_writing_is_507_and_cleans_temp(make, monkeypatch):
    import app.services.upload_service as upload_service

    h = make()

    class FullDisk:
        def write(self, _data):
            raise OSError(28, "No space left on device")

        def close(self):
            pass

    monkeypatch.setattr(upload_service, "open", lambda *a, **k: FullDisk(), raising=False)
    response = _upload(h, FAKE_VIDEO + bytes(3 * 1024 * 1024))
    assert response.status_code == 507
    assert response.json()["code"] == "disk_full"
    assert _upload_temps(h) == []
    assert h.app.state.upload_slots._active == 0


def _asgi_post(app, chunks: list[bytes], *, disconnect: bool) -> list[dict]:
    sent: list[dict] = []

    async def run():
        queue = list(chunks)

        async def receive():
            if queue:
                return {"type": "http.request", "body": queue.pop(0), "more_body": bool(queue) or disconnect}
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                 "scheme": "http", "path": URL, "raw_path": URL.encode(), "query_string": b"", "root_path": "",
                 "headers": [(b"host", b"127.0.0.1:8765"), (b"content-type", b"multipart/form-data; boundary=BND"),
                             (b"x-media-tool-client", b"t")],
                 "client": ("127.0.0.1", 5), "server": ("127.0.0.1", 8765), "state": {}}
        await app(scope, receive, send)

    asyncio.run(run())
    return sent


def test_client_disconnect_is_handled_quietly(make, caplog):
    h = make()
    head = (b"--BND\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.mp4\"\r\n\r\n" + FAKE_VIDEO)
    with caplog.at_level("INFO"):
        sent = _asgi_post(h.app, [head, bytes(100_000)], disconnect=True)
    statuses = [m["status"] for m in sent if m["type"] == "http.response.start"]
    assert statuses == [400]
    assert not [r for r in caplog.records if r.levelname in ("ERROR", "CRITICAL")]
    assert _upload_temps(h) == [] and _media_count(h) == 0
    assert h.app.state.upload_slots._active == 0


def test_filename_drops_control_and_bidi_characters():
    from app.services.upload_service import sanitize_filename

    assert sanitize_filename("‮gpj.mp4") == "gpj.mp4"
    assert sanitize_filename("a​b⁦c⁩\x00\x1f\x7f .mp4") == "abc.mp4"
    assert sanitize_filename("pasta\\..\\‮nome.mov") == "nome.mov"
    assert sanitize_filename("‮​") == "video"


def test_filename_with_bidi_is_stored_clean(make):
    h = make()
    response = _upload(h, FAKE_VIDEO, name="vid‮4pm.exe")
    assert response.status_code == 200, response.text
    assert response.json()["media"]["original_filename"] == "vid4pm.exe"


def test_chunked_body_with_extra_parts_and_epilogue_is_capped(make):
    h = make(max_download_bytes=1000)
    from app.services.upload_service import MULTIPART_OVERHEAD

    filler = b"--BND\r\nContent-Disposition: form-data; name=\"x\"\r\n\r\n\r\n"
    file_part = b"--BND\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.mp4\"\r\n\r\n" + FAKE_VIDEO[:900]

    def body():
        # Sem Content-Length (gerador): partes vazias em quantidade e epílogo grande.
        for _ in range(MULTIPART_OVERHEAD // len(filler) + 10):
            yield filler
        yield file_part + b"\r\n--BND--\r\n"
        yield b"E" * (MULTIPART_OVERHEAD * 2)

    response = h.client.post(URL, content=body(), headers={"Content-Type": "multipart/form-data; boundary=BND"})
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"
    assert _upload_temps(h) == [] and _media_count(h) == 0


def test_only_exact_upload_path_is_exempt(make):
    h = make()
    big = b"x" * (1024 * 1024 + 10)
    for path in (URL + "/", URL + "/x", URL + "x"):
        response = h.client.post(path, content=big)
        assert response.status_code == 413, path
        assert response.json()["detail"] == "Corpo da requisição maior que o limite de 1 MB."


# --------------------------------------------------------------------------- revisão 2: falso positivo e disfarce


def _x264(path: Path, params: str, seconds: int = 12) -> bytes:
    _ff("-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=30:duration={seconds}", "-f", "lavfi", "-i",
        f"sine=frequency=440:sample_rate=48000:duration={seconds}", "-c:v", "libx264", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", "-x264-params", params, "-c:a", "aac", str(path))
    return path.read_bytes()


@requires_ffmpeg
@pytest.mark.parametrize("params", ["open_gop=1:keyint=60", "intra-refresh=1:keyint=60"])
def test_upload_open_gop_and_intra_refresh_are_accepted(make, tmp_path, params):
    """O salto para o fim cai fora de um IDR e o decodificador avisa ("mmco: unref short failure"...), mas o vídeo
    decodifica inteiro sem erro: não pode ser recusado."""
    h = make(processor=MediaProcessor())
    response = _upload(h, _x264(tmp_path / "v.mp4", params), name="gop.mp4")
    assert response.status_code == 200, response.text
    assert response.json()["media"]["duration"] == pytest.approx(12.0, abs=0.1)
    assert _upload_temps(h) == []


@requires_ffmpeg
def test_upload_truncated_open_gop_is_still_invalid(make, tmp_path):
    h = make(processor=MediaProcessor())
    data = _x264(tmp_path / "v.mp4", "open_gop=1:keyint=60")
    _assert_rejected(h, _upload(h, data[: int(len(data) * 0.7)], name="gop.mp4"), "invalid_media")


@requires_ffmpeg
def test_upload_container_disguised_by_extension_is_refused(make, tmp_path):
    h = make(processor=MediaProcessor())
    src = tmp_path / "v.mkv"
    _ff("-f", "lavfi", "-i", "testsrc=size=64x48:rate=10:duration=2", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", str(src))
    response = _upload(h, src.read_bytes(), name="disfarce.mp4")
    _assert_rejected(h, response, "unsupported_media")
    assert ".mp4" in response.json()["detail"]


def test_integrity_errors_ignores_only_seek_reference_warnings():
    from app.services.upload_service import integrity_errors

    benign = (
        "[h264 @ 000001] mmco: unref short failure\n"
        "[h264 @ 000001] number of reference frames (0+4) exceeds max (3; probably corrupt input), discarding one\n"
        "    Last message repeated 3 times\n"
        "[hevc @ 000002] Could not find ref with POC 12\n"
    )
    assert integrity_errors(benign) == []
    bad = (
        "[h264 @ 000001] mmco: unref short failure\n"
        "[in#0/mov,mp4,m4a,3gp,3g2,mj2 @ 000003] stream 1, offset 0xa08c1: partial file\n"
        "[h264 @ 000001] Invalid NAL unit size (0 > 575).\n"
        "    Last message repeated 2 times\n"
        "[aist#0:1/aac @ 1] [dec:aac @ 2] Error submitting packet to decoder: Invalid data found\n"
    )
    assert integrity_errors(bad) == [
        "stream 1, offset 0xa08c1: partial file",
        "Invalid NAL unit size (0 > 575).",
        "Last message repeated 2 times",
        "Error submitting packet to decoder: Invalid data found",
    ]


@requires_ffmpeg
def test_integrity_timeout_has_its_own_code(make, tmp_path, monkeypatch):
    import app.services.upload_service as upload_service

    h = make(processor=MediaProcessor())
    monkeypatch.setattr(upload_service, "INTEGRITY_TIMEOUT", 0.001)
    response = _upload(h, _faststart_mp4(tmp_path / "ok.mp4"), name="ok.mp4")
    assert response.status_code == 422, response.text
    assert response.json() == {
        "detail": "Não foi possível verificar o arquivo a tempo. Tente um arquivo menor ou com mais quadros-chave.",
        "code": "integrity_timeout",
    }
    assert _media_count(h) == 0 and _originals(h) == [] and _upload_temps(h) == []
    assert h.app.state.upload_slots._active == 0
