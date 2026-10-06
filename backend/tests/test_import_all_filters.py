""""Todos" até o fim real do perfil, filtro por tipo de mídia com quantidade do TIPO pedido, e fim anômalo da paginação.

Provider web REAL sobre um Instagram falso (httpx.MockTransport) e o import runner real. 100% offline, valores FAKE.
"""

from __future__ import annotations

import json

import pytest

from app.integrations.instagram.downloader import InstagramError
from app.integrations.instagram.web.provider import InstagramWebDownloader
from import_group_fakes import CookieSession, MultiProfileSite, Profile, closed, drive, web_app

USER = "perfil_teste"


def start(h, content: str, limit: int | None, profile: str = USER) -> int:
    response = h.client.post("/api/imports", json={"profile": profile, "content_type": content, "limit": limit})
    assert response.status_code == 202, response.text
    return response.json()["id"]


def finish(h, batch_id: int) -> dict:
    drive(h, closed(h, batch_id))
    return h.batch(batch_id)


def jobs_of(batch: dict) -> list[tuple[str, int, str]]:
    return [(j["source_post_id"], j["source_media_index"], j["media_type"])
            for j in sorted(batch["jobs"], key=lambda j: j["id"])]


def row(h, batch_id: int) -> dict:
    return dict(h.ctx.db.fetchone("SELECT * FROM batches WHERE id = ?", (batch_id,)))


# --------------------------------------------------------------------------- 1. "Todos" chega ao fim real


def test_all_lists_past_every_old_cap_in_a_single_stretch():
    # 900 páginas x 12 = 10.800 mídias num trecho só: mais que o antigo MAX_ITEMS (10.000) e que as 500 páginas
    # padrão do GraphQL. Só `has_next_page=false` encerra.
    site = MultiProfileSite({USER: Profile(900)}, record=False)
    dl = InstagramWebDownloader(CookieSession(), transport=site.transport, page_delay=(0, 0))
    checkpoints = []
    delivered = sum(1 for _ in dl.iter_profile_media(USER, "all", None, on_checkpoint=checkpoints.append))
    assert delivered == 10_800 and site.posts == 900
    assert dl.last_listing_stats["stop_reason"] == "end_of_profile"
    assert [cp.has_next for cp in checkpoints[-2:]] == [True, False] and len(checkpoints) == 900


def test_all_batch_reaches_the_real_end_of_the_profile(harness_factory):
    # 1.700 páginas (1 mídia cada): mais que o antigo teto proporcional de um lote de 10.000 (1.670 páginas).
    # O teto de páginas por hora (600) faria a 601a página ESPERAR de verdade; ele fica desligado só aqui (a
    # espera do teto com "Todos" é coberta em test_import_groups.py).
    site = MultiProfileSite({USER: Profile(1700, per_page=1)}, record=False)
    h = web_app(harness_factory, site, env={"INSTAGRAM_PAGE_RATE_LIMIT": "0"})
    batch_id = start(h, "all", None)
    drive(h, closed(h, batch_id), per_round=1000)
    batch = h.client.get(f"/api/batches/{batch_id}").json()
    assert batch["status"] == "completed" and batch["requested_limit"] is None
    assert batch["found"] == 1700 == site.posts and batch["listing_end"] == "profile_end"
    assert batch["progress"]["downloaded"] == 1700 and batch["error_code"] is None
    assert batch["listing_dropped"] == {"unexpected_total": 0, "reasons": {}, "pending": 0}


# --------------------------------------------------------------------------- 2. N do tipo pedido


def test_n_reels_means_n_reels_even_after_many_pages_of_photos(harness_factory):
    # 40 páginas só de fotos antes dos Reels: muito além das antigas 6 páginas improdutivas e das 20 de baixo
    # rendimento. "30 + Reels" = 30 REELS, não "examinar 30 posts".
    site = MultiProfileSite({USER: Profile(60, lambda page, i: "reel" if page > 40 else "image")})
    h = web_app(harness_factory, site)
    batch = finish(h, start(h, "reels", 30))
    assert batch["status"] == "completed" and batch["found"] == 30 and batch["listing_end"] == "limit"
    assert {kind for _code, _index, kind in jobs_of(batch)} == {"video"}
    assert site.posts == 43, "40 páginas de fotos + 3 de Reels; nenhuma página além do necessário"
    assert batch["listing_dropped"]["unexpected_total"] == 0


def test_n_photos_and_carousels_in_a_profile_full_of_reels(harness_factory):
    site = MultiProfileSite({USER: Profile(60, lambda page, i: "image" if page > 25 else "reel")})
    h = web_app(harness_factory, site)
    batch = finish(h, start(h, "photos_carousels", 20))
    assert batch["status"] == "completed" and batch["found"] == 20 and batch["listing_end"] == "limit"
    assert {kind for _code, _index, kind in jobs_of(batch)} == {"image"}
    assert site.posts == 27


# --------------------------------------------------------------------------- 3. perfil termina antes de N


def test_profile_ends_before_the_requested_quantity(harness_factory):
    # 37 Reels no perfil inteiro, pedidos 100: conclui normalmente com 37 e diz que o perfil chegou ao fim.
    site = MultiProfileSite({USER: Profile(10, lambda page, i: "reel" if (page, i) < (4, 1) else "image")})
    h = web_app(harness_factory, site)
    batch = finish(h, start(h, "reels", 100))
    assert batch["status"] == "completed" and batch["found"] == 37 and batch["requested_limit"] == 100
    assert batch["listing_end"] == "profile_end" and batch["error_code"] is None
    assert "37" in batch["error_message"], "mensagem informativa: o perfil terminou antes da quantidade pedida"
    assert site.posts == 10


# --------------------------------------------------------------------------- 4. carrossel


def mixed(page: int, i: int) -> str:
    return ("image", "reel", "carousel", "feed_video")[i]


def test_photos_and_carousels_takes_every_carousel_item_and_excludes_reels(harness_factory):
    site = MultiProfileSite({USER: Profile(1, mixed, per_page=4)})
    h = web_app(harness_factory, site)
    batch = finish(h, start(h, "photos_carousels", 100))
    code = "PERFILTESTEP00001N"
    # "Publicações do feed": foto simples + os 3 itens do carrossel (inclusive o VÍDEO do carrossel) + o vídeo
    # avulso antigo que não é Reel; só o Reel fica de fora.
    assert jobs_of(batch) == [(f"{code}00", 0, "image"), (f"{code}02", 0, "image"), (f"{code}02", 1, "video"),
                              (f"{code}02", 2, "image"), (f"{code}03", 0, "video")]
    assert batch["listing_end"] == "profile_end" and batch["progress"]["downloaded"] == 5


def test_reels_and_feed_posts_are_complementary(harness_factory):
    # Toda mídia suportada cai em exatamente um dos dois filtros: Reels + Publicações do feed = Todos.
    profiles = {name: Profile(3, mixed, per_page=4) for name in ("perfil_reels", "perfil_feed", "perfil_todos")}
    h = web_app(harness_factory, MultiProfileSite(profiles))
    reels = jobs_of(finish(h, start(h, "reels", None, profile="perfil_reels")))
    feed = jobs_of(finish(h, start(h, "photos_carousels", None, profile="perfil_feed")))
    everything = jobs_of(finish(h, start(h, "all", None, profile="perfil_todos")))
    strip = lambda jobs: sorted((code[-3:], index, kind) for code, index, kind in jobs)  # noqa: E731
    assert len(reels) == 3 and len(feed) == 15 and len(everything) == 18
    assert sorted(strip(reels) + strip(feed)) == strip(everything)


def test_reels_excludes_carousels_and_all_takes_everything(harness_factory):
    site = MultiProfileSite({USER: Profile(1, mixed, per_page=4), "outro_perfil": Profile(1, mixed, per_page=4)})
    h = web_app(harness_factory, site)
    reels = finish(h, start(h, "reels", 100))
    assert jobs_of(reels) == [("PERFILTESTEP00001N01", 0, "video")], "o vídeo do carrossel não é Reel"
    everything = finish(h, start(h, "all", None, profile="outro_perfil"))
    assert len(jobs_of(everything)) == 6   # foto + reel + 3 do carrossel + vídeo avulso


def test_limit_cuts_in_the_middle_of_a_carousel(harness_factory):
    # Carrossel conta por ITEM: o limite 2 corta o carrossel de 3 no segundo item (comportamento mantido).
    site = MultiProfileSite({USER: Profile(3, lambda page, i: "carousel", per_page=2)})
    h = web_app(harness_factory, site)
    batch = finish(h, start(h, "photos_carousels", 2))
    assert jobs_of(batch) == [("PERFILTESTEP00001N00", 0, "image"), ("PERFILTESTEP00001N00", 1, "video")]
    assert batch["found"] == 2 and batch["listing_end"] == "limit" and site.posts == 1


# --------------------------------------------------------------------------- 5. filtro não é descarte


def test_content_filter_is_not_counted_as_dropped(harness_factory):
    site = MultiProfileSite({USER: Profile(3, lambda page, i: "reel" if page == 3 else "image")})
    h = web_app(harness_factory, site)
    batch_id = start(h, "reels", 100)
    batch = finish(h, batch_id)
    assert batch["found"] == 12 and batch["listing_end"] == "profile_end"
    assert batch["listing_dropped"] == {"unexpected_total": 0, "reasons": {}, "pending": 0}
    stored = row(h, batch_id)
    assert stored["listing_dropped_unexpected"] == 0
    reasons = json.loads(stored["listing_dropped_reasons"])
    assert reasons == {"expected": {"content_filter": 24}}, "ignorado pelo filtro: esperado, fora do total de perdas"


@pytest.mark.parametrize("content, found", [("reels", 0), ("photos_carousels", 6), ("all", 6), ("videos", 6)])
def test_standalone_non_reel_video_is_a_feed_post_not_a_reel(harness_factory, content, found):
    # Vídeo avulso legado (sem product_type "clips", fora de carrossel): não é Reel. Entra em "Publicações do feed"
    # (`photos_carousels`), em "all" e no antigo "videos"; em "reels" é ignorado como `content_filter` (esperado),
    # nunca como descarte.
    site = MultiProfileSite({USER: Profile(2, lambda page, i: "feed_video", per_page=3)})
    h = web_app(harness_factory, site)
    batch_id = start(h, content, 100)
    batch = finish(h, batch_id)
    assert batch["status"] == "completed" and batch["found"] == found and batch["listing_end"] == "profile_end"
    assert batch["listing_dropped"] == {"unexpected_total": 0, "reasons": {}, "pending": 0}
    stored = row(h, batch_id)
    assert stored["listing_dropped_unexpected"] == 0
    reasons = json.loads(stored["listing_dropped_reasons"]) if stored["listing_dropped_reasons"] else {}
    assert reasons == ({} if found else {"expected": {"content_filter": 6}})


def test_real_discard_still_counts_with_a_filter(harness_factory):
    site = MultiProfileSite({USER: Profile(2, lambda page, i: "no_url" if (page, i) == (1, 0) else "reel",
                                           per_page=3)})
    h = web_app(harness_factory, site)
    batch = finish(h, start(h, "reels", 100))
    assert batch["found"] == 5 and batch["listing_dropped"]["reasons"] == {"image_no_url": 1}


# --------------------------------------------------------------------------- 14. fim anômalo x fim real


@pytest.mark.parametrize("breaks, found, next_page, final", [
    ({3: "empty"}, 24, 3, 72),              # página vazia com has_next_page=true
    ({3: "no_cursor"}, 36, 3, 72),          # sem cursor com has_next_page=true, sem `errors`
    ({3: "no_cursor_errors"}, 36, 3, 72),   # idem, com `errors`
    ({3: "repeat_cursor"}, 36, 3, 72),      # cursor repetido
    ({3: "dup", 4: "dup"}, 24, 4, 60),      # duas páginas seguidas sem post novo
])
def test_anomalous_end_goes_to_needs_attention_and_keeps_the_checkpoint(harness_factory, breaks, found, next_page,
                                                                        final):
    site = MultiProfileSite({USER: Profile(6, breaks=dict(breaks))})
    h = web_app(harness_factory, site)
    batch_id = start(h, "all", None)
    h.run_imports()
    stored = row(h, batch_id)
    assert (stored["status"], stored["listing_state"]) == ("listing", "needs_attention"), "NÃO concluído"
    assert stored["error_code"] == "contract_changed" and stored["error_message"]
    assert stored["listing_cursor"] and stored["listing_next_page"] == next_page, "checkpoint preservado"
    assert stored["listing_has_next"] == 1 and stored["listing_end"] is None and stored["finished_at"] is None
    assert stored["found"] == found and stored["listing_retry_at"] is not None, "nova tentativa só em 6 h"
    api = h.client.get(f"/api/batches/{batch_id}").json()
    assert (api["status"], api["listing_state"], api["listing_end"]) == ("listing", "needs_attention", None)
    asked = site.posts
    h.run_imports()
    h.run_jobs()
    h.run_imports()
    assert site.posts == asked, "nenhuma requisição imediata extra"
    assert row(h, batch_id)["status"] == "listing", "os downloads terminaram e o lote continua aberto"

    site.profiles[USER].breaks.clear()   # o Instagram voltou ao normal: retomada do checkpoint, sem duplicar
    assert h.client.post(f"/api/batches/{batch_id}/resume-listing").status_code == 200
    batch = finish(h, batch_id)
    assert batch["status"] == "completed" and batch["listing_end"] == "profile_end" and batch["error_code"] is None
    assert batch["found"] == final == len({(c, i) for c, i, _k in jobs_of(batch)})
    assert (USER, 1) not in site.order[asked:], "a retomada não recomeça da página 1"


def test_anomalous_end_on_the_first_page_without_items_fails_visibly(harness_factory):
    site = MultiProfileSite({USER: Profile(3, breaks={1: "empty"})})
    h = web_app(harness_factory, site)
    batch_id = start(h, "all", 50)
    h.run_imports()
    batch = h.batch(batch_id)
    assert batch["status"] == "failed", "nunca 'perfil vazio concluído'"
    assert batch["error_code"] == "contract_changed" and batch["error_message"]
    assert batch["found"] == 0 and batch["listing_end"] is None and site.posts == 1


def test_anomalous_end_on_the_first_page_with_items_is_not_concluded(harness_factory):
    site = MultiProfileSite({USER: Profile(3, breaks={1: "no_cursor"})})
    h = web_app(harness_factory, site)
    batch_id = start(h, "all", 50)
    h.run_imports()
    h.run_jobs()
    h.run_imports()
    batch = h.batch(batch_id)
    assert (batch["status"], batch["listing_state"]) == ("listing", "needs_attention")
    assert batch["error_code"] == "contract_changed" and batch["error_message"] and batch["found"] == 12
    assert batch["listing_end"] is None and site.posts == 1

    site.profiles[USER].breaks.clear()
    assert h.client.post(f"/api/batches/{batch_id}/resume-listing").status_code == 200
    batch = finish(h, batch_id)
    assert batch["status"] == "completed" and batch["found"] == 36 and batch["listing_end"] == "profile_end"


def test_provider_reports_the_anomaly_detail_and_only_real_end_closes_the_checkpoint():
    site = MultiProfileSite({USER: Profile(4, breaks={2: "empty"})})
    dl = InstagramWebDownloader(CookieSession(), transport=site.transport, page_delay=(0, 0))
    checkpoints = []
    with pytest.raises(InstagramError) as info:
        list(dl.iter_profile_media(USER, "all", None, on_checkpoint=checkpoints.append))
    assert info.value.code == "contract_changed" and info.value.diagnostic["detail"] == "empty_page"
    assert [cp.has_next for cp in checkpoints] == [True] and site.posts == 2


# --------------------------------------------------------------------------- trecho encerrado sem fim real


def test_page_cap_per_stretch_is_not_end_of_profile(harness_factory):
    # `max_pages` do construtor (só testes/POC) encerra o TRECHO; o lote abre outro trecho do checkpoint até o fim real.
    site = MultiProfileSite({USER: Profile(5)})
    h = web_app(harness_factory, site)
    h.downloader = InstagramWebDownloader(CookieSession(), cdn_downloader=h.cdn, transport=site.transport,
                                          page_delay=(0, 0), request_gate=h.ctx.instagram_gate, max_pages=2)
    batch = finish(h, start(h, "all", None))
    assert batch["status"] == "completed" and batch["found"] == 60 and batch["listing_end"] == "profile_end"
    assert site.order == [(USER, page) for page in range(1, 6)], "cada página uma vez, em 3 trechos"


def test_stretch_that_ends_without_progress_and_without_real_end_needs_attention(harness):
    # O provider devolve "nada" sem erro, mas o checkpoint diz que ainda há páginas: não é fim do perfil.
    batch_id = harness.import_profile(limit=None)["id"]
    harness.ctx.batches.update_listing(batch_id, listing_cursor="FAKE-CURSOR-2", listing_next_page=2,
                                       listing_pages=1, listing_has_next=1)
    harness.run_imports()
    batch = harness.batch(batch_id)
    assert (batch["status"], batch["listing_state"], batch["error_code"]) == ("listing", "needs_attention",
                                                                             "contract_changed")
    assert batch["listing_end"] is None and batch["error_message"]
    assert row(harness, batch_id)["listing_cursor"] == "FAKE-CURSOR-2"


# --------------------------------------------------------------------------- revisão: cancelamento, auto, tentativas


def test_batch_cancelled_after_being_chosen_is_not_listed(harness_factory):
    # Corrida: o runner já escolheu o lote, o cancelamento commita, e só então o trecho começaria.
    site = MultiProfileSite({USER: Profile(5)})
    h = web_app(harness_factory, site)
    batch_id = start(h, "all", None)
    chosen = h.ctx.import_runner._next_eligible()
    assert chosen["id"] == batch_id
    assert h.client.post(f"/api/batches/{batch_id}/cancel").status_code == 200
    h.ctx.import_runner._run_listing(chosen)
    assert site.posts == 0 and site.gets == 0, "zero requisições ao Instagram"
    stored = row(h, batch_id)
    assert (stored["status"], stored["listing_state"], stored["found"]) == ("cancelled", "cancelled", 0)
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM jobs") == 0 and h.ctx.import_runner.current_batch_id is None


def test_cancel_committed_in_the_middle_stops_at_the_page_boundary(harness_factory):
    # Cancelamento gravado no banco SEM o aviso em memória (pior caso): a conferência por página para a listagem.
    site = MultiProfileSite({USER: Profile(5)})
    h = web_app(harness_factory, site)
    batch_id = start(h, "all", None)

    def cancel_on_page_two(username: str, page: int) -> None:
        if page == 2:
            h.ctx.batches.update(batch_id, status="cancelled")

    site.on_post = cancel_on_page_two
    h.run_imports()
    assert site.posts == 2, "a página em voo termina; nenhuma página extra"
    stored = row(h, batch_id)
    assert (stored["status"], stored["listing_state"]) == ("cancelled", "cancelled")
    assert stored["listing_next_page"] == 2, "a página 2 não ganhou checkpoint"
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM jobs WHERE status <> 'cancelled'") == 0
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM jobs") == 12, "só os jobs da página 1 (criados antes do cancelamento)"


def test_auto_strategy_anomalous_end_does_not_fall_back_to_gallery_dl(harness_factory):
    from app.integrations.instagram.composite import CompositeInstagramDownloader
    from media_fakes import FakeInstagramDownloader, remote

    # Página 1 só com fotos (nenhum Reel entregue), página 2 vazia com has_next_page=true.
    site = MultiProfileSite({USER: Profile(4, breaks={2: "empty"})})
    h = web_app(harness_factory, site)
    legacy = FakeInstagramDownloader(items=[remote(f"LEG{i}", media_type="video") for i in range(9)])
    h.downloader = CompositeInstagramDownloader(h.downloader, legacy, strategy="auto")
    batch_id = start(h, "reels", 5)
    h.run_imports()
    batch = h.batch(batch_id)
    assert legacy.listings == 0, "o gallery-dl não 'conclui' no lugar do web"
    assert (batch["status"], batch["listing_state"], batch["error_code"]) == ("listing", "needs_attention",
                                                                             "contract_changed")
    assert batch["found"] == 0 and batch["listing_end"] is None and site.posts == 2


def test_automatic_attention_retries_stop_at_four_when_server_repeats_the_same_posts(harness_factory):
    # O servidor devolve sempre os mesmos posts com cursores novos: cada retomada ganha checkpoints mas nenhum job
    # novo. O contador de tentativas não zera; depois de 4 tentativas automáticas o lote espera ação do usuário.
    site = MultiProfileSite({USER: Profile(500, breaks={page: "dup" for page in range(3, 500)})})
    h = web_app(harness_factory, site)
    batch_id = start(h, "all", None)
    asked = []
    for _ in range(8):   # 1a listagem + 7 "vencimentos" das 6 h
        h.run_imports()
        asked.append(site.posts)
        h.ctx.db.execute("UPDATE batches SET listing_retry_at = '2000-01-01T00:00:00.000Z'"
                         " WHERE id = ? AND listing_retry_at IS NOT NULL", (batch_id,))
    stored = row(h, batch_id)
    assert (stored["status"], stored["listing_state"]) == ("listing", "needs_attention")
    assert stored["listing_retry_at"] is None and stored["listing_attention_tries"] == 4
    assert asked[4] == asked[5] == asked[6] == asked[7], "depois da 4a tentativa automática, nenhuma requisição"
    assert asked[4] > asked[3] > asked[0], "as 4 tentativas automáticas aconteceram"
    assert stored["found"] == 24, "nada duplicado"


def test_attention_retries_restart_after_real_progress(harness_factory):
    site = MultiProfileSite({USER: Profile(8, breaks={3: "empty"})})
    h = web_app(harness_factory, site)
    batch_id = start(h, "all", None)
    h.run_imports()
    assert row(h, batch_id)["listing_attention_tries"] == 1
    site.profiles[USER].breaks = {6: "empty"}   # a retomada avança de verdade (jobs novos) e quebra mais adiante
    h.ctx.db.execute("UPDATE batches SET listing_retry_at = '2000-01-01T00:00:00.000Z' WHERE id = ?", (batch_id,))
    h.run_imports()
    stored = row(h, batch_id)
    assert stored["found"] == 60 and stored["listing_state"] == "needs_attention"
    assert stored["listing_attention_tries"] == 1, "progresso real zerou o contador antes da nova anomalia"
