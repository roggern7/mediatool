import pytest

from app.integrations.instagram.validation import (
    parse_profile_input,
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
