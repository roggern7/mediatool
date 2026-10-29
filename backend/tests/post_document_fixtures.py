"""Fixture do DOCUMENTO PÚBLICO de uma publicação (GET /reel/<código>/ sem login). Só valores FAKE; nenhuma rede.

Fiel ao envelope observado no Instagram real em 2026-10-10 (Chrome anônimo, UM Reel público): ~40 scripts
``<script type="application/json" data-sjs>`` e, num deles, ``["RelayPrefetchedStreamCache", "next", [], [<preloader>,
{"__bbox": {"complete": true, "result": {"data": {"xig_polaris_media": {...}}}, "sequence_number": 0}}]]``.

O nó de VÍDEO segue o observado (3 ``video_versions`` só com ``type``/``url``, capas de 640/480/320 px, original
1080x1920). Os nós de FOTO e de CARROSSEL são HIPÓTESE (formato v1 da listagem de perfil): essas formas NÃO foram
observadas no documento público.
"""

from __future__ import annotations

import json

OWNER = "Dono_Fake"
PRELOADER = "adp_PolarisLoggedOutDesktopWWWPostRootContentQueryRelayPreloader_abc"   # prefixo observado + hash FAKE
IMAGE_HOST = "https://scontent-fake.cdninstagram.com/v/t51.fake"
VIDEO_HOST = "https://video-fake.fbcdn.net/v/t50.fake"


def image_url(code: str, name: str, width: int, height: int) -> str:
    return f"{IMAGE_HOST}/{code}/{name}_{width}x{height}.jpg?stp=fake&oh=FAKE_OH&oe=00000000"


def video_url(code: str, name: str, version: int) -> str:
    return f"{VIDEO_HOST}/{code}/{name}_{version}.mp4?efg=FAKE&oh=FAKE_OH&oe=00000000"


def _user(owner: str) -> dict:
    return {"pk": "900000001", "username": owner, "id": "17840000000000001", "full_name": "Dono Fake",
            "is_verified": False, "is_private": False, "__typename": "User"}


def _video_fields(code: str, name: str) -> dict:
    """Observado: capas de no máximo 640 px e versões de vídeo SEM largura/altura."""
    return {
        "media_type": 2,
        "original_width": 1080,
        "original_height": 1920,
        "image_versions2": {"candidates": [
            {"url": image_url(code, name, 640, 1136), "height": 1136, "width": 640},
            {"url": image_url(code, name, 480, 852), "height": 852, "width": 480},
            {"url": image_url(code, name, 320, 568), "height": 568, "width": 320},
        ]},
        "has_audio": True,
        "video_versions": [{"type": version, "url": video_url(code, name, version)} for version in (101, 102, 103)],
    }


def _photo_fields(code: str, name: str) -> dict:
    """Forma NÃO observada, hipótese v1."""
    return {
        "media_type": 1,
        "original_width": 1080,
        "original_height": 1350,
        "image_versions2": {"candidates": [
            {"url": image_url(code, name, 640, 800), "height": 800, "width": 640},
            {"url": image_url(code, name, 1080, 1350), "height": 1350, "width": 1080},
            {"url": image_url(code, name, 750, 937), "height": 937, "width": 750},
        ]},
    }


def media_node(kind: str, code: str, owner: str = OWNER, *, product_type: str | None = None) -> dict:
    """Nó da mídia (``if_not_gated_logged_out``). ``kind``: "video" (fiel ao observado; ``product_type`` padrão
    "clips" = Reel), "photo" ou "carousel" de 4 itens foto/vídeo/foto/vídeo (formas NÃO observadas, hipótese v1)."""
    pk = "3200000000000000001"
    node: dict = {
        "__typename": "XIGPolarisVideoMedia" if kind == "video" else "XIGPolarisFakeMedia",
        "user": _user(owner),
        "pk": pk,
        "code": code,
        "taken_at": 1700000000,
        "product_type": product_type,
        "caption": {"pk": "17900000000000001", "text": "Legenda FAKE"},
        "id": f"POLARIS_{pk}",
    }
    if kind == "video":
        node["product_type"] = product_type or "clips"
        node.update(_video_fields(code, "preel1"))
    elif kind == "photo":
        node["product_type"] = product_type or "feed"
        node.update(_photo_fields(code, "pimg1"))
    elif kind == "carousel":
        node["product_type"] = product_type or "carousel_container"
        node["media_type"] = 8
        children = []
        for index, letter in enumerate("abcd"):
            fields = _video_fields if index % 2 else _photo_fields
            children.append({"pk": f"320000000000000003{index + 1}", **fields(code, f"pcar1{letter}")})
        node["carousel_media"] = children
        node["carousel_media_count"] = len(children)
    else:
        raise ValueError(f"tipo de fixture desconhecido: {kind!r}")
    return node


def envelope(node: object, *, code: str | None = None, gating: object = None,
             typename: object = "XIGPolarisVideoMedia") -> dict:
    """``data.xig_polaris_media``: ``code`` (padrão: o do nó), ``gating_ruling`` e a mídia em
    ``if_not_gated_logged_out``. Observado só com ``gating_ruling: null``."""
    if code is None:
        code = node.get("code") if isinstance(node, dict) else None
    pk = node.get("pk") if isinstance(node, dict) else None
    return {"__typename": typename, "pk": pk, "code": code, "if_not_gated_logged_out": node,
            "gating_ruling": gating, "id": f"POLARIS_{pk}"}


def media_payload(media: object, *, preloader: object = PRELOADER) -> dict:
    """JSON do script que carrega o envelope, no caminho observado (ScheduledServerJS -> __bbox.require)."""
    return {"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"define": [], "require": [
        ["RelayPrefetchedStreamCache", "next", [], [preloader, {"__bbox": {
            "complete": True, "result": {"data": {"xig_polaris_media": media}, "extensions": {"is_final": True}},
            "sequence_number": 0}}]],
    ]}}]]]}


def noise_payload(index: int) -> dict:
    return {"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"require": [
        ["FakeNoiseModule", "init", [], [{"index": index, "rsrcMap": {"fake": {"type": "js"}}}]]]}}]]]}


def data_sjs_script(payload: object, *, raw: str | None = None) -> str:
    """``<script type="application/json" data-sjs>`` com o JSON (``raw`` = corpo cru, ex.: JSON inválido)."""
    body = raw if raw is not None else json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    return f'<script type="application/json" data-content-len="{len(body)}" data-sjs>{body}</script>'


def post_html(scripts: list[str], *, noise: int = 38) -> str:
    """Página com ``noise`` scripts ``data-sjs`` de ruído, um ``text/javascript`` e um ``application/json`` sem
    ``data-sjs`` (ambos ignorados pela leitura), e os ``scripts`` dados no meio."""
    parts = ['<!DOCTYPE html><html lang="pt-br"><head><meta charset="utf-8"><title>Instagram</title>',
             '<script type="text/javascript">window.__fake = {"xig_polaris_media": {"code": "JSFAKE00001"}};</script>',
             '<script type="application/json" id="__fake_no_sjs">{"xig_polaris_media": {"code": "NOSJS000001"}}</script>',
             "</head><body><div id=\"mount\">Entrar &middot; Cadastre-se</div>"]
    noise_scripts = [data_sjs_script(noise_payload(index)) for index in range(noise)]
    half = len(noise_scripts) // 2
    parts.extend(noise_scripts[:half])
    parts.extend(scripts)
    parts.extend(noise_scripts[half:])
    parts.append("</body></html>")
    return "\n".join(parts)


def post_page(node: object, *, code: str | None = None, gating: object = None, noise: int = 38) -> str:
    """HTML completo de uma publicação: um envelope com ``node`` no meio do ruído."""
    return post_html([data_sjs_script(media_payload(envelope(node, code=code, gating=gating)))], noise=noise)


# --------------------------------------------------------------------------- documento aberto COM a sessão
# Estrutura observada em 2026-10-29 (só nomes; valores FAKE): o mesmo caminho ScheduledServerJS ->
# RelayPrefetchedStreamCache -> __bbox.result.data, com `xig_fetch_media` = nó v1 (sem `__typename`) e, em outro
# script, o feed de Reels `xdt_api__v1__clips__home__connection_v2.edges[i].node.media` (outras publicações).
SESSION_PRELOADER = "adp_PolarisFakeLoggedInPostQueryRelayPreloader_fake0001"
FEED_FIELD = "xdt_api__v1__clips__home__connection_v2"


def session_media_node(code: str, owner: str = OWNER, *, sized: bool = True) -> dict:
    """Nó de Reel como em ``xig_fetch_media``: sem ``__typename``; ``video_versions`` com largura/altura."""
    node = media_node("video", code, owner)
    del node["__typename"]
    if sized:
        sizes = ((720, 1280), (1080, 1920), (480, 852))
        node["video_versions"] = [{**version, "width": w, "height": h}
                                  for version, (w, h) in zip(node["video_versions"], sizes, strict=True)]
    node["carousel_media"] = None
    node["owner_id"] = "900000001"
    return node


def relay_payload(data: dict, *, preloader: object = SESSION_PRELOADER) -> dict:
    return {"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"define": [], "require": [
        ["RelayPrefetchedStreamCache", "next", [], [preloader, {"__bbox": {
            "complete": True, "result": {"data": data, "extensions": {"is_final": True}}, "sequence_number": 0}}]],
    ]}}]]]}


def feed_payload(nodes: list[dict]) -> dict:
    edges = [{"node": {"media": {**node, "__typename": "XDTMediaDict"}, "__typename": "XDTClipsItem"},
              "cursor": f"FAKECURSOR{index}"} for index, node in enumerate(nodes)]
    return relay_payload({FEED_FIELD: {"edges": edges, "page_info": {"has_next_page": True}}})


def session_page(node: object, *, feed: list[dict] | None = None, noise: int = 38) -> str:
    """HTML logado: ``xig_fetch_media`` = ``node`` (None = campo nulo) e, se ``feed``, o feed de Reels ANTES dele."""
    scripts = [data_sjs_script(feed_payload(feed))] if feed else []
    scripts.append(data_sjs_script(relay_payload({"xig_fetch_media": node})))
    return post_html(scripts, noise=noise)
