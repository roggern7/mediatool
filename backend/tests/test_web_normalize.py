"""web/normalize.py: nó do PolarisProfilePostsQuery -> RemoteMedia (fixtures sintéticas, offline)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.integrations.instagram.web.normalize import NormalizeStats, filter_by_content, normalize_node

FIXTURES = Path(__file__).parent / "fixtures" / "instagram_web" / "graphql"
ROOT = "xdt_api__v1__feed__user_timeline_graphql_connection"


def nodes(name: str) -> list[dict]:
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return [edge["node"] for edge in payload["data"][ROOT]["edges"]]


def test_image_picks_largest_candidate():
    stats = NormalizeStats()
    [item] = normalize_node(nodes("image.json")[0], profile="perfil_fake", stats=stats)
    assert item.media_type == "image"
    assert "img1_1080x1350.jpg" in item.media_url
    assert (item.width, item.height) == (1080, 1350)
    assert item.source_post_id == "FAKEIMG0001"
    assert item.post_id == "3100000000000000001" and item.media_id == "3100000000000000001"
    assert item.source_media_index == 0
    assert item.is_reel is False
    assert item.source_url == "https://www.instagram.com/p/FAKEIMG0001/"
    assert item.thumbnail_url is None and item.duration is None
    assert item.taken_at == "2023-11-14T22:13:20Z"
    assert item.extension == "jpg"
    assert item.source_profile == "perfil_fake" and item.source_platform == "instagram"
    assert (stats.nodes, stats.items, stats.skipped_no_url, stats.skipped_unsupported) == (1, 1, 0, 0)


def test_reel_video_largest_tie_first_and_thumbnail():
    reel, _ = nodes("video_reel.json")
    [item] = normalize_node(reel, profile="perfil_fake")
    assert item.media_type == "video" and item.is_reel is True
    assert item.source_url == "https://www.instagram.com/reel/FAKEREEL001/"
    assert "reel1_1080x1920_102.mp4" in item.media_url  # empate 1080x1920: o primeiro
    assert "reel1_1080x1920.jpg" in item.thumbnail_url
    assert (item.width, item.height) == (1080, 1920)
    assert item.duration == 12.5
    assert item.extension == "mp4"


def test_feed_video_is_not_reel():
    _, video = nodes("video_reel.json")
    [item] = normalize_node(video, profile="perfil_fake")
    assert item.media_type == "video" and item.is_reel is False
    assert item.source_url == "https://www.instagram.com/p/FAKEVID0001/"
    assert "vid1_720x720_102.mp4" in item.media_url
    assert item.duration == 7.0


def test_from_reels_marks_video_as_reel():
    _, video = nodes("video_reel.json")
    [item] = normalize_node(video, profile="perfil_fake", from_reels=True)
    assert item.is_reel is True and item.source_url.endswith("/reel/FAKEVID0001/")


def test_mixed_carousel_order_and_skips():
    stats = NormalizeStats()
    items = normalize_node(nodes("carousel_mixed.json")[0], profile="perfil_fake", stats=stats)
    assert [i.source_media_index for i in items] == [0, 1, 4]
    assert [i.media_type for i in items] == ["image", "video", "image"]
    assert [i.media_id for i in items] == ["3100000000000000041", "3100000000000000042", "3100000000000000045"]
    assert all(i.post_id == "3100000000000000004" and i.source_post_id == "FAKECAR0001" for i in items)
    assert all(i.source_url == "https://www.instagram.com/p/FAKECAR0001/" and not i.is_reel for i in items)
    assert "car1a_1080x1350.jpg" in items[0].media_url
    assert items[1].thumbnail_url and "car1b_720x1280.jpg" in items[1].thumbnail_url
    assert stats.items == 3 and stats.skipped_no_url == 1 and stats.skipped_unsupported == 1


def test_unknown_top_level_media_type_counted():
    node = copy.deepcopy(nodes("image.json")[0])
    node["media_type"] = 42
    stats = NormalizeStats()
    assert normalize_node(node, profile="p", stats=stats) == []
    assert stats.skipped_unsupported == 1 and stats.items == 0


def test_video_without_url_is_skipped_not_raised():
    node = copy.deepcopy(nodes("video_reel.json")[0])
    node["video_versions"] = [{"url": None, "width": 1, "height": 1}]
    stats = NormalizeStats()
    assert normalize_node(node, profile="p", stats=stats) == []
    assert stats.skipped_no_url == 1


def test_image_without_candidates_is_skipped():
    node = copy.deepcopy(nodes("image.json")[0])
    node["image_versions2"] = {"candidates": []}
    stats = NormalizeStats()
    assert normalize_node(node, profile="p", stats=stats) == []
    assert stats.skipped_no_url == 1


def test_node_without_code_is_skipped():
    node = copy.deepcopy(nodes("image.json")[0])
    del node["code"]
    stats = NormalizeStats()
    assert normalize_node(node, profile="p", stats=stats) == []
    assert stats.skipped_invalid_node == 1


def test_missing_taken_at_is_none():
    node = copy.deepcopy(nodes("image.json")[0])
    node["taken_at"] = None
    [item] = normalize_node(node, profile="p")
    assert item.taken_at is None


@pytest.mark.parametrize("value", [10 ** 20, -(10 ** 20), float("inf"), "abc"])
def test_absurd_taken_at_is_none_without_raising(value):
    node = copy.deepcopy(nodes("image.json")[0])
    node["taken_at"] = value
    [item] = normalize_node(node, profile="p")
    assert item.taken_at is None


@pytest.fixture
def all_items():
    out = []
    for name in ("image.json", "video_reel.json", "carousel_mixed.json"):
        for node in nodes(name):
            out.extend(normalize_node(node, profile="perfil_fake"))
    return out


def test_filter_by_content(all_items):
    assert len(all_items) == 6
    assert [i.source_post_id for i in filter_by_content(all_items, "reels")] == ["FAKEREEL001"]
    assert {i.source_post_id for i in filter_by_content(all_items, "videos")} == {"FAKEREEL001", "FAKEVID0001",
                                                                                 "FAKECAR0001"}
    assert all(i.media_type == "image" for i in filter_by_content(all_items, "images"))
    assert len(filter_by_content(all_items, "images")) == 3
    assert filter_by_content(all_items, "posts") == all_items
    assert filter_by_content(all_items, "all") == all_items


def test_filter_unknown_content_raises(all_items):
    with pytest.raises(ValueError):
        filter_by_content(all_items, "stories")  # type: ignore[arg-type]
