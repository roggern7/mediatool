"""Importação por link: formatos e limites do diagnóstico seguro (`safe_diagnostic`), teto de 500 caracteres da
mensagem gravada com o sufixo do diagnóstico, diagnóstico nos avisos do runner e a 2a recusa de um link diante do
disjuntor `links_failing`. 100% offline, valores FAKE."""

from __future__ import annotations

import pytest

from app.integrations.instagram.downloader import InstagramError
from app.integrations.instagram.web.errors import InstagramWebError
from app.jobs.errors import MAX_MESSAGE_LENGTH, message_with_diagnostic, safe_diagnostic
from app.jobs.import_runner import LINKS_REFUSED_MESSAGE
from import_links_fakes import create_links, link, link_rows, links_app, photo
from media_fakes import FakeInstagramDownloader, remote
from test_import_links_refused import (
    BODY,
    SECRET_CDN,
    SECRET_SESSION,
    A,
    B,
    C,
    assert_nothing_sensitive,
    refused,
    row,
    warnings,
)

# --------------------------------------------------------------------------- diagnóstico: formatos e limites


class _LoudInt(int):
    """Subclasse de int cujo texto não é o número: só `int` exato pode entrar no diagnóstico."""

    def __str__(self) -> str:
        return "9 detail=injetado"

    def __format__(self, spec: str) -> str:
        return str(self)


def diag_error(stage: object = "graphql", **diagnostic) -> InstagramError:
    error = InstagramError("access_refused")
    error.stage = stage
    error.diagnostic = dict(diagnostic)
    return error


@pytest.mark.parametrize("value, shown", [
    ("text/html", "text/html"), ("TEXT/HTML; charset=utf-8", "text/html"), (" application/json ", "application/json"),
    ("video/mp4", "video/mp4"), ("application/x-javascript;q=1", "application/x-javascript"),
])
def test_content_type_is_shown_only_from_the_fixed_list(value, shown):
    assert safe_diagnostic(diag_error(content_type=value)) == f"stage=graphql content_type={shown}"


@pytest.mark.parametrize("value", [
    "text/htmlx", "xtext/html", "text/html evil", "text/html\nstatus=200", "text/html/x", "text/x-secret",
    "application/vnd.fake+json", "text", "", 5, None, b"text/html", f"text/{SECRET_SESSION}",
])
def test_content_type_outside_the_fixed_list_is_omitted(value):
    assert safe_diagnostic(diag_error(content_type=value)) == "stage=graphql"


@pytest.mark.parametrize("value", ["a", "http_403", "x12345", "a" * 64, "a1" + "b" * 62])
def test_token_fields_accept_the_strict_format(value):
    assert safe_diagnostic(diag_error(stage=value, detail=value)) == f"stage={value} detail={value}"


@pytest.mark.parametrize("value", [
    "HTTP_403", "Http_403", "http_403X", "a" * 65, "_http", "4xx", "", " http", "http 403", "http_403\n", "http-403",
    "http=403", "id_123456", "a1234567b", "x" + "9" * 6, "détail", 403, None,
])
def test_token_fields_outside_the_format_are_omitted(value):
    assert safe_diagnostic(diag_error(stage=value, detail=value, status=403)) == "status=403"


@pytest.mark.parametrize("value", ["redirect_login", "redirect_challenge", "redirect_checkpoint", "redirect_consent",
                                   "redirect_home", "redirect_other"])
def test_redirect_accepts_only_the_closed_set(value):
    assert safe_diagnostic(diag_error(redirect=value)) == f"stage=graphql redirect={value}"


@pytest.mark.parametrize("value", ["redirect_evil", "redirect_home2", "REDIRECT_HOME", "redirect_home\n", "home",
                                   "http_403", "", None, 1])
def test_redirect_outside_the_closed_set_is_omitted(value):
    assert safe_diagnostic(diag_error(redirect=value)) == "stage=graphql"


@pytest.mark.parametrize("key, value", [("status", 100), ("status", 403), ("status", 599), ("size", 0),
                                        ("size", 1), ("size", 10**12)])
def test_numbers_inside_the_range_are_shown(key, value):
    assert safe_diagnostic(diag_error(**{key: value})) == f"stage=graphql {key}={value}"


@pytest.mark.parametrize("key, value", [
    ("status", 99), ("status", 600), ("status", 0), ("status", -403), ("status", 10**6), ("status", True),
    ("status", 403.0), ("status", "403"), ("status", _LoudInt(403)), ("status", None),
    ("size", -1), ("size", 10**12 + 1), ("size", True), ("size", False), ("size", 1.0), ("size", "1"),
    ("size", _LoudInt(7)),
])
def test_numbers_outside_the_range_or_not_plain_int_are_omitted(key, value):
    assert safe_diagnostic(diag_error(**{key: value})) == "stage=graphql"


def test_stage_of_the_error_wins_over_the_stage_in_the_diagnostic():
    error = diag_error(stage="graphql")
    error.diagnostic["stage"] = "bootstrap"
    assert safe_diagnostic(error) == "stage=graphql", "o stage do erro tem precedência"
    error.stage = ""
    assert safe_diagnostic(error) == "stage=bootstrap", "sem stage no erro, vale o do diagnóstico"
    del error.stage
    assert safe_diagnostic(error) == "stage=bootstrap"
    error.stage = "Fora Do Formato"
    assert safe_diagnostic(error) == "", "stage do erro inválido: omitido, sem cair para o do diagnóstico"


def test_no_diagnostic_value_can_break_the_log_line():
    hostile = "ok\nLote 1: linha forjada\r\nstage=graphql"
    error = diag_error(stage=hostile, status=hostile, redirect=hostile, detail=hostile, content_type=hostile,
                       size=hostile, path=hostile)
    assert safe_diagnostic(error) == ""
    error = diag_error(stage="graphql", status=403, redirect="redirect_home\n", detail="http_403\r",
                       content_type="text/html\n; x", size=5, path="/graphql/query\n")
    text = safe_diagnostic(error)
    assert text == "stage=graphql status=403 content_type=text/html size=5"
    full = safe_diagnostic(refused(302, "/"))
    for value in (text, full):
        assert "\n" not in value and "\r" not in value and "  " not in value
        assert all(part.count("=") == 1 for part in value.split(" "))


def test_safe_diagnostic_result_passes_through_redact(monkeypatch):
    from app.jobs import errors as job_errors

    monkeypatch.setattr(job_errors, "redact", lambda text: f"<{text}>")
    assert safe_diagnostic(diag_error(status=403)) == "<stage=graphql status=403>"


# --------------------------------------------------------------------------- mensagem gravada: teto de 500


def test_message_with_diagnostic_respects_the_limit_dropping_whole_fields():
    diagnostic = "stage=graphql status=403 detail=http_403 content_type=text/html size=5 path=/graphql/query"
    assert message_with_diagnostic("Recusado.", diagnostic) == f"Recusado. (diagnóstico: {diagnostic})"
    assert message_with_diagnostic("Recusado.", "") == "Recusado."
    fields = diagnostic.split()
    prefix = " (diagnóstico: "
    seen = set()
    for size in range(380, 520):
        message = "m" * size
        kept_message = message[:MAX_MESSAGE_LENGTH]
        text = message_with_diagnostic(message, diagnostic)
        assert len(text) <= MAX_MESSAGE_LENGTH and text.startswith(kept_message)
        rest = text[len(kept_message):]
        if not rest:
            assert len(f"{kept_message}{prefix}{fields[0]})") > MAX_MESSAGE_LENGTH, "cabia ao menos um campo"
            seen.add(0)
            continue
        assert rest.startswith(prefix) and rest.endswith(")")
        kept = rest[len(prefix):-1].split(" ")
        assert kept == fields[:len(kept)], "só campos inteiros, na ordem; nenhum valor cortado ao meio"
        if len(kept) < len(fields):
            longer = f"{kept_message}{prefix}{' '.join(fields[:len(kept) + 1])})"
            assert len(longer) > MAX_MESSAGE_LENGTH, "o campo seguinte não cabia"
        seen.add(len(kept))
    assert seen == set(range(len(fields) + 1)), "o laço passou por todos os cortes possíveis"
    assert message_with_diagnostic("m" * 600, diagnostic) == "m" * MAX_MESSAGE_LENGTH


def test_second_refusal_fails_the_link_with_the_diagnostic_suffix(harness_factory):
    h, dl = links_app(harness_factory, {A: photo(A), C: photo(C)})
    first, second = refused(403, None), refused(302, "/")
    dl.errors = {B: [first, second]}
    batch = create_links(h, [link(A), link(B), link(C)])
    h.run_all()
    assert h.client.post(f"/api/batches/{batch['id']}/resume-listing").status_code == 200
    h.run_all()
    assert dl.calls == [A, B, B]
    diagnostic = ("stage=graphql status=302 redirect=redirect_home detail=redirect_home content_type=text/html "
                  f"size={len(BODY)} path=/graphql/query")
    failed = link_rows(h, batch["id"])[1]
    assert (failed["status"], failed["error_code"]) == ("failed", "access_refused")
    assert failed["error_message"] == f"{second.message} (diagnóstico: {diagnostic})"
    assert row(h, batch["id"])["error_message"] == f"{LINKS_REFUSED_MESSAGE} (diagnóstico: {diagnostic})"
    assert_nothing_sensitive(failed["error_message"])


def test_recorded_messages_never_pass_the_limit(harness_factory):
    h, dl = links_app(harness_factory, {A: photo(A), C: photo(C)})
    errors = []
    for _ in range(2):
        error = refused(403, None)
        error.message = ("Recusado pelo servidor. " * 20).strip()     # 479 caracteres
        error.stage = "s" * 64
        error.diagnostic["detail"] = "d" * 64
        errors.append(error)
    message = errors[0].message
    dl.errors = {B: errors}
    batch = create_links(h, [link(A), link(B), link(C)])
    h.run_all()
    saved = row(h, batch["id"])["error_message"]
    assert len(saved) <= MAX_MESSAGE_LENGTH and saved.startswith(LINKS_REFUSED_MESSAGE + " (diagnóstico: stage=sss")
    assert saved.endswith(")"), saved
    assert h.client.post(f"/api/batches/{batch['id']}/resume-listing").status_code == 200
    h.run_all()
    failed = link_rows(h, batch["id"])[1]["error_message"]
    assert len(message) == 479 and failed == message, "nem o 1o campo do diagnóstico cabe: fica só a mensagem"
    saved = row(h, batch["id"])["error_message"]
    assert len(saved) <= MAX_MESSAGE_LENGTH and saved.endswith(")")


# --------------------------------------------------------------------------- diagnóstico nos outros avisos


def test_backoff_warning_carries_the_diagnostic(harness_factory, caplog):
    h, dl = links_app(harness_factory, {A: photo(A)})
    dl.errors = {A: [InstagramWebError("network_error", "Falha de rede.", retryable=True, stage="graphql",
                                       diagnostic={"detail": "timeout", "path": "/graphql/query"})]}
    with caplog.at_level("WARNING"):
        batch = create_links(h, [link(A)])
        h.run_all()
    assert (f"Lote {batch['id']}: falha na listagem (network_error); retomada do checkpoint em 60s "
            "[stage=graphql detail=timeout path=/graphql/query]") in warnings(caplog)


@pytest.mark.parametrize("after, expected", [
    (0, "Lote {id} falhou na listagem: contract_changed [stage=graphql status=200 detail=invalid_json]"),
    (3, "Lote {id}: listagem parcial (3 itens) com erro contract_changed [stage=graphql status=200 "
        "detail=invalid_json]"),
])
def test_finished_listing_warnings_carry_the_diagnostic(harness_factory, caplog, after, expected):
    dl = FakeInstagramDownloader(items=[remote(f"R{i:04d}") for i in range(5)])
    dl.listing_error = InstagramWebError("contract_changed", "Formato mudou.", stage="graphql",
                                         diagnostic={"status": 200, "detail": "invalid_json", "url": SECRET_CDN})
    dl.listing_error_after = after
    h = harness_factory(downloader=dl)
    with caplog.at_level("WARNING"):
        batch_id = h.import_profile(limit=100)["id"]
        h.run_imports()
    assert expected.format(id=batch_id) in warnings(caplog), warnings(caplog)
    assert_nothing_sensitive(warnings(caplog))


# --------------------------------------------------------------------------- 2a recusa x disjuntor links_failing


def test_refused_link_as_the_third_failure_in_a_row_does_not_schedule_the_automatic_retry(harness_factory):
    """Dois links falhos seguidos por outro motivo e o 3o falho por `access_refused` (2a recusa dele): a sequência
    chega a 3 com link ainda pendente, mas o disjuntor `links_failing` (nova tentativa em 6 h) NÃO é acionado."""
    gone1, gone2 = "GONE100001", "GONE200002"
    h, dl = links_app(harness_factory, {C: photo(C)})      # as duas primeiras publicações não trazem mídia
    dl.errors = {B: [refused(403, None)]}
    batch_id = create_links(h, [link(gone1), link(gone2), link(B), link(C)])["id"]
    # O link B já foi recusado uma vez (ação anterior do dono): a próxima recusa é a 2a dele.
    h.ctx.db.execute("UPDATE batch_links SET attention_count = 1 WHERE batch_id = ? AND position = 3", (batch_id,))
    h.run_all()
    assert dl.calls == [gone1, gone2, B]
    rows = link_rows(h, batch_id)
    assert [(r["status"], r["error_code"]) for r in rows] == [
        ("failed", "content_unavailable"), ("failed", "content_unavailable"), ("failed", "access_refused"),
        ("pending", None)]
    saved = row(h, batch_id)
    assert (saved["status"], saved["listing_state"], saved["error_code"]) == ("listing", "needs_attention",
                                                                              "access_refused")
    assert saved["listing_retry_at"] is None and saved["listing_attention_tries"] == 0
    assert saved["error_message"].startswith(LINKS_REFUSED_MESSAGE)
    for _ in range(4):
        h.run_all()
    assert dl.calls == [gone1, gone2, B], "nada é pedido sozinho"
    assert h.client.post(f"/api/batches/{batch_id}/resume-listing").status_code == 200
    h.run_all()
    assert dl.calls == [gone1, gone2, B, C] and h.batch(batch_id)["status"] == "completed"


# --------------------------------------------------------------------------- textos: só `str` exato


class _LoudStr(str):
    """Subclasse de str que compara/casa como um valor válido mas se escreve como outra coisa."""

    def __str__(self) -> str:
        return "ok status=200 detail=injetado"

    def __format__(self, spec: str) -> str:
        return str(self)


@pytest.mark.parametrize("key, value", [
    ("stage", "graphql"), ("detail", "http_403"), ("redirect", "redirect_home"), ("content_type", "text/html"),
    ("path", "/graphql/query"),
])
def test_text_fields_accept_only_plain_str(key, value):
    loud = _LoudStr(value)
    assert loud == value and f"{loud}" != value, "a subclasse passaria nas comparações e imprimiria outra coisa"
    plain = diag_error(stage=None, **{key: value}) if key != "stage" else diag_error(stage=value)
    assert safe_diagnostic(plain) == f"{key}={value}"
    hostile = diag_error(stage=None, **{key: loud}) if key != "stage" else diag_error(stage=loud)
    assert safe_diagnostic(hostile) == ""
    hostile = diag_error(stage=None, status=403)
    hostile.diagnostic["stage"] = _LoudStr("graphql")
    assert safe_diagnostic(hostile) == "status=403"
