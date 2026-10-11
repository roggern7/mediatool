"""Regressão da importação por link sobre o DOCUMENTO PÚBLICO (lacunas dos testes do builder): cancelamento durante o
GET, job sem URL de origem reconhecível, caminho preservado na retomada, carrossel colado como /reel/, estratégia
`auto` sem fallback ponta a ponta, setting salvo com o nome da consulta antiga e host de CDN inesperado.
100% offline (conexão de rede proibida), valores FAKE."""

from __future__ import annotations

import pytest

from app.integrations.instagram.composite import CompositeInstagramDownloader
from app.integrations.instagram.validation import post_path_kind
from audit_links_helpers import (  # noqa: F401 - fixtures
    WebSim,
    audit_env,
    base_media,
    check_invariants,
    document,
    make_app,
    storage_leftovers,
)
from import_links_fakes import LinksDownloader, create_links, link, link_rows, photo

A, B, C = "AAAAAaaaa11", "BBBBBbbbb22", "CCCCCcccc33"


def media_count(h) -> int:
    return h.ctx.db.scalar("SELECT COUNT(*) FROM media")


# --------------------------------------------------------------------------- post_path_kind (puro)


@pytest.mark.parametrize("url, expected", [
    (f"https://www.instagram.com/p/{A}/", "p"),
    (f"https://www.instagram.com/reel/{A}/", "reel"),
    (f"https://www.instagram.com/reels/{A}/", "reel"),
    (f"instagram.com/dono_fake/reel/{A}/?igsh=X", "reel"),
    (f"https://www.instagram.com/P/{A}/", "p"),
    ("", None),
    ("lixo", None),
    ("https://www.instagram.com/dono_fake/", None),
    (f"https://www.instagram.com/stories/dono_fake/{A}/", None),
])
def test_post_path_kind(url, expected):
    assert post_path_kind(url) == expected


# --------------------------------------------------------------------------- cancelamento durante o GET


def test_cancel_during_the_public_get_creates_no_job_and_requests_nothing_else(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    for code in (A, B):
        sim.add("carousel", code)
    batch = create_links(h, [link(A), link(B, "reel")])

    def cancel_inside_get(code: str) -> None:
        if code == A:
            assert h.client.post(f"/api/batches/{batch['id']}/cancel").status_code == 200

    sim.http.on_call = cancel_inside_get
    h.run_all()
    h.run_all()
    assert sim.calls == [A], "o GET em andamento termina; nenhum outro link é pedido"
    done = h.batch(batch["id"])
    assert done["status"] == "cancelled"
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM jobs") == 0 and media_count(h) == 0
    assert sim.cdn.hits == [] and storage_leftovers(h) == []
    check_invariants(h)


def test_cancel_during_the_refresh_get_downloads_nothing_with_the_fresh_urls(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("reel", A)
    sim.cdn.expired = {"oh=FAKE_OH"}
    batch = create_links(h, [link(A, "reel")])
    h.run_imports()

    def cancel_inside_refresh(code: str) -> None:
        if sim.http.counts[code] == 2:
            assert h.client.post(f"/api/batches/{batch['id']}/cancel").status_code == 200

    sim.http.on_call = cancel_inside_refresh
    h.run_jobs()
    h.run_jobs()
    assert sim.calls == [A, A]
    assert not [u for u in sim.cdn.hits if "FRESH" in u], "URL renovada de job cancelado não é baixada"
    assert media_count(h) == 0 and storage_leftovers(h) == []
    assert all(j["status"] != "completed" for j in h.ctx.jobs.for_batch(batch["id"]))
    check_invariants(h)


# --------------------------------------------------------------------------- job sem URL de origem reconhecível


@pytest.mark.parametrize("source_url", [None, "", "lixo-sem-caminho", "https://www.instagram.com/dono_fake/"])
def test_refresh_of_a_job_without_a_recognizable_source_url_fails_safely(harness_factory, base_media, audit_env,
                                                                         source_url):
    """Job antigo (ou gravado por outro provider) sem /p/ ou /reel/: o provider recusa sem requisição (nunca escolhe o
    tipo sozinho), o job falha com código claro, o lote não fica travado e nada é importado."""
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("image", A)
    sim.cdn.expired = {"oh=FAKE_OH"}
    batch = create_links(h, [link(A)])
    h.run_imports()
    h.ctx.db.execute("UPDATE jobs SET source_url = ?", (source_url,))
    h.run_jobs()
    h.run_jobs()
    assert sim.calls == [A], "nenhum GET de renovação sem tipo de caminho conhecido"
    [job] = h.ctx.jobs.for_batch(batch["id"])
    assert job["status"] == "failed" and job["error_code"], job
    assert job["error_code"] in ("invalid_input", "content_unavailable", "unknown"), job["error_code"]
    assert not h.ctx.batches.links_hold(batch["id"]) and media_count(h) == 0
    check_invariants(h)


# --------------------------------------------------------------------------- caminho ao retomar / carrossel como /reel/


def test_resume_after_login_redirect_keeps_the_path_kind_of_each_link(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("reel", A)
    sim.add("image", B)
    sim.http.script[A] = [document(302, "", location="/accounts/login/")]
    batch = create_links(h, [f"https://www.instagram.com/reels/{A}/", link(B)])
    h.run_all()
    assert sim.http.paths == [f"/reel/{A}/"]
    assert h.client.post(f"/api/batches/{batch['id']}/resume-listing").status_code == 200
    h.run_all()
    h.run_all()
    assert sim.http.paths == [f"/reel/{A}/", f"/reel/{A}/", f"/p/{B}/"]
    assert h.batch(batch["id"])["status"] == "completed" and media_count(h) == 2
    check_invariants(h)


def test_carousel_pasted_as_reel_renews_on_the_path_of_its_own_source_url(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("carousel", C)
    sim.cdn.expired = {"oh=FAKE_OH"}
    batch = create_links(h, [link(C, "reel")])
    h.run_all()
    h.run_all()
    assert sim.http.paths == [f"/reel/{C}/", f"/p/{C}/"], "a renovação segue a URL de origem do carrossel (/p/)"
    assert h.batch(batch["id"])["status"] == "completed" and media_count(h) == 4
    check_invariants(h)


# --------------------------------------------------------------------------- estratégia auto sem fallback, ponta a ponta


def test_auto_strategy_never_asks_the_legacy_downloader_when_the_document_has_no_envelope(
        harness_factory, base_media, audit_env):
    sim = WebSim(base_media)
    legacy = LinksDownloader(posts={A: photo(A), B: photo(B)})
    audit_env(sim.cdn)
    sim.http.roots[A] = "<html><body>Entrar</body></html>"
    sim.add("image", B)
    h = harness_factory(downloader=CompositeInstagramDownloader(sim.downloader, legacy, "auto"))
    batch = create_links(h, [link(A), link(B)])
    h.run_all()
    h.run_all()
    assert legacy.calls == [] and legacy.downloads == [], "falha do documento público não vira consulta do gallery-dl"
    rows = link_rows(h, batch["id"])
    assert [(r["status"], r["error_code"]) for r in rows] == [("failed", "contract_changed"), ("resolved", None)]
    assert sim.calls == [A, B] and media_count(h) == 1
    check_invariants(h)


# --------------------------------------------------------------------------- setting salvo com o nome antigo


def test_saved_setting_with_the_old_query_name_is_kept_and_does_not_affect_link_import(
        harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    old = {"PolarisPostActionLoadPostQueryQuery": "8845758582119845"}
    h.ctx.app_settings.set("instagram_web_doc_ids", old)
    body = h.client.get("/api/settings").json()
    assert body["instagram_web_doc_ids"] == old
    assert h.client.put("/api/settings", json={"instagram_web_doc_ids": old}).status_code == 200
    sim.add("reel", A)
    batch = create_links(h, [link(A, "reel")])
    h.run_all()
    assert h.batch(batch["id"])["status"] == "completed" and sim.http.paths == [f"/reel/{A}/"]
    assert h.ctx.app_settings.get("instagram_web_doc_ids") == old
    check_invariants(h)


# --------------------------------------------------------------------------- CDN inesperado


def test_media_url_on_an_unexpected_host_fails_the_job_without_any_cdn_request(harness_factory, base_media, audit_env):
    """Host do CDN sem login é NÃO VERIFICADO: fora de .cdninstagram.com/.fbcdn.net o download recusa (invalid_url)."""
    h, sim = make_app(harness_factory, base_media, audit_env)
    root = sim.add("reel", A)
    for version in root["video_versions"]:
        version["url"] = version["url"].replace("video-fake.fbcdn.net", "cdn.exemplo-fake.test")
    batch = create_links(h, [link(A, "reel")])
    h.run_all()
    h.run_all()
    [job] = h.ctx.jobs.for_batch(batch["id"])
    assert job["status"] == "failed" and job["error_code"], job
    assert sim.cdn.hits == [] and media_count(h) == 0 and storage_leftovers(h) == []
    check_invariants(h)
