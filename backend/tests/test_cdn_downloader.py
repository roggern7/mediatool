import socket
import threading

import httpx
import pytest

from app.services import cdn_downloader as cdn
from app.services.cdn_downloader import DownloadError, DownloadResult, cdn_download, validate_cdn_url

GOOD_URL = "https://scontent-gru1-1.cdninstagram.com/v/t51/abc.mp4?oe=1"
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 200
MOV = b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00" + b"\x00" * 100
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 100
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 100
WEBM = b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x84webm" + b"\x00" * 100


def _public_dns(host, port, *args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("157.240.1.1", port))]


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    monkeypatch.setattr(cdn.socket, "getaddrinfo", _public_dns)

    def forbid_client(timeout):  # pragma: no cover - só dispara se um teste esquecer o transport
        raise AssertionError("rede real não permitida nos testes")

    monkeypatch.setattr(cdn, "_build_client", forbid_client)


def _install(monkeypatch, handler):
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    def build(timeout):
        return httpx.Client(transport=httpx.MockTransport(wrapped), follow_redirects=False, trust_env=False)

    monkeypatch.setattr(cdn, "_build_client", build)
    return seen


def _files(path):
    return sorted(p.name for p in path.iterdir()) if path.exists() else []


# --------------------------------------------------------------------------- validate_cdn_url


@pytest.mark.parametrize(
    "url",
    [
        GOOD_URL,
        "https://instagram.fgru5-1.fna.fbcdn.net/v/t51.2885-15/x.jpg?stp=1",
        "https://SCONTENT.cdninstagram.com:443/x.jpg",
    ],
)
def test_validate_accepts(url):
    assert validate_cdn_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "", "file:///C:/Windows/win.ini", "http://scontent.cdninstagram.com/x.jpg",
        "https://scontent.cdninstagram.com:8443/x.jpg", "https://scontent.cdninstagram.com:80/x.jpg",
        "https://evil.com/x.jpg", "https://cdninstagram.com/x.jpg", "https://.cdninstagram.com/x.jpg",
        "https://scontent.cdninstagram.com.evil.com/x.jpg", "https://evilcdninstagram.com/x.jpg",
        "https://user:pass@scontent.cdninstagram.com/x.jpg", "https://user@scontent.cdninstagram.com/x.jpg",
        "https://157.240.1.1/x.jpg", "https://[::1]/x.jpg", "https://127.0.0.1/x.jpg",
        "https://scontent.cdninstagram.com./x.jpg", "https://scontent.cdninstagram.com/x y.jpg",
        "https://scontent.cdninstagram.com\\@evil.com/x", "ftp://scontent.cdninstagram.com/x",
        "https://scontent.cdninstagram.com:abc/x", "https://xn--evil-.fbcdn.net/x",
        "javascript:alert(1)",
    ],
)
def test_validate_rejects(url):
    with pytest.raises(DownloadError) as info:
        validate_cdn_url(url)
    assert info.value.code == "invalid_url"


# --------------------------------------------------------------------------- DNS


@pytest.mark.parametrize(
    "addr,family",
    [("127.0.0.1", socket.AF_INET), ("10.1.2.3", socket.AF_INET), ("192.168.0.10", socket.AF_INET),
     ("172.16.5.5", socket.AF_INET), ("169.254.169.254", socket.AF_INET), ("0.0.0.0", socket.AF_INET),
     ("100.64.0.1", socket.AF_INET), ("224.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6),
     ("fe80::1%3", socket.AF_INET6), ("fc00::1", socket.AF_INET6), ("::ffff:127.0.0.1", socket.AF_INET6)],
)
def test_dns_to_non_public_rejected(monkeypatch, tmp_path, addr, family):
    def resolver(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("157.240.1.1", port)),
                (family, socket.SOCK_STREAM, 6, "", (addr, port))]

    monkeypatch.setattr(cdn.socket, "getaddrinfo", resolver)
    seen = _install(monkeypatch, lambda r: httpx.Response(200, content=JPEG))
    with pytest.raises(DownloadError) as info:
        cdn_download(GOOD_URL, tmp_path, max_bytes=10_000)
    assert info.value.code == "invalid_url"
    assert seen == []
    assert _files(tmp_path) == []


def test_dns_failure_is_network_error(monkeypatch, tmp_path):
    def resolver(*a, **k):
        raise socket.gaierror("falhou")

    monkeypatch.setattr(cdn.socket, "getaddrinfo", resolver)
    _install(monkeypatch, lambda r: httpx.Response(200, content=JPEG))
    with pytest.raises(DownloadError) as info:
        cdn_download(GOOD_URL, tmp_path, max_bytes=10_000)
    assert info.value.code == "network_error"


# --------------------------------------------------------------------------- download


@pytest.mark.parametrize(
    "content,suffix,mime",
    [(MP4, ".mp4", "video/mp4"), (MOV, ".mov", "video/quicktime"), (HEIC, ".heic", "image/heic"),
     (JPEG, ".jpg", "image/jpeg"), (PNG, ".png", "image/png"), (WEBP, ".webp", "image/webp"),
     (WEBM, ".webm", "video/webm")],
)
def test_download_success_suffix_from_content(monkeypatch, tmp_path, content, suffix, mime):
    _install(monkeypatch, lambda r: httpx.Response(200, content=content, headers={"content-type": "text/html"}))
    calls = []
    url = "https://scontent.cdninstagram.com/v/evil.exe?name=payload.bat"
    result = cdn_download(url, tmp_path / "dest", max_bytes=10_000, progress=lambda d, t: calls.append((d, t)))
    assert isinstance(result, DownloadResult)
    assert result.suffix == suffix
    assert result.content_type == mime
    assert result.path.suffix == suffix
    assert result.path.parent == tmp_path / "dest"
    assert result.path.read_bytes() == content
    assert result.size == len(content)
    assert calls and calls[-1] == (len(content), len(content))
    assert _files(tmp_path / "dest") == [result.path.name]


def test_no_cookies_sent_and_redirect_cookies_dropped(monkeypatch, tmp_path):
    def handler(request):
        if request.url.path == "/first":
            return httpx.Response(302, headers={"location": "https://other.fbcdn.net/second",
                                                "set-cookie": "sessionid=FAKE-SESSION-0000000000; Path=/"})
        return httpx.Response(200, content=JPEG)

    seen = _install(monkeypatch, handler)
    result = cdn_download("https://scontent.cdninstagram.com/first", tmp_path, max_bytes=10_000)
    assert result.suffix == ".jpg"
    assert [str(r.url) for r in seen] == ["https://scontent.cdninstagram.com/first", "https://other.fbcdn.net/second"]
    for r in seen:
        assert "cookie" not in r.headers
        assert "authorization" not in r.headers


@pytest.mark.parametrize(
    "location",
    ["https://evil.com/x.jpg", "http://scontent.cdninstagram.com/x.jpg", "https://127.0.0.1/x",
     "https://scontent.cdninstagram.com:8443/x", "file:///C:/Windows/win.ini",
     "https://user:pw@scontent.cdninstagram.com/x"],
)
def test_redirect_to_forbidden_target(monkeypatch, tmp_path, location):
    seen = _install(monkeypatch, lambda r: httpx.Response(302, headers={"location": location}))
    with pytest.raises(DownloadError) as info:
        cdn_download(GOOD_URL, tmp_path, max_bytes=10_000)
    assert info.value.code == "invalid_url"
    assert len(seen) == 1
    assert _files(tmp_path) == []


def test_redirect_to_host_resolving_private(monkeypatch, tmp_path):
    def resolver(host, port, *a, **k):
        ip = "10.0.0.5" if host == "internal.fbcdn.net" else "157.240.1.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(cdn.socket, "getaddrinfo", resolver)
    seen = _install(monkeypatch, lambda r: httpx.Response(302, headers={"location": "https://internal.fbcdn.net/x"}))
    with pytest.raises(DownloadError) as info:
        cdn_download(GOOD_URL, tmp_path, max_bytes=10_000)
    assert info.value.code == "invalid_url"
    assert len(seen) == 1


def test_too_many_redirects(monkeypatch, tmp_path):
    seen = _install(monkeypatch, lambda r: httpx.Response(302, headers={"location": GOOD_URL}))
    with pytest.raises(DownloadError) as info:
        cdn_download(GOOD_URL, tmp_path, max_bytes=10_000)
    assert info.value.code == "invalid_url"
    assert len(seen) == cdn.MAX_REDIRECTS + 1


@pytest.mark.parametrize("status,code", [(401, "forbidden"), (403, "forbidden"), (404, "not_found"),
                                         (410, "not_found"), (500, "network_error"), (429, "network_error")])
def test_status_mapping(monkeypatch, tmp_path, status, code):
    _install(monkeypatch, lambda r: httpx.Response(status, content=b"erro"))
    with pytest.raises(DownloadError) as info:
        cdn_download(GOOD_URL, tmp_path, max_bytes=10_000)
    assert info.value.code == code
    assert _files(tmp_path) == []


def test_size_limit_by_content_length(monkeypatch, tmp_path):
    _install(monkeypatch, lambda r: httpx.Response(200, content=JPEG + b"\x00" * 5000))
    with pytest.raises(DownloadError) as info:
        cdn_download(GOOD_URL, tmp_path, max_bytes=1000)
    assert info.value.code == "too_large"
    assert _files(tmp_path) == []


def test_size_limit_while_streaming_without_length(monkeypatch, tmp_path):
    def chunks():
        yield JPEG
        for _ in range(20):
            yield b"\x00" * 100

    _install(monkeypatch, lambda r: httpx.Response(200, content=chunks()))
    with pytest.raises(DownloadError) as info:
        cdn_download(GOOD_URL, tmp_path, max_bytes=1000)
    assert info.value.code == "too_large"
    assert _files(tmp_path) == []


@pytest.mark.parametrize("content", [b"<html>not media</html>", b"MZ\x90\x00" + b"\x00" * 100, b"\x00" * 50])
def test_invalid_magic_bytes(monkeypatch, tmp_path, content):
    _install(monkeypatch, lambda r: httpx.Response(200, content=content, headers={"content-type": "video/mp4"}))
    with pytest.raises(DownloadError) as info:
        cdn_download(GOOD_URL, tmp_path, max_bytes=10_000)
    assert info.value.code == "invalid_media"
    assert _files(tmp_path) == []


def test_empty_body_is_invalid_media(monkeypatch, tmp_path):
    _install(monkeypatch, lambda r: httpx.Response(200, content=b""))
    with pytest.raises(DownloadError) as info:
        cdn_download(GOOD_URL, tmp_path, max_bytes=10_000)
    assert info.value.code == "invalid_media"


def test_cancel_before_and_during(monkeypatch, tmp_path):
    seen = _install(monkeypatch, lambda r: httpx.Response(200, content=JPEG))
    ev = threading.Event()
    ev.set()
    with pytest.raises(DownloadError) as info:
        cdn_download(GOOD_URL, tmp_path, max_bytes=10_000, cancel=ev)
    assert info.value.code == "cancelled"
    assert seen == []

    ev2 = threading.Event()

    def chunks():
        yield JPEG
        ev2.set()
        yield b"\x00" * 100

    _install(monkeypatch, lambda r: httpx.Response(200, content=chunks()))
    with pytest.raises(DownloadError) as info:
        cdn_download(GOOD_URL, tmp_path, max_bytes=10_000, cancel=ev2)
    assert info.value.code == "cancelled"
    assert _files(tmp_path) == []


def test_transport_error_and_timeout(monkeypatch, tmp_path):
    def raise_connect(request):
        raise httpx.ConnectError("falhou", request=request)

    _install(monkeypatch, raise_connect)
    with pytest.raises(DownloadError) as info:
        cdn_download(GOOD_URL, tmp_path, max_bytes=10_000)
    assert info.value.code == "network_error"

    def raise_timeout(request):
        raise httpx.ReadTimeout("lento", request=request)

    _install(monkeypatch, raise_timeout)
    with pytest.raises(DownloadError) as info:
        cdn_download(GOOD_URL, tmp_path, max_bytes=10_000)
    assert info.value.code == "network_error"
    assert _files(tmp_path) == []


def test_progress_exception_cleans_part(monkeypatch, tmp_path):
    _install(monkeypatch, lambda r: httpx.Response(200, content=JPEG))

    def bad_progress(done, total):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        cdn_download(GOOD_URL, tmp_path, max_bytes=10_000, progress=bad_progress)
    assert _files(tmp_path) == []


def test_truncated_body_is_network_error(monkeypatch, tmp_path):
    _install(monkeypatch, lambda r: httpx.Response(200, content=iter([JPEG]),
                                                    headers={"content-length": str(len(JPEG) + 50)}))
    with pytest.raises(DownloadError) as info:
        cdn_download(GOOD_URL, tmp_path, max_bytes=10_000)
    assert info.value.code == "network_error"
    assert _files(tmp_path) == []


def test_build_client_settings(monkeypatch):
    monkeypatch.undo()
    c = cdn._build_client(5)
    try:
        assert c.follow_redirects is False
        assert c.trust_env is False
        assert len(c.cookies) == 0
    finally:
        c.close()
