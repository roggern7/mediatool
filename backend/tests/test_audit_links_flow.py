"""Auditoria da importação por link, itens 4 a 7: duplicidade, concorrência/ordem, retomada e cancelamento, com a
pilha web real (GraphQL/CDN simulados) e arquivos sintéticos reais. Offline."""

from __future__ import annotations

from collections import Counter

from audit_links_helpers import audit_env, base_media, check_invariants, make_app, storage_leftovers  # noqa: F401
from conftest import wait_until
from import_links_fakes import create_links, link, link_rows
from media_fakes import remote
from app.integrations.instagram.downloader import InstagramError

A, B, C, D, E, P = "AAAAAaaaa11", "BBBBBbbbb22", "CCCCCcccc33", "DDDDDdddd44", "EEEEEeeee55", "PPPPPpppp66"


def paths(sim, code):
    return [p for p in sim.cdn.hit_paths() if f"/{code}/" in p or f"/{code}_" in p]


def media_count(h):
    return h.ctx.db.scalar("SELECT COUNT(*) FROM media")


# --------------------------------------------------------------------------- 4. duplicidade


def test_post_already_in_library_via_profile_is_not_downloaded_again(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.profile.items = [remote(P, 0, profile="perfil_teste")]
    h.import_profile(profile="perfil_teste")
    h.run_all()
    assert media_count(h) == 1 and len(sim.cdn.hits) == 1
    sim.add("image", P)
    batch = create_links(h, [link(P)])
    h.run_all()
    assert len(sim.cdn.hits) == 1, "nenhum download repetido"
    assert media_count(h) == 1
    [job] = h.ctx.jobs.for_batch(batch["id"])
    assert (job["status"], job["stage"], job["is_duplicate"], job["duplicate_reason"]) == (
        "completed", "skipped", 1, "source_id")
    done = h.batch(batch["id"])
    assert (done["status"], done["found"], done["error_code"]) == ("completed", 1, None)
    assert done["progress"]["skipped"] == 1 and done["progress"]["failed"] == 0
    [row] = link_rows(h, batch["id"])
    assert (row["status"], row["items_found"]) == ("resolved", 1)
    assert h.ctx.db.scalar("SELECT source_profile FROM media") == "perfil_teste", "a mídia antiga não é reescrita"
    assert sim.calls == [P], "a publicação é consultada uma vez (necessário para saber as mídias)"
    check_invariants(h)


def test_carousel_partly_in_library_downloads_only_the_missing_items(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.profile.items = [remote(C, 0, profile="perfil_teste"), remote(C, 1, profile="perfil_teste")]
    h.import_profile(profile="perfil_teste")
    h.run_all()
    before = len(sim.cdn.hits)
    sim.add("carousel", C)
    batch = create_links(h, [link(C)])
    h.run_all()
    assert len(sim.cdn.hits) - before == 2, "só os índices 2 e 3 são baixados"
    assert {p.rsplit("/", 1)[-1].split("_")[0] for p in sim.cdn.hit_paths()[before:]} == {"pcar1c", "pcar1d"}
    done = h.batch(batch["id"])
    assert (done["status"], done["found"]) == ("completed", 4)
    assert done["progress"]["skipped"] == 2 and done["progress"]["downloaded"] + done["progress"]["skipped"] >= 4
    assert sorted(m["source_media_index"] for m in h.ctx.db.fetchall(
        "SELECT source_media_index FROM media WHERE source_post_id = ?", (C,))) == [0, 1, 2, 3]
    check_invariants(h)


def test_same_links_in_two_simultaneous_batches_download_once(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("image", P)
    sim.add("carousel", C)
    first = create_links(h, [link(P), link(C)])
    second = create_links(h, [link(C), link(P)])
    h.run_imports()
    h.run_jobs()
    h.run_jobs()
    assert sorted(Counter(sim.calls).items()) == [(C, 2), (P, 2)]
    assert len(sim.cdn.hits) == 5 and len(set(sim.cdn.hit_paths())) == 5, "cada arquivo baixado uma única vez"
    assert media_count(h) == 5
    for batch in (first, second):
        done = h.batch(batch["id"])
        assert (done["status"], done["found"], done["progress"]["failed"]) == ("completed", 5, 0), done
        jobs = h.ctx.jobs.for_batch(batch["id"])
        assert len(jobs) == 5 and all(j["status"] == "completed" and j["media_id"] for j in jobs)
    # nenhuma mídia aponta para um lote errado: cada job do lote 2 liga à MESMA media do job equivalente do lote 1
    by_key = {(j["source_post_id"], j["source_media_index"]): j["media_id"] for j in h.ctx.jobs.for_batch(first["id"])}
    for j in h.ctx.jobs.for_batch(second["id"]):
        assert by_key[(j["source_post_id"], j["source_media_index"])] == j["media_id"]
    check_invariants(h)


# --------------------------------------------------------------------------- 5. concorrência e ordem


def test_link_batches_and_a_profile_batch_in_between_one_query_at_a_time(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    for code, kind in ((A, "image"), (B, "reel"), (C, "carousel"), (D, "image"), (E, "video")):
        sim.add(kind, code)
    sim.profile.items = [remote(f"PROF{n:06d}", 0) for n in range(3)]
    first = create_links(h, [link(A), link(B), link(C)])
    profile = h.import_profile(profile="perfil_teste")
    second = create_links(h, [link(D), link(E)])
    h.run_imports()
    h.run_jobs()
    h.run_jobs()
    assert sim.http.max_in_flight == 1
    order = sim.calls
    assert [c for c in order if c in (A, B, C)] == [A, B, C] and [c for c in order if c in (D, E)] == [D, E]
    expected = {first["id"]: {A, B, C}, second["id"]: {D, E}, profile["id"]: {f"PROF{n:06d}" for n in range(3)}}
    for batch_id, posts in expected.items():
        jobs = h.ctx.jobs.for_batch(batch_id)
        assert {j["source_post_id"] for j in jobs} == posts, batch_id
        assert h.batch(batch_id)["status"] == "completed"
    assert media_count(h) == 1 + 1 + 4 + 1 + 1 + 3
    check_invariants(h)


def test_real_worker_threads_resolve_one_link_at_a_time_across_batches(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env, start_worker=True)
    for code, kind in ((A, "image"), (B, "carousel"), (C, "video"), (D, "reel")):
        sim.add(kind, code)
    batches = [create_links(h, [link(A), link(B)]), create_links(h, [link(C), link(D)]), create_links(h, [link(A)])]

    def finished():
        return all(h.batch(b["id"])["status"] in ("completed", "failed") for b in batches)

    assert wait_until(finished, timeout=60), [h.batch(b["id"])["status"] for b in batches]
    assert sim.http.max_in_flight == 1
    assert [h.batch(b["id"])["status"] for b in batches] == ["completed"] * 3
    assert media_count(h) == 1 + 4 + 1 + 1
    assert len(set(sim.cdn.hit_paths())) == len(sim.cdn.hits) == 7, "nenhum arquivo baixado duas vezes"
    check_invariants(h)


# --------------------------------------------------------------------------- 6. retomada


def test_restart_after_the_first_link_does_not_requery_it(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    for code, kind in ((A, "image"), (B, "image"), (C, "carousel")):
        sim.add(kind, code)
    sim.http.script[B] = [InstagramError("network_error")]
    batch = create_links(h, [link(A), link(B), link(C)])
    h.run_all()
    assert sim.calls == [A, B]
    h2, _ = make_app(harness_factory, base_media, audit_env, root=h.root, sim=sim)
    h2.ctx.db.execute("UPDATE batches SET listing_retry_at = NULL")
    h2.run_all()
    assert sim.calls == [A, B, B, C], "A não é consultado de novo"
    done = h2.batch(batch["id"])
    assert (done["status"], done["found"], done["progress"]["downloaded"]) == ("completed", 6, 6)
    assert len(sim.cdn.hits) == 6 and len(set(sim.cdn.hit_paths())) == 6
    assert [r["status"] for r in link_rows(h2, batch["id"])] == ["resolved"] * 3
    check_invariants(h2)


def test_restart_with_a_job_left_downloading_finishes_it_once_without_orphans(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("carousel", C)
    sim.add("image", P)
    batch = create_links(h, [link(C), link(P)])
    h.run_imports()
    jobs = h.ctx.jobs.for_batch(batch["id"])
    stuck = jobs[1]
    # "app caiu no meio do download do 2o item": job em `downloading` com temporário e .part deixados no disco
    h.ctx.db.execute("UPDATE jobs SET status = 'downloading', stage = 'downloading', started_at = ? WHERE id = ?",
                     ("2026-01-01T00:00:00Z", stuck["id"]))
    temp = h.ctx.storage.job_temp_dir(stuck["id"], fresh=True)
    (temp / "cdn-dead.part").write_bytes(b"\xff\xd8\xff\xe0truncado")
    h.ctx.db.execute("UPDATE jobs SET temp_path = ? WHERE id = ?", (h.ctx.storage.relative(temp), stuck["id"]))
    h.run_jobs()   # os outros 4 terminam; o preso fica
    assert media_count(h) == 4
    calls_before, hits_before = list(sim.calls), len(sim.cdn.hits)
    h2, _ = make_app(harness_factory, base_media, audit_env, root=h.root, sim=sim)
    h2.run_all()
    assert sim.calls == calls_before, "nenhum link reconsultado"
    assert len(sim.cdn.hits) - hits_before == 1, "só o job preso baixa; os concluídos não repetem"
    assert media_count(h2) == 5
    done = h2.batch(batch["id"])
    assert (done["status"], done["found"], done["progress"]["downloaded"], done["progress"]["failed"]) == (
        "completed", 5, 5, 0)
    assert all(j["status"] == "completed" for j in h2.ctx.jobs.for_batch(batch["id"]))
    assert storage_leftovers(h2) == [], "temporário e .part do job interrompido removidos"
    check_invariants(h2)


def test_restart_in_the_middle_of_resolution_with_pending_links_and_ready_jobs(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    for code, kind in ((A, "carousel"), (B, "image"), (C, "image")):
        sim.add(kind, code)
    batch = create_links(h, [link(A), link(B), link(C)])

    def explode_on_b(code):
        if code == B:   # erro inesperado ao consultar B: B segue pendente e nada de B fica no banco
            sim.http.on_call = None
            raise RuntimeError("falha interna simulada")

    sim.http.on_call = explode_on_b
    h.run_imports()
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM jobs WHERE source_post_id = ?", (B,)) == 0
    assert [r["status"] for r in link_rows(h, batch["id"])] == ["resolved", "pending", "pending"]
    h2, _ = make_app(harness_factory, base_media, audit_env, root=h.root, sim=sim)
    row = h2.ctx.batches.get(batch["id"])
    assert (row["listing_state"], row["error_code"]) == ("needs_attention", "internal_error")
    assert h2.client.post(f"/api/batches/{batch['id']}/resume-listing").status_code == 200   # botão "Retomar"
    h2.run_all()
    h2.run_all()
    done = h2.batch(batch["id"])
    assert done["status"] == "completed" and done["found"] == 6 and done["progress"]["downloaded"] == 6, done
    assert sim.calls.count(A) == 1, "A não é reconsultado"
    assert len(set(sim.cdn.hit_paths())) == len(sim.cdn.hits) == 6
    check_invariants(h2)


# --------------------------------------------------------------------------- 7. cancelamento


def _cancel(h, batch_id):
    response = h.client.post(f"/api/batches/{batch_id}/cancel")
    assert response.status_code == 200, response.text


def test_cancel_while_the_first_link_is_being_resolved(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    for code in (A, B):
        sim.add("carousel", code)
    batch = create_links(h, [link(A), link(B)])
    sim.http.on_call = lambda code: _cancel(h, batch["id"])
    h.run_all()
    h.run_all()
    assert sim.calls == [A] and sim.cdn.hits == []
    done = h.batch(batch["id"])
    assert done["status"] == "cancelled" and done["found"] == 0
    assert [r["status"] for r in link_rows(h, batch["id"])] == ["cancelled", "cancelled"]
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM jobs") == 0 and media_count(h) == 0
    assert storage_leftovers(h) == []
    check_invariants(h)


def test_cancel_with_pending_links_after_jobs_exist_keeps_only_what_finished(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    for code in (A, B, C):
        sim.add("carousel", code)
    batch = create_links(h, [link(A), link(B), link(C)])
    sim.http.on_call = lambda code: _cancel(h, batch["id"]) if code == B else None
    h.run_imports()
    h.run_jobs()
    assert sim.calls == [A, B]
    jobs = h.ctx.jobs.for_batch(batch["id"])
    assert len(jobs) == 4 and {j["source_post_id"] for j in jobs} == {A}
    assert {j["status"] for j in jobs} == {"cancelled"} and sim.cdn.hits == []
    done = h.batch(batch["id"])
    assert done["status"] == "cancelled" and done["links"]["resolved"] == 1
    assert [r["status"] for r in link_rows(h, batch["id"])] == ["resolved", "cancelled", "cancelled"]
    assert media_count(h) == 0 and storage_leftovers(h) == []
    check_invariants(h)


def test_cancel_during_download_leaves_only_finished_originals_and_no_temp_files(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("carousel", C)
    sim.add("image", P)
    batch = create_links(h, [link(C), link(P)])
    h.run_imports()
    hits = []

    def on_hit(url):
        hits.append(url)
        if len(hits) == 3:   # durante o 3o download
            _cancel(h, batch["id"])

    sim.cdn.on_hit = on_hit
    h.run_jobs()
    done = h.batch(batch["id"])
    assert done["status"] == "cancelled"
    jobs = h.ctx.jobs.for_batch(batch["id"])
    states = [j["status"] for j in jobs]
    assert set(states) <= {"completed", "cancelled"} and states.count("completed") <= 2, states
    assert len(sim.cdn.hits) == 3, "nada é baixado depois do cancelamento"
    for j in jobs:
        assert (j["media_id"] is not None) == (j["status"] == "completed"), j
    assert media_count(h) == states.count("completed")
    assert storage_leftovers(h) == []
    check_invariants(h)
