"""Importação por link sobre o DOCUMENTO PÚBLICO da publicação: o runner e a renovação de URL informam o tipo do
caminho (/p/ ou /reel/), a sessão do dono não é usada nem "comprovada" por esse caminho, a recusa do documento arma
a espera do lote e o documento sem o envelope é erro só daquele link. 100% offline (conexão de rede proibida), valores
FAKE."""

from __future__ import annotations

from app.integrations.instagram.composite import CompositeInstagramDownloader
from app.integrations.instagram.web.errors import InstagramWebError
from app.services.instagram_recovery import AUTH_FAIL_STREAK_KEY
from audit_links_helpers import (  # noqa: F401 - fixtures
    audit_env,
    base_media,
    check_invariants,
    document,
    make_app,
    storage_leftovers,
)
from import_links_fakes import LinksDownloader, create_links, link, link_rows, links_app, listing_state, photo, reel

A, B, C, D, E = "AAAAAaaaa11", "BBBBBbbbb22", "CCCCCcccc33", "DDDDDdddd44", "EEEEEeeee55"


def expire(dl: LinksDownloader, *codes: str) -> None:
    dl.expired.update(item.media_url for code in codes for item in dl.posts[code])


def streak(h) -> str | None:
    return h.ctx.app_settings.get_state(AUTH_FAIL_STREAK_KEY)


# --------------------------------------------------------------------------- rede zero, ponta a ponta


def test_link_batch_runs_end_to_end_on_the_public_document_with_no_network(harness_factory, base_media, audit_env,
                                                                           caplog):
    """Item 4 do "done": com conexão de rede proibida (audit_env), o lote por link resolve pelo documento HTML,
    baixa pelo CDN simulado e conclui. O path pedido segue o tipo do link colado."""
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("reel", A)
    sim.add("image", B)
    sim.add("carousel", C)
    with caplog.at_level("DEBUG"):
        batch = create_links(h, [f"https://www.instagram.com/reels/{A}/?igsh=FAKEQUERY", link(B), link(C, "reel")])
        h.run_all()
    assert sim.http.paths == [f"/reel/{A}/", f"/p/{B}/", f"/reel/{C}/"], "/reels/ colado vira /reel/"
    done = h.batch(batch["id"])
    assert (done["status"], done["found"], done["error_code"]) == ("completed", 6, None)
    assert done["progress"]["downloaded"] == 6 and len(sim.cdn.hits) == 6
    assert [(r["status"], r["items_found"]) for r in link_rows(h, batch["id"])] == [
        ("resolved", 1), ("resolved", 1), ("resolved", 4)]
    assert "Publicação (documento público): outcome=ok" in caplog.text
    for secret in (A, B, C, "FAKE_OH", "cdninstagram", "fbcdn"):
        assert secret not in caplog.text, secret
    assert storage_leftovers(h) == []
    check_invariants(h)


# --------------------------------------------------------------------------- tipo do caminho


def test_runner_passes_the_path_kind_of_the_stored_link(harness_factory):
    h, dl = links_app(harness_factory, {A: reel(A), B: photo(B), C: photo(C)})
    create_links(h, [f"instagram.com/reels/{A}/", link(B), f"https://www.instagram.com/dono_fake/reel/{C}/?x=1"])
    h.run_imports()
    assert dl.calls == [A, B, C] and dl.path_kinds == ["reel", "p", "reel"]


def test_refresh_passes_the_path_kind_of_the_job_source_url(harness_factory):
    """A renovação usa a URL de origem do job (o /reel/ vem da resposta), não o link colado."""
    h, dl = links_app(harness_factory, {A: reel(A), B: photo(B)})
    create_links(h, [link(A), link(B, "reel")])   # colados "ao contrário" do que a publicação é
    h.run_imports()
    assert dl.path_kinds == ["p", "reel"]
    expire(dl, A, B)
    h.run_jobs()
    assert dl.calls == [A, B, A, B] and dl.path_kinds[2:] == ["reel", "p"]


def test_real_provider_refresh_asks_the_reel_path_even_for_a_link_pasted_as_p(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("reel", A)
    sim.cdn.expired = {"oh=FAKE_OH"}
    batch = create_links(h, [link(A)])
    h.run_all()
    h.run_all()
    assert sim.http.paths == [f"/p/{A}/", f"/reel/{A}/"]
    assert h.batch(batch["id"])["status"] == "completed"


# --------------------------------------------------------------------------- a sessão do dono não é "comprovada"


def test_authenticated_provider_still_notes_auth_success(harness_factory):
    h, dl = links_app(harness_factory, {A: photo(A)})
    assert dl.resolve_post_authenticated is True
    h.ctx.app_settings.set_state(AUTH_FAIL_STREAK_KEY, "2")
    create_links(h, [link(A)])
    h.run_imports()
    assert streak(h) is None, "publicação entregue por consulta autenticada zera o contador"
    h.ctx.app_settings.set_state(AUTH_FAIL_STREAK_KEY, "2")
    expire(dl, A)
    h.run_jobs()
    assert dl.calls == [A, A] and streak(h) is None, "a renovação autenticada também"


def test_anonymous_resolution_does_not_note_auth_success(harness_factory):
    class Anonymous(LinksDownloader):
        resolve_post_authenticated = False

    downloader = Anonymous(posts={A: photo(A)})
    h = harness_factory(downloader=downloader)
    h.ctx.app_settings.set_state(AUTH_FAIL_STREAK_KEY, "2")
    batch = create_links(h, [link(A)])
    h.run_imports()
    assert downloader.calls == [A] and streak(h) == "2", "o documento público não prova a sessão do dono"
    expire(downloader, A)
    h.run_jobs()
    assert downloader.calls == [A, A] and streak(h) == "2", "nem a renovação da publicação"
    assert h.batch(batch["id"])["status"] == "completed"


def test_real_web_provider_never_notes_auth_success(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    assert sim.downloader.resolve_post_authenticated is False
    sim.add("image", A)
    sim.cdn.expired = {"oh=FAKE_OH"}
    h.ctx.app_settings.set_state(AUTH_FAIL_STREAK_KEY, "2")
    batch = create_links(h, [link(A)])
    h.run_all()
    h.run_all()
    assert sim.calls == [A, A] and h.batch(batch["id"])["status"] == "completed"
    assert streak(h) == "2"


def test_composite_reports_the_provider_that_answers_resolve_post():
    web, legacy = LinksDownloader(), LinksDownloader()
    web.resolve_post_authenticated = False
    assert CompositeInstagramDownloader(web, legacy, "web").resolve_post_authenticated is False
    assert CompositeInstagramDownloader(web, legacy, "auto").resolve_post_authenticated is False
    assert CompositeInstagramDownloader(web, legacy, "gallery_dl").resolve_post_authenticated is True


# --------------------------------------------------------------------------- recusa do documento: espera do lote


def test_login_redirect_arms_the_hold_and_no_other_request_happens(harness_factory, base_media, audit_env, caplog):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("image", A)
    sim.add("image", B)
    sim.http.script[A] = [document(302, "", location="/accounts/login/")]
    with caplog.at_level("WARNING"):
        batch = create_links(h, [link(A), link(B)])
        h.run_all()
        h.run_all()
    assert sim.calls == [A], "redirect não é seguido, sem nova tentativa e o link seguinte não é pedido"
    row = h.ctx.batches.get(batch["id"])
    assert (row["listing_state"], row["error_code"], row["listing_retry_at"]) == (
        "needs_attention", "access_refused", None)
    assert h.ctx.batches.links_hold(batch["id"]), "a recusa em espera do lote foi armada"
    assert "stage=post_document status=302 redirect=redirect_login detail=redirect_login" in row["error_message"]
    assert "path=/p/" in row["error_message"] and A not in row["error_message"] and A not in caplog.text
    assert [r["status"] for r in link_rows(h, batch["id"])] == ["pending", "pending"]
    assert not h.ctx.worker.downloads_paused(), "não é pausa do Instagram nem problema da sessão"
    assert h.client.post(f"/api/batches/{batch['id']}/resume-listing").status_code == 200
    h.run_all()
    h.run_all()
    assert sim.calls == [A, A, B] and h.batch(batch["id"])["status"] == "completed"
    check_invariants(h)


def test_refused_refresh_of_the_document_arms_the_hold(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("image", A)
    sim.add("image", B)
    sim.cdn.expired = {"oh=FAKE_OH"}
    batch = create_links(h, [link(A), link(B)])
    h.run_imports()
    sim.http.script[A] = [document(302, "", location="/accounts/login/")]
    h.run_jobs()
    h.run_jobs()
    assert sim.calls == [A, B, A], "a renovação recusada segura as demais consultas do lote"
    assert h.ctx.batches.links_hold(batch["id"])
    assert {(j["status"], j["error_code"]) for j in h.ctx.jobs.for_batch(batch["id"])} == {
        ("failed", "access_refused")}


# --------------------------------------------------------------------------- documento sem o envelope: erro do link


def test_document_without_the_envelope_fails_only_that_link(harness_factory, base_media, audit_env, caplog):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.http.roots[A] = "<html><body>Entrar</body></html>"   # ex.: tela de login respondida com 200
    sim.add("image", B)
    with caplog.at_level("WARNING"):
        batch = create_links(h, [link(A), link(B)])
        h.run_all()
    rows = link_rows(h, batch["id"])
    assert [(r["status"], r["error_code"]) for r in rows] == [("failed", "contract_changed"), ("resolved", None)]
    assert "Nada foi importado deste link" in rows[0]["error_message"]
    assert (f"Lote {batch['id']}: link 1 não pôde ser importado (contract_changed) [stage=post_document "
            "detail=post_envelope_missing path=/p/]") in caplog.text
    assert sim.calls == [A, B] and not h.ctx.batches.links_hold(batch["id"])
    done = h.batch(batch["id"])
    assert (done["status"], done["error_message"]) == ("completed", "1 de 2 links não puderam ser importados.")
    check_invariants(h)


def test_three_documents_without_the_envelope_in_a_row_stop_the_batch(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    for code in (A, B, C):
        sim.http.roots[code] = "<html><body>Entrar</body></html>"
    sim.add("image", D)
    sim.add("image", E)
    batch = create_links(h, [link(code) for code in (A, B, C, D, E)])
    h.run_all()
    assert sim.calls == [A, B, C], "3 links seguidos falhos: nenhum outro é pedido"
    stopped = h.batch(batch["id"])
    assert (stopped["listing_state"], stopped["error_code"]) == ("needs_attention", "links_failing")
    assert [r["status"] for r in link_rows(h, batch["id"])] == ["failed"] * 3 + ["pending"] * 2


def test_gated_post_fails_the_link_without_arming_the_hold(harness_factory, base_media, audit_env):
    """Sem sessão salva (com a sessão: test_import_links_session_document.py)."""
    from audit_links_helpers import WebSim
    from post_document_fixtures import data_sjs_script, envelope, media_payload, post_html

    h, sim = make_app(harness_factory, base_media, audit_env, sim=WebSim(base_media, session=False))
    sim.http.roots[A] = post_html([data_sjs_script(media_payload(envelope(None, code=A)))], noise=2)
    sim.add("image", B)
    batch = create_links(h, [link(A), link(B)])
    h.run_all()
    rows = link_rows(h, batch["id"])
    assert [(r["status"], r["error_code"]) for r in rows] == [("failed", "content_unavailable"), ("resolved", None)]
    assert "exige login ou está restrita" in rows[0]["error_message"]
    assert not h.ctx.batches.links_hold(batch["id"]) and listing_state(h, batch["id"]) == "done"
    assert sim.calls == [A, B]


def test_scripted_error_keeps_its_classification(harness_factory, base_media, audit_env):
    """`rate_limited` do documento (429) pausa o Instagram e mantém o link pendente, como antes."""
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("image", A)
    sim.http.script[A] = [InstagramWebError("rate_limited", "FAKE", retryable=True, stage="post_document",
                                            diagnostic={"detail": "http_429"})]
    batch = create_links(h, [link(A)])
    h.run_imports()
    assert listing_state(h, batch["id"]) == "paused" and h.ctx.worker.downloads_paused() is True
    assert [r["status"] for r in link_rows(h, batch["id"])] == ["pending"]
