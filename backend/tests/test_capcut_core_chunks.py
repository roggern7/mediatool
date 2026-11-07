"""Núcleo CapCut: divisão das palavras transcritas em blocos de legenda (chunk_words)."""

from __future__ import annotations

import pytest

from app.integrations.capcut.transcribe import Word, chunk_words


def W(*items):
    return [Word(a, b, t, 0.9) for a, b, t in items]


def texts(chunks):
    return [c.text for c in chunks]


def assert_no_overlap(chunks, gap=0.02):
    for a, b in zip(chunks, chunks[1:]):
        assert a.end <= b.start - gap + 1e-9, (a, b)
    for c in chunks:
        assert c.end > c.start


def test_breaks_on_pause_and_limits():
    ws = W((0.0, 0.2, "um"), (0.2, 0.4, "dois"), (0.4, 0.6, "três"), (0.6, 0.95, "quatro"),
           (1.4, 1.6, "cinco"))  # pausa de 400 ms antes de "cinco"
    chunks = chunk_words(ws, max_words=3, max_chars=24)
    assert texts(chunks) == ["um dois três", "quatro", "cinco"]
    assert_no_overlap(chunks)
    # pausa >= 180 ms: o bloco termina na última palavra + 50 ms
    assert chunks[1].end == pytest.approx(1.0)
    # bloco curto ganha a duração mínima de 300 ms
    assert chunks[2].end == pytest.approx(1.7)


def test_max_chars():
    ws = W((0.0, 0.3, "extraordinariamente"), (0.3, 0.6, "bonito"), (0.6, 0.9, "sim"))
    chunks = chunk_words(ws, max_words=3, max_chars=24)
    assert texts(chunks) == ["extraordinariamente", "bonito sim"]
    assert all(len(c.text) <= 24 for c in chunks)


def test_prefers_break_after_punctuation():
    ws = W((0.0, 0.2, "Pra"), (0.2, 0.4, "cá,"), (0.4, 0.5, "pra"), (0.5, 0.8, "lá"), (0.8, 1.0, "agora"))
    assert texts(chunk_words(ws, max_words=3)) == ["Pra cá,", "pra lá agora"]
    # fim de frase sempre quebra
    ws = W((0.0, 0.2, "Nada."), (0.2, 0.4, "Rua"), (0.4, 0.6, "boa"))
    assert texts(chunk_words(ws, max_words=3)) == ["Nada.", "Rua boa"]


def test_short_preposition_not_left_alone_at_end():
    ws = W((0.0, 0.2, "sabe"), (0.2, 0.4, "que"), (0.4, 0.5, "fica"), (0.5, 0.6, "a"), (0.6, 0.8, "rua"),
           (0.8, 0.9, "da"), (0.9, 1.3, "Conceição?"))
    chunks = chunk_words(ws, max_words=3)
    assert texts(chunks) == ["sabe que fica", "a rua", "da Conceição?"]
    for c in chunks:
        assert c.text.split()[-1].lower() not in {"a", "da", "de", "que"}
    assert_no_overlap(chunks)


def test_timing_tail_gap_and_min_duration():
    ws = W((1.0, 1.1, "Oi"), (1.15, 1.3, "você"))
    chunks = chunk_words(ws, max_words=1)
    # "Oi": fim = 1.1 + 0.05 = 1.15, mas nunca além do próximo - 20 ms
    assert chunks[0].start == pytest.approx(1.0)
    assert chunks[0].end == pytest.approx(1.13)
    # "você": duração mínima de 300 ms (sem próximo)
    assert chunks[1].end == pytest.approx(1.45)
    assert_no_overlap(chunks)


def test_overlapping_words_never_produce_overlapping_blocks():
    ws = W((0.0, 0.5, "a."), (0.4, 0.6, "b."), (0.41, 0.42, "c."), (0.43, 0.9, "d."))
    chunks = chunk_words(ws, max_words=3)
    assert len(chunks) == 4
    assert_no_overlap(chunks)


def test_clean_text_and_empty_input():
    assert chunk_words([]) == []
    ws = W((0.0, 0.2, "  Bom "), (0.2, 0.22, ""), (0.25, 0.6, " dia\n"))
    chunks = chunk_words(ws)
    assert texts(chunks) == ["Bom dia"]
    assert [w.text for w in chunks[0].words] == ["  Bom ", " dia\n"]


def test_real_transcript_sample():
    """Palavras reais do medium no vídeo de teste (prova de conceito)."""
    sample = [(0.69, 1.39, "Bom"), (1.39, 3.29, "dia"), (3.29, 3.63, "gente,"), (3.87, 4.01, "sabe"),
              (4.01, 4.21, "onde"), (4.21, 4.33, "é"), (4.33, 4.37, "que"), (4.37, 4.63, "fica"), (4.63, 4.75, "a"),
              (4.75, 4.83, "rua"), (4.83, 5.19, "da"), (5.19, 5.77, "Conceição?"), (6.43, 6.61, "Onde"),
              (8.91, 8.97, "A"), (8.97, 9.29, "próxima?")]
    chunks = chunk_words(W(*sample))
    assert texts(chunks) == ["Bom dia gente,", "sabe onde é", "que fica", "a rua", "da Conceição?", "Onde",
                             "A próxima?"]
    assert_no_overlap(chunks)
    assert all(len(c.words) <= 3 and len(c.text) <= 24 for c in chunks)
