"""Auditoria da importação por link, item 3: entrada (formas de link, separadores, repetidos, limites). Offline."""

from __future__ import annotations

import time

import pytest

from audit_links_helpers import audit_env, base_media, check_invariants, make_app  # noqa: F401 - fixtures
from import_links_fakes import create_links, link, link_rows

CODE = "AbCdEfGhIjK"
LOWER = CODE.lower()


def preview(h, **body):
    response = h.client.post("/api/import-links/preview", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_shortcodes_are_case_sensitive_end_to_end(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    sim.add("image", CODE)
    sim.add("image", LOWER)
    pv = preview(h, text=f"{link(CODE)}\n{link(LOWER)}\n{link(CODE, 'reel')}")
    assert [v["shortcode"] for v in pv["valid"]] == [CODE, LOWER] and pv["duplicates"] == 1
    assert pv["valid"][0]["url"] == link(CODE), "o primeiro (/p/) fica"
    batch = create_links(h, [link(CODE), link(LOWER), link(CODE, "reel")])
    assert batch["duplicates"] == 1 and batch["links"]["total"] == 2
    h.run_all()
    assert sim.calls == [CODE, LOWER], "o mesmo shortcode como /p/ e /reel/ é consultado uma vez"
    assert [r["shortcode"] for r in link_rows(h, batch["id"])] == [CODE, LOWER]
    posts = {m["source_post_id"] for m in h.ctx.db.fetchall("SELECT source_post_id FROM media")}
    assert posts == {CODE, LOWER}, "duas publicações distintas (maiúsculas importam)"
    check_invariants(h)


def test_preview_has_no_side_effects_and_no_instagram_call(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    before = h.ctx.db.scalar("SELECT COUNT(*) FROM batches")
    pv = preview(h, entries=[link(CODE)], text="lixo https://www.instagram.com/stories/x/1/")
    assert len(pv["valid"]) == 1 and len(pv["invalid"]) == 2
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM batches") == before and sim.calls == [] and sim.cdn.hits == []


@pytest.mark.parametrize("entry", [
    "https://www.instagram.com/tv/ABCDE12345/",
    "https://www.instagram.com/stories/dono_fake/3300000000000000000/",
    "https://www.instagram.com/stories/highlights/17900000000000000/",
    "https://www.instagram.com/explore/tags/fake/",
    "https://www.instagram.com/dono_fake/",
    "https://www.instagram.com/dono_fake/reels/",
    "https://www.instagram.com/dono_fake/tagged/",
    "https://www.instagram.com/p/ABCDE12345/comments/",
    "https://www.instagram.com/reel/",
    "https://www.instagram.com/accounts/login/?next=/p/ABCDE12345/",
    "https://l.instagram.com/?u=https://www.instagram.com/p/ABCDE12345/",
    "https://www.instagram.com.evil.example/p/ABCDE12345/",
    "https://evil.example/www.instagram.com/p/ABCDE12345/",
    "https://www.instagram.com@evil.example/p/ABCDE12345/",
    "https://user:pw@www.instagram.com/p/ABCDE12345/",
    "https://www.instagram.com:8080/p/ABCDE12345/",
    "https://www.instagram.com/p/ABCDE12345\\..\\x/",
    "javascript:alert(1)//instagram.com/p/ABCDE12345/",
    "https://www.instagram.com/p/ABCD/",              # curto demais (< 5)
    "https://www.instagram.com/p/" + "A" * 65 + "/",   # longo demais (> 64)
    "https://www.instagram.com/p/ABCDEé12345/",
    "ｉnstagram.com/p/ABCDE12345/",
])
def test_non_post_and_hostile_entries_are_invalid(harness_factory, base_media, audit_env, entry):
    h, sim = make_app(harness_factory, base_media, audit_env)
    pv = preview(h, entries=[entry])
    assert pv["valid"] == [] and len(pv["invalid"]) == 1, pv
    response = h.client.post("/api/import-links", json={"links": [entry]})
    assert response.status_code == 422 and response.json()["code"] in ("invalid_links", "no_links")
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0 and sim.calls == []


@pytest.mark.parametrize("entry, kind", [
    (f"HTTPS://WWW.INSTAGRAM.COM/P/{CODE}/", "p"),
    (f"https://www.instagram.com/Reel/{CODE}/", "reel"),
    (f"https://www.instagram.com/REELS/{CODE}", "reel"),
    (f"https://www.instagram.com/p/{CODE}/?img_index=2#x", "p"),
    (f"https://www.instagram.com/p/{CODE}?igsh=abc", "p"),
    (f"https://www.instagram.com/p/{CODE}/#", "p"),
    (f"www.instagram.com/p/{CODE}", "p"),
    (f"https://www.instagram.com//p/{CODE}/", "p"),
])
def test_accepted_variants_normalise_to_the_same_post(harness_factory, base_media, audit_env, entry, kind):
    h, _ = make_app(harness_factory, base_media, audit_env)
    pv = preview(h, entries=[entry])
    assert pv["invalid"] == [], pv
    assert pv["valid"] == [{"shortcode": CODE, "url": f"https://www.instagram.com/{kind}/{CODE}/"}]


def test_pasted_text_separators(harness_factory, base_media, audit_env):
    h, _ = make_app(harness_factory, base_media, audit_env)
    codes = ["AAAAA00001", "BBBBB00002", "CCCCC00003", "DDDDD00004", "EEEEE00005"]
    text = (f"{link(codes[0])}, {link(codes[1])};{link(codes[2])}\r\n\r\n\t{link(codes[3])}  ,,;; {link(codes[4])}\n"
            f"{link(codes[0])}\n")
    pv = preview(h, text=text)
    assert [v["shortcode"] for v in pv["valid"]] == codes and pv["duplicates"] == 1 and pv["invalid"] == []
    # texto e entries juntos: entries primeiro, o texto depois; repetição entre eles conta
    pv = preview(h, entries=[link(codes[2])], text=f"{link(codes[0])} {link(codes[2])}")
    assert [v["shortcode"] for v in pv["valid"]] == [codes[2], codes[0]] and pv["duplicates"] == 1


def test_empty_inputs(harness_factory, base_media, audit_env):
    h, _ = make_app(harness_factory, base_media, audit_env)
    assert preview(h) == {"valid": [], "invalid": [], "duplicates": 0}
    assert preview(h, text="  \n , ; ") == {"valid": [], "invalid": [], "duplicates": 0}
    for links in ([], ["", "  ", ",;"]):
        response = h.client.post("/api/import-links", json={"links": links})
        assert response.status_code == 422, response.text
    assert h.client.post("/api/import-links", json={"links": ["", " "]}).json()["code"] == "no_links"
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0


def test_query_string_with_a_comma_splits_the_link_pergunta(harness_factory, base_media, audit_env):
    """Pergunta: a vírgula/ponto e vírgula separa links, então um link com `,` na query vira uma entrada válida e
    uma inválida (a lista é recusada com 422). O contrato diz que vírgula separa; documenta o efeito colateral."""
    h, _ = make_app(harness_factory, base_media, audit_env)
    pv = preview(h, text=f"{link(CODE)}?a=1,2")
    assert [v["shortcode"] for v in pv["valid"]] == [CODE] and [i["entry"] for i in pv["invalid"]] == ["2"]


def test_limit_of_500_links_and_huge_inputs(harness_factory, base_media, audit_env):
    h, sim = make_app(harness_factory, base_media, audit_env)
    codes = [f"Q{n:09d}" for n in range(501)]
    ok = h.client.post("/api/import-links/preview", json={"entries": [link(c) for c in codes[:500]]})
    assert ok.status_code == 200 and len(ok.json()["valid"]) == 500
    over = h.client.post("/api/import-links/preview", json={"text": " ".join(link(c) for c in codes)})
    assert over.status_code == 422 and over.json()["code"] == "too_many_links"
    assert h.client.post("/api/import-links", json={"links": [link(c) for c in codes]}).json()["code"] == "too_many_links"
    # 500 repetições do mesmo link + 1 inválido também estouram: o teto conta ENTRADAS, não links distintos
    over = h.client.post("/api/import-links/preview", json={"text": " ".join([link(CODE)] * 501)})
    assert over.status_code == 422 and over.json()["code"] == "too_many_links"
    # Lote com exatamente 500: cria, pagina e responde sem consultar o Instagram na criação
    batch = create_links(h, [link(c) for c in codes[:500]])
    assert batch["links"]["total"] == 500 and sim.calls == []
    page = h.client.get(f"/api/batches/{batch['id']}/links", params={"limit": 500}).json()
    assert page["total"] == 500 and len(page["items"]) == 500 and page["items"][0]["position"] == 1
    assert [i["shortcode"] for i in page["items"]] == codes[:500]
    assert h.client.get(f"/api/batches/{batch['id']}/links", params={"limit": 501}).status_code == 422
    tail = h.client.get(f"/api/batches/{batch['id']}/links", params={"limit": 200, "offset": 450}).json()
    assert len(tail["items"]) == 50 and tail["items"][0]["position"] == 451


def test_enormous_single_entry_and_enormous_token_list_are_rejected_fast(harness_factory, base_media, audit_env):
    h, _ = make_app(harness_factory, base_media, audit_env)
    started = time.monotonic()
    huge = "https://www.instagram.com/p/" + "A" * 500_000 + "/"
    pv = preview(h, entries=[huge])
    assert h.client.post("/api/import-links/preview", json={"text": "A" * 2_000_000}).status_code == 413
    assert pv["valid"] == [] and len(pv["invalid"][0]["entry"]) <= 200
    many = preview_status(h, " ".join(["x"] * 200_000))
    assert many == 422
    assert time.monotonic() - started < 10
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0


def preview_status(h, text) -> int:
    return h.client.post("/api/import-links/preview", json={"text": text}).status_code


def test_invalid_entry_repeated_counts_as_duplicate_and_is_listed_once(harness_factory, base_media, audit_env):
    h, _ = make_app(harness_factory, base_media, audit_env)
    pv = preview(h, text=f"nao-e-link nao-e-link {link(CODE)}")
    assert len(pv["invalid"]) == 1 and pv["duplicates"] == 1 and len(pv["valid"]) == 1
