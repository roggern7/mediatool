"""Editor visual das Legendas CapCut (núcleo): catálogo local a partir de um rp.db sintético, design -> material de
texto/segmento/animações nos formatos verificados no CapCut 9.7.1, maiúsculas, destaque e validação do design. Sem
CapCut real, sem registro real, sem rede."""

from __future__ import annotations

import copy
import json
import os
import sqlite3

import pytest
from capcut_editor_fakes import (
    CAPTION_ONLY,
    CONFLICT,
    FADE_IN,
    FADE_OUT,
    LATO,
    MONTSERRAT,
    PRO_FONT,
    PULSE,
    TYPEWRITER,
    design,
    install_fakes,
    item,
    write_rp_db,
)
from pydantic import ValidationError

from app.integrations.capcut import catalog
from app.integrations.capcut import design as design_mod
from app.integrations.capcut.catalog import CatalogError
from app.integrations.capcut.style import CaptionStyle, HighlightOptions, apply_style, default_style
from app.integrations.capcut.transcribe import Chunk, Word
from app.schemas.capcut import CaptionDesign


@pytest.fixture
def env(monkeypatch, tmp_path):
    data = install_fakes(monkeypatch, tmp_path)
    yield data
    catalog.reset_cache()


def text_style(material: dict) -> dict:
    return json.loads(material["content"])["styles"][0]


# --------------------------------------------------------------------------------------------- catálogo

def test_catalog_reads_a_copy_and_classifies(env, tmp_path, monkeypatch):
    opened: list[str] = []
    real_connect = sqlite3.connect

    def spy(path, *args, **kwargs):
        opened.append(str(path))
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(catalog.sqlite3, "connect", spy)
    cat = catalog.capcut_catalog(env["paths"], tmp_path / "cache.json")
    assert opened and all(os.path.normcase(p) != os.path.normcase(str(env["rp_db"])) for p in opened), \
        "o rp.db do CapCut nunca é aberto no lugar (só a cópia temporária)"
    assert not any(p.startswith(str(env["rp_db"].parent)) for p in opened)

    assert set(cat.fonts) == {MONTSERRAT, PRO_FONT, LATO}, "só respostas get_panel_info; o corpo zlib é lido"
    assert cat.fonts[PRO_FONT]["is_pro"] is True and cat.fonts[MONTSERRAT]["is_pro"] is False
    by_id = {a["id"]: a for a in cat.animations}
    assert {k: by_id[k]["type"] for k in by_id} == {FADE_IN: "in", TYPEWRITER: "in", FADE_OUT: "out", PULSE: "loop"}
    assert CAPTION_ONLY not in by_id and CONFLICT not in by_id and cat.excluded_animations == 2
    assert by_id[TYPEWRITER]["is_pro"] is True and by_id[TYPEWRITER]["default_duration_ms"] == 2000
    assert by_id[FADE_OUT]["default_duration_ms"] == 1500, "duração do sdk_extra (segundos)"
    assert by_id[FADE_IN]["default_duration_ms"] == catalog.DEFAULT_ANIM_MS
    assert by_id[PULSE]["default_duration_ms"] == 800


def test_catalog_cache_memory_file_and_mtime_invalidation(env, tmp_path, monkeypatch):
    cache = tmp_path / "cache.json"
    first = catalog.capcut_catalog(env["paths"], cache)
    assert cache.is_file()
    calls = []
    real_parse = catalog.parse_rp_db

    def counting(path):
        calls.append(path)
        return real_parse(path)

    monkeypatch.setattr(catalog, "parse_rp_db", counting)
    assert catalog.capcut_catalog(env["paths"], cache) is first and calls == [], "memória"
    catalog.reset_cache()
    again = catalog.capcut_catalog(env["paths"], cache)
    assert calls == [] and set(again.fonts) == set(first.fonts), "arquivo de cache, sem reler o banco"

    write_rp_db(env["rp_db"], extra_fonts=[item("7999", "Nova Fonte", 10)])
    st = env["rp_db"].stat()
    os.utime(env["rp_db"], ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    changed = catalog.capcut_catalog(env["paths"], cache)
    assert len(calls) == 1 and "7999" in changed.fonts, "mtime/tamanho mudou: relido"


def test_catalog_unreadable_db_is_skipped_until_it_changes(env, tmp_path, monkeypatch):
    def broken(path):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(catalog, "parse_rp_db", broken)
    cat = catalog.capcut_catalog(env["paths"], tmp_path / "c.json")
    assert cat.fonts == {} and cat.animations == []


def test_list_fonts_sources_search_pro_and_pages(env):
    items, total = catalog.list_fonts(env["paths"], limit=200)
    keys = [f["key"] for f in items]
    assert keys[:2] == ["bundled:CapCutSansText-Bold.otf", "bundled:en.ttf"], "embutidas primeiro (txt fora)"
    assert {"windows:arial.ttf", "windows:impact.ttf", "windows:minha.otf"} <= set(keys)
    assert "windows:cambria.ttc" not in keys and "windows:segredo.ttf" not in keys, "ttc e fora das pastas: fora"
    assert f"capcut:{PRO_FONT}" not in keys and total == len(items), "Pro só com include_pro"
    mont = next(f for f in items if f["id"] == MONTSERRAT)
    assert mont["downloaded"] is True and mont["previewable"] is True and mont["path"].endswith("font.ttf")
    lato = next(f for f in items if f["id"] == LATO)
    assert lato["downloaded"] is False and lato["previewable"] is False and lato["path"] is None
    impact = next(f for f in items if f["key"] == "windows:impact.ttf")
    assert impact["name"] == "Impact" and impact["path"].endswith("/impact.ttf") and impact["is_pro"] is False

    with_pro, _ = catalog.list_fonts(env["paths"], source="capcut", include_pro=True)
    assert {f["id"] for f in with_pro} == {MONTSERRAT, PRO_FONT, LATO}
    assert [f["name"] for f in catalog.list_fonts(env["paths"], q="MONTSÉ")[0]] == ["Montserrat"], "sem acento/caixa"
    page, total = catalog.list_fonts(env["paths"], source="windows", limit=1, offset=1)
    assert total == 3 and len(page) == 1


def test_list_animations_filters(env):
    items, total = catalog.list_animations(env["paths"], type="in")
    assert [a["id"] for a in items] == [FADE_IN] and total == 1, "Pro fora por padrão"
    assert items[0]["downloaded"] is True and items[0]["default_duration_ms"] == 500
    with_pro, _ = catalog.list_animations(env["paths"], type="in", include_pro=True)
    assert [a["name"] for a in with_pro] == ["Fade-in", "Máquina de escrever"]
    assert [a["id"] for a in catalog.list_animations(env["paths"], type="out")[0]] == [FADE_OUT]
    assert [a["id"] for a in catalog.list_animations(env["paths"], q="puls")[0]] == [PULSE]


@pytest.mark.parametrize("key", ["windows:../fora/segredo.ttf", "windows:..\\fora\\segredo.ttf", "windows:segredo.ttf",
                                 "windows:C:/Windows/win.ini", "bundled:../../../CapCut.exe", "capcut:../x",
                                 f"capcut:{LATO}", "capcut:abc", "outro:impact.ttf", "impact.ttf", "windows:",
                                 "windows:cambria.ttc", "bundled:leia-me.txt"])
def test_font_file_refuses_outside_whitelist(env, key):
    assert catalog.font_file(env["paths"], key) is None


def test_font_file_allowed(env):
    assert catalog.font_file(env["paths"], "windows:impact.ttf").read_bytes() == b"TTF-impact"
    assert catalog.font_file(env["paths"], "windows:minha.otf").read_bytes() == b"OTF-minha"
    assert catalog.font_file(env["paths"], "bundled:en.ttf").read_bytes() == b"TTF-en"
    assert catalog.font_file(env["paths"], f"capcut:{MONTSERRAT}").read_bytes() == b"TTF-montserrat"


def test_validate_font(env):
    win = env["windows"] / "impact.ttf"
    ok = catalog.validate_font(env["paths"], {"source": "windows", "path": str(win), "name": "Impact"})
    assert ok == {"source": "windows", "id": None, "path": win.resolve().as_posix(), "name": "Impact"}
    assert catalog.validate_font(env["paths"], {"source": "capcut", "id": LATO, "name": ""})["name"] == "Lato"
    bundled = env["system_font"] / "en.ttf"
    assert catalog.validate_font(env["paths"], {"source": "bundled", "path": str(bundled), "name": "en"})["path"]
    bad = [{"source": "windows", "path": str(env["outside"] / "segredo.ttf"), "name": "x"},
           {"source": "windows", "path": str(env["windows"] / ".." / ".." / "fora" / "segredo.ttf"), "name": "x"},
           {"source": "windows", "path": str(env["windows"] / "sumiu.ttf"), "name": "x"},
           {"source": "windows", "path": str(env["windows"] / "cambria.ttc"), "name": "x"},
           {"source": "windows", "path": "impact.ttf", "name": "x"},
           {"source": "windows", "path": "\\\\servidor\\fontes\\impact.ttf", "name": "x"},
           {"source": "bundled", "path": str(win), "name": "x"},
           {"source": "windows", "path": str(bundled), "name": "x"},
           {"source": "capcut", "id": "123", "name": "x"},
           {"source": "capcut", "id": "../1", "name": "x"}]
    for font in bad:
        with pytest.raises(CatalogError) as err:
            catalog.validate_font(env["paths"], font)
        assert err.value.code == "invalid_design"


# --------------------------------------------------------------------------------------------- design -> material

def test_capcut_font_by_id_only(env):
    cs = design_mod.style_from_design(design())
    mat = cs.material
    st = text_style(mat)
    assert st["font"] == {"id": MONTSERRAT, "path": ""}
    assert (mat["font_id"], mat["font_resource_id"], mat["font_title"], mat["font_name"], mat["font_path"]) == (
        MONTSERRAT, MONTSERRAT, "Montserrat", "Montserrat", "")
    assert st["size"] == 8.0 and st["bold"] is True and st["fill"]["content"]["solid"]["color"] == [1.0, 1.0, 1.0]
    assert st["strokes"][0]["width"] == pytest.approx(0.06) and st["strokes"][0]["content"]["solid"]["color"] == [0, 0, 0]
    assert mat["check_flag"] == 7 | 8 and "shadows" not in st and mat["background_style"] == 0
    assert cs.segment["clip"]["scale"] == {"x": 1.9, "y": 1.9} and cs.segment["clip"]["transform"] == {"x": 0.0,
                                                                                                      "y": -0.7}
    assert cs.extras == [] and cs.source_draft is None and cs.font_path is None


def test_windows_and_bundled_font_by_path(env):
    path = (env["windows"] / "impact.ttf").resolve().as_posix()
    cs = design_mod.style_from_design(design(font={"source": "windows", "id": None, "path": path, "name": "Impact"}))
    st = text_style(cs.material)
    assert st["font"] == {"id": "", "path": path} and cs.material["font_path"] == path and cs.font_path == path
    assert cs.material["font_id"] == "" and cs.material["font_title"] == "Impact"
    en = (env["system_font"] / "en.ttf").resolve().as_posix()
    cs = design_mod.style_from_design(design(font={"source": "bundled", "id": None, "path": en, "name": "en"}))
    assert text_style(cs.material)["font"] == {"id": "", "path": en} and cs.material["font_path"] == en


def test_shadow_background_stroke_and_layout(env):
    d = design(shadow={"enabled": True, "color": "#FF0000", "opacity": 0.9, "blur": 30, "distance": 8, "angle": -45},
               background={"enabled": True, "color": "#1E40AF", "opacity": 0.8, "radius": 0.4},
               stroke={"enabled": False, "color": "#000000", "width": 30}, color="#00FF00", opacity=0.5,
               align="left", letter_spacing=2, line_spacing=1, max_line_width=0.6, italic=True, underline=True,
               position={"x": 0.25, "y": 0.5}, scale=1.2)
    cs = design_mod.style_from_design(d)
    mat, st = cs.material, text_style(cs.material)
    assert st["shadows"] == [{"diffuse": pytest.approx(0.05), "alpha": 0.9, "distance": 8.0, "angle": -45.0,
                              "content": {"solid": {"color": [1.0, 0.0, 0.0]}}}]
    assert mat["has_shadow"] is True and mat["shadow_color"] == "#ff0000" and mat["shadow_alpha"] == 0.9
    assert mat["shadow_distance"] == 8.0 and mat["shadow_angle"] == -45.0 and mat["shadow_smoothing"] == 0.3
    assert (mat["background_style"], mat["background_color"], mat["background_alpha"],
            mat["background_round_radius"], mat["background_height"], mat["background_width"],
            mat["background_horizontal_offset"], mat["background_vertical_offset"]) == (
        1, "#1e40af", 0.8, 0.4, 0.14, 0.14, 0.0, 0.0)
    assert st["strokes"] == [] and mat["check_flag"] == 7 | 16 | 32, "sem contorno: sem o bit 8"
    assert st["fill"]["content"]["solid"] == {"alpha": 0.5, "color": [0.0, 1.0, 0.0]} and mat["text_alpha"] == 0.5
    assert mat["alignment"] == 0 and mat["letter_spacing"] == pytest.approx(0.1)
    assert mat["line_spacing"] == pytest.approx(0.07) and mat["line_max_width"] == 0.6
    assert st["italic"] is True and st["underline"] is True and mat["underline"] is True
    assert cs.segment["clip"]["scale"] == {"x": 1.2, "y": 1.2} and cs.segment["clip"]["transform"] == {"x": 0.25,
                                                                                                      "y": 0.5}


def test_animations_in_out_with_times_on_highlight_pieces(env):
    d = design(animation={"in": {"id": FADE_IN, "name": "Fade-in", "duration_ms": 400},
                          "out": {"id": FADE_OUT, "name": "Fade-out", "duration_ms": 300}, "loop": None})
    cs = design_mod.style_from_design(d)
    [extra] = cs.extras
    assert extra["array"] == "material_animations"
    obj = extra["object"]
    assert obj["type"] == "sticker_animation" and obj["multi_language_current"] == "none"
    first, second = obj["animations"]
    assert first == {"id": FADE_IN, "type": "in", "start": 0, "duration": 400_000, "path": "", "platform": "all",
                     "resource_id": FADE_IN, "third_resource_id": FADE_IN, "source_platform": 1, "name": "Fade-in",
                     "category_id": "ruchang", "category_name": "Entrada", "panel": "", "material_type": "sticker",
                     "anim_adjust_params": None, "request_id": ""}
    assert (second["type"], second["category_id"], second["category_name"]) == ("out", "chuchang", "Saída")

    words = [Word(0.0, 0.5, "um"), Word(0.5, 1.0, "dois"), Word(1.0, 2.0, "três")]
    mats, segs, extras = apply_style(cs, Chunk(0.0, 2.0, "um dois três", words), 0, 2_000_000,
                                     HighlightOptions(color="#ffd400"))
    assert len(segs) == 3
    by_id = {e["object"]["id"]: e["object"] for e in extras}
    kinds = [[a["type"] for a in by_id[s["extra_material_refs"][0]]["animations"]] if s["extra_material_refs"] else []
             for s in segs]
    assert kinds == [["in"], [], ["out"]], "entrada só no 1º pedaço, saída só no último"
    out_anim = by_id[segs[2]["extra_material_refs"][0]]["animations"][0]
    assert out_anim["duration"] == 300_000 and out_anim["start"] == 1_000_000 - 300_000, "saída = duração - animação"
    colors = [[st["fill"]["content"]["solid"]["color"] for st in json.loads(m["content"])["styles"]] for m in mats]
    assert colors[0][0] == pytest.approx([1.0, 212 / 255, 0.0], abs=1e-4), "destaque com a cor do design"


def test_loop_animation_entry(env):
    d = design(animation={"in": None, "out": None, "loop": {"id": PULSE, "name": "Pulso", "duration_ms": 800}})
    anim = design_mod.style_from_design(d).extras[0]["object"]["animations"][0]
    assert (anim["type"], anim["category_id"], anim["category_name"], anim["duration"]) == ("loop", "xunhuan", "Loop",
                                                                                          800_000)


def test_uppercase_and_highlight_disabled(env):
    cs = design_mod.style_from_design(design(uppercase=True, highlight={"enabled": False, "color": "#FFD400"}))
    assert cs.uppercase is True and cs.highlight_enabled is False and cs.highlight_color is None
    again = CaptionStyle.from_dict(cs.to_dict())
    assert again.uppercase is True and again.highlight_enabled is False, "sobrevive à serialização"
    words = [Word(0.0, 0.5, "olá"), Word(0.5, 1.0, "você")]
    mats, segs, _ = apply_style(again, Chunk(0.0, 1.0, "olá você", words), 0, 1_000_000)
    content = json.loads(mats[0]["content"])
    assert content["text"] == "OLÁ VOCÊ" and content["styles"][0]["range"] == [0, 8]
    old = CaptionStyle.from_dict({k: v for k, v in cs.to_dict().items() if k not in ("uppercase", "highlight_enabled")})
    assert old.uppercase is False and old.highlight_enabled is None, "modelos antigos não mudam"


def test_default_design_is_equivalent_to_builtin(env):
    d = design_mod.default_design(env["install"])
    assert d["font"]["path"].endswith("SystemFont/en.ttf")
    CaptionDesign.model_validate(d)
    built = design_mod.style_from_design(design_mod.validate_design(d, env["paths"]))
    ref = default_style(env["install"])
    a, b = text_style(built.material), text_style(ref.material)
    assert a["size"] == b["size"] and a["bold"] == b["bold"] and a["fill"] == b["fill"]
    assert a["strokes"][0]["width"] == pytest.approx(b["strokes"][0]["width"])
    assert built.material["check_flag"] == ref.material["check_flag"] == 15
    assert built.material["font_path"] == ref.material["font_path"]
    assert built.segment["clip"] == ref.segment["clip"]
    assert built.material["line_spacing"] == pytest.approx(ref.material["line_spacing"])
    assert built.material["line_max_width"] == ref.material["line_max_width"]


def test_validate_design_animations_must_exist_with_type(env):
    good = design(animation={"in": {"id": FADE_IN, "name": "", "duration_ms": 400}, "out": None, "loop": None})
    assert design_mod.validate_design(good, env["paths"])["animation"]["in"]["name"] == "Fade-in"
    for anim in ({"in": {"id": FADE_OUT, "name": "x", "duration_ms": 400}},       # tipo errado
                 {"in": {"id": CAPTION_ONLY, "name": "x", "duration_ms": 400}},   # sem classificação segura
                 {"loop": {"id": "999", "name": "x", "duration_ms": 400}}):
        with pytest.raises(CatalogError):
            design_mod.validate_design(design(animation={"in": None, "out": None, "loop": None, **anim}),
                                       env["paths"])


# --------------------------------------------------------------------------------------------- schema

def test_schema_accepts_valid_and_rejects_bad_values():
    CaptionDesign.model_validate(design())
    parsed = CaptionDesign.model_validate(design(animation={"in": {"id": FADE_IN, "duration_ms": 400}}))
    assert parsed.to_json()["animation"] == {"in": {"id": FADE_IN, "name": "", "duration_ms": 400}, "out": None,
                                             "loop": None}
    bad = [
        {"color": "branco"}, {"color": "#FFF"}, {"color": "#GGGGGG"}, {"size": 1}, {"size": 31}, {"scale": 0.2},
        {"scale": 4.5}, {"opacity": 1.5}, {"version": 2}, {"align": "justify"}, {"max_line_width": 0},
        {"position": {"x": 0, "y": -1.5}}, {"stroke": {"enabled": True, "color": "#000000", "width": 101}},
        {"shadow": {"enabled": True, "color": "#000000", "opacity": 0.8, "blur": 20, "distance": 8, "angle": 200}},
        {"background": {"enabled": True, "color": "#000000", "opacity": 0.6, "radius": 2}},
        {"highlight": {"enabled": True, "color": "amarelo"}},
        {"font": {"source": "capcut", "id": None, "path": None, "name": "x"}},
        {"font": {"source": "windows", "id": None, "path": None, "name": "x"}},
        {"font": {"source": "outro", "id": None, "path": "x.ttf", "name": "x"}},
        {"font": {"source": "capcut", "id": "abc", "path": None, "name": "x"}},
        {"animation": {"in": {"id": FADE_IN, "duration_ms": 50}}},
        {"animation": {"entrada": None}},
        {"extra": 1},
    ]
    for change in bad:
        with pytest.raises(ValidationError):
            CaptionDesign.model_validate(design(**change))
    missing = copy.deepcopy(design())
    del missing["stroke"]
    with pytest.raises(ValidationError):
        CaptionDesign.model_validate(missing)
