"""Importação por link: resposta de OUTRA publicação (shortcode diferente do pedido, inclusive só nas maiúsculas)
nunca vira mídia nem renova URL de job. Checagem no provider web (`post_mismatch`) e defesa no runner e na renovação
(qualquer provider). 100% offline, valores FAKE."""

from __future__ import annotations

import pytest

from app.integrations.instagram.downloader import InstagramError
from app.integrations.instagram.web.normalize import post_code
from app.jobs.import_runner import LINK_MISMATCH_MESSAGE
from app.jobs.pipeline import POST_MISMATCH_MESSAGE
from audit_links_helpers import WebSim, audit_env, base_media, check_invariants, make_app, make_root  # noqa: F401
from import_links_fakes import carousel, create_links, link, link_rows, links_app, photo

A, B = "AbCdE12345", "BBBBB22222"
A_UPPER = "ABCDE12345"   # a mesma sequência de letras, só com as maiúsculas diferentes: é OUTRA publicação
P, R, C = "PPPPPpppp01", "RRRRRrrrr02", "CCCCCcccc03"


def media_count(h) -> int:
    return h.ctx.db.scalar("SELECT COUNT(*) FROM media")


# --------------------------------------------------------------------------- provider web


@pytest.mark.parametrize("kind", ["image", "reel", "carousel", "v1"])
def test_provider_rejects_the_answer_for_another_post(kind, caplog):
    sim = WebSim({})
    sim.add(kind, R)
    sim.http.answer_with[P] = R
    with caplog.at_level("DEBUG"), pytest.raises(InstagramError) as raised:
        sim.downloader.resolve_post(P)
    error = raised.value
    assert (error.code, error.stage, error.diagnostic) == ("contract_changed", "normalize", {"detail": "post_mismatch"})
    assert error.retryable is False and sim.calls == [P]
    text = caplog.text + str(error.message)
    assert P not in text and R not in text, "nenhum dos dois shortcodes vai para o log nem para a mensagem"


def test_provider_rejects_a_case_only_difference_and_accepts_the_exact_code():
    sim = WebSim({})
    sim.add("image", A)
    sim.http.answer_with[A_UPPER] = A
    with pytest.raises(InstagramError) as raised:
        sim.downloader.resolve_post(A_UPPER)
    assert raised.value.diagnostic == {"detail": "post_mismatch"}
    assert [item.source_post_id for item in sim.downloader.resolve_post(A)] == [A]


def test_provider_accepts_the_right_carousel_whose_children_have_their_own_codes():
    sim = WebSim({})
    root = sim.add("carousel", C)
    children = [edge["node"]["shortcode"] for edge in root["edge_sidecar_to_children"]["edges"]]
    assert children and C not in children, "os filhos trazem shortcode próprio; só o da publicação é comparado"
    items = sim.downloader.resolve_post(C)
    assert [(item.source_post_id, item.source_media_index) for item in items] == [(C, i) for i in range(4)]


@pytest.mark.parametrize("kind, key", [("image", "shortcode"), ("v1", "code")])
def test_answer_without_any_shortcode_keeps_the_existing_behaviour(kind, key):
    """Decisão: resposta SEM shortcode não é `post_mismatch`. Continua como antes: normalize não devolve mídia
    (`node_no_code`) e o provider levanta `contract_changed` com `post_shape_unknown` (erro do link)."""
    sim = WebSim({})
    root = sim.add(kind, P)
    node = root["items"][0] if kind == "v1" else root
    assert post_code(root) == P
    del node[key]
    assert post_code(root) is None
    with pytest.raises(InstagramError) as raised:
        sim.downloader.resolve_post(P)
    assert (raised.value.code, raised.value.diagnostic["detail"]) == ("contract_changed", "post_shape_unknown")


@pytest.mark.parametrize("root", [{}, {"items": []}, {"items": [None]}, {"shortcode": ""}, {"shortcode": 123},
                                  {"media_type": 1, "shortcode": "X"}, {"media_type": True, "code": "X"}])
def test_post_code_is_none_when_the_answer_declares_no_usable_code(root):
    assert post_code(root) is None


# --------------------------------------------------------------------------- lote: pilha web real simulada


def test_wrong_answer_fails_only_that_link_and_writes_nothing(harness_factory, base_media, audit_env, caplog):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("image", P)
    sim.add("image", R)
    sim.add("carousel", C)
    sim.http.answer_with[P] = R
    with caplog.at_level("WARNING"):
        batch = create_links(h, [link(P), link(C)])
        h.run_all()
    rows = link_rows(h, batch["id"])
    assert [(r["status"], r["error_code"], r["items_found"]) for r in rows] == [
        ("failed", "contract_changed", None), ("resolved", None, 4)], rows
    assert "outra publicação" in rows[0]["error_message"]
    assert f"Lote {batch['id']}: link 1 não pôde ser importado (contract_changed) [stage=normalize " \
           "detail=post_mismatch]" in caplog.text
    posts = {m["source_post_id"] for m in h.ctx.db.fetchall("SELECT source_post_id FROM media")}
    assert posts == {C} and not [p for p in sim.cdn.hit_paths() if R in p or P in p]
    assert sim.calls == [P, C], "uma consulta por link; o link errado não é pedido de novo"
    done = h.batch(batch["id"])
    assert (done["status"], done["found"], done["error_message"]) == (
        "completed", 4, "1 de 2 links não puderam ser importados.")
    check_invariants(h)


def test_refresh_answered_with_another_post_never_updates_the_job_url(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("image", P)
    sim.add("image", R)
    batch = create_links(h, [link(P)])
    h.run_imports()
    sim.cdn.expired.add(f"/{P}/")          # a URL guardada venceu: o job pede a publicação de novo
    sim.http.answer_with[P] = R            # e o "Instagram" responde com outra
    h.run_jobs()
    h.run_jobs()
    [job] = h.ctx.jobs.for_batch(batch["id"])
    assert (job["status"], job["error_code"]) == ("failed", "contract_changed"), job
    assert sim.calls == [P, P] and not [p for p in sim.cdn.hit_paths() if R in p], sim.cdn.hit_paths()
    assert media_count(h) == 0
    record = h.ctx.jobs.listing_page(batch["id"], 1)
    assert (record["last_refresh_ok"], record["last_refresh_code"]) == (0, "contract_changed")
    check_invariants(h)


# --------------------------------------------------------------------------- defesa do runner e da renovação (fakes)


@pytest.mark.parametrize("answer", [
    photo(B), photo(A_UPPER), [*carousel(A, 2), *photo(B)], [*photo(B), *carousel(A, 2)],
], ids=["outra", "so_maiusculas", "carrossel_com_item_de_outra", "item_de_outra_primeiro"])
def test_runner_refuses_items_of_another_post_from_any_provider(harness_factory, caplog, answer):
    h, dl = links_app(harness_factory, {A: answer, B: photo(B)})
    with caplog.at_level("WARNING"):
        batch = create_links(h, [link(A), link(B)])
        h.run_all()
    rows = link_rows(h, batch["id"])
    assert [(r["status"], r["error_code"], r["error_message"], r["items_found"]) for r in rows] == [
        ("failed", "contract_changed", LINK_MISMATCH_MESSAGE, None), ("resolved", None, None, 1)], rows
    jobs = h.ctx.jobs.for_batch(batch["id"])
    assert [j["source_post_id"] for j in jobs] == [B], "nenhum job criado a partir da resposta trocada"
    assert dl.calls == [A, B] and sorted(dl.downloads) == [f"{B}:0"] and media_count(h) == 1
    assert f"Lote {batch['id']}: link 1 não pôde ser importado (contract_changed) [detail=post_mismatch]" in caplog.text
    assert A not in caplog.text and A_UPPER not in caplog.text


def test_runner_accepts_the_right_carousel(harness_factory):
    h, dl = links_app(harness_factory, {A: carousel(A, 4)})
    batch = create_links(h, [link(A)])
    h.run_all()
    [row] = link_rows(h, batch["id"])
    assert (row["status"], row["items_found"]) == ("resolved", 4) and media_count(h) == 4


def test_refresh_that_returns_another_post_updates_no_job_at_all(harness_factory):
    """A renovação de A devolve as mídias de B (que também está no lote, com URL vencida): sem a defesa, o job de B
    receberia a URL vinda da consulta de A. Com ela, A falha e B faz a própria consulta."""
    post_a, post_b = photo(A), photo(B)
    h, dl = links_app(harness_factory, {A: post_a, B: post_b})
    batch = create_links(h, [link(A), link(B)])
    h.run_imports()
    dl.expired.update(item.media_url for item in (*post_a, *post_b))
    dl.posts[A] = post_b
    h.run_jobs()
    h.run_jobs()
    jobs = {j["source_post_id"]: j for j in h.ctx.jobs.for_batch(batch["id"])}
    assert (jobs[A]["status"], jobs[A]["error_code"], jobs[A]["error_message"]) == (
        "failed", "contract_changed", POST_MISMATCH_MESSAGE)
    assert jobs[B]["status"] == "completed"
    assert dl.calls == [A, B, A, B], "B não aproveita a resposta trocada: consulta a própria publicação"
    assert dl.download_urls[f"{A}:0"] == [post_a[0].media_url], "A nunca baixou por URL de outra publicação"
    record = h.ctx.jobs.listing_page(batch["id"], 1)
    assert (record["last_refresh_ok"], record["last_refresh_code"]) == (0, "contract_changed")


@pytest.mark.parametrize("answer", [
    photo(A_UPPER), [*carousel(A, 2), *photo(B)], [*photo(B), *carousel(A, 2)], [*carousel(A, 2), *photo(A_UPPER)],
], ids=["so_maiusculas", "mista_outra_no_fim", "mista_outra_no_inicio", "mista_so_maiusculas_no_fim"])
def test_refresh_refuses_case_only_and_mixed_answers(harness_factory, answer):
    """A renovação compara TODOS os itens, de forma exata: basta um de outra publicação (ou só com as maiúsculas
    diferentes) para nenhum job receber URL."""
    post = carousel(A, 2)
    h, dl = links_app(harness_factory, {A: post})
    batch = create_links(h, [link(A)])
    h.run_imports()
    dl.expired.update(item.media_url for item in post)
    dl.posts[A] = answer
    h.run_jobs()
    h.run_jobs()
    jobs = h.ctx.jobs.for_batch(batch["id"])
    assert [(j["status"], j["error_code"]) for j in jobs] == [("failed", "contract_changed")] * 2, jobs
    assert jobs[0]["error_message"] == POST_MISMATCH_MESSAGE
    assert dl.calls == [A, A], "uma consulta; os irmãos repetem o erro sem pedir de novo"
    assert dl.download_urls == {f"{A}:0": [post[0].media_url], f"{A}:1": [post[1].media_url]}, dl.download_urls
    assert media_count(h) == 0
