"""web/queries.py: QuerySpec, overrides de doc_id e variáveis do PolarisProfilePostsQuery (offline)."""

from __future__ import annotations

import json
import logging

from app.integrations.instagram.web.queries import (
    KNOWN_QUERIES,
    POST_BY_SHORTCODE,
    PROFILE_POSTS,
    QuerySpec,
    post_variables,
    profile_posts_variables,
    resolve_spec,
)

RELAY = {
    "__relay_internal__pv__PolarisMultiCaptionCarouselEnabledrelayprovider": False,
    "__relay_internal__pv__PolarisShortDramaEnabledrelayprovider": False,
    "__relay_internal__pv__PolarisReelsRecoDebugOverlayEnabledrelayprovider": False,
}
ENV_KEY = "MEDIA_TOOL_IG_DOC_POLARISPROFILEPOSTSQUERY"


def test_profile_posts_spec_matches_contract():
    assert PROFILE_POSTS == QuerySpec("PolarisProfilePostsQuery", "28991540097136703",
                                      "xdt_api__v1__feed__user_timeline_graphql_connection", "2026-10-03", RELAY)
    assert KNOWN_QUERIES == ("PolarisProfilePostsQuery", "PolarisPostActionLoadPostQueryQuery")


# --------------------------------------------------------------------------- consulta de post (NÃO verificada)

POST_DOC_ENV = "MEDIA_TOOL_IG_DOC_POLARISPOSTACTIONLOADPOSTQUERYQUERY"
POST_ROOT_ENV = "MEDIA_TOOL_IG_ROOT_POLARISPOSTACTIONLOADPOSTQUERYQUERY"


def test_post_spec_is_marked_unverified():
    assert POST_BY_SHORTCODE == QuerySpec("PolarisPostActionLoadPostQueryQuery", "8845758582119845",
                                          "xdt_shortcode_media", "unverified", {})


def test_post_variables():
    variables = post_variables(POST_BY_SHORTCODE, "FAKEPOSTIMG1")
    assert variables == {"shortcode": "FAKEPOSTIMG1", "fetch_tagged_user_count": None, "hoisted_comment_id": None,
                         "hoisted_reply_id": None}


def test_post_doc_id_override_by_setting_and_env():
    assert resolve_spec(POST_BY_SHORTCODE, {}, {}) is POST_BY_SHORTCODE
    assert resolve_spec(POST_BY_SHORTCODE, {"polarispostactionloadpostqueryquery": "777777"}, {}).doc_id == "777777"
    assert resolve_spec(POST_BY_SHORTCODE, {}, {POST_DOC_ENV: "888888"}).doc_id == "888888"
    # o override de uma consulta não vaza para a outra
    assert resolve_spec(PROFILE_POSTS, {"PolarisPostActionLoadPostQueryQuery": "777777"},
                        {POST_DOC_ENV: "888888"}) is PROFILE_POSTS


def test_root_field_override_by_env_only_for_that_query():
    env = {POST_ROOT_ENV: " xdt_api__v1__media__shortcode__web_info ", POST_DOC_ENV: "999999"}
    spec = resolve_spec(POST_BY_SHORTCODE, {}, env)
    assert spec.root_field == "xdt_api__v1__media__shortcode__web_info" and spec.doc_id == "999999"
    assert spec.name == POST_BY_SHORTCODE.name
    assert resolve_spec(PROFILE_POSTS, {}, env) is PROFILE_POSTS


def test_invalid_root_field_override_ignored_with_safe_warning(caplog):
    caplog.set_level(logging.WARNING)
    for bad in ("FAKE Root-Value;1", "Xdt_Upper", "q7", "x" * 121, "raiz\nnova", "ráiz_falsa"):
        caplog.clear()
        assert resolve_spec(POST_BY_SHORTCODE, {}, {POST_ROOT_ENV: bad}) is POST_BY_SHORTCODE
        assert "campo raiz inválido" in caplog.text and bad not in caplog.text


def test_resolve_without_overrides_keeps_builtin():
    assert resolve_spec(PROFILE_POSTS, {}, {}) is PROFILE_POSTS
    assert resolve_spec(PROFILE_POSTS, {}) is PROFILE_POSTS


def test_setting_override_by_name_case_insensitive():
    spec = resolve_spec(PROFILE_POSTS, {"polarisprofilepostsquery": "1234567890123"}, {})
    assert spec.doc_id == "1234567890123"
    assert spec.name == PROFILE_POSTS.name and spec.root_field == PROFILE_POSTS.root_field


def test_setting_has_precedence_over_env():
    spec = resolve_spec(PROFILE_POSTS, {"PolarisProfilePostsQuery": "11111"}, {ENV_KEY: "22222"})
    assert spec.doc_id == "11111"


def test_env_used_when_no_setting():
    assert resolve_spec(PROFILE_POSTS, {}, {ENV_KEY: "22222"}).doc_id == "22222"


def test_os_environ_is_not_read(monkeypatch):
    monkeypatch.setenv(ENV_KEY, "33333333")
    assert resolve_spec(PROFILE_POSTS, {}).doc_id == PROFILE_POSTS.doc_id
    assert resolve_spec(PROFILE_POSTS, {}, {ENV_KEY: "44444444"}).doc_id == "44444444"


def test_invalid_overrides_ignored_with_safe_warning(caplog):
    caplog.set_level(logging.WARNING)
    bad_env = "FAKE_TOKEN_IN_ENV_abc"
    bad_setting = "1234"  # curto demais
    spec = resolve_spec(PROFILE_POSTS, {"PolarisProfilePostsQuery": bad_setting}, {ENV_KEY: bad_env})
    assert spec.doc_id == PROFILE_POSTS.doc_id
    assert "PolarisProfilePostsQuery" in caplog.text
    assert bad_env not in caplog.text and "'1234'" not in caplog.text


def test_invalid_setting_falls_back_to_valid_env():
    spec = resolve_spec(PROFILE_POSTS, {"PolarisProfilePostsQuery": "abc"}, {ENV_KEY: "44444"})
    assert spec.doc_id == "44444"


def test_too_long_non_string_or_non_ascii_override_ignored():
    for bad in ("1" * 26, 12345, "\u0661\u0662\u0663\u0664\u0665", "12345\n6"):
        assert resolve_spec(PROFILE_POSTS, {"PolarisProfilePostsQuery": bad}, {}).doc_id == "28991540097136703"
    assert resolve_spec(PROFILE_POSTS, {}, {ENV_KEY: "\uff11\uff12\uff13\uff14\uff15"}).doc_id == "28991540097136703"


def test_unknown_override_names_ignored_with_safe_warning(caplog):
    caplog.set_level(logging.WARNING)
    spec = resolve_spec(PROFILE_POSTS, {"FAKE_UNKNOWN_QUERY_NAME": "55555", "PolarisProfilePostsQuery": "66666"}, {})
    assert spec.doc_id == "66666"
    assert "desconhecido" in caplog.text
    assert "FAKE_UNKNOWN_QUERY_NAME" not in caplog.text and "55555" not in caplog.text


def test_first_page_variables_exactly_as_observed():
    variables = profile_posts_variables(PROFILE_POSTS, "perfil_fake", 12, None)
    assert variables == {
        "data": {"count": 12, "include_reel_media_seen_timestamp": True, "include_relationship_info": True,
                 "latest_besties_reel_media": True, "latest_reel_media": True},
        "username": "perfil_fake",
        **RELAY,
    }
    assert list(variables)[:2] == ["data", "username"]
    assert "after" not in json.dumps(variables)


def test_next_page_variables_hypothesis_relay_connection():
    variables = profile_posts_variables(PROFILE_POSTS, "perfil_fake", 12, "FAKE_CURSOR")
    assert variables["after"] == "FAKE_CURSOR"
    assert variables["before"] is None and variables["last"] is None
    assert variables["first"] == 12
    assert variables["data"]["count"] == 12
    assert variables["username"] == "perfil_fake"
    for key, value in RELAY.items():
        assert variables[key] is value
