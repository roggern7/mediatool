"""Editor visual de legendas: `design` (JSON versionado, fonte única da verdade da tela) -> CaptionStyle.

O CaptionStyle gerado usa o mesmo motor dos modelos extraídos de rascunho (`apply_style`/`build_draft`): material de
texto nativo do capcut-agent-skill alterado nos campos verificados no CapCut 9.7.1 (fonte por id do catálogo ou por
caminho, contorno, sombra, caixa de fundo), segmento padrão com `clip.scale`/`clip.transform` do design e, com
animação, um material `material_animations` (`sticker_animation`) referenciado pelo segmento. Formatos em
docs/CAPCUT.md ("Editor visual").
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from app.integrations.capcut import catalog, skill
from app.integrations.capcut.catalog import CatalogError, CatalogPaths
from app.integrations.capcut.install import CapCutInstall
from app.integrations.capcut.media import new_id
from app.integrations.capcut.style import (
    DEFAULT_HIGHLIGHT,
    DEFAULT_SEGMENT,
    CaptionStyle,
    hex_to_rgb,
    parse_content,
)

DESIGN_VERSION = 1
ALIGNMENT = {"left": 0, "center": 1, "right": 2}
# check_flag do material de texto: 7 = base; 8 = contorno; 16 = caixa de fundo; 32 = sombra (pyCapCut).
FLAG_BASE, FLAG_STROKE, FLAG_BACKGROUND, FLAG_SHADOW = 7, 8, 16, 32
BACKGROUND_SIZE = 0.14


def default_design(install: CapCutInstall | None = None) -> dict[str, Any]:
    """Design equivalente ao estilo embutido "Padrão (capcut-agent-skill)" (ponto de partida do editor)."""
    paths = catalog.paths_for(install)
    en = paths.bundled_dir / "SystemFont" / "en.ttf" if paths.bundled_dir is not None else None
    return {
        "version": DESIGN_VERSION,
        "font": {"source": "bundled", "id": None, "path": en.as_posix() if en is not None and en.is_file() else None,
                 "name": "en"},
        "size": 7.0, "scale": skill.DEFAULT_SCALE,
        "bold": True, "italic": False, "underline": False, "uppercase": False,
        "color": "#FFFFFF", "opacity": 1.0,
        "stroke": {"enabled": True, "color": "#000000", "width": 30},
        "shadow": {"enabled": False, "color": "#000000", "opacity": 0.8, "blur": 20, "distance": 8, "angle": -45},
        "background": {"enabled": False, "color": "#000000", "opacity": 0.6, "radius": 0.3},
        "align": "center", "letter_spacing": 0, "line_spacing": 0,
        "position": {"x": 0.0, "y": skill.DEFAULT_Y},
        "max_line_width": 0.82,
        "highlight": {"enabled": True, "color": DEFAULT_HIGHLIGHT},
        "animation": {"in": None, "out": None, "loop": None},
    }


def validate_design(design: dict[str, Any], paths: CatalogPaths, cache_file: Path | None = None) -> dict[str, Any]:
    """Confere o que depende do disco/catálogo (formato e faixas já validados pelo schema): fonte permitida e
    animações existentes no catálogo com o tipo pedido. Devolve o design normalizado; CatalogError se não vale."""
    out = copy.deepcopy(design)
    out["font"] = catalog.validate_font(paths, design.get("font") or {}, cache_file)
    for kind in catalog.ANIM_TYPES:
        anim = (design.get("animation") or {}).get(kind)
        if anim is None:
            continue
        found = catalog.find_animation(paths, str(anim.get("id") or ""), kind, cache_file)
        if found is None:
            raise CatalogError(f"Animação de {_KIND_LABEL[kind]} não encontrada no catálogo local.")
        out["animation"][kind] = {"id": found["id"], "name": str(anim.get("name") or found["name"]),
                                  "duration_ms": int(anim["duration_ms"])}
    return out


_KIND_LABEL = {"in": "entrada", "out": "saída", "loop": "loop"}


def design_warnings(design: dict[str, Any]) -> list[str]:
    """Avisos (não bloqueiam) de combinações que o CapCut renderiza diferente do editor. Mesma regra e mesmo texto de
    `designWarnings` no frontend (frontend/src/lib/captionDesign.ts).

    Verificado no CapCut 9.7.1 (vídeo exportado, 2026-11-07): com a caixa de fundo ligada, as animações de ENTRADA e
    de SAÍDA atuam só no texto; a caixa aparece vazia por alguns instantes. O loop não tem esse efeito."""
    background = design.get("background") or {}
    animation = design.get("animation") or {}
    if not background.get("enabled"):
        return []
    kinds = [_KIND_LABEL[kind] for kind in ("in", "out") if animation.get(kind)]
    if not kinds:
        return []
    which = " e de ".join(kinds)
    return [f"Com a caixa de fundo ligada, a animação de {which} do CapCut atua só no texto: a caixa de fundo aparece "
            "vazia por alguns instantes. Para evitar, desligue a caixa de fundo ou tire essa animação."]


def _rgb(value: str) -> list[float]:
    return hex_to_rgb(value)


def _apply_font(mat: dict[str, Any], st: dict[str, Any], font: dict[str, Any]) -> str | None:
    name = str(font.get("name") or "")
    if font.get("source") == "capcut":
        fid = str(font.get("id") or "")
        # Só pelo id: o CapCut baixa a fonte ao abrir o rascunho e regrava `path` (verificado no 9.7.1).
        st["font"] = {"id": fid, "path": ""}
        mat.update(font_id=fid, font_resource_id=fid, font_title=name, font_name=name, font_path="")
        return None
    path = str(font.get("path") or "")
    st["font"] = {"id": "", "path": path}
    mat.update(font_path=path, font_id="", font_resource_id="", font_title=name, font_name=name)
    return path or None


def _animation_entry(kind: str, anim: dict[str, Any]) -> dict[str, Any]:
    category_id, category_name = catalog.ANIM_CATEGORY_INFO[kind]
    rid = str(anim["id"])
    return {"id": rid, "type": kind, "start": 0, "duration": int(anim["duration_ms"]) * 1000, "path": "",
            "platform": "all", "resource_id": rid, "third_resource_id": rid, "source_platform": 1,
            "name": str(anim.get("name") or ""), "category_id": category_id, "category_name": category_name,
            "panel": "", "material_type": "sticker", "anim_adjust_params": None, "request_id": ""}


def style_from_design(design: dict[str, Any]) -> CaptionStyle:
    """CaptionStyle do design (já validado). O texto real de cada bloco entra no `apply_style`."""
    mat = skill.native_text_material(new_id(), "Legenda")
    content = parse_content(mat)
    st = content["styles"][0]
    flag = FLAG_BASE

    st["size"] = float(design["size"])
    st["bold"] = bool(design["bold"])
    st["italic"] = bool(design["italic"])
    st["underline"] = bool(design["underline"])
    color = str(design["color"])
    opacity = float(design["opacity"])
    st["fill"] = {"alpha": 1.0, "content": {"render_type": "solid", "solid": {"alpha": opacity, "color": _rgb(color)}}}
    mat.update(text_color=color.lower(), text_alpha=opacity, underline=bool(design["underline"]))

    stroke = design["stroke"]
    if stroke["enabled"]:
        width = float(stroke["width"]) / 100 * 0.2          # pyCapCut: 0..100 -> 0..0,2
        st["strokes"] = [{"alpha": 1.0, "content": {"render_type": "solid",
                                                    "solid": {"alpha": 1.0, "color": _rgb(stroke["color"])}},
                          "width": width}]
        mat.update(border_color=str(stroke["color"]).lower(), border_width=width, border_alpha=1.0)
        flag |= FLAG_STROKE
    else:
        st["strokes"] = []

    shadow = design["shadow"]
    if shadow["enabled"]:
        blur = float(shadow["blur"]) / 100                   # pyCapCut: diffuse = suavização / 6
        st["shadows"] = [{"diffuse": blur / 6, "alpha": float(shadow["opacity"]), "distance": float(shadow["distance"]),
                          "angle": float(shadow["angle"]), "content": {"solid": {"color": _rgb(shadow["color"])}}}]
        mat.update(has_shadow=True, shadow_color=str(shadow["color"]).lower(), shadow_alpha=float(shadow["opacity"]),
                   shadow_distance=float(shadow["distance"]), shadow_angle=float(shadow["angle"]),
                   shadow_smoothing=blur)
        flag |= FLAG_SHADOW
    else:
        st.pop("shadows", None)
        mat["has_shadow"] = False

    bg = design["background"]
    if bg["enabled"]:
        mat.update(background_style=1, background_color=str(bg["color"]).lower(), background_alpha=float(bg["opacity"]),
                   background_round_radius=float(bg["radius"]), background_height=BACKGROUND_SIZE,
                   background_width=BACKGROUND_SIZE, background_horizontal_offset=0.0, background_vertical_offset=0.0)
        flag |= FLAG_BACKGROUND
    else:
        mat["background_style"] = 0

    font_path = _apply_font(mat, st, design["font"])
    mat.update(alignment=ALIGNMENT[design["align"]], letter_spacing=float(design["letter_spacing"]) * 0.05,
               line_spacing=0.02 + float(design["line_spacing"]) * 0.05, line_max_width=float(design["max_line_width"]),
               check_flag=flag)
    content["styles"] = [st]
    mat["content"] = json.dumps(content, ensure_ascii=False)

    segment = copy.deepcopy(DEFAULT_SEGMENT)
    scale = float(design["scale"])
    segment["clip"]["scale"] = {"x": scale, "y": scale}
    segment["clip"]["transform"] = {"x": float(design["position"]["x"]), "y": float(design["position"]["y"])}

    anims = [_animation_entry(kind, a) for kind in catalog.ANIM_TYPES
             if (a := (design.get("animation") or {}).get(kind)) is not None]
    extras = []
    if anims:
        extras.append({"array": "material_animations",
                       "object": {"id": new_id(), "type": "sticker_animation", "multi_language_current": "none",
                                  "animations": anims}})
    highlight = design["highlight"]
    return CaptionStyle(material=mat, segment=segment, extras=extras, source_draft=None, segment_index=0,
                        highlight_color=str(highlight["color"]).lower() if highlight["enabled"] else None,
                        caption_template_info=None, font_path=font_path, fonts=[],
                        uppercase=bool(design["uppercase"]), highlight_enabled=bool(highlight["enabled"]))
