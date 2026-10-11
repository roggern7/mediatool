"""Regra de uso dos fluxos separados em `normalize_post`/`normalize_node` (web/normalize.py). Sem rede; valores FAKE.

Só o nó de Reel segue a forma observada no real (3 `video_versions` sem largura/altura, original 1080x1920)."""

from __future__ import annotations

import pytest

from app.integrations.instagram.downloader import RemoteMedia
from app.integrations.instagram.web.normalize import NormalizeStats, normalize_node, normalize_post
from dash_fixtures import OBSERVED_MPD, dash_url, media_node_with_dash, simple_mpd
from post_document_fixtures import media_node, video_url

CODE = "NORMaaaa001"
DASH_NAMES = ("dash_video_url", "dash_audio_url", "dash_video_bytes", "dash_audio_bytes", "dash_width",
              "dash_height", "dash_duration")


def one(node: dict, stats: NormalizeStats | None = None) -> RemoteMedia:
    [item] = normalize_post(node, stats=stats)
    return item


def assert_progressive(item: RemoteMedia) -> None:
    assert all(getattr(item, name) is None for name in DASH_NAMES), item
    assert item.media_url == video_url(CODE, "preel1", 101)


def test_observed_shape_uses_dash_and_keeps_the_progressive_as_media_url():
    """Progressivo sem dimensões (0), original 1080x1920, DASH 1080x1920 -> DASH."""
    stats = NormalizeStats()
    item = one(media_node_with_dash(CODE, simple_mpd(CODE, video_bytes=111, audio_bytes=22)), stats)
    assert item.media_url == video_url(CODE, "preel1", 101), "media_url continua sendo o MP4 direto (fallback)"
    assert (item.dash_video_url, item.dash_audio_url) == (dash_url(CODE, "v"), dash_url(CODE, "a"))
    assert (item.dash_video_bytes, item.dash_audio_bytes, item.dash_width, item.dash_height) == (111, 22, 1080, 1920)
    assert item.dash_duration == pytest.approx(2.0)
    assert (item.width, item.height, item.duration) == (1080, 1920, pytest.approx(2.0))
    assert item.is_reel and item.media_type == "video" and item.extension == "mp4"
    assert (stats.dash, stats.dash_chosen) == ({"ok": 1}, 1)


def test_real_sanitized_manifest_selects_1080p59():
    item = one(media_node_with_dash(CODE, OBSERVED_MPD.read_text(encoding="utf-8")))
    assert (item.dash_width, item.dash_height, item.dash_video_bytes, item.dash_audio_bytes) == (
        1080, 1920, 28366712, 758004)
    assert item.dash_duration == pytest.approx(69.288345) and "rep9.mp4" in item.dash_video_url


def test_dash_below_the_original_resolution_keeps_the_progressive():
    """DASH máximo 720 com original 1080: não troca H.264 por VP9 sem alcançar a resolução original."""
    stats = NormalizeStats()
    item = one(media_node_with_dash(CODE, simple_mpd(CODE, width=720, height=1280)), stats)
    assert_progressive(item)
    assert (item.width, item.height) == (1080, 1920), "sem DASH, largura/altura seguem a regra de sempre"
    assert (stats.dash, stats.dash_chosen) == ({"ok": 1}, 0)


def test_progressive_that_declares_the_same_area_keeps_the_progressive():
    node = media_node_with_dash(CODE, simple_mpd(CODE))
    node["video_versions"][0].update(width=1080, height=1920)
    stats = NormalizeStats()
    assert_progressive(one(node, stats))
    assert stats.dash_chosen == 0


def test_progressive_that_declares_a_smaller_area_uses_dash():
    node = media_node_with_dash(CODE, simple_mpd(CODE))
    for version in node["video_versions"]:
        version.update(width=720, height=1280)
    item = one(node)
    assert item.dash_video_url == dash_url(CODE, "v") and (item.width, item.height) == (1080, 1920)


def test_without_original_dimensions_a_smaller_dash_still_wins_over_an_undeclared_progressive():
    item = one(media_node_with_dash(CODE, simple_mpd(CODE, width=540, height=960), original=None))
    assert (item.dash_width, item.dash_height, item.width, item.height) == (540, 960, 540, 960)


@pytest.mark.parametrize("manifest, outcome", [
    (None, "absent"), ("", "absent"), ("<MPD FAKE/>", "xml_error"), ("<MPD/>", "unsupported_form"),
    ('<!DOCTYPE x><MPD xmlns="urn:mpeg:dash:schema:mpd:2011"/>', "unsafe_xml"),
])
def test_unusable_manifest_falls_back_without_error(manifest, outcome):
    stats = NormalizeStats()
    assert_progressive(one(media_node_with_dash(CODE, manifest), stats))
    assert (stats.dash, stats.dash_chosen, stats.items) == ({outcome: 1}, 0, 1)


def test_node_without_the_manifest_key_counts_as_absent():
    stats = NormalizeStats()
    assert_progressive(one(media_node("video", CODE), stats))
    assert stats.dash == {"absent": 1}


def test_profile_listing_ignores_the_manifest():
    """Fluxo por perfil: `normalize_node` sem `dash` (padrão) nem olha o manifesto, mesmo que ele seja válido."""
    node = media_node_with_dash(CODE, simple_mpd(CODE))
    stats = NormalizeStats()
    [item] = normalize_node(node, profile="dono_fake", stats=stats)
    assert_progressive(item)
    assert (stats.dash, stats.dash_chosen) == ({}, 0)
    [explicit] = normalize_node(node, profile="dono_fake", dash=False)
    assert explicit == item and (item.width, item.height) == (1080, 1920)


def test_photo_never_reads_a_manifest():
    node = media_node("photo", CODE)
    node["video_dash_manifest"] = simple_mpd(CODE)
    stats = NormalizeStats()
    item = one(node, stats)
    assert item.media_type == "image" and item.dash_video_url is None and stats.dash == {}


def test_stats_count_each_video_of_a_carousel():
    """Carrossel no documento público é HIPÓTESE (não observado): cada vídeo conta o próprio manifesto."""
    node = media_node("carousel", CODE)
    node["carousel_media"][1]["video_dash_manifest"] = simple_mpd(CODE)
    stats = NormalizeStats()
    items = normalize_post(node, stats=stats)
    assert [bool(item.dash_video_url) for item in items] == [False, True, False, False]
    assert (stats.dash, stats.dash_chosen) == ({"ok": 1, "absent": 1}, 1)


def test_repr_of_remote_media_hides_the_dash_urls():
    text = repr(one(media_node_with_dash(CODE, simple_mpd(CODE))))
    assert "/dash/" not in text and "dash_width=1080" in text
