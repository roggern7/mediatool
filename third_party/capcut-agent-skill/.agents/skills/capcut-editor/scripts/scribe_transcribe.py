#!/usr/bin/env python3
"""
ElevenLabs Scribe V2 Transcription Engine for CapCut Editor Skill.
Powered by fal.ai (fal-ai/elevenlabs/speech-to-text/scribe-v2).

Features:
- Extracts audio from local video/audio footage using FFmpeg.
- Uploads audio securely to fal.ai.
- Subscribes to ElevenLabs Scribe V2 with custom keyterms for domain-specific vocabulary.
- Parses word-level timestamps (type="word", start, end, text).
- Assembles words into 2-3 word kinetic subtitle chunks.
- Injects formatted text tracks into CapCut Desktop 9.5.0 draft files.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def get_fal_key() -> Optional[str]:
    """Retrieves FAL_KEY from environment, .env, or ~/.env."""
    key = os.environ.get("FAL_KEY") or os.environ.get("FAL_API_KEY")
    if key:
        return key

    candidate_paths = [
        Path(".env"),
        Path.home() / ".env",
        Path(__file__).resolve().parent.parent / ".env",
        Path(__file__).resolve().parent / ".env",
    ]
    for env_path in candidate_paths:
        if env_path.exists():
            try:
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("FAL_KEY=") or line.startswith("FAL_API_KEY="):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if val:
                            return val
            except Exception:
                pass
    return None


def extract_audio_for_upload(input_media: Path) -> Path:
    """Extracts optimized MP3 audio from a media file for fast cloud upload."""
    temp_dir = Path(tempfile.gettempdir())
    out_mp3 = temp_dir / f"scribe_{uuid.uuid4().hex[:8]}.mp3"

    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_media),
        "-vn",
        "-acodec", "libmp3lame",
        "-ac", "1",
        "-ar", "44100",
        "-q:a", "4",
        str(out_mp3)
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out_mp3


def transcribe_with_scribe_v2(
    media_path: Path,
    keyterms: Optional[List[str]] = None,
    language_code: Optional[str] = None
) -> Dict[str, Any]:
    """
    Invokes ElevenLabs Scribe V2 via fal_client.
    Returns full transcription payload with word-level timings.
    """
    fal_key = get_fal_key()
    if not fal_key:
        raise ValueError(
            "FAL_KEY not found. Please set your FAL_KEY via: export FAL_KEY=\"your_key_here\" or add FAL_KEY=... in ~/.env"
        )

    os.environ["FAL_KEY"] = fal_key

    try:
        import fal_client
    except ImportError:
        raise ImportError("fal_client is required. Run via 'uv run --with fal-client python3 ...' or install fal-client.")

    print(f"[*] Extracting audio from {media_path.name}...")
    audio_file = extract_audio_for_upload(media_path)

    try:
        print("[*] Uploading audio to fal storage...")
        audio_url = fal_client.upload_file(str(audio_file))
        print(f"[+] Uploaded audio URL: {audio_url}")

        default_keyterms = [
            "macOS", "Gemini", "Split Window", "Fn", "Claude",
            "Notion", "MCP", "Learning", "Double-tap", "Transcribe"
        ]
        active_keyterms = keyterms if keyterms is not None else default_keyterms

        arguments: Dict[str, Any] = {
            "audio_url": audio_url,
            "tag_audio_events": True,
            "diarize": False,
            "keyterms": active_keyterms
        }
        if language_code:
            arguments["language_code"] = language_code

        print("[*] Requesting ElevenLabs Scribe V2 inference...")
        result = fal_client.subscribe(
            "fal-ai/elevenlabs/speech-to-text/scribe-v2",
            arguments=arguments,
            with_logs=True
        )
        return result
    finally:
        if audio_file.exists():
            try:
                audio_file.unlink()
            except Exception:
                pass


def align_thai_words_with_timestamps(
    full_text: str,
    raw_tokens: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Aligns Thai text segmented by pythainlp with Scribe V2 subword/character tokens
    to calculate precise start and end timestamps for whole semantic words.
    """
    try:
        import pythainlp
    except ImportError:
        return []

    tokens = [w for w in raw_tokens if w.get("type") == "word" and w.get("text", "").strip()]
    segmented = [w.strip() for w in pythainlp.word_tokenize(full_text, engine="newmm") if w.strip()]

    aligned = []
    tok_idx = 0
    for seg in segmented:
        matched_chars = ""
        start_time = None
        end_time = None

        while tok_idx < len(tokens) and len(matched_chars) < len(seg):
            t = tokens[tok_idx]
            txt = t.get("text", "")
            if start_time is None:
                start_time = float(t.get("start", 0.0))
            end_time = float(t.get("end", 0.0))
            matched_chars += txt
            tok_idx += 1

        aligned.append({
            "word": seg,
            "start": start_time or 0.0,
            "end": end_time or 0.0
        })
    return aligned


def join_words_with_smart_spacing(words: List[str]) -> str:
    """Joins words inserting spaces around Latin/ASCII tokens or where appropriate."""
    res = ""
    for idx, w in enumerate(words):
        if idx == 0:
            res += w
        else:
            prev = words[idx - 1]
            prev_ascii = prev.isascii() and prev.isalnum()
            curr_ascii = w.isascii() and w.isalnum()
            if prev_ascii or curr_ascii:
                res += " " + w
            else:
                res += w
    return res


def chunk_words_to_subtitles(
    words_data: List[Dict[str, Any]],
    full_text: str = "",
    words_per_chunk: int = 3,
    min_chunk_duration: float = 0.45
) -> List[Tuple[str, float, float]]:
    """
    Groups individual words from Scribe V2 into compact 2-3 word kinetic subtitle chunks.
    Supports intelligent Thai semantic word alignment if pythainlp is present.
    """
    has_thai = any('\u0e00' <= ch <= '\u0e7f' for ch in full_text)
    if has_thai and full_text:
        aligned_words = align_thai_words_with_timestamps(full_text, words_data)
        if aligned_words:
            chunks = []
            for i in range(0, len(aligned_words), words_per_chunk):
                grp = aligned_words[i:i + words_per_chunk]
                text = join_words_with_smart_spacing([x["word"] for x in grp])
                st = grp[0]["start"]
                en = grp[-1]["end"]
                if en - st < min_chunk_duration:
                    en = st + min_chunk_duration
                chunks.append((text, st, en))
            return chunks

    # Default fallback for English or when pythainlp is not installed
    spoken_words = [
        w for w in words_data
        if w.get("type") == "word" and w.get("text", "").strip()
    ]

    chunks = []
    i = 0
    while i < len(spoken_words):
        group = spoken_words[i:i + words_per_chunk]
        if not group:
            break
        text = " ".join(w.get("text", "").strip() for w in group).strip()
        start = float(group[0].get("start", 0.0))
        end = float(group[-1].get("end", start + min_chunk_duration))
        if end - start < min_chunk_duration:
            end = start + min_chunk_duration
        chunks.append((text, start, end))
        i += words_per_chunk
    return chunks


def build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ElevenLabs Scribe V2 Speech-to-Text Transcriber for CapCut."
    )
    parser.add_argument("input_media", type=Path, help="Path to input audio or video file.")
    parser.add_argument("--keyterms", nargs="*", default=None, help="Custom keyterms to bias transcription.")
    parser.add_argument("--language", default=None, help="Language code (e.g. tha, eng).")
    parser.add_argument("--words-per-chunk", type=int, default=3, help="Words per subtitle chunk (default: 3).")
    parser.add_argument("--output-json", type=Path, default=None, help="Optional output path for raw JSON.")
    return parser


def main() -> int:
    parser = build_cli()
    args = parser.parse_args()

    if not args.input_media.exists():
        print(f"Error: Input media does not exist: {args.input_media}", file=sys.stderr)
        return 1

    try:
        result = transcribe_with_scribe_v2(
            media_path=args.input_media,
            keyterms=args.keyterms,
            language_code=args.language
        )
    except Exception as e:
        print(f"Transcription failed: {e}", file=sys.stderr)
        return 1

    raw_words = result.get("words", [])
    full_text = result.get("text", "")
    chunks = chunk_words_to_subtitles(raw_words, full_text=full_text, words_per_chunk=args.words_per_chunk)

    print("\n" + "=" * 50)
    print(f"ElevenLabs Scribe V2 Result for {args.input_media.name}:")
    print(f"Detected Language: {result.get('language_code')}")
    print(f"Total Words: {len(raw_words)} | Kinetic Chunks: {len(chunks)}")
    print("=" * 50)

    for idx, (txt, st, en) in enumerate(chunks[:15]):
        print(f"{idx + 1:02d}. [{st:.2f}s -> {en:.2f}s] {txt}")
    if len(chunks) > 15:
        print(f"... and {len(chunks) - 15} more chunks.")

    if args.output_json:
        result["kinetic_chunks"] = [
            {"text": c[0], "start": c[1], "end": c[2]} for c in chunks
        ]
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n[+] Saved Scribe V2 output and kinetic chunks to: {args.output_json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
