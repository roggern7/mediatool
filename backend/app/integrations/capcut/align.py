"""Alinhamento forçado das palavras com o áudio (CTC, wav2vec2 português, ONNX, CPU, sem nuvem).

O Whisper decide O QUE foi dito; este módulo decide QUANDO cada palavra foi dita, encaixando o texto letra a letra no
áudio exato que vai para o CapCut (Viterbi sobre as probabilidades por quadro de 20 ms do modelo acústico). O tempo por
palavra do Whisper (atenção cruzada + limites de segmento) junta palavras faladas rápido no mesmo instante e as prende
ao fim estimado do segmento; aqui cada palavra ganha o trecho em que suas letras realmente soam.

Modelo: `jonatasgrosman/wav2vec2-large-xlsr-53-portuguese` (Apache-2.0, o mesmo que o WhisperX usa para "pt"),
convertido uma vez para ONNX (`model.onnx` + `vocab.json`) em `<data>/models/aligner/wav2vec2-xlsr-53-pt`.
Sem o modelo, `available()` é False e a transcrição usa os tempos do Whisper (o chamador avisa).

Também devolve, por palavra, a nota acústica (média do log da probabilidade das letras nos quadros escolhidos): texto que
não está no áudio recebe nota muito baixa — base para descartar palavra inventada (ver transcribe.py).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("media_tool.capcut.align")

SAMPLE_RATE = 16000
MODEL_DIR_NAME = "wav2vec2-xlsr-53-pt"
WINDOW_S = 30
CONTEXT_S = 2

_lock = threading.Lock()
_sessions: dict[str, tuple[Any, dict[str, int], float]] = {}


@dataclass
class Aligned:
    start: float
    end: float
    score: float          # média do log-prob das letras (0 = perfeito; < -6 = provavelmente não está no áudio)
    aligned: bool         # False = palavra sem letras alinháveis (tempo interpolado)


def model_dir(models_root: Path) -> Path:
    return Path(models_root) / "aligner" / MODEL_DIR_NAME


def available(models_root: Path) -> bool:
    folder = model_dir(models_root)
    return (folder / "model.onnx").is_file() and (folder / "vocab.json").is_file()


def _session(models_root: Path) -> tuple[Any, dict[str, int], float]:
    folder = model_dir(models_root)
    key = str(folder.resolve())
    with _lock:
        if key not in _sessions:
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.log_severity_level = 3
            session = ort.InferenceSession(str(folder / "model.onnx"), sess_options=opts,
                                           providers=["CPUExecutionProvider"])
            vocab = {str(k): int(v) for k, v in json.loads((folder / "vocab.json").read_text("utf-8")).items()}
            meta_file = folder / "meta.json"
            meta = json.loads(meta_file.read_text("utf-8")) if meta_file.is_file() else {}
            _sessions[key] = (session, vocab, float(meta.get("frame_seconds", 0.02)))
        return _sessions[key]


def release() -> None:
    with _lock:
        _sessions.clear()


# ------------------------------------------------------------------------------------------------ emissões

def _log_softmax(x: Any) -> Any:
    import numpy as np

    m = x.max(axis=-1, keepdims=True)
    y = x - m
    return y - np.log(np.exp(y).sum(axis=-1, keepdims=True))


def emissions(audio: Any, models_root: Path) -> tuple[Any, float]:
    """Log-probabilidades por quadro (T × vocab) e a duração de um quadro em segundos."""
    import numpy as np

    session, _vocab, _frame = _session(models_root)
    audio = np.asarray(audio, dtype=np.float32)
    window, context = WINDOW_S * SAMPLE_RATE, CONTEXT_S * SAMPLE_RATE
    outs = []
    for start in range(0, max(len(audio), 1), window):
        a, b = max(0, start - context), min(len(audio), start + window + context)
        piece = audio[a:b]
        piece = (piece - piece.mean()) / np.sqrt(piece.var() + 1e-7)   # do_normalize do extrator do modelo
        logits = session.run(None, {session.get_inputs()[0].name: piece[None, :]})[0][0]
        frame = (b - a) / SAMPLE_RATE / max(1, logits.shape[0])
        skip_a = int(round((start - a) / SAMPLE_RATE / frame))
        keep = int(round((min(start + window, len(audio)) - start) / SAMPLE_RATE / frame))
        outs.append(logits[skip_a:skip_a + keep])
    logits = np.concatenate(outs, axis=0) if outs else np.zeros((0, 1), dtype=np.float32)
    frame_s = len(audio) / SAMPLE_RATE / max(1, logits.shape[0])
    return _log_softmax(logits.astype(np.float32)), frame_s


# ------------------------------------------------------------------------------------------------ texto

def normalize_word(text: str, vocab: dict[str, int]) -> str:
    """Letras do vocabulário (minúsculas). Acento fora do vocabulário vira a letra base; o resto sai."""
    out = []
    for ch in text.lower():
        if ch in vocab and ch not in ("|", " "):
            out.append(ch)
            continue
        base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
        out.extend(c for c in base if c in vocab and c not in ("|", " "))
    return re.sub(r"\s+", "", "".join(out))


# ------------------------------------------------------------------------------------------------ Viterbi CTC

def _viterbi(logp: Any, tokens: list[int], blank: int) -> Any:
    """Caminho CTC mais provável que emite exatamente `tokens`. Devolve, por quadro, o índice do estado
    (estados pares = branco, ímpar 2k+1 = token k). numpy, vetorizado por quadro."""
    import numpy as np

    T = logp.shape[0]
    S = 2 * len(tokens) + 1
    labels = np.full(S, blank, dtype=np.int64)
    labels[1::2] = tokens
    skip = np.zeros(S, dtype=bool)
    skip[3::2] = np.asarray(tokens[1:], dtype=np.int64) != np.asarray(tokens[:-1], dtype=np.int64)
    neg = np.float32(-1e30)
    score = np.full(S, neg, dtype=np.float32)
    score[0] = logp[0, labels[0]]
    if S > 1:
        score[1] = logp[0, labels[1]]
    back = np.zeros((T, S), dtype=np.int8)       # 0 = ficou, 1 = veio de s-1, 2 = veio de s-2
    for t in range(1, T):
        stay = score
        prev1 = np.concatenate(([neg], score[:-1]))
        prev2 = np.where(skip, np.concatenate(([neg, neg], score[:-2])), neg)
        best = np.maximum(np.maximum(stay, prev1), prev2)
        back[t] = np.where(best == stay, 0, np.where(best == prev1, 1, 2))
        score = best + logp[t, labels]
    state = S - 1 if S == 1 or score[S - 1] >= score[S - 2] else S - 2
    path = np.empty(T, dtype=np.int64)
    for t in range(T - 1, -1, -1):
        path[t] = state
        state -= int(back[t, state])
    return path


def align_words(audio: Any, words: list[str], models_root: Path, *, offset: float = 0.0) -> list[Aligned]:
    """Tempo (s, somado `offset`) e nota acústica de cada palavra de `words`, na ordem, dentro de `audio`."""
    import numpy as np

    _session_, vocab, _ = _session(models_root)
    blank = vocab.get("<pad>", vocab.get("<blank>", 0))
    sep = vocab.get("|")
    logp, frame_s = emissions(audio, models_root)
    norm = [normalize_word(w, vocab) for w in words]
    tokens: list[int] = []
    owner: list[int] = []            # índice da palavra dona de cada token (-1 = separador)
    for i, letters in enumerate(norm):
        if not letters:
            continue
        if tokens and sep is not None:
            tokens.append(sep)
            owner.append(-1)
        for ch in letters:
            tokens.append(vocab[ch])
            owner.append(i)
    T = logp.shape[0]
    result = [Aligned(offset, offset, -99.0, False) for _ in words]
    if not tokens or T == 0 or T < len(tokens):
        return result
    path = _viterbi(logp, tokens, blank)
    frames: dict[int, list[int]] = {}
    for t, state in enumerate(path):
        if state % 2 == 1:
            k = (state - 1) // 2
            if owner[k] >= 0:
                frames.setdefault(owner[k], []).append(t)
    for i in range(len(words)):
        ts = frames.get(i)
        if not ts:
            continue
        lp = [float(logp[t, tokens[(path[t] - 1) // 2]]) for t in ts]
        result[i] = Aligned(start=offset + ts[0] * frame_s, end=offset + (ts[-1] + 1) * frame_s,
                            score=float(np.mean(lp)), aligned=True)
    # palavras sem letras alinháveis (números, símbolos): entre as vizinhas
    for i, item in enumerate(result):
        if item.aligned:
            continue
        prev_end = next((result[j].end for j in range(i - 1, -1, -1) if result[j].aligned), offset)
        next_start = next((result[j].start for j in range(i + 1, len(result)) if result[j].aligned), prev_end)
        result[i] = Aligned(prev_end, max(prev_end, next_start), -99.0, False)
    return result
