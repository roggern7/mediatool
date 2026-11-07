# CapCut Audio Architecture & Ducking Guide

This reference provides audio engineering guidelines, multi-track gain staging, ducking curves, and sound effect synchronization for CapCut Desktop 9.5.0.

---

## 1. Multi-Track Gain Staging

A professional short-form video audio stack balances three distinct acoustic layers:

```
[0.00s] -----------------------------------------------------> [End of Video]
LAYER 1: VOICEOVER / DIALOGUE  ==============================> Gain: 1.0 (0 dB)
LAYER 2: WHOOSH TRANSITION SFX ========>                       Gain: 1.1 (+1 dB, 0.0s - 0.5s)
LAYER 3: BACKGROUND MUSIC      ==============================> Gain: 0.22 (-15 dB Ducked)
                               |<- Fade In ->|  |<- Fade Out ->|
```

| Layer | Role | Target Multiplier | Decibel Equivalent | Timing Strategy |
|---|---|---|---|---|
| **Voiceover / Speech** | Primary communication | `1.0` | $0\text{ dBFS}$ | Continuous, centered |
| **Whoosh SFX** | Hook punctuation | `1.10 - 1.20` | $+1.0\text{ dBFS}$ | $0.0\text{s} - 0.5\text{s}$ (peaks at $0.2\text{s}$) |
| **Background Music (BGM)** | Rhythmic momentum | `0.20 - 0.25` | $-14\text{ dB}$ to $-16\text{ dB}$ | Ducked across video duration |

---

## 2. Audio Fade Material (`materials.audio_fades`)

CapCut Desktop applies fade curves via reference objects in `materials.audio_fades`:

```json
{
  "id": "<AUDIO_FADE_UUID>",
  "type": "audio_fade",
  "fade_in_duration": 300000,
  "fade_out_duration": 500000,
  "fade_type": 0
}
```

- `fade_in_duration`: Microseconds of linear ramp-up from silence (e.g. `300000` = 0.3s).
- `fade_out_duration`: Microseconds of linear decay into silence (e.g. `500000` = 0.5s).
- Attached to segments via `extra_material_refs: ["<SPEED_UUID>", "<AUDIO_FADE_UUID>"]`.

---

## 3. Whoosh SFX Synchronization

In the **Shorts Hook Layout**, the Whoosh sound effect must start at frame 0 ($t = 0\,\mu\text{s}$) and conclude within $500,000\,\mu\text{s}$ ($0.5\text{s}$).

1. **Alignment with Hook Header**: The visual text pop and the audio peak coincide within the first $200\text{ ms}$, creating an immediate audiovisual orienting reflex.
2. **Fade Out**: Applying a short $150\text{ ms}$ fade-out at the end of the whoosh sample prevents clicks or clipping when transitioning to the ongoing BGM bed.

---

## 4. Synthetic Asset Generation (FFmpeg)

When external royalty-free audio tracks are not provided, `capcut_draft.py` uses FFmpeg to synthesize clean, mathematically generated audio assets:

### Whoosh SFX:
```bash
ffmpeg -f lavfi -i "anoisesrc=d=0.5:c=white:r=44100,bandpass=f=950:width_type=h:w=600,volume=2.2,afade=t=in:ss=0:d=0.1,afade=t=out:st=0.25:d=0.25" -y outputs/assets/whoosh.wav
```

### Upbeat BGM Bed:
```bash
ffmpeg -f lavfi -i "sine=frequency=440:duration=3.0,volume=0.5" \
       -f lavfi -i "sine=frequency=554.37:duration=3.0,volume=0.4" \
       -f lavfi -i "sine=frequency=659.25:duration=3.0,volume=0.4" \
       -filter_complex "[0:a][1:a][2:a]amix=inputs=3[a];[a]lowpass=f=2000,afade=t=in:ss=0:d=0.3,afade=t=out:st=2.5:d=0.5[out]" \
       -map "[out]" -y outputs/assets/upbeat_bgm.wav
```

---

## 5. Curated Audio Presets & Gain Staging

Extracted and verified from user project `0924 (1)`:

### 5.1 Primary BGM: Sunset Chill Vibes
- **CapCut Resource ID**: `7548447633133963290` ("Sunset Chill Vibes")
- **Genre**: Lo-Fi Chill / Modern Warm Tech Ambient
- **Master Multiplier**: `volume: 0.14` (14% / -17 dB ducked).
- **Strategy**: Continuous background bed across entire video. Provides momentum and acoustic warmth while ensuring spoken voice consonants remain 100% intelligible.

### 5.2 Sensory Foley Trigger: Mouse Click 1
- **CapCut Resource ID**: `7380059209545746473` ("Mouse click 1")
- **Master Multiplier**: `volume: 1.00` (100% full transient attack).
- **Anticipation Lead Rule**: Starts $180\text{ ms} - 200\text{ ms}$ *before* visual pop-up elements (e.g. SFX at 5.30s, logo badge appears at 5.50s) to orient sensory attention.

### 5.3 Vocal Gain Normalization
- **Talking Head Segment** (close mic): `volume: 1.00`
- **Screen Recording / Desktop Demo**: `volume: 1.40` (+40% gain compensation) to equalize loudness across different recording environments.

---

## 6. Curated Motion-Sound Mapping Matrix (SFX Palette)

Use this deterministic matrix to pair video motion events and psychological beats with appropriate sound effects on Track 5:

| Visual / Narrative Event | Recommended SFX | Psychological & Acoustic Rationale | Target Volume | Timing / Placement Invariant |
|:---|:---|:---|:---|:---|
| **Zoom Out / Wide Pan** | `Whoosh` | Complements camera motion, physical frame reveal, and perspective expansion. | `0.90 - 1.10` | Starts exactly at zoom start; $150\text{ ms}$ fade-out at motion settle. |
| **Graphic / Text Pop-up** | `Pop!` / `Click` / `Camera Shutter` | Punctuate visual pop-ups, badges, logo stickers, or key bold text cards. | `0.95 - 1.00` | Follows **The Anticipation Lead Law** ($180 - 200\text{ ms}$ lead). |
| **Anticipation & Suspense** | `RISER_01` | Ascending pitch builds forward momentum before critical reveals or turning points. | `0.70 - 0.90` | Starts $1.5\text{s} - 3.0\text{s}$ before climax; cuts cleanly at the drop. |
| **Scene / Chapter Transition** | `Camera Shutter` | Delivers sharp, tactile bookmarking between distinct tutorial chapters or screen cuts. | `1.00` | Centered directly on cut transition boundary ($t = 0$). |
| **Tech Edgy / Glitch Moment** | `Glitch` | Heightens digital intensity, tech disruption, unexpected errors, or bold pivots. | `0.85 - 1.00` | Coincides with digital distortion or rapid visual flash ($100 - 300\text{ ms}$). |


