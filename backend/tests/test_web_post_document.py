"""web/post_document.py: leitura do envelope da publicação no documento público (offline; só valores FAKE)."""

from __future__ import annotations

import dataclasses
import json

import pytest

from app.integrations.instagram.web import post_document as pd
from app.integrations.instagram.web.post_document import (
    find_media_envelopes,
    iter_json_scripts,
    parse_post_document,
    select_media_node,
)
from post_document_fixtures import (
    PRELOADER,
    data_sjs_script,
    envelope,
    media_node,
    media_payload,
    post_html,
    post_page,
)

CODE, OTHER = "FAKEREEL001", "FAKEREEL002"


def summary(parse) -> str:
    return json.dumps(dataclasses.asdict(parse))


# --------------------------------------------------------------------------- scripts


def test_only_data_sjs_json_scripts_are_read_among_the_noise():
    html = post_page(media_node("video", CODE))
    scripts = list(iter_json_scripts(html))
    assert len(scripts) == 39, "38 de ruído + o da publicação; text/javascript e o JSON sem data-sjs ficam de fora"
    assert not any("JSFAKE00001" in text or "NOSJS000001" in text for text in scripts)
    assert sum(CODE in text for text in scripts) == 1


def test_script_type_is_case_insensitive_and_needs_the_attribute():
    html = ('<script type="Application/JSON" data-sjs>{"a":1}</script>'
            '<script type="application/json">{"b":2}</script>'
            '<script type="text/javascript" data-sjs>{"c":3}</script>'
            '<script data-sjs>{"d":4}</script>')
    assert list(iter_json_scripts(html)) == ['{"a":1}']


def test_script_content_is_raw_text_not_markup():
    body = json.dumps({"html": "<div class=\"x\">a &amp; b</div><!-- c -->"})
    assert list(iter_json_scripts(f'<script type="application/json" data-sjs>{body}</script>')) == [body]


def test_script_caps():
    html = "".join(data_sjs_script({"n": n}) for n in range(5))
    assert len(list(iter_json_scripts(html, max_scripts=3))) == 3
    big = data_sjs_script({"pad": "x" * 500})
    assert list(iter_json_scripts(big + data_sjs_script({"n": 1}), max_script_bytes=100)) == ['{"n":1}']


def test_empty_or_scriptless_html():
    for html in ("", "<html><body>Entrar</body></html>", None):
        node, parse = parse_post_document(html, CODE)
        assert node is None and parse.outcome == "no_scripts" and parse.scripts == 0


# --------------------------------------------------------------------------- envelopes


def test_reel_envelope_is_found_and_selected():
    node = media_node("video", CODE)
    found, parse = parse_post_document(post_page(node), CODE)
    assert found == node
    assert (parse.outcome, parse.scripts, parse.json_scripts, parse.envelopes) == ("ok", 39, 39, 1)
    assert parse.preloader_observed is True and parse.typename == "XIGPolarisVideoMedia"


def test_invalid_json_script_does_not_break_the_rest():
    scripts = [data_sjs_script(None, raw="{isto não é json"), data_sjs_script(media_payload(envelope(media_node("video", CODE))))]
    found, parse = parse_post_document(post_html(scripts, noise=2), CODE)
    assert found is not None and parse.outcome == "ok"
    assert (parse.scripts, parse.json_scripts) == (4, 3)


def test_envelope_at_top_level_and_without_the_observed_preloader_name():
    entry = ["RelayPrefetchedStreamCache", "next", [], ["adp_FakeOtherPreloader_1", {"__bbox": {"result": {"data": {
        "xig_polaris_media": envelope(media_node("video", CODE))}}}}]]
    found, parse = parse_post_document(post_html([data_sjs_script(entry)], noise=0), CODE)
    assert found is not None and parse.outcome == "ok" and parse.preloader_observed is False
    [(name, _)] = find_media_envelopes([json.dumps(entry)])
    assert name == "adp_FakeOtherPreloader_1"


@pytest.mark.parametrize("entry", [
    ["OtherMarker", "next", [], [PRELOADER, {"__bbox": {"result": {"data": {"xig_polaris_media": {"code": CODE}}}}}]],
    ["RelayPrefetchedStreamCache", "next", [], "não é lista"],
    ["RelayPrefetchedStreamCache", "next", [], [PRELOADER, {"__bbox": {"result": {"data": {"outro_campo": {}}}}}]],
    ["RelayPrefetchedStreamCache", "next", [], [PRELOADER, {"__bbox": {"result": {"data": {"xig_polaris_media": None}}}}]],
    ["RelayPrefetchedStreamCache", "next", [], [PRELOADER, {"__bbox": {"result": None}}]],
    ["RelayPrefetchedStreamCache"],
    {"xig_polaris_media": {"code": CODE, "if_not_gated_logged_out": {"code": CODE}}},   # fora do caminho observado
])
def test_shapes_that_are_not_the_envelope(entry):
    assert find_media_envelopes([json.dumps(entry)]) == []
    node, parse = parse_post_document(post_html([data_sjs_script(entry)], noise=3), CODE)
    assert node is None and parse.outcome == "envelope_missing" and parse.envelopes == 0


def test_walk_stops_at_max_depth():
    payload: object = media_payload(envelope(media_node("video", CODE)))
    shallow = payload
    for _ in range(10):
        shallow = [shallow]
    assert len(find_media_envelopes([json.dumps(shallow)])) == 1
    deep = payload
    for _ in range(pd.MAX_DEPTH + 1):
        deep = [deep]
    assert find_media_envelopes([json.dumps(deep)]) == []


def test_absurdly_nested_json_is_ignored():
    text = "[" * 100000 + "]" * 100000
    assert find_media_envelopes([text, json.dumps(media_payload(envelope(media_node("video", CODE))))])


# --------------------------------------------------------------------------- escolha do nó


def envs(*media):
    return [(PRELOADER, item) for item in media]


def test_two_envelopes_pick_the_requested_code():
    first, second = media_node("video", OTHER), media_node("video", CODE)
    html = post_html([data_sjs_script(media_payload(envelope(first))), data_sjs_script(media_payload(envelope(second)))])
    found, parse = parse_post_document(html, CODE)
    assert found == second and parse.outcome == "ok" and parse.envelopes == 2
    assert parse_post_document(html, OTHER)[0] == first


def test_only_another_post_is_mismatch_and_case_matters():
    node = media_node("video", "AbCdE12345")
    assert select_media_node(envs(envelope(node)), "ABCDE12345") == ("mismatch", None)
    assert select_media_node(envs(envelope(node)), OTHER) == ("mismatch", None)
    assert select_media_node(envs(envelope(node)), "AbCdE12345")[0] == "ok"


def test_inner_node_of_another_post_is_mismatch():
    assert select_media_node(envs(envelope(media_node("video", OTHER), code=CODE)), CODE) == ("mismatch", None)


def test_inner_node_without_code_goes_on_as_ok():
    node = media_node("video", CODE)
    del node["code"]
    assert select_media_node(envs(envelope(node, code=CODE)), CODE) == ("ok", node)


@pytest.mark.parametrize("media", [
    envelope(media_node("video", CODE), gating={"fake_ruling": "FAKE"}),
    envelope(media_node("video", CODE), gating="FAKE"),
    envelope(None, code=CODE),
    envelope("texto", code=CODE),
    {"code": CODE},
])
def test_gated_shapes(media):
    assert select_media_node(envs(media), CODE) == ("gated", None)
    node, parse = parse_post_document(post_html([data_sjs_script(media_payload(media))]), CODE)
    assert node is None and parse.outcome == "gated"


def test_gated_wins_over_another_posts_envelope_and_ok_wins_over_both():
    gated = envelope(None, code=CODE)
    other = envelope(media_node("video", OTHER))
    assert select_media_node(envs(other, gated), CODE)[0] == "gated"
    assert select_media_node(envs(gated, other), CODE)[0] == "gated"
    good = envelope(media_node("video", CODE))
    assert select_media_node(envs(other, gated, good), CODE)[0] == "ok"


def test_no_envelope_is_envelope_missing():
    assert select_media_node([], CODE) == ("envelope_missing", None)
    node, parse = parse_post_document(post_html([]), CODE)
    assert node is None and parse.outcome == "envelope_missing" and parse.scripts == 38


# --------------------------------------------------------------------------- resumo seguro


@pytest.mark.parametrize("typename", ["Tipo Com Espaço", "X" * 41, "tipo_1", "https://fake.invalid/x", 123, None, "",
                                      CODE.replace("001", "ABC")])
def test_typename_outside_the_pattern_is_not_kept(typename):
    code = CODE.replace("001", "ABC")   # só letras: um typename igual ao shortcode também não entra
    media = envelope(media_node("video", code), typename=typename)
    _, parse = parse_post_document(post_html([data_sjs_script(media_payload(media))]), code)
    assert parse.outcome == "ok" and parse.typename is None


@pytest.mark.parametrize("kind, gating", [("video", None), ("photo", None), ("carousel", None), ("video", "FAKE")])
def test_nothing_from_the_html_reaches_the_summary(kind, gating):
    node = media_node(kind, CODE)
    _, parse = parse_post_document(post_page(node, gating=gating), CODE)
    text = summary(parse) + repr(parse)
    for secret in (CODE, "cdninstagram", "fbcdn", "FAKE_OH", "Dono_Fake", "dono_fake", "Legenda", PRELOADER,
                   "3200000000000000001"):
        assert secret not in text, secret
    assert set(dataclasses.asdict(parse)) == {"scripts", "json_scripts", "envelopes", "preloader_observed", "outcome",
                                              "typename"}


def test_observed_constants():
    assert (pd.CACHE_MARKER, pd.ROOT_FIELD, pd.UNGATED_FIELD, pd.GATING_FIELD) == (
        "RelayPrefetchedStreamCache", "xig_polaris_media", "if_not_gated_logged_out", "gating_ruling")
    assert pd.OBSERVED_AT == "2026-10-10" and pd.MAX_DOCUMENT_BYTES == 4 * 1024 * 1024
    assert PRELOADER.startswith(pd.OBSERVED_PRELOADER_PREFIX)
