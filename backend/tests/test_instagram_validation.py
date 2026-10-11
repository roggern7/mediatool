import pytest

from app.integrations.instagram.validation import (
    parse_post_link,
    parse_profile_input,
    post_path_kind,
    validate_instagram_url,
    validate_shortcode,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("instagram", "instagram"),
        ("@Instagram", "instagram"),
        ("  user.name_1  ", "user.name_1"),
        ("https://www.instagram.com/some.user/", "some.user"),
        ("https://instagram.com/some_user", "some_user"),
        ("https://www.instagram.com/some.user/reels/?igsh=abc", "some.user"),
        ("instagram.com/some.user", "some.user"),
        ("www.instagram.com/some.user/", "some.user"),
        ("a" * 30, "a" * 30),
    ],
)
def test_parse_profile_input_valid(value, expected):
    assert parse_profile_input(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "@",
        "a" * 31,
        "user..name",
        "...",
        "user name",
        "user-name",
        "usér",
        "user\u200bname",
        "ｉｎｓｔａｇｒａｍ",
        "p",
        "explore",
        "reel",
        "http://www.instagram.com/user/",
        "file:///C:/Windows/win.ini",
        "https://evil.com/user/",
        "https://instagram.com.evil.com/user/",
        "https://user:pass@www.instagram.com/user/",
        "https://www.instagram.com:8443/user/",
        "https://www.instagram.com/",
        "https://www.instagram.com/p/ABCDEFGHIJK/",
        "https://www.instagram.com/explore/tags/x/",
        "https://www.instagram.com/../etc/passwd",
        "https://www.instagram.com/%2e%2e/user",
        "x" * 600,
        None,
        123,
    ],
)
def test_parse_profile_input_invalid(value):
    with pytest.raises(ValueError):
        parse_profile_input(value)


def test_validate_instagram_url_normalizes():
    assert validate_instagram_url("https://instagram.com/p/ABCDEFGHIJK/?igsh=1#x") == (
        "https://www.instagram.com/p/ABCDEFGHIJK/"
    )
    assert validate_instagram_url("https://WWW.Instagram.com:443/reel/ABC12/") == (
        "https://www.instagram.com/reel/ABC12/"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://www.instagram.com/p/ABCDEFGHIJK/",
        "file://www.instagram.com/p/x",
        "https://cdninstagram.com/p/x",
        "https://www.instagram.com.attacker.net/",
        "https://@www.instagram.com/",
        "https://www.instagram.com/a/../b",
        "https://www.instagram.com\\@evil.com/",
        "javascript:alert(1)",
        "https://www.instagram.com/" + "a" * 600,
    ],
)
def test_validate_instagram_url_invalid(url):
    with pytest.raises(ValueError):
        validate_instagram_url(url)


@pytest.mark.parametrize("code", ["ABCDEFGHIJK", "C0dE-ab_123", "a" * 40])
def test_validate_shortcode_valid(code):
    assert validate_shortcode(code) == code


@pytest.mark.parametrize("code", ["", "abc", "ABC/DEF", "../../x", "abc def", "é" * 11, "a" * 65, None])
def test_validate_shortcode_invalid(code):
    with pytest.raises(ValueError):
        validate_shortcode(code)


P = "https://www.instagram.com/p/ABCDEFGHIJK/"
R = "https://www.instagram.com/reel/ABCDEFGHIJK/"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://www.instagram.com/p/ABCDEFGHIJK/", ("ABCDEFGHIJK", P)),
        ("https://www.instagram.com/reel/ABCDEFGHIJK/", ("ABCDEFGHIJK", R)),
        ("https://www.instagram.com/reels/ABCDEFGHIJK/", ("ABCDEFGHIJK", R)),
        ("https://www.instagram.com/some.user/p/ABCDEFGHIJK/", ("ABCDEFGHIJK", P)),
        ("https://www.instagram.com/some.user/reel/ABCDEFGHIJK/", ("ABCDEFGHIJK", R)),
        ("http://www.instagram.com/p/ABCDEFGHIJK/", ("ABCDEFGHIJK", P)),
        ("HTTP://www.instagram.com/p/ABCDEFGHIJK/", ("ABCDEFGHIJK", P)),
        ("instagram.com/p/ABCDEFGHIJK/", ("ABCDEFGHIJK", P)),
        ("www.instagram.com/reel/ABCDEFGHIJK", ("ABCDEFGHIJK", R)),
        ("https://instagram.com/p/ABCDEFGHIJK", ("ABCDEFGHIJK", P)),
        ("https://www.instagram.com/p/ABCDEFGHIJK/?igsh=abc123&img_index=2#x", ("ABCDEFGHIJK", P)),
        ("https://www.instagram.com/reel/ABCDEFGHIJK?utm_source=ig_web", ("ABCDEFGHIJK", R)),
        ("https://www.instagram.com/P/ABCDEFGHIJK/", ("ABCDEFGHIJK", P)),
        ("  https://www.instagram.com/p/C0dE-ab_123/  ", ("C0dE-ab_123", "https://www.instagram.com/p/C0dE-ab_123/")),
        ("https://WWW.Instagram.com:443/p/ABCDEFGHIJK/", ("ABCDEFGHIJK", P)),
    ],
)
def test_parse_post_link_valid(value, expected):
    assert parse_post_link(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "ABCDEFGHIJK",
        "texto solto",
        "p/ABCDEFGHIJK",
        "/p/ABCDEFGHIJK/",
        "https://www.instagram.com/some.user/",
        "https://www.instagram.com/",
        "https://www.instagram.com/explore/",
        "https://www.instagram.com/explore/tags/x/",
        "https://www.instagram.com/stories/some.user/1234567890/",
        "https://www.instagram.com/tv/ABCDEFGHIJK/",
        "https://www.instagram.com/p/",
        "https://www.instagram.com/p/abc/",
        "https://www.instagram.com/p/ABC$DEFGHIJ/",
        "https://www.instagram.com/p/ABCDEFGHIJK/extra/",
        "https://www.instagram.com/a/b/p/ABCDEFGHIJK/",
        "https://www.instagram.com/explore/p/ABCDEFGHIJK/",
        "https://www.instagram.com/user..name/p/ABCDEFGHIJK/",
        "https://evil.com/p/ABCDEFGHIJK/",
        "https://instagram.com.evil.com/p/ABCDEFGHIJK/",
        "https://cdninstagram.com/p/ABCDEFGHIJK/",
        "https://user:pass@www.instagram.com/p/ABCDEFGHIJK/",
        "https://@www.instagram.com/p/ABCDEFGHIJK/",
        "https://www.instagram.com/p/ABCDE%46GHIJK/",
        "https://www.instagram.com/%70/ABCDEFGHIJK/",
        "https://www.instagram.com:8443/p/ABCDEFGHIJK/",
        "http://www.instagram.com:80/p/ABCDEFGHIJK/",
        "ftp://www.instagram.com/p/ABCDEFGHIJK/",
        "https://www.instagram.com/p/../p/ABCDEFGHIJK/",
        "https://www.instagram.com\\p\\ABCDEFGHIJK",
        "https://www.instagram.com/p/ABCDÉFGHIJK/",
        "https://www.instagram.com/p/" + "a" * 600,
        None,
        123,
    ],
)
def test_parse_post_link_invalid(value):
    with pytest.raises(ValueError):
        parse_post_link(value)


def test_parse_post_link_messages_do_not_echo_input():
    for value in ("https://www.instagram.com/some.user/?token=FAKEBAD1", "https://evil.com/p/FAKEBAD1x/",
                  "https://www.instagram.com/p/FAKE$BAD1/"):
        with pytest.raises(ValueError) as info:
            parse_post_link(value)
        assert "FAKE" not in str(info.value) and "evil" not in str(info.value)


def test_parse_post_link_same_code_in_p_and_reel_is_same_shortcode():
    assert parse_post_link(P)[0] == parse_post_link(R)[0] == parse_post_link(R.replace("/reel/", "/reels/"))[0]


@pytest.mark.parametrize("url, kind", [
    ("https://www.instagram.com/p/ABCDE12345/", "p"),
    ("https://www.instagram.com/reel/ABCDE12345/", "reel"),
    ("https://www.instagram.com/reels/ABCDE12345/?igsh=FAKE", "reel"),
    ("instagram.com/dono_fake/p/ABCDE12345", "p"),
    ("https://www.instagram.com/dono_fake/", None),
    ("https://www.instagram.com/tv/ABCDE12345/", None),
    ("https://evil.example.com/reel/ABCDE12345/", None),
    ("ABCDE12345", None),
    ("", None),
    (None, None),
])
def test_post_path_kind(url, kind):
    assert post_path_kind(url) == kind
