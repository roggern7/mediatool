# CapCut Agent Skill (`capcut-editor`)

> **Autonomous CapCut Desktop (9.5.0+) Draft Manipulation Engine & AG Kit Modular Skill for Vertical Short-Form Video Automation**  
> *Engineered & maintained by **Greedy Gains** for the AI Agent & Video Creator Community.*

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python: 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/)
[![CapCut Desktop: 9.5.0+](https://img.shields.io/badge/CapCut%20Desktop-9.5.0%2B-black.svg)](https://www.capcut.com/)
[![AG Kit Architecture](https://img.shields.io/badge/AG%20Kit-2026.9.0-purple.svg)](.agents/)
[![ElevenLabs Scribe V2](https://img.shields.io/badge/ElevenLabs-Scribe%20V2-orange.svg)](https://fal.run/fal-ai/elevenlabs/speech-to-text/scribe-v2)
[![Maintainer: Greedy Gains](https://img.shields.io/badge/Maintained%20by-Greedy%20Gains-success.svg)](https://github.com/greedygains)

---

## 💡 Overview

**CapCut Agent Skill (`capcut-editor`)** is a headless, autonomous draft manipulation engine and AI agent skill designed for **CapCut Desktop (9.5.0+)** on macOS and Windows. It enables AI coding agents (Antigravity, Claude Code, Cursor) and automated pipelines to programmatically generate, clone, manipulate, style, and register CapCut video drafts without manual GUI intervention.

Built strictly in conformance with the **AG Kit Modular Architecture**, it pairs a zero-dependency Python draft manipulation engine with **ElevenLabs Scribe V2** multilingual speech-to-text, pause-aware kinetic subtitles, automated audio ducking, and battle-tested vertical short-form retention blueprints.

---

## ✨ Key Features

### 🎬 1. Pure Python CapCut Draft Engine (`capcut_draft.py`)
- **Zero Third-Party Library Overhead**: Base timeline manipulation uses strictly the Python standard library. Media probing and asset synthesis leverage standard FFmpeg subprocesses.
- **Microsecond Precision**: All timeline coordinates, durations, and keyframes are computed and formatted strictly in microseconds ($1\text{ s} = 1,000,000\,\mu\text{s}$).
- **Timeline Mirroring Invariant**: Automatically maintains 100% byte-level synchronization between root `draft_info.json` and mirrored timeline files in `Timelines/<TIMELINE_ID>/draft_info.json`, eliminating CapCut "conflict resolution" dialogs.
- **Global Project Registration**: Directly registers cloned or mutated drafts into CapCut's desktop launcher index (`root_meta_info.json`) with safe atomic replacement.

### 🎙️ 2. ElevenLabs Scribe V2 Kinetic Subtitles (`scribe_transcribe.py`)
- **Blazingly Fast Speech-to-Text**: Powered by fal.ai (`fal-ai/elevenlabs/speech-to-text/scribe-v2`) with custom domain keyterms biasing (e.g. `macOS`, `Gemini`, `Notion`, `Claude`, `MCP`).
- **Pause-Aware Silence Clearing**: Automatically clears subtitle text off-screen during natural pauses and breath gaps ($\ge 180\,\text{ms}$). Subtitles never linger awkwardly across silent dead air.
- **Monotonic Non-Overlap Clamping**: Enforces $\text{start}_{i+1} - \text{end}_i \ge 20\,\text{ms}$, preventing CapCut from bumping overlapping text onto unwanted secondary tracks.
- **Cognitive Constituent Chunking**: Segments speech according to 5 linguistic rules (prepositional binding, subject-predicate separation, idiomatic linking, subordinate clause breakdown, technical term precision) instead of mechanical token counting.

### 🎨 3. Native Zero-Dependency Typography Preset (`check_flag: 15`)
- **Native Pure White Outline Preset**: Pure white letterforms (`#ffffff`) with high-contrast black outline (`#000000`, stroke width `0.06`).
- **No External Template Cache**: Avoids referencing volatile local cache folders (such as `Cache/effect/...`), ensuring projects open cleanly on any machine without missing material warnings.
- **Vertical Lower-Third Safe Zone**: Subtitles stay anchored at $x = 0.0, y = -0.70$ with normalized width $\le 0.80$ to clear social media interaction overlays.

### 🎚️ 4. Standard 7-Track Hierarchy & Audio Staging
- **Track 0**: Main Footage Base (talking head + desktop screen recording sequential cuts)
- **Track 1**: Single Kinetic Dialogue Subtitles (pause-aware, non-overlapping)
- **Track 2**: Opening Hook Card ($0.0\text{s} - 3.0\text{s}$, sub-face $y = -0.38$)
- **Track 3**: Contextual Badges / Logo Overlays (e.g. Gemini Spark Logo scaled $18.7\%$)
- **Track 4**: Background Music (BGM ducked to `volume: 0.14` / -17 dB)
- **Track 5**: Foley Sound Effects (SFX with $180-200\,\text{ms}$ Anticipation Lead)
- **Track 6**: Primary Voice Dialogue (screen recording audio boosted to `volume: 1.40` for loudness equalization)

### 🔊 5. Motion-Sound Effect Mapping Taxonomy (SFX Palette)
Standardized motion-to-sound triggers for high-engagement tech tutorials:
- **Zoom Out / Wide Pan** $\rightarrow$ `Whoosh` (`volume: 0.90 - 1.10`)
- **Graphic / Text Pop-up** $\rightarrow$ `Pop!`, `Click`, or `Camera Shutter` (with $180 - 200\,\text{ms}$ Anticipation Lead)
- **Anticipation & Suspense** $\rightarrow$ `RISER_01` (`volume: 0.70 - 0.90`)
- **Scene / Chapter Transition** $\rightarrow$ `Camera Shutter` (`volume: 1.00`)
- **Tech Edgy / Glitch Moment** $\rightarrow$ `Glitch` (`volume: 0.85 - 1.00`)

### ⚡ 6. The 3-Second High-Stakes FOMO Hook Formula
- Formula: $\text{Hook} = \mathbf{[Audience\ Identifier]} + \mathbf{[Action/Scroll\ Neglect]} + \mathbf{[High-Stakes\ Loss/FOMO]}$
- Example: *"และนี่ก็คือ เหตุผลว่า ทำไมคนที่ ใช้ macOS ถึงไม่ควร เลื่อนผ่านคลิปนี้ นะครับ"*
- Timing & Layout: $0.00\text{s} - 3.00\text{s}$ at Sub-Face coordinates ($x = 0.0, y = -0.38$) under chin safe zone.

---

## 🏗️ AG Kit Architecture & Directory Layout

```text
.
├── .agents/                               # AG Kit Agent Core
│   ├── manifest.json                     # Canonical asset & SHA-256 registry
│   ├── manifest.lock.json                # Deterministic lockfile
│   ├── agent/                            # Specialist Agent Personas
│   │   ├── orchestrator.md               # Pipeline supervisor persona
│   │   └── video-editor.md               # CapCut Desktop specialist persona
│   ├── rules/                            # Workspace Behavioral Standards
│   │   ├── 8-ironclad-rules.md           # AG Kit architectural guardrails
│   │   ├── capcut-conventions.md         # Microsecond timing & coordinate conventions
│   │   └── short_form_rules.md           # 7-track hierarchy & audio/subtitle invariants
│   ├── scripts/                          # Toolkit Verification Scripts
│   │   ├── generate_manifest.py          # Manifest generator & hasher
│   │   └── validate_kit.py               # Pre-flight deterministic kit validator
│   ├── skills/                           # Modular Capabilities
│   │   └── capcut-editor/                # CapCut Editor Engine Skill
│   │       ├── SKILL.md                  # Skill entry point (< 500 lines)
│   │       ├── .env.example              # API key template
│   │       ├── scripts/                  # Executable engine scripts
│   │       │   ├── capcut_draft.py       # Main timeline manipulation engine
│   │       │   ├── scribe_transcribe.py  # ElevenLabs Scribe V2 transcriber
│   │       │   └── update_scribe_subtitles.py # Subtitle injector & synchronizer
│   │       └── references/               # Progressive disclosure domain docs
│   │           ├── draft_schema.md       # CapCut 9.5.0 JSON schema & registries
│   │           ├── text_styles.md        # Typography presets & check flags
│   │           ├── audio_guide.md        # Gain staging, ducking & SFX palette
│   │           └── elevenlabs_scribe.md  # Scribe V2 API schema & keyterms
│   └── workflows/                        # Slash Commands
│       └── capcut.md                     # /capcut IDE slash command workflow
├── .env.example                          # Root environment template
├── .gitignore                            # Comprehensive security & asset filter
├── LICENSE                               # MIT Open Source License
└── README.md                             # Repository Documentation
```

---

## 🚀 Quick Start

### 📋 Prerequisites
1. **Python 3.9+**
2. **FFmpeg & FFprobe** installed and accessible on your system `$PATH`:
   ```bash
   # macOS via Homebrew
   brew install ffmpeg
   ```
3. **CapCut Desktop (9.5.0+)** installed on macOS or Windows.
4. *(Optional for Speech-to-Text)*: A free or paid [fal.ai](https://fal.ai) API key.

### 🔑 Environment Setup
Copy `.env.example` to `.env` and insert your Fal.ai key:
```bash
cp .env.example .env
export FAL_KEY="your_fal_key_here"
```

---

## 💻 CLI Usage

All paths resolve dynamically to CapCut Desktop's default project directory (`~/Movies/CapCut/User Data/Projects/com.lveditor.draft`).

```bash
# Display global help
python3 .agents/skills/capcut-editor/scripts/capcut_draft.py --help
```

### 1. Deep Clone an Existing Project
Clones an existing draft to create a fresh workspace with new UUIDs and registers it into CapCut:
```bash
python3 .agents/skills/capcut-editor/scripts/capcut_draft.py clone \
  --source "MyProject" \
  --target "MyProject_v2" \
  --name "My Edited Video" \
  --register
```

### 2. Apply Shorts Hook Layout
Injects top bold hook header, ducked background music, and synchronized whoosh transitions:
```bash
python3 .agents/skills/capcut-editor/scripts/capcut_draft.py hook \
  --project "MyProject_v2" \
  --title "THE 3-SECOND SECRET" \
  --hook-style bold_yellow \
  --hook-pos 0.75 \
  --bgm path/to/bgm.wav \
  --bgm-vol 0.14 \
  --sfx path/to/whoosh.wav \
  --sfx-vol 1.0 \
  --register
```

### 3. Insert Styled Text
```bash
python3 .agents/skills/capcut-editor/scripts/capcut_draft.py add-text \
  --project "MyProject_v2" \
  --text "DON'T MISS THIS" \
  --start 0s \
  --duration 3s \
  --y -0.38 \
  --preset bold_yellow
```

### 4. Insert Audio with Volume & Fade Curves
```bash
python3 .agents/skills/capcut-editor/scripts/capcut_draft.py add-audio \
  --project "MyProject_v2" \
  --file path/to/sunset_chill.mp3 \
  --start 0s \
  --vol 0.14 \
  --fade-in 0.3s \
  --fade-out 0.5s \
  --name "BGM"
```

### 5. Inspect Timeline Structure
Outputs structured JSON metrics of tracks, segments, canvas ratios, and materials:
```bash
python3 .agents/skills/capcut-editor/scripts/capcut_draft.py inspect --project "MyProject_v2"
```

### 6. Validate Project Structural Integrity
Verifies timeline mirroring, material foreign keys, and file completeness:
```bash
python3 .agents/skills/capcut-editor/scripts/capcut_draft.py validate --project "MyProject_v2"
```

### 7. Transcribe Media via ElevenLabs Scribe V2
```bash
uv run --with fal-client python3 .agents/skills/capcut-editor/scripts/scribe_transcribe.py \
  path/to/media.mov \
  --keyterms macOS Gemini Claude Notion \
  --words-per-chunk 3 \
  --output-json outputs/scribe_result.json
```

---

## 🐍 Programmatic Python API

Autonomous agents can invoke `DraftProject` directly:

```python
from pathlib import Path
from .agents.skills.capcut_editor.scripts.capcut_draft import DraftProject

# 1. Open or clone project
project = DraftProject.clone_project(
    source="SourceDraft",
    target="AutomatedDraft",
    project_name="Automated Tutorial"
)

# 2. Add text track
project.add_text_track(
    text="WATCH THIS FIRST",
    start_us=0,
    duration_us=3_000_000,
    y=-0.38,
    preset="bold_yellow"
)

# 3. Add ducked background audio
project.add_audio_track(
    file_path=Path("assets/audio/bgm.mp3"),
    start_us=0,
    duration_us=73_400_000,
    volume=0.14,
    track_name="Ducked BGM"
)

# 4. Save and synchronize timeline mirror
project.save()

# 5. Register in CapCut project picker
project.register_in_capcut()
```

---

## 🛠️ Verification & Toolkit Integrity

Ensure structural compliance with AG Kit standards:

```bash
# Generate manifest and compute SHA-256 hashes
python3 .agents/scripts/generate_manifest.py

# Run pre-flight deterministic validator
python3 .agents/scripts/validate_kit.py
# Output: [PASS] Toolkit is structurally valid.
```

---

## 🔒 Security & Credential Quarantine

This repository adheres to strict open-source security standards:
- **No Hardcoded API Keys or Secrets**: All API tokens are resolved dynamically from system environment variables or local uncommitted `.env` files.
- **Zero Local User Path Leakage**: No hardcoded `/Users/` or absolute system paths. All directory resolutions leverage `Path.home()` or dynamic relative paths.
- **Strict `.gitignore`**: Automatically filters out media files (`.mov`, `.mp4`, `.wav`), renders, outputs, and credentials.

---

## 📄 License & Attribution

Distributed under the **MIT License**. See [`LICENSE`](LICENSE) for details.

Developed with ❤️ by **[Greedy Gains](https://github.com/greedygains)**.
