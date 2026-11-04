"""Horário dos posts (migration 0026): gravação na listagem por perfil (um registro por publicação, sem duplicar na
retomada), modo "só padrões" (`media: "none"`: nada baixado, nenhum job), validação do pedido e o ZIP do relatório
(GET /api/post-times/report.zip). 100% offline, valores FAKE."""

from __future__ import annotations

import io
import zipfile
from dataclasses import replace

import pytest

from app.integrations.instagram.downloader import InstagramError
from media_fakes import FakeInstagramDownloader, remote


class PostTimesDownloader(FakeInstagramDownloader):
    """Listagem FAKE que chama `on_post` UMA vez por publicação (antes dos itens dela), como o provider web."""

    def iter_profile_media(self, username, content, limit, *, cancel=None, resume=None, on_checkpoint=None,
                           on_post=None, **extra):
        inner = super().iter_profile_media(username, content, limit, cancel=cancel, resume=resume,
                                           on_checkpoint=on_checkpoint)
        seen: set[str] = set()
        for item in inner:
            if on_post is not None and item.source_post_id not in seen:
                seen.add(item.source_post_id)
                on_post({"code": item.source_post_id, "taken_at": item.taken_at,
                         "kind": "carousel" if item.source_post_id == "CARR" else "photo", "pinned": False})
            yield item


def items(count: int = 8) -> list:
    """`count` publicações de uma foto (P0 mais nova) e um carrossel de 2 itens no meio (conta UMA publicação)."""
    result = [replace(remote(f"P{i}"), taken_at=f"2026-10-0{1 + i % 9}T1{i % 10}:00:00Z") for i in range(count)]
    result[3:3] = [replace(remote("CARR", index), taken_at="2026-10-03T08:00:00Z") for index in (0, 1)]
    return result


def start(h, expect: int = 202, **body) -> dict:
    response = h.client.post("/api/imports/start", json=body)
    assert response.status_code == expect, response.text
    return response.json()


def rows(h, batch_id: int) -> list[dict]:
    return [dict(row) for row in h.ctx.db.fetchall(
        "SELECT username, code, taken_at, kind, pinned FROM post_times WHERE batch_id = ? ORDER BY id", (batch_id,))]


def jobs(h, batch_id: int) -> int:
    return h.ctx.db.scalar("SELECT COUNT(*) FROM jobs WHERE batch_id = ?", (batch_id,))


def test_patterns_only_records_times_creates_no_job_and_completes(harness_factory):
    dl = PostTimesDownloader(items=items(), page_size=3)
    h = harness_factory(downloader=dl)
    created = start(h, inputs=["@perfil_teste"], media="none", post_times=True, limit=5)
    batch_id = created["batch_ids"][0]
    h.run_all()

    assert jobs(h, batch_id) == 0 and dl.downloads == []
    stored = rows(h, batch_id)
    # 5 PUBLICAÇÕES (o carrossel de 2 itens conta uma vez), na ordem da listagem.
    assert [row["code"] for row in stored] == ["P0", "P1", "P2", "CARR", "P3"]
    assert all(row["username"] == "perfil_teste" and row["pinned"] == 0 for row in stored)
    assert stored[3]["kind"] == "carousel" and stored[3]["taken_at"] == "2026-10-03T08:00:00Z"
    batch = h.batch(batch_id)
    assert batch["status"] == "completed" and batch["listing_end"] == "limit"
    assert batch["post_times"] is True and batch["post_times_count"] == 5
    assert batch["media"] == "none" and batch["media_set"] == "none"
    assert batch["found"] == 0 and batch["error_message"].startswith("5 publicações analisadas")


def test_patterns_only_until_profile_end(harness_factory):
    dl = PostTimesDownloader(items=items(4), page_size=2)
    h = harness_factory(downloader=dl)
    batch_id = start(h, inputs=["perfil_teste"], media="none", post_times=True, limit=None,
                     confirm_all=True)["batch_ids"][0]
    h.run_all()
    batch = h.batch(batch_id)
    assert batch["status"] == "completed" and batch["listing_end"] == "profile_end"
    assert batch["post_times_count"] == 5 and jobs(h, batch_id) == 0


def test_normal_import_with_post_times_downloads_and_records_every_post(harness_factory):
    dl = PostTimesDownloader(items=items(), page_size=4)
    h = harness_factory(downloader=dl)
    batch_id = start(h, inputs=["@perfil_teste"], media="all", post_times=True, limit=100)["batch_ids"][0]
    for _ in range(3):
        h.run_all()
    batch = h.batch(batch_id)
    assert batch["status"] == "completed"
    assert jobs(h, batch_id) == 10 and len(dl.downloads) == 10   # 8 fotos + 2 itens do carrossel
    assert len(rows(h, batch_id)) == 9 and batch["post_times_count"] == 9   # 9 publicações
    assert batch["post_times"] is True and batch["media"] == "all" and batch["media_set"] == "all"


def test_import_without_post_times_records_nothing(harness_factory):
    dl = PostTimesDownloader(items=items(3))
    h = harness_factory(downloader=dl)
    batch_id = start(h, inputs=["@perfil_teste"], media="all", limit=100)["batch_ids"][0]
    h.run_all()
    batch = h.batch(batch_id)
    assert rows(h, batch_id) == [] and batch["post_times"] is False and batch["post_times_count"] == 0
    response = h.client.get("/api/post-times/report.zip", params={"batch_id": batch_id})
    assert response.status_code == 404 and response.json()["code"] == "post_times_not_found"


def test_resume_after_error_mid_page_does_not_duplicate(harness_factory):
    # Erro no meio da página 2 (depois de 4 itens): a página 2 já teve publicações gravadas sem checkpoint; a retomada
    # refaz a página e nada é duplicado.
    dl = PostTimesDownloader(items=items(), page_size=3, listing_error=InstagramError("contract_changed", "FAKE"),
                             listing_error_after=4)
    h = harness_factory(downloader=dl)
    batch_id = start(h, inputs=["@perfil_teste"], media="none", post_times=True, limit=None,
                     confirm_all=True)["batch_ids"][0]
    h.run_imports()
    first = h.batch(batch_id)
    assert first["status"] == "listing" and first["listing_state"] == "needs_attention"
    assert [row["code"] for row in rows(h, batch_id)] == ["P0", "P1", "P2", "CARR"]

    dl.listing_error = None
    response = h.client.post(f"/api/batches/{batch_id}/resume-listing")
    assert response.status_code in (200, 202, 204), response.text
    h.run_all()
    codes = [row["code"] for row in rows(h, batch_id)]
    assert codes == ["P0", "P1", "P2", "CARR", "P3", "P4", "P5", "P6", "P7"]
    assert len(codes) == len(set(codes)) and dl.page_requests.count(2) == 2   # página 2 refeita
    batch = h.batch(batch_id)
    assert batch["status"] == "completed" and batch["post_times_count"] == 9 and jobs(h, batch_id) == 0


def test_group_patterns_only_sums_children(harness_factory):
    dl = PostTimesDownloader(items=items(3))
    h = harness_factory(downloader=dl)
    group_id = start(h, inputs=["@perfil_um", "@perfil_dois"], media="none", post_times=True, limit=10)["group_id"]
    h.run_all()
    h.run_all()
    detail = h.client.get(f"/api/import-groups/{group_id}").json()
    assert detail["group"]["post_times"] is True and detail["group"]["post_times_count"] == 8
    assert detail["group"]["media"] == "none" and detail["group"]["media_set"] == "none"
    assert {child["source_profile"] for child in detail["children"]} == {"perfil_um", "perfil_dois"}
    assert all(child["post_times_count"] == 4 and child["status"] == "completed" for child in detail["children"])
    assert dl.downloads == []


@pytest.mark.parametrize("body, code", [
    ({"inputs": ["@perfil"], "media": "none"}, "validation_error"),
    ({"inputs": ["@perfil"], "media": "none", "post_times": True, "post_processing": "preset"}, "validation_error"),
    ({"inputs": ["@perfil"], "media": "none", "post_times": True, "auto_export": {"content": "originals"}},
     "validation_error"),
    ({"inputs": ["@perfil"], "media": "none", "post_times": True, "min_views": 1000}, "validation_error"),
    ({"inputs": ["https://www.instagram.com/p/ABCDE12345/"], "media": "none", "post_times": True},
     "patterns_need_profiles"),
    ({"inputs": ["@perfil"], "media": "videos", "post_times": "sim"}, "validation_error"),
])
def test_invalid_requests_are_422(harness, body, code):
    body.setdefault("limit", 10)
    response = harness.client.post("/api/imports/start", json=body)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == code
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0


def test_report_endpoint_validation(harness):
    for params in ({}, {"batch_id": 1, "group_id": 1}):
        response = harness.client.get("/api/post-times/report.zip", params=params)
        assert response.status_code == 422, response.text
    assert harness.client.get("/api/post-times/report.zip", params={"batch_id": 999}).status_code == 404
    assert harness.client.get("/api/post-times/report.zip", params={"group_id": 999}).status_code == 404


def test_report_zip_for_batch_and_group(harness_factory):
    pytest.importorskip("app.services.post_patterns")
    dl = PostTimesDownloader(items=items())
    h = harness_factory(downloader=dl)
    batch_id = start(h, inputs=["@perfil_teste"], media="none", post_times=True, limit=100)["batch_ids"][0]
    h.run_all()
    group_id = start(h, inputs=["@perfil_um", "@perfil_dois"], media="none", post_times=True,
                     limit=100)["group_id"]
    h.run_all()
    h.run_all()
    for params, profiles in (({"batch_id": batch_id}, ["perfil_teste"]),
                             ({"group_id": group_id}, ["perfil_um", "perfil_dois"])):
        response = h.client.get("/api/post-times/report.zip", params=params)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/zip"
        disposition = response.headers["content-disposition"]
        assert disposition.startswith("attachment;") and ".zip" in disposition
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        expected = {f"{p}/{name}" for p in profiles for name in (f"{p}.xlsx", "padroes.txt", "posts.csv",
                                                                   "sequencia.csv")}
        if len(profiles) >= 2:
            expected |= {"comparativo.xlsx", "comparativo.txt"}
            assert all(f"@{p}" in files["comparativo.txt"].decode("utf-8") for p in profiles)
        assert set(files) == expected
        for profile in profiles:
            posts = files[f"{profile}/posts.csv"]
            assert posts.startswith(b"\xef\xbb\xbf")   # BOM (Excel pt-BR)
            posts_text = posts.decode("utf-8-sig")
            assert ";" in posts_text.splitlines()[0]
            assert len(posts_text.strip().splitlines()) == 1 + 9   # cabeçalho + 9 publicações do perfil
            assert all(f"/p/{code}/" in posts_text for code in ("P0", "P7", "CARR"))
            report = files[f"{profile}/padroes.txt"].decode("utf-8")
            assert f"=== @{profile} ===" in report and report.count("=== @") == 1


def test_web_provider_calls_on_post_once_per_publication_before_type_filter():
    """Pilha real do provider web (transporte FAKE): um registro por publicação, antes do filtro de tipo (conteúdo
    "videos" entrega só o Reel, mas foto e carrossel também têm horário); carrossel conta uma vez; fora do período
    não entra; fixada é marcada."""
    import test_graphql_partial_errors as gp
    from app.integrations.instagram.web.provider import LISTING_PINNED_FIELD

    base = 1_800_000_000
    photo = {**gp.image(1, 0), "taken_at": base}
    reel = {**gp.image(1, 1), "taken_at": base - 60, "media_type": 2, "product_type": "clips",
            "video_versions": [{"url": f"{gp.SIGNED_URL}&v=11", "width": 720, "height": 1280}]}
    carousel = {**gp.carousel(1, 2, [gp.image(1, 90), gp.image(1, 91)]), "taken_at": base - 120}
    pinned_old = {**gp.image(1, 3), "taken_at": base - 86_400 * 30, LISTING_PINNED_FIELD: ["1"]}
    site = gp.Site(gp.body(1, [photo, reel, carousel, pinned_old], has_next=False))
    records: list[dict] = []
    items = list(gp.downloader(site).iter_profile_media("perfil_teste", "videos", None, on_post=records.append))
    assert gp.codes(items) == ["SYNP01N01"]
    assert [(r["code"], r["kind"], r["pinned"]) for r in records] == [
        ("SYNP01N00", "photo", None), ("SYNP01N01", "reel", None), ("SYNP01N02", "carousel", None),
        ("SYNP01N03", "photo", True)]
    assert records[0]["taken_at"] == "2027-01-15T08:00:00Z"

    records.clear()
    cutoff = "2027-01-01T00:00:00Z"   # a fixada antiga fica fora do período
    list(gp.downloader(gp.Site(gp.body(1, [photo, reel, carousel, pinned_old], has_next=False))).iter_profile_media(
        "perfil_teste", "all", None, posted_after=cutoff, on_post=records.append))
    assert [r["code"] for r in records] == ["SYNP01N00", "SYNP01N01", "SYNP01N02"]


# ---------------------------------------------------------------- etapa "aba Reels" (migration 0027)

class ReelsTabDownloader(PostTimesDownloader):
    """Grade FAKE (PostTimesDownloader) + aba Reels FAKE (`iter_reels_post_times`) + `resolve_post` dos Reels."""

    def iter_reels_post_times(self, username, *, limit, posted_after=None, cancel=None):
        self.reels_calls.append(limit)
        for number, record in enumerate(self.reels):
            if self.reels_error is not None and number >= self.reels_error_after:
                raise self.reels_error
            yield {"kind": "reel", "pinned": False, "in_grid": None, "play_count": None, "source": "reels_tab",
                   **record}
        if self.reels_error is not None and self.reels_error_after >= len(self.reels):
            raise self.reels_error

    def resolve_post(self, shortcode, *, cancel=None, path_kind=None, **extra):
        self.resolved.append((shortcode, path_kind))
        if shortcode in self.resolve_errors:
            raise self.resolve_errors[shortcode]
        return [video(shortcode, self.reel_times.get(shortcode, "2026-10-01T00:00:00Z"))]


def video(code: str, taken_at: str):
    return replace(remote(code, media_type="video"), taken_at=taken_at,
                   source_url=f"https://www.instagram.com/reel/{code}/")


def reels_dl(grid: list, reels: list[dict], *, error=None, error_after: int = 0, resolve_errors=None,
             **kwargs) -> ReelsTabDownloader:
    dl = ReelsTabDownloader(items=grid, **kwargs)
    dl.reels, dl.reels_error, dl.reels_error_after = reels, error, error_after
    dl.resolve_errors, dl.reels_calls, dl.resolved = dict(resolve_errors or {}), [], []
    dl.reel_times = {record["code"]: record["taken_at"] for record in reels if record.get("taken_at")}
    return dl


def reel(code: str, taken_at: str, **extra) -> dict:
    return {"code": code, "taken_at": taken_at, **extra}


def sources(h, batch_id: int) -> dict[str, tuple]:
    return {row["code"]: (row["taken_at"], row["source"]) for row in h.ctx.db.fetchall(
        "SELECT code, taken_at, source FROM post_times WHERE batch_id = ?", (batch_id,))}


def job_codes(h, batch_id: int) -> set[str]:
    return {row["source_post_id"] for row in h.ctx.db.fetchall(
        "SELECT source_post_id FROM jobs WHERE batch_id = ?", (batch_id,))}


def test_reels_only_account_patterns_records_reels_tab_times(harness_factory):
    # Grade vazia (conta só de Reels) + 5 Reels na aba: 5 horários estimados, lote concluído, nada baixado.
    dl = reels_dl([], [reel(f"R{i}", f"2026-10-1{i}T10:00:00Z") for i in range(5)])
    h = harness_factory(downloader=dl)
    batch_id = start(h, inputs=["@perfil_teste"], media="none", post_times=True, limit=None,
                     confirm_all=True)["batch_ids"][0]
    h.run_all()
    stored = sources(h, batch_id)
    assert len(stored) == 5 and {source for _, source in stored.values()} == {"reels_tab"}
    batch = h.batch(batch_id)
    assert batch["status"] == "completed" and batch["post_times_count"] == 5 and jobs(h, batch_id) == 0
    assert batch["error_message"].startswith("5 publicações analisadas") and dl.resolved == []


def test_reels_tab_overlap_keeps_grid_time_without_duplicates(harness_factory):
    dl = reels_dl(items(3), [reel("R0", "2026-10-20T10:00:00Z"), reel("P1", "2026-10-02T11:00:20Z"),
                             reel("R1", "2026-09-20T10:00:00Z")])
    h = harness_factory(downloader=dl)
    batch_id = start(h, inputs=["@perfil_teste"], media="none", post_times=True, limit=None,
                     confirm_all=True)["batch_ids"][0]
    h.run_all()
    stored = sources(h, batch_id)
    assert set(stored) == {"P0", "P1", "P2", "CARR", "R0", "R1"}
    assert stored["P1"] == ("2026-10-02T11:00:00Z", None) and stored["R0"][1] == "reels_tab"
    assert h.batch(batch_id)["post_times_count"] == 6


def test_reels_tab_failure_completes_with_warning_and_keeps_grid(harness_factory):
    dl = reels_dl(items(3), [reel("R0", "2026-10-20T10:00:00Z")],
                  error=InstagramError("contract_changed", "A aba Reels mudou de formato (FAKE)."), error_after=1)
    h = harness_factory(downloader=dl)
    batch_id = start(h, inputs=["@perfil_teste"], media="none", post_times=True, limit=None,
                     confirm_all=True)["batch_ids"][0]
    h.run_all()
    batch = h.batch(batch_id)
    assert batch["status"] == "completed" and batch["error_code"] is None
    assert batch["error_message"].startswith("5 publicações analisadas")
    assert "A aba Reels não pôde ser lida: A aba Reels mudou de formato (FAKE)." in batch["error_message"]
    assert {"P0", "P1", "P2", "CARR", "R0"} == set(sources(h, batch_id))


def test_report_keeps_only_the_latest_n_unpinned(harness_factory):
    # Limite 3: a grade grava P0..P2; a aba traz um Reel mais novo e um mais antigo; o relatório fica com os 3 mais
    # recentes (RNEW, P2, P1), e a coluna Origem marca o estimado.
    dl = reels_dl(items(3), [reel("RNEW", "2026-10-20T10:00:00Z"), reel("ROLD", "2026-09-01T10:00:00Z")])
    h = harness_factory(downloader=dl)
    batch_id = start(h, inputs=["@perfil_teste"], media="none", post_times=True, limit=3)["batch_ids"][0]
    h.run_all()
    assert h.batch(batch_id)["post_times_count"] == 5
    response = h.client.get("/api/post-times/report.zip", params={"batch_id": batch_id})
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        text = archive.read("perfil_teste/posts.csv").decode("utf-8-sig")
        report = archive.read("perfil_teste/padroes.txt").decode("utf-8")
    lines = text.strip().splitlines()
    assert len(lines) == 1 + 3 and "Origem do horário" in lines[0]
    assert all(f"/p/{code}/" in text for code in ("RNEW", "P2", "P1"))
    assert "/p/P0/" not in text and "/p/ROLD/" not in text
    assert "Estimado (aba Reels)" in text and text.count("Publicação") == 2
    assert "Horários estimados pela aba Reels: 1 (precisão de ~±30 s)" in report


def test_reels_only_account_downloads_reels(harness_factory):
    dl = reels_dl([], [reel(f"R{i}", f"2026-10-1{i}T10:00:00Z") for i in range(3)])
    h = harness_factory(downloader=dl)
    batch_id = start(h, inputs=["@perfil_teste"], media="videos", limit=10)["batch_ids"][0]
    h.run_all()
    assert job_codes(h, batch_id) == {"R0", "R1", "R2"} and len(dl.downloads) == 3
    assert {kind for _, kind in dl.resolved} == {"reel"}
    batch = h.batch(batch_id)
    assert batch["status"] == "completed"
    assert "Grade: 0 itens; aba Reels: 3 vídeos que não estavam na grade." in batch["error_message"]


def test_grid_limit_takes_only_reels_newer_than_the_oldest_grid_item(harness_factory):
    grid = [video(f"V{i}", f"2026-10-0{9 - i}T10:00:00Z") for i in range(5)]   # V0 = 09/10 ... V4 = 05/10
    tab = [reel("RNEW", "2026-10-08T12:00:00Z"), reel("V0", "2026-10-09T10:00:10Z"),
           reel("RPIN", "2026-01-01T10:00:00Z", pinned=True), reel("ROLD", "2026-10-06T10:00:00Z"),
           reel("RAFTER", "2026-10-08T00:00:00Z")]
    dl = reels_dl(grid, tab)
    h = harness_factory(downloader=dl)
    batch_id = start(h, inputs=["@perfil_teste"], media="videos", limit=3)["batch_ids"][0]
    h.run_all()
    # Grade: V0..V2 (o mais antigo é 07/10). Da aba: só RNEW; V0 já veio da grade; o fixado antigo é pulado sem
    # encerrar; ROLD é mais antigo que a grade e encerra a etapa (RAFTER nem é avaliado).
    assert job_codes(h, batch_id) == {"V0", "V1", "V2", "RNEW"}
    assert [code for code, _ in dl.resolved] == ["RNEW"]
    assert "Grade: 3 itens; aba Reels: 1 vídeo que não estava na grade" in h.batch(batch_id)["error_message"]


def test_reels_tab_views_filter_uses_play_count(harness_factory):
    tab = [reel("RA", "2026-10-12T10:00:00Z", play_count=5000), reel("RB", "2026-10-11T10:00:00Z", play_count=10),
           reel("RC", "2026-10-10T10:00:00Z")]
    dl = reels_dl([], tab)
    h = harness_factory(downloader=dl)
    batch_id = start(h, inputs=["@perfil_teste"], media="videos", limit=10, min_views=1000)["batch_ids"][0]
    h.run_all()
    assert job_codes(h, batch_id) == {"RA"} and [code for code, _ in dl.resolved] == ["RA"]


def test_photos_import_does_not_read_reels_tab(harness_factory):
    dl = reels_dl([], [reel("R0", "2026-10-12T10:00:00Z")])
    h = harness_factory(downloader=dl)
    batch_id = start(h, inputs=["@perfil_teste"], media="photos", limit=10)["batch_ids"][0]
    h.run_all()
    assert dl.reels_calls == [] and dl.resolved == [] and jobs(h, batch_id) == 0


def test_error_on_one_reel_skips_only_that_reel(harness_factory):
    tab = [reel(f"R{i}", f"2026-10-1{i}T10:00:00Z") for i in range(3)]
    dl = reels_dl([], tab, resolve_errors={"R1": InstagramError("content_unavailable", "FAKE")})
    h = harness_factory(downloader=dl)
    batch_id = start(h, inputs=["@perfil_teste"], media="videos", limit=10)["batch_ids"][0]
    h.run_all()
    assert job_codes(h, batch_id) == {"R0", "R2"}
    message = h.batch(batch_id)["error_message"]
    assert "aba Reels: 2 vídeos que não estavam na grade (1 não pôde ser importado)." in message


def test_rate_limited_in_reels_step_keeps_grid(harness_factory):
    grid = [video("V0", "2026-10-09T10:00:00Z"), video("V1", "2026-10-08T10:00:00Z")]
    tab = [reel("R0", "2026-10-12T10:00:00Z"), reel("R1", "2026-10-11T10:00:00Z")]
    dl = reels_dl(grid, tab, resolve_errors={"R1": InstagramError("rate_limited", "Muitas requisições (FAKE).",
                                                                  retryable=True)})
    h = harness_factory(downloader=dl)
    batch_id = start(h, inputs=["@perfil_teste"], media="videos", limit=10)["batch_ids"][0]
    h.run_imports()
    batch = h.batch(batch_id)
    assert job_codes(h, batch_id) == {"V0", "V1", "R0"}
    assert batch["status"] != "failed" and batch["error_code"] is None
    assert batch["listing_state"] == "done" and "A aba Reels não pôde ser lida" in batch["error_message"]
