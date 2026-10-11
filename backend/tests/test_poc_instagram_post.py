"""POC do documento público da publicação (tools/poc_instagram_post.py): offline, httpx.MockTransport, valores
FAKE, sem banco.

Nenhuma requisição real: a POC só roda contra o Instagram com autorização do dono.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from conftest import make_settings
from post_document_fixtures import data_sjs_script, envelope, media_node, media_payload, post_html, post_page

_SPEC = importlib.util.spec_from_file_location(
    "poc_instagram_post", Path(__file__).resolve().parents[1] / "tools" / "poc_instagram_post.py"
)
poc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(poc)

CODE = "FAKEPOSTIMG1"
OTHER = "FAKEPOSTREEL"
LINK = f"https://www.instagram.com/reel/{CODE}/?igsh=FAKEQUERY123"
SECRETS = (CODE, OTHER, "FAKEQUERY123", "FAKE_OH", "scontent-fake", "video-fake", "dono_fake", "Dono_Fake",
           "Legenda", "3200000000000000", "900000001", "adp_Polaris")


def html(body: str, status: int = 200, **headers) -> httpx.Response:
    return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8", **headers})


class Server:
    def __init__(self, *answers: httpx.Response | BaseException) -> None:
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "www.instagram.com" and request.method == "GET"
        if not self.answers:
            raise AssertionError("requisição inesperada")
        item = self.answers.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def run_tool(server: Server, url: str = LINK, *, running: bool = False, tmp_path: Path):
    settings = make_settings(tmp_path)
    code, report = poc.run(
        poc.parse_args(["--url", url]), settings=settings, transport=httpx.MockTransport(server.handle),
        is_app_running=lambda s: running,
    )
    text = json.dumps(report, ensure_ascii=True)
    for secret in SECRETS:
        assert secret not in text, secret
    assert "https://" not in text and "http://" not in text, "nenhuma URL no relatório"
    return code, report


def test_reel_report_and_exactly_one_anonymous_request(tmp_path):
    server = Server(html(post_page(media_node("video", CODE))))
    code, report = run_tool(server, tmp_path=tmp_path)
    assert code == 0 and report["outcome"] == "ok" and "error" not in report
    assert report["requests"] == [{"method": "GET", "path": "/reel/", "status": 200}]
    assert report["instagram_requests"] == 1 and report["cdn_requests"] == 0 and report["other_requests"] == 0
    assert report["session_used"] is False and report["cookies_sent"] == 0
    assert report["source"] == "public_document" and report["verified_at"] == "2026-10-10"
    assert report["link_kind"] == "reel" and report["media_count"] == 1 and report["media_types"] == ["video"]
    assert report["document"] == {"status": 200, "redirect_kind": None, "content_type": "text/html",
                                  "size": report["document"]["size"]}
    assert report["parse"] == {"scripts": 39, "json_scripts": 39, "envelopes": 1, "preloader_observed": True,
                               "outcome": "ok", "typename": "XIGPolarisVideoMedia"}
    assert report["node"] == {
        "media_type": 2, "product_type": "clips", "image_candidates": 3, "max_image_width": 640,
        "max_image_height": 1136, "video_versions": 3, "video_versions_have_size": False,
        "video_version_types": [101, 102, 103], "original_width": 1080, "original_height": 1920}
    assert report["items"] == [{
        "media_type": "video", "is_reel": True, "source_media_index": 0, "width": 1080, "height": 1920,
        "taken_at": "2023-11-14T22:13:20Z", "cdn_url_valid": True, "cdn_domain_suffix": ".fbcdn.net",
        "shortcode_matches": True, "owner_present": True, "thumbnail_present": True}]
    # a requisição enviada: UM GET do documento, sem cookies e sem query
    [request] = server.requests
    assert request.url.path == f"/reel/{CODE}/" and request.url.query == b""
    assert "cookie" not in request.headers and "referer" not in request.headers


def test_p_link_asks_the_p_path_and_reports_the_photo_shape(tmp_path):
    server = Server(html(post_page(media_node("photo", CODE))))
    code, report = run_tool(server, f"instagram.com/p/{CODE}/", tmp_path=tmp_path)
    assert code == 0 and report["link_kind"] == "p" and server.paths == [f"/p/{CODE}/"]
    assert report["requests"] == [{"method": "GET", "path": "/p/", "status": 200}]
    assert report["node"]["max_image_width"] == 1080 and report["node"]["video_versions"] == 0
    [item] = report["items"]
    assert item["media_type"] == "image" and item["cdn_domain_suffix"] == ".cdninstagram.com"
    assert (item["width"], item["height"]) == (1080, 1350)


def test_carousel_lists_every_item(tmp_path):
    server = Server(html(post_page(media_node("carousel", CODE))))
    code, report = run_tool(server, f"https://www.instagram.com/p/{CODE}/", tmp_path=tmp_path)
    assert code == 0 and report["media_count"] == 4 and report["media_types"] == ["image", "video"]
    assert [(i["source_media_index"], i["media_type"], i["is_reel"]) for i in report["items"]] == [
        (0, "image", False), (1, "video", False), (2, "image", False), (3, "video", False)]
    assert report["node"]["carousel_children"] == 4 and len(report["node"]["children"]) == 4
    assert report["node"]["children"][1]["video_versions_have_size"] is False


def test_video_versions_with_size_are_reported(tmp_path):
    node = media_node("video", CODE)
    for version in node["video_versions"]:
        version.update(width=720, height=1280)
    _, report = run_tool(Server(html(post_page(node))), tmp_path=tmp_path)
    assert report["node"]["video_versions_have_size"] is True
    assert (report["items"][0]["width"], report["items"][0]["height"]) == (720, 1280)


def test_item_with_url_outside_cdn_is_flagged(tmp_path):
    node = media_node("photo", CODE)
    node["image_versions2"]["candidates"] = [{"url": "https://evil.example.com/a.jpg", "width": 10, "height": 10}]
    _, report = run_tool(Server(html(post_page(node))), f"https://www.instagram.com/p/{CODE}/", tmp_path=tmp_path)
    assert report["items"][0]["cdn_url_valid"] is False and report["items"][0]["cdn_domain_suffix"] is None


def test_another_post_in_the_document_is_flagged_without_printing_codes(tmp_path):
    server = Server(html(post_page(media_node("video", OTHER))))
    code, report = run_tool(server, tmp_path=tmp_path)
    assert code == 1 and report["outcome"] == "contract_changed"
    assert report["error"]["detail"] == "post_mismatch" and report["parse"]["outcome"] == "mismatch"
    assert report["items"] == [] and report["media_count"] == 0 and report["node"] is None


def test_document_without_envelope(tmp_path):
    server = Server(html("<html><body>Entrar</body></html>"))
    code, report = run_tool(server, tmp_path=tmp_path)
    assert code == 1 and report["outcome"] == "contract_changed"
    assert report["error"]["detail"] == "post_envelope_missing" and report["error"]["stage"] == "post_document"
    assert report["parse"]["outcome"] == "no_scripts" and report["document"]["status"] == 200


def test_gated_post(tmp_path):
    body = post_html([data_sjs_script(media_payload(envelope(None, code=CODE, gating={"fake": "FAKE"})))])
    code, report = run_tool(Server(html(body)), tmp_path=tmp_path)
    assert code == 1 and report["outcome"] == "content_unavailable"
    assert report["error"]["detail"] == "post_gated_logged_out" and report["parse"]["outcome"] == "gated"


@pytest.mark.parametrize("failure, code, detail", [
    (lambda: html("", 429), "rate_limited", "http_429"),
    (lambda: httpx.Response(302, headers={"location": "https://www.instagram.com/challenge/FAKE/"}),
     "access_refused", "redirect_challenge"),
    (lambda: httpx.Response(302, headers={"location": f"https://www.instagram.com/accounts/login/?next=/reel/{CODE}/"}),
     "access_refused", "redirect_login"),
    (lambda: html("<html>Forbidden</html>", 403), "access_refused", "http_403"),
    (lambda: html("<html>404</html>", 404), "content_unavailable", "post_http_404"),
    (lambda: httpx.Response(200, json={"data": None}), "contract_changed", "document_not_html"),
    (lambda: html("", 503), "network_error", "http_503"),
    (lambda: httpx.ConnectError("falha FAKE"), "network_error", None),
    (lambda: RuntimeError("falha FAKE"), "internal_error", None),
])
def test_error_stops_immediately_without_retry_or_redirect(tmp_path, failure, code, detail):
    server = Server(failure())
    exit_code, report = run_tool(server, tmp_path=tmp_path)
    assert exit_code == 1 and report["outcome"] == code and report["error"]["code"] == code
    assert report["error"]["detail"] == detail and {"code", "message", "stage", "detail"} <= set(report["error"])
    assert server.paths == [f"/reel/{CODE}/"], "UM GET: sem seguir redirect e sem nova tentativa"
    assert report["cdn_requests"] == 0 and report["items"] == [] and report["media_count"] == 0
    assert report["cookies_sent"] == 0 and "falha FAKE" not in json.dumps(report)


def test_refusal_by_redirect_reports_only_the_redirect_kind(tmp_path):
    server = Server(httpx.Response(302, headers={"location": f"https://www.instagram.com/p/{CODE}/?next=FAKEQUERY123"}))
    code, report = run_tool(server, tmp_path=tmp_path)
    error = report["error"]
    assert code == 1 and report["outcome"] == "access_refused"
    assert error == {"code": "access_refused", "message": error["message"], "stage": "post_document",
                     "detail": "redirect_other", "status": 302, "redirect": "redirect_other", "size": 0}
    assert report["document"] == {"status": 302, "redirect_kind": "redirect_other", "content_type": None, "size": 0}
    assert report["parse"] is None and report["requests"] == [{"method": "GET", "path": "/reel/", "status": 302}]


PLANTED = ("FAKE-COOKIE-PLANTED-01", "FAKE_TOKEN_PLANTED_01", "FAKEPLANTSC1", "igsh=FAKEPLANTQS",
           "scontent-plant.cdninstagram.com", "plantcookie")


def test_planted_sensitive_values_never_reach_the_report(tmp_path):
    # Pela resposta: cookie, token, shortcode, query string e URL de CDN no Location, Set-Cookie, content-type e corpo.
    body = (f'<html>https://scontent-plant.cdninstagram.com/v/FAKEPLANTSC1.jpg?oh=FAKE_TOKEN_PLANTED_01 '
            f'sessionid={PLANTED[0]}</html>')
    server = Server(httpx.Response(302, text=body, headers=[
        ("location", "https://www.instagram.com/p/FAKEPLANTSC1/?igsh=FAKEPLANTQS&token=FAKE_TOKEN_PLANTED_01"),
        ("set-cookie", f"plantcookie={PLANTED[0]}; Path=/"),
        ("content-type", "text/html; charset=utf-8; token=FAKE_TOKEN_PLANTED_01")]))
    code, report = run_tool(server, tmp_path=tmp_path)
    text = json.dumps(report, ensure_ascii=True)
    for secret in PLANTED:
        assert secret not in text, secret
    error = report["error"]
    assert code == 1 and error["status"] == 302 and error["redirect"] == "redirect_other"
    assert error["content_type"] == "text/html" and error["size"] == len(body)
    assert set(error) == {"code", "message", "stage", "detail", "status", "redirect", "content_type", "size"}

    # Pelo corpo de um documento 200: typename, product_type e tipos fora do formato são omitidos.
    node = media_node("video", CODE)
    node["product_type"] = PLANTED[1]
    node["video_versions"][0]["type"] = PLANTED[2]
    media = envelope(node, typename=f"https://{PLANTED[4]}/x")
    _, report = run_tool(Server(html(post_html([data_sjs_script(media_payload(media))]))), tmp_path=tmp_path)
    text = json.dumps(report, ensure_ascii=True)
    for secret in PLANTED:
        assert secret not in text, secret
    assert report["parse"]["typename"] is None and report["node"]["product_type"] is None
    assert report["node"]["video_version_types"] == [102, 103]

    # Pelo próprio diagnóstico: valor fora do formato é omitido, e campo fora da lista de permissão nunca sai.
    exc = poc.InstagramWebError("access_refused", "Recusado.", stage="post_document", diagnostic={
        "status": f"403 {PLANTED[1]}", "redirect": "https://scontent-plant.cdninstagram.com/v/FAKEPLANTSC1.jpg",
        "detail": f"sessionid={PLANTED[0]}", "content_type": f"text/{PLANTED[1]}?igsh=FAKEPLANTQS", "size": PLANTED[2],
        "path": "/p/FAKEPLANTSC1/", "location_path": "/p/FAKEPLANTSC1/", "set_cookie_names": ["plantcookie"],
        "json_keys": [PLANTED[1]], "token": PLANTED[1]})
    error = poc.safe_error(exc)
    assert error == {"code": "access_refused", "message": "Recusado.", "stage": "post_document", "detail": None}


def test_log_never_has_the_shortcode(tmp_path, caplog):
    caplog.set_level("DEBUG")
    run_tool(Server(html(post_page(media_node("video", CODE)))), tmp_path=tmp_path)
    assert CODE not in caplog.text and "FAKE_OH" not in caplog.text


@pytest.mark.parametrize("url", [
    "https://www.instagram.com/perfil_teste/", "https://evil.example.com/p/FAKEBAD0001/?token=FAKEBAD",
    "https://www.instagram.com/stories/perfil_teste/123/", "FAKEBAD0001", "https://www.instagram.com/p/abc/",
])
def test_invalid_link_makes_no_request_and_is_not_echoed(tmp_path, url):
    server = Server()
    code, report = run_tool(server, url, tmp_path=tmp_path)
    assert code == 1 and report["outcome"] == "invalid_input" and report["requests"] == []
    assert server.requests == []
    assert "FAKEBAD" not in json.dumps(report) and "perfil_teste" not in json.dumps(report)


def test_runs_without_any_saved_session(tmp_path):
    """A POC não lê a sessão salva (não há provider de sessão): o relatório sai igual sem nenhuma configurada."""
    settings = make_settings(tmp_path)
    assert not settings.secrets_dir.exists() or not any(settings.secrets_dir.iterdir())
    code, report = run_tool(Server(html(post_page(media_node("video", CODE)))), tmp_path=tmp_path)
    assert code == 0 and report["session_used"] is False


def test_refuses_when_app_is_running(tmp_path):
    server = Server()
    code, report = run_tool(server, running=True, tmp_path=tmp_path)
    assert code == 2 and report["outcome"] == "app_running" and server.requests == []
    assert "requests" not in report


def test_no_database_or_storage_created(tmp_path):
    run_tool(Server(html(post_page(media_node("video", CODE)))), tmp_path=tmp_path)
    settings = make_settings(tmp_path)
    assert not settings.database_path.exists() and not settings.storage_dir.exists()


def test_keyboard_interrupt_during_request_reports_json(tmp_path):
    server = Server(KeyboardInterrupt())
    code, report = run_tool(server, tmp_path=tmp_path)
    assert code == 130 and report["outcome"] == "interrupted" and report["error"]["code"] == "interrupted"
    assert report["items"] == []
    json.dumps(report)


def test_keyboard_interrupt_in_main_prints_json(monkeypatch, capsys):
    def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr(poc, "load_settings", interrupted)
    assert poc.main(["--url", LINK]) == 130
    assert json.loads(capsys.readouterr().out)["outcome"] == "interrupted"


def test_script_passes_the_url_and_does_not_run_anything_else():
    script = (Path(__file__).resolve().parents[2] / "scripts" / "poc-instagram-post.ps1").read_text(
        encoding="utf-8-sig")
    assert "poc_instagram_post.py" in script and "--url $Url" in script and "[string]$Url" in script
