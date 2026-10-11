"""Auditoria da importação por link, itens 1, 2 e 10: tipos de publicação ponta a ponta com a pilha web REAL
(documento público simulado -> normalize_post -> cdn_download simulado) e mídias sintéticas reais. Offline."""

from __future__ import annotations

import hashlib

import pytest
from media_processor import MediaProcessor, sha256_file

from audit_links_helpers import (  # noqa: F401 - fixtures
    OWNER,
    audit_env,
    base_media,
    check_invariants,
    make_app,
    make_root,
    storage_leftovers,
)
from import_links_fakes import create_links, link, link_rows
from post_document_fixtures import data_sjs_script, envelope, media_payload, post_html

P, R, V, C = "AAAAAaaaa01", "BBBBBbbbb02", "CCCCCcccc03", "DDDDDdddd04"


def lib(h, **params):
    return h.client.get("/api/media", params={"limit": 100, **params}).json()


def test_all_four_publication_types_end_to_end(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("image", P, "Foto_Dono")
    sim.add("reel", R)
    sim.add("video", V, "Video_Dono")
    sim.add("carousel", C)
    batch = create_links(h, [link(C), link(P), link(R, "reel"), link(V)])
    h.run_all()
    assert sim.calls == [C, P, R, V], "uma consulta por link, na ordem colada"
    assert sim.http.max_in_flight == 1
    done = h.batch(batch["id"])
    assert (done["status"], done["found"], done["error_code"]) == ("completed", 7, None)
    assert done["progress"]["downloaded"] == 7 and done["progress"]["failed"] == 0

    rows = link_rows(h, batch["id"])
    assert [(r["status"], r["items_found"], r["profile"]) for r in rows] == [
        ("resolved", 4, OWNER), ("resolved", 1, "foto_dono"), ("resolved", 1, OWNER), ("resolved", 1, "video_dono")]

    media = sorted(h.ctx.db.fetchall("SELECT * FROM media"), key=lambda m: m["id"])
    assert [(m["source_post_id"], m["source_media_index"], m["media_type"]) for m in media] == [
        (C, 0, "image"), (C, 1, "video"), (C, 2, "image"), (C, 3, "video"),
        (P, 0, "image"), (R, 0, "video"), (V, 0, "video")], "carrossel na ordem; uma mídia por job"
    jobs = h.ctx.jobs.for_batch(batch["id"])
    assert len(jobs) == 7 and len({j["id"] for j in jobs}) == 7
    assert all(j["status"] == "completed" and j["media_id"] for j in jobs)
    assert [m["source_profile"] for m in media] == [OWNER] * 4 + ["foto_dono", OWNER, "video_dono"]
    # Reel x vídeo comum: a URL de origem diferencia (o /reel/ vem da resposta, não do link colado)
    by_post = {m["source_post_id"]: m for m in media}
    assert "/reel/" in by_post[R]["source_url"] and "/p/" in by_post[V]["source_url"]
    assert "/p/" in by_post[P]["source_url"]
    # arquivo no storage: íntegro e igual ao que o "CDN" serviu
    served = {hashlib.sha256(b).hexdigest() for b in sim.cdn.bodies.values()}
    for m in media:
        assert sha256_file(h.ctx.storage.resolve(m["original_path"])) == m["original_sha256"]
        assert m["original_sha256"] in served
    assert len({m["original_sha256"] for m in media}) == 7

    # Biblioteca e filtros
    assert lib(h)["total"] == 7
    assert lib(h, media_type="video")["total"] == 4 and lib(h, media_type="image")["total"] == 3
    assert lib(h, profile="FOTO_DONO")["total"] == 1 and lib(h, profile=OWNER)["total"] == 5
    assert lib(h, q=C)["total"] == 4
    for item in lib(h)["items"]:
        assert h.client.get(f"/api/media/{item['id']}").status_code == 200
    assert storage_leftovers(h) == []
    check_invariants(h)


def test_download_only_has_no_thumbnail_pergunta(harness_factory, base_media, audit_env):
    """Pergunta: download puro não gera miniatura (pipeline._download_only). Documenta o comportamento atual."""
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("image", P)
    create_links(h, [link(P)])
    h.run_all()
    [m] = lib(h)["items"]
    assert h.ctx.media.get(m["id"])["thumbnail_path"] is None
    assert h.client.get(f"/api/media/{m['id']}/thumbnail").status_code == 404
    check_invariants(h)


# --------------------------------------------------------------------------- formatos de resposta


def test_document_with_two_envelopes_and_little_noise_resolves_the_requested_post(harness_factory, base_media,
                                                                                    audit_env):
    """O documento traz o envelope de OUTRA publicação antes do da pedida: vale o do shortcode do link."""
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("image", P)
    scripts = [data_sjs_script(media_payload(envelope(make_root("image", V)))),
               data_sjs_script(media_payload(envelope(make_root("carousel", R))))]
    sim.http.roots[R] = post_html(scripts, noise=1)   # corpo HTML cru
    batch = create_links(h, [link(P), link(R)])
    h.run_all()
    rows = link_rows(h, batch["id"])
    assert [(r["status"], r["items_found"]) for r in rows] == [("resolved", 1), ("resolved", 4)], rows
    assert lib(h)["total"] == 5 and lib(h, q=V)["total"] == 0
    check_invariants(h)


def _no_url(root):
    return {k: v for k, v in root.items() if k != "image_versions2"}


@pytest.mark.parametrize("name, mutate, code", [
    ("null", lambda root: None, "content_unavailable"),
    ("no_url", _no_url, "content_unavailable"),
    ("unsupported", lambda root: {**root, "media_type": 99}, "unsupported_media"),
    ("unknown_shape", lambda root: {"fake_unknown": {"a": 1}}, "contract_changed"),
])
def test_bad_responses_fail_only_that_link_and_import_nothing(harness_factory, base_media, audit_env, name, mutate,
                                                                code):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("image", P)
    sim.http.roots[R] = mutate(sim.add("image", R))
    batch = create_links(h, [link(R), link(P)])
    h.run_all()
    rows = link_rows(h, batch["id"])
    assert rows[0]["status"] == "failed" and rows[0]["error_code"] == code, rows
    assert rows[1]["status"] == "resolved"
    assert lib(h)["total"] == 1 and sim.calls.count(R) == 1
    done = h.batch(batch["id"])
    assert done["status"] == "completed" and done["error_message"] == "1 de 2 links não puderam ser importados."
    check_invariants(h)


def test_carousel_child_without_url_pergunta_index_gap(harness_factory, base_media, audit_env):
    """Pergunta: filho sem URL num carrossel: o índice dos demais fica 'furado' ([0,2,3]) ou é compactado?"""
    h, sim = make_app(harness_factory, base_media, audit_env)
    root = sim.add("carousel", C)
    child = root["carousel_media"][1]
    for key in ("image_versions2", "video_versions"):
        child.pop(key, None)
    batch = create_links(h, [link(C)])
    h.run_all()
    [row] = link_rows(h, batch["id"])
    media = h.ctx.db.fetchall("SELECT source_media_index FROM media ORDER BY source_media_index")
    assert [m["source_media_index"] for m in media] == [0, 2, 3], media   # índices originais preservados
    assert row["status"] == "resolved" and row["items_found"] == 3
    check_invariants(h)


# --------------------------------------------------------------------------- resposta de OUTRA publicação


def test_response_for_a_different_post_is_rejected(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("image", P)
    sim.add("image", R)
    sim.http.answer_with[P] = R   # pedimos P; o "Instagram" responde com a publicação R
    batch = create_links(h, [link(P)])
    h.run_all()
    [row] = link_rows(h, batch["id"])
    wrong = [dict(w) for w in h.ctx.db.fetchall("SELECT source_post_id FROM media")]
    assert row["status"] == "failed" and wrong == [], (row, wrong)
    # O motivo gravado é o da resposta trocada (detail `post_mismatch`), não um erro genérico.
    assert row["error_code"] == "contract_changed" and "outra publicação" in row["error_message"], row
    assert h.ctx.jobs.for_batch(batch["id"]) == [] and sim.calls == [P]
    with pytest.raises(Exception) as raised:
        sim.downloader.resolve_post(P, path_kind="p")
    assert (raised.value.code, raised.value.diagnostic) == ("contract_changed", {"detail": "post_mismatch"})


# --------------------------------------------------------------------------- processamento opcional LIGADO


def test_processing_on_creates_rendition_and_thumbnail_for_a_link_video(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env, processor=MediaProcessor())
    sim.add("reel", R)
    batch = create_links(h, [link(R, "reel")], process=True)
    h.run_all()
    done = h.batch(batch["id"])
    assert done["status"] == "completed", done
    assert done["progress"]["processed"] == 1 and done["progress"]["failed"] == 0
    [job] = h.ctx.jobs.for_batch(batch["id"])
    assert job["rendition_id"]
    media = h.ctx.media.get(job["media_id"])
    rendition = h.ctx.media.get_rendition(job["rendition_id"])
    original = h.ctx.storage.resolve(media["original_path"])
    processed = h.ctx.storage.resolve(rendition["processed_path"])
    assert sha256_file(original) == media["original_sha256"] != rendition["processed_sha256"]
    assert processed.is_file() and sha256_file(processed) == rendition["processed_sha256"]
    assert media["thumbnail_path"] and h.ctx.storage.resolve(media["thumbnail_path"]).is_file()
    assert media["width"] == 160 and media["has_audio"] == 1
    assert lib(h, view="processed")["total"] == 1
    check_invariants(h)
