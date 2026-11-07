"""Motor de rascunho do CapCut 9.x (Windows): draft_content.json na raiz + espelho em Timelines/<id>/.

Diferente do capcut-agent-skill (layout macOS com draft_info.json). Regras: tempos inteiros em microssegundos, o
mesmo JSON na raiz e no espelho, uma trilha de vídeo principal + UMA trilha de texto sem sobreposição, gravação
atômica, rascunho sempre novo com prefixo `MT_` e nada é gravado/registrado com o CapCut aberto.
"""

from __future__ import annotations

import copy
import re
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.integrations.capcut import install as install_mod
from app.integrations.capcut.errors import CapCutBusy, CapCutError
from app.integrations.capcut.install import CapCutInstall
from app.integrations.capcut.media import MediaProbe, atomic_write_json, new_id, probe, read_json
from app.integrations.capcut.style import GENERATED_PREFIX, CaptionStyle, HighlightOptions, apply_style
from app.integrations.capcut.transcribe import Chunk

ASSETS_BASE_DRAFT = Path(__file__).resolve().parent / "assets" / "base_draft"
ROOT_META_BACKUP = "root_meta_info.json.bak-media-tool"
_TRANSIENT = ("draft_content.json.bak", "template-2.tmp", "template.tmp", ".locked", "project.json.bak",
              "draft_cover.jpg")
_NAME_RE = re.compile(r"^MT_[A-Za-z0-9_\-]{1,120}$")

# Campos mínimos de uma entrada de root_meta_info.json (observados no CapCut 9.7.1).
MINIMAL_ROOT_ENTRY: dict[str, Any] = {
    "cloud_draft_cover": False, "cloud_draft_sync": False, "draft_cloud_last_action_download": False,
    "draft_cloud_purchase_info": "", "draft_cloud_template_id": "", "draft_cloud_tutorial_info": "",
    "draft_cloud_videocut_purchase_info": "", "draft_cover": "", "draft_fold_path": "", "draft_id": "",
    "draft_is_ai_shorts": False, "draft_is_cloud_temp_draft": False, "draft_is_infinite_canvas_draft": False,
    "draft_is_invisible": False, "draft_is_pippit_draft": False, "draft_is_web_article_video": False,
    "draft_json_file": "", "draft_name": "", "draft_new_version": "", "draft_root_path": "",
    "draft_timeline_materials_size": 0, "draft_type": "", "draft_web_article_video_enter_from": "",
    "pippit_avatar_url": "", "pippit_extra_info": "", "pippit_id": "", "pippit_user_name": "",
    "streaming_edit_draft_ready": True, "tm_draft_cloud_completed": "", "tm_draft_cloud_entry_id": -1,
    "tm_draft_cloud_modified": 0, "tm_draft_cloud_parent_entry_id": -1, "tm_draft_cloud_space_id": -1,
    "tm_draft_cloud_user_id": -1, "tm_draft_create": 0, "tm_draft_modified": 0, "tm_draft_removed": 0,
    "tm_duration": 0,
}

RunningCheck = Callable[[], list[int]]

# Nuvem/compartilhamento: nada do projeto-modelo do dono (ids de nuvem, espaço, usuário, empresa) vai para o MT_.
NEUTRAL_ENTERPRISE_INFO: dict[str, Any] = {"draft_enterprise_extra": "", "draft_enterprise_id": "",
                                           "draft_enterprise_name": "", "enterprise_material": []}


def _is_cloud_key(key: str) -> bool:
    return "cloud" in key or key.startswith(("pippit_", "draft_enterprise")) or key == "draft_is_pippit_draft"


def neutralize_cloud_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Zera os campos de nuvem/compartilhamento com os valores neutros dos rascunhos locais (False, "", -1 nos ids,
    0 nos demais números; `draft_enterprise_info` vazio). Altera e devolve `data`."""
    for key, value in list(data.items()):
        if not _is_cloud_key(key):
            continue
        if key == "draft_enterprise_info":
            data[key] = copy.deepcopy(NEUTRAL_ENTERPRISE_INFO)
        elif isinstance(value, bool):
            data[key] = False
        elif isinstance(value, (int, float)):
            data[key] = -1 if key.endswith("_id") else 0
        elif isinstance(value, str):
            data[key] = ""
        elif isinstance(value, list):
            data[key] = []
        elif isinstance(value, dict):
            data[key] = {}
        else:
            data[key] = None
    return data


def _ensure_closed(running: RunningCheck | None) -> None:
    pids = (running or install_mod.is_running)()
    if pids:
        raise CapCutBusy("CapCut aberto: rascunhos só são gravados com o CapCut fechado.")


def validate_name(name: str) -> str:
    if not _NAME_RE.fullmatch(name or ""):
        raise CapCutError("invalid_draft_name", "Nome de rascunho inválido (precisa começar com MT_).")
    return name


# --------------------------------------------------------------------------------------------- montagem

def _refs_of(segment: dict[str, Any]) -> set[str]:
    refs = set(segment.get("extra_material_refs") or [])
    if segment.get("material_id"):
        refs.add(segment["material_id"])
    return refs


def _index_materials(materials: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for items in materials.values():
        if isinstance(items, list):
            for obj in items:
                if isinstance(obj, dict) and obj.get("id"):
                    index[obj["id"]] = obj
    return index


def _full_crop() -> dict[str, float]:
    return {"upper_left_x": 0.0, "upper_left_y": 0.0, "upper_right_x": 1.0, "upper_right_y": 0.0,
            "lower_left_x": 0.0, "lower_left_y": 1.0, "lower_right_x": 1.0, "lower_right_y": 1.0}


def _compose(content: dict[str, Any], *, video: Path, info: MediaProbe, chunks: list[Chunk], style: CaptionStyle,
             fps: float | None = None,
             highlight: HighlightOptions | None, clip: tuple[float, float] | None = None) -> dict[str, Any]:
    d = copy.deepcopy(content)
    materials: dict[str, Any] = d.setdefault("materials", {})
    tracks = d.get("tracks") or []
    main = next((t for t in tracks if t.get("type") == "video" and t.get("segments")), None)
    if main is None:
        raise CapCutError("draft_invalid", "O rascunho-base não tem trilha de vídeo.")
    seg = main["segments"][0]

    kept_refs = _refs_of(seg)
    removed_refs: set[str] = set()
    for track in tracks:
        for other in track.get("segments") or []:
            if other is not seg:
                removed_refs |= _refs_of(other)
    drop = removed_refs - kept_refs
    for name, items in list(materials.items()):
        if isinstance(items, list):
            materials[name] = [o for o in items if not (isinstance(o, dict) and o.get("id") in drop)]
    materials["texts"] = []
    if isinstance(materials.get("audios"), list):
        materials["audios"] = []

    index = _index_materials(materials)
    video_mat = index.get(seg.get("material_id"))
    if video_mat is None:
        raise CapCutError("draft_invalid", "O rascunho-base não tem o material do vídeo.")
    materials["videos"] = [video_mat]
    full = max(1, int(round(info.duration * 1_000_000)))
    # Recorte (prévia): a linha do tempo mostra só o trecho [início, início + duração) do vídeo.
    src_start = min(max(0, int(round(clip[0] * 1_000_000))), full - 1) if clip else 0
    dur = max(1, min(int(round(clip[1] * 1_000_000)), full - src_start)) if clip else full
    vpath = Path(video).resolve()
    video_mat.update(id=new_id(), path=vpath.as_posix(), duration=full, width=info.width, height=info.height,
                     has_audio=info.has_audio, material_name=vpath.name, local_material_id="", media_path="",
                     crop=_full_crop(), type="video")
    new_refs = []
    for ref in seg.get("extra_material_refs") or []:
        obj = index.get(ref)
        if obj is None:
            continue
        obj["id"] = new_id()
        if obj.get("type") == "speed":
            obj.update(speed=1.0, mode=0, curve_speed=None)
        new_refs.append(obj["id"])
    seg.update(id=new_id(), material_id=video_mat["id"], extra_material_refs=new_refs,
               source_timerange={"start": src_start, "duration": dur}, target_timerange={"start": 0, "duration": dur},
               speed=1.0, reverse=False, is_loop=False, keyframe_refs=[], common_keyframes=[])
    clip = seg.setdefault("clip", {})
    clip.update(scale={"x": 1.0, "y": 1.0}, rotation=0.0, transform={"x": 0.0, "y": 0.0})
    main.update(id=new_id(), segments=[seg])
    for key, value in list((d.get("keyframes") or {}).items()):
        if isinstance(value, list):
            d["keyframes"][key] = []

    text_segments: list[dict[str, Any]] = []
    for chunk in chunks:
        start_us = int(round(chunk.start * 1_000_000))
        end_us = min(int(round(chunk.end * 1_000_000)), dur)
        if text_segments:
            last = text_segments[-1]["target_timerange"]
            start_us = max(start_us, last["start"] + last["duration"])
        if end_us <= start_us:
            continue
        mats, segs, extras = apply_style(style, chunk, start_us, end_us - start_us, highlight)
        materials["texts"].extend(mats)
        for extra in extras:
            materials.setdefault(extra["array"], []).append(extra["object"])
        text_segments.extend(segs)
    for i, s in enumerate(text_segments):
        s["render_index"] = 14000 + i
    d["tracks"] = [main]
    if text_segments:
        d["tracks"].append({"id": new_id(), "type": "text", "segments": text_segments, "flag": 0, "attribute": 0,
                            "name": "", "is_default_name": True})
    d["duration"] = dur
    # fps da linha do tempo = fps escolhido para a exportação (o CapCut usa o do rascunho como padrão).
    d["fps"] = float(fps) if fps else (float(info.fps) if info.fps > 0 else 30.0)
    canvas = d.setdefault("canvas_config", {})
    canvas.update(ratio="original", width=info.width, height=info.height)
    d["id"] = new_id()
    return d


def build_draft(install: CapCutInstall, *, name: str, video: Path, chunks: list[Chunk], style: CaptionStyle,
                highlight: HighlightOptions | None, base_draft: Path | None, info: MediaProbe | None = None,
                running: RunningCheck | None = None, fps: float | None = None,
                clip: tuple[float, float] | None = None) -> Path:
    """Cria o rascunho `name` (prefixo MT_) com o vídeo e as legendas; devolve a pasta. Não registra.
    `clip` = (início, duração) em segundos: só esse trecho do vídeo entra na linha do tempo (prévia)."""
    validate_name(name)
    _ensure_closed(running)
    base = Path(base_draft) if base_draft else ASSETS_BASE_DRAFT
    if not (base / "draft_content.json").is_file():
        raise CapCutError("draft_invalid", "Rascunho-base não encontrado.")
    info = info or probe(video)
    drafts_dir = install.drafts_dir
    drafts_dir.mkdir(parents=True, exist_ok=True)
    dst = drafts_dir / name
    if dst.exists():
        # Só nomes MT_ chegam aqui: é sobra de uma tentativa anterior deste mesmo job.
        shutil.rmtree(dst)
    # Pasta temporária curta (caminhos do Windows limitados a 260 caracteres); sobra antiga é do mesmo job.
    staging = drafts_dir / f".{name}.building"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        shutil.copytree(base, staging, ignore=shutil.ignore_patterns(*_TRANSIENT))
        content = read_json(staging / "draft_content.json")
        old_tl = content.get("id")
        draft = _compose(content, video=video, info=info, chunks=chunks, style=style, highlight=highlight, fps=fps,
                         clip=clip)
        new_tl = draft["id"]

        tl_root = staging / "Timelines"
        tl_root.mkdir(exist_ok=True)
        pj_path = tl_root / "project.json"
        pj = read_json(pj_path) if pj_path.is_file() else {}
        main_tl = pj.get("main_timeline_id") or old_tl
        entries = [t for t in pj.get("timelines") or [] if t.get("id") == main_tl] or [
            {"create_time": 0, "id": main_tl, "is_marked_delete": False, "name": "Linha do tempo 01", "update_time": 0}]
        for child in tl_root.iterdir():
            if child.is_dir() and child.name != main_tl:
                shutil.rmtree(child)
        if main_tl and (tl_root / main_tl).is_dir():
            (tl_root / main_tl).rename(tl_root / new_tl)
        (tl_root / new_tl).mkdir(exist_ok=True)
        timeline_entry = dict(entries[0], id=new_tl)
        pj.update(id=new_id(), main_timeline_id=new_tl, timelines=[timeline_entry])
        atomic_write_json(pj_path, pj)

        layout_path = staging / "timeline_layout.json"
        layout = read_json(layout_path) if layout_path.is_file() else {"dockItems": [{}], "layoutOrientation": 1}
        docks = layout.get("dockItems") or [{}]
        docks[0].update(dockIndex=docks[0].get("dockIndex", 0), ratio=docks[0].get("ratio", 1), timelineIds=[new_tl],
                        timelineNames=[timeline_entry.get("name") or "Linha do tempo 01"])
        layout["dockItems"] = docks[:1]
        atomic_write_json(layout_path, layout)

        atomic_write_json(staging / "draft_content.json", draft)
        atomic_write_json(tl_root / new_tl / "draft_content.json", draft)
        for leftover in (tl_root / new_tl).iterdir():
            if leftover.name in _TRANSIENT:
                leftover.unlink()

        now = int(time.time() * 1_000_000)
        vpath = Path(video).resolve()
        dur = draft["duration"]
        meta_path = staging / "draft_meta_info.json"
        meta = read_json(meta_path) if meta_path.is_file() else {}
        meta.update(draft_id=new_id(), draft_name=name, draft_fold_path=dst.as_posix(),
                    draft_root_path=drafts_dir.as_posix(), tm_draft_create=now, tm_draft_modified=now,
                    tm_duration=dur, draft_timeline_materials_size_=info.size, draft_cover="draft_cover.jpg")
        neutralize_cloud_fields(meta)
        video_entry = {
            "ai_group_type": "", "create_time": now // 1_000_000, "duration": dur, "enter_from": 0,
            "extra_info": vpath.name, "file_Path": vpath.as_posix(), "height": info.height, "id": str(uuid.uuid4()),
            "import_time": now // 1_000_000, "import_time_ms": now, "item_source": 1, "material_color_tag": "",
            "md5": "", "metetype": "video", "roughcut_time_range": {"duration": dur, "start": 0},
            "sub_time_range": {"duration": -1, "start": -1}, "type": 0, "width": info.width,
        }
        others = [b for b in meta.get("draft_materials") or [] if b.get("type") != 0]
        meta["draft_materials"] = [{"type": 0, "value": [video_entry]}] + others
        atomic_write_json(meta_path, meta)

        problems = validate_draft(staging, expected_name=None)
        if problems:
            raise CapCutError("draft_invalid", "Rascunho gerado inválido: " + "; ".join(problems[:3]))
        staging.rename(dst)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return dst


# --------------------------------------------------------------------------------------------- validação

def validate_draft(draft_dir: Path, *, expected_name: str | None = None) -> list[str]:
    """Problemas encontrados (lista vazia = válido)."""
    problems: list[str] = []
    draft_dir = Path(draft_dir)
    root_file = draft_dir / "draft_content.json"
    try:
        raw = root_file.read_bytes()
        d = read_json(root_file)
    except (OSError, ValueError):
        return ["draft_content.json ilegível"]
    tl = d.get("id")
    mirror = draft_dir / "Timelines" / str(tl) / "draft_content.json"
    if not mirror.is_file() or mirror.read_bytes() != raw:
        problems.append("espelho da linha do tempo diferente da raiz")
    try:
        pj = read_json(draft_dir / "Timelines" / "project.json")
        if pj.get("main_timeline_id") != tl or [t.get("id") for t in pj.get("timelines") or []] != [tl]:
            problems.append("project.json incoerente")
    except (OSError, ValueError):
        problems.append("project.json ausente")
    layout_path = draft_dir / "timeline_layout.json"
    if layout_path.is_file():
        ids = [i for dock in read_json(layout_path).get("dockItems") or [] for i in dock.get("timelineIds") or []]
        if ids != [tl]:
            problems.append("timeline_layout.json incoerente")
    if expected_name is not None:
        try:
            if read_json(draft_dir / "draft_meta_info.json").get("draft_name") != expected_name:
                problems.append("nome no draft_meta_info diferente")
        except (OSError, ValueError):
            problems.append("draft_meta_info.json ausente")
    index = _index_materials(d.get("materials") or {})
    duration = int(d.get("duration") or 0)
    text_tracks = 0
    for track in d.get("tracks") or []:
        segs = track.get("segments") or []
        if track.get("type") == "text":
            text_tracks += 1
            ordered = sorted(segs, key=lambda s: s["target_timerange"]["start"])
            for a, b in zip(ordered, ordered[1:]):
                if a["target_timerange"]["start"] + a["target_timerange"]["duration"] > b["target_timerange"]["start"]:
                    problems.append("sobreposição na trilha de texto")
                    break
        for s in segs:
            if s.get("material_id") not in index:
                problems.append(f"material_id inexistente: {s.get('material_id')}")
            for ref in s.get("extra_material_refs") or []:
                if ref not in index:
                    problems.append(f"extra_material_ref inexistente: {ref}")
            tr = s.get("target_timerange") or {}
            if int(tr.get("duration") or 0) <= 0:
                problems.append("segmento com duração zero")
            if int(tr.get("start") or 0) + int(tr.get("duration") or 0) > duration:
                problems.append("segmento além da duração do vídeo")
    if text_tracks > 1:
        problems.append("mais de uma trilha de texto")
    return problems


# --------------------------------------------------------------------------------------------- root_meta_info

def _load_root_meta(install: CapCutInstall) -> dict[str, Any]:
    path = install.root_meta_path
    if path.is_file():
        data = read_json(path)
        if not isinstance(data, dict):
            raise CapCutError("draft_invalid", "root_meta_info.json inesperado.")
        data.setdefault("all_draft_store", [])
        return data
    return {"all_draft_store": [], "draft_ids": 0, "root_path": install.drafts_dir.as_posix()}


def _backup_root_meta(install: CapCutInstall) -> None:
    backup = install.drafts_dir / ROOT_META_BACKUP
    if install.root_meta_path.is_file() and not backup.exists():
        shutil.copy2(install.root_meta_path, backup)


def register_draft(install: CapCutInstall, draft_dir: Path, *, template_name: str | None = None,
                   running: RunningCheck | None = None) -> dict[str, Any]:
    """Põe o rascunho no índice global (1º da lista). Só com o CapCut fechado."""
    _ensure_closed(running)
    draft_dir = Path(draft_dir)
    meta = read_json(draft_dir / "draft_meta_info.json")
    name = validate_name(meta.get("draft_name") or draft_dir.name)
    rm = _load_root_meta(install)
    store: list[dict[str, Any]] = rm["all_draft_store"]
    template = next((e for e in store if template_name and e.get("draft_name") == template_name), None)
    entry = copy.deepcopy(MINIMAL_ROOT_ENTRY)
    if template is not None:
        entry.update(copy.deepcopy(template))
        neutralize_cloud_fields(entry)
    fold = draft_dir.as_posix()
    entry.update(
        draft_id=meta.get("draft_id"), draft_name=name, draft_fold_path=fold,
        draft_json_file=f"{fold}\\draft_content.json", draft_cover=f"{fold}\\draft_cover.jpg",
        draft_root_path=install.drafts_dir.as_posix(), tm_draft_create=meta.get("tm_draft_create", 0),
        tm_draft_modified=meta.get("tm_draft_modified", 0), tm_duration=meta.get("tm_duration", 0),
        draft_timeline_materials_size=meta.get("draft_timeline_materials_size_", 0), tm_draft_removed=0,
        draft_is_invisible=False,
    )
    _backup_root_meta(install)
    store[:] = [e for e in store if e.get("draft_name") != name and e.get("draft_fold_path") != fold]
    store.insert(0, entry)
    rm["draft_ids"] = len(store)
    rm.setdefault("root_path", install.drafts_dir.as_posix())
    atomic_write_json(install.root_meta_path, rm)
    return entry


def unregister_draft(install: CapCutInstall, name: str, *, running: RunningCheck | None = None) -> bool:
    """Tira o rascunho do índice global; True se havia entrada."""
    validate_name(name)
    _ensure_closed(running)
    if not install.root_meta_path.is_file():
        return False
    rm = _load_root_meta(install)
    store = rm["all_draft_store"]
    kept = [e for e in store if e.get("draft_name") != name]
    if len(kept) == len(store):
        return False
    _backup_root_meta(install)
    store[:] = kept
    rm["draft_ids"] = len(store)
    atomic_write_json(install.root_meta_path, rm)
    return True


def delete_draft(install: CapCutInstall, name: str, *, running: RunningCheck | None = None) -> None:
    """Desregistra e apaga a pasta de um rascunho gerado (só prefixo MT_)."""
    if not name.startswith(GENERATED_PREFIX):
        raise CapCutError("invalid_draft_name", "Só rascunhos criados pelo Media Tool podem ser apagados.")
    unregister_draft(install, name, running=running)
    folder = install.drafts_dir / name
    if folder.is_dir():
        shutil.rmtree(folder)
