# ElevenLabs Scribe V2 Reference Guide (`elevenlabs_scribe.md`)

This guide covers the integration of **ElevenLabs Speech to Text - Scribe V2** via [fal.ai](https://fal.ai/models/fal-ai/elevenlabs/speech-to-text/scribe-v2) for programmatic speech recognition, word-level alignment, and kinetic subtitle generation in CapCut Desktop 9.5.0+.

---

## 1. Model Overview & Specifications

- **Model ID**: `fal-ai/elevenlabs/speech-to-text/scribe-v2`
- **Provider**: ElevenLabs hosted on fal.ai Serverless GPU infrastructure
- **Type**: Multilingual Word-Level Speech-to-Text & Audio Event Tagging
- **Latency**: Blazingly fast streaming / low-latency batch inference
- **Pricing**:
  - Base: **$0.008 per input audio minute**
  - With `keyterms`: **+30%** ($0.0104 per minute)
- **Key Capability**: Extremely accurate phonetic transcription with exact word start/end timestamps and custom domain vocabulary biasing (`keyterms`).

---

## 2. API Input Schema

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `audio_url` | `string` | **Yes** | — | Direct HTTPS or fal storage URI to audio/video file. Supported: MP3, WAV, AAC, M4A, MOV, MP4. |
| `language_code` | `string` | No | `null` | ISO-639-3 or BCP-47 language code (e.g. `tha` for Thai, `eng` for English). When omitted, automatic language detection is applied. |
| `tag_audio_events` | `boolean` | No | `true` | When `true`, tags non-speech events (laughter, applause, sighing, background noise). |
| `diarize` | `boolean` | No | `false` | When `true`, assigns speaker identifiers (`speaker_id`) across speech segments. |
| `keyterms` | `list[str]` | No | `null` | Array of up to 100 domain terms, brand names, or technical terms to bias decoding phonetics. |

### Technical Vocabulary Keyterms Example
```python
keyterms = [
    "macOS", "Gemini", "Split Window", "Fn", "Claude",
    "Notion", "MCP", "Learning", "Double-tap", "CapCut"
]
```

---

## 3. API Output Schema

The JSON response contains high-resolution word tokens with precise temporal bounds:

```json
{
  "text": "คลิปนี้ผมจะมาสอนตั้งค่า macOS...",
  "language_code": "tha",
  "language_probability": 0.998,
  "words": [
    {
      "text": "คลิป",
      "start": 0.12,
      "end": 0.38,
      "type": "word"
    },
    {
      "text": "นี้",
      "start": 0.39,
      "end": 0.54,
      "type": "word"
    },
    {
      "text": "ผม",
      "start": 0.55,
      "end": 0.72,
      "type": "word"
    }
  ]
}
```

- Each word object specifies `text`, `start` (seconds), `end` (seconds), and `type` (`"word"` vs `"spacing"` / `"audio_event"`).
- Microsecond conversion for CapCut timeline:
  $$\text{target\_timerange.start} = \text{round}(start \times 1,000,000)$$
  $$\text{target\_timerange.duration} = \text{round}((end - start) \times 1,000,000)$$

---

## 4. Kinetic Subtitle Chunking Algorithm

Displaying full sentences causes visual clutter and low retention in 9:16 vertical shorts. Scribe V2 words are grouped into **kinetic 2-3 word bursts**:

1. **Filtering**: Retain only items where `type == "word"` and `text.strip() != ""`.
2. **Chunking**: Slice words in batches of $N \in [2, 3]$.
3. **Boundary Calculation**:
   - `chunk_start = group[0].start`
   - `chunk_end = max(group[-1].end, chunk_start + 0.40)` (enforce minimum 400ms duration for human readability).
4. **CapCut Alignment**: Insert into subtitle track with identical font styling and positioning so transitions appear seamless as the speaker talks.

---

## 5. Visual Styling Standards for CapCut 9.5.0

To match high-converting vertical Shorts and user specifications:

- **Color**: Pure White (`#FFFFFF`, float `[1.0, 1.0, 1.0]`).
- **Stroke/Outline**: Solid Black (`#000000`, float `[0.0, 0.0, 0.0]`), width `0.15` - `0.18`.
- **Drop Shadow**: Soft subtle black (`#000000`, alpha `0.60`, blur `0.40`).
- **Font Size**: Large high-legibility scale (`font_size = 7.0`, transform scale `1.90` - `1.95`).
- **Positioning**: Fixed Bottom Safe Zone:
  $$x = 0.0, \quad y \in [-0.70, -0.72]$$
  *(Never place subtitles in the center $y \in [-0.30, 0.30]$ where they obstruct demonstration content).*
- **Audio Accents**: 0 Sound effects or BGM unless explicitly requested by the user.

---

## 6. Execution via UV & fal-client

To avoid polluting the system Python environment (macOS PEP 668), execute via `uv`:

```bash
# Verify credentials safely
grep -sq "^FAL_KEY=" ~/.env

# Run transcription engine
uv run --with fal-client python3 scripts/scribe_transcribe.py \
  /path/to/media.mov \
  --keyterms macOS Gemini Claude Notion \
  --words-per-chunk 3 \
  --output-json outputs/scribe_result.json
```
