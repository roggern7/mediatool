"""Escala da exportação para pasta: 10.000 arquivos pequenos com memória plana e poucas escritas de progresso."""

from __future__ import annotations

import os
import time
import tracemalloc

import pytest

from app.jobs import export_runner
from export_helpers import prepare, run_exports, seed_media, start_export

TOTAL = 10_000


@pytest.mark.slow   # ~2 min nesta máquina: o tempo é do sistema de arquivos (abrir/criar 20.000 arquivos)
def test_ten_thousand_files_with_flat_memory_and_batched_progress(harness, tmp_path, monkeypatch):
    dest = prepare(harness, tmp_path)
    seed_media(harness, TOTAL)
    created = start_export(harness, {"kind": "library", "selection": {"filter": {"view": "all"}}}, dest)
    assert created["total"] == TOTAL
    # Seleção por filtro: nenhum id foi materializado para a exportação.
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM export_items") == 0

    runner = harness.ctx.export_runner
    samples: list[tuple[int, int]] = []
    pages: list[int] = []
    real_flush, real_page = runner._flush, harness.ctx.exports.page

    def traced_bytes() -> int:
        # Fora da conta: a tabela de strings internadas do CPython, que o pathlib alimenta com cada nome de arquivo
        # distinto e que cresce em poucos blocos grandes (redimensionamento), em momentos que dependem do que o
        # processo já fez antes. Não é memória retida pela exportação.
        snapshot = tracemalloc.take_snapshot().filter_traces([tracemalloc.Filter(False, "*pathlib*")])
        return sum(stat.size for stat in snapshot.statistics("filename"))

    def flush(export_id, counts):
        real_flush(export_id, counts)
        shown = harness.client.get(f"/api/exports/{export_id}").json()   # o que a tela veria agora
        assert shown["status"] == "running"
        samples.append((shown["processed"], traced_bytes()))

    def page(export, **kwargs):
        rows = real_page(export, **kwargs)
        pages.append(len(rows))
        return rows

    monkeypatch.setattr(runner, "_flush", flush)
    monkeypatch.setattr(harness.ctx.exports, "page", page)
    tracemalloc.start()
    started = time.monotonic()
    try:
        run_exports(harness)
    finally:
        elapsed = time.monotonic() - started
        top = [str(stat) for stat in tracemalloc.take_snapshot().statistics("lineno")[:8]]
        tracemalloc.stop()

    export = harness.client.get(f"/api/exports/{created['id']}").json()
    assert (export["status"], export["copied"], export["processed"], export["failed"]) == ("completed", TOTAL, TOTAL, 0)
    assert len(os.listdir(dest)) == TOTAL
    # Leitura paginada por keyset: páginas de no máximo PAGE_SIZE, nunca a lista inteira.
    assert max(pages) == export_runner.PAGE_SIZE and sum(pages) == TOTAL
    # Progresso crescente e escritas limitadas (a cada N arquivos ou a cada intervalo), não uma por arquivo.
    progress = [processed for processed, _ in samples]
    assert progress == sorted(set(progress)) and 0 < progress[0] < TOTAL
    limit = TOTAL / export_runner.PROGRESS_EVERY_FILES + elapsed / export_runner.PROGRESS_EVERY_SECONDS + 2
    assert len(samples) == runner.progress_writes <= limit, (len(samples), limit)
    # Memória plana: o uso no fim não cresce com o número de arquivos já copiados.
    early = samples[len(samples) // 10][1]
    late = max(used for _, used in samples[len(samples) // 2:])
    assert late - early < 2 * 1024 * 1024, (early, late, top)
    print(f"\n{TOTAL} arquivos exportados em {elapsed:.1f} s; {len(samples)} escritas de progresso;"
          f" memória {early / 1024:.0f} KiB -> {late / 1024:.0f} KiB")
