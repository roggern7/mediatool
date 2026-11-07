# Short-Form Video Editing Rules & Invariants

This rule codifies the production standards, timeline hierarchy, audio staging, and typography invariants derived from high-retention vertical short-form video benchmarks (TikTok, Reels, YouTube Shorts) and verified on CapCut Desktop 9.5.0+.

---

## 1. Standard 7-Track Hierarchy

To prevent track collisions and visual/acoustic overlapping, multi-track vertical drafts must adhere to the standard 7-track arrangement:

| Track Index | Track Type | Content & Function | Invariants & Constraints |
|:---|:---|:---|:---|
| **Track 0** | `video` | **Main Footage Base** | Talking head hook + Desktop screen recordings. Sequential cuts. |
| **Track 1** | `text` | **Kinetic Subtitles** | Strict single text track. Pause-aware, clamped non-overlapping dialogue. |
| **Track 2** | `text` | **Opening Hook Card** | High-stakes hook title ($0.0\text{s} - 3.0\text{s}$), sub-face placement ($y = -0.38$). |
| **Track 3** | `video` (overlay) | **Contextual Badges** | Logo overlays (e.g. Gemini Spark Logo, $x = -0.40, y = -0.36$, scale $18.7\%$). |
| **Track 4** | `audio` | **Background Music (BGM)** | "Sunset Chill Vibes", continuous loop, ducked to `volume: 0.14`. |
| **Track 5** | `audio` | **Foley Sound Effects (SFX)** | "Mouse click 1", `volume: 1.00`, with $180 - 200\text{ ms}$ anticipation lead. |
| **Track 6** | `audio` | **Primary Voice / Dialogue** | Raw dialogue vocals; screen recording audio boosted to `volume: 1.40`. |

---

## 2. Audio Staging Invariants

1. **BGM Ducking Law**:
   - In educational, tech, and commentary short-form video, spoken speech consonants (high frequencies) must remain crisp and intelligible.
   - BGM must be ducked to `volume: 0.14` (14% amplitude / -17 dB).
2. **The Anticipation Lead Law**:
   - Sound effects (e.g. mouse clicks, pop-ups, whooshes) must trigger **$180\text{ ms} - 200\text{ ms}$ before** the visual graphic or badge appears.
   - Acoustic anticipation primes the human visual cortex to orient gaze toward the incoming element.
3. **Dialogue Loudness Normalization**:
   - Talking-head footage (close-mic condenser) defaults to `volume: 1.00`.
   - Desktop screen recording audio (often recorded via system mic or further away) must be boosted to `volume: 1.40` (+40% gain) to equalize acoustic energy across cuts.
4. **Motion-Sound FX Mapping Matrix**:
   - **Zoom Out / Camera Move**: Trigger `Whoosh` ($0.90 - 1.10\times$).
   - **Pop-up (Badge/Text/Sticker)**: Trigger `Pop!`, `Click`, or `Camera Shutter` with $180 - 200\text{ ms}$ anticipation lead.
   - **Anticipation Build-up**: Trigger `RISER_01` ($0.70 - 0.90\times$, rising into reveal).
   - **Scene / Chapter Change**: Trigger `Camera Shutter` ($1.00\times$, placed directly on the cut).
   - **Intense Tech / Glitch**: Trigger `Glitch` ($0.85 - 1.00\times$, synchronized to visual stutters).

---

## 3. The 3-Second High-Stakes FOMO Hook Formula

The initial $3.0\text{ seconds}$ govern scroll retention. Every video hook must satisfy:

$$\text{Hook} = \mathbf{[Audience\ Identifier]} + \mathbf{[Action/Scroll\ Neglect]} + \mathbf{[High-Stakes\ Loss/FOMO]}$$

- **Formula Execution**:
  1. *Audience Identifier*: Pinpoints the target viewer (e.g. *"คนที่ใช้ macOS"*, *"คนเขียนโค้ด"*).
  2. *Action / Scroll Neglect*: Commands behavior (e.g. *"ถึงไม่ควรเลื่อนผ่านคลิปนี้"*).
  3. *High-Stakes Loss / FOMO*: Creates curiosity tension (e.g. *"และนี่ก็คือเหตุผลว่า..."*).
- **Hook Timing**: $0.00\text{s} - 3.00\text{s}$ ($3,000,000\,\mu\text{s}$). Cuts cleanly at 3 seconds as the screen transition occurs.
- **Hook Framing**: $x = 0.0, y = -0.38$ (Sub-Face placement directly below chin level; does not obscure creator face or eyes).

---

## 4. Kinetic Subtitle Timeline Invariants

1. **Monotonic Non-Overlap Clamping**:
   - Two text segments on the same track must never overlap in time:
     $$\text{start}_{i+1} - (\text{start}_i + \text{duration}_i) \ge 20,000\,\mu\text{s}\quad (20\,\text{ms})$$
   - *Failure Consequence*: If overlap is $> 0\,\mu\text{s}$, CapCut automatically bumps the overlapping subtitle into a new track above it, creating accidental double-text rendering.
2. **Strict Single Subtitle Track**:
   - All primary speech subtitles must stay on Track 1. Secondary text tracks are reserved exclusively for persistent hook titles or static stickers.
3. **Pause-Aware Silence Clearing**:
   - Whenever a speech silence gap $\ge 180\,\text{ms}$ occurs, clear the subtitle text off screen ($\text{end} = \text{word}_{\text{last}} + 50\,\text{ms}$).
   - Subtitles must never linger across dead air or silent breath pauses.
4. **Native Rich Text Styling (Zero Cache Dependencies)**:
   - Use Preset 5 (`user_native_white_outline`):
     - `check_flag: 15` (`7` base fill + `8` stroke outline).
     - Text fill: Pure White (`#ffffff`, `[1.0, 1.0, 1.0]`).
     - Stroke outline: Solid Black (`#000000`, `[0.0, 0.0, 0.0]`), width `0.06`.
     - Overrides: `useLetterColor: true`, `text_color: "#ffffff"`, `use_effect_default_color: false`.
     - Font: System San Francisco / Roboto (`/Applications/CapCut.app/Contents/Resources/Font/SystemFont/en.ttf`), font size `15.0`.
     - Lower-third safe position: $x = 0.0, y = -0.70$.
     - Never reference external cached template paths (e.g. `Cache/effect/...`), ensuring zero-dependency portability across systems.

---

## 5. Cognitive Constituent Phrasing (5 Semantic Rules)

Subtitles must be chunked into coherent cognitive phrases ($\le 4$ words, $0.3\text{s} - 1.2\text{s}$ duration) rather than mechanical token limits:

1. **Prepositional Binding**: Bind prepositions to their noun objects (e.g. `"สามารถพูด"` \| `"กับแอปไหน"`).
2. **Subject-Predicate Separation**: Keep subjects clean and attach conjunctions to the verbal predicate (e.g. `"ตัว Gemini "` \| `"ก็ช่วยเราใน"`).
3. **Idiomatic Linking**: Treat idiomatic set phrases as single semantic units (e.g. `"และนี่ก็คือ"` \| `"เหตุผลว่า"`).
4. **Subordinate Clause Breakdown**: Split compound conditional clauses into digestible mini-beats (e.g. `"ได้ว่า"` \| `"ไอสิ่งที่"` \| `"เราพูดอ่ะ"` \| `"มันพูดผิดตรงไหน"`).
5. **Technical Term Precision**: Match official casing and spelling of software names and features (e.g. `"Speak to Window"`).
