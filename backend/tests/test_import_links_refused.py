"""Importação por link com a consulta recusada (`access_refused`): diagnóstico seguro no log e lote parado esperando
ação manual, sem nova tentativa automática. Também trava o que NÃO muda (listagem por perfil e outros códigos).
100% offline, valores FAKE."""

from __future__ import annotations

import pytest

from app.db.connection import parse_utc, utc_now
from app.integrations.instagram.downloader import InstagramError
from app.integrations.instagram.web.errors import InstagramWebError, classify_response
from app.integrations.instagram.web.http import WebResponse
from app.jobs.errors import safe_diagnostic
from app.jobs.import_runner import ATTENTION_RETRY_SECONDS, LINKS_REFUSED_MESSAGE
from import_links_fakes import LinksDownloader, create_links, link, link_rows, links_app, photo
from media_fakes import FakeInstagramDownloader, remote

A, B, C, D = "AAAAA11111", "BBBBB22222", "CCCCC33333", "DDDDD44444"

# Valores sensíveis FAKE plantados no erro: nenhum pode aparecer no log nem na mensagem gravada.
SECRET_SESSION = "FAKESESSIONID0123456789"
SECRET_CSRF = "FAKECSRFTOKEN0123456789"
SECRET_TOKEN = "FAKEDTSGTOKEN0123456789"
SECRET_SHORTCODE = "ZZsecretShort9"
SECRET_QUERY = "igsh=FAKEQUERYVALUE01"
SECRET_CDN = "https://scontent.cdninstagram.com/v/t51/FAKEFILE.jpg?oh=FAKESIGNATURE01&oe=FAKEEXP"
SECRET_ID = "3400000000000000001"
SENSITIVE = (SECRET_SESSION, SECRET_CSRF, SECRET_TOKEN, SECRET_SHORTCODE, SECRET_QUERY, "FAKEQUERYVALUE01",
             "scontent", "FAKESIGNATURE01", "FAKEFILE", SECRET_ID, "sessionid", "csrftoken", "fb_dtsg", "Bearer",
             "?", B)
BODY = (f"<html>sessionid={SECRET_SESSION}; csrftoken={SECRET_CSRF} fb_dtsg={SECRET_TOKEN} {SECRET_CDN} "
        f"/p/{SECRET_SHORTCODE}/</html>")


def refused(status: int, location: str | None, *, path: str = "/graphql/query") -> InstagramWebError:
    """Erro como o provider web o monta (`classify_response`), com valores sensíveis plantados no diagnóstico."""
    response = WebResponse(status, location, "text/html; charset=utf-8", 5, ["sessionid", "csrftoken"], False, BODY,
                           path)
    error = classify_response(response, stage="graphql")
    assert error is not None
    error.diagnostic.update({
        "cookie": f"sessionid={SECRET_SESSION}", "authorization": f"Bearer {SECRET_TOKEN}", "url": SECRET_CDN,
        "query": f"?{SECRET_QUERY}", "shortcode": SECRET_SHORTCODE, "media_id": SECRET_ID, "body": BODY,
    })
    return error


def log_blob(caplog) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


def warnings(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records if r.levelname == "WARNING" and r.name == "media_tool.imports")


def assert_nothing_sensitive(text: str) -> None:
    for needle in SENSITIVE:
        assert needle not in text, needle


def row(h, batch_id: int) -> dict:
    return h.ctx.batches.get(batch_id)


# --------------------------------------------------------------------------- diagnóstico seguro (função)


def test_safe_diagnostic_keeps_only_allowed_fields_with_strict_formats():
    error = refused(403, None)
    assert safe_diagnostic(error) == (
        "stage=graphql status=403 detail=http_403 content_type=text/html size=%d path=/graphql/query" % len(BODY))
    redirect = refused(302, f"/p/{SECRET_SHORTCODE}/")
    assert safe_diagnostic(redirect) == (
        "stage=graphql status=302 redirect=redirect_other detail=redirect_other content_type=text/html size=%d "
        "path=/graphql/query" % len(BODY))
    assert_nothing_sensitive(safe_diagnostic(redirect))


def test_safe_diagnostic_omits_values_outside_the_format():
    error = InstagramError("access_refused")
    error.stage = f"graphql {SECRET_SESSION}"
    error.diagnostic = {
        "status": "403", "redirect": f"redirect_home?{SECRET_QUERY}", "detail": f"sessionid={SECRET_SESSION}",
        "content_type": SECRET_CDN, "size": True, "path": f"/p/{SECRET_SHORTCODE}/", "location_path": f"/{SECRET_ID}/",
    }
    assert safe_diagnostic(error) == ""
    error.stage = "graphql"
    error.diagnostic.update(status=403, detail="http_403", path=f"/graphql/query?{SECRET_QUERY}", size=-1)
    assert safe_diagnostic(error) == "stage=graphql status=403 detail=http_403"
    assert safe_diagnostic(InstagramError("access_refused")) == "", "erro sem diagnóstico: nada"
    assert safe_diagnostic(RuntimeError(SECRET_SESSION)) == ""


# --------------------------------------------------------------------------- diagnóstico seguro no log do lote


@pytest.mark.parametrize("status, location, code, expected", [
    (403, None, "access_refused", ("status=403", "detail=http_403")),
    (302, "/", "access_refused", ("status=302", "redirect=redirect_home", "detail=redirect_home")),
    (302, f"/consent/{SECRET_SHORTCODE}/", "access_refused",
     ("status=302", "redirect=redirect_consent", "detail=redirect_consent")),
    (301, f"/p/{SECRET_SHORTCODE}/", "access_refused",
     ("status=301", "redirect=redirect_other", "detail=redirect_other")),
    (302, f"/accounts/login/{SECRET_SHORTCODE}", "request_auth_failed",
     ("status=302", "redirect=redirect_login", "detail=redirect_login")),
    (302, f"/challenge/{SECRET_ID}/", "checkpoint_required",
     ("status=302", "redirect=redirect_challenge", "detail=redirect_challenge")),
    (302, f"/checkpoint/{SECRET_ID}/", "checkpoint_required",
     ("status=302", "redirect=redirect_checkpoint", "detail=redirect_checkpoint")),
])
def test_link_error_log_has_the_safe_diagnostic_and_nothing_sensitive(harness_factory, caplog, status, location, code,
                                                                      expected):
    h, dl = links_app(harness_factory, {A: photo(A), C: photo(C)})
    error = refused(status, location)
    assert error.code == code
    dl.errors = {B: [error]}
    with caplog.at_level("DEBUG"):
        batch = create_links(h, [link(A), link(B), link(C)])
        h.run_all()
    assert dl.calls == [A, B]
    lines = [line for line in warnings(caplog).splitlines() if f"Lote {batch['id']}" in line and code in line]
    assert len(lines) == 1, warnings(caplog)
    for part in ("stage=graphql", "content_type=text/html", f"size={len(BODY)}", "path=/graphql/query", *expected):
        assert part in lines[0], (part, lines[0])
    assert_nothing_sensitive(log_blob(caplog))
    api = h.batch(batch["id"])
    assert api["error_code"] == code
    assert_nothing_sensitive(str(api["error_message"]))
    assert_nothing_sensitive(str([r["error_message"] for r in link_rows(h, batch["id"])]))


def test_unsafe_request_path_is_omitted_from_the_log(harness_factory, caplog):
    h, dl = links_app(harness_factory, {A: photo(A)})
    dl.errors = {B: [refused(403, None, path=f"/p/{SECRET_SHORTCODE}/")]}
    with caplog.at_level("DEBUG"):
        create_links(h, [link(B), link(A)])
        h.run_all()
    text = warnings(caplog)
    assert "status=403 detail=http_403" in text and "path=" not in text
    assert_nothing_sensitive(log_blob(caplog))


def test_failed_link_is_logged_by_position_with_the_diagnostic(harness_factory, caplog):
    h, dl = links_app(harness_factory, {A: photo(A)})
    error = InstagramWebError("content_unavailable", "A publicação não existe mais.", stage="graphql",
                              diagnostic={"status": 200, "detail": "post_null", "path": "/graphql/query",
                                          "content_type": "application/json", "size": 42, "url": SECRET_CDN})
    dl.errors = {B: [error]}
    with caplog.at_level("DEBUG"):
        batch = create_links(h, [link(B), link(A)])
        h.run_all()
    assert (f"Lote {batch['id']}: link 1 não pôde ser importado (content_unavailable) [stage=graphql status=200 "
            "detail=post_null content_type=application/json size=42 path=/graphql/query]") in warnings(caplog)
    assert_nothing_sensitive(log_blob(caplog))
    # Comportamento inalterado: erro DO LINK, mensagem do erro sem sufixo, lote segue e conclui.
    rows = link_rows(h, batch["id"])
    assert [(r["status"], r["error_code"], r["error_message"]) for r in rows] == [
        ("failed", "content_unavailable", "A publicação não existe mais."), ("resolved", None, None)]
    assert dl.calls == [B, A] and h.batch(batch["id"])["status"] == "completed"


# --------------------------------------------------------------------------- access_refused: espera manual


def test_refused_link_batch_waits_for_the_owner_without_any_automatic_retry(harness_factory, caplog):
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B), C: photo(C), D: photo(D)})
    dl.errors = {B: [refused(403, None), InstagramError("access_refused")]}
    with caplog.at_level("WARNING"):
        batch = create_links(h, [link(A), link(B), link(C), link(D)])
        h.run_all()
    batch_id = batch["id"]
    assert dl.calls == [A, B], "depois da recusa nenhum outro link é pedido"
    saved = row(h, batch_id)
    assert (saved["status"], saved["listing_state"], saved["error_code"]) == ("listing", "needs_attention",
                                                                              "access_refused")
    assert saved["listing_retry_at"] is None and saved["listing_attention_tries"] == 0
    assert saved["listing_retry_level"] == 0
    diagnostic = f"stage=graphql status=403 detail=http_403 content_type=text/html size={len(BODY)} path=/graphql/query"
    assert saved["error_message"] == f"{LINKS_REFUSED_MESSAGE} (diagnóstico: {diagnostic})"
    assert "recusou a consulta" in LINKS_REFUSED_MESSAGE and "Não haverá nova tentativa automática" in (
        LINKS_REFUSED_MESSAGE)
    assert (f"Lote {batch_id}: consulta por link recusada (access_refused); aguardando ação manual, sem nova "
            f"tentativa automática [{diagnostic}]") in warnings(caplog)
    assert [r["status"] for r in link_rows(h, batch_id)] == ["resolved", "pending", "pending", "pending"]
    assert h.ctx.batches.has_active_work() is False and h.ctx.worker.downloads_paused() is False

    # Novas passadas do runner, mesmo "muito tempo depois": nada é pedido.
    for _ in range(5):
        h.run_all()
    assert dl.calls == [A, B]

    # Reinício do app sobre os mesmos dados (recovery): nada é pedido e o estado continua o mesmo.
    dl2 = LinksDownloader(posts=dict(dl.posts), errors={B: [InstagramError("access_refused")]})
    h2 = harness_factory(root=h.root, downloader=dl2)
    for _ in range(3):
        h2.run_all()
    assert dl2.calls == []
    saved = row(h2, batch_id)
    assert (saved["listing_state"], saved["listing_retry_at"], saved["error_code"]) == (
        "needs_attention", None, "access_refused")

    # Retomada manual: exatamente UMA nova consulta. Recusada de novo, o MESMO link fica falho (LINK_ATTENTION_LIMIT),
    # mas o lote continua parado: o próximo link não é pedido sozinho.
    resumed = h2.client.post(f"/api/batches/{batch_id}/resume-listing")
    assert resumed.status_code == 200 and resumed.json()["listing_state"] == "pending"
    for _ in range(3):
        h2.run_all()
    assert dl2.calls == [B], "uma consulta por ação do dono"
    saved = row(h2, batch_id)
    assert (saved["status"], saved["listing_state"], saved["listing_retry_at"], saved["error_code"]) == (
        "listing", "needs_attention", None, "access_refused")
    assert saved["error_message"] == LINKS_REFUSED_MESSAGE, "erro sem diagnóstico: mensagem sem sufixo"
    rows = link_rows(h2, batch_id)
    assert [(r["status"], r["error_code"]) for r in rows] == [
        ("resolved", None), ("failed", "access_refused"), ("pending", None), ("pending", None)]

    # Nova ação do dono: a consulta volta a funcionar e o lote segue normalmente até o fim.
    assert h2.client.post(f"/api/batches/{batch_id}/resume-listing").status_code == 200
    h2.run_all()
    assert dl2.calls == [B, C, D]
    done = h2.batch(batch_id)
    assert (done["status"], done["found"], done["error_code"]) == ("completed", 3, None)
    assert done["error_message"] == "1 de 4 links não puderam ser importados."


def test_refused_link_batch_can_be_cancelled(harness_factory):
    h, dl = links_app(harness_factory, {A: photo(A), C: photo(C)})
    dl.errors = {B: [InstagramError("access_refused") for _ in range(5)]}
    batch = create_links(h, [link(A), link(B), link(C)])
    h.run_all()
    assert row(h, batch["id"])["listing_state"] == "needs_attention"
    cancelled = h.client.post(f"/api/batches/{batch['id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    h.run_all()
    assert dl.calls == [A, B]
    assert [r["status"] for r in link_rows(h, batch["id"])] == ["resolved", "cancelled", "cancelled"]
    assert h.client.post(f"/api/batches/{batch['id']}/resume-listing").status_code == 409


def test_refused_batch_scheduled_by_an_older_version_is_not_retried(harness_factory):
    h, dl = links_app(harness_factory, {A: photo(A)})
    dl.errors = {A: [InstagramError("access_refused")]}
    batch = create_links(h, [link(A)])
    h.run_all()
    # Estado deixado pela versão anterior: needs_attention com a nova tentativa de 6 h já vencida.
    h.ctx.db.execute("UPDATE batches SET listing_retry_at = ?, listing_attention_tries = 1 WHERE id = ?",
                     (utc_now(-60), batch["id"]))
    for _ in range(3):
        h.run_all()
    assert dl.calls == [A]
    assert h.client.post(f"/api/batches/{batch['id']}/resume-listing").status_code == 200
    h.run_all()
    assert dl.calls == [A, A] and h.batch(batch["id"])["status"] == "completed"


@pytest.mark.parametrize("codes, status, error_code, message", [
    ((A, B), "completed", None, "1 de 2 links não puderam ser importados."),
    ((B,), "failed", "links_failed", "O link não pôde ser importado."),
])
def test_last_pending_link_refused_twice_finishes_the_batch(harness_factory, codes, status, error_code, message):
    h, dl = links_app(harness_factory, {A: photo(A)})
    dl.errors = {B: [InstagramError("access_refused") for _ in range(5)]}
    batch = create_links(h, [link(code) for code in codes])
    h.run_all()
    assert row(h, batch["id"])["listing_state"] == "needs_attention" and dl.calls == list(codes)
    assert h.client.post(f"/api/batches/{batch['id']}/resume-listing").status_code == 200
    h.run_all()
    h.run_all()
    assert dl.calls == [*codes, B], "sem link pendente não há o que esperar: o lote encerra"
    done = h.batch(batch["id"])
    assert (done["status"], done["error_code"], done["error_message"]) == (status, error_code, message)
    assert link_rows(h, batch["id"])[-1]["error_code"] == "access_refused"


# --------------------------------------------------------------------------- o que NÃO muda


def test_profile_listing_with_access_refused_keeps_the_automatic_retry(harness_factory, caplog):
    dl = FakeInstagramDownloader(items=[remote(f"R{i:04d}") for i in range(100)])
    dl.listing_error = refused(403, None)
    dl.listing_error_after = 24   # páginas 1-2 entregues; erro ao pedir a página 3
    h = harness_factory(downloader=dl)
    with caplog.at_level("WARNING"):
        batch_id = h.import_profile(limit=100)["id"]
        h.run_all()
    saved = row(h, batch_id)
    assert (saved["status"], saved["listing_state"], saved["error_code"]) == ("listing", "needs_attention",
                                                                              "access_refused")
    assert saved["error_message"] == "O Instagram recusou esta consulta. Isso não indica sessão inválida."
    assert saved["listing_attention_tries"] == 1
    wait = (parse_utc(saved["listing_retry_at"]) - parse_utc(utc_now())).total_seconds()
    assert abs(wait - ATTENTION_RETRY_SECONDS) <= 5
    assert (f"Lote {batch_id}: listagem aguardando ação (access_refused); checkpoint mantido; nova tentativa "
            f"automática em 6 h [stage=graphql status=403 detail=http_403 content_type=text/html size={len(BODY)} "
            "path=/graphql/query]") in warnings(caplog)
    assert_nothing_sensitive(warnings(caplog))
    # A nova tentativa automática continua existindo na listagem por perfil.
    listings = dl.listings
    h.ctx.db.execute("UPDATE batches SET listing_retry_at = ?", (utc_now(-1),))
    h.run_imports()
    assert dl.listings == listings + 1


def test_other_batch_errors_on_link_batches_keep_the_automatic_retry(harness_factory):
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B), C: photo(C)})
    error = InstagramWebError("contract_changed", "Formato mudou.", stage="graphql",
                              diagnostic={"status": 200, "detail": "invalid_json", "path": "/graphql/query"})
    dl.errors = {B: [error]}
    batch = create_links(h, [link(A), link(B), link(C)])
    h.run_all()
    saved = row(h, batch["id"])
    assert (saved["listing_state"], saved["error_code"], saved["error_message"]) == (
        "needs_attention", "contract_changed", "Formato mudou.")
    hours = (parse_utc(saved["listing_retry_at"]) - parse_utc(utc_now())).total_seconds() / 3600
    assert 5.9 < hours <= 6 and saved["listing_attention_tries"] == 1
    h.ctx.db.execute("UPDATE batches SET listing_retry_at = ?", (utc_now(-1),))   # as 6 h passaram
    h.run_all()
    assert dl.calls == [A, B, B, C] and h.batch(batch["id"])["status"] == "completed"


@pytest.mark.parametrize("code, state", [("rate_limited", "paused"), ("network_error", "paused")])
def test_pause_and_network_errors_on_link_batches_are_unchanged(harness_factory, code, state):
    h, dl = links_app(harness_factory, {A: photo(A), B: photo(B)})
    dl.errors = {A: [InstagramError(code)]}
    batch = create_links(h, [link(A), link(B)])
    h.run_all()
    saved = row(h, batch["id"])
    assert (saved["status"], saved["listing_state"], saved["error_code"]) == ("listing", state, code)
    assert dl.calls == [A] and [r["status"] for r in link_rows(h, batch["id"])] == ["pending", "pending"]
    if code == "network_error":
        wait = (parse_utc(saved["listing_retry_at"]) - parse_utc(utc_now())).total_seconds()
        assert 0 < wait <= 60 and saved["listing_retry_level"] == 1
        h.ctx.db.execute("UPDATE batches SET listing_retry_at = NULL")
    else:
        assert h.ctx.worker.downloads_paused() is True
        assert h.client.delete("/api/settings/instagram/pause").status_code == 200
    h.run_all()
    assert dl.calls == [A, A, B] and h.batch(batch["id"])["status"] == "completed", "retomada automática mantida"
