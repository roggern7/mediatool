"""Fidelidade das legendas: tempos das palavras, trechos duvidosos e alinhamento forçado (sem modelos reais).

Caso real que motivou (2026-11-07, Reel com diálogo rápido): o Whisper devolveu "Esse é o meu laranjinha." com quatro
palavras de duração zero presas em 8,10 s e a última com o tempo de todas (8,10–8,46).
"""

from __future__ import annotations

import numpy as np
import pytest

from app.integrations.capcut import align
from app.integrations.capcut import transcribe as T
from app.integrations.capcut.transcribe import Word, _Segment

REAL_COLLAPSE = [
    Word(7.26, 7.66, "Laranjão,", 0.99), Word(7.92, 7.92, "viu?", 1.0),
    Word(8.10, 8.10, "Esse", 0.09), Word(8.10, 8.10, "é", 0.98), Word(8.10, 8.10, "o", 0.37),
    Word(8.10, 8.10, "meu", 0.99), Word(8.10, 8.46, "laranjinha.", 0.98), Word(8.82, 9.12, "Vixe,", 0.68),
]


def test_refine_times_spreads_collapsed_words_without_zero_duration_or_overlap():
    out = T.refine_times(REAL_COLLAPSE)
    assert [w.text for w in out] == [w.text for w in REAL_COLLAPSE]
    assert all(w.end - w.start >= T.MIN_WORD_S - 1e-9 for w in out)
    assert all(out[i].start >= out[i - 1].end - 1e-9 for i in range(1, len(out))), "sem sobreposição"
    # o amontoado ocupa o espaço entre a palavra anterior e o fim de "laranjinha.", pela proporção das sílabas
    run = out[1:7]
    assert run[0].start == pytest.approx(7.66, abs=1e-3)
    assert run[-1].end == pytest.approx(8.46, abs=0.05)
    assert run[-1].end - run[-1].start > run[3].end - run[3].start, "laranjinha (4 sílabas) > meu (1)"
    assert out[-1].start == pytest.approx(8.82), "palavra boa seguinte não muda"


def test_refine_times_prefers_confident_alignment_and_keeps_whisper_when_score_is_low():
    words = [Word(1.0, 1.5, "bom", 0.9), Word(1.5, 2.0, "dia", 0.9)]
    aligned = [align.Aligned(1.10, 1.40, -0.5, True), align.Aligned(1.80, 2.30, -9.0, True)]
    out = T.refine_times(words, aligned)
    assert (out[0].start, out[0].end) == (pytest.approx(1.10), pytest.approx(1.5))  # fim estendido até a próxima
    assert out[1].start == pytest.approx(1.5), "nota acústica baixa: fica o tempo do Whisper"


def test_refine_times_keeps_seg_and_doubt_and_joins_short_gaps_only():
    words = [Word(0.0, 0.3, "a", seg=2, doubt=True), Word(0.4, 0.7, "b", seg=2), Word(1.5, 1.8, "c", seg=3)]
    out = T.refine_times(words)
    assert [(w.seg, w.doubt) for w in out] == [(2, True), (2, False), (3, False)]
    assert out[0].end == pytest.approx(0.4), "pausa curta: o destaque segue até a próxima palavra"
    assert out[1].end == pytest.approx(0.7), "pausa longa fica"


def test_mark_doubts_flags_collapsed_sentence_and_low_confidence_words_only():
    segments = [
        _Segment(6.08, 7.92, [Word(6.08, 6.38, "Esse", 1.0), Word(6.38, 6.80, "top,", 1.0)]),
        _Segment(8.10, 8.46, REAL_COLLAPSE[2:7]),
        _Segment(17.11, 19.59, [Word(18.67, 19.09, "entendeu,", 1.0), Word(19.29, 19.59, "Gerson.", 0.51)]),
    ]
    words = T.mark_doubts(segments)
    by_seg = {}
    for w in words:
        by_seg.setdefault(w.seg, []).append(w)
    assert not any(w.doubt for w in by_seg[0])
    assert all(w.doubt for w in by_seg[1]), "frase amontoada/ritmo impossível: toda marcada"
    assert [w.doubt for w in by_seg[2]] == [False, True], "só a palavra de confiança baixa"


def test_word_dict_roundtrip_is_backward_compatible():
    old = {"start": 1.0, "end": 1.2, "text": "oi", "probability": 0.9}
    w = Word.from_dict(old)
    assert (w.seg, w.doubt) == (0, False)
    assert Word.from_dict(Word(1, 2, "x", 0.5, seg=4, doubt=True).to_dict()).doubt is True


def test_align_text_without_model_spreads_by_syllables_inside_window(tmp_path):
    audio = np.zeros(16000 * 5, dtype=np.float32)
    out = T.align_text(audio, ["laranjão,", "viu?", "laranjinha."], 1.0, 3.0, tmp_path / "whisper")
    assert [w.text for w in out] == ["laranjão,", "viu?", "laranjinha."]
    assert out[0].start == pytest.approx(1.0) and out[-1].end == pytest.approx(3.0)
    assert all(1.0 <= w.start < w.end <= 3.0 for w in out)
    assert out[1].end - out[1].start < out[2].end - out[2].start


def test_normalize_word_keeps_portuguese_letters_in_vocab_and_drops_punctuation():
    vocab = {c: i for i, c in enumerate(["<pad>", "|", "'", *"abcdefghijklmnopqrstuvwxyz", "ã", "ç", "é"])}
    assert align.normalize_word("Atenção,", vocab) == "atenção"
    assert align.normalize_word("Você?", vocab) == "voce", "ê fora do vocabulário vira a letra base"
    assert align.normalize_word("123", vocab) == ""


def test_viterbi_places_each_token_where_its_probability_peaks():
    # 3 tokens (1, 2, 1) em 12 quadros: picos em 2, 6 e 10; o resto é branco (0)
    T_, V = 12, 3
    logp = np.full((T_, V), np.log(0.01), dtype=np.float32)
    logp[:, 0] = np.log(0.98)
    for t, tok in ((2, 1), (6, 2), (10, 1)):
        logp[t, :] = np.log(0.01)
        logp[t, tok] = np.log(0.98)
    path = align._viterbi(logp, [1, 2, 1], blank=0)
    emitted = {int((s - 1) // 2): t for t, s in enumerate(path) if s % 2 == 1}
    assert emitted == {0: 2, 1: 6, 2: 10}


def test_chunks_merge_blocks_too_short_to_read_without_crossing_sentence_end():
    # caso real: "Ei," ficava sozinho 0,07 s na tela entre "É o mercegão." e "eu tô vendo"
    words = [Word(0.56, 0.78, "É"), Word(0.78, 1.00, "o"), Word(1.00, 1.67, "mercegão."),
             Word(1.67, 1.76, "Ei,"), Word(1.76, 1.85, "eu"), Word(1.85, 2.01, "tô"), Word(2.01, 2.15, "vendo"),
             Word(2.15, 2.25, "que"), Word(2.25, 2.51, "você")]
    chunks = T.chunk_words(words, max_words=3, max_chars=24)
    assert all(c.end - c.start >= 0.3 - 1e-9 or c is chunks[-1] for c in chunks)
    assert not any(c.text == "Ei," for c in chunks)
    assert chunks[0].text == "É o mercegão.", "não atravessa o fim da frase"
    assert all(len(c.words) <= 3 and len(c.text) <= 24 for c in chunks), "respeita os limites configurados"
