"""Revisão das legendas (docs/CAPCUT.md, "Fidelidade das legendas"): frases da transcrição de um job parado em
`waiting`/`review`, o arquivo exato que foi transcrito e a aprovação com o realinhamento das frases corrigidas.

O núcleo (`app.integrations.capcut.transcribe`: `decode_audio`, `align_text`, `aligner_available`) é importado só
dentro das funções. As rotas são síncronas (threadpool do FastAPI): a decodificação e o alinhamento ficam fora do
event loop."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.errors import AppError, not_found
from app.core.security import UnsafePathError, ensure_within
from app.db.connection import utc_now
from app.services import capcut_service

if TYPE_CHECKING:
    from app.services.context import AppContext

logger = logging.getLogger("media_tool.capcut")

SAMPLE_RATE = 16000
# Janela do realinhamento de uma frase corrigida: começa um pouco antes da frase (nunca antes do fim da anterior) e
# vai até o início da próxima (a última: até o fim dela + 0,6 s, limitado à duração).
WINDOW_BEFORE_S = 0.15
WINDOW_AFTER_LAST_S = 0.6


def _job(ctx: AppContext, job_id: int) -> dict[str, Any]:
    job = capcut_service.repo(ctx).get_job(job_id)
    if job is None:
        raise not_found("Job de legenda", "capcut_job_not_found")
    return job


def in_review(job: dict[str, Any]) -> bool:
    return job["status"] == "waiting" and job.get("wait_reason") == capcut_service.REVIEW_WAIT_REASON


def _models_dir(ctx: AppContext) -> Path:
    return ctx.settings.data_dir / "models" / "whisper"


def group_segments(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Frases (palavras seguidas com o mesmo `seg`), na ordem da transcrição."""
    groups: list[dict[str, Any]] = []
    for raw in words:
        seg = int(raw.get("seg", 0) or 0)
        word = {"text": str(raw["text"]), "start": float(raw["start"]), "end": float(raw["end"]),
                "doubt": bool(raw.get("doubt", False)), "probability": float(raw.get("probability", 1.0))}
        if not groups or groups[-1]["index"] != seg:
            groups.append({"index": seg, "words": []})
        groups[-1]["words"].append(word)
    for group in groups:
        ws = group["words"]
        group.update(start=ws[0]["start"], end=max(w["end"] for w in ws),
                     text=" ".join(w["text"] for w in ws if w["text"].strip()), doubt=any(w["doubt"] for w in ws))
    return groups


def source_path(ctx: AppContext, job: dict[str, Any]) -> Path:
    """O arquivo exato que foi transcrito (`options.transcript_source.path`: original da mídia ou rendition da
    edição), só dentro do storage e se ainda existir."""
    recorded = (job["options"] or {}).get("transcript_source") or {}
    raw = recorded.get("path")
    if not raw:
        raise AppError(404, "capcut_source_not_found", "O vídeo transcrito deste job não foi encontrado.")
    try:
        resolved = ensure_within(Path(raw), [ctx.storage.root])
    except UnsafePathError:
        raise AppError(403, "not_allowed", "Arquivo fora da Biblioteca.") from None
    if not resolved.is_file():
        raise AppError(404, "capcut_source_not_found", "O vídeo transcrito deste job não está mais na Biblioteca.")
    return resolved


def _duration(path: Path, words: list[dict[str, Any]]) -> float:
    """Duração do vídeo (ffprobe); sem conseguir ler, o fim da última palavra."""
    fallback = max((float(w["end"]) for w in words), default=0.0)
    try:
        from app.integrations.capcut.media import probe

        return max(float(probe(path).duration or 0.0), fallback)
    except Exception:  # noqa: BLE001 - só a duração da tela
        return fallback


def _aligner(ctx: AppContext) -> bool:
    try:
        from app.integrations.capcut.transcribe import aligner_available

        return bool(aligner_available(_models_dir(ctx)))
    except Exception:  # noqa: BLE001 - sem núcleo: sem alinhador
        return False


def review_view(ctx: AppContext, job_id: int) -> dict[str, Any]:
    job = _job(ctx, job_id)
    if not job["transcript"]:
        raise AppError(409, "capcut_no_transcript", "Este job ainda não tem transcrição para revisar.")
    words = list(job["transcript"])
    path = source_path(ctx, job)
    return {"job": capcut_service.get_job(ctx, job_id), "duration": round(_duration(path, words), 3),
            "source_url": f"/api/capcut/jobs/{job_id}/source", "aligner": _aligner(ctx),
            "segments": group_segments(words)}


def _clean_words(group: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"start": w["start"], "end": w["end"], "text": w["text"], "probability": w["probability"],
             "seg": group["index"], "doubt": False} for w in group["words"]]


def approve(ctx: AppContext, job_id: int, edits: list[dict[str, Any]]) -> dict[str, Any]:
    """Aplica as frases corrigidas (realinhadas na janela de cada uma; texto vazio remove a frase), limpa as dúvidas,
    grava `options.review` e devolve o job à fila. 409 se o job não está em revisão."""
    job = _job(ctx, job_id)
    if not in_review(job):
        raise AppError(409, "capcut_job_not_in_review", "Este job não está aguardando a revisão das legendas.")
    groups = group_segments(list(job["transcript"] or []))
    current = {g["index"]: g["text"] for g in groups}
    wanted: dict[int, str] = {}
    for edit in edits:
        index = int(edit["index"])
        if index not in current:
            raise AppError(422, "invalid_review_segment", f"A frase {index} não existe nesta transcrição.")
        text = " ".join(str(edit["text"]).split())
        if text != current[index]:
            wanted[index] = text
    new_words: list[dict[str, Any]] = []
    if wanted:
        from app.integrations.capcut import transcribe as tr

        path = source_path(ctx, job)
        audio = None
        duration = 0.0
        if any(wanted.values()):   # só remoções não precisam do áudio
            audio = tr.decode_audio(path)
            if audio is None or not len(audio):
                raise AppError(409, "capcut_source_no_audio", "O vídeo transcrito não tem áudio para realinhar.")
            duration = len(audio) / SAMPLE_RATE
        for k, group in enumerate(groups):
            if group["index"] not in wanted:
                new_words.extend(_clean_words(group))
                continue
            text = wanted[group["index"]]
            if not text:
                continue   # frase removida
            prev_end = new_words[-1]["end"] if new_words else 0.0
            lo = max(group["start"] - WINDOW_BEFORE_S, prev_end, 0.0)
            hi = (groups[k + 1]["start"] if k + 1 < len(groups)
                  else min(group["end"] + WINDOW_AFTER_LAST_S, duration))
            aligned = tr.align_text(audio, text.split(), lo, hi, _models_dir(ctx))
            new_words.extend({"start": float(w.start), "end": float(w.end), "text": w.text, "probability": 1.0,
                              "seg": group["index"], "doubt": False} for w in aligned)
    else:
        for group in groups:
            new_words.extend(_clean_words(group))
    options = {**(job["options"] or {}), "review": {"approved": True, "at": utc_now(), "edited": len(wanted)}}
    if not capcut_service.repo(ctx).approve_review(job_id, transcript=new_words, options=options):
        raise AppError(409, "capcut_job_not_in_review", "Este job não está aguardando a revisão das legendas.")
    logger.info("Legendas CapCut: revisão do job %s aprovada (%s frase(s) alterada(s))", job_id, len(wanted))
    capcut_service._wake(ctx)
    return capcut_service.get_job(ctx, job_id)
