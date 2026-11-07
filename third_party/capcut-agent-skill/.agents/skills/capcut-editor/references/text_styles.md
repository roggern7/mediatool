# CapCut Text Styles & Typography Reference

This reference outlines typography specifications, stroke styling, shadow parameters, and JSON payloads for text elements in CapCut Desktop 9.5.0.

---

## 1. Text Material Architecture

Text elements in CapCut store their style definitions in `materials.texts[]`. The text and character formatting are serialized as a nested JSON string in the `content` field.

```json
{
  "id": "<TEXT_MATERIAL_UUID>",
  "type": "text",
  "content": "{\"styles\":[{\"fill\":{\"alpha\":1.0,\"content\":{\"solid\":{\"color\":[1.0,0.9,0.0]}}},\"font\":{\"id\":\"\",\"path\":\"\"},\"range\":[0,24],\"size\":15.0,\"bold\":true,\"italic\":false,\"underline\":false,\"align\":1}],\"text\":\"SHOCKING SECRET REVEALED\"}",
  "typesetting": 0,
  "check_flag": 47,
  "global_alpha": 1.0,
  "line_spacing": 0.02,
  "letter_spacing": 0.0,
  "alignment": 1,
  "text_border": {
    "alpha": 1.0,
    "color": [0.0, 0.0, 0.0],
    "width": 0.08
  },
  "text_shadow": {
    "alpha": 0.9,
    "color": [0.0, 0.0, 0.0],
    "blur": 0.3,
    "distance": 0.15,
    "angle": -45.0
  }
}
```

---

## 2. Check Flag Bitmask Reference

The `check_flag` integer determines which rendering passes are active:
- Base Fill: `7` (`1 | 2 | 4`)
- Stroke / Outline: `+8`
- Background Plate: `+16`
- Drop Shadow: `+32`

**Standard Combinations:**
- Base Only: `7`
- Outline Only: `7 | 8 = 15`
- Shadow Only: `7 | 32 = 39`
- **Stroke + Shadow (High-Contrast Broadcast Hook)**: `7 | 8 | 32 = 47`

---

## 3. Style Presets for Shorts Hook Video

### Preset 1: `bold_yellow` (Top Hook Header)
Designed for immediate scroller stoppage in the top safe zone ($y = 0.70$ to $0.80$).
- **Color**: Neon Yellow `[1.0, 0.90, 0.0]`
- **Font Size**: `15.0`
- **Weight**: Bold `true`
- **Border / Stroke**: Pure Black `[0.0, 0.0, 0.0]`, width `0.08`
- **Drop Shadow**: Black `[0.0, 0.0, 0.0]`, blur `0.30`, distance `0.15`, angle `-45.0`
- **Check Flag**: `47`
- **Alignment**: Center (`1`)

### Preset 2: `modern_white` (Kinetic Subtitles)
Optimized for rapid dialogue readability in the center zone ($y = -0.20$ to $0.0$).
- **Color**: Pure White `[1.0, 1.0, 1.0]`
- **Font Size**: `10.5`
- **Weight**: Bold `true`
- **Border / Stroke**: Black `[0.0, 0.0, 0.0]`, width `0.06`
- **Drop Shadow**: Black `[0.0, 0.0, 0.0]`, blur `0.20`, distance `0.10`, angle `-45.0`
- **Check Flag**: `47`
- **Alignment**: Center (`1`)

### Preset 3: `neon_cyan` (Emphasis Words)
High-tech accent for call-to-actions and highlighted keywords.
- **Color**: Electric Cyan `[0.0, 0.90, 1.0]`
- **Font Size**: `14.0`
- **Weight**: Bold `true`
- **Border / Stroke**: Deep Black `[0.0, 0.0, 0.0]`, width `0.07`
- **Drop Shadow**: Black `[0.0, 0.0, 0.0]`, blur `0.25`, distance `0.12`, angle `-45.0`
- **Check Flag**: `47`

### Preset 4: `punchy_red` (Urgency / Warning)
Breaking news or alert announcements.
- **Color**: Bright Red `[1.0, 0.20, 0.20]`
- **Font Size**: `15.0`
- **Weight**: Bold `true`
- **Border / Stroke**: White Outline `[1.0, 1.0, 1.0]`, width `0.06`
- **Drop Shadow**: Black `[0.0, 0.0, 0.0]`, blur `0.30`, distance `0.15`, angle `-45.0`
- **Check Flag**: `47`

### Preset 5: `user_native_white_outline` (Default User Preset - Bottom Kinetic Subtitles)
Extracted and verified from user project `0924 (1)`. Designed for maximum legibility in vertical short-form video with zero external cache dependencies.
- **Visual Style**: Solid Pure White letterforms (`#ffffff`) with crisp, high-contrast Black outline (`#000000`).
- **CapCut Check Flag**: `15` (`7` base fill + `8` outline).
- **Inline Stroke**: `width: 0.06`, solid black `[0.0, 0.0, 0.0]`, alpha `1.0`.
- **Fill**: Solid pure white `[1.0, 1.0, 1.0]`, alpha `1.0`.
- **Color Overrides**: `useLetterColor: true`, `text_color: "#ffffff"`, `use_effect_default_color: false` (prevents external template tinting).
- **Rich Text Command**: OpCode 67 `TEXT_PRESET_STYLE` embedded in text material.
- **Border Parameters**: `border_color: "#000000"`, `border_width: 0.06`, `border_alpha: 1.0`, `border_mode: 0`.
- **Font**: System San Francisco / Roboto (`/Applications/CapCut.app/Contents/Resources/Font/SystemFont/en.ttf`), `font_size: 15.0`, `styles[0].size: 7`.
- **Segment Scale**: `1.919x` (192% scale zoom on X and Y axes).
- **Segment Position**: Bottom lower-third safe zone: `x: 0.0`, `y: -0.70` (never center `y: 0.0`).
- **Dependencies**: **Zero external cache path dependency**. Renders natively on any CapCut Desktop installation.

---

## 4. Typography Layout Constraints

1. **Max Line Width**: All text elements in 9:16 vertical video should not exceed $0.80$ normalized width to clear social platform UI overlays on the right margin.
2. **Line Spacing**: Set `line_spacing: 0.02` for tight, cohesive display headers.
3. **Vertical Safe Positioning**:
   - Top Header Zone: $y = +0.70$ to $+0.80$
   - Bottom Subtitle Zone: $y = -0.65$ to $-0.72$
   - Center Dead Zone: Avoid placing persistent subtitles between $y = -0.30$ and $+0.30$ unless specifically requested.
4. **Character Case**: High-converting hook headers should be formatted in `ALL CAPS` to maximize font glyph surface area and visual weight.

---

## 5. Semantic & Syntactic Phrasing Rules (Cognitive Chunking)

Never divide words mechanically by raw token counts. Follow these 5 linguistic constituent rules to ensure instant cognitive comprehension in vertical video:

1. **Prepositional Binding**:
   - Always bind prepositions directly with their noun phrase object.
   - Example: `"สามารถพูด"` | `"กับแอปไหน"` (Correct) vs `"สามารถพูดกับ"` | `"แอปไหน"` (Incorrect).
2. **Subject-Predicate Separation**:
   - Keep clean subjects isolated from verbal predicates; attach conjunctions/adverbs to the predicate.
   - Example: `"ตัว Gemini "` | `"ก็ช่วยเราใน"` (Correct) vs `"ตัว Gemini ก็"` | `"ช่วยเราใน"` (Incorrect).
3. **Idiomatic Linking**:
   - Keep common introductory idioms as single complete conceptual chunks.
   - Example: `"และนี่ก็คือ"` | `"เหตุผลว่า"` (Correct) vs `"และนี่ก็"` | `"คือเหตุผลว่า"` (Incorrect).
4. **Subordinate Clause Breakdown**:
   - Split complex compound clauses into grammatically coherent units so the viewer grasps meaning in $\le 0.3\,\text{s}$.
   - Example: `"ได้ว่า"` (Conjunction) | `"ไอสิ่งที่"` (Noun topic) | `"เราพูดอ่ะ"` (Subject+Verb) | `"มันพูดผิดตรงไหน"` (Predicate clause).
5. **Technical Term Precision**:
   - Ensure proper nouns match exact software features (e.g. `"Speak to Window"` rather than `"Speak to Windows"`).

---

## 6. The 3-Second High-Stakes FOMO Hook Formula

In vertical short-form video (TikTok, Reels, Shorts), the first $3.0\text{ seconds}$ dictate video retention and algorithmic distribution.

### 6.1 Copywriting Structure
Every opening hook must strictly follow the tripartite architecture:
$$\text{Hook} = \mathbf{[Audience\ Identifier]} + \mathbf{[Action/Scroll\ Neglect]} + \mathbf{[High-Stakes\ Loss/FOMO]}$$

- **Audience Identifier**: Instantly targets the specific tribe or device demographic (e.g., *"คนที่ใช้ macOS"*, *"คนเขียนโค้ด"*, *"ฟรีแลนซ์"*).
- **Action / Scroll Neglect**: Directs viewer physical engagement (e.g., *"ถึงไม่ควรเลื่อนผ่านคลิปนี้"*, *"อย่าเพิ่งกดข้าม"*).
- **High-Stakes Loss / Curiosity Gap**: Establishes immediate fear of missing out or hidden productivity gain (e.g., *"เหตุผลว่า..."*, *"ถ้ายังไม่รู้สิ่งนี้"*).
- **Thai Example (User Project `0924 (1)` Benchmark)**:
  - Phrase: *"และนี่ก็คือ เหตุผลว่า ทำไมคนที่ ใช้ macOS ถึงไม่ควร เลื่อนผ่านคลิปนี้ นะครับ"*
  - Hook Card Display: Visual hook title active across $0.00\text{s} - 3.00\text{s}$.

### 6.2 Visual Layout & Sub-Face Placement
When the creator is talking on camera (talking head frame):
- **Placement**: $x = 0.0, y = -0.38$ (Sub-Face placement directly below chin/upper-chest safe area).
- **Scale**: `1.0x` - `1.2x` (compact, punchy headline framing).
- **Timing**: $0.00\text{s} - 3.00\text{s}$ ($3,000,000\,\mu\text{s}$). Cuts cleanly exactly at 3 seconds before transition to desktop screen demo.
- **Contrast & Styling**: Uses Preset 1 (`bold_yellow` `#ffd600`) or Preset 5 (`user_native_white_outline` with solid outline) to establish focal dominance over video background.
