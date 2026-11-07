#!/usr/bin/env python3
"""
Synchronizes Pause-Aware, Syntactically-Aligned ElevenLabs Scribe V2 Subtitles into CapCut Desktop.
Applies:
- Native CapCut Preset Style (check_flag: 15, inline strokes, zero external cache dependency)
- Pure white fill (#ffffff), solid black stroke outline (0.06)
- Monotonic non-overlapping timeline with pause-aware silence clearing
- Bottom safe zone placement (x: 0.0, y: -0.70)
- Single text track invariant
"""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

# Exact 78 pause-aware and syntactically perfected chunks
POLISHED_CHUNKS = [
    # ช่วงที่ 1: Hook เปิดคลิป
    (0.16, 0.60, "และนี่ก็คือ"),
    (0.62, 1.40, "เหตุผลว่า"),
    (1.42, 1.92, "ทำไมคนที่"),
    (1.94, 2.64, "ใช้ macOS"),
    (2.78, 3.16, "ถึงไม่ควร"),
    (3.18, 3.76, "เลื่อนผ่านคลิปนี้"),
    (3.78, 4.10, "นะครับ"),
    # ช่วงที่ 2: เริ่มเข้าสู่หน้าจอ Mac
    (4.32, 5.48, "ซึ่งนั่นก็เพราะว่า"),
    (5.50, 6.44, "ตัว Gemini ตอนนี้"),
    (6.46, 6.90, "นะครับก็"),
    (6.92, 7.46, "คือจะมี"),
    (7.48, 8.28, "ฟีเจอร์ที่ชื่อว่า"),
    (8.30, 9.10, "Speak to Window"),
    (9.12, 9.34, "ครับ"),
    # คำว่า "ซึ่ง" ขึ้นเดี่ยวๆ ตามจังหวะพูด
    (9.78, 10.10, "ซึ่ง"),
    (10.82, 11.86, "เราสามารถใช้"),
    (11.98, 12.90, "Gemini ช่วยเรา"),
    (12.92, 13.46, "ในการถอด"),
    (13.48, 14.04, "เสียงได้"),
    (14.38, 14.94, "และเราก็"),
    (14.96, 15.58, "สามารถพูด"),
    (15.60, 15.96, "กับแอปไหน"),
    (15.98, 16.46, "ก็ได้ครับ"),
    # ช่วงที่ 3: อธิบายปุ่มคีย์ลัด Fn
    (17.24, 17.92, "นี่ครับก็"),
    (17.94, 18.54, "คือวิธีการก็"),
    (18.56, 18.94, "คือเราจะ"),
    (18.96, 19.78, "ทำการกด"),
    (20.28, 20.98, "Hold ตัว"),
    (21.14, 21.90, "Fn หรือไม่ก็"),
    (21.92, 23.16, "เป็นการ Double-tap ตัว"),
    (23.24, 23.80, "Fn นะครับ"),
    # ช่วงที่ 4: เดโมสั่งงาน Claude & Notion MCP
    (24.32, 24.72, "ซึ่งเดี๋ยวผม"),
    (24.74, 25.32, "จะลองเทสต์"),
    (25.34, 26.38, "กับตัว AI"),
    (26.80, 27.40, "บน Browser ครับ"),
    (27.42, 28.46, "ที่ชื่อว่า Claude"),
    (29.64, 30.60, "สมมุติผมสั่ง"),
    (30.62, 31.26, "Claude ว่า"),
    (31.30, 31.82, "อ่าให้คุณ"),
    (31.84, 33.50, "เข้าไปดูตัว Notion"),
    (34.76, 35.50, "MCP หน่อยครับ"),
    (35.90, 36.62, "ตัวเพจที่"),
    (36.64, 38.00, "ชื่อว่า Learning Hub"),
    (38.52, 39.58, "มีข้อมูลข่าวสารอะไร"),
    (39.60, 40.36, "เข้ามาใหม่บ้าง"),
    # ช่วงที่ 5: ผลการ Transcribe & แกล้งพูดผิด
    (42.64, 42.98, "นี่ครับ"),
    (43.34, 44.36, "ตัว Gemini "),
    (44.38, 44.74, "ก็ช่วยเราใน"),
    (44.76, 45.86, "การ Transcribe ตัว"),
    (45.88, 46.66, "เสียงพูดนะครับ"),
    (47.90, 48.58, "ซึ่งเดี๋ยวตรงนี้"),
    (48.60, 48.94, "ผมจะลอง"),
    (49.46, 49.96, "ผมอยากให้"),
    (49.98, 50.82, "คุณเข้าไปเช็ก"),
    (50.84, 51.60, "ตัวเพจที่"),
    (51.62, 52.20, "ชื่อว่า"),
    (52.54, 53.28, "Leaning Hub"),
    (54.82, 55.24, "เอ้ยเดี๋ยวผม"),
    (55.26, 55.78, "พูดใหม่ดีกว่า"),
    (55.96, 56.26, "เป็น"),
    (56.64, 57.32, "Learning Hub"),
    # ช่วงที่ 6: สรุปเรื่อง Context
    (60.26, 60.64, "นี่ครับ"),
    (60.88, 61.26, "เห็นไหมครับ"),
    (61.28, 62.20, "ว่าตัว Gemini"),
    (62.22, 63.00, "เนี่ยสามารถเข้าใจ"),
    (63.02, 63.96, "ได้ว่า"),
    (63.98, 64.28, "ไอสิ่งที่"),
    (64.30, 64.82, "เราพูดอ่ะ"),
    (64.84, 65.48, "มันพูดผิดตรงไหน"),
    (65.70, 66.92, "และ Context ประโยค"),
    (66.94, 67.76, "ต้องการสื่ออะไร"),
    (68.66, 69.22, "นี่ครับผม"),
    (69.66, 69.98, "สำหรับคนที่"),
    (70.00, 70.50, "ใช้ Mac ก็"),
    (70.52, 70.82, "อย่าลืมไป"),
    (70.84, 71.16, "ใช้กันนะ"),
    (71.18, 72.20, "ครับดาวน์โหลดได้ที่"),
    (72.22, 73.20, "ตัว Gemini App"),
]


def create_native_text_material(text_id: str, text: str) -> dict:
    """Builds native CapCut text material (check_flag: 15, strokes[], zero external cache)."""
    content_obj = {
        "styles": [
            {
                "bold": True,
                "fill": {
                    "alpha": 1.0,
                    "content": {
                        "render_type": "solid",
                        "solid": {"alpha": 1.0, "color": [1.0, 1.0, 1.0]}
                    }
                },
                "range": [0, len(text)],
                "size": 7,
                "strokes": [
                    {
                        "alpha": 1.0,
                        "content": {
                            "render_type": "solid",
                            "solid": {"alpha": 1.0, "color": [0.0, 0.0, 0.0]}
                        },
                        "width": 0.05999999865889549
                    }
                ],
                "useLetterColor": True
            }
        ],
        "text": text
    }

    rich_text_command = {
        "iOpCode": 67,
        "iParam1": 0.0,
        "iParam2": 0.0,
        "iParam3": 0.0,
        "iParam4": 0.0,
        "iParam5": (
            "{\"bloomColorCustomized\":false,\"bloomDirX\":0.0,\"bloomDirY\":0.0,"
            "\"bloomPath\":\"\",\"bloomRange\":1.0,\"bloomStrength\":0.0,\"canvas\":false,"
            "\"richText\":\"{\\\"styles\\\":[{\\\"fill\\\":{\\\"alpha\\\":1.0,\\\"content\\\":"
            "{\\\"render_type\\\":\\\"solid\\\",\\\"solid\\\":{\\\"alpha\\\":1.0,\\\"color\\\":[1.0,1.0,1.0]}}},"
            "\\\"range\\\":[0,17],\\\"strokes\\\":[{\\\"alpha\\\":1.0,\\\"content\\\":{\\\"render_type\\\":"
            "\\\"solid\\\",\\\"solid\\\":{\\\"alpha\\\":1.0,\\\"color\\\":[0.0,0.0,0.0]}},"
            "\\\"width\\\":0.05999999865889549}]}],\\\"text\\\":\\\"TEXT_PRESET_STYLE\\\"}\"}"
        ),
        "id": str(uuid.uuid4()).upper()
    }

    return {
        "add_type": 0,
        "alignment": 1,
        "autoAdaptCanvasEnabled": False,
        "background_alpha": 1.0,
        "background_color": "#000000",
        "background_fill": "",
        "background_height": 0.14,
        "background_horizontal_offset": 0.0,
        "background_round_radius": 0.0,
        "background_style": 0,
        "background_vertical_offset": 0.0,
        "background_width": 0.14,
        "base_content": "",
        "bold_width": 0.0,
        "border_alpha": 1.0,
        "border_color": "#000000",
        "border_mode": 0,
        "border_width": 0.06,
        "check_flag": 15,
        "content": json.dumps(content_obj, ensure_ascii=False),
        "cutoff_postfix": "",
        "enable_path_typesetting": False,
        "fixed_height": -1.0,
        "fixed_width": -1.0,
        "font_category_id": "",
        "font_category_name": "",
        "font_id": "",
        "font_name": "",
        "font_path": "/Applications/CapCut.app/Contents/Resources/Font/SystemFont/en.ttf",
        "font_resource_id": "",
        "font_size": 15.0,
        "font_source_platform": 0,
        "font_team_id": "",
        "font_third_resource_id": "",
        "font_title": "none",
        "font_url": "",
        "fonts": [],
        "force_apply_line_max_width": False,
        "global_alpha": 1.0,
        "group_id": "",
        "has_shadow": False,
        "id": text_id,
        "initial_scale": 0.0,
        "inner_padding": -1.0,
        "is_batch_replace": False,
        "is_blank_text": False,
        "is_rich_text": False,
        "is_words_linear": False,
        "italic_degree": 0,
        "ktv_color": "",
        "language": "",
        "layer_weight": 0,
        "letter_spacing": 0.0,
        "line_feed": 1,
        "line_max_width": 0.82,
        "line_spacing": 0.02,
        "lyric_group_id": "",
        "multi_language_current": 0,
        "name": "",
        "offset_on_path": 0.0,
        "oneline_cutoff": False,
        "operation_type": 0,
        "original_size": [],
        "preset_category": "",
        "preset_category_id": "",
        "preset_has_set_alignment": False,
        "preset_id": "",
        "preset_index": 0,
        "preset_name": "",
        "punc_model": "",
        "recognize_model": "",
        "recognize_task_id": "",
        "recognize_text": "",
        "recognize_type": 0,
        "relevance_segment": [],
        "rich_text_command": rich_text_command,
        "shadow_alpha": 0.8,
        "shadow_angle": 0.0,
        "shadow_color": "",
        "shadow_distance": 0.0,
        "shadow_smoothing": 0.0,
        "shadow_thickness_projection_angle": 0.0,
        "shadow_thickness_projection_distance": 0.0,
        "shadow_thickness_projection_enable": False,
        "shape_clip_x": False,
        "shape_clip_y": False,
        "single_char_bg_alpha": 1.0,
        "single_char_bg_color": "",
        "single_char_bg_enable": False,
        "single_char_bg_height": 0.0,
        "single_char_bg_horizontal_offset": 0.0,
        "single_char_bg_round_radius": 0.3,
        "single_char_bg_vertical_offset": 0.0,
        "single_char_bg_width": 0.0,
        "source_from": "",
        "ssml_content": "",
        "style_name": "",
        "sub_template_id": -1,
        "sub_type": 0,
        "subtitle_keywords": None,
        "subtitle_keywords_config": None,
        "subtitle_template_original_fontsize": 0.0,
        "text": text,
        "text_alpha": 1.0,
        "text_color": "#ffffff",
        "text_curve": None,
        "text_exceeds_path_process_type": 0,
        "text_loop_on_path": False,
        "text_preset_resource_id": "",
        "text_size": 30,
        "text_to_audio_ids": [],
        "text_typesetting_path_index": 0,
        "text_typesetting_paths": None,
        "text_typesetting_paths_file": "",
        "translate_original_text": "",
        "translate_text": "",
        "tts_auto_update": False,
        "type": "text",
        "typesetting": 0,
        "underline": False,
        "underline_offset": 0.22,
        "underline_width": 0.05,
        "use_effect_default_color": False
    }


def main():
    draft_dir = Path.home() / "Movies/CapCut/User Data/Projects/com.lveditor.draft/0924 (1)"
    if not draft_dir.exists():
        print(f"Error: Draft directory not found: {draft_dir}", file=sys.stderr)
        return 1

    # Terminate running CapCut process to prevent memory overwrite
    subprocess.run(["pkill", "-15", "-i", "capcut"], check=False)

    root_draft_file = draft_dir / "draft_info.json"
    with open(root_draft_file, "r", encoding="utf-8") as f:
        draft = json.load(f)

    text_track_id = str(uuid.uuid4()).upper()
    text_track = {
        "attribute": 0,
        "flag": 0,
        "id": text_track_id,
        "is_default_name": True,
        "name": "Subtitles",
        "segments": [],
        "type": "text"
    }

    new_segments = []
    new_texts = []

    for idx, (st, en, txt) in enumerate(POLISHED_CHUNKS):
        st_us = int(round(st * 1_000_000))
        dur_us = int(round((en - st) * 1_000_000))
        text_id = str(uuid.uuid4()).upper()
        seg_id = str(uuid.uuid4()).upper()

        text_mat = create_native_text_material(text_id, txt)
        new_texts.append(text_mat)

        seg = {
            "id": seg_id,
            "material_id": text_id,
            "source_timerange": {"start": 0, "duration": dur_us},
            "target_timerange": {"start": st_us, "duration": dur_us},
            "render_index": 14000 + idx,
            "clip": {
                "alpha": 1.0,
                "flip": {"horizontal": False, "vertical": False},
                "rotation": 0.0,
                "scale": {"x": 1.919363623500053, "y": 1.919363623500053},
                "transform": {"x": 0.0, "y": -0.70}
            },
            "speed": 1.0,
            "visible": True,
            "volume": 1.0,
            "extra_material_refs": [],
            "enable_adjust": True,
            "enable_color_curves": True,
            "enable_color_match": True,
            "enable_color_wheels": True,
            "enable_lut": True,
            "enable_smart_color": True,
            "responsive_layout": {
                "enable": False,
                "horizontal_pos_layout": 0,
                "size_layout": 0,
                "target_follow": "",
                "vertical_pos_layout": 0
            },
            "keyframe_refs": []
        }
        new_segments.append(seg)

    text_track["segments"] = new_segments
    draft["materials"]["texts"] = new_texts
    draft["materials"]["effects"] = []

    # Enforce strictly 1 video track + 1 text track, zero audio tracks
    video_tracks = [t for t in draft.get("tracks", []) if t.get("type") == "video"]
    draft["tracks"] = video_tracks + [text_track]

    # Write root draft_info.json
    with open(root_draft_file, "w", encoding="utf-8") as f:
        json.dump(draft, f, ensure_ascii=False, indent=2)

    # Mirror to active timeline
    timelines_dir = draft_dir / "Timelines"
    if timelines_dir.exists():
        for tl_dir in timelines_dir.iterdir():
            if tl_dir.is_dir():
                tl_file = tl_dir / "draft_info.json"
                with open(tl_file, "w", encoding="utf-8") as f:
                    json.dump(draft, f, ensure_ascii=False, indent=2)
                print(f"[+] Mirrored timeline to: {tl_file}")

    print(f"[+] Successfully injected {len(new_segments)} polished Scribe V2 subtitles into '0924 (1)'")

    # Launch CapCut
    subprocess.run(["open", "-a", "/Applications/CapCut.app"], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
