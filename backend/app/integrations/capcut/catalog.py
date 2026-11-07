"""Catálogo local de fontes e animações de texto do CapCut (sem rede, só leitura).

Fontes: embutidas do CapCut (`Apps\\<versão>\\Resources\\Font\\SystemFont`), do Windows (registro HKLM/HKCU
`SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Fonts`, arquivos em `%WINDIR%\\Fonts` ou
`%LOCALAPPDATA%\\Microsoft\\Windows\\Fonts`) e do catálogo do CapCut (effect_type 10). Animações de texto: effect_type
18, classificadas em entrada/saída/loop pelas categorias do painel (`ruchang`/`chuchang`/`xunhuan`).

O catálogo do CapCut vem das respostas guardadas em `User Data\\Cache\\ressdk_db\\<hash>\\rp.db` (tabela `http_cache`).
Os bancos NUNCA são abertos no lugar: cada um é copiado para uma pasta temporária e lido de lá. O resultado fica em
memória e num arquivo de cache, invalidados pelo mtime/tamanho de cada banco. `cover_url` (URLs externas assinadas) não
é usado. Recurso já baixado: `User Data\\Cache\\effect\\<id>\\<md5>\\`.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unicodedata
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.integrations.capcut.errors import CapCutError
from app.integrations.capcut.install import CapCutInstall

CACHE_VERSION = 1
FONT_EXTS = (".ttf", ".otf")
FONT_SOURCES = ("bundled", "windows", "capcut")
ANIM_TYPES = ("in", "out", "loop")
# Categorias do painel de animação de texto (respostas get_panel_info) -> tipo da animação no rascunho.
ANIM_CATEGORY_TYPES = {"ruchang": "in", "chuchang": "out", "xunhuan": "loop"}
# (category_id, category_name) gravados no rascunho para cada tipo (verificado: "ruchang"/"Entrada").
ANIM_CATEGORY_INFO = {"in": ("ruchang", "Entrada"), "out": ("chuchang", "Saída"), "loop": ("xunhuan", "Loop")}
DEFAULT_ANIM_MS = 500
_REG_SUFFIXES = (" (TrueType)", " (OpenType)", " (All res)")
_lock = threading.Lock()


class CatalogError(CapCutError):
    def __init__(self, message: str) -> None:
        super().__init__("invalid_design", message)


# --------------------------------------------------------------------------------------------- pastas

@dataclass(frozen=True)
class CatalogPaths:
    ressdk_dir: Path | None      # User Data\Cache\ressdk_db
    effect_dir: Path | None      # User Data\Cache\effect
    bundled_dir: Path | None     # Apps\<versão>\Resources\Font
    windows_dirs: tuple[Path, ...] = ()


def windows_font_dirs(env: dict[str, str] | None = None) -> list[Path]:
    env = dict(os.environ) if env is None else env
    dirs = []
    windir = (env.get("WINDIR") or env.get("SystemRoot") or "").strip()
    if windir:
        dirs.append(Path(windir) / "Fonts")
    local = (env.get("LOCALAPPDATA") or "").strip()
    if local:
        dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    return dirs


def paths_for(install: CapCutInstall | None) -> CatalogPaths:
    cache = install.root / "Cache" if install is not None else None
    bundled = install.app_dir / "Resources" / "Font" if install is not None and install.app_dir is not None else None
    return CatalogPaths(ressdk_dir=cache / "ressdk_db" if cache else None, effect_dir=cache / "effect" if cache else None,
                        bundled_dir=bundled, windows_dirs=tuple(windows_font_dirs()))


def _within(path: Path, roots: Iterable[Path]) -> Path | None:
    """Caminho resolvido (seguindo links) se estiver dentro de alguma das pastas; senão None."""
    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    for root in roots:
        try:
            base = Path(root).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if resolved == base or resolved.is_relative_to(base):
            return resolved
    return None


def _plain_name(name: str) -> bool:
    """Só um nome de arquivo (sem separadores, `..`, unidade ou NUL)."""
    return bool(name) and name not in (".", "..") and ".." not in name and not any(c in name for c in "/\\:\x00")


def _fold(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in norm if not unicodedata.combining(c)).casefold()


# --------------------------------------------------------------------------------------------- leitura do rp.db

def _body_text(body: Any) -> str:
    if isinstance(body, bytes):
        for decode in (lambda b: b.decode("utf-8"), lambda b: zlib.decompress(b).decode("utf-8")):
            try:
                return decode(body)
            except (UnicodeDecodeError, zlib.error):
                continue
        return ""
    return body if isinstance(body, str) else ""


def _json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}
    return {}


def _is_pro(common: dict[str, Any]) -> bool:
    biz = _json_obj((common.get("business_info") or {}).get("json_str")
                    if isinstance(common.get("business_info"), dict) else None)
    if biz:
        paid = str(biz.get("paid_type") or "free").lower()
        return bool(biz.get("is_vip")) or paid not in ("", "free")
    return bool(_json_obj(common.get("extra")).get("is_vip"))


def _anim_duration_ms(item: dict[str, Any], common: dict[str, Any]) -> int | None:
    ta = item.get("text_animation") if isinstance(item.get("text_animation"), dict) else {}
    try:
        ms = int(ta.get("duration") or 0)
    except (TypeError, ValueError):
        ms = 0
    if ms <= 0:
        setting = _json_obj(common.get("sdk_extra")).get("setting") or {}
        try:
            ms = int(round(float(setting.get("animation_duration") or 0) * 1000))
        except (TypeError, ValueError, AttributeError):
            ms = 0
    return ms if ms > 0 else None


def _walk_fonts(obj: Any, fonts: dict[str, dict[str, Any]]) -> None:
    if isinstance(obj, dict):
        common = obj.get("common_attr")
        if isinstance(common, dict) and common.get("effect_type") == 10 and common.get("id") and common.get("title"):
            fid = str(common["id"])
            fonts.setdefault(fid, {"id": fid, "name": str(common["title"]), "is_pro": _is_pro(common)})
        for value in obj.values():
            _walk_fonts(value, fonts)
    elif isinstance(obj, list):
        for value in obj:
            _walk_fonts(value, fonts)


def _merge_anim(anims: dict[str, dict[str, Any]], item: dict[str, Any], category_key: str | None) -> None:
    common = item.get("common_attr") if isinstance(item.get("common_attr"), dict) else {}
    if common.get("effect_type") != 18 or not common.get("id") or not common.get("title"):
        return
    aid = str(common["id"])
    entry = anims.setdefault(aid, {
        "id": aid, "name": str(common["title"]), "is_pro": _is_pro(common),
        "third_resource_id": str(common.get("third_resource_id_str") or common.get("third_resource_id") or aid),
        "categories": [], "anim_types": [], "duration_ms": None})
    if category_key and category_key not in entry["categories"]:
        entry["categories"].append(category_key)
    ta = item.get("text_animation") if isinstance(item.get("text_animation"), dict) else {}
    kind = ta.get("animation_type")
    if isinstance(kind, str) and kind and kind not in entry["anim_types"]:
        entry["anim_types"].append(kind)
    if entry["duration_ms"] is None:
        entry["duration_ms"] = _anim_duration_ms(item, common)


def _walk_panels(obj: Any, anims: dict[str, dict[str, Any]]) -> None:
    """Respostas de painel: `data.categories` (id -> chave) + `data.category_resources[id].effect_item_list`."""
    if isinstance(obj, dict):
        cats, resources = obj.get("categories"), obj.get("category_resources")
        if isinstance(cats, list) and isinstance(resources, dict):
            keys = {str(c.get("category_id")): c.get("category_key") for c in cats if isinstance(c, dict)}
            for cid, blob in resources.items():
                items = blob.get("effect_item_list") if isinstance(blob, dict) else None
                for item in items if isinstance(items, list) else []:
                    if isinstance(item, dict):
                        _merge_anim(anims, item, keys.get(str(cid)))
            return
        for value in obj.values():
            _walk_panels(value, anims)
    elif isinstance(obj, list):
        for value in obj:
            _walk_panels(value, anims)


def parse_rp_db(path: Path) -> dict[str, Any]:
    """Fontes e animações das respostas guardadas num rp.db (lido de uma CÓPIA; o original não é aberto)."""
    fonts: dict[str, dict[str, Any]] = {}
    anims: dict[str, dict[str, Any]] = {}
    tmp = Path(tempfile.mkdtemp(prefix="mt_capcut_catalog_"))
    try:
        copy = tmp / "rp.db"
        shutil.copy2(path, copy)
        for suffix in ("-wal", "-shm"):
            side = Path(f"{path}{suffix}")
            if side.is_file():
                shutil.copy2(side, Path(f"{copy}{suffix}"))
        con = sqlite3.connect(str(copy))
        try:
            rows = con.execute("SELECT response_body FROM http_cache WHERE url LIKE '%get_panel_info%' ORDER BY id")
            for (body,) in rows:
                try:
                    data = json.loads(_body_text(body))
                except ValueError:
                    continue
                _walk_fonts(data, fonts)
                _walk_panels(data, anims)
        finally:
            con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"fonts": fonts, "animations": anims}


def _signature(path: Path) -> list[int]:
    sig = []
    for p in (path, Path(f"{path}-wal")):
        try:
            st = p.stat()
            sig += [st.st_mtime_ns, st.st_size]
        except OSError:
            sig += [0, 0]
    return sig


# --------------------------------------------------------------------------------------------- catálogo do CapCut

@dataclass
class CapCutCatalog:
    fonts: dict[str, dict[str, Any]] = field(default_factory=dict)
    animations: list[dict[str, Any]] = field(default_factory=list)    # só as classificadas (com `type`)
    excluded_animations: int = 0                                      # sem classificação segura


_memory: dict[str, Any] = {"key": None, "dbs": {}, "catalog": None}


def classify(entry: dict[str, Any]) -> str | None:
    """`in`/`out`/`loop` quando as categorias do painel apontam um só tipo e o `animation_type` do item (se houver)
    concorda; senão None (fica fora da lista)."""
    types = {ANIM_CATEGORY_TYPES[c] for c in entry.get("categories") or [] if c in ANIM_CATEGORY_TYPES}
    declared = {t for t in entry.get("anim_types") or [] if t}
    if len(types) != 1 or not declared <= types:
        return None
    return next(iter(types))


def _build(dbs: dict[str, dict[str, Any]]) -> CapCutCatalog:
    fonts: dict[str, dict[str, Any]] = {}
    merged: dict[str, dict[str, Any]] = {}
    for key in sorted(dbs):
        data = dbs[key]
        for fid, font in (data.get("fonts") or {}).items():
            fonts.setdefault(fid, font)
        for aid, anim in (data.get("animations") or {}).items():
            cur = merged.setdefault(aid, {**anim, "categories": [], "anim_types": []})
            cur["categories"] = list(dict.fromkeys([*cur["categories"], *anim.get("categories", [])]))
            cur["anim_types"] = list(dict.fromkeys([*cur["anim_types"], *anim.get("anim_types", [])]))
            if cur.get("duration_ms") is None:
                cur["duration_ms"] = anim.get("duration_ms")
    animations, excluded = [], 0
    for anim in merged.values():
        kind = classify(anim)
        if kind is None:
            excluded += 1
            continue
        animations.append({"id": anim["id"], "name": anim["name"], "type": kind, "is_pro": bool(anim["is_pro"]),
                           "third_resource_id": anim.get("third_resource_id") or anim["id"],
                           "default_duration_ms": int(anim.get("duration_ms") or DEFAULT_ANIM_MS)})
    return CapCutCatalog(fonts=fonts, animations=animations, excluded_animations=excluded)


def _load_file_cache(cache_file: Path | None) -> dict[str, Any]:
    if cache_file is None or not cache_file.is_file():
        return {}
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION or not isinstance(data.get("dbs"), dict):
        return {}
    return data["dbs"]


def _save_file_cache(cache_file: Path | None, dbs: dict[str, Any]) -> None:
    if cache_file is None:
        return
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_name(cache_file.name + ".tmp")
        tmp.write_text(json.dumps({"version": CACHE_VERSION, "dbs": dbs}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, cache_file)
    except OSError:
        pass  # cache é só desempenho


def capcut_catalog(paths: CatalogPaths, cache_file: Path | None = None) -> CapCutCatalog:
    """Catálogo do CapCut (fontes + animações classificadas), relendo só os bancos que mudaram."""
    ressdk = paths.ressdk_dir
    db_files = sorted(ressdk.glob("*/rp.db")) if ressdk is not None and ressdk.is_dir() else []
    with _lock:
        if _memory["key"] != str(ressdk):
            _memory.update(key=str(ressdk), dbs=_load_file_cache(cache_file), catalog=None)
        old: dict[str, Any] = _memory["dbs"]
        dbs: dict[str, Any] = {}
        changed = False
        for db in db_files:
            key = str(db)
            sig = _signature(db)
            cached = old.get(key)
            if cached is not None and cached.get("sig") == sig:
                dbs[key] = cached
                continue
            try:
                parsed = parse_rp_db(db)
            except (OSError, sqlite3.Error):
                # Banco em gravação/ilegível: fica a leitura anterior (ou vazio) até o arquivo mudar de novo.
                dbs[key] = {**(cached or {"fonts": {}, "animations": {}}), "sig": sig}
                changed = True
                continue
            dbs[key] = {"sig": sig, **parsed}
            changed = True
        if set(dbs) != set(old):
            changed = True
        if changed or _memory["catalog"] is None:
            _memory["dbs"] = dbs
            _memory["catalog"] = _build(dbs)
            if changed:
                _save_file_cache(cache_file, dbs)
        return _memory["catalog"]


def reset_cache() -> None:
    with _lock:
        _memory.update(key=None, dbs={}, catalog=None)


# --------------------------------------------------------------------------------------------- recursos baixados

def _effect_ids(paths: CatalogPaths) -> set[str]:
    """Ids com pasta em `Cache/effect` (uma listagem só, em vez de um stat por item do catálogo)."""
    try:
        return set(os.listdir(paths.effect_dir)) if paths.effect_dir is not None else set()
    except OSError:
        return set()


def _downloaded_dir(paths: CatalogPaths, resource_id: str) -> Path | None:
    if paths.effect_dir is None or not resource_id.isdigit():
        return None
    folder = paths.effect_dir / resource_id
    if not folder.is_dir():
        return None
    for sub in sorted(folder.iterdir()):
        if sub.is_dir() and not sub.name.endswith("_tmp") and any(sub.iterdir()):
            return sub
    return None


def _downloaded_font_file(paths: CatalogPaths, resource_id: str) -> Path | None:
    sub = _downloaded_dir(paths, resource_id)
    if sub is None:
        return None
    for candidate in sorted(sub.rglob("*")):
        if candidate.is_file() and candidate.suffix.lower() in FONT_EXTS:
            return _within(candidate, [paths.effect_dir / resource_id])
    return None


# --------------------------------------------------------------------------------------------- fontes locais

def _registry_fonts() -> list[tuple[str, str]]:
    """(nome, arquivo) das fontes registradas no Windows (HKLM e HKCU)."""
    try:
        import winreg
    except ImportError:
        return []
    out: list[tuple[str, str]] = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if isinstance(name, str) and isinstance(value, str):
                    out.append((name, value))
    return out


def _clean_reg_name(name: str) -> str:
    for suffix in _REG_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name.strip()


def windows_fonts(paths: CatalogPaths) -> list[dict[str, Any]]:
    roots = list(paths.windows_dirs)
    if not roots:
        return []
    seen: dict[str, dict[str, Any]] = {}
    for name, value in _registry_fonts():
        raw = Path(value)
        candidates = [raw] if raw.is_absolute() else [root / value for root in roots]
        for cand in candidates:
            if cand.suffix.lower() not in FONT_EXTS or not cand.is_file():
                continue
            resolved = _within(cand, roots)
            if resolved is None:
                continue
            file_name = resolved.name
            if file_name.casefold() not in seen:
                seen[file_name.casefold()] = {
                    "key": f"windows:{file_name}", "name": _clean_reg_name(name) or resolved.stem, "source": "windows",
                    "id": None, "path": resolved.as_posix(), "is_pro": False, "downloaded": True, "previewable": True}
            break
    return list(seen.values())


def bundled_fonts(paths: CatalogPaths) -> list[dict[str, Any]]:
    folder = paths.bundled_dir / "SystemFont" if paths.bundled_dir is not None else None
    if folder is None or not folder.is_dir():
        return []
    out = []
    for f in sorted(folder.iterdir()):
        if f.is_file() and f.suffix.lower() in FONT_EXTS:
            out.append({"key": f"bundled:{f.name}", "name": f.stem, "source": "bundled", "id": None,
                        "path": f.resolve().as_posix(), "is_pro": False, "downloaded": True, "previewable": True})
    return out


def capcut_fonts(paths: CatalogPaths, cache_file: Path | None) -> list[dict[str, Any]]:
    out = []
    present = _effect_ids(paths)
    for font in capcut_catalog(paths, cache_file).fonts.values():
        here = font["id"] in present
        local = _downloaded_font_file(paths, font["id"]) if here else None
        out.append({"key": f"capcut:{font['id']}", "name": font["name"], "source": "capcut", "id": font["id"],
                    "path": local.as_posix() if local else None, "is_pro": bool(font["is_pro"]),
                    "downloaded": here and _downloaded_dir(paths, font["id"]) is not None,
                    "previewable": local is not None})
    return out


# --------------------------------------------------------------------------------------------- consultas

def _page(items: list[dict[str, Any]], limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
    return items[offset:offset + limit], len(items)


def list_fonts(paths: CatalogPaths, *, q: str | None = None, source: str | None = None, include_pro: bool = False,
               limit: int = 50, offset: int = 0, cache_file: Path | None = None) -> tuple[list[dict[str, Any]], int]:
    items: list[dict[str, Any]] = []
    for src, getter in (("bundled", lambda: bundled_fonts(paths)), ("windows", lambda: windows_fonts(paths)),
                        ("capcut", lambda: capcut_fonts(paths, cache_file))):
        if source in (None, src):
            items.extend(sorted(getter(), key=lambda f: _fold(f["name"])))
    if not include_pro:
        items = [f for f in items if not f["is_pro"]]
    if q:
        needle = _fold(q.strip())
        items = [f for f in items if needle in _fold(f["name"])]
    return _page(items, limit, offset)


def list_animations(paths: CatalogPaths, *, type: str | None = None, q: str | None = None,  # noqa: A002
                    include_pro: bool = False, limit: int = 50, offset: int = 0,
                    cache_file: Path | None = None) -> tuple[list[dict[str, Any]], int]:
    items = []
    present = _effect_ids(paths)
    for anim in capcut_catalog(paths, cache_file).animations:
        if type is not None and anim["type"] != type:
            continue
        if not include_pro and anim["is_pro"]:
            continue
        if q and _fold(q.strip()) not in _fold(anim["name"]):
            continue
        items.append({"id": anim["id"], "name": anim["name"], "type": anim["type"], "is_pro": anim["is_pro"],
                      "downloaded": anim["id"] in present and _downloaded_dir(paths, anim["id"]) is not None,
                      "default_duration_ms": anim["default_duration_ms"]})
    return _page(items, limit, offset)


def find_animation(paths: CatalogPaths, anim_id: str, kind: str,
                   cache_file: Path | None = None) -> dict[str, Any] | None:
    return next((a for a in capcut_catalog(paths, cache_file).animations if a["id"] == anim_id and a["type"] == kind),
                None)


def font_file(paths: CatalogPaths, key: str) -> Path | None:
    """Arquivo local da fonte `key` SÓ de pastas permitidas (fontes do Windows, Resources\\Font do CapCut e
    Cache\\effect\\<id> já baixado). None se a chave for inválida ou não houver arquivo."""
    source, sep, rest = (key or "").partition(":")
    if not sep or not _plain_name(rest):
        return None
    if source == "windows":
        roots = list(paths.windows_dirs)
        for root in roots:
            cand = root / rest
            if cand.suffix.lower() in FONT_EXTS and cand.is_file():
                return _within(cand, roots)
        return None
    if source == "bundled":
        if paths.bundled_dir is None or not paths.bundled_dir.is_dir():
            return None
        for cand in sorted(paths.bundled_dir.rglob(rest)):
            if cand.name == rest and cand.is_file() and cand.suffix.lower() in FONT_EXTS:
                return _within(cand, [paths.bundled_dir])
        return None
    if source == "capcut" and rest.isdigit():
        return _downloaded_font_file(paths, rest)
    return None


def validate_font(paths: CatalogPaths, font: dict[str, Any], cache_file: Path | None = None) -> dict[str, Any]:
    """Fonte do design normalizada; CatalogError (`invalid_design`) se não for permitida."""
    source = font.get("source")
    name = str(font.get("name") or "").strip()
    if source == "capcut":
        fid = str(font.get("id") or "")
        if not fid.isdigit() or fid not in capcut_catalog(paths, cache_file).fonts:
            raise CatalogError("Fonte do CapCut não encontrada no catálogo local.")
        return {"source": "capcut", "id": fid, "path": None,
                "name": name or capcut_catalog(paths, cache_file).fonts[fid]["name"]}
    raw = str(font.get("path") or "")
    if not raw or "\x00" in raw or raw.startswith(("\\\\", "//")):
        raise CatalogError("Informe o arquivo da fonte.")
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts or path.suffix.lower() not in FONT_EXTS:
        raise CatalogError("Arquivo de fonte inválido (use .ttf ou .otf).")
    if source == "windows":
        roots = list(paths.windows_dirs)
    elif source == "bundled":
        roots = [paths.bundled_dir] if paths.bundled_dir is not None else []
    else:
        raise CatalogError("Origem da fonte inválida.")
    resolved = _within(path, roots) if roots else None
    if resolved is None or not resolved.is_file():
        raise CatalogError("A fonte precisa ser um arquivo existente nas pastas de fontes permitidas.")
    return {"source": source, "id": None, "path": resolved.as_posix(), "name": name or resolved.stem}
