"""Importação por link com a 2a tentativa de um link restrito pelo documento aberto com a sessão do dono (provider web
REAL sobre ``ScriptedHttp``: um roteiro para o cliente anônimo e outro para o cliente com a sessão). Links públicos
nunca usam a sessão; recusa da tentativa com a sessão aplica a política existente e desliga a sessão no resto do lote.
100% offline (conexão de rede proibida), valores FAKE."""

from __future__ import annotations

from app.integrations.instagram.web.errors import InstagramWebError
from app.services.instagram_gate import InstagramPaused
from audit_links_helpers import (  # noqa: F401 - fixtures
    ScriptedHttp,
    WebSim,
    audit_env,
    base_media,
    check_invariants,
    document,
    make_app,
    make_root,
)
from import_links_fakes import create_links, link, link_rows, listing_state
from post_document_fixtures import data_sjs_script, envelope, media_payload, post_html, session_page

A, B, C, D = "AAAAAgated1", "BBBBBpublic", "CCCCCgated3", "DDDDDgated4"
SID = "FAKE-SESSIONID-0123456789"   # FakeProvider de test_web_provider
MSG_WITH_SESSION = ("A publicação continua indisponível mesmo com a sessão: pode ser de conta privada que você não "
                    "segue, removida ou restrita por região/idade.")


def gated(code: str) -> str:
    return post_html([data_sjs_script(media_payload(envelope(None, code=code)))], noise=2)


def session_app(harness_factory, base_media, audit_env):
    session_http = ScriptedHttp(logged=True)
    sim = WebSim(base_media, session_http=session_http)
    h, sim = make_app(harness_factory, base_media, audit_env, sim=sim)
    return h, sim, session_http


def assert_clean(text: str, *codes: str) -> None:
    for secret in (*codes, "FAKE_OH", "cdninstagram", "fbcdn"):
        assert secret not in text, secret
    for word in ("cookie", "sessionid", "csrftoken", "token"):
        assert word not in text.lower(), word


def test_public_links_never_create_the_session_client(harness_factory, base_media, audit_env):
    h, sim, session_http = session_app(harness_factory, base_media, audit_env)
    sim.add("image", A)
    sim.add("reel", B)
    batch = create_links(h, [link(A), link(B, "reel")])
    h.run_all()
    assert h.batch(batch["id"])["status"] == "completed"
    assert sim.calls == [A, B] and sim.session_clients == 0 and session_http.calls == []


def test_gated_link_is_downloaded_through_the_session_document(harness_factory, base_media, audit_env, caplog):
    h, sim, session_http = session_app(harness_factory, base_media, audit_env)
    sim.http.roots[A] = gated(A)
    session_http.roots[A] = make_root("reel", A)
    sim.add("image", B)
    with caplog.at_level("DEBUG"):
        batch = create_links(h, [link(A, "reel"), link(B)])
        h.run_all()
    done = h.batch(batch["id"])
    assert (done["status"], done["found"], done["error_code"]) == ("completed", 2, None)
    assert [(r["status"], r["items_found"]) for r in link_rows(h, batch["id"])] == [("resolved", 1), ("resolved", 1)]
    assert sim.calls == [A, B] and session_http.calls == [A] and sim.session_clients == 1
    assert session_http.paths == [f"/reels/{A}/"], "o endereço do navegador logado (/reels/), nunca GraphQL"
    assert "Publicação (documento com a sessão): outcome=ok" in caplog.text and "via=session_document" in caplog.text
    assert_clean(caplog.text, A, B)
    check_invariants(h)


def test_gated_link_still_gated_with_the_session_fails_only_that_link(harness_factory, base_media, audit_env,
                                                                      caplog):
    h, sim, session_http = session_app(harness_factory, base_media, audit_env)
    sim.http.roots[A] = gated(A)
    session_http.roots[A] = session_page(None, noise=2)   # `xig_fetch_media` nulo: continua restrita
    sim.add("image", B)
    with caplog.at_level("DEBUG"):
        batch = create_links(h, [link(A), link(B)])
        h.run_all()
    rows = link_rows(h, batch["id"])
    assert [(r["status"], r["error_code"]) for r in rows] == [("failed", "content_unavailable"), ("resolved", None)]
    assert rows[0]["error_message"].startswith(MSG_WITH_SESSION)
    assert session_http.calls == [A] and not h.ctx.batches.links_hold(batch["id"])
    assert batch["id"] in h.ctx.links_session_off, "falha de formato pela sessão desliga a sessão no lote"
    assert "detail=post_gated_with_session" in caplog.text
    assert_clean(caplog.text, A, B)


def test_refused_session_attempt_pauses_without_retry_and_turns_the_session_off(harness_factory, base_media,
                                                                                 audit_env, caplog):
    """429 na tentativa com a sessão: pausa do Instagram (a política existente), nenhuma nova tentativa com a sessão
    e, depois da pausa, os links seguintes do lote só pelo documento público (os restritos falham explicando)."""
    h, sim, session_http = session_app(harness_factory, base_media, audit_env)
    sim.http.roots[A] = gated(A)
    sim.http.roots[C] = gated(C)
    sim.add("image", B)
    session_http.roots[A] = make_root("reel", A)
    session_http.roots[C] = make_root("reel", C)
    session_http.script[A] = [document(429, "")]
    with caplog.at_level("DEBUG"):
        batch = create_links(h, [link(A), link(B), link(C)])
        h.run_imports()
        assert listing_state(h, batch["id"]) == "paused" and h.ctx.worker.downloads_paused() is True
        assert [r["status"] for r in link_rows(h, batch["id"])] == ["pending"] * 3, "o link fica pendente"
        assert session_http.calls == [A] and sim.calls == [A], "sem retry e sem pedir o link seguinte"
        assert batch["id"] in h.ctx.links_session_off
        assert "[stage=session_document status=429 detail=http_429 content_type=text/html size=0 path=/p/]" in (
            caplog.text)
        assert h.ctx.worker.clear_pause() is True
        h.run_all()
    rows = link_rows(h, batch["id"])
    assert [(r["status"], r["error_code"]) for r in rows] == [
        ("failed", "content_unavailable"), ("resolved", None), ("failed", "content_unavailable")]
    for row in (rows[0], rows[2]):
        assert "recusou a tentativa com a sessão salva" in row["error_message"]
    assert sim.calls == [A, A, B, C] and session_http.calls == [A], "nenhuma outra tentativa com a sessão"
    assert sim.session_clients == 1
    assert_clean(caplog.text, A, B, C)


def test_forbidden_session_attempt_arms_the_hold_and_the_resume_skips_the_session(harness_factory, base_media,
                                                                                  audit_env):
    h, sim, session_http = session_app(harness_factory, base_media, audit_env)
    sim.http.roots[A] = gated(A)
    sim.add("image", B)
    session_http.roots[A] = make_root("reel", A)
    session_http.script[A] = [document(403, "<html>Forbidden</html>")]
    batch = create_links(h, [link(A), link(B)])
    h.run_all()
    row = h.ctx.batches.get(batch["id"])
    assert (row["listing_state"], row["error_code"], row["listing_retry_at"]) == (
        "needs_attention", "access_refused", None)
    assert h.ctx.batches.links_hold(batch["id"]) and sim.calls == [A] and session_http.calls == [A]
    assert "stage=session_document" in row["error_message"]
    assert h.client.post(f"/api/batches/{batch['id']}/resume-listing").status_code == 200
    h.run_all()
    rows = link_rows(h, batch["id"])
    assert [(r["status"], r["error_code"]) for r in rows] == [("failed", "content_unavailable"), ("resolved", None)]
    assert session_http.calls == [A], "a retomada não tenta a sessão de novo"
    assert h.batch(batch["id"])["status"] == "completed"


def test_login_redirect_on_the_session_attempt_is_a_verification_pause(harness_factory, base_media, audit_env):
    h, sim, session_http = session_app(harness_factory, base_media, audit_env)
    sim.http.roots[A] = gated(A)
    session_http.script[A] = [document(302, "", location="/accounts/login/")]
    batch = create_links(h, [link(A)])
    h.run_imports()
    assert listing_state(h, batch["id"]) == "paused" and h.ctx.worker.downloads_paused() is True
    assert h.ctx.worker.current_pause_scope() == "session"
    assert session_http.calls == [A] and batch["id"] in h.ctx.links_session_off
    assert [r["status"] for r in link_rows(h, batch["id"])] == ["pending"]


def test_without_a_saved_session_the_gated_link_asks_for_it(harness_factory, base_media, audit_env):
    session_http = ScriptedHttp(logged=True)
    sim = WebSim(base_media, session=False, session_http=session_http)
    h, sim = make_app(harness_factory, base_media, audit_env, sim=sim)
    sim.http.roots[A] = gated(A)
    batch = create_links(h, [link(A)])
    h.run_all()
    [row] = link_rows(h, batch["id"])
    assert (row["status"], row["error_code"]) == ("failed", "content_unavailable")
    assert "Salve a sessão em Configurações" in row["error_message"]
    assert sim.session_clients == 0 and session_http.calls == []


def test_pause_refusing_the_session_get_keeps_the_link_pending(harness_factory, base_media, audit_env):
    """O portão recusa a admissão (pausa publicada entre as duas requisições): nada foi enviado, o link segue
    pendente e a sessão continua liberada para ele."""
    h, sim, session_http = session_app(harness_factory, base_media, audit_env)
    sim.http.roots[A] = gated(A)
    session_http.roots[A] = make_root("reel", A)
    session_http.script[A] = [InstagramPaused("instagram")]
    batch = create_links(h, [link(A)])
    h.run_all()
    # O trecho recusado pelo portão parou com o link pendente; o trecho seguinte pediu o link de novo.
    assert sim.calls == [A, A] and session_http.calls == [A, A]
    assert batch["id"] not in h.ctx.links_session_off
    assert [r["status"] for r in link_rows(h, batch["id"])] == ["resolved"]
    assert h.batch(batch["id"])["status"] == "completed"


def test_refresh_of_a_batch_with_the_session_off_does_not_use_it(harness_factory, base_media, audit_env):
    h, sim, session_http = session_app(harness_factory, base_media, audit_env)
    sim.add("image", A)
    sim.cdn.expired = {"oh=FAKE_OH"}
    batch = create_links(h, [link(A)])
    h.run_imports()
    h.ctx.links_session_off.add(batch["id"])
    sim.http.roots[A] = gated(A)   # na renovação a publicação passou a exigir login
    h.run_jobs()
    assert sim.calls == [A, A] and session_http.calls == []
    assert {(j["status"], j["error_code"]) for j in h.ctx.jobs.for_batch(batch["id"])} == {
        ("failed", "content_unavailable")}


def test_session_error_from_the_provider_is_not_a_link_error_for_a_scripted_refusal(harness_factory, base_media,
                                                                                     audit_env):
    """Erro já classificado vindo do cliente com a sessão (ex.: transporte) também desliga a sessão no lote."""
    h, sim, session_http = session_app(harness_factory, base_media, audit_env)
    sim.http.roots[A] = gated(A)
    session_http.script[A] = [InstagramWebError("network_error", "FAKE", retryable=True, stage="http")]
    batch = create_links(h, [link(A)])
    h.run_imports()
    assert [r["status"] for r in link_rows(h, batch["id"])] == ["pending"]
    assert batch["id"] in h.ctx.links_session_off and session_http.calls == [A]


def test_session_format_failure_turns_the_session_off_without_tripping_links_failing(harness_factory, base_media,
                                                                                    audit_env, caplog):
    """A 1a falha de formato do documento logado desliga a sessão; ela não conta no disjuntor, então os restritos
    seguintes (sem sessão) não param o lote antes do link público."""
    h, sim, session_http = session_app(harness_factory, base_media, audit_env)
    for code in (A, C, D):
        sim.http.roots[code] = gated(code)
        session_http.roots[code] = "<html><body>formato FAKE desconhecido</body></html>"
    sim.add("image", B)
    with caplog.at_level("DEBUG"):
        batch = create_links(h, [link(A), link(C), link(D), link(B)])
        h.run_all()
    rows = link_rows(h, batch["id"])
    assert [(r["status"], r["error_code"]) for r in rows] == [
        ("failed", "contract_changed"), ("failed", "content_unavailable"), ("failed", "content_unavailable"),
        ("resolved", None)]
    assert "recusou a tentativa com a sessão salva" in rows[1]["error_message"]
    assert session_http.calls == [A] and sim.calls == [A, C, D, B]
    assert batch["id"] in h.ctx.links_session_off
    done = h.batch(batch["id"])
    assert done["status"] == "completed" and done["error_code"] != "links_failing"
    assert "detail=post_envelope_missing" in caplog.text
    assert_clean(caplog.text, A, B, C, D)


def test_p_link_redirected_to_reels_is_followed_once(harness_factory, base_media, audit_env):
    h, sim, session_http = session_app(harness_factory, base_media, audit_env)
    sim.http.roots[A] = gated(A)
    session_http.roots[A] = make_root("reel", A)
    session_http.script[A] = [document(302, "", location=f"/reels/{A}/")]
    batch = create_links(h, [link(A)])
    h.run_all()
    assert session_http.paths == [f"/p/{A}/", f"/reels/{A}/"]
    assert h.batch(batch["id"])["status"] == "completed"


def test_restricted_links_after_the_session_is_off_do_not_trip_links_failing(harness_factory, base_media,
                                                                            audit_env):
    """Restrito com falha de formato pela sessão + 3 restritos (sessão desligada) + 1 público: o público é
    importado e o lote não para com `links_failing`."""
    e = "EEEEEgated5"
    h, sim, session_http = session_app(harness_factory, base_media, audit_env)
    codes = (A, C, D, e)
    for code in codes:
        sim.http.roots[code] = gated(code)
    session_http.roots[A] = "<html><body>formato FAKE desconhecido</body></html>"
    sim.add("image", B)
    batch = create_links(h, [link(code) for code in codes] + [link(B)])
    h.run_all()
    rows = link_rows(h, batch["id"])
    assert [r["status"] for r in rows] == ["failed"] * 4 + ["resolved"]
    assert [r["error_code"] for r in rows] == ["contract_changed"] + ["content_unavailable"] * 3 + [None]
    assert session_http.calls == [A] and sim.calls == [A, C, D, e, B]
    done = h.batch(batch["id"])
    assert done["status"] == "completed" and done["error_code"] != "links_failing"
