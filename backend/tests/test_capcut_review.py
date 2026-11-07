"""Revisão das legendas (docs/CAPCUT.md, "Fidelidade das legendas"): parada em `waiting`/`review` sem gastar
tentativa, aprovação com realinhamento (fallback por sílabas, sem o modelo ONNX), rotas da revisão, fonte trocada e
contagens. Processador e transcrição falsos (`capcut_jobs_fakes`); o áudio da fonte é um numpy sintético."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from capcut_jobs_fakes import FakeCapCutError, FakeCapCutProcessor, add_media

from app.integrations.capcut import align
from app.integrations.capcut import transcribe as tr
from app.jobs.capcut_runner import CapCutRunner, recover_capcut_jobs
from app.services import capcut_service

DOUBT_WORDS = [
    {"start": 0.0, "end": 0.4, "text": "Olá", "probability": 0.9, "seg": 0, "doubt": False},
    {"start": 0.4, "end": 0.8, "text": "pessoal", "probability": 0.9, "seg": 0, "doubt": False},
    {"start": 1.0, "end": 1.3, "text": "Esse", "probability": 0.3, "seg": 1, "doubt": True},
    {"start": 1.3, "end": 1.6, "text": "laranjinha", "probability": 0.3, "seg": 1, "doubt": True},
    {"start": 2.0, "end": 2.5, "text": "tchau", "probability": 0.95, "seg": 2, "doubt": False},
]
CLEAN_WORDS = [{"start": 0.0, "end": 0.5, "text": "olá", "probability": 0.9, "seg": 0, "doubt": False}]


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def setup(harness, tmp_path):
    processor = FakeCapCutProcessor(words=[dict(w) for w in DOUBT_WORDS])
    harness.ctx.capcut_processor = processor
    clock = Clock()
    runner = CapCutRunner(harness.ctx, clock=clock)
    harness.ctx.capcut_runner = runner
    out = tmp_path / "saida"
    made: list[int] = []

    def make_job(**overrides) -> dict:
        media_id = add_media(harness, name=f"v{len(made)}")
        made.append(media_id)
        payload = {"media_ids": [media_id], "overrides": {"output_dir": str(out), **overrides}}
        response = harness.client.post("/api/capcut/jobs", json=payload)
        assert response.status_code == 202, response.text
        return response.json()["created"][0]

    return harness, processor, runner, clock, make_job


@pytest.fixture
def audio(monkeypatch):
    """Áudio sintético de 3 s no lugar do FFmpeg; conta as decodificações."""
    calls: list[str] = []

    def fake_decode(path, **_kwargs):
        calls.append(str(path))
        return np.zeros(3 * tr.SAMPLE_RATE, dtype=np.float32)

    monkeypatch.setattr(tr, "decode_audio", fake_decode)
    return calls


def row(harness, job_id) -> dict:
    return capcut_service.repo(harness.ctx).get_job(job_id)


def to_review(setup) -> dict:
    harness, _, runner, _, make_job = setup
    created = make_job()
    assert runner.run_next() is True
    stopped = row(harness, created["id"])
    assert stopped["status"] == "waiting" and stopped["wait_reason"] == "review"
    return stopped


# ---- parada na revisão -----------------------------------------------------------------------------------------------
def test_doubtful_stops_in_review_without_spending_attempt(setup):
    harness, processor, runner, clock, make_job = setup
    created = make_job()
    assert created["options"]["review_mode"] == "doubtful", "padrão das configurações"
    assert runner.run_next() is True
    stopped = row(harness, created["id"])
    assert stopped["status"] == "waiting" and stopped["wait_reason"] == "review"
    assert stopped["attempts"] == 0 and stopped["next_attempt_at"] is None and stopped["stage"] is None
    assert stopped["detail"] == "Aguardando revisão das legendas"
    assert processor.specs == [], "o CapCut não roda antes da revisão"
    assert len(processor.transcribed) == 1 and stopped["transcript"] == DOUBT_WORDS
    source = stopped["options"]["transcript_source"]
    video = harness.ctx.storage.resolve("originals/test/v0.mp4")
    assert source == {"path": str(video), "size": video.stat().st_size, "mtime_ns": video.stat().st_mtime_ns}
    clock.advance(3600)
    assert capcut_service.repo(harness.ctx).claim_next("2099-01-01T00:00:00.000Z") is None
    assert runner.run_next() is False
    view = harness.client.get(f"/api/capcut/jobs/{created['id']}").json()
    assert view["needs_review"] is True and view["doubt_count"] == 1


def test_no_doubts_goes_straight_to_capcut(setup):
    harness, processor, runner, _, make_job = setup
    processor.words = [dict(w) for w in CLEAN_WORDS]
    created = make_job()
    assert runner.run_next() is True
    done = row(harness, created["id"])
    assert done["status"] == "completed" and done["attempts"] == 1
    assert len(processor.transcribed) == 1
    assert processor.specs[-1].transcript == CLEAN_WORDS, "o pipeline reaproveita a transcrição"
    view = harness.client.get(f"/api/capcut/jobs/{created['id']}").json()
    assert view["needs_review"] is False and view["doubt_count"] == 0


def test_always_stops_even_without_doubts(setup):
    harness, processor, runner, _, make_job = setup
    processor.words = [dict(w) for w in CLEAN_WORDS]
    created = make_job(review_mode="always")
    runner.run_next()
    stopped = row(harness, created["id"])
    assert stopped["status"] == "waiting" and stopped["wait_reason"] == "review" and processor.specs == []


def test_never_and_old_job_run_as_before(setup):
    harness, processor, runner, _, make_job = setup
    never = make_job(review_mode="never")
    old = make_job()
    options = dict(row(harness, old["id"])["options"])
    options.pop("review_mode")
    capcut_service.repo(harness.ctx).update_job(old["id"], options=options)
    assert runner.run_next() is True and runner.run_next() is True
    for job in (never, old):
        done = row(harness, job["id"])
        assert done["status"] == "completed", job
        assert done["options"]["transcript_source"]["path"].endswith(".mp4"), "gravada junto com a transcrição"
    assert processor.transcribed == [] and all(spec.transcript is None for spec in processor.specs)


def test_transcription_error_is_classified_like_the_processor(setup):
    harness, processor, runner, _, make_job = setup
    processor.transcribe_errors = [FakeCapCutError("transcription_failed", "Falhou.", transient=False)]
    created = make_job()
    runner.run_next()
    failed = row(harness, created["id"])
    assert failed["status"] == "failed" and failed["error_code"] == "transcription_failed"


def test_restart_keeps_review_and_cancel_works(setup):
    harness, _, runner, _, _ = setup
    stopped = to_review(setup)
    assert recover_capcut_jobs(harness.ctx) == 0
    after = row(harness, stopped["id"])
    assert after["status"] == "waiting" and after["wait_reason"] == "review"
    assert runner.run_next() is False
    response = harness.client.post(f"/api/capcut/jobs/{stopped['id']}/cancel")
    assert response.status_code == 200 and response.json()["status"] == "cancelled"


# ---- rotas da revisão ------------------------------------------------------------------------------------------------
def test_get_review_groups_segments(setup):
    harness, _, _, _, _ = setup
    stopped = to_review(setup)
    response = harness.client.get(f"/api/capcut/jobs/{stopped['id']}/review")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job"]["id"] == stopped["id"] and body["job"]["needs_review"] is True
    assert body["source_url"] == f"/api/capcut/jobs/{stopped['id']}/source"
    assert body["aligner"] is False and body["duration"] >= 2.5
    segs = body["segments"]
    assert [s["index"] for s in segs] == [0, 1, 2]
    assert [s["text"] for s in segs] == ["Olá pessoal", "Esse laranjinha", "tchau"]
    assert [s["doubt"] for s in segs] == [False, True, False]
    assert segs[1]["start"] == 1.0 and segs[1]["end"] == 1.6
    assert segs[1]["words"][0] == {"text": "Esse", "start": 1.0, "end": 1.3, "doubt": True, "probability": 0.3}


def test_get_source_serves_the_transcribed_file(setup):
    harness, _, _, _, _ = setup
    stopped = to_review(setup)
    response = harness.client.get(f"/api/capcut/jobs/{stopped['id']}/source")
    assert response.status_code == 200
    assert response.content == harness.ctx.storage.resolve("originals/test/v0.mp4").read_bytes()
    assert response.headers["content-type"].startswith("video/mp4")


def test_source_outside_storage_is_refused(setup, tmp_path):
    harness, _, _, _, _ = setup
    stopped = to_review(setup)
    outside = tmp_path / "fora.mp4"
    outside.write_bytes(b"x")
    options = {**stopped["options"], "transcript_source": {"path": str(outside), "size": 1, "mtime_ns": 1}}
    capcut_service.repo(harness.ctx).update_job(stopped["id"], options=options)
    assert harness.client.get(f"/api/capcut/jobs/{stopped['id']}/source").status_code == 403


def test_approve_with_edit_realigns_inside_the_window(setup, audio):
    harness, processor, runner, _, _ = setup
    models_root = harness.settings.data_dir / "models"
    assert align.available(models_root) is False, "sem o modelo ONNX: fallback por sílabas"
    stopped = to_review(setup)
    response = harness.client.post(f"/api/capcut/jobs/{stopped['id']}/review",
                                   json={"segments": [{"index": 1, "text": "Esse é o Gerson"}]})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending" and response.json()["needs_review"] is False
    approved = row(harness, stopped["id"])
    assert approved["wait_reason"] is None and approved["next_attempt_at"] is None
    review = approved["options"]["review"]
    assert review["approved"] is True and review["edited"] == 1 and review["at"]
    words = approved["transcript"]
    assert not any(w["doubt"] for w in words)
    seg1 = [w for w in words if w["seg"] == 1]
    assert [w["text"] for w in seg1] == ["Esse", "é", "o", "Gerson"]
    lo, hi = max(1.0 - 0.15, 0.8), 2.0   # janela: início - 0,15 s (não antes do fim da anterior) até a próxima
    assert all(lo <= w["start"] < w["end"] <= hi for w in seg1)
    assert all(a["end"] <= b["start"] for a, b in zip(seg1, seg1[1:])), "em ordem, sem sobreposição"
    assert [w["text"] for w in words if w["seg"] != 1] == ["Olá", "pessoal", "tchau"]
    assert len(audio) == 1 and audio[0].endswith("v0.mp4"), "o áudio decodificado é o da fonte transcrita"
    assert runner.run_next() is True
    done = row(harness, stopped["id"])
    assert done["status"] == "completed" and done["attempts"] == 1
    assert processor.specs[-1].transcript == words, "o processador recebe a transcrição editada"
    assert len(processor.transcribed) == 1, "a transcrição aprovada não é refeita"


def test_approve_without_edit(setup, monkeypatch):
    harness, processor, runner, _, _ = setup
    monkeypatch.setattr(tr, "decode_audio", lambda *a, **k: pytest.fail("sem edição não decodifica o áudio"))
    stopped = to_review(setup)
    response = harness.client.post(f"/api/capcut/jobs/{stopped['id']}/review", json={"segments": []})
    assert response.status_code == 200, response.text
    approved = row(harness, stopped["id"])
    assert approved["options"]["review"]["edited"] == 0 and approved["status"] == "pending"
    assert [w["text"] for w in approved["transcript"]] == [w["text"] for w in DOUBT_WORDS]
    assert [w["start"] for w in approved["transcript"]] == [w["start"] for w in DOUBT_WORDS]
    assert not any(w["doubt"] for w in approved["transcript"])
    unchanged = harness.client.post(f"/api/capcut/jobs/{stopped['id']}/review",
                                    json={"segments": [{"index": 0, "text": "Olá pessoal"}]})
    assert unchanged.status_code == 409, "já aprovado: não está mais em revisão"
    runner.run_next()
    assert row(harness, stopped["id"])["status"] == "completed"


def test_same_text_does_not_count_as_edit(setup, monkeypatch):
    harness, _, _, _, _ = setup
    monkeypatch.setattr(tr, "decode_audio", lambda *a, **k: pytest.fail("texto igual não realinha"))
    stopped = to_review(setup)
    response = harness.client.post(f"/api/capcut/jobs/{stopped['id']}/review",
                                   json={"segments": [{"index": 1, "text": "  Esse   laranjinha "}]})
    assert response.status_code == 200
    assert row(harness, stopped["id"])["options"]["review"]["edited"] == 0


def test_empty_text_removes_the_segment(setup, audio):
    harness, _, _, _, _ = setup
    stopped = to_review(setup)
    response = harness.client.post(f"/api/capcut/jobs/{stopped['id']}/review",
                                   json={"segments": [{"index": 1, "text": "   "}]})
    assert response.status_code == 200, response.text
    words = row(harness, stopped["id"])["transcript"]
    assert [w["text"] for w in words] == ["Olá", "pessoal", "tchau"] and {w["seg"] for w in words} == {0, 2}
    assert audio == [], "só remoção: sem decodificar o áudio"


def test_approve_conflicts_when_not_in_review(setup):
    harness, processor, runner, _, make_job = setup
    processor.words = [dict(w) for w in CLEAN_WORDS]
    created = make_job()
    pending = harness.client.post(f"/api/capcut/jobs/{created['id']}/review", json={"segments": []})
    assert pending.status_code == 409 and pending.json()["code"] == "capcut_job_not_in_review"
    runner.run_next()
    done = harness.client.post(f"/api/capcut/jobs/{created['id']}/review", json={"segments": []})
    assert done.status_code == 409


def test_unknown_segment_is_rejected(setup):
    harness, _, _, _, _ = setup
    stopped = to_review(setup)
    response = harness.client.post(f"/api/capcut/jobs/{stopped['id']}/review",
                                   json={"segments": [{"index": 9, "text": "x"}]})
    assert response.status_code == 422
    assert row(harness, stopped["id"])["wait_reason"] == "review"


def test_review_mutation_requires_client_header(setup):
    harness, _, _, _, _ = setup
    stopped = to_review(setup)
    response = harness.client.post(f"/api/capcut/jobs/{stopped['id']}/review", json={"segments": []},
                                   headers={"X-Media-Tool-Client": ""})
    assert response.status_code == 403


# ---- fonte trocada ---------------------------------------------------------------------------------------------------
def test_changed_source_discards_transcript_and_review(setup):
    harness, processor, runner, _, _ = setup
    stopped = to_review(setup)
    assert harness.client.post(f"/api/capcut/jobs/{stopped['id']}/review", json={"segments": []}).status_code == 200
    processor.errors = [FakeCapCutError("export_failed", "Falhou.", transient=False)]
    runner.run_next()
    assert row(harness, stopped["id"])["status"] == "failed"
    video = harness.ctx.storage.resolve("originals/test/v0.mp4")
    video.write_bytes(b"OUTRO-ARQUIVO-COM-OUTRO-TAMANHO")
    assert harness.client.post(f"/api/capcut/jobs/{stopped['id']}/retry").status_code == 200
    runner.run_next()
    again = row(harness, stopped["id"])
    assert len(processor.transcribed) == 2, "transcrição de outra fonte: refeita"
    assert "review" not in again["options"], "a aprovação da fonte antiga caiu"
    assert again["options"]["transcript_source"]["size"] == len(b"OUTRO-ARQUIVO-COM-OUTRO-TAMANHO")
    assert again["status"] == "waiting" and again["wait_reason"] == "review"


# ---- contagens -------------------------------------------------------------------------------------------------------
def test_review_counts_in_queue_and_batch(setup, tmp_path):
    harness, _, runner, _, _ = setup
    media_id = add_media(harness, name="lote0")
    created = harness.client.post("/api/capcut/batches", json={
        "mode": "caption", "media_ids": [media_id],
        "caption": {"overrides": {"output_dir": str(tmp_path / "lote")}}})
    assert created.status_code == 202, created.text
    batch_id = created.json()["batch"]["id"]
    assert runner.run_next() is True
    batch = harness.client.get(f"/api/capcut/batches/{batch_id}").json()
    assert batch["counts"]["review"] == 1 and batch["counts"]["waiting"] == 0
    assert batch["status"] == "review" and batch["finished_at"] is None
    jobs = harness.client.get(f"/api/capcut/batches/{batch_id}/jobs").json()
    assert jobs["counts"]["review"] == 1 and jobs["items"][0]["needs_review"] is True
    queue = harness.client.get("/api/capcut/status").json()["queue"]
    assert queue["review"] == 1 and queue["waiting"] == 0
    assert harness.client.get("/api/capcut/jobs").json()["counts"]["review"] == 1


def test_app_restart_keeps_review(harness_factory, tmp_path):
    first = harness_factory()
    processor = FakeCapCutProcessor(words=[dict(w) for w in DOUBT_WORDS])
    first.ctx.capcut_processor = processor
    runner = CapCutRunner(first.ctx, clock=Clock())
    first.ctx.capcut_runner = runner
    media_id = add_media(first, name="r0")
    created = first.client.post("/api/capcut/jobs", json={
        "media_ids": [media_id], "overrides": {"output_dir": str(tmp_path / "out")}}).json()["created"][0]
    assert runner.run_next() is True
    first.__exit__(None, None, None)
    second = harness_factory(root=first.root)
    after = capcut_service.repo(second.ctx).get_job(created["id"])
    assert after["status"] == "waiting" and after["wait_reason"] == "review" and after["attempts"] == 0
    assert after["detail"] == "Aguardando revisão das legendas" and after["transcript"] == DOUBT_WORDS
    second.ctx.capcut_processor = FakeCapCutProcessor()
    assert CapCutRunner(second.ctx, clock=Clock()).run_next() is False


def test_queue_keeps_processing_other_videos_while_one_waits_for_review(setup):
    harness, processor, runner, _, make_job = setup
    first, second = make_job(), make_job()          # o 1º terá dúvidas; o 2º sai limpo
    assert runner.run_next() is True                # 1º: transcreve e para na revisão
    assert row(harness, first["id"])["wait_reason"] == "review"
    processor.words = [dict(w) for w in CLEAN_WORDS]
    assert runner.run_next() is True                # a fila segue: o 2º é processado até o fim
    assert row(harness, second["id"])["status"] == "completed"
    assert row(harness, first["id"])["wait_reason"] == "review", "o 1º continua esperando a revisão"
    assert runner.run_next() is False               # nada mais vencido; a revisão não trava nem repete
