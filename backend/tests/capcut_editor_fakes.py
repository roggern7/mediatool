"""Fakes do editor visual das Legendas CapCut: instalação falsa do CapCut (MEDIA_TOOL_CAPCUT_ROOT) com fontes
embutidas, rp.db sintético mínimo (respostas get_panel_info) e recursos "baixados" em Cache/effect; fontes do Windows
falsas. Nada toca o CapCut real, o registro real nem a rede."""

from __future__ import annotations

import json
import sqlite3
import zlib
from pathlib import Path
from typing import Any

from app.integrations.capcut import catalog
from app.integrations.capcut import install as install_mod

VERSION = "9.7.1.4203"
MONTSERRAT = "7540633113777540353"     # grátis, baixada
PRO_FONT = "7461911939803892998"       # Pro
LATO = "7000000000000000010"           # grátis, não baixada
FADE_IN = "6724916044072227332"        # entrada, grátis, baixada
TYPEWRITER = "7210980292243231233"     # entrada, Pro, 2000 ms
FADE_OUT = "6800000000000000001"       # saída, grátis, duração pelo sdk_extra (1,5 s)
PULSE = "6800000000000000002"          # loop, grátis
CAPTION_ONLY = "6800000000000000003"   # só "Legenda" (caption): fora da lista
CONFLICT = "6800000000000000004"       # categoria entrada com animation_type loop: fora da lista


def _biz(pro: bool) -> dict[str, Any]:
    info = {"is_vip": True, "paid_type": "subscribe"} if pro else {"is_vip": False, "paid_type": "free"}
    return {"json_str": json.dumps(info), "sign": "x"}


def item(rid: str, title: str, effect_type: int, *, pro: bool = False, anim_type: str | None = None,
         duration: int | None = None, sdk_seconds: float | None = None) -> dict[str, Any]:
    common = {"id": rid, "effect_id": rid, "effect_type": effect_type, "title": title, "md5": "m" * 32,
              "third_resource_id_str": rid, "business_info": _biz(pro),
              "cover_url": {"small": "https://externo.invalid/capa.webp"}}
    if sdk_seconds is not None:
        common["sdk_extra"] = json.dumps({"setting": {"animation_duration": sdk_seconds}})
    out: dict[str, Any] = {"common_attr": common}
    if anim_type is not None:
        out["text_animation"] = {"duration": duration, "animation_type": anim_type}
    return out


def anim_panel() -> dict[str, Any]:
    cats = [{"category_id": 7026, "category_name": "Entrada", "category_key": "ruchang"},
            {"category_id": 7027, "category_name": "Saída", "category_key": "chuchang"},
            {"category_id": 7028, "category_name": "Loop", "category_key": "xunhuan"},
            {"category_id": 25205, "category_name": "Legenda", "category_key": "caption_animation"}]
    res = {
        "7026": {"effect_item_list": [item(FADE_IN, "Fade-in", 18, anim_type="in"),
                                      item(TYPEWRITER, "Máquina de escrever", 18, pro=True, anim_type="in",
                                           duration=2000),
                                      item(CONFLICT, "Confusa", 18, anim_type="loop")]},
        "7027": {"effect_item_list": [item(FADE_OUT, "Fade-out", 18, anim_type="out", sdk_seconds=1.5)]},
        "7028": {"effect_item_list": [item(PULSE, "Pulso", 18, anim_type="loop", duration=800)]},
        "25205": {"effect_item_list": [item(CAPTION_ONLY, "Legenda X", 18, anim_type="caption")]},
    }
    return {"ret": "0", "data": {"categories": cats, "category_resources": res}}


def font_panel(extra: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    fonts = [item(MONTSERRAT, "Montserrat", 10), item(PRO_FONT, "Integral Pro", 10, pro=True),
             item(LATO, "Lato", 10), *(extra or [])]
    return {"ret": "0", "data": {"categories": [{"category_id": 1, "category_key": "font"}],
                                 "category_resources": {"1": {"effect_item_list": fonts}}}}


PANEL_URL = "https://api.invalid/artist/v1/panel/get_panel_info_{}__capcutpc_x_get_all_resource"


def write_rp_db(path: Path, *, extra_fonts: list[dict[str, Any]] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE http_cache (id INTEGER PRIMARY KEY, url TEXT, response_body TEXT, version INTEGER,"
                " timestamp INTEGER)")
    con.execute("INSERT INTO http_cache (url, response_body) VALUES (?, ?)",
                (PANEL_URL.format("ANIM"), json.dumps(anim_panel(), ensure_ascii=False)))
    # Corpo comprimido (zlib) também é lido.
    con.execute("INSERT INTO http_cache (url, response_body) VALUES (?, ?)",
                (PANEL_URL.format("FONT"), zlib.compress(json.dumps(font_panel(extra_fonts)).encode("utf-8"))))
    # Fora de get_panel_info: ignorado.
    con.execute("INSERT INTO http_cache (url, response_body) VALUES (?, ?)",
                ("https://api.invalid/artist/v1/effect/search", json.dumps(font_panel([item("1", "Busca", 10)]))))
    con.commit()
    con.close()
    return path


def make_capcut(tmp_path: Path) -> dict[str, Any]:
    """Instalação falsa completa; devolve as pastas e arquivos usados nos testes."""
    base = tmp_path / "CapCut"
    apps = base / "Apps"
    (apps / VERSION / "Resources" / "Font" / "SystemFont").mkdir(parents=True)
    (apps / "CapCut.exe").write_bytes(b"MZ")
    (apps / "ProductInfo.xml").write_text(f'<packet><full_appver value="{VERSION}" /></packet>', encoding="utf-8")
    system_font = apps / VERSION / "Resources" / "Font" / "SystemFont"
    (system_font / "en.ttf").write_bytes(b"TTF-en")
    (system_font / "CapCutSansText-Bold.otf").write_bytes(b"OTF-bold")
    (system_font / "leia-me.txt").write_text("não é fonte", encoding="utf-8")
    user = base / "User Data"
    (user / "Projects" / "com.lveditor.draft").mkdir(parents=True)
    (user / "Config").mkdir(parents=True)
    (base / "Videos").mkdir()
    rp_db = write_rp_db(user / "Cache" / "ressdk_db" / "1234" / "rp.db")
    effect = user / "Cache" / "effect"
    (effect / MONTSERRAT / ("a" * 32)).mkdir(parents=True)
    (effect / MONTSERRAT / ("a" * 32) / "font.ttf").write_bytes(b"TTF-montserrat")
    (effect / MONTSERRAT / (("a" * 32) + "_tmp")).mkdir()
    (effect / FADE_IN / ("b" * 32)).mkdir(parents=True)
    (effect / FADE_IN / ("b" * 32) / "config.json").write_text("{}", encoding="utf-8")
    win = tmp_path / "Windows" / "Fonts"
    win.mkdir(parents=True)
    (win / "impact.ttf").write_bytes(b"TTF-impact")
    (win / "arial.ttf").write_bytes(b"TTF-arial")
    (win / "cambria.ttc").write_bytes(b"TTC")
    user_fonts = tmp_path / "LocalFonts"
    user_fonts.mkdir()
    (user_fonts / "minha.otf").write_bytes(b"OTF-minha")
    outside = tmp_path / "fora"
    outside.mkdir()
    (outside / "segredo.ttf").write_bytes(b"SEGREDO")
    registry = [("Impact (TrueType)", "impact.ttf"), ("Arial (TrueType)", "arial.ttf"),
                ("Cambria & Cambria Math (TrueType)", "cambria.ttc"), ("Sumiu (TrueType)", "sumiu.ttf"),
                ("Minha Fonte (OpenType)", str(user_fonts / "minha.otf")),
                ("Fora (TrueType)", str(outside / "segredo.ttf"))]
    return {"base": base, "user": user, "rp_db": rp_db, "effect": effect, "system_font": system_font,
            "windows": win, "user_fonts": user_fonts, "outside": outside, "registry": registry}


def install_fakes(monkeypatch, tmp_path: Path) -> dict[str, Any]:
    """Instalação falsa + fontes do Windows falsas, ativas para `install.detect()` e para o catálogo."""
    env = make_capcut(tmp_path)
    monkeypatch.setenv("MEDIA_TOOL_CAPCUT_ROOT", str(env["base"]))
    monkeypatch.setattr(catalog, "windows_font_dirs", lambda env_=None: [env["windows"], env["user_fonts"]])
    monkeypatch.setattr(catalog, "_registry_fonts", lambda: list(env["registry"]))
    catalog.reset_cache()
    env["install"] = install_mod.detect()
    env["paths"] = catalog.paths_for(env["install"])
    return env


def design(**changes: Any) -> dict[str, Any]:
    """Design válido (fonte do Windows `impact.ttf` precisa do caminho da instalação falsa: use `font=`)."""
    base: dict[str, Any] = {
        "version": 1, "font": {"source": "capcut", "id": MONTSERRAT, "path": None, "name": "Montserrat"},
        "size": 8.0, "scale": 1.9, "bold": True, "italic": False, "underline": False, "uppercase": False,
        "color": "#FFFFFF", "opacity": 1.0, "stroke": {"enabled": True, "color": "#000000", "width": 30},
        "shadow": {"enabled": False, "color": "#000000", "opacity": 0.8, "blur": 20, "distance": 8, "angle": -45},
        "background": {"enabled": False, "color": "#000000", "opacity": 0.6, "radius": 0.3}, "align": "center",
        "letter_spacing": 0, "line_spacing": 0, "position": {"x": 0.0, "y": -0.7}, "max_line_width": 0.82,
        "highlight": {"enabled": True, "color": "#FFD400"}, "animation": {"in": None, "out": None, "loop": None},
    }
    base.update(changes)
    return base
