"""Transcrição local (faster-whisper, CPU, sem nuvem) e divisão em blocos de legenda.

O áudio é decodificado pelo FFmpeg para PCM 16 kHz mono float32 (o decodificador PyAV do faster-whisper é
incompatível com o av 19 instalado: "open() got an unexpected keyword argument 'metadata_errors'").
`faster_whisper`/`numpy` só são importados dentro das funções.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from app.integrations.capcut.errors import CapCutCancelled, CapCutError
from app.integrations.capcut.media import run_tool, tool_path
from app.integrations.capcut.skill import MIN_GAP_S, PAUSE_CLEAR_S, TAIL_S

MODELS = ("small", "medium", "large-v3-turbo", "large-v3")
DEFAULT_MODEL = "large-v3-turbo"
# Repositórios do Hugging Face usados pelo faster-whisper 1.2.1 (faster_whisper.utils._MODELS).
MODEL_REPOS = {
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "large-v3": "Systran/faster-whisper-large-v3",
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
    seg: int = 0            # frase (segmento do Whisper) a que a palavra pertence: unidade da revisão
    doubt: bool = False     # trecho duvidoso: vai marcado para a revisão das legendas

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Word:
        return cls(start=float(data["start"]), end=float(data["end"]), text=str(data["text"]),
                   probability=float(data.get("probability", 1.0)), seg=int(data.get("seg", 0) or 0),
                   doubt=bool(data.get("doubt", False)))


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


@dataclass
class _Segment:
    start: float
    end: float
    words: list[Word]


# Trechos duvidosos (docs/CAPCUT.md, "Fidelidade das legendas"). Verificado no real (2026-11-07, Reel com diálogo
# rápido, duas vozes e ruído): na janela de 30 s o Whisper completa o que ouve com o contexto — inventou "Esse é o meu
# laranjinha" (repetição de uma frase posterior) espremido em 0,36 s com 4 palavras de duração zero e trocou palavras
# ("Gerson" por "mesmo"); retranscrever o trecho sozinho consertou uns erros e criou outros, então o trecho não é
# reescrito às cegas: vai MARCADO para a revisão (o dono ouve, corrige e o alinhador recalcula os tempos).
# Frase suspeita (toda marcada) = 2+ palavras de duração ~0, ritmo impossível ou confiança média baixa;
# fora isso, só a palavra de confiança baixa fica marcada.
SUSPECT_MIN_WORD_S = 0.03
SUSPECT_MAX_SYLLABLES_PER_S = 9.0
SUSPECT_MIN_MEAN_PROB = 0.60
DOUBT_WORD_PROB = 0.6
# Nota acústica (média do log-prob das letras) a partir da qual o tempo do alinhador forçado é usado.
ALIGN_MIN_SCORE = -3.0
MIN_WORD_S = 0.08
JOIN_GAP_S = 0.25
_VOWELS = re.compile(r"[aeiouyáéíóúâêôãõàü]+", re.IGNORECASE)


def syllables(text: str) -> int:
    return max(1, len(_VOWELS.findall(text)))


def _segment_suspicious(seg: _Segment) -> bool:
    if not seg.words:
        return False
    if sum(1 for w in seg.words if w.end - w.start < SUSPECT_MIN_WORD_S) >= 2:
        return True
    span = max(seg.words[-1].end - seg.words[0].start, 1e-3)
    if len(seg.words) > 1 and sum(syllables(w.text) for w in seg.words) / span > SUSPECT_MAX_SYLLABLES_PER_S:
        return True
    probs = [w.probability for w in seg.words]
    return sum(probs) / len(probs) < SUSPECT_MIN_MEAN_PROB


def _decode(whisper: Any, audio: Any, language: str, *, clip: tuple[float, float] | None = None,
            cancel: threading.Event | None = None, progress: Callable[[float], None] | None = None,
            total: float = 0.0) -> list[_Segment]:
    """Segmentos com palavras. Sem `clip`: o áudio todo, com o filtro de voz (silêncio/ruído não vira texto
    inventado). Com `clip`: só aquele trecho, sozinho (sem o contexto do resto do vídeo)."""
    kwargs: dict[str, Any] = dict(language=language, word_timestamps=True, condition_on_previous_text=False,
                                  beam_size=5)
    if clip is None:
        kwargs["vad_filter"] = True
    else:
        kwargs.update(vad_filter=False, clip_timestamps=[round(clip[0], 3), round(clip[1], 3)])
    segments, _info = whisper.transcribe(audio, **kwargs)
    out: list[_Segment] = []
    for segment in segments:  # gerador: a decodificação acontece aqui, segmento a segmento
        if cancel is not None and cancel.is_set():
            raise CapCutCancelled()
        words = [Word(float(w.start), float(w.end), " ".join(str(w.word).split()), float(w.probability))
                 for w in segment.words or []]
        words = [w for w in words if w.text]
        if words:
            out.append(_Segment(float(segment.start), float(segment.end), words))
        if progress is not None and total > 0:
            progress(min(1.0, float(segment.end) / total))
    return out


def _distribute(words: list[Word], start: float, end: float) -> None:
    """Reparte [start, end] entre `words` na proporção das sílabas (palavras amontoadas no mesmo instante)."""
    weights = [syllables(w.text) for w in words]
    total = sum(weights)
    span = max(end - start, MIN_WORD_S * len(words))
    t = start
    for w, weight in zip(words, weights):
        w.start = t
        t += span * weight / total
        w.end = t


def refine_times(words: list[Word], aligned: list[Any] | None = None) -> list[Word]:
    """Tempo final das palavras: o do alinhador forçado quando a nota acústica é boa; senão o do Whisper. Depois:
    ordem crescente sem sobreposição, sem palavra de duração ~0 (as amontoadas são redistribuídas entre a vizinha
    anterior e a seguinte, pelas sílabas) e fim estendido até a próxima palavra em pausas curtas (destaque contínuo)."""
    out = [replace(w) for w in words]
    if aligned is not None:
        for w, a in zip(out, aligned):
            if getattr(a, "aligned", False) and a.score >= ALIGN_MIN_SCORE and a.end > a.start:
                w.start, w.end = float(a.start), float(a.end)
    for k in range(1, len(out)):                       # monotônico
        if out[k].start < out[k - 1].start:
            out[k].start = out[k - 1].start
        if out[k].end < out[k].start:
            out[k].end = out[k].start
    n = len(out)
    k = 0
    while k < n:                                        # amontoadas: duração ~0 (o Whisper prende ao fim do segmento)
        if out[k].end - out[k].start >= MIN_WORD_S:
            k += 1
            continue
        j = k
        while j < n and out[j].end - out[j].start < MIN_WORD_S:
            j += 1
        last = j if j < n else n - 1                    # a palavra seguinte costuma ter ficado com o tempo de todas
        run = out[k:last + 1]
        lo = out[k - 1].end if k > 0 else run[0].start
        hi = max(run[-1].end, lo + MIN_WORD_S * len(run))
        _distribute(run, lo, hi)
        k = last + 1
    for k in range(1, n):                               # sem sobreposição depois da redistribuição
        if out[k].start < out[k - 1].end:
            out[k].start = out[k - 1].end
            out[k].end = max(out[k].end, out[k].start + MIN_WORD_S)
    for k in range(n - 1):                              # pausa curta: o destaque segue até a próxima palavra
        if 0 < out[k + 1].start - out[k].end < JOIN_GAP_S:
            out[k].end = out[k + 1].start
    for w in out:
        w.start, w.end = round(w.start, 3), round(max(w.end, w.start + 0.01), 3)
    return out


def mark_doubts(segments: list[_Segment]) -> list[Word]:
    """Palavras com `seg` (índice da frase) e `doubt`: a frase toda quando é suspeita (amontoada/ritmo impossível/
    confiança baixa), senão só as palavras de confiança baixa."""
    words: list[Word] = []
    for index, seg in enumerate(segments):
        suspicious = _segment_suspicious(seg)
        for w in seg.words:
            words.append(Word(w.start, w.end, w.text, w.probability, seg=index,
                              doubt=suspicious or w.probability < DOUBT_WORD_PROB))
    return words


def align_text(audio: Any, texts: list[str], lo: float, hi: float, models_dir: Path) -> list[Word]:
    """Tempo de `texts` (palavras corrigidas na revisão) dentro de [lo, hi] do áudio: alinhamento forçado quando o
    modelo está instalado; senão repartido pelas sílabas. Sempre dentro da janela, em ordem, sem duração zero."""
    from app.integrations.capcut import align

    lo, hi = max(0.0, lo), max(lo + MIN_WORD_S * max(1, len(texts)), hi)
    words = [Word(lo, lo, text) for text in texts if text.strip()]
    if not words:
        return []
    models_root = Path(models_dir).parent
    aligned = None
    if align.available(models_root):
        piece = audio[int(lo * SAMPLE_RATE):int(hi * SAMPLE_RATE)]
        try:
            aligned = align.align_words(piece, [w.text for w in words], models_root, offset=lo)
        except Exception:  # noqa: BLE001 - sem alinhamento: repartido pelas sílabas
            aligned = None
    if aligned is not None and all(a.aligned for a in aligned):
        for w, a in zip(words, aligned):
            w.start, w.end = float(a.start), float(a.end)
    else:
        _distribute(words, lo, hi)
    refined = refine_times(words)
    for w in refined:
        w.start, w.end = min(max(w.start, lo), hi), min(max(w.end, lo), hi)
    return refined


ALIGN_WINDOW_S = 30.0
ALIGN_PAD_S = 0.3


def _align_by_windows(audio: Any, words: list[Word], models_root: Path, total: float,
                      cancel: threading.Event | None) -> list[Any] | None:
    """Alinhamento forçado por grupos de frases (até ~30 s cada, janela entre os grupos vizinhos): memória e tempo
    limitados para qualquer duração de vídeo. None = alinhador indisponível ou falhou (ficam os tempos do Whisper)."""
    from app.integrations.capcut import align

    if not words or not align.available(models_root):
        return None
    groups: list[list[int]] = []
    for i, w in enumerate(words):
        if groups and w.seg == words[groups[-1][-1]].seg:
            groups[-1].append(i)
        elif groups and w.end - words[groups[-1][0]].start <= ALIGN_WINDOW_S:
            groups[-1].append(i)
        else:
            groups.append([i])
    result: list[Any] = [None] * len(words)
    try:
        for g, idx in enumerate(groups):
            if cancel is not None and cancel.is_set():
                raise CapCutCancelled()
            prev_end = words[groups[g - 1][-1]].end if g > 0 else 0.0
            next_start = words[groups[g + 1][0]].start if g + 1 < len(groups) else total
            lo = max(prev_end, words[idx[0]].start - ALIGN_PAD_S, 0.0)
            hi = min(next_start, max(words[k].end for k in idx) + ALIGN_PAD_S, total)
            if hi - lo < 0.1:
                continue
            piece = audio[int(lo * SAMPLE_RATE):int(hi * SAMPLE_RATE)]
            for k, a in zip(idx, align.align_words(piece, [words[k].text for k in idx], models_root, offset=lo)):
                result[k] = a
    except CapCutCancelled:
        raise
    except Exception:  # noqa: BLE001 - alinhador é refinamento; sem ele ficam os tempos consertados
        return None
    return [a if a is not None else align.Aligned(w.start, w.end, -99.0, False) for a, w in zip(result, words)]


def transcribe_file(video: Path, *, model: str = DEFAULT_MODEL, language: str = "pt", models_dir: Path,
                    cancel: threading.Event | None = None,
                    progress: Callable[[float], None] | None = None) -> list[Word]:
    """Palavras com tempo (segundos) do áudio exato do arquivo. Sem fala (ou sem áudio) => lista vazia.

    1. Whisper no áudio todo com o filtro de voz (cobertura completa; silêncio não vira texto).
    2. Segmento suspeito e palavra de confiança baixa ficam marcados (`doubt`) para a revisão das legendas.
    3. Tempo de cada palavra pelo alinhamento forçado (align.py) onde a nota acústica é boa; o resto é consertado
       (ordem, sem duração zero, sem amontoado). Sem o modelo do alinhador, só o conserto.
    """
    from app.integrations.capcut import align

    audio = decode_audio(video)
    if audio is None or len(audio) == 0:
        return []
    total = len(audio) / SAMPLE_RATE
    whisper = get_model(model, models_dir)
    if cancel is not None and cancel.is_set():
        raise CapCutCancelled()
    try:
        segments = _decode(whisper, audio, language, cancel=cancel,
                           progress=(lambda p: progress(0.75 * p)) if progress else None, total=total)
        if progress is not None:
            progress(0.85)
        words = mark_doubts(segments)
        aligned = _align_by_windows(audio, words, Path(models_dir).parent, total, cancel)
        words = refine_times(words, aligned)
    except CapCutError:
        raise
    except Exception as exc:
        raise CapCutError("transcription_failed", "A transcrição falhou.") from exc
    if progress is not None:
        progress(1.0)
    return words


def aligner_available(models_dir: Path) -> bool:
    """O modelo do alinhamento forçado está instalado (models_dir = pasta dos modelos do Whisper)."""
    from app.integrations.capcut import align

    return align.available(Path(models_dir).parent)


# --------------------------------------------------------------------------------------------- blocos

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _join(words: list[Word]) -> str:
    return " ".join(_clean(w.text) for w in words if _clean(w.text))


def _fits(words: list[Word], max_words: int, max_chars: int) -> bool:
    return len(words) <= max_words and len(_join(words)) <= max_chars


def _is_function_word(word: Word) -> bool:
    return _clean(word.text).lower().strip(",;:") in FUNCTION_WORDS


# Bloco de legenda com menos fala que isto pisca na tela (verificado: "Ei," ficava 0,07 s): junta com o vizinho.
MIN_READ_S = 0.35


def _merge_flashes(groups: list[list[Word]], max_words: int, max_chars: int, pause_split: float) -> list[list[Word]]:
    """Junta o bloco curto demais para ler com o seguinte (ou o anterior) quando cabe nos limites configurados
    (palavras e caracteres por legenda), sem atravessar fim de frase nem pausa longa."""
    def duration(g: list[Word]) -> float:
        return max(w.end for w in g) - g[0].start

    def can_join(a: list[Word], b: list[Word]) -> bool:
        joined = a + b
        return (_fits(joined, max_words, max_chars) and not _clean(a[-1].text).endswith(_HARD_PUNCT)
                and b[0].start - a[-1].end < pause_split)

    out = [list(g) for g in groups]
    i = 0
    while i < len(out):
        if duration(out[i]) < MIN_READ_S:
            if i + 1 < len(out) and can_join(out[i], out[i + 1]):
                out[i:i + 2] = [out[i] + out[i + 1]]
                continue
            if i > 0 and can_join(out[i - 1], out[i]):
                out[i - 1:i + 1] = [out[i - 1] + out[i]]
                i -= 1
                continue
        i += 1
    return out


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
                    if cut is not None and current[cut].end - current[0].start >= MIN_READ_S:
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
    groups = _merge_flashes(groups, max_words, max_chars, pause_split)

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
