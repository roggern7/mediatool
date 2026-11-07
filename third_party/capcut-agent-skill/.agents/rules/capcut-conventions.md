# CapCut Desktop 9.5.0 Engineering Conventions

This document outlines technical conventions, invariants, and operational guidelines for automating CapCut Desktop (9.5.0+) timelines.

---

## 1. Timing Precision & Units

- **Microsecond Invariant**: All time coordinates (`start`, `duration`, `tm_draft_create`, `tm_draft_modified`, `tm_duration`, `fade_in_duration`, `fade_out_duration`) are expressed strictly as **integers in microseconds** ($1\text{ s} = 1,000,000\,\mu\text{s}$).
- **Floating-point time values** in JSON timelines cause parser errors and must never be written.
- **Standard Frame Durations**:
  - $30.0\text{ fps}$: $33,333\,\mu\text{s}$ per frame.
  - $60.0\text{ fps}$: $16,666\,\mu\text{s}$ per frame.

---

## 2. Canvas Ratios & Normalized Coordinates

- **Standard Vertical Canvas**: Width = `1080`, Height = `1920`, Ratio = `"9:16"`.
- **Coordinate Space**:
  - Origin `(0.0, 0.0)` is centered in the canvas viewport.
  - Horizontal Axis `x \in [-1.0, 1.0]`: `-1.0` is left edge, `+1.0` is right edge.
  - Vertical Axis `y \in [-1.0, 1.0]`: `+1.0` is top edge, `-1.0` is bottom edge.
- **Vertical Safe Zones**:
  - Top Hook Header: $y \in [+0.70, +0.80]$ (clears notch and search bar).
  - Center Subtitles: $y \in [-0.25, 0.0]$ (main visual focus).
  - Bottom UI Exclusion: Do not place text at $y < -0.65$ (avoids caption and audio overlays).
  - Horizontal Margin: Keep max line width $\le 0.80$ to clear social interaction buttons on the right margin.

---

## 3. Atomic File Mutations

To prevent draft corruption if execution is interrupted:
1. Always write changes to a temporary file (e.g. `draft_info.json.tmp`).
2. Atomically swap into place using `os.replace()` or `shutil.move()`.
3. Create backup copies (`root_meta_info.json.bak`) prior to modifying shared global registries.

---

## 4. Timeline Synchronization Rule

CapCut Desktop 9.5.0 requires exact byte synchronization between:
- `<project>/draft_info.json`
- `<project>/Timelines/<TIMELINE_ID>/draft_info.json`

Both files must be written simultaneously during every save operation.

---

## 5. Media Registration Rule

Any video or audio asset placed onto timeline tracks must also be registered in `draft_meta_info.json` under `draft_materials` (type `0` for video, type `1` for audio). This ensures CapCut displays the asset in the project media bin without warnings.
