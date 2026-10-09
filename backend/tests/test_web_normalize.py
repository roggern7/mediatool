"""web/normalize.py: nó do PolarisProfilePostsQuery -> RemoteMedia (fixtures sintéticas, offline)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.integrations.instagram.web.normalize import (
    NormalizeStats,
    filter_by_content,
    legacy_to_v1,
    normalize_node,
    normalize_post,
)

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


# --------------------------------------------------------------------------- post isolado (normalize_post)

POST_ROOT = "xdt_shortcode_media"


def post_root(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["data"][POST_ROOT]


def test_post_legacy_image_picks_largest_and_owner_lowercase():
    stats = NormalizeStats()
    [item] = normalize_post(post_root("post_image.json"), stats=stats)
    assert item.media_type == "image" and item.is_reel is False and item.in_carousel is False
    assert "pimg1_1080x1350.jpg" in item.media_url and (item.width, item.height) == (1080, 1350)
    assert item.source_post_id == "FAKEPOSTIMG1" and item.source_media_index == 0
    assert item.post_id == "3200000000000000001" and item.media_id == "3200000000000000001"
    assert item.source_url == "https://www.instagram.com/p/FAKEPOSTIMG1/"
    assert item.source_profile == "dono_fake" and item.listing_ref is None
    assert item.taken_at == "2023-11-14T22:13:20Z" and item.extension == "jpg"
    assert (stats.nodes, stats.items, stats.skipped_no_url, stats.skipped_unsupported) == (1, 1, 0, 0)
    assert stats.reasons == {}


def test_post_legacy_reel_and_plain_video():
    root = post_root("post_video_reel.json")
    [reel] = normalize_post(root)
    assert reel.media_type == "video" and reel.is_reel is True and reel.duration == 12.5
    assert reel.source_url == "https://www.instagram.com/reel/FAKEPOSTREEL/"
    assert "preel1_1080x1920.mp4" in reel.media_url and "preel1_1080x1920.jpg" in reel.thumbnail_url
    assert (reel.width, reel.height) == (1080, 1920) and reel.extension == "mp4"
    # vídeo avulso (não Reel): mesmo nó com product_type "feed"
    [video] = normalize_post({**root, "product_type": "feed"})
    assert video.media_type == "video" and video.is_reel is False
    assert video.source_url == "https://www.instagram.com/p/FAKEPOSTREEL/"


def test_post_legacy_carousel_returns_every_item_in_order():
    items = normalize_post(post_root("post_carousel_mixed.json"))
    assert [(i.source_media_index, i.media_type) for i in items] == [
        (0, "image"), (1, "video"), (2, "image"), (3, "video")]
    assert [i.media_id for i in items] == [f"320000000000000003{n}" for n in (1, 2, 3, 4)]
    assert all(i.source_post_id == "FAKEPOSTCAR1" and i.in_carousel and not i.is_reel for i in items)
    assert all(i.source_profile == "dono_fake" and i.post_id == "3200000000000000003" for i in items)
    assert "pcar1b_1080x1920.mp4" in items[1].media_url and items[1].duration == 12.5


@pytest.mark.parametrize("typename", ["GraphImage", "XDTGraphImage", None])
def test_post_legacy_typename_with_or_without_prefix_or_inferred(typename):
    root = post_root("post_image.json")
    if typename is None:
        del root["__typename"]
    else:
        root["__typename"] = typename
    assert [i.media_type for i in normalize_post(root)] == ["image"]


def test_post_legacy_carousel_inferred_without_typename_and_invalid_child_keeps_indexes():
    root = post_root("post_carousel_mixed.json")
    del root["__typename"]
    root["edge_sidecar_to_children"]["edges"][1] = {"node": None}
    stats = NormalizeStats()
    items = normalize_post(root, stats=stats)
    assert [i.source_media_index for i in items] == [0, 2, 3]
    assert stats.reasons == {"carousel_child_invalid": 1}


def test_post_legacy_falls_back_to_display_url_and_counts_missing_urls():
    root = post_root("post_image.json")
    del root["display_resources"]
    [item] = normalize_post(root)
    assert "pimg1_1080x1350.jpg" in item.media_url and (item.width, item.height) == (1080, 1350)
    del root["display_url"]
    stats = NormalizeStats()
    assert normalize_post(root, stats=stats) == [] and stats.reasons == {"image_no_url": 1}
    video = post_root("post_video_reel.json")
    video["video_url"] = None
    stats = NormalizeStats()
    assert normalize_post(video, stats=stats) == [] and stats.reasons == {"video_no_url": 1}
    assert stats.skipped_no_url == 1 and stats.skipped_unsupported == 0


def test_post_legacy_unknown_typename_is_unsupported():
    root = post_root("post_image.json")
    root["__typename"] = "XDTGraphFakeNewKind"
    stats = NormalizeStats()
    assert normalize_post(root, stats=stats) == []
    assert stats.skipped_unsupported == 1 and stats.reasons == {"unsupported_type": 1}


def test_post_v1_items_and_direct_v1_node():
    root = post_root("post_v1_items.json")
    items = normalize_post(root)
    assert [(i.source_media_index, i.media_type, i.in_carousel) for i in items] == [(0, "image", True),
                                                                                  (1, "video", True)]
    assert all(i.source_post_id == "FAKEPOSTV1A1" and i.source_profile == "dono_fake" for i in items)
    assert "pv1a_1080x1350.jpg" in items[0].media_url and items[1].duration == 7.0
    assert normalize_post(root["items"][0]) == items   # nó v1 direto (media_type numérico)


def test_post_v1_empty_items_and_object_without_code():
    stats = NormalizeStats()
    assert normalize_post({"items": []}, stats=stats) == [] and stats.nodes == 0
    stats = NormalizeStats()
    assert normalize_post({"fake_unknown_shape": True}, stats=stats) == []
    assert stats.skipped_invalid_node == 1 and stats.reasons == {"node_no_code": 1}


@pytest.mark.parametrize("owner", [None, {}, {"username": None}, {"username": ""}, {"username": "nome invalido!"},
                                   {"username": "x" * 31}, "texto"])
def test_post_owner_absent_or_invalid_is_empty(owner):
    root = post_root("post_image.json")
    root["owner"] = owner
    assert normalize_post(root)[0].source_profile == ""


def test_legacy_to_v1_is_pure():
    root = post_root("post_carousel_mixed.json")
    before = copy.deepcopy(root)
    v1 = legacy_to_v1(root)
    assert root == before
    assert v1["code"] == "FAKEPOSTCAR1" and v1["media_type"] == 8 and v1["carousel_media_count"] == 4
    assert [c["media_type"] for c in v1["carousel_media"]] == [1, 2, 1, 2]
    assert v1["carousel_media"][1]["video_versions"][0]["url"].startswith("https://video-fake.fbcdn.net/")
