"""Escala (lote de 1.000 midias, offline): backpressure, reinicio, cancelamento, URLs expiradas, API paginada.

Marcado slow (pytest -m slow tests/scale). Tamanhos maiores e relatorio JSON: tools/scale_bench.py.
Criterios da API nova (seção 6 de docs/BATCH_ROBUSTNESS.md) viram xfail enquanto a API nao existir.
"""

from __future__ import annotations

import logging
import math

import pytest

from scale_harness import scenario

pytestmark = pytest.mark.slow
N = 1000


@pytest.fixture(scope="module")
def clean(tmp_path_factory):
    return scenario("clean", N, tmp_path_factory.mktemp("clean"))


def test_clean_batch_completes_without_repeats_and_with_backpressure(clean):
    r = clean
    assert r["batch_status"] == "completed" and r["found"] == N and r["listing_state"] == "done"
    assert r["downloads_ok"] == N and r["downloads_repeated"] == 0 and r["items_never_downloaded"] == 0
    assert r["duplicate_jobs"] == 0 and r["job_statuses"] == {"completed": N}
    assert r["max_pending_ingest"] <= 100
    assert r["pages_requested"] == math.ceil(N / 12) and r["extra_page_requests"] == 0
    assert r["jobs_with_media_url_left"] in (0, None) and r["job_remote_refs_rows_left"] in (0, None)


def test_clean_cost_is_linear(clean):
    """A 2a metade do lote nao pode custar muito mais (queries e tempo) que a 1a: sem degradacao estrutural."""
    pts = [p for p in clean["progress"] if p["completed"] > 0]
    half = next(p for p in pts if p["completed"] >= N // 2)
    end = pts[-1]
    assert (end["queries"] - half["queries"]) <= 1.5 * half["queries"]
    assert (end["elapsed_s"] - half["elapsed_s"]) <= 2.0 * max(half["elapsed_s"], 0.5)


def test_api_batch_summary_is_constant_size(clean):
    api = clean["api"]
    sizes = [api[k]["batch_summary"]["bytes"] for k in ("at_25pct", "at_50pct", "final")]
    if any(api[k]["batch_summary"]["jobs_in_body"] for k in ("at_25pct", "final")):
        pytest.xfail("GET /api/batches/{id} ainda devolve jobs (include_jobs nao implementado)")
    assert max(sizes) <= 1.2 * min(sizes)


def test_api_jobs_keyset_pagination(clean):
    page = clean["api"]["final"]["jobs_page"]
    if not page["has_next_before_id_key"]:
        pytest.xfail("GET /api/jobs sem next_before_id (keyset nao implementado)")
    assert page["items"] == 100 and page["next_before_id"]
    assert clean["api"]["final"]["jobs_keyset_page2"]["items"] == 100
    api = clean["api"]
    assert api["final"]["jobs_page"]["bytes"] <= 1.3 * api["at_25pct"]["jobs_page"]["bytes"]


def test_restart_resumes_from_checkpoint_without_repeating_downloads(tmp_path):
    r = scenario("restart", N, tmp_path)
    assert r["batch_status"] == "completed" and r["found"] == N
    assert r["downloads_repeated"] == 0 and r["items_never_downloaded"] == 0 and r["duplicate_jobs"] == 0
    info = r["restart"]
    assert info["first_page_requested_after_restart"] == info["next_page_saved"] > 1
    assert r["extra_page_requests"] <= 1, "no maximo a pagina parcial e pedida de novo"
    assert r["max_pending_ingest"] <= 100


def test_cancel_mid_batch_stops_listing_and_downloads(tmp_path):
    r = scenario("cancel", N, tmp_path)
    c = r["cancel"]
    assert c["http"] == 200 and c["batch_status"] == "cancelled"
    assert c["downloads_after_cancel"] == 0 and c["pages_after_cancel"] == 0 and c["non_terminal_after"] == 0
    assert r["downloads_repeated"] == 0


def test_expired_urls_refresh_at_most_once_per_page(tmp_path):
    r = scenario("expired", N, tmp_path)
    f = r["refresh"]
    assert r["batch_status"] == "completed" and r["downloads_ok"] == N and r["downloads_repeated"] == 0
    assert f["expired_hits"] == f["expired_items"] - 0 or f["expired_hits"] <= f["expired_items"]
    assert f["max_refresh_per_page"] <= 1 and f["refresh_calls"] <= f["pages_total"]


def test_unproductive_pages_do_not_stall_listing(tmp_path):
    r = scenario("unproductive", N, tmp_path)
    assert r["batch_status"] == "completed" and r["found"] == N and r["downloads_repeated"] == 0
    assert r["pages_requested"] == (math.ceil(N / 12) - 1) * 4 + 1 and r["max_pending_ingest"] <= 100


# ---- download-only (fluxo padrão): 1.000 e 10.000 itens sem FFmpeg ----------------------------------------------
@pytest.mark.parametrize("total", [1000, 10000])
def test_download_only_batch_never_touches_ffmpeg_and_memory_stays_flat(tmp_path, total):
    """Corpo padrão da API (sem `process`/`preset_id`) e só o pool de download rodando: o lote conclui sem job de
    processamento, sem rendition, sem chamada ao FFmpeg; o backpressure é só o de download; memória plana."""
    # Os registros INFO de cada job ficariam retidos pela captura de log do pytest (não pelo app) e inflariam o RSS.
    logging.disable(logging.INFO)
    try:
        r = scenario("download_only", total, tmp_path)
    finally:
        logging.disable(logging.NOTSET)
    assert r["api_accepts_limit"] and r["batch_status"] == "completed" and r["listing_state"] == "done"
    assert r["found"] == total and r["media_rows"] == total and r["job_statuses"] == {"completed": total}
    assert r["downloads_ok"] == total and r["downloads_repeated"] == 0 and r["duplicate_jobs"] == 0
    assert r["download_only"] == {
        "batch_process": False, "process_jobs": 0, "jobs_with_processing": 0, "renditions": 0,
        "ffmpeg_process_calls": 0, "ffmpeg_pool_claimable": False, "process_backlog": 0,
    }
    # Backpressure só de download: a listagem encheu a fila (parou e retomou) sem passar do teto por lote.
    assert 40 <= r["max_pending_ingest"] <= 100
    assert r["pages_requested"] == math.ceil(total / 12) and r["extra_page_requests"] == 0
    # Custo linear e memória plana: do 1o terço ao fim o RSS não acompanha o número de itens.
    pts = [p for p in r["progress"] if p["completed"] > 0]
    early = next(p for p in pts if p["completed"] >= total * 0.3)
    half = next(p for p in pts if p["completed"] >= total // 2)
    end = pts[-1]
    assert (end["queries"] - half["queries"]) <= 1.5 * half["queries"]
    if end["rss_mb"] is not None and early["rss_mb"] is not None:
        assert end["rss_mb"] - early["rss_mb"] <= 25, (early["rss_mb"], end["rss_mb"])
