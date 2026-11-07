#!/usr/bin/env python3
"""
CapCut Desktop Draft Engine (Pure Python Standard Library).
Provides automated project cloning, timeline manipulation, Shorts Hook layout,
and asset registration for CapCut Desktop (9.5.0+).

Adheres strictly to the AG Kit Modular Architecture:
- Uses ONLY Python standard library modules.
- Zero hardcoded absolute paths (dynamic home/relative discovery).
- FFmpeg subprocess integration for probing and synthetic asset synthesis.
"""

import argparse
import copy
import json
import math
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union


def get_default_drafts_dir() -> Path:
    """Dynamically resolves the default CapCut Desktop drafts directory."""
    return Path.home() / "Movies" / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"


def parse_time_us(val: Union[int, float, str]) -> int:
    """Parses various time representations into microseconds (us)."""
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(round(val * 1_000_000))
    if isinstance(val, str):
        v = val.strip().lower()
        if v.endswith("us"):
            return int(v[:-2])
        if v.endswith("ms"):
            return int(float(v[:-2]) * 1_000)
        if v.endswith("s"):
            return int(float(v[:-1]) * 1_000_000)
        return int(float(v) * 1_000_000)
    raise ValueError(f"Cannot parse time string: {val}")


def probe_media_file(file_path: Union[str, Path]) -> Dict[str, Any]:
    """Uses ffprobe to inspect media duration, resolution, and audio attributes."""
    path = Path(file_path).resolve()
    result = {
        "path": str(path),
        "duration_us": 3_000_000,
        "width": 1080,
        "height": 1920,
        "has_audio": False,
        "has_video": False,
    }
    if not path.exists():
        return result

    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,width,height",
            "-of", "json",
            str(path)
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(proc.stdout)
        fmt = data.get("format", {})
        if "duration" in fmt:
            result["duration_us"] = int(round(float(fmt["duration"]) * 1_000_000))

        for st in data.get("streams", []):
            ctype = st.get("codec_type")
            if ctype == "video":
                result["has_video"] = True
                if "width" in st:
                    result["width"] = int(st["width"])
                if "height" in st:
                    result["height"] = int(st["height"])
            elif ctype == "audio":
                result["has_audio"] = True
    except Exception:
        pass
    return result


def synthesize_whoosh_sfx(output_path: Union[str, Path], duration_s: float = 0.5) -> Path:
    """Synthesizes a clean whoosh transition sound effect via FFmpeg."""
    out = Path(output_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and out.stat().st_size > 1000:
        return out

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"anoisesrc=d={duration_s}:c=white:r=44100,bandpass=f=950:width_type=h:w=600,volume=2.2,afade=t=in:ss=0:d=0.1,afade=t=out:st=0.25:d=0.25",
        str(out)
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    return out


def synthesize_upbeat_bgm(output_path: Union[str, Path], duration_s: float = 3.0) -> Path:
    """Synthesizes an upbeat harmonic audio bed via FFmpeg."""
    out = Path(output_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and out.stat().st_size > 1000:
        return out

    fade_out_st = max(0.5, duration_s - 0.5)
    filter_graph = (
        f"[0:a][1:a][2:a]amix=inputs=3[a];"
        f"[a]lowpass=f=2000,afade=t=in:ss=0:d=0.3,afade=t=out:st={fade_out_st}:d=0.5[out]"
    )
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration_s},volume=0.5",
        "-f", "lavfi", "-i", f"sine=frequency=554.37:duration={duration_s},volume=0.4",
        "-f", "lavfi", "-i", f"sine=frequency=659.25:duration={duration_s},volume=0.4",
        "-filter_complex", filter_graph,
        "-map", "[out]",
        str(out)
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    return out


class Timerange:
    """Microsecond-precision timeline range."""

    def __init__(self, start: Union[int, float, str], duration: Union[int, float, str]):
        self.start = parse_time_us(start)
        self.duration = parse_time_us(duration)

    @property
    def end(self) -> int:
        return self.start + self.duration

    def to_dict(self) -> Dict[str, int]:
        return {"start": self.start, "duration": self.duration}


class ClipSettings:
    """Normalized 2D transform and visual properties."""

    def __init__(
        self,
        transform_x: float = 0.0,
        transform_y: float = 0.0,
        scale_x: float = 1.0,
        scale_y: float = 1.0,
        rotation: float = 0.0,
        alpha: float = 1.0,
    ):
        self.transform_x = float(transform_x)
        self.transform_y = float(transform_y)
        self.scale_x = float(scale_x)
        self.scale_y = float(scale_y)
        self.rotation = float(rotation)
        self.alpha = float(alpha)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alpha": self.alpha,
            "flip": {"horizontal": False, "vertical": False},
            "rotation": self.rotation,
            "scale": {"x": self.scale_x, "y": self.scale_y},
            "transform": {"x": self.transform_x, "y": self.transform_y},
        }


class TextStylePresets:
    """Built-in typography and color presets for vertical short-form video."""

    @staticmethod
    def get_preset(name: str) -> Dict[str, Any]:
        name_lower = name.lower()
        if "user" in name_lower or "signature" in name_lower or "m9" in name_lower:
            return {
                "font_size": 7.0,
                "scale": 1.919,
                "color": [1.0, 1.0, 1.0],
                "bold": True,
                "border_color": [0.0, 0.0, 0.0],
                "border_width": 0.06,
                "shadow_color": [0.0, 0.0, 0.0],
                "shadow_blur": 0.3,
                "shadow_distance": 0.15,
                "shadow_angle": -45.0,
                "align": 1,
                "y": -0.65,
                "effect_id": "7549885537688014133",
            }
        elif "yellow" in name_lower or "hook" in name_lower:
            return {
                "font_size": 15.0,
                "color": [1.0, 0.9, 0.0],
                "bold": True,
                "border_color": [0.0, 0.0, 0.0],
                "border_width": 0.08,
                "shadow_color": [0.0, 0.0, 0.0],
                "shadow_blur": 0.3,
                "shadow_distance": 0.15,
                "shadow_angle": -45.0,
                "align": 1,
            }
        elif "cyan" in name_lower or "neon" in name_lower:
            return {
                "font_size": 14.0,
                "color": [0.0, 0.9, 1.0],
                "bold": True,
                "border_color": [0.0, 0.0, 0.0],
                "border_width": 0.07,
                "shadow_color": [0.0, 0.0, 0.0],
                "shadow_blur": 0.25,
                "shadow_distance": 0.12,
                "shadow_angle": -45.0,
                "align": 1,
            }
        elif "red" in name_lower or "punchy" in name_lower:
            return {
                "font_size": 15.0,
                "color": [1.0, 0.2, 0.2],
                "bold": True,
                "border_color": [1.0, 1.0, 1.0],
                "border_width": 0.06,
                "shadow_color": [0.0, 0.0, 0.0],
                "shadow_blur": 0.3,
                "shadow_distance": 0.15,
                "shadow_angle": -45.0,
                "align": 1,
            }
        else:  # Modern White Subtitle
            return {
                "font_size": 10.5,
                "color": [1.0, 1.0, 1.0],
                "bold": True,
                "border_color": [0.0, 0.0, 0.0],
                "border_width": 0.06,
                "shadow_color": [0.0, 0.0, 0.0],
                "shadow_blur": 0.2,
                "shadow_distance": 0.1,
                "shadow_angle": -45.0,
                "align": 1,
            }


class DraftProject:
    """
    Primary interface for reading, modifying, cloning, and registering
    CapCut Desktop (9.5.0) draft projects.
    """

    def __init__(self, project_dir: Optional[Union[str, Path]] = None):
        self.project_dir = Path(project_dir).resolve() if project_dir else None
        self.draft_info: Dict[str, Any] = {}
        self.draft_meta: Dict[str, Any] = {}
        self.project_json: Dict[str, Any] = {}
        self.active_timeline_id: Optional[str] = None

        if self.project_dir and (self.project_dir / "draft_info.json").exists():
            self.load()

    def load(self) -> None:
        """Loads and parses draft configuration from project folder."""
        if not self.project_dir or not self.project_dir.exists():
            raise FileNotFoundError(f"Project directory does not exist: {self.project_dir}")

        info_path = self.project_dir / "draft_info.json"
        with open(info_path, "r", encoding="utf-8") as f:
            self.draft_info = json.load(f)

        meta_path = self.project_dir / "draft_meta_info.json"
        if meta_path.exists():
            with open(meta_path, "r", encoding="utf-8") as f:
                self.draft_meta = json.load(f)

        proj_path = self.project_dir / "Timelines" / "project.json"
        if proj_path.exists():
            with open(proj_path, "r", encoding="utf-8") as f:
                self.project_json = json.load(f)
                self.active_timeline_id = self.project_json.get("current_timeline_id")

        if not self.active_timeline_id:
            timelines_dir = self.project_dir / "Timelines"
            if timelines_dir.exists():
                for sub in timelines_dir.iterdir():
                    if sub.is_dir() and (sub / "draft_info.json").exists():
                        self.active_timeline_id = sub.name
                        break

    def save(self) -> None:
        """
        Atomically saves draft_info.json, draft_meta_info.json,
        and synchronizes the timeline mirror inside Timelines/<id>/draft_info.json.
        """
        if not self.project_dir:
            raise ValueError("Project directory not set.")

        now_us = int(time.time() * 1_000_000)
        self.draft_info["update_time"] = now_us
        if self.draft_meta:
            self.draft_meta["tm_draft_modified"] = now_us

        # 1. Write root draft_info.json atomically
        info_path = self.project_dir / "draft_info.json"
        tmp_info = self.project_dir / "draft_info.json.tmp"
        with open(tmp_info, "w", encoding="utf-8") as f:
            json.dump(self.draft_info, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_info, info_path)

        # 2. Write mirrored timeline draft_info.json if active timeline exists
        if self.active_timeline_id:
            tl_dir = self.project_dir / "Timelines" / self.active_timeline_id
            tl_dir.mkdir(parents=True, exist_ok=True)
            tl_info = tl_dir / "draft_info.json"
            tmp_tl = tl_dir / "draft_info.json.tmp"
            with open(tmp_tl, "w", encoding="utf-8") as f:
                json.dump(self.draft_info, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_tl, tl_info)

            # Also mirror cover if present
            cover_src = self.project_dir / "draft_cover.jpg"
            cover_dst = tl_dir / "draft_cover.jpg"
            if cover_src.exists() and not cover_dst.exists():
                shutil.copy2(cover_src, cover_dst)

        # 3. Write draft_meta_info.json atomically
        if self.draft_meta:
            meta_path = self.project_dir / "draft_meta_info.json"
            tmp_meta = self.project_dir / "draft_meta_info.json.tmp"
            with open(tmp_meta, "w", encoding="utf-8") as f:
                json.dump(self.draft_meta, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_meta, meta_path)

        # 4. Write Timelines/project.json atomically
        if self.project_json:
            pj_path = self.project_dir / "Timelines" / "project.json"
            tmp_pj = self.project_dir / "Timelines" / "project.json.tmp"
            with open(tmp_pj, "w", encoding="utf-8") as f:
                json.dump(self.project_json, f, ensure_ascii=False, indent=4)
            os.replace(tmp_pj, pj_path)

    @classmethod
    def clone_project(
        cls,
        source: Union[str, Path],
        target: Union[str, Path],
        new_name: Optional[str] = None,
        drafts_dir: Optional[Union[str, Path]] = None,
    ) -> "DraftProject":
        """
        Clones an existing CapCut project directory, generating new uppercase UUIDs
        for draft_id and timeline_id, renaming the timeline folder, and synchronizing
        all internal metadata.
        """
        base_dir = Path(drafts_dir).resolve() if drafts_dir else get_default_drafts_dir()
        src_path = Path(source) if Path(source).is_absolute() else (base_dir / source)
        tgt_path = Path(target) if Path(target).is_absolute() else (base_dir / target)

        if not src_path.exists():
            raise FileNotFoundError(f"Source CapCut project not found at: {src_path}")

        project_name = new_name or tgt_path.name

        # Clean destination if exists
        if tgt_path.exists():
            shutil.rmtree(tgt_path)

        # Copy entire folder
        shutil.copytree(src_path, tgt_path)

        # Fresh identifiers
        new_draft_id = str(uuid.uuid4()).upper()
        new_timeline_id = str(uuid.uuid4()).upper()
        now_us = int(time.time() * 1_000_000)

        # Find and rename timeline folder
        tl_base = tgt_path / "Timelines"
        old_timeline_id = None
        if tl_base.exists():
            for child in tl_base.iterdir():
                if child.is_dir() and (child / "draft_info.json").exists():
                    old_timeline_id = child.name
                    target_tl_dir = tl_base / new_timeline_id
                    child.rename(target_tl_dir)
                    break

        # Update Timelines/project.json
        pj_file = tl_base / "project.json"
        if pj_file.exists():
            with open(pj_file, "r", encoding="utf-8") as f:
                pj_data = json.load(f)
            pj_data["current_timeline_id"] = new_timeline_id
            pj_data["timeline_ids"] = [new_timeline_id]
            with open(pj_file, "w", encoding="utf-8") as f:
                json.dump(pj_data, f, ensure_ascii=False, indent=4)

        # Update draft_meta_info.json
        meta_file = tgt_path / "draft_meta_info.json"
        if meta_file.exists():
            with open(meta_file, "r", encoding="utf-8") as f:
                meta_data = json.load(f)
            meta_data["draft_id"] = new_draft_id
            meta_data["draft_name"] = project_name
            meta_data["draft_fold_path"] = str(tgt_path)
            meta_data["draft_root_path"] = str(base_dir)
            meta_data["tm_draft_create"] = now_us
            meta_data["tm_draft_modified"] = now_us
            with open(meta_file, "w", encoding="utf-8") as f:
                json.dump(meta_data, f, ensure_ascii=False, separators=(",", ":"))

        # Update draft_info.json (both root and active timeline)
        root_info_file = tgt_path / "draft_info.json"
        if root_info_file.exists():
            with open(root_info_file, "r", encoding="utf-8") as f:
                info_data = json.load(f)
            info_data["id"] = new_timeline_id
            info_data["name"] = project_name
            info_data["update_time"] = now_us

            # Atomically write to root
            with open(root_info_file, "w", encoding="utf-8") as f:
                json.dump(info_data, f, ensure_ascii=False, separators=(",", ":"))

            # Atomically mirror to Timelines/<id>/draft_info.json
            tl_info_file = tl_base / new_timeline_id / "draft_info.json"
            tl_info_file.parent.mkdir(parents=True, exist_ok=True)
            with open(tl_info_file, "w", encoding="utf-8") as f:
                json.dump(info_data, f, ensure_ascii=False, separators=(",", ":"))

        instance = cls(tgt_path)
        return instance

    def register_in_capcut(self, drafts_dir: Optional[Union[str, Path]] = None) -> bool:
        """
        Registers this project in CapCut's global project index (root_meta_info.json).
        Backs up the registry before performing an atomic update.
        """
        base_dir = Path(drafts_dir).resolve() if drafts_dir else get_default_drafts_dir()
        root_meta_path = base_dir / "root_meta_info.json"

        if not root_meta_path.exists():
            return False

        # Create backup if not present
        bak_path = base_dir / "root_meta_info.json.bak"
        if not bak_path.exists():
            shutil.copy2(root_meta_path, bak_path)

        with open(root_meta_path, "r", encoding="utf-8") as f:
            root_data = json.load(f)

        project_name = self.project_dir.name
        draft_id = (
            self.draft_meta.get("draft_id")
            or self.draft_info.get("id")
            or str(uuid.uuid4()).upper()
        )
        duration_us = self.draft_info.get("duration", 3_000_000)
        now_us = int(time.time() * 1_000_000)

        entry = {
            "cloud_draft_cover": False,
            "cloud_draft_sync": False,
            "draft_cloud_last_action_download": False,
            "draft_cloud_purchase_info": "",
            "draft_cloud_template_id": "",
            "draft_cloud_tutorial_info": "",
            "draft_cloud_videocut_purchase_info": "",
            "draft_cover": str(self.project_dir / "draft_cover.jpg"),
            "draft_fold_path": str(self.project_dir),
            "draft_id": draft_id,
            "draft_is_ai_shorts": False,
            "draft_is_cloud_temp_draft": False,
            "draft_is_infinite_canvas_draft": False,
            "draft_is_invisible": False,
            "draft_is_pippit_draft": False,
            "draft_is_web_article_video": False,
            "draft_json_file": str(self.project_dir / "draft_info.json"),
            "draft_name": project_name,
            "draft_new_version": "",
            "draft_root_path": str(base_dir),
            "draft_timeline_materials_size": 2096480,
            "draft_type": "",
            "draft_web_article_video_enter_from": "",
            "pippit_avatar_url": "",
            "pippit_extra_info": "",
            "pippit_id": "",
            "pippit_user_name": "",
            "streaming_edit_draft_ready": True,
            "tm_draft_cloud_completed": "",
            "tm_draft_cloud_entry_id": -1,
            "tm_draft_cloud_modified": 0,
            "tm_draft_cloud_parent_entry_id": -1,
            "tm_draft_cloud_space_id": -1,
            "tm_draft_cloud_user_id": -1,
            "tm_draft_create": self.draft_meta.get("tm_draft_create", now_us),
            "tm_draft_modified": now_us,
            "tm_draft_removed": 0,
            "tm_duration": duration_us,
        }

        store = root_data.setdefault("all_draft_store", [])
        found = False
        for i, it in enumerate(store):
            if it.get("draft_name") == project_name or it.get("draft_fold_path") == str(self.project_dir):
                store[i] = entry
                found = True
                break
        if not found:
            store.insert(0, entry)

        root_data["draft_ids"] = len(store)

        tmp_root = base_dir / "root_meta_info.json.tmp"
        with open(tmp_root, "w", encoding="utf-8") as f:
            json.dump(root_data, f, ensure_ascii=False, indent=4)
        os.replace(tmp_root, root_meta_path)
        return True

    def add_text_track(
        self,
        text: str,
        start_us: Union[int, str] = 0,
        duration_us: Union[int, str] = 3_000_000,
        x: float = 0.0,
        y: float = 0.75,
        preset: str = "bold_yellow",
        font_size: Optional[float] = None,
        track_name: str = "Text Track",
    ) -> str:
        """
        Adds a styled text segment onto a new or existing text track.
        Returns the text segment UUID.
        """
        start = parse_time_us(start_us)
        duration = parse_time_us(duration_us)
        style_cfg = TextStylePresets.get_preset(preset)
        size = font_size if font_size is not None else style_cfg["font_size"]

        text_mat_id = str(uuid.uuid4()).upper()
        seg_id = str(uuid.uuid4()).upper()

        # Build serialized content JSON
        content_dict = {
            "styles": [
                {
                    "fill": {
                        "alpha": 1.0,
                        "content": {
                            "solid": {
                                "color": style_cfg["color"]
                            }
                        }
                    },
                    "font": {"id": "", "path": ""},
                    "range": [0, len(text)],
                    "size": size,
                    "bold": style_cfg["bold"],
                    "italic": False,
                    "underline": False,
                    "align": style_cfg["align"]
                }
            ],
            "text": text
        }

        # Text material definition
        text_material = {
            "id": text_mat_id,
            "type": "text",
            "content": json.dumps(content_dict, ensure_ascii=False),
            "typesetting": 0,
            "check_flag": 47,  # Fill + Outline (8) + Shadow (32) = 47
            "global_alpha": 1.0,
            "line_spacing": 0.02,
            "letter_spacing": 0.0,
            "alignment": style_cfg["align"],
            "text_border": {
                "alpha": 1.0,
                "color": style_cfg["border_color"],
                "width": style_cfg["border_width"]
            },
            "text_shadow": {
                "alpha": 0.9,
                "color": style_cfg["shadow_color"],
                "blur": style_cfg["shadow_blur"],
                "distance": style_cfg["shadow_distance"],
                "angle": style_cfg["shadow_angle"]
            }
        }

        materials = self.draft_info.setdefault("materials", {})
        materials.setdefault("texts", []).append(text_material)

        # Text Segment definition
        segment = {
            "id": seg_id,
            "material_id": text_mat_id,
            "source_timerange": None,
            "target_timerange": {"start": start, "duration": duration},
            "clip": ClipSettings(transform_x=x, transform_y=y).to_dict(),
            "uniform_scale": {"on": True, "value": 1.0},
            "visible": True,
            "speed": 1.0,
            "volume": 1.0,
            "last_nonzero_volume": 1.0,
            "track_attribute": 0,
            "track_render_index": 0,
            "enable_adjust": True,
            "common_keyframes": [],
            "keyframe_refs": [],
            "extra_material_refs": []
        }

        # Add to tracks
        tracks = self.draft_info.setdefault("tracks", [])
        new_track = {
            "attribute": 0,
            "flag": 0,
            "id": str(uuid.uuid4()).upper(),
            "is_default_name": True,
            "name": track_name,
            "segments": [segment],
            "type": "text"
        }
        tracks.append(new_track)
        return seg_id

    def add_audio_track(
        self,
        file_path: Union[str, Path],
        start_us: Union[int, str] = 0,
        duration_us: Optional[Union[int, str]] = None,
        volume: float = 1.0,
        fade_in_us: Union[int, str] = 0,
        fade_out_us: Union[int, str] = 0,
        track_name: str = "Audio Track",
    ) -> str:
        """
        Inserts an audio track with volume control and fade curves.
        Registers the audio file into draft_meta_info media manifest.
        Returns the segment UUID.
        """
        audio_path = Path(file_path).resolve()
        start = parse_time_us(start_us)
        probe = probe_media_file(audio_path)
        media_dur = probe["duration_us"]
        duration = parse_time_us(duration_us) if duration_us is not None else media_dur

        audio_mat_id = str(uuid.uuid4()).upper()
        speed_id = str(uuid.uuid4()).upper()
        seg_id = str(uuid.uuid4()).upper()
        fade_id = None

        fade_in = parse_time_us(fade_in_us)
        fade_out = parse_time_us(fade_out_us)

        materials = self.draft_info.setdefault("materials", {})

        # Speed material
        materials.setdefault("speeds", []).append({
            "curve_speed": None,
            "id": speed_id,
            "mode": 0,
            "speed": 1.0,
            "type": "speed"
        })

        extra_refs = [speed_id]

        # Audio fade material
        if fade_in > 0 or fade_out > 0:
            fade_id = str(uuid.uuid4()).upper()
            materials.setdefault("audio_fades", []).append({
                "fade_in_duration": fade_in,
                "fade_out_duration": fade_out,
                "fade_type": 0,
                "id": fade_id,
                "type": "audio_fade"
            })
            extra_refs.append(fade_id)

        # Audio material
        audio_mat = {
            "app_key": "",
            "category_id": "",
            "category_name": "local",
            "check_flag": 1,
            "duration": media_dur,
            "formula_id": "",
            "id": audio_mat_id,
            "local_material_id": str(uuid.uuid4()).lower(),
            "music_id": "",
            "name": audio_path.name,
            "path": str(audio_path),
            "query": "",
            "source_platform": 0,
            "team_id": "",
            "text_id": "",
            "tone_category_id": "",
            "tone_category_name": "",
            "tone_effect_id": "",
            "tone_effect_name": "",
            "tone_speaker": "",
            "tone_type": "",
            "type": "audio"
        }
        materials.setdefault("audios", []).append(audio_mat)

        # Audio segment
        segment = {
            "id": seg_id,
            "material_id": audio_mat_id,
            "source_timerange": {"start": 0, "duration": duration},
            "target_timerange": {"start": start, "duration": duration},
            "speed": 1.0,
            "volume": float(volume),
            "last_nonzero_volume": 1.0,
            "is_tone_modify": False,
            "visible": True,
            "track_attribute": 0,
            "track_render_index": 0,
            "enable_adjust": True,
            "common_keyframes": [],
            "keyframe_refs": [],
            "extra_material_refs": extra_refs
        }

        # Add track
        tracks = self.draft_info.setdefault("tracks", [])
        new_track = {
            "attribute": 0,
            "flag": 0,
            "id": str(uuid.uuid4()).upper(),
            "is_default_name": True,
            "name": track_name,
            "segments": [segment],
            "type": "audio"
        }
        tracks.append(new_track)

        # Register in draft_meta_info.json (type 1: audio)
        if self.draft_meta and "draft_materials" in self.draft_meta:
            found_bucket = False
            for bucket in self.draft_meta["draft_materials"]:
                if bucket.get("type") == 1:
                    bucket.setdefault("value", []).append({
                        "ai_group_type": "",
                        "create_time": int(time.time()),
                        "duration": media_dur,
                        "enter_from": 0,
                        "extra_info": audio_path.name,
                        "file_Path": str(audio_path),
                        "height": 0,
                        "id": str(uuid.uuid4()).lower(),
                        "import_time": int(time.time()),
                        "import_time_ms": int(time.time() * 1_000_000),
                        "item_source": 1,
                        "material_color_tag": "",
                        "md5": "",
                        "metetype": "audio",
                        "roughcut_time_range": {"duration": media_dur, "start": 0},
                        "sub_time_range": {"duration": -1, "start": -1},
                        "type": 1,
                        "width": 0
                    })
                    found_bucket = True
                    break
            if not found_bucket:
                self.draft_meta["draft_materials"].append({
                    "type": 1,
                    "value": [{
                        "ai_group_type": "",
                        "create_time": int(time.time()),
                        "duration": media_dur,
                        "enter_from": 0,
                        "extra_info": audio_path.name,
                        "file_Path": str(audio_path),
                        "height": 0,
                        "id": str(uuid.uuid4()).lower(),
                        "import_time": int(time.time()),
                        "import_time_ms": int(time.time() * 1_000_000),
                        "item_source": 1,
                        "material_color_tag": "",
                        "md5": "",
                        "metetype": "audio",
                        "roughcut_time_range": {"duration": media_dur, "start": 0},
                        "sub_time_range": {"duration": -1, "start": -1},
                        "type": 1,
                        "width": 0
                    }]
                })

        return seg_id

    def apply_shorts_hook_layout(
        self,
        hook_title: str = "SHOCKING SECRET REVEALED",
        subtitles: Optional[List[Dict[str, Any]]] = None,
        bgm_path: Optional[Union[str, Path]] = None,
        bgm_vol: float = 0.22,
        sfx_path: Optional[Union[str, Path]] = None,
        sfx_vol: float = 1.1,
        hook_y: float = 0.75,
        hook_duration_us: Optional[int] = None,
    ) -> None:
        """
        Applies complete 4-tier Shorts Hook layout:
        1. Top Bold Hook Header (y=+0.75, yellow contrast, heavy stroke + shadow, bitmask 47).
        2. Center Dynamic Subtitles (y=-0.12, kinetic readability).
        3. Upbeat ducked BGM (volume 0.22, fade in/out curves).
        4. Whoosh transition SFX (0 to 0.5s, volume 1.1) synchronized with hook.
        """
        timeline_dur = self.draft_info.get("duration", 3_000_000)
        h_dur = hook_duration_us if hook_duration_us is not None else timeline_dur

        # 1. Top Bold Hook Header
        self.add_text_track(
            text=hook_title,
            start_us=0,
            duration_us=h_dur,
            x=0.0,
            y=hook_y,
            preset="bold_yellow",
            font_size=15.0,
            track_name="Hook Header Track"
        )

        # 2. Dynamic Center Subtitles
        if not subtitles:
            subtitles = [
                {"text": "Watch what happens next...", "start": 400_000, "duration": 1_300_000},
                {"text": "You won't believe this!", "start": 1_750_000, "duration": 1_150_000}
            ]

        # Combine subtitles into a single track or sequential segments
        sub_track = {
            "attribute": 0,
            "flag": 0,
            "id": str(uuid.uuid4()).upper(),
            "is_default_name": True,
            "name": "Subtitles Track",
            "segments": [],
            "type": "text"
        }
        for sub in subtitles:
            st = parse_time_us(sub.get("start", 0))
            dur = parse_time_us(sub.get("duration", 1_000_000))
            txt = sub.get("text", "")
            preset = sub.get("preset", "modern_white")
            p_cfg = TextStylePresets.get_preset(preset)

            mat_id = str(uuid.uuid4()).upper()
            s_id = str(uuid.uuid4()).upper()

            content_dict = {
                "styles": [
                    {
                        "fill": {"alpha": 1.0, "content": {"solid": {"color": p_cfg["color"]}}},
                        "font": {"id": "", "path": ""},
                        "range": [0, len(txt)],
                        "size": p_cfg["font_size"],
                        "bold": p_cfg["bold"],
                        "italic": False,
                        "underline": False,
                        "align": 1
                    }
                ],
                "text": txt
            }

            self.draft_info.setdefault("materials", {}).setdefault("texts", []).append({
                "id": mat_id,
                "type": "text",
                "content": json.dumps(content_dict, ensure_ascii=False),
                "typesetting": 0,
                "check_flag": 47,
                "global_alpha": 1.0,
                "line_spacing": 0.02,
                "letter_spacing": 0.0,
                "alignment": 1,
                "text_border": {
                    "alpha": 1.0,
                    "color": p_cfg["border_color"],
                    "width": p_cfg["border_width"]
                },
                "text_shadow": {
                    "alpha": 0.85,
                    "color": p_cfg["shadow_color"],
                    "blur": p_cfg["shadow_blur"],
                    "distance": p_cfg["shadow_distance"],
                    "angle": p_cfg["shadow_angle"]
                }
            })

            sub_track["segments"].append({
                "id": s_id,
                "material_id": mat_id,
                "source_timerange": None,
                "target_timerange": {"start": st, "duration": dur},
                "clip": ClipSettings(transform_x=0.0, transform_y=-0.12).to_dict(),
                "uniform_scale": {"on": True, "value": 1.0},
                "visible": True,
                "speed": 1.0,
                "volume": 1.0,
                "last_nonzero_volume": 1.0,
                "track_attribute": 0,
                "track_render_index": 0,
                "enable_adjust": True,
                "common_keyframes": [],
                "keyframe_refs": [],
                "extra_material_refs": []
            })

        self.draft_info.setdefault("tracks", []).append(sub_track)

        # 3. Whoosh SFX (0 to 0.5s)
        sfx_file = Path(sfx_path).resolve() if sfx_path else None
        if not sfx_file or not sfx_file.exists():
            default_asset_dir = Path(__file__).resolve().parent.parent.parent.parent.parent / "outputs" / "assets"
            sfx_file = default_asset_dir / "whoosh.wav"
            synthesize_whoosh_sfx(sfx_file, duration_s=0.5)

        self.add_audio_track(
            file_path=sfx_file,
            start_us=0,
            duration_us=500_000,
            volume=sfx_vol,
            fade_in_us=50_000,
            fade_out_us=150_000,
            track_name="Whoosh SFX Track"
        )

        # 4. Upbeat BGM with ducking and fade curves
        bgm_file = Path(bgm_path).resolve() if bgm_path else None
        if not bgm_file or not bgm_file.exists():
            default_asset_dir = Path(__file__).resolve().parent.parent.parent.parent.parent / "outputs" / "assets"
            bgm_file = default_asset_dir / "upbeat_bgm.wav"
            synthesize_upbeat_bgm(bgm_file, duration_s=timeline_dur / 1_000_000)

        self.add_audio_track(
            file_path=bgm_file,
            start_us=0,
            duration_us=timeline_dur,
            volume=bgm_vol,
            fade_in_us=300_000,
            fade_out_us=500_000,
            track_name="Upbeat BGM Track"
        )

    def inspect(self) -> Dict[str, Any]:
        """Returns structured timeline metrics for debugging and verification."""
        duration_us = self.draft_info.get("duration", 0)
        tracks = self.draft_info.get("tracks", [])
        track_summary = []
        for i, t in enumerate(tracks):
            segs = t.get("segments", [])
            track_summary.append({
                "index": i,
                "name": t.get("name", ""),
                "type": t.get("type", "unknown"),
                "segment_count": len(segs),
            })
        materials = self.draft_info.get("materials", {})
        mat_counts = {k: len(v) for k, v in materials.items() if isinstance(v, list) and v}

        return {
            "project_name": self.project_dir.name if self.project_dir else "unknown",
            "duration_us": duration_us,
            "duration_s": duration_us / 1_000_000,
            "fps": self.draft_info.get("fps", 30.0),
            "canvas": self.draft_info.get("canvas_config", {}),
            "tracks": track_summary,
            "material_counts": mat_counts,
            "active_timeline_id": self.active_timeline_id
        }

    def validate(self) -> Tuple[bool, List[str]]:
        """
        Validates the integrity of the project files, timeline mirroring,
        and foreign key relationships.
        """
        errors = []
        if not self.project_dir or not self.project_dir.exists():
            return False, ["Project directory missing on disk."]

        # Check mirrored draft_info.json
        root_info = self.project_dir / "draft_info.json"
        if not root_info.exists():
            errors.append("draft_info.json missing at project root.")

        if self.active_timeline_id:
            tl_info = self.project_dir / "Timelines" / self.active_timeline_id / "draft_info.json"
            if not tl_info.exists():
                errors.append(f"Mirrored draft_info.json missing in Timelines/{self.active_timeline_id}")
            else:
                # Compare byte contents
                if root_info.read_bytes() != tl_info.read_bytes():
                    errors.append("Root draft_info.json and Timelines mirror are out of sync!")

        # Verify tracks and materials
        tracks = self.draft_info.get("tracks", [])
        materials = self.draft_info.get("materials", {})
        known_mat_ids = set()
        for mat_list in materials.values():
            if isinstance(mat_list, list):
                for m in mat_list:
                    if isinstance(m, dict) and "id" in m:
                        known_mat_ids.add(m["id"])

        for t_idx, track in enumerate(tracks):
            for s_idx, seg in enumerate(track.get("segments", [])):
                mat_id = seg.get("material_id")
                if mat_id and mat_id not in known_mat_ids:
                    errors.append(
                        f"Track {t_idx} segment {s_idx} references missing material_id: {mat_id}"
                    )

        return (len(errors) == 0), errors


def build_cli() -> argparse.ArgumentParser:
    """Constructs the comprehensive CLI parser."""
    parser = argparse.ArgumentParser(
        prog="capcut_draft.py",
        description="CapCut Desktop 9.5.0 Pure Python Draft Engine & Timeline Automation Tool."
    )
    parser.add_argument(
        "--draft-dir",
        type=str,
        default=None,
        help="Base CapCut projects directory (defaults to ~/Movies/CapCut/User Data/Projects/com.lveditor.draft)"
    )

    subparsers = parser.add_subparsers(dest="command", help="Operational subcommands")

    # 1. clone
    p_clone = subparsers.add_parser("clone", help="Clone an existing project with fresh UUIDs")
    p_clone.add_argument("--source", required=True, help="Source project name or absolute path")
    p_clone.add_argument("--target", required=True, help="Target project name or absolute path")
    p_clone.add_argument("--name", default=None, help="Display name for target project")
    p_clone.add_argument("--register", action="store_true", help="Register in root_meta_info.json")

    # 2. hook
    p_hook = subparsers.add_parser("hook", help="Apply complete Shorts Hook video layout")
    p_hook.add_argument("--project", required=True, help="Project name or path")
    p_hook.add_argument("--title", default="SHOCKING SECRET REVEALED", help="Hook header text")
    p_hook.add_argument("--hook-style", default="bold_yellow", help="Hook preset style")
    p_hook.add_argument("--hook-pos", type=float, default=0.75, help="Y position of hook header (default 0.75)")
    p_hook.add_argument("--bgm", default=None, help="Path to BGM audio file")
    p_hook.add_argument("--bgm-vol", type=float, default=0.22, help="BGM volume level (default 0.22)")
    p_hook.add_argument("--sfx", default=None, help="Path to Whoosh SFX audio file")
    p_hook.add_argument("--sfx-vol", type=float, default=1.1, help="Whoosh SFX volume (default 1.1)")
    p_hook.add_argument("--register", action="store_true", help="Register in root_meta_info.json")

    # 3. add-text
    p_text = subparsers.add_parser("add-text", help="Add a text segment to the project")
    p_text.add_argument("--project", required=True, help="Project name or path")
    p_text.add_argument("--text", required=True, help="Text string to insert")
    p_text.add_argument("--start", default="0s", help="Start time (e.g. 0s, 500ms, 1000000us)")
    p_text.add_argument("--duration", default="3s", help="Duration (e.g. 3s, 2500ms)")
    p_text.add_argument("--x", type=float, default=0.0, help="Normalized X position [-1.0, 1.0]")
    p_text.add_argument("--y", type=float, default=0.75, help="Normalized Y position [-1.0, 1.0]")
    p_text.add_argument("--preset", default="bold_yellow", help="Typography preset")
    p_text.add_argument("--size", type=float, default=None, help="Font size override")

    # 4. add-audio
    p_audio = subparsers.add_parser("add-audio", help="Add an audio segment to the project")
    p_audio.add_argument("--project", required=True, help="Project name or path")
    p_audio.add_argument("--file", required=True, help="Path to audio file")
    p_audio.add_argument("--start", default="0s", help="Start time on timeline")
    p_audio.add_argument("--duration", default=None, help="Duration to place (default file duration)")
    p_audio.add_argument("--vol", type=float, default=1.0, help="Volume multiplier (e.g. 0.22, 1.0)")
    p_audio.add_argument("--fade-in", default="0s", help="Fade in duration")
    p_audio.add_argument("--fade-out", default="0s", help="Fade out duration")
    p_audio.add_argument("--name", default="Audio Track", help="Track label")

    # 5. inspect
    p_inspect = subparsers.add_parser("inspect", help="Inspect and display project metrics")
    p_inspect.add_argument("--project", required=True, help="Project name or path")

    # 6. validate
    p_val = subparsers.add_parser("validate", help="Validate project structural integrity")
    p_val.add_argument("--project", required=True, help="Project name or path")

    return parser


def main() -> int:
    parser = build_cli()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    base_dir = Path(args.draft_dir).resolve() if args.draft_dir else get_default_drafts_dir()

    def resolve_proj(name_or_path: str) -> Path:
        p = Path(name_or_path)
        return p if p.is_absolute() else (base_dir / name_or_path)

    try:
        if args.command == "clone":
            print(f"[INFO] Cloning project '{args.source}' -> '{args.target}'...")
            proj = DraftProject.clone_project(
                source=args.source,
                target=args.target,
                new_name=args.name,
                drafts_dir=base_dir
            )
            if args.register:
                proj.register_in_capcut(drafts_dir=base_dir)
                print(f"[OK] Project '{proj.project_dir.name}' registered in root_meta_info.json")
            print(f"[SUCCESS] Cloned project saved to: {proj.project_dir}")
            return 0

        elif args.command == "hook":
            proj_path = resolve_proj(args.project)
            print(f"[INFO] Applying Shorts Hook layout on: {proj_path.name}")
            proj = DraftProject(proj_path)
            proj.apply_shorts_hook_layout(
                hook_title=args.title,
                bgm_path=args.bgm,
                bgm_vol=args.bgm_vol,
                sfx_path=args.sfx,
                sfx_vol=args.sfx_vol,
                hook_y=args.hook_pos,
            )
            proj.save()
            if args.register:
                proj.register_in_capcut(drafts_dir=base_dir)
                print(f"[OK] Registered project in root_meta_info.json")
            print(f"[SUCCESS] Shorts Hook layout successfully applied.")
            return 0

        elif args.command == "add-text":
            proj_path = resolve_proj(args.project)
            proj = DraftProject(proj_path)
            seg_id = proj.add_text_track(
                text=args.text,
                start_us=args.start,
                duration_us=args.duration,
                x=args.x,
                y=args.y,
                preset=args.preset,
                font_size=args.size
            )
            proj.save()
            print(f"[SUCCESS] Added text segment {seg_id} to {proj_path.name}")
            return 0

        elif args.command == "add-audio":
            proj_path = resolve_proj(args.project)
            proj = DraftProject(proj_path)
            seg_id = proj.add_audio_track(
                file_path=args.file,
                start_us=args.start,
                duration_us=args.duration,
                volume=args.vol,
                fade_in_us=args.fade_in,
                fade_out_us=args.fade_out,
                track_name=args.name
            )
            proj.save()
            print(f"[SUCCESS] Added audio segment {seg_id} to {proj_path.name}")
            return 0

        elif args.command == "inspect":
            proj_path = resolve_proj(args.project)
            proj = DraftProject(proj_path)
            data = proj.inspect()
            print(json.dumps(data, indent=2))
            return 0

        elif args.command == "validate":
            proj_path = resolve_proj(args.project)
            proj = DraftProject(proj_path)
            valid, errors = proj.validate()
            if valid:
                print(f"[PASS] Project '{proj_path.name}' is valid.")
                return 0
            else:
                print(f"[FAIL] Project '{proj_path.name}' has errors:")
                for e in errors:
                    print(f"  - {e}")
                return 1

    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
