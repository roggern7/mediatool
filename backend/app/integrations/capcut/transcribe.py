"""Transcrição local (faster-whisper, CPU, sem nuvem) e divisão em blocos de legenda.

O áudio é decodificado pelo FFmpeg para PCM 16 kHz mono float32 (o decodificador PyAV do faster-whisper é
incompatível com o av 19 instalado: "open() got an unexpected keyword argument 'metadata_errors'").
`faster_whisper`/`numpy` só são importados dentro das funções.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.integrations.capcut.errors import CapCutCancelled, CapCutError
from app.integrations.capcut.media import run_tool, tool_path
from app.integrations.capcut.skill import MIN_GAP_S, PAUSE_CLEAR_S, TAIL_S

MODELS = ("small", "medium", "large-v3-turbo")
DEFAULT_MODEL = "large-v3-turbo"
# Repositórios do Hugging Face usados pelo faster-whisper 1.2.1 (faster_whisper.utils._MODELS).
MODEL_REPOS = {
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}
SAMPLE_RATE = 16000

# Palavras curtas que não devem ficar sozinhas no fim de um bloco (pt).
FUNCTION_WORDS = frozenset(
    "de da do das dos a o as os e em no na nos nas com pra para que um uma".split())
_HARD_PUNCT = (".", "!", "?", "…")
_SOFT_PUNCT = (",", ";", ":")


@dataclass
class Word:
    start: float
    end: float
    text: str
    probability: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Word:
        return cls(start=float(data["start"]), end=float(data["end"]), text=str(data["text"]),
                   probability=float(data.get("probability", 1.0)))


@dataclass
class Chunk:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)


# --------------------------------------------------------------------------------------------- modelo (cache)

_model_lock = threading.Lock()
_models: dict[tuple[str, str], Any] = {}


def model_downloaded(models_dir: Path, size: str) -> bool:
    """O modelo já está no cache do Hugging Face em `models_dir` (só olha arquivos)."""
    repo = MODEL_REPOS.get(size)
    if not repo:
        return False
    folder = Path(models_dir) / ("models--" + repo.replace("/", "--")) / "snapshots"
    return any(folder.glob("*/model.bin"))


def get_model(size: str, models_dir: Path) -> Any:
    """WhisperModel carregado (um por tamanho, em memória); baixa no 1º uso para `models_dir`."""
    if size not in MODELS:
        raise CapCutError("transcription_failed", f"Modelo de transcrição desconhecido: {size}.")
    key = (size, str(Path(models_dir).resolve()))
    with _model_lock:
        if key in _models:
            return _models[key]
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise CapCutError("deps_missing", "faster-whisper não está instalado.") from exc
        Path(models_dir).mkdir(parents=True, exist_ok=True)
        try:
            model = WhisperModel(size, device="cpu", compute_type="int8", download_root=str(models_dir))
        except Exception as exc:  # download/carga: rede ou disco
            raise CapCutError("transcription_failed", "Não foi possível carregar o modelo de transcrição.",
                              transient=True) from exc
        _models[key] = model
        return model


def release_models(size: str | None = None) -> None:
    """Libera da memória os modelos carregados (todos ou só um tamanho)."""
    with _model_lock:
        for key in [k for k in _models if size is None or k[0] == size]:
            del _models[key]


# --------------------------------------------------------------------------------------------- transcrição

def decode_audio(path: Path, *, timeout: float = 1800) -> Any:
    """numpy float32 mono 16 kHz; None se o arquivo não tiver áudio."""
    import numpy as np

    result = run_tool([tool_path("ffmpeg"), "-nostdin", "-v", "error", "-i", str(path), "-vn", "-map", "0:a:0?",
                       "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-acodec", "pcm_f32le", "-"],
                      timeout=timeout, binary=True)
    if result.returncode != 0:
        raise CapCutError("transcription_failed", "Não foi possível extrair o áudio do vídeo.")
    if not result.stdout:
        return None
    return np.frombuffer(result.stdout, dtype=np.float32)


def transcribe_file(video: Path, *, model: str = DEFAULT_MODEL, language: str = "pt", models_dir: Path,
                    cancel: threading.Event | None = None,
                    progress: Callable[[float], None] | None = None) -> list[Word]:
    """Palavras com tempo (segundos). Sem fala (ou sem áudio) => lista vazia."""
    audio = decode_audio(video)
    if audio is None or len(audio) == 0:
        return []
    total = len(audio) / SAMPLE_RATE
    whisper = get_model(model, models_dir)
    if cancel is not None and cancel.is_set():
        raise CapCutCancelled()
    try:
        segments, _info = whisper.transcribe(
            audio, language=language, word_timestamps=True, vad_filter=True, condition_on_previous_text=False,
            beam_size=5)
        words: list[Word] = []
        for segment in segments:  # gerador: a decodificação acontece aqui, segmento a segmento
            if cancel is not None and cancel.is_set():
                raise CapCutCancelled()
            for w in segment.words or []:
                text = " ".join(str(w.word).split())
                if text:
                    words.append(Word(float(w.start), float(w.end), text, float(w.probability)))
            if progress is not None and total > 0:
                progress(min(1.0, float(segment.end) / total))
    except CapCutError:
        raise
    except Exception as exc:
        raise CapCutError("transcription_failed", "A transcrição falhou.") from exc
    return words


# --------------------------------------------------------------------------------------------- blocos

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _join(words: list[Word]) -> str:
    return " ".join(_clean(w.text) for w in words if _clean(w.text))


def _fits(words: list[Word], max_words: int, max_chars: int) -> bool:
    return len(words) <= max_words and len(_join(words)) <= max_chars


def _is_function_word(word: Word) -> bool:
    return _clean(word.text).lower().strip(",;:") in FUNCTION_WORDS


def chunk_words(words: list[Word], max_words: int = 3, max_chars: int = 24, pause_split: float = PAUSE_CLEAR_S,
                min_duration: float = 0.30, gap: float = MIN_GAP_S) -> list[Chunk]:
    """Agrupa palavras em blocos de legenda sem sobreposição (regras em docs/CAPCUT.md)."""
    items = sorted((w for w in words if _clean(w.text)), key=lambda w: (w.start, w.end))
    groups: list[list[Word]] = []
    current: list[Word] = []
    for word in items:
        if current:
            prev = current[-1]
            pause = word.start - prev.end >= pause_split
            sentence_end = _clean(prev.text).endswith(_HARD_PUNCT)
            if pause or sentence_end:
                groups.append(current)
                current = [word]
                continue
            if not _fits(current + [word], max_words, max_chars):
                emit, carry = current, []
                if len(current) > 1:
                    cut = next((i for i in range(len(current) - 2, -1, -1)
                                if _clean(current[i].text).endswith(_SOFT_PUNCT)), None)
                    if cut is not None:
                        emit, carry = current[:cut + 1], current[cut + 1:]
                    elif _is_function_word(current[-1]):
                        emit, carry = current[:-1], current[-1:]
                    if carry and not _fits(carry + [word], max_words, max_chars):
                        emit, carry = current, []
                groups.append(emit)
                current = carry + [word]
                continue
        current.append(word)
    if current:
        groups.append(current)

    chunks = [Chunk(start=g[0].start, end=max(w.end for w in g) + TAIL_S, text=_join(g), words=list(g))
              for g in groups]
    for i, chunk in enumerate(chunks):
        if i > 0:
            chunk.start = max(chunk.start, chunks[i - 1].end + gap)
        limit = chunks[i + 1].start - gap if i + 1 < len(chunks) else None
        if chunk.end - chunk.start < min_duration:
            chunk.end = chunk.start + min_duration
        if limit is not None:
            chunk.end = min(chunk.end, limit)
        if chunk.end <= chunk.start:  # palavras coladas: garante duração positiva; o próximo é empurrado
            chunk.end = chunk.start + 0.01
        chunk.start = round(chunk.start, 6)
        chunk.end = round(chunk.end, 6)
    return chunks
