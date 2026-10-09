"""Fakes dos testes da importação por link (100% offline; só valores FAKE): provider falso que resolve publicações
por shortcode, registra cada chamada e aceita erros por link."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace

from app.integrations.instagram.downloader import InstagramError, RemoteMedia
from media_fakes import FakeInstagramDownloader, remote

OWNER = "dono_fake"


def link(code: str, kind: str = "p") -> str:
    return f"https://www.instagram.com/{kind}/{code}/"


def photo(code: str, profile: str = OWNER) -> list[RemoteMedia]:
    return [remote(code, profile=profile)]


def video(code: str, profile: str = OWNER) -> list[RemoteMedia]:
    return [remote(code, media_type="video", profile=profile)]


def reel(code: str, profile: str = OWNER) -> list[RemoteMedia]:
    return [replace(remote(code, media_type="video", profile=profile), is_reel=True, source_url=link(code, "reel"))]


def carousel(code: str, count: int = 3, profile: str = OWNER) -> list[RemoteMedia]:
    """Carrossel de `count` itens; o segundo é vídeo."""
    return [replace(remote(code, index, "video" if index == 1 else "image", profile=profile), in_carousel=True)
            for index in range(count)]


def fresh_url(item: RemoteMedia) -> str:
    return f"https://scontent.cdninstagram.com/v/FRESH-{item.source_post_id}_{item.source_media_index}?oh=SIGFAKE"


@dataclass
class LinksDownloader(FakeInstagramDownloader):
    """`posts[shortcode]` = mídias da publicação; `errors[shortcode]` = erros a levantar, um por chamada, em ordem
    (depois deles a publicação responde normal). `calls` = shortcode de cada `resolve_post`, em ordem."""

    posts: dict[str, list[RemoteMedia]] = field(default_factory=dict)
    errors: dict[str, list[Exception]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    on_resolve: Callable[[str], None] | None = None
    # Renovação de URL: `expired` = media_url recusadas pelo "CDN"; a 2a consulta da publicação em diante devolve
    # URLs novas (`fresh_url`), menos as chaves "shortcode:índice" em `missing`.
    expired: set[str] = field(default_factory=set)
    missing: set[str] = field(default_factory=set)
    download_urls: dict[str, list[str]] = field(default_factory=dict)

    def resolve_post(self, shortcode: str, *, cancel=None) -> list[RemoteMedia]:
        self.calls.append(shortcode)
        if self.on_resolve is not None:
            self.on_resolve(shortcode)
        errors = self.errors.get(shortcode)
        if errors:
            raise errors.pop(0)
        items = list(self.posts.get(shortcode, []))
        if self.calls.count(shortcode) > 1:
            items = [replace(item, media_url=fresh_url(item)) for item in items
                     if f"{item.source_post_id}:{item.source_media_index}" not in self.missing]
        return items

    def download(self, item, dest_dir, *, cancel=None, progress=None):
        key = f"{item.source_post_id}:{item.source_media_index}"
        self.download_urls.setdefault(key, []).append(item.media_url)
        if item.media_url in self.expired:
            raise InstagramError("media_url_expired")
        return super().download(item, dest_dir, cancel=cancel, progress=progress)


def links_app(harness_factory, posts: dict[str, list[RemoteMedia]], **kwargs):
    downloader = LinksDownloader(posts=posts)
    return harness_factory(downloader=downloader, **kwargs), downloader


def create_links(h, links: list[str], *, expect: int = 202, **extra) -> dict:
    response = h.client.post("/api/import-links", json={"links": links, **extra})
    assert response.status_code == expect, response.text
    return response.json()


def link_rows(h, batch_id: int, **params) -> list[dict]:
    response = h.client.get(f"/api/batches/{batch_id}/links", params=params)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def listing_state(h, batch_id: int) -> str | None:
    return h.ctx.db.scalar("SELECT listing_state FROM batches WHERE id = ?", (batch_id,))
