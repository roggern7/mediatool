# CapCut Desktop 9.5.0 Draft JSON Schema Reference

This reference document provides the definitive data model and schema specifications for CapCut Desktop (version 9.5.0+ on macOS/Windows), used by the `capcut-editor` skill and `capcut_draft.py` engine.

---

## 1. Project Filesystem Topology

CapCut Desktop projects reside in the local application workspace:
`~/Movies/CapCut/User Data/Projects/com.lveditor.draft/`

Each project directory contains the following coordinated registries:

```
<project_name>/
├── draft_info.json               # Canonical timeline definition (root)
├── draft_meta_info.json          # Project media manifest & metadata
├── draft_cover.jpg               # Project preview thumbnail
└── Timelines/
    ├── project.json              # Timeline manager indexing active timeline
    └── <TIMELINE_UUID>/
        ├── draft_info.json       # Mirror copy (MUST match root draft_info.json)
        └── draft_cover.jpg
```

---

## 2. Global Project Registry (`root_meta_info.json`)

Located at `~/Movies/CapCut/User Data/Projects/com.lveditor.draft/root_meta_info.json`.
Indexes all drafts rendered on the CapCut home/project launcher screen.

```json
{
  "all_draft_store": [
    {
      "draft_id": "<UUID_UPPERCASE>",
      "draft_name": "<project_name>",
      "draft_fold_path": "<PATH_TO_PROJECT_DIR>",
      "draft_json_file": "<PATH_TO_PROJECT_DIR>/draft_info.json",
      "draft_cover": "<PATH_TO_PROJECT_DIR>/draft_cover.jpg",
      "draft_root_path": "<PATH_TO_DRAFTS_ROOT>",
      "streaming_edit_draft_ready": true,
      "tm_draft_create": 1790253783855584,
      "tm_draft_modified": 1790254109811133,
      "tm_duration": 3000000,
      "cloud_draft_cover": false,
      "cloud_draft_sync": false
    }
  ],
  "draft_ids": 1,
  "root_path": "<PATH_TO_DRAFTS_ROOT>"
}
```

### Key Field Rules:
- `draft_id`: Uppercase UUID string with hyphens (e.g. `1361612F-2749-4241-8814-25FA3BF87224`).
- `tm_draft_create` / `tm_draft_modified`: Timestamps in **microseconds** (`int(time.time() * 1_000_000)`).
- `tm_duration`: Total timeline length in **microseconds** (e.g. `3000000` = 3.0 seconds).
- `streaming_edit_draft_ready`: Must be `true` for immediate editing.

---

## 3. Project Media Manifest (`draft_meta_info.json`)

Registers media items in the project's media bin. If a media item is used in `draft_info.json` but missing from `draft_meta_info.json`, CapCut may display a "Media missing" prompt.

### Material Buckets (`draft_materials`):
- `type: 0`: Video clips
- `type: 1`: Audio files
- `type: 2`: Still images
- `type: 3`: Subtitles / text files

```json
{
  "draft_id": "<UUID_UPPERCASE>",
  "draft_name": "<project_name>",
  "draft_fold_path": "<PATH_TO_PROJECT_DIR>",
  "draft_materials": [
    {
      "type": 0,
      "value": [
        {
          "create_time": 1783602691,
          "duration": 5041000,
          "extra_info": "clip.mp4",
          "file_Path": "<PATH_TO_MEDIA>",
          "height": 1920,
          "width": 1080,
          "id": "<UUID_LOWERCASE>",
          "metetype": "video",
          "roughcut_time_range": {"duration": 5041000, "start": 0},
          "type": 0
        }
      ]
    }
  ],
  "tm_duration": 3000000
}
```

---

## 4. Canonical Timeline (`draft_info.json`)

Defines tracks, segments, timing, and material assets.

### Top-Level Properties:
```json
{
  "id": "<TIMELINE_UUID>",
  "version": 360000,
  "new_version": "187.0.0",
  "name": "<project_name>",
  "duration": 3000000,
  "fps": 30.0,
  "canvas_config": {
    "ratio": "9:16",
    "width": 1080,
    "height": 1920,
    "background": null
  },
  "tracks": [],
  "materials": {
    "videos": [],
    "audios": [],
    "texts": [],
    "speeds": [],
    "audio_fades": [],
    "canvases": []
  }
}
```

### Canvas Aspect Ratios:
- `"9:16"`: $1080 \times 1920$ (YouTube Shorts, TikTok, Instagram Reels)
- `"16:9"`: $1920 \times 1080$ (YouTube Landscape standard)
- `"1:1"`: $1080 \times 1080$ (Square post)
- `"original"`: Matches resolution of the primary video asset

---

## 5. Normalized Cartesian Coordinate System

CapCut centers coordinates on the canvas at `(0.0, 0.0)`:
- `transform.x`: Horizontal offset $[-1.0, 1.0]$.
  - `-1.0` = Left border, `0.0` = Center, `+1.0` = Right border.
- `transform.y`: Vertical offset $[-1.0, 1.0]$.
  - `+1.0` = Top border, `0.0` = Center, `-1.0` = Bottom border.

### Safe Zones for 9:16 Vertical Video:
```
+1.0 |-----------------------------------------| Top Screen Edge
     | [TOP SAFE MARGIN: 0.85 to 1.0]          | Phone status bar, search bar
+0.75| [TOP HOOK HEADER ZONE: 0.70 to 0.80]    | High-impact Hook Text
     |                                         |
 0.0 | [CENTER ZONE: -0.20 to +0.20]           | Focus area & Subtitles
-0.12| [DYNAMIC SUBTITLE ZONE: -0.25 to 0.0]   | Kinetic Speech Subtitles
     |                                         |
-0.65| [BOTTOM SAFE MARGIN: -1.0 to -0.65]     | TikTok/Reels UI, description
-1.0 |-----------------------------------------| Bottom Screen Edge
```

---

## 6. Track & Segment Specifications

### Track Object:
```json
{
  "id": "<TRACK_UUID>",
  "type": "video" | "audio" | "text" | "effect",
  "name": "Track Name",
  "is_default_name": true,
  "flag": 0,
  "attribute": 0,
  "segments": []
}
```

### Segment Target & Source Timeranges:
- `source_timerange`: Sliced window from the source media:
  `{"start": 0, "duration": 3000000}`
- `target_timerange`: Timeline placement coordinates:
  `{"start": 0, "duration": 3000000}`
- Unit: All values are in **microseconds** ($1\text{ s} = 1,000,000\,\mu\text{s}$).

---

## 7. Timeline Mirroring Invariant

In CapCut Desktop 9.5.0:
- `<project>/draft_info.json` AND
- `<project>/Timelines/<TIMELINE_ID>/draft_info.json`

**MUST be strictly synchronized**. Any difference in duration, tracks, or material IDs will cause CapCut to display a conflict resolution dialog or restore an older cached state.

---

## 8. Subtitle Invariants & Voice Volume Synchronization

### 8.1 Monotonic Non-Overlap Clamping
When inserting speech subtitles, segments on the same track must NEVER overlap in target time:
- For every segment $i$:
  $$\text{target\_timerange.start}[i] + \text{target\_timerange.duration}[i] \le \text{target\_timerange.start}[i+1] - 20,000\,\mu\text{s}$$
- Never pad small durations (`min_duration`) past the start of the next segment. An overlap forces CapCut to split segments onto secondary tracks (`Track 2`), causing simultaneous double-text display.

### 8.2 Strict Single Text Track Invariant
All primary dialogue subtitles must reside in exactly **one** text track:
```python
video_tracks = [t for t in draft.get("tracks", []) if t.get("type") == "video"]
draft["tracks"] = video_tracks + [single_subtitle_track]
```

### 8.3 Pause-Aware Silence Clearing
When the speaker stops talking (silence gap $\ge 180\,\text{ms}$):
- The current subtitle must terminate at $\text{word}_{\text{last}}.\text{end} + 50,000\,\mu\text{s}$.
- The canvas remains empty of subtitles during the silence gap.
- Subtitles reappear only when speech resumes.
