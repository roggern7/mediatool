from __future__ import annotations

import dataclasses

import httpx
import pytest

from app.integrations.instagram.web.errors import InstagramWebError
from app.integrations.instagram.web.http import InstagramWebHttp
from app.integrations.instagram.web.profile_snapshot import (
    MAX_COUNT,
    ProfileSnapshot,
    fetch_profile_snapshot,
    parse_posts_text,
    parse_profile_document,
)
from profile_fakes import (
    ERROR_ROUTE,
    MISSING,
    NOW,
    clip,
    clips_connection,
    highlights_connection,
    profile_html,
    user_envelope,
)


def parse(html: str, username: str = "perfil", now: float = NOW):
    return parse_profile_document(html, username, now)


def fetch(handler, username: str = "perfil", now: float = NOW):
    http = InstagramWebHttp.anonymous(transport=httpx.MockTransport(handler))
    try:
        return fetch_profile_snapshot(http, username, now=lambda: now)
    finally:
        http.close()


def html_response(text: str, status: int = 200, **headers) -> httpx.Response:
    return httpx.Response(status, text=text, headers={"content-type": "text/html; charset=utf-8", **headers})


def assert_all_unverified(snap: ProfileSnapshot) -> None:
    assert (snap.stories, snap.highlights, snap.posts_state, snap.views_state) == ("unverified",) * 4
    assert snap.posts_count is None and snap.views_total is None and snap.views_reels is None


# ---- status -------------------------------------------------------------------------------------------------------
def test_active_profile_reads_every_field():
    snap = parse(profile_html())
    assert snap.account_status == "active" and snap.display_name == "Perfil Fake"
    assert snap.stories == "yes" and snap.highlights == "yes"
    assert (snap.posts_state, snap.posts_count, snap.posts_exact, snap.posts_text) == ("known", 8630, 1, None)
    assert (snap.views_state, snap.views_total, snap.views_reels) == ("known", 150, 2)
    assert snap.error_code is None


def test_snapshot_has_no_html_and_is_frozen():
    fields = {field.name for field in dataclasses.fields(ProfileSnapshot)}
    assert not any("html" in name or "text" == name for name in fields)
    with pytest.raises(dataclasses.FrozenInstanceError):
        parse(profile_html()).stories = "no"


def test_username_comparison_ignores_case():
    assert parse(profile_html(user_envelope("PerFil")), "perfil").account_status == "active"


def test_private_profile():
    snap = parse(profile_html(user_envelope(is_private=True, latest_reel_media=0,
                                            lox_highlights_connection=highlights_connection(0))))
    assert snap.account_status == "private"
    assert snap.stories == "unverified" and snap.highlights == "unverified" and snap.views_state == "unverified"
    assert snap.posts_state == "known"


@pytest.mark.parametrize("html", [
    "<html><body>" + ERROR_ROUTE + "</body></html>",
    '<html>"canonicalRouteName":"comet.igweb.PolarisErrorRoute"</html>',
    '<html>"tracePolicy":"polaris.httpErrorPage"</html>',
])
def test_inexistent_with_200_is_unavailable(html):
    snap = parse(html)
    assert snap.account_status == "unavailable"
    assert snap.error_message == "Perfil não encontrado, removido ou restrito para visitantes sem login."
    assert_all_unverified(snap)


def test_unpublished_is_unavailable():
    assert parse(profile_html(user_envelope(is_unpublished=True))).account_status == "unavailable"


def test_html_without_envelope_nor_marker_is_unverified():
    snap = parse("<html><body>nada aqui: PolarisErrorRoute solto não é a marca exata</body></html>")
    assert snap.account_status == "unverified" and snap.error_code == "profile_unreadable"
    assert_all_unverified(snap)


def test_broken_json_is_unverified_even_with_marker():
    html = '<script type="application/json" data-sjs>{quebrado</script>' + ERROR_ROUTE
    assert parse(html).account_status == "unverified"


def test_valid_user_envelope_wins_over_error_marker():
    assert parse(profile_html(extra=ERROR_ROUTE)).account_status == "active"


def test_other_username_is_unverified_even_with_marker():
    snap = parse(profile_html(user_envelope("outro"), extra=ERROR_ROUTE))
    assert snap.account_status == "unverified" and snap.error_code == "username_mismatch"
    assert snap.display_name is None
    assert_all_unverified(snap)


@pytest.mark.parametrize("overrides", [
    {"is_private": "false"}, {"is_private": None}, {"is_private": MISSING}, {"is_private": 0},
    {"is_unpublished": MISSING}, {"is_unpublished": None},
])
def test_undefined_flags_are_unverified(overrides):
    assert parse(profile_html(user_envelope(**overrides))).account_status == "unverified"


def test_only_clips_envelope_is_unverified():
    assert parse(profile_html(None), "perfil").account_status == "unverified"


# ---- stories ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("value", "private", "expected"), [
    (NOW - 3600, False, "yes"),
    (NOW - 86_399, False, "yes"),
    (NOW - 86_400, False, "no"),            # expirou: 24 h exatas
    (NOW - 90_000, True, "no"),
    (NOW + 600, False, "yes"),              # timestamp no futuro
    (NOW - 3600, True, "yes"),
    (0, False, "no"),
    (0, True, "unverified"),
    (MISSING, False, "unverified"),
    (None, False, "unverified"),
    ("123", False, "unverified"),
    (True, False, "unverified"),
    (-5, False, "unverified"),
])
def test_stories(value, private, expected):
    html = profile_html(user_envelope(latest_reel_media=value, is_private=private))
    assert parse(html).stories == expected


# ---- destaques ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("value", "private", "expected"), [
    (highlights_connection(3), False, "yes"),
    (highlights_connection(3), True, "yes"),
    (highlights_connection(0), False, "no"),
    (highlights_connection(0), True, "unverified"),
    (MISSING, False, "unverified"),
    (None, False, "unverified"),
    ({"page_info": {}}, False, "unverified"),
    ({"edges": "x"}, False, "unverified"),
    ([], False, "unverified"),
])
def test_highlights(value, private, expected):
    html = profile_html(user_envelope(lox_highlights_connection=value, is_private=private))
    assert parse(html).highlights == expected


# ---- posts --------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("text", "expected"), [
    ("686M seguidores, seguindo 163, 8,630 posts — Veja as fotos", (8630, 1, None)),
    ("686M seguidores, 163 seguindo, 8,630 posts — Veja", (8630, 1, None)),
    ("1 seguidor, seguindo 2, 139 posts — x", (139, 1, None)),
    ("10 seguidores, 4,986 posts", (4986, 1, None)),
    ("10 seguidores, 8.630 posts — x", (8630, 1, None)),
    ("10 seguidores, 1,234,567 posts — x", (1234567, 1, None)),
    ("10 seguidores, 1 post — x", (1, 1, None)),
    ("10 seguidores, 0 posts — x", (0, 1, None)),
    ("10 seguidores, 12 publicações — x", (12, 1, None)),
    ("10 seguidores, 1 publicação — x", (1, 1, None)),
    ("10 followers, 12 Posts - See Instagram photos", (12, 1, None)),
    ("10 seguidores, 33K posts — x", (33000, 0, "33K")),
    ("10 seguidores, 1,2K posts — x", (1200, 0, "1,2K")),
    ("10 seguidores, 1.2K posts — x", (1200, 0, "1.2K")),
    ("10 seguidores, 3M posts — x", (3000000, 0, "3M")),
    ("10 seguidores, 3m publicações — x", (3000000, 0, "3m")),
])
def test_posts_text(text, expected):
    assert parse_posts_text(text) == expected


@pytest.mark.parametrize("text", [
    None, "", "sem número nenhum", "10 seguidores — o perfil tem 5 posts na bio", "10 seguidores, 1,2 posts — x",
    "10 seguidores, 12,34 posts — x", "10 seguidores, 1,234K posts — x", "10 seguidores, 1.234K posts — x",
    "10 seguidores, 99999999999999999999 posts — x", "10 seguidores, 9223372036854775808 posts — x",
    "10 seguidores, 99999999999999999999K posts — x", "10 seguidores, " + "9" * 400 + "K posts — x",
    "10 seguidores, " + "9" * 5000 + " posts — x",
])
def test_posts_unreadable(text):
    assert parse_posts_text(text) is None


def test_posts_limit_is_accepted():
    assert parse_posts_text(f"10 seguidores, {MAX_COUNT} posts — x") == (MAX_COUNT, 1, None)


def test_posts_out_of_range_on_document_is_unverified():
    snap = parse(profile_html(og="1 seguidor, 99999999999999999999 posts &#x2014; x"))
    assert snap.posts_state == "unverified" and snap.posts_count is None and snap.account_status == "active"


def test_posts_approximate_on_document():
    snap = parse(profile_html(og="10 seguidores, seguindo 2, 33K posts &#x2014; Veja"))
    assert (snap.posts_state, snap.posts_count, snap.posts_exact, snap.posts_text) == ("known", 33000, 0, "33K")


def test_posts_fall_back_to_name_description():
    snap = parse(profile_html(og=None, description="10 seguidores, 2 seguindo, 77 posts &#x2014; x"))
    assert snap.posts_count == 77


def test_posts_absent_is_unverified():
    snap = parse(profile_html(og=None))
    assert snap.posts_state == "unverified" and snap.posts_count is None and snap.account_status == "active"


def test_all_media_count_integer_counts_as_exact():
    snap = parse(profile_html(user_envelope(all_media_count=33123), og="1 seguidor, 33K posts &#x2014; x"))
    assert (snap.posts_count, snap.posts_exact) == (33123, 1)
    assert parse(profile_html(user_envelope(all_media_count=None))).posts_count == 8630


# ---- views --------------------------------------------------------------------------------------------------------
def test_views_sum_is_first_page_only_and_flags_partial():
    edges = [clip(10), clip(MISSING), clip(None), clip("7"), clip(True), clip(-1), clip(5), {"node": None}, "x"]
    snap = parse(profile_html(connection=clips_connection(edges, has_next=True)))
    assert (snap.views_state, snap.views_total, snap.views_reels) == ("known", 15, 2)


def test_views_ignore_reels_with_hidden_counts():
    hidden = clip(0)
    hidden["node"]["like_and_view_counts_disabled"] = True
    snap = parse(profile_html(connection=clips_connection([clip(10), hidden])))
    assert (snap.views_state, snap.views_total, snap.views_reels) == ("known", 10, 1)
    assert parse(profile_html(connection=clips_connection([hidden]))).views_state == "unverified"


def test_absurd_counts_are_never_shown_and_cannot_overflow_the_sum():
    limit = MAX_COUNT
    assert parse(profile_html(connection=clips_connection([clip(limit + 1)]))).views_state == "unverified"
    assert parse(profile_html(og=f"{limit + 1} posts - x")).posts_state == "unverified"
    assert 1000 * 12 * limit < 2**63 - 1      # a soma de um lote inteiro cabe no INTEGER do SQLite


def test_number_with_space_is_never_truncated():
    assert parse(profile_html(og="1 234 posts - x")).posts_state == "unverified"


RESTRICTED_PAGE = ('<meta property="og:description" content="820K seguidores, seguindo 22, 51 posts &#x2014; x" />'
                   '<script>{"canonicalRouteName":"comet.igweb.PolarisLoggedOutDesktopWWWProfileReelsTabRoute"}</script>'
                   '<script>{"xig_user_by_username":null}</script>')


def test_restricted_profile_keeps_only_the_posts():
    snap = parse(RESTRICTED_PAGE)
    assert (snap.account_status, snap.posts_state, snap.posts_count, snap.posts_exact) == ("restricted", "known", 51, 1)
    assert snap.error_code == "profile_restricted"
    assert (snap.stories, snap.highlights, snap.views_state) == ("unverified", "unverified", "unverified")


def test_null_user_is_only_restricted_with_all_the_evidence():
    without_posts = RESTRICTED_PAGE.replace("51 posts", "sem contagem")
    without_route = RESTRICTED_PAGE.replace("ProfileReelsTabRoute", "OutraRota")
    without_null = RESTRICTED_PAGE.replace('"xig_user_by_username":null', '"outro":null')
    for page in (without_posts, without_route, without_null):
        snap = parse(page)
        assert snap.account_status == "unverified" and snap.error_code == "profile_unreadable"


def test_views_none_without_reels():
    for html in (profile_html(user_envelope(has_any_clips=False), clips_connection([])),
                 profile_html(user_envelope(has_any_clips=False), None)):
        snap = parse(html)
        assert (snap.views_state, snap.views_total, snap.views_reels) == ("none", None, None)


@pytest.mark.parametrize(("has_clips", "connection"), [
    (True, None), (MISSING, None), (None, None),
    (True, clips_connection([])), (MISSING, clips_connection([])),
    (True, clips_connection([clip(MISSING)])), (False, clips_connection([clip(MISSING)])),
    (True, {"edges": "x"}),
])
def test_views_unverified(has_clips, connection):
    snap = parse(profile_html(user_envelope(has_any_clips=has_clips), connection))
    assert (snap.views_state, snap.views_total, snap.views_reels) == ("unverified", None, None)


@pytest.mark.parametrize("connection", [
    {"edges": {}}, {"edges": ""}, {"edges": 0}, {"edges": None}, {}, {"edges": "x"}, "", 0, [], "x",
])
def test_malformed_connection_is_never_no_reels(connection):
    snap = parse(profile_html(user_envelope(has_any_clips=False), connection))
    assert (snap.views_state, snap.views_total, snap.views_reels) == ("unverified", None, None)


@pytest.mark.parametrize("disabled", [True, 1, "sim"])
def test_hidden_counts_stay_out_of_the_sum(disabled):
    hidden = clip(0)
    hidden["node"]["like_and_view_counts_disabled"] = disabled
    snap = parse(profile_html(connection=clips_connection([hidden, clip(7)])))
    assert (snap.views_state, snap.views_total, snap.views_reels) == ("known", 7, 1)


@pytest.mark.parametrize("edges", [[clip(2**63)], [clip(2**63 - 1), clip(1)]])
def test_views_out_of_range_are_unverified(edges):
    snap = parse(profile_html(connection=clips_connection(edges)))
    assert (snap.views_state, snap.views_total, snap.views_reels) == ("unverified", None, None)


def test_stories_timestamp_out_of_range_is_unverified():
    assert parse(profile_html(user_envelope(latest_reel_media=2**63))).stories == "unverified"


def test_views_of_another_pk_are_ignored():
    snap = parse(profile_html(clips_pk="999"))
    assert snap.views_state == "unverified"


# ---- texto não confiável ------------------------------------------------------------------------------------------
def test_display_name_drops_control_chars_and_lone_surrogates():
    snap = parse(profile_html(user_envelope(full_name="A\x00B\x1f\ud83d C \U0001f600")))
    assert snap.display_name == "AB C \U0001f600"


@pytest.mark.parametrize("value", [None, 5, "", "\x00"])
def test_display_name_absent(value):
    assert parse(profile_html(user_envelope(full_name=value))).display_name is None


# ---- HTTP ---------------------------------------------------------------------------------------------------------
def test_fetch_is_one_anonymous_get_of_reels_tab():
    requests: list[httpx.Request] = []

    def handler(request):
        requests.append(request)
        return html_response(profile_html(), **{"set-cookie": "csrftoken=FAKEcsrf; Path=/"})

    assert fetch(handler).account_status == "active"
    assert [(r.method, r.url.path) for r in requests] == [("GET", "/perfil/reels/")]
    assert "cookie" not in requests[0].headers


def test_404_is_unavailable():
    snap = fetch(lambda _r: html_response("x", 404))
    assert snap.account_status == "unavailable"
    assert_all_unverified(snap)


def test_inexistent_with_200_through_fetch():
    assert fetch(lambda _r: html_response(ERROR_ROUTE)).account_status == "unavailable"


@pytest.mark.parametrize(("status", "headers", "code"), [
    (301, {"location": "https://www.instagram.com/outro/"}, "access_refused"),
    (302, {"location": "https://www.instagram.com/accounts/login/"}, "access_refused"),
    (401, {}, "access_refused"),
    (403, {}, "access_refused"),
    (429, {}, "rate_limited"),
    (500, {}, "network_error"),
    (503, {}, "network_error"),
])
def test_refusals_raise_and_never_become_a_snapshot(status, headers, code):
    with pytest.raises(InstagramWebError) as caught:
        fetch(lambda _r: html_response("x", status, **headers))
    assert caught.value.code == code
    assert caught.value.diagnostic["path"] == "/reels/"
    assert caught.value.stage == "profile_snapshot"


def test_timeout_and_network_errors_raise():
    def timeout(_request):
        raise httpx.ReadTimeout("t")

    def broken(_request):
        raise httpx.ConnectError("c")

    for handler, code in ((timeout, "timeout"), (broken, "network_error")):
        with pytest.raises(InstagramWebError) as caught:
            fetch(handler)
        assert caught.value.code == code


def test_non_html_200_raises_contract_changed():
    with pytest.raises(InstagramWebError) as caught:
        fetch(lambda _r: httpx.Response(200, json={"ok": True}))
    assert caught.value.code == "contract_changed"


# ---- Reels da aba Reels (conta que publica só lá) --------------------------------------------------------------------
def reel_pk(epoch_s: int) -> str:
    """pk sintético de um Reel publicado em `epoch_s` (inverso de `reel_taken_at_from_pk`, sem os 25 s)."""
    return str((epoch_s * 1000 - 1314220021721) << 23)


def test_reels_only_account_is_not_empty():
    edges = [clip(10) for _ in range(12)]
    for index, edge in enumerate(edges):
        edge["node"]["pk"] = reel_pk(NOW - 86_400 * index)
    edges[0]["node"]["pk"] = reel_pk(NOW - 86_400 * 400)      # fixado no topo, antigo: não é o mais recente
    og = "2,909 seguidores, seguindo 10, 0 posts &#x2014; Veja as fotos e v&#xed;deos do Instagram"
    snap = parse(profile_html(connection=clips_connection(edges, has_next=True), og=og))
    assert (snap.account_status, snap.posts_state, snap.posts_count) == ("active", "known", 0)
    assert (snap.reels_count, snap.reels_more) == (12, True)
    assert snap.latest_reel_at == "2026-10-31T02:27:05Z"      # NOW - 1 dia, +25 s


def test_reels_fields_follow_the_views_rule():
    assert (parse(profile_html()).reels_count, parse(profile_html()).reels_more) == (2, False)
    none = parse(profile_html(user_envelope(has_any_clips=False), connection=None))
    assert (none.reels_count, none.reels_more, none.latest_reel_at) == (0, False, None)
    for snap in (parse(profile_html(user_envelope(is_private=True))),
                 parse(profile_html(connection={"edges": "x"})),
                 parse(profile_html(connection=clips_connection([]))),        # has_any_clips verdadeiro: não prova 0
                 parse(profile_html(connection={"edges": [clip()]}))):          # sem page_info: "há mais" desconhecido
        assert snap.reels_more is None
    assert parse(profile_html(connection={"edges": [clip()]})).reels_count == 1
    assert parse(profile_html(user_envelope(is_private=True))).reels_count is None
