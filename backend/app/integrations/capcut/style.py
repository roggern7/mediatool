"""Modelo visual de legenda: extração de um rascunho do CapCut e aplicação aos blocos.

O CaptionStyle guarda o material de texto inteiro do segmento escolhido, o segmento (clip/escala/posição) e todos os
materiais referenciados em `extra_material_refs` (animações, efeitos/bolhas, modelos de legenda), procurados em todos
os arrays de `materials`. A aplicação clona tudo com ids novos e troca só o texto, os trechos de estilo e `words`.
Leitura de rascunhos: só leitura.
"""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.integrations.capcut import skill
from app.integrations.capcut.errors import CapCutError
from app.integrations.capcut.install import CapCutInstall
from app.integrations.capcut.media import new_id, read_json
from app.integrations.capcut.transcribe import Chunk

STYLE_VERSION = 1
DEFAULT_HIGHLIGHT = "#FFD400"
GENERATED_PREFIX = "MT_"

# Segmento de texto como o CapCut 9.7 regrava (copiado de um rascunho real 9.7.1, sem ids/tempos).
DEFAULT_SEGMENT: dict[str, Any] = {
    "id": "", "source_timerange": None, "target_timerange": {"start": 0, "duration": 0},
    "render_timerange": {"start": 0, "duration": 0}, "desc": "", "state": 0, "speed": 1.0, "is_loop": False,
    "is_tone_modify": False, "reverse": False, "intensifies_audio": False, "cartoon": False, "volume": 1.0,
    "last_nonzero_volume": 1.0,
    "clip": {"scale": {"x": skill.DEFAULT_SCALE, "y": skill.DEFAULT_SCALE}, "rotation": 0.0, "rotation3d": [],
             "transform": {"x": 0.0, "y": skill.DEFAULT_Y}, "flip": {"vertical": False, "horizontal": False},
             "alpha": 1.0},
    "uniform_scale": {"on": True, "value": 1.0}, "material_id": "", "extra_material_refs": [], "render_index": 14000,
    "keyframe_refs": [], "enable_lut": False, "enable_adjust": False, "enable_hsl": False, "visible": True,
    "group_id": "", "enable_color_curves": True, "enable_hsl_curves": True, "track_render_index": 1,
    "hdr_settings": None, "hdr_vivid_settings": None, "enable_color_wheels": True, "track_attribute": 0,
    "is_placeholder": False, "template_id": "", "enable_smart_color_adjust": False, "template_scene": "default",
    "common_keyframes": [], "caption_info": None,
    "responsive_layout": {"enable": False, "target_follow": "", "size_layout": 0, "horizontal_pos_layout": 0,
                          "vertical_pos_layout": 0},
    "enable_color_match_adjust": False, "enable_color_correct_adjust": False, "enable_adjust_mask": False,
    "raw_segment_id": "", "lyric_keyframes": None, "enable_video_mask": True,
    "digital_human_template_group_id": "", "color_correct_alg_result": "", "source": "segmentsourcenormal",
    "enable_mask_stroke": False, "enable_mask_shadow": False, "enable_color_adjust_pro": False,
    "segment_color_tag": "",
}


@dataclass
class HighlightOptions:
    color: str = DEFAULT_HIGHLIGHT
    mode: str = "word"


@dataclass
class CaptionStyle:
    material: dict[str, Any]
    segment: dict[str, Any]
    extras: list[dict[str, Any]] = field(default_factory=list)   # [{array, object}]
    source_draft: str | None = None
    segment_index: int = 0
    highlight_color: str | None = None
    caption_template_info: dict[str, Any] | None = None
    font_path: str | None = None
    fonts: list[Any] = field(default_factory=list)
    words_convention: dict[str, Any] = field(default_factory=lambda: {"unit": "ms", "relative": True})
    version: int = STYLE_VERSION

    @property
    def clip(self) -> dict[str, Any]:
        return self.segment.get("clip") or {}

    @property
    def uniform_scale(self) -> dict[str, Any] | None:
        return self.segment.get("uniform_scale")

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(asdict(self))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CaptionStyle:
        try:
            return cls(
                material=copy.deepcopy(data["material"]), segment=copy.deepcopy(data["segment"]),
                extras=copy.deepcopy(data.get("extras") or []), source_draft=data.get("source_draft"),
                segment_index=int(data.get("segment_index") or 0), highlight_color=data.get("highlight_color"),
                caption_template_info=copy.deepcopy(data.get("caption_template_info")),
                font_path=data.get("font_path"), fonts=copy.deepcopy(data.get("fonts") or []),
                words_convention=dict(data.get("words_convention") or {"unit": "ms", "relative": True}),
                version=int(data.get("version") or STYLE_VERSION),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CapCutError("invalid_style", "Modelo de legenda inválido.") from exc


@dataclass
class DraftSummary:
    name: str
    path: Path
    modified_at: datetime
    text_segments: int
    preview: str


# --------------------------------------------------------------------------------------------- cores/texto

def utf16_len(text: str) -> int:
    """Comprimento em unidades UTF-16 (o `range` dos estilos do CapCut)."""
    return len(text.encode("utf-16-le")) // 2


def rgb_to_hex(color: list[float] | tuple[float, ...]) -> str:
    r, g, b = (max(0, min(255, round(float(c) * 255))) for c in list(color)[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


def hex_to_rgb(value: str) -> list[float]:
    raw = value.strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) != 6:
        raise CapCutError("invalid_style", "Cor de destaque inválida (use #RRGGBB).")
    try:
        return [round(int(raw[i:i + 2], 16) / 255, 6) for i in (0, 2, 4)]
    except ValueError as exc:
        raise CapCutError("invalid_style", "Cor de destaque inválida (use #RRGGBB).") from exc


def _style_color(style: dict[str, Any]) -> str | None:
    try:
        return rgb_to_hex(style["fill"]["content"]["solid"]["color"])
    except (KeyError, TypeError, ValueError):
        return None


def _set_style_color(style: dict[str, Any], hex_color: str) -> None:
    fill = style.setdefault("fill", {"alpha": 1.0})
    content = fill.setdefault("content", {})
    content["render_type"] = "solid"
    solid = content.setdefault("solid", {"alpha": 1.0})
    solid["color"] = hex_to_rgb(hex_color)
    solid.setdefault("alpha", 1.0)


def parse_content(material: dict[str, Any]) -> dict[str, Any]:
    raw = material.get("content")
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                data.setdefault("styles", [])
                data.setdefault("text", "")
                return data
        except ValueError:
            pass
    return {"styles": [], "text": str(raw or "")}


def infer_highlight_color(material: dict[str, Any]) -> str | None:
    """Com trechos de cores diferentes, a cor do trecho minoritário (em caracteres) é a de destaque."""
    totals: dict[str, int] = {}
    order: list[str] = []
    for st in parse_content(material)["styles"]:
        color = _style_color(st)
        rng = st.get("range") or [0, 0]
        if color is None or len(rng) < 2:
            continue
        if color not in totals:
            order.append(color)
            totals[color] = 0
        totals[color] += max(0, int(rng[1]) - int(rng[0]))
    if len(totals) < 2:
        return None
    return min(order, key=lambda c: (totals[c], order.index(c) == 0))


def infer_words_convention(material: dict[str, Any], segment: dict[str, Any]) -> dict[str, Any]:
    """Unidade (ms/us) e referência (relativa ao segmento ou absoluta) de `words` a partir do modelo."""
    words = material.get("words") or {}
    starts = [int(x) for x in words.get("start_time") or []]
    ends = [int(x) for x in words.get("end_time") or []]
    if not starts or not ends:
        return {"unit": "ms", "relative": True, "inferred": False}
    tr = segment.get("target_timerange") or {}
    seg_start, seg_dur = int(tr.get("start") or 0), int(tr.get("duration") or 0)
    hi, lo = max(ends), min(starts)
    if seg_start > 0 and lo >= seg_start / 1000 - 50 and hi <= (seg_start + seg_dur) / 1000 + 50:
        return {"unit": "ms", "relative": False, "inferred": True}
    if hi <= seg_dur / 1000 * 1.2 + 50:
        return {"unit": "ms", "relative": True, "inferred": True}
    if seg_start > 0 and lo >= seg_start - 50_000:
        return {"unit": "us", "relative": False, "inferred": True}
    return {"unit": "us", "relative": True, "inferred": True}


# --------------------------------------------------------------------------------------------- leitura de rascunhos

def _text_segments(content: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    texts = {m.get("id"): m for m in (content.get("materials") or {}).get("texts") or [] if isinstance(m, dict)}
    pairs = []
    for track in content.get("tracks") or []:
        if track.get("type") != "text":
            continue
        for seg in track.get("segments") or []:
            mat = texts.get(seg.get("material_id"))
            if mat is not None:
                pairs.append((seg, mat))
    pairs.sort(key=lambda p: int((p[0].get("target_timerange") or {}).get("start") or 0))
    return pairs


def _draft_dirs(install: CapCutInstall) -> list[Path]:
    if not install.drafts_dir.is_dir():
        return []
    return sorted(p for p in install.drafts_dir.iterdir() if p.is_dir() and (p / "draft_content.json").is_file())


def _safe_draft_name(install: CapCutInstall, name: str) -> bool:
    """Nome de rascunho sem separadores nem `..`, que resolve para uma pasta logo abaixo da pasta de rascunhos."""
    if not name or name in (".", "..") or ".." in name or any(c in name for c in "/\\:\x00"):
        return False
    try:
        root = install.drafts_dir.resolve()
        return (install.drafts_dir / name).resolve().parent == root
    except (OSError, ValueError):
        return False


def find_draft_dir(install: CapCutInstall, name: str) -> Path | None:
    if not _safe_draft_name(install, name):
        return None
    direct = install.drafts_dir / name
    if (direct / "draft_content.json").is_file():
        return direct
    for folder in _draft_dirs(install):
        try:
            if read_json(folder / "draft_meta_info.json").get("draft_name") == name:
                return folder
        except (OSError, ValueError, AttributeError):
            continue
    return None


def list_caption_drafts(install: CapCutInstall, *, include_generated: bool = False) -> list[DraftSummary]:
    """Rascunhos com pelo menos um texto (só leitura), mais recentes primeiro."""
    out: list[DraftSummary] = []
    for folder in _draft_dirs(install):
        name = folder.name
        try:
            meta = read_json(folder / "draft_meta_info.json")
            name = meta.get("draft_name") or name
            modified_us = int(meta.get("tm_draft_modified") or 0)
        except (OSError, ValueError, AttributeError, TypeError):
            modified_us = 0
        if not include_generated and name.startswith(GENERATED_PREFIX):
            continue
        try:
            pairs = _text_segments(read_json(folder / "draft_content.json"))
        except (OSError, ValueError, AttributeError):
            continue  # ilegível/cifrado: fora da lista
        if not pairs:
            continue
        modified = (datetime.fromtimestamp(modified_us / 1e6, UTC) if modified_us > 0
                    else datetime.fromtimestamp((folder / "draft_content.json").stat().st_mtime, UTC))
        preview = " ".join(parse_content(pairs[0][1])["text"].split())[:80]
        out.append(DraftSummary(name=name, path=folder, modified_at=modified, text_segments=len(pairs),
                                preview=preview))
    out.sort(key=lambda d: d.modified_at, reverse=True)
    return out


def _find_material(materials: dict[str, Any], ref: str) -> tuple[str, dict[str, Any]] | None:
    for array, items in materials.items():
        if not isinstance(items, list):
            continue
        for obj in items:
            if isinstance(obj, dict) and obj.get("id") == ref:
                return array, obj
    return None


def extract_style(install: CapCutInstall, draft_name: str, segment_index: int = 0) -> CaptionStyle:
    folder = find_draft_dir(install, draft_name)
    if folder is None:
        raise CapCutError("draft_not_found", "Rascunho do CapCut não encontrado.")
    try:
        content = read_json(folder / "draft_content.json")
    except (OSError, ValueError) as exc:
        raise CapCutError("draft_not_found", "Não foi possível ler o rascunho do CapCut.") from exc
    pairs = _text_segments(content)
    if not pairs or segment_index < 0 or segment_index >= len(pairs):
        raise CapCutError("no_text_segment", "O rascunho não tem o texto escolhido.")
    seg, mat = pairs[segment_index]
    materials = content.get("materials") or {}
    extras = []
    for ref in seg.get("extra_material_refs") or []:
        found = _find_material(materials, ref)
        if found is not None:
            extras.append({"array": found[0], "object": copy.deepcopy(found[1])})
    return CaptionStyle(
        material=copy.deepcopy(mat), segment=copy.deepcopy(seg), extras=extras, source_draft=draft_name,
        segment_index=segment_index, highlight_color=infer_highlight_color(mat),
        caption_template_info=copy.deepcopy(mat.get("caption_template_info")), font_path=mat.get("font_path"),
        fonts=copy.deepcopy(mat.get("fonts") or []), words_convention=infer_words_convention(mat, seg),
    )


def default_style(install: CapCutInstall | None = None) -> CaptionStyle:
    """Estilo do capcut-agent-skill (material nativo, escala 1,919, y -0,70)."""
    # Texto provisório (não vazio) para que `text` do material do skill seja preenchido com o texto de cada bloco.
    material = skill.native_text_material(new_id(), "Legenda")
    if install is not None and install.app_dir is not None:
        font = install.app_dir / "Resources" / "Font" / "SystemFont" / "en.ttf"
        if font.is_file():
            material["font_path"] = font.as_posix()
    return CaptionStyle(material=material, segment=copy.deepcopy(DEFAULT_SEGMENT), extras=[],
                        caption_template_info=None, font_path=material.get("font_path"), fonts=[])


def style_summary(style: dict[str, Any]) -> dict[str, Any]:
    cs = CaptionStyle.from_dict(style)
    mat = cs.material
    styles = parse_content(mat)["styles"]
    base = _base_style(styles, cs.highlight_color)
    font = mat.get("font_title") if mat.get("font_title") not in (None, "", "none") else None
    font = font or mat.get("font_name") or None
    if not font and cs.fonts and isinstance(cs.fonts[0], dict):
        font = cs.fonts[0].get("title") or cs.fonts[0].get("name")
    if not font and isinstance(base, dict) and isinstance(base.get("font"), dict):
        font = base["font"].get("path", "").replace("\\", "/").rsplit("/", 1)[-1] or None
    if not font and cs.font_path:
        font = cs.font_path.replace("\\", "/").rsplit("/", 1)[-1]
    color = (_style_color(base) if base else None) or (mat.get("text_color") or "#ffffff").lower()
    has_animation = any(e.get("array") == "material_animations" and (e.get("object") or {}).get("animations")
                        for e in cs.extras)
    has_effect = any(e.get("array") in ("effects", "text_effects", "text_shapes") for e in cs.extras)
    cti = cs.caption_template_info or {}
    clip = cs.clip
    return {
        "font": font, "color": color, "highlight_color": cs.highlight_color, "has_animation": has_animation,
        "has_effect": has_effect, "caption_template": cti.get("resource_name") or None,
        "position_y": float((clip.get("transform") or {}).get("y") or 0.0),
        "scale": float((clip.get("scale") or {}).get("x") or 1.0),
    }


# --------------------------------------------------------------------------------------------- aplicação

def _base_style(styles: list[dict[str, Any]], highlight_color: str | None) -> dict[str, Any] | None:
    if not styles:
        return None
    if highlight_color:
        for st in styles:
            if _style_color(st) != highlight_color:
                return st
    return styles[0]


def _fallback_style() -> dict[str, Any]:
    return parse_content(skill.native_text_material("x", "x"))["styles"][0]


def _word_spans(chunk: Chunk) -> tuple[str, list[tuple[int, int]]]:
    """Texto do bloco e o intervalo UTF-16 [ini, fim) de cada palavra."""
    parts: list[str] = []
    spans: list[tuple[int, int]] = []
    pos = 0
    for w in chunk.words:
        word = " ".join(w.text.split())
        if not word:
            continue
        if parts:
            pos += 1
        n = utf16_len(word)
        spans.append((pos, pos + n))
        parts.append(word)
        pos += n
    text = " ".join(parts) if parts else " ".join(chunk.text.split())
    return text, spans


def _build_styles(base: dict[str, Any], hl: dict[str, Any] | None, total: int,
                  span: tuple[int, int] | None) -> list[dict[str, Any]]:
    def piece(src: dict[str, Any], a: int, b: int) -> dict[str, Any]:
        st = copy.deepcopy(src)
        st["range"] = [a, b]
        return st

    if span is None or hl is None:
        return [piece(base, 0, total)]
    a, b = span
    out = []
    if a > 0:
        out.append(piece(base, 0, a))
    out.append(piece(hl, a, b))
    if b < total:
        out.append(piece(base, b, total))
    return out


def _fit_animations(obj: dict[str, Any], seg_dur: int, position: str) -> dict[str, Any] | None:
    """Mantém 'in' só no 1º pedaço do bloco e 'out' só no último ('loop' sempre); ajusta tempos ao segmento."""
    anims = []
    for anim in obj.get("animations") or []:
        kind = str(anim.get("type") or "")
        if kind == "in" and position not in ("first", "single"):
            continue
        if kind == "out" and position not in ("last", "single"):
            continue
        anim = copy.deepcopy(anim)
        dur = int(anim.get("duration") or 0)
        if kind == "in":
            anim["start"] = 0
            anim["duration"] = min(dur, seg_dur)
        elif kind == "out":
            anim["duration"] = min(dur, seg_dur)
            anim["start"] = seg_dur - anim["duration"]
        else:
            start = min(int(anim.get("start") or 0), seg_dur)
            anim["start"] = start
            anim["duration"] = max(0, min(dur, seg_dur - start)) if dur else dur
        anims.append(anim)
    if not anims and obj.get("animations"):
        return None
    out = copy.deepcopy(obj)
    out["animations"] = anims
    return out


def _words_field(chunk: Chunk, seg_start: int, convention: dict[str, Any]) -> dict[str, list[Any]]:
    unit = 1000 if convention.get("unit", "ms") == "ms" else 1
    ref = seg_start if convention.get("relative", True) else 0
    starts, ends, texts = [], [], []
    for w in chunk.words:
        word = " ".join(w.text.split())
        if not word:
            continue
        s = max(0, round(w.start * 1e6) - ref)
        e = max(s, round(w.end * 1e6) - ref)
        starts.append(int(s // unit))
        ends.append(int(e // unit))
        texts.append(word)
    return {"start_time": starts, "end_time": ends, "text": texts}


def _one(style: CaptionStyle, chunk: Chunk, text: str, styles: list[dict[str, Any]], start_us: int, dur_us: int,
         position: str) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    mat = copy.deepcopy(style.material)
    mat["id"] = new_id()
    content = parse_content(style.material)
    content["styles"] = styles
    content["text"] = text
    mat["content"] = json.dumps(content, ensure_ascii=False)
    # Campos de texto duplicados: atualizados só se o original os preenchia (vazios/ausentes ficam como estão).
    for key in ("text", "recognize_text"):
        if isinstance(style.material.get(key), str) and style.material[key]:
            mat[key] = text
    if "words" in mat:
        mat["words"] = _words_field(chunk, start_us, style.words_convention)
    if isinstance(mat.get("current_words"), dict) and (mat["current_words"].get("text") or []):
        mat["current_words"] = _words_field(chunk, start_us, style.words_convention)

    extras: list[dict[str, Any]] = []
    refs: list[str] = []
    for extra in style.extras:
        obj = copy.deepcopy(extra.get("object") or {})
        if extra.get("array") == "material_animations":
            obj = _fit_animations(obj, dur_us, position)
            if obj is None:
                continue
        obj["id"] = new_id()
        refs.append(obj["id"])
        extras.append({"array": extra["array"], "object": obj})

    seg = copy.deepcopy(style.segment)
    seg.update(id=new_id(), material_id=mat["id"], target_timerange={"start": int(start_us), "duration": int(dur_us)},
               extra_material_refs=refs, keyframe_refs=[], common_keyframes=[], group_id="", raw_segment_id="",
               source_timerange=None)  # texto no 9.7: o CapCut regrava source_timerange como null
    return mat, seg, extras


def apply_style(style: CaptionStyle, chunk: Chunk, start_us: int, duration_us: int,
                highlight: HighlightOptions | None = None
                ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Materiais de texto, segmentos e materiais extras ({array, object}) de um bloco."""
    text, spans = _word_spans(chunk)
    total = utf16_len(text)
    styles = parse_content(style.material)["styles"]
    base = _base_style(styles, style.highlight_color) or _fallback_style()

    if highlight is None or len(spans) == 0:
        mat, seg, extras = _one(style, chunk, text, _build_styles(base, None, total, None), start_us, duration_us,
                                "single")
        return [mat], [seg], extras

    color = highlight.color.lower()
    hl = next((st for st in styles if _style_color(st) == color), None)
    if hl is None:
        hl = copy.deepcopy(base)
        _set_style_color(hl, color)
    end_us = start_us + duration_us
    words = [w for w in chunk.words if " ".join(w.text.split())]
    bounds = [start_us]
    for w in words[1:]:
        bounds.append(min(max(round(w.start * 1e6), bounds[-1]), end_us))
    bounds.append(end_us)
    pieces = [(i, bounds[i], bounds[i + 1]) for i in range(len(words)) if bounds[i + 1] > bounds[i]]
    materials, segments, extras = [], [], []
    for n, (i, a, b) in enumerate(pieces):
        if len(pieces) == 1:
            position = "single"
        elif n == 0:
            position = "first"
        elif n == len(pieces) - 1:
            position = "last"
        else:
            position = "middle"
        mat, seg, ex = _one(style, chunk, text, _build_styles(base, hl, total, spans[i]), a, b - a, position)
        materials.append(mat)
        segments.append(seg)
        extras.extend(ex)
    return materials, segments, extras
