"""Auditoria da importação por link: bordas extras (duplicidade entre origens, dependentes de job primário,
resposta trocada que cai em mídia já existente, dono ausente, 429 do CDN). Pilha web real simulada. Offline."""

from __future__ import annotations

import pytest

from audit_links_helpers import audit_env, base_media, check_invariants, make_app, storage_leftovers  # noqa: F401
from import_links_fakes import create_links, link, link_rows
from media_fakes import remote

P, R, Q = "PPPPPpppp66", "RRRRRrrrr77", "QQQQQqqqq88"


def media_count(h):
    return h.ctx.db.scalar("SELECT COUNT(*) FROM media")


def test_link_first_then_profile_import_of_the_same_post_is_skipped(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("image", P)
    create_links(h, [link(P)])
    h.run_all()
    assert media_count(h) == 1 and len(sim.cdn.hits) == 1
    sim.profile.items = [remote(P, 0, profile="dono_fake")]
    batch = h.import_profile(profile="dono_fake")
    h.run_all()
    [job] = h.ctx.jobs.for_batch(batch["id"])
    assert (job["status"], job["is_duplicate"], job["duplicate_reason"]) == ("completed", 1, "source_id")
    assert media_count(h) == 1 and len(sim.cdn.hits) == 1
    check_invariants(h)


@pytest.mark.parametrize("how", ["cancelled", "failed"])
def test_dependent_job_downloads_alone_when_the_primary_batch_does_not_finish(harness_factory, base_media, audit_env,
                                                                              how):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("image", P)
    first = create_links(h, [link(P)])
    second = create_links(h, [link(P)])
    h.run_imports()
    [dependent] = h.ctx.jobs.for_batch(second["id"])
    assert dependent["duplicate_reason"] == "duplicate_in_flight"
    if how == "cancelled":
        assert h.client.post(f"/api/batches/{first['id']}/cancel").status_code == 200
    else:
        sim.cdn.rules = [(P, b"<html>erro</html>")]   # o primário falha (assinatura inválida), sem nova tentativa
    h.run_jobs()
    h.run_jobs()
    sim.cdn.rules = []
    h.run_jobs()
    [dependent] = h.ctx.jobs.for_batch(second["id"])
    if how == "failed":
        assert dependent["status"] == "failed" and dependent["error_code"] == "download_failed", dependent   # baixou sozinho e também recebeu HTML
    else:
        assert dependent["status"] == "completed" and dependent["media_id"], dependent
        assert media_count(h) == 1 and h.batch(second["id"])["status"] == "completed"
    assert storage_leftovers(h) == []
    check_invariants(h)


def test_different_posts_with_identical_bytes_share_one_original_pergunta(harness_factory, base_media, audit_env):
    """Pergunta: dois posts com bytes idênticos viram UMA linha em media (sha256 único). O 2o post não aparece na
    Biblioteca com o seu shortcode; o job fica concluído como duplicata `sha256`."""
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("image", P)
    sim.add("image", Q)
    same = base_media["image"] + b"\x00same"
    sim.cdn.rules = [("/v/t51.fake/", same)]
    batch = create_links(h, [link(P), link(Q)])
    h.run_all()
    jobs = h.ctx.jobs.for_batch(batch["id"])
    assert [j["status"] for j in jobs] == ["completed", "completed"]
    assert media_count(h) == 1 and sorted(j["duplicate_reason"] or "" for j in jobs) == ["", "sha256"]
    assert h.batch(batch["id"])["status"] == "completed"
    check_invariants(h)


def test_owner_absent_in_the_response_keeps_the_media_without_profile(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    root = sim.add("image", P)
    del root["owner"]
    batch = create_links(h, [link(P)])
    h.run_all()
    [row] = link_rows(h, batch["id"])
    assert row["status"] == "resolved" and row["profile"] is None
    assert h.client.get("/api/media").json()["total"] == 1
    assert h.ctx.db.scalar("SELECT source_profile FROM media") in ("", None)
    check_invariants(h)


def test_wrong_post_answer_that_hits_an_existing_media_does_not_resolve_the_requested_link(harness_factory, base_media,
                                                                                         audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("image", R)
    sim.add("image", P)
    create_links(h, [link(R)])
    h.run_all()
    sim.http.answer_with[P] = R
    batch = create_links(h, [link(P)])
    h.run_all()
    [row] = link_rows(h, batch["id"])
    assert row["status"] == "failed", row
    assert row["error_code"] == "contract_changed" and "outra publicação" in row["error_message"], row
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM media WHERE source_post_id = ?", (P,)) == 0
    assert h.ctx.jobs.for_batch(batch["id"]) == [], "nenhum job criado a partir da resposta trocada"
    with pytest.raises(Exception) as raised:
        sim.downloader.resolve_post(P)
    assert (raised.value.code, raised.value.diagnostic) == ("contract_changed", {"detail": "post_mismatch"})


def test_cdn_429_on_a_media_file_does_not_leave_files_or_media(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("image", P)
    sim.cdn.rules = [(P, 429)]
    batch = create_links(h, [link(P)])
    h.run_all()
    [job] = h.ctx.jobs.for_batch(batch["id"])
    assert media_count(h) == 0 and storage_leftovers(h) == []
    assert job["status"] in ("pending", "failed") and job["attempts"] >= 1, job
    check_invariants(h)
