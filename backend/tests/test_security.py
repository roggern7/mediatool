import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.security import CLIENT_HEADER, UnsafePathError, ensure_within, install_security, safe_join

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
FAKE_SECRET = "FAKE-SESSION-0000000000"


def _make_app(**kwargs):
    app = FastAPI()

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/things")
    def create():
        return {"ok": True}

    @app.delete("/api/things/1")
    def delete():
        return {"ok": True}

    @app.get("/api/boom")
    def boom():
        raise RuntimeError(f"explodiu com sessionid={FAKE_SECRET}")

    install_security(app, port=PORT, **kwargs)
    return app


@pytest.fixture()
def client():
    return TestClient(_make_app(), base_url=BASE, raise_server_exceptions=False)


# --------------------------------------------------------------------------- Host


def test_allowed_hosts(client):
    assert client.get("/api/health").status_code == 200
    r = client.get("/api/health", headers={"Host": f"localhost:{PORT}"})
    assert r.status_code == 200


@pytest.mark.parametrize(
    "host", ["evil.com", f"evil.com:{PORT}", "127.0.0.1", "127.0.0.1:9999", "localhost", f"0.0.0.0:{PORT}",
             "testserver", f"127.0.0.1.nip.io:{PORT}", ""],
)
def test_rejected_hosts(client, host):
    r = client.get("/api/health", headers={"Host": host})
    assert r.status_code == 400
    assert r.json() == {"detail": "Host não permitido.", "code": "invalid_host"}
    assert r.headers["x-content-type-options"] == "nosniff"


def test_testserver_only_with_flag():
    plain = TestClient(_make_app())
    assert plain.get("/api/health").status_code == 400
    flagged = TestClient(_make_app(allow_testserver=True))
    assert flagged.get("/api/health").status_code == 200


def test_extra_hosts():
    c = TestClient(_make_app(extra_hosts=("media.local:8765",)), base_url="http://media.local:8765")
    assert c.get("/api/health").status_code == 200


# --------------------------------------------------------------------------- mutações


def test_mutation_requires_client_header(client):
    r = client.post("/api/things")
    assert r.status_code == 403
    assert r.json()["code"] == "client_header_required"
    assert set(r.json()) == {"detail", "code"}
    r = client.post("/api/things", headers={CLIENT_HEADER: "   "})
    assert r.status_code == 403
    r = client.delete("/api/things/1")
    assert r.status_code == 403
    assert client.post("/api/things", headers={CLIENT_HEADER: "panel"}).status_code == 200
    assert client.delete("/api/things/1", headers={CLIENT_HEADER: "panel"}).status_code == 200


def test_get_does_not_require_client_header(client):
    assert client.get("/api/health").status_code == 200


@pytest.mark.parametrize(
    "origin", ["http://evil.com", "null", "http://127.0.0.1:5174", "https://127.0.0.1:5173", "http://localhost"],
)
def test_mutation_from_disallowed_origin(client, origin):
    r = client.post("/api/things", headers={CLIENT_HEADER: "panel", "Origin": origin})
    assert r.status_code == 403
    assert r.json()["code"] == "origin_not_allowed"
    assert "access-control-allow-origin" not in r.headers


@pytest.mark.parametrize(
    "origin", ["http://127.0.0.1:5173", "http://localhost:5173", f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"],
)
def test_mutation_from_allowed_origin(client, origin):
    r = client.post("/api/things", headers={CLIENT_HEADER: "panel", "Origin": origin})
    assert r.status_code == 200


# --------------------------------------------------------------------------- CORS


def test_cors_preflight_allowed(client):
    r = client.options(
        "/api/things",
        headers={"Origin": "http://127.0.0.1:5173", "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": f"{CLIENT_HEADER}, content-type"},
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    assert "access-control-allow-credentials" not in r.headers


def test_cors_preflight_disallowed_origin(client):
    r = client.options(
        "/api/things",
        headers={"Origin": "http://evil.com", "Access-Control-Request-Method": "POST"},
    )
    assert r.status_code == 400
    assert "access-control-allow-origin" not in r.headers


def test_cors_preflight_disallowed_header(client):
    r = client.options(
        "/api/things",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": "x-evil"},
    )
    assert r.status_code == 400


def test_cors_simple_get_from_disallowed_origin_has_no_acao(client):
    r = client.get("/api/health", headers={"Origin": "http://evil.com"})
    assert "access-control-allow-origin" not in r.headers


def test_cors_simple_get_allowed_origin(client):
    r = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"


# --------------------------------------------------------------------------- headers e erros


def test_security_headers(client):
    r = client.get("/api/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-frame-options"] == "DENY"


def test_unhandled_exception_does_not_leak(client):
    r = client.get("/api/boom")
    assert r.status_code == 500
    assert r.json() == {"detail": "Erro interno.", "code": "internal_error"}
    assert FAKE_SECRET not in r.text
    assert "Traceback" not in r.text
    assert "RuntimeError" not in r.text


# --------------------------------------------------------------------------- safe_join / ensure_within


def test_safe_join_valid(tmp_path):
    root = tmp_path / "storage"
    root.mkdir()
    assert safe_join(root, "originals/2026/10/ab/file.mp4") == (root / "originals/2026/10/ab/file.mp4").resolve()
    assert safe_join(root, "originals\\2026\\x.jpg") == (root / "originals" / "2026" / "x.jpg").resolve()
    assert safe_join(root, "./a/./b.png") == (root / "a" / "b.png").resolve()
    assert safe_join(root, "conexao/console.mp4").name == "console.mp4"


@pytest.mark.parametrize(
    "rel",
    [
        "", "../x", "..\\x", "a/../../x", "a\\..\\..\\x", "..", "a/..", "/etc/passwd", "\\Windows\\win.ini",
        "C:\\Windows\\win.ini", "C:/Windows", "c:relative", "\\\\server\\share\\x", "//server/share/x",
        "file.mp4:stream", "file.mp4::$DATA", "a/b:c/d", "CON", "con.txt", "a/NUL", "nul.mp4", "COM1.jpg",
        "LPT9", "AUX.tar.gz", "prn", "a/b.", "a/b ", "a\x00b", "a/b?c", "a/b*c", "a<b", "a|b", 'a"b',
    ],
)
def test_safe_join_rejects(tmp_path, rel):
    with pytest.raises(UnsafePathError):
        safe_join(tmp_path, rel)


def test_safe_join_rejects_non_string(tmp_path):
    with pytest.raises(UnsafePathError):
        safe_join(tmp_path, None)  # type: ignore[arg-type]


@pytest.mark.skipif(os.name != "nt", reason="junção NTFS")
def test_safe_join_rejects_junction_escape(tmp_path):
    import subprocess

    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "link"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True,
                   shell=False)
    with pytest.raises(UnsafePathError):
        safe_join(root, "link/secret.txt")


def test_ensure_within(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert ensure_within(a / "x" / "y.mp4", [b, a]) == (a / "x" / "y.mp4").resolve()
    assert ensure_within(a, [a]) == a.resolve()
    with pytest.raises(UnsafePathError):
        ensure_within(tmp_path / "c" / "z", [a, b])
    with pytest.raises(UnsafePathError):
        ensure_within(a / ".." / "b2", [a])
    with pytest.raises(UnsafePathError):
        ensure_within(Path(str(a) + "-sibling") / "f", [a])


# --------------------------------------------------------------------------- CSP e origens configuráveis


def test_csp_on_regular_responses(client):
    from app.core.security import CONTENT_SECURITY_POLICY

    for r in (client.get("/api/health"), client.post("/api/things"), client.get("/naoexiste")):
        assert r.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert "frame-ancestors 'none'" in CONTENT_SECURITY_POLICY
    assert "script-src 'self';" in CONTENT_SECURITY_POLICY


def test_csp_absent_on_api_docs(client):
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "content-security-policy" not in r.headers, path
        assert r.headers["x-content-type-options"] == "nosniff"


def test_mutation_origin_uses_configured_cors_origins():
    c = TestClient(_make_app(cors_origins=()), base_url=BASE)
    for origin in ("http://127.0.0.1:5173", "http://localhost:5173"):
        r = c.post("/api/things", headers={CLIENT_HEADER: "panel", "Origin": origin})
        assert r.status_code == 403
        assert r.json()["code"] == "origin_not_allowed"
        pre = c.options("/api/things", headers={"Origin": origin, "Access-Control-Request-Method": "POST"})
        assert "access-control-allow-origin" not in pre.headers
    for origin in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
        assert c.post("/api/things", headers={CLIENT_HEADER: "panel", "Origin": origin}).status_code == 200
    custom = TestClient(_make_app(cors_origins=("http://127.0.0.1:4000",)), base_url=BASE)
    ok = custom.post("/api/things", headers={CLIENT_HEADER: "panel", "Origin": "http://127.0.0.1:4000"})
    assert ok.status_code == 200
    bad = custom.post("/api/things", headers={CLIENT_HEADER: "panel", "Origin": "http://127.0.0.1:5173"})
    assert bad.status_code == 403
