---
description: End-to-end CapCut video editing and Shorts Hook styling workflow.
slash_command: /capcut
requires_agents:
  - video-editor
requires_skills:
  - capcut-editor
artifact_outputs:
  - outputs/renders/
  - outputs/test/
parameters:
  source_project:
    type: string
    description: Name or path of source CapCut draft to clone from
    required: false
    default: "0924"
  target_project:
    type: string
    description: Target draft project name to create or update
    required: true
    default: "0924_demo"
  hook_title:
    type: string
    description: Main bold hook text displayed at top of vertical video
    required: false
    default: "WATCH THIS FIRST!"
  bgm_preset:
    type: string
    description: Background music preset or audio file path
    required: false
    default: "upbeat_electronic"
  auto_launch:
    type: boolean
    description: Whether to launch CapCut Desktop upon completion
    required: false
    default: false
---

# CapCut Automated Editing Workflow (`/capcut`)

This workflow coordinates the creation, customization, and validation of vertical short-form video projects in CapCut Desktop 9.5.0+.

---

## Workflow Playbook

### Phase 1: Environment Pre-Flight
1. Verify CapCut projects directory exists at `~/Movies/CapCut/User Data/Projects/com.lveditor.draft`.
2. Confirm `ffmpeg` and `ffprobe` are present on the system `PATH`.
3. Check that the source project (default `0924`) exists.

### Phase 2: Ingestion & Project Cloning
Clone the base project to isolate experimental edits and avoid corrupting original user assets:
```bash
python3 skills/capcut-editor/scripts/capcut_draft.py clone \
  --source 0924 \
  --target 0924_demo \
  --name "0924_demo" \
  --register
```

### Phase 3: Shorts Hook Layout Assembly
Inject high-impact visual and auditory layers:
```bash
python3 skills/capcut-editor/scripts/capcut_draft.py hook \
  --project 0924_demo \
  --title "WATCH THIS FIRST!" \
  --hook-style bold_yellow \
  --hook-pos 0.75 \
  --bgm-vol 0.22 \
  --sfx-vol 1.1 \
  --register
```

### Phase 4: Kinetic Subtitle Generation (ElevenLabs Scribe V2)
Transcribe speech with exact word-level boundaries and assemble 2-3 word bottom subtitles:
```bash
# Verify credentials safely
grep -sq "^FAL_KEY=" ~/.env

# Transcribe and output kinetic chunks
uv run --with fal-client python3 skills/capcut-editor/scripts/scribe_transcribe.py \
  /path/to/media.mov \
  --keyterms macOS Gemini Claude Notion \
  --words-per-chunk 3 \
  --output-json outputs/scribe_result.json
```

### Phase 5: Structural Verification
Confirm that draft timelines and manifests are intact:
```bash
python3 skills/capcut-editor/scripts/capcut_draft.py validate --project 0924_demo
python3 skills/capcut-editor/scripts/capcut_draft.py inspect --project 0924_demo
```

### Phase 6: Handoff & Preview
1. Document results in `outputs/test/` or task summary.
2. Prompt user to open CapCut Desktop to inspect the newly styled project.
