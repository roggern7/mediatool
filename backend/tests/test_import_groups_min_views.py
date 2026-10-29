"""Filtro de visualizações mínimas pela API: validação do pedido (só vídeos, só o método web, inteiro estrito >= 1) e
grupo com vários perfis (contadores por perfil e agregado). 100% offline, valores FAKE.
"""

from __future__ import annotations

import pytest


MIN = 500_000


@pytest.fixture(autouse=True)
def _views_filter_on(monkeypatch):
    """O filtro de visualizações está desligado no app (import_service.MIN_VIEWS_ENABLED); aqui o backend dele é
    testado com o interruptor ligado."""
    from app.services import import_service

    monkeypatch.setattr(import_service, "MIN_VIEWS_ENABLED", True)


def post_single(h, **body):
    return h.client.post("/api/imports", json={"profile": "perfil_um", "content_type": "reels", "limit": 10, **body})


def post_group(h, **body):
    return h.client.post("/api/import-groups", json={"profiles": ["perfil_dois", "perfil_tres"],
                                                     "content_type": "reels", "limit": 10, **body})


@pytest.mark.parametrize("value", [0, -1, 1.5, 5e5, "500000", True, False, 10_000_000_001])
def test_invalid_min_views_is_422_and_creates_nothing(harness, value):
    for response in (post_single(harness, min_views=value), post_group(harness, min_views=value)):
        assert response.status_code == 422, response.text
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM import_groups") == 0


@pytest.mark.parametrize("content", ["all", "posts", "images", "photos_carousels"])
def test_min_views_requires_video_content(harness, content):
    for response in (post_single(harness, content_type=content, min_views=MIN),
                     post_group(harness, content_type=content, min_views=MIN)):
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "min_views_requires_video"
        assert "Reels" in body["detail"] and "Vídeos" in body["detail"]
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0


def test_gallery_dl_method_refuses_min_views(harness):
    assert harness.client.put("/api/settings", json={"instagram_provider": "gallery_dl"}).status_code == 200
    for response in (post_single(harness, min_views=MIN), post_group(harness, min_views=MIN)):
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "not_supported_by_provider" and "visualizações mínimas" in body["detail"]
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0
    # Sem o filtro, o mesmo pedido segue aceito no legado; com o método auto, o filtro é aceito (sem fallback).
    assert post_single(harness).status_code == 202
    assert harness.client.put("/api/settings", json={"instagram_provider": "auto"}).status_code == 200
    assert post_group(harness, min_views=MIN).status_code == 202


@pytest.mark.parametrize("content", ["reels", "videos"])
def test_valid_min_views_is_stored_on_group_and_children(harness, content):
    single = post_single(harness, content_type=content, min_views=MIN)
    assert single.status_code == 202
    assert single.json()["views_filter"] == {"min_views": MIN, "posted_within_days": None, "posted_after": None,
                                             "analyzed": 0, "approved": 0, "below": 0, "no_count": 0,
                                             "out_of_period": 0, "pending": 0}
    group = post_group(harness, content_type=content, min_views=1).json()
    assert group["group"]["views_filter"]["min_views"] == 1
    assert [child["views_filter"]["min_views"] for child in group["children"]] == [1, 1]
    rows = harness.ctx.db.fetchall("SELECT min_views, listing_views, listing_views_partial FROM batches ORDER BY id")
    assert [tuple(r) for r in rows] == [(MIN, None, None), (1, None, None), (1, None, None)]
    assert harness.ctx.db.scalar("SELECT min_views FROM import_groups") == 1


def test_omitted_or_null_min_views_keeps_everything_off(harness):
    for body in ({}, {"min_views": None}):
        single = post_single(harness, content_type="all", **body)
        assert single.status_code == 202 and single.json()["views_filter"] is None
        harness.client.post(f"/api/batches/{single.json()['id']}/cancel")
        group = post_group(harness, content_type="all", **body).json()
        assert group["group"]["views_filter"] is None and all(c["views_filter"] is None for c in group["children"])
        harness.client.post(f"/api/import-groups/{group['group']['id']}/cancel")
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches WHERE min_views IS NOT NULL OR listing_views IS NOT NULL"
                                 " OR listing_views_partial IS NOT NULL") == 0
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM import_groups WHERE min_views IS NOT NULL") == 0
    assert harness.client.get("/api/import-groups").json()["items"][0]["views_filter"] is None


# O grupo com vários perfis (contagem pela aba Reels de cada perfil) está em test_profile_filters.py.
