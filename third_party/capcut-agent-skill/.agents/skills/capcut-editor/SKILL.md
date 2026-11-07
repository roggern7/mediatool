---
name: capcut-editor
description: Autonomous CapCut Desktop (9.5.0) draft manipulation engine for vertical short-form video, dynamic hook styling, audio ducking, and timeline automation.
version: 1.0.0
author: AG Kit Architecture Team
category: multimedia-editing
entry_point: scripts/capcut_draft.py
references:
  - references/draft_schema.md
  - references/text_styles.md
  - references/audio_guide.md
  - references/elevenlabs_scribe.md
requirements:
  python: ">=3.9"
  system_tools:
    - ffmpeg
    - ffprobe
---

# CapCut Editor Skill (`capcut-editor`)

The `capcut-editor` skill enables autonomous agents and developers to programmatically generate, clone, manipulate, and register CapCut Desktop projects (version 9.5.0+) on macOS and Windows without requiring manual GUI interactions.

---

## 1. Capability Overview

- **Project Cloning & Management**: Deep-clones existing user projects, generates fresh uppercase UUIDs for draft and timeline entities, and registers drafts into CapCut's launch picker (`root_meta_info.json`).
- **Shorts Hook Layout Automation**: Applies the high-retention vertical layout featuring a bold top header, center dynamic subtitles, ducked background music, and synchronized whoosh transitions.
- **Microsecond Precision**: Full timeline calculations in microseconds ($1\text{ s} = 1,000,000\,\mu\text{s}$).
- **Timeline Mirroring**: Automatically maintains byte-level synchronization between root `draft_info.json` and mirrored timeline files in `Timelines/<TIMELINE_ID>/draft_info.json`.
- **Pure Standard Library**: Zero external Python package requirements for base draft manipulation. All media probing and asset generation leverage standard library tools and FFmpeg subprocesses.
- **ElevenLabs Scribe V2 Speech-to-Text**: High-precision multilingual transcription engine with domain-biased keyterms.
- **Pause-Aware Voice Sync & Syntactic Chunking**: Speech-volume synchronization that clears text during silence gaps ($\ge 180\,\text{ms}$) and segments by grammatical constituents rather than rigid word counts.
- **Curated Audio Staging & Foley Anticipation**: Background music ducking to 0.14 (-17 dB), sensory SFX anticipation lead (180-200ms before visual pop-ups), and vocal loudness normalization (+40% on screen recordings).
- **Curated SFX Palette & Motion Mapping**: Standardized 5-tier sound design triggers (Whoosh, Pop/Click, RISER_01, Camera Shutter, Glitch) for dynamic zooms, badges, scene cuts, and tension curves.
- **The 3-Second High-Stakes FOMO Hook**: Audience targeting, scroll-neglect phrasing, and curiosity gap formulas placed at sub-face coordinates ($y = -0.38$).

---

## 2. Command Line Interface (CLI)

The entry point is `scripts/capcut_draft.py`. All paths resolve dynamically relative to the user's home directory (`~/Movies/CapCut/User Data/Projects/com.lveditor.draft`).

```bash
# Global help
python3 scripts/capcut_draft.py --help
```

### 2.1 Project Cloning (`clone`)
Clones an existing draft to create a new workspace with fresh identifiers:
```bash
python3 scripts/capcut_draft.py clone \
  --source 0924 \
  --target 0924_demo \
  --name "0924_demo" \
  --register
```

### 2.2 Shorts Hook Application (`hook`)
Assembles the complete 4-tier Shorts Hook layout onto an existing project:
```bash
python3 scripts/capcut_draft.py hook \
  --project 0924_demo \
  --title "THE 3-SECOND SECRET" \
  --hook-style bold_yellow \
  --hook-pos 0.75 \
  --bgm outputs/assets/upbeat_bgm.wav \
  --bgm-vol 0.22 \
  --sfx outputs/assets/whoosh.wav \
  --sfx-vol 1.1 \
  --register
```

### 2.3 Add Text Track (`add-text`)
Inserts a single styled text segment:
```bash
python3 scripts/capcut_draft.py add-text \
  --project 0924_demo \
  --text "SUBSCRIBE FOR PART 2" \
  --start 0s \
  --duration 3s \
  --y 0.75 \
  --preset bold_yellow
```

### 2.4 Add Audio Track (`add-audio`)
Inserts an audio track with volume and fade controls:
```bash
python3 scripts/capcut_draft.py add-audio \
  --project 0924_demo \
  --file outputs/assets/upbeat_bgm.wav \
  --start 0s \
  --duration 3s \
  --vol 0.25 \
  --fade-in 0.3s \
  --fade-out 0.5s \
  --name "BGM"
```

### 2.5 Timeline Inspection (`inspect`)
Outputs structured JSON metrics of tracks, segments, and materials:
```bash
python3 scripts/capcut_draft.py inspect --project 0924_demo
```

### 2.6 Structural Validation (`validate`)
Verifies schema compliance, timeline mirroring, and foreign key integrity:
```bash
python3 scripts/capcut_draft.py validate --project 0924_demo
```

### 2.7 Kinetic Subtitles via ElevenLabs Scribe V2 (`scribe_transcribe.py`)
Extracts audio and generates 2-3 word kinetic subtitles aligned with spoken speech:
```bash
uv run --with fal-client python3 scripts/scribe_transcribe.py \
  /path/to/media.mov \
  --keyterms macOS Gemini Claude Notion \
  --words-per-chunk 3 \
  --output-json outputs/scribe_result.json
```

---

## 3. Python API Integration

Autonomous agents can invoke the engine programmatically:

```python
from pathlib import Path
from scripts.capcut_draft import DraftProject

# Clone and open project
project = DraftProject.clone_project(source="0924", target="0924_demo")

# Apply Shorts Hook Layout
project.apply_shorts_hook_layout(
    hook_title="WATCH THIS FIRST!",
    bgm_vol=0.22,
    sfx_vol=1.1,
    hook_y=0.75
)

# Save changes to disk (synchronizes root and timeline mirror)
project.save()

# Register in CapCut project launcher
project.register_in_capcut()
```

---

## 4. 9:16 Vertical Safe Zones & Layout Rules

CapCut uses a normalized Cartesian coordinate system with center at `(0.0, 0.0)`:
- `transform.x`: `[-1.0, 1.0]`, center at `0.0`.
- `transform.y`: `[-1.0, 1.0]`, center at `0.0`, top at `+1.0`, bottom at `-1.0`.

```
+1.0  |--------------------------------------| Top Canvas Border
      | [TOP SAFE MARGIN: +0.85 to +1.0]     | Search bars, phone notch
+0.75 | [TOP HOOK HEADER: +0.70 to +0.80]    | Bold Yellow Hook Text
      |                                      |
 0.0  | [CANVAS CENTER: -0.30 to +0.30]      | Primary Subject Focus (Keep Free!)
      |                                      |
-0.68 | [DYNAMIC SUBTITLES: -0.65 to -0.72]  | Kinetic Subtitles (User Preferred Safe Zone)
-0.85 | [BOTTOM MARGIN: -0.85 to -1.0]       | Platform UI / audio title
-1.0  |--------------------------------------| Bottom Canvas Border
```

---

## 5. Progressive Disclosure References

For detailed schema structures and domain guides, refer to:
- [Draft JSON Schema Reference](references/draft_schema.md): Complete CapCut 9.5.0 JSON structures, registry definitions, and track models.
- [Text Styles Reference](references/text_styles.md): Typography presets, stroke borders, drop shadows, bitmask check flags, and FOMO hook formulas.
- [Audio Architecture Guide](references/audio_guide.md): Multi-track gain staging, ducking curves, and sensory Foley synchronization.
- [ElevenLabs Scribe V2 Guide](references/elevenlabs_scribe.md): Multilingual speech-to-text schema, keyterms biasing, and kinetic subtitle chunking.
- [Short-Form Video Rules](../../rules/short_form_rules.md): Production invariants for 7-track hierarchy, audio gain staging, hook positioning, and non-overlapping subtitle clamping.

---

## 6. Troubleshooting & Error Recovery

| Symptom | Root Cause | Remediation |
|---|---|---|
| Project missing in CapCut launcher | Unregistered in `root_meta_info.json` | Run `capcut_draft.py hook --project <name> --register` |
| "Project has been modified" prompt | Timeline mirror out of sync | Run `capcut_draft.py validate --project <name>` and `save()` |
| Text styling not visible | Bitmask check flag mismatch | Ensure `check_flag: 47` for text with border + shadow |
| Audio track silent | Segment volume zeroed or missing file | Check `inspect` output to confirm audio file path and volume |
