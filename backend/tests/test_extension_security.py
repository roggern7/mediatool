"""Extensão do Chrome: pareamento, token, origem, CORS e segredos fora dos logs (docs/EXTENSION.md)."""

from __future__ import annotations

import hashlib
import json
import logging

import pytest

from app.core.security import CLIENT_HEADER
from app.services import extension_auth

EXT_ORIGIN = "chrome-extension://lnpealhnkmjaohdlkpapjmdealokkdbf"   # ID fixo da nossa extensão
APP_ORIGIN = "http://127.0.0.1:8765"


def ext_headers(token: str | None = None, *, origin: str = EXT_ORIGIN, bearer: bool = True) -> dict[str, str]:
    headers = {"Origin": origin, CLIENT_HEADER: "extension"}
    if token is not None:
        headers.update({"Authorization": f"Bearer {token}"} if bearer else {"X-Media-Tool-Token": token})
    return headers


def new_code(harness) -> str:
    response = harness.client.post("/api/settings/extension/pairing-code")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()["code"]


def pair(harness, code: str, name: str = "Chrome do escritório"):
    return harness.client.post("/api/ext/pair", json={"code": code, "device_name": name}, headers=ext_headers())


def paired_token(harness) -> tuple[str, str]:
    response = pair(harness, new_code(harness))
    assert response.status_code == 200, response.text
    body = response.json()
    return body["token"], body["device_id"]


# --------------------------------------------------------------------------- pareamento


def test_pairing_code_format_and_only_hash_stored(harness):
    response = harness.client.post("/api/settings/extension/pairing-code")
    body = response.json()
    code = body["code"]
    assert len(code) == 8 and all(ch in extension_auth.CODE_ALPHABET for ch in code)
    assert body["ttl_seconds"] == 300
    raw = harness.ctx.app_settings.get_state(extension_auth.PAIRING_KEY)
    assert code not in raw
    assert json.loads(raw)["code_hash"] == hashlib.sha256(code.encode()).hexdigest()


def test_pair_with_right_code_returns_token_once_and_stores_only_hash(harness):
    code = new_code(harness)
    response = pair(harness, code.lower()[:4] + "-" + code.lower()[4:])  # aceita minúsculas e hífen
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    token = response.json()["token"]
    assert len(token) >= 43  # 32 bytes em base64url
    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    raw = harness.ctx.app_settings.get_state(extension_auth.DEVICES_KEY)
    assert token not in raw
    assert json.loads(raw)[0]["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    devices = harness.client.get("/api/settings/extension/devices").json()["items"]
    assert [d["name"] for d in devices] == ["Chrome do escritório"]
    assert set(devices[0]) == {"id", "name", "created_at", "last_used_at"}
    assert token not in json.dumps(devices)


def test_pair_code_is_single_use(harness):
    code = new_code(harness)
    assert pair(harness, code).status_code == 200
    again = pair(harness, code)
    assert again.status_code == 400
    assert again.json()["code"] == "pairing_code_expired"


def test_pair_wrong_code(harness):
    new_code(harness)
    response = pair(harness, "ZZZZZZZZ")
    assert response.status_code == 400
    assert response.json()["code"] == "pairing_code_invalid"


def test_pair_expired_code(harness, monkeypatch):
    code = new_code(harness)
    state = json.loads(harness.ctx.app_settings.get_state(extension_auth.PAIRING_KEY))
    state["expires_at"] = "2000-01-01T00:00:00Z"
    harness.ctx.app_settings.set_state(extension_auth.PAIRING_KEY, json.dumps(state))
    response = pair(harness, code)
    assert response.status_code == 400
    assert response.json()["code"] == "pairing_code_expired"
    assert harness.client.get("/api/settings/extension/devices").json()["items"] == []


def test_pair_locks_after_too_many_wrong_attempts(harness):
    code = new_code(harness)
    for _ in range(extension_auth.MAX_FAILED_ATTEMPTS - 1):
        assert pair(harness, "WRONGWRG").status_code == 400
    locked = pair(harness, "WRONGWRG")
    assert locked.status_code == 429
    assert locked.json()["code"] == "pairing_locked"
    # Bloqueado: nem o código certo vale (e ele foi invalidado).
    assert pair(harness, code).status_code == 429
    # Gerar um código novo no app libera o bloqueio.
    assert pair(harness, new_code(harness)).status_code == 200


def test_attempts_without_active_code_do_not_count_for_the_lock(harness):
    # Nenhum código gerado: tentativas além do limite nunca bloqueiam.
    for _ in range(extension_auth.MAX_FAILED_ATTEMPTS + 2):
        response = pair(harness, "WRONGWRG")
        assert response.status_code == 400 and response.json()["code"] == "pairing_code_expired"
    # Código usado (não há mais código ativo): idem.
    assert pair(harness, new_code(harness)).status_code == 200
    for _ in range(extension_auth.MAX_FAILED_ATTEMPTS + 2):
        assert pair(harness, "WRONGWRG").status_code == 400
    state = json.loads(harness.ctx.app_settings.get_state(extension_auth.PAIRING_KEY))
    assert state["failed"] == 0 and state["locked_until"] is None
    # Com código ativo o erro volta a contar e o certo ainda vale.
    assert pair(harness, new_code(harness)).status_code == 200


def test_pairing_code_status_pending_used_and_expired(harness):
    first = harness.client.post("/api/settings/extension/pairing-code").json()
    url = f"/api/settings/extension/pairing-code/{first['id']}"
    status = harness.client.get(url)
    assert status.status_code == 200 and status.headers["cache-control"] == "no-store"
    assert status.json() == {"status": "pending", "device_id": None}
    assert first["code"] not in status.text
    # Um código novo substitui o anterior: o anterior passa a "expired".
    second = harness.client.post("/api/settings/extension/pairing-code").json()
    assert harness.client.get(url).json()["status"] == "expired"
    paired = pair(harness, second["code"]).json()
    used = harness.client.get(f"/api/settings/extension/pairing-code/{second['id']}").json()
    assert used == {"status": "used", "device_id": paired["device_id"]}
    # Desconhecido: "expired"; formato inválido: 422; a extensão não consulta (403 fora de /api/ext/*).
    assert harness.client.get("/api/settings/extension/pairing-code/0123456789abcdef").json()["status"] == "expired"
    assert harness.client.get("/api/settings/extension/pairing-code/xyz").status_code == 422
    assert harness.client.get(url, headers={"Origin": EXT_ORIGIN}).status_code == 403


def test_pair_validates_body(harness):
    response = harness.client.post("/api/ext/pair", json={"code": "ABCDEFGH", "device_name": ""},
                                   headers=ext_headers())
    assert response.status_code == 422
    response = harness.client.post("/api/ext/pair", json={"code": "ABCDEFGH", "device_name": "x", "extra": 1},
                                   headers=ext_headers())
    assert response.status_code == 422


# --------------------------------------------------------------------------- token


@pytest.mark.parametrize("bearer", [True, False])
def test_valid_token_both_headers(harness, bearer):
    token, _ = paired_token(harness)
    response = harness.client.get("/api/ext/status", headers=ext_headers(token, bearer=bearer))
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert token not in response.text


@pytest.mark.parametrize("token", [None, "", "x", "Bearer", "a" * 43])
def test_missing_or_invalid_token_is_401(harness, token):
    paired_token(harness)
    headers = {"Origin": EXT_ORIGIN}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    for method, path, body in (("GET", "/api/ext/status", None), ("POST", "/api/ext/page-info", {"url": "x"}),
                               ("POST", "/api/ext/start", {"inputs": ["perfil"]}),
                               ("GET", "/api/ext/progress?batch_ids=1", None)):
        response = harness.client.request(method, path, json=body, headers={**headers, CLIENT_HEADER: "ext"})
        assert response.status_code == 401, (path, response.text)
        assert response.json()["code"] == "extension_unauthorized"


def test_revoked_token_gets_401_immediately(harness):
    token, device_id = paired_token(harness)
    assert harness.client.get("/api/ext/status", headers=ext_headers(token)).status_code == 200
    revoked = harness.client.delete(f"/api/settings/extension/devices/{device_id}")
    assert revoked.status_code == 200
    assert revoked.json()["items"] == []
    assert harness.client.get("/api/ext/status", headers=ext_headers(token)).status_code == 401
    assert harness.client.delete(f"/api/settings/extension/devices/{device_id}").status_code == 404
    assert harness.client.delete("/api/settings/extension/devices/NAO-E-ID").status_code == 422


def test_last_used_is_recorded(harness):
    token, _ = paired_token(harness)
    harness.client.get("/api/ext/status", headers=ext_headers(token))
    devices = harness.client.get("/api/settings/extension/devices").json()["items"]
    assert devices[0]["last_used_at"] is not None


def test_token_comparison_is_constant_time(harness, monkeypatch):
    """Toda verificação passa por hmac.compare_digest e compara com todos os dispositivos (sem atalho)."""
    first, _ = paired_token(harness)
    paired_token(harness)
    calls: list[tuple[str, str]] = []
    real = extension_auth.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(extension_auth.hmac, "compare_digest", spy)
    assert extension_auth.verify_token(harness.ctx, first) is not None
    assert len(calls) == 2
    calls.clear()
    assert extension_auth.verify_token(harness.ctx, "inválido") is None
    assert len(calls) == 2


def test_token_and_code_never_logged(harness, caplog):
    caplog.set_level(logging.DEBUG)
    code = new_code(harness)
    token = pair(harness, code).json()["token"]
    harness.client.get("/api/ext/status", headers=ext_headers(token))
    harness.client.get("/api/ext/status", headers=ext_headers(token + "x"))
    harness.client.post("/api/ext/page-info", json={"url": "https://www.instagram.com/p/ABCDE12345/"},
                        headers=ext_headers(token))
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "pareada" in text
    assert token not in text and code not in text


# --------------------------------------------------------------------------- origem e CORS


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/settings"),
    ("GET", "/api/settings/extension/devices"),
    ("POST", "/api/settings/extension/pairing-code"),
    ("DELETE", "/api/settings/extension/devices/0123456789abcdef"),
    ("POST", "/api/imports/start"),
    ("POST", "/api/imports"),
    ("GET", "/api/batches"),
    ("OPTIONS", "/api/settings"),
])
def test_extension_origin_blocked_outside_ext_routes(harness, method, path):
    token, _ = paired_token(harness)
    response = harness.client.request(method, path, headers=ext_headers(token))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "origin_not_allowed"
    assert "access-control-allow-origin" not in response.headers


def test_management_routes_work_from_app(harness):
    assert harness.client.post("/api/settings/extension/pairing-code",
                               headers={"Origin": APP_ORIGIN}).status_code == 200
    assert harness.client.get("/api/settings/extension/devices").status_code == 200


@pytest.mark.parametrize("origin", ["http://evil.com", "chrome-extension://short", "chrome-extension://" + "z" * 32,
                                    "moz-extension://abcdefghijklmnopabcdefghijklmnop"])
def test_other_origins_blocked_on_ext_routes(harness, origin):
    token, _ = paired_token(harness)
    response = harness.client.get("/api/ext/status", headers=ext_headers(token, origin=origin))
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


OTHER_EXT_ORIGIN = "chrome-extension://" + "abcdefghijklmnopabcdefghijklmnop"


def test_only_our_extension_id_is_accepted(harness):
    token, _ = paired_token(harness)
    response = harness.client.get("/api/ext/status", headers=ext_headers(token, origin=OTHER_EXT_ORIGIN))
    assert response.status_code == 403 and response.json()["code"] == "origin_not_allowed"
    assert "access-control-allow-origin" not in response.headers
    preflight = harness.client.options("/api/ext/status", headers={"Origin": OTHER_EXT_ORIGIN,
                                                                   "Access-Control-Request-Method": "GET"})
    assert preflight.status_code == 403
    mutation = harness.client.post("/api/ext/page-info", json={"url": "x"},
                                   headers=ext_headers(token, origin=OTHER_EXT_ORIGIN))
    assert mutation.status_code == 403
    assert harness.client.get("/api/ext/status", headers=ext_headers(token)).status_code == 200


def test_extra_extension_ids_come_from_the_environment(harness_factory):
    extra = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    h = harness_factory(env={"MEDIA_TOOL_EXTENSION_IDS": f" {extra.upper()} , invalido,"})
    token, _ = paired_token(h)
    for origin in (EXT_ORIGIN, f"chrome-extension://{extra}"):
        response = h.client.get("/api/ext/status", headers=ext_headers(token, origin=origin))
        assert response.status_code == 200, (origin, response.text)
        assert response.headers["access-control-allow-origin"] == origin
    assert h.client.get("/api/ext/status", headers=ext_headers(token, origin=OTHER_EXT_ORIGIN)).status_code == 403
    # O ID extra continua barrado fora de /api/ext/*.
    assert h.client.get("/api/batches", headers=ext_headers(token, origin=f"chrome-extension://{extra}")
                        ).status_code == 403


def test_ext_preflight_and_cors_only_on_ext_routes(harness):
    preflight = harness.client.options("/api/ext/status", headers={
        "Origin": EXT_ORIGIN, "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization", "Access-Control-Request-Private-Network": "true"})
    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-origin"] == EXT_ORIGIN
    assert "Authorization" in preflight.headers["access-control-allow-headers"]
    assert preflight.headers["access-control-allow-private-network"] == "true"
    assert "access-control-allow-credentials" not in preflight.headers
    no_pna = harness.client.options("/api/ext/pair", headers={"Origin": EXT_ORIGIN,
                                                               "Access-Control-Request-Method": "POST"})
    assert no_pna.status_code == 204
    assert "access-control-allow-private-network" not in no_pna.headers
    token, _ = paired_token(harness)
    response = harness.client.get("/api/ext/status", headers=ext_headers(token))
    assert response.headers["access-control-allow-origin"] == EXT_ORIGIN
    # Fora de /api/ext/* nenhuma origem de extensão recebe CORS (preflight também é 403).
    outside = harness.client.options("/api/settings", headers={"Origin": EXT_ORIGIN,
                                                                "Access-Control-Request-Method": "GET"})
    assert outside.status_code == 403
    assert "access-control-allow-origin" not in outside.headers


def test_ext_mutation_still_requires_client_header(harness):
    token, _ = paired_token(harness)
    response = harness.client.post("/api/ext/page-info", json={"url": "x"},
                                   headers={"Origin": EXT_ORIGIN, "Authorization": f"Bearer {token}",
                                            CLIENT_HEADER: ""})
    assert response.status_code == 403
    assert response.json()["code"] == "client_header_required"


def test_host_still_local_only_on_ext_routes(harness):
    token, _ = paired_token(harness)
    response = harness.client.get("/api/ext/status", headers={**ext_headers(token), "Host": "evil.com"})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_host"
