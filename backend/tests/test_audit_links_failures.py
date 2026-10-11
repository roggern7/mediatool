"""Auditoria da importação por link, item 8: falhas de download, URL expirada com renovação por publicação,
retry-failed, disjuntor, repetição de erro do ambiente e pausa do Instagram. Pilha web real simulada. Offline."""

from __future__ import annotations

import httpx

from app.integrations.instagram.downloader import InstagramError
from app.integrations.instagram.web.errors import InstagramWebError
from audit_links_helpers import (  # noqa: F401 - fixtures
    audit_env,
    base_media,
    check_invariants,
    document,
    make_app,
    storage_leftovers,
)
from import_links_fakes import create_links, link, link_rows, listing_state

A, B, C, D, E, P = "AAAAAaaaa11", "BBBBBbbbb22", "CCCCCcccc33", "DDDDDdddd44", "EEEEEeeee55", "PPPPPpppp66"


def media_count(h):
    return h.ctx.db.scalar("SELECT COUNT(*) FROM media")


def jobs_of(h, batch):
    return h.ctx.jobs.for_batch(batch["id"])


def make_due(h):
    h.ctx.db.execute("UPDATE jobs SET next_attempt_at = NULL")


# --------------------------------------------------------------------------- falhas de download


def test_non_retryable_download_failures_fail_only_that_job(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env, max_download_bytes=100_000)
    sim.add("carousel", C)   # 4 itens: pcar1a ok, pcar1b = HTML (assinatura), pcar1c = grande demais, pcar1d ok
    sim.cdn.rules = [("pcar1b", b"<html><body>erro</body></html>"), ("pcar1c", b"\xff\xd8\xff\xe0" + b"0" * 150_000)]
    batch = create_links(h, [link(C)])
    h.run_all()
    h.run_all()
    jobs = {j["source_media_index"]: j for j in jobs_of(h, batch)}
    assert [jobs[i]["status"] for i in range(4)] == ["completed", "failed", "failed", "completed"], jobs
    assert jobs[1]["error_code"] and jobs[2]["error_code"]
    assert media_count(h) == 2
    done = h.batch(batch["id"])
    assert (done["status"], done["progress"]["downloaded"], done["progress"]["failed"]) == ("completed", 2, 2) \
        or done["status"] in ("completed", "failed"), done
    assert storage_leftovers(h) == [], "nenhum .part/arquivo temporário depois das recusas"
    check_invariants(h)


def test_truncated_download_is_a_retryable_failure_and_retry_failed_recovers_it(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("image", P)
    full = sim.cdn.base["image"]

    def truncated(request):   # Content-Length promete mais do que chega
        return httpx.Response(200, content=full[: len(full) // 2], headers={"content-length": str(len(full))})

    sim.cdn.rules = [("/v/t51.fake/" + P, truncated)]
    batch = create_links(h, [link(P)])
    h.run_all()
    [job] = jobs_of(h, batch)
    assert job["status"] != "completed" and media_count(h) == 0
    assert job["attempts"] == 1, "falha de rede é tentada de novo mais tarde"
    # esgota as tentativas
    for _ in range(4):
        make_due(h)
        h.run_jobs()
    [job] = jobs_of(h, batch)
    assert job["status"] == "failed", job
    assert storage_leftovers(h) == [] and media_count(h) == 0
    # o servidor volta ao normal e o dono usa "tentar de novo os que falharam"
    sim.cdn.rules = []
    calls_before = list(sim.calls)
    response = h.client.post(f"/api/batches/{batch['id']}/retry-failed")
    assert response.status_code == 200, response.text
    h.run_all()
    h.run_all()
    [job] = jobs_of(h, batch)
    assert job["status"] == "completed", job
    assert media_count(h) == 1
    assert len(sim.calls) - len(calls_before) <= 1, "no máximo uma consulta da publicação para renovar a URL apagada"
    assert h.batch(batch["id"])["status"] == "completed"
    check_invariants(h)


def test_corrupt_body_with_valid_signature_is_accepted_in_download_only_pergunta(harness_factory, base_media, audit_env):
    """Pergunta: no download puro a validação é só por assinatura (magic bytes). Um JPEG cortado (assinatura boa,
    Content-Length coerente) vira original na Biblioteca. Documenta o comportamento atual."""
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("image", P)
    sim.cdn.rules = [(P, b"\xff\xd8\xff\xe0" + b"lixo" * 10)]
    batch = create_links(h, [link(P)])
    h.run_all()
    assert h.batch(batch["id"])["status"] == "completed" and media_count(h) == 1
    check_invariants(h)


# --------------------------------------------------------------------------- URL expirada e renovação


def test_expired_urls_renew_the_whole_carousel_with_one_post_query(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("carousel", C)
    sim.add("image", P)
    sim.cdn.expired = {"oh=FAKE_OH"}   # toda URL original foi recusada pelo CDN; as renovadas (FRESH) servem
    batch = create_links(h, [link(C), link(P)])
    h.run_all()
    h.run_all()
    assert sim.calls == [C, P, C, P], "1 consulta por link + 1 renovação por publicação (não por mídia)"
    assert h.batch(batch["id"])["status"] == "completed" and media_count(h) == 5
    fresh = [u for u in sim.cdn.hits if "FRESH" in u]
    assert len(fresh) == 5 and all("FRESH2" in u for u in fresh)
    check_invariants(h)


def test_renewal_that_keeps_returning_refused_urls_fails_without_a_second_query(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("carousel", C)
    sim.cdn.expired = {"oh="}   # até as renovadas são recusadas
    batch = create_links(h, [link(C)])
    h.run_all()
    h.run_all()
    assert sim.calls == [C, C], "uma renovação para a publicação toda"
    assert {j["status"] for j in jobs_of(h, batch)} == {"failed"} and media_count(h) == 0
    assert len(sim.cdn.hits) == 5, "1 tentativa com a URL velha + 4 com a nova (irmãos já usam a renovada)"
    check_invariants(h)


def test_renewal_access_refused_and_content_unavailable(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("carousel", C)
    sim.add("image", P)
    sim.cdn.expired = {"oh=FAKE_OH"}
    # 2a consulta de C (a renovação): recusada; 2a consulta de P: 404 (publicação apagada)
    batch = create_links(h, [link(C), link(P)])
    h.run_imports()
    sim.http.script[C] = [document(403, "<html>Forbidden</html>")]
    sim.http.script[P] = [document(404, "<html>Página não encontrada</html>")]
    h.run_jobs()
    jobs = jobs_of(h, batch)
    by_post = {}
    for job in jobs:
        by_post.setdefault(job["source_post_id"], []).append(job)
    # Renovação recusada (`access_refused`): o lote espera a ação do dono. Nenhuma outra renovação consulta, nem a de
    # OUTRA publicação (P): no máximo 1 consulta recusada no lote inteiro.
    assert sim.calls.count(C) == 2 and sim.calls.count(P) == 1, "1 consulta recusada no lote; P não é consultada"
    assert all(j["status"] != "completed" for j in jobs), [(j["status"], j["error_code"]) for j in jobs]
    assert {(j["status"], j["error_code"]) for j in by_post[P]} == {("failed", "access_refused")}, by_post[P]
    assert {(j["status"], j["error_code"]) for j in by_post[C]} == {("failed", "access_refused")}, "os 4 irmãos repetem o código sem nova consulta"
    assert media_count(h) == 0 and storage_leftovers(h) == []
    check_invariants(h)
    # Ação do dono: as renovações voltam. C responde e baixa; P vem com 404 (publicação apagada).
    assert h.client.post(f"/api/batches/{batch['id']}/retry-failed").status_code == 200
    h.run_jobs()
    by_post = {}
    for job in jobs_of(h, batch):
        by_post.setdefault(job["source_post_id"], []).append(job)
    assert sim.calls.count(C) == 3 and sim.calls.count(P) == 2, "no máximo 1 renovação por publicação"
    assert {j["status"] for j in by_post[C]} == {"completed"}, by_post[C]
    assert {(j["status"], j["error_code"]) for j in by_post[P]} == {("failed", "content_unavailable")}, by_post[P]
    assert media_count(h) == 4 and storage_leftovers(h) == []
    check_invariants(h)


def test_post_removed_between_listing_and_download_is_content_unavailable(harness_factory, base_media, audit_env):
    """A mídia sumiu da publicação na renovação (outro filho do carrossel continua lá)."""
    h, sim = make_app(harness_factory, base_media, audit_env)
    root = sim.add("carousel", C)
    sim.cdn.expired = {"oh=FAKE_OH"}
    batch = create_links(h, [link(C)])
    h.run_imports()
    trimmed = {**root, "carousel_media": root["carousel_media"][:2], "carousel_media_count": 2}
    sim.http.roots[C] = trimmed
    h.run_jobs()
    states = {j["source_media_index"]: j["status"] for j in jobs_of(h, batch)}
    assert states[0] == "completed" and states[1] == "completed" and states[2] == "failed" and states[3] == "failed", states
    assert sim.calls == [C, C]
    check_invariants(h)


# --------------------------------------------------------------------------- disjuntor e repetição de erro


def test_three_missing_links_in_a_row_trip_the_breaker_and_resume_continues(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    for code in (A, B, C):
        sim.http.roots[code] = None
    sim.add("image", D)
    sim.add("image", E)
    batch = create_links(h, [link(A), link(B), link(C), link(D), link(E)])
    h.run_all()
    assert sim.calls == [A, B, C], "3 falhas seguidas: nenhum outro link é pedido"
    stopped = h.batch(batch["id"])
    assert (stopped["listing_state"], stopped["error_code"]) == ("needs_attention", "links_failing")
    assert [r["status"] for r in link_rows(h, batch["id"])] == ["failed"] * 3 + ["pending"] * 2
    assert h.client.post(f"/api/batches/{batch['id']}/resume-listing").status_code == 200
    h.run_all()
    h.run_all()
    done = h.batch(batch["id"])
    assert (done["status"], done["found"], done["error_message"]) == ("completed", 2, "3 de 5 links não puderam ser importados.")
    assert sim.calls == [A, B, C, D, E] and media_count(h) == 2
    check_invariants(h)


def test_access_refused_twice_on_the_same_link_fails_it_and_keeps_the_batch_stopped(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("image", B)
    refusal = lambda: InstagramWebError("access_refused", "FAKE", stage="post_document",  # noqa: E731
                                        diagnostic={"detail": "http_403"})
    sim.http.script[A] = [refusal(), refusal()]
    batch = create_links(h, [link(A), link(B)])
    h.run_all()
    assert sim.calls == [A] and listing_state(h, batch["id"]) == "needs_attention"
    assert h.ctx.batches.get(batch["id"])["listing_retry_at"] is None, "sem nova tentativa automática"
    h.run_all()
    assert sim.calls == [A], "nada é pedido sozinho"
    assert h.client.post(f"/api/batches/{batch['id']}/resume-listing").status_code == 200
    h.run_all()
    assert sim.calls == [A, A], "um Retomar = uma consulta"
    rows = link_rows(h, batch["id"])
    assert (rows[0]["status"], rows[0]["error_code"], rows[1]["status"]) == ("failed", "access_refused", "pending")
    assert listing_state(h, batch["id"]) == "needs_attention", "o próximo link só sai por outro Retomar"
    assert h.client.post(f"/api/batches/{batch['id']}/resume-listing").status_code == 200
    h.run_all()
    h.run_all()
    assert sim.calls == [A, A, B] and h.batch(batch["id"])["status"] == "completed" and media_count(h) == 1
    check_invariants(h)


def test_rate_limit_in_the_middle_pauses_the_batch_and_the_resume_finishes_everything(harness_factory, base_media,
                                                                                    audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    for code, kind in ((A, "carousel"), (B, "image"), (C, "image")):
        sim.add(kind, code)
    sim.http.script[B] = [document(429)]
    batch = create_links(h, [link(A), link(B), link(C)])
    h.run_imports()
    paused = h.batch(batch["id"])
    assert sim.calls == [A, B] and paused["listing_state"] == "paused" and paused["error_code"] == "rate_limited"
    assert h.ctx.worker.downloads_paused() is True
    hits_in_pause = len(sim.cdn.hits)
    h.run_jobs()
    assert len(sim.cdn.hits) == hits_in_pause == 0, "durante a pausa do Instagram nada é baixado"
    assert h.client.delete("/api/settings/instagram/pause").status_code == 200
    h.run_all()
    h.run_all()
    done = h.batch(batch["id"])
    assert (done["status"], done["found"], done["progress"]["downloaded"]) == ("completed", 6, 6), done
    assert sim.calls == [A, B, B, C]
    check_invariants(h)
