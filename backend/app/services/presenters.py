"""Converte linhas do banco nos formatos de resposta da API (contrato, seção 8)."""

from __future__ import annotations

from typing import Any

from app.db.connection import parse_utc, utc_now
from app.db.repositories.batches import load_listing_counts
from app.db.repositories.common import JOB_STATUSES
from app.services.context import AppContext

_PROGRESS_KEYS = ("downloaded", "processed", "skipped", "failed", "pending", "active")
# Contadores congelados em `batches.final_*` quando o lote termina sem job aberto (migrations 0005/0006).
_FROZEN_KEYS = ("downloaded", "processed", "skipped", "failed")
_CLOSED_BATCH_STATUSES = ("completed", "failed", "cancelled")


def _frozen(batch: dict[str, Any]) -> bool:
    return batch["status"] in _CLOSED_BATCH_STATUSES and batch.get("final_found") is not None
# GET /api/batches/{id}?include_jobs=true: no máximo estes jobs (os mais recentes) embutidos.
MAX_EMBEDDED_JOBS = 500


def _elapsed(job: dict[str, Any]) -> float | None:
    started = parse_utc(job.get("started_at"))
    if started is None:
        return None
    end = parse_utc(job.get("finished_at")) or parse_utc(utc_now())
    return round(max(0.0, (end - started).total_seconds()), 3)


def job_out(job: dict[str, Any], thumbs: set[int]) -> dict[str, Any]:
    media_id = job.get("media_id")
    return {
        "id": job["id"],
        "batch_id": job["batch_id"],
        "media_id": media_id,
        "rendition_id": job["rendition_id"],
        "kind": job["kind"],
        "preset_id": job["preset_id"],
        "preset_name": job["preset_name"],
        "status": job["status"],
        "stage": job["stage"],
        "progress": job["progress"],
        "error_code": job["error_code"],
        "error_message": job["error_message"],
        "attempts": job["attempts"],
        "max_attempts": job["max_attempts"],
        "next_attempt_at": job["next_attempt_at"],
        "is_duplicate": bool(job["is_duplicate"]),
        "duplicate_reason": job["duplicate_reason"],
        "source_platform": job["source_platform"],
        "source_profile": job["source_profile"],
        "source_url": job["source_url"],
        "source_post_id": job["source_post_id"],
        "source_media_index": job["source_media_index"],
        "media_type": job["media_type"],
        "thumbnail_url": f"/api/media/{media_id}/thumbnail" if media_id in thumbs else None,
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "updated_at": job["updated_at"],
        "elapsed_seconds": _elapsed(job),
    }


def jobs_out(ctx: AppContext, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    thumbs = ctx.jobs.thumbnails_for(sorted({j["media_id"] for j in jobs if j.get("media_id")}))
    return [job_out(job, thumbs) for job in jobs]


def job_single(ctx: AppContext, job_id: int) -> dict[str, Any] | None:
    job = ctx.jobs.get(job_id)
    return None if job is None else jobs_out(ctx, [job])[0]


def _listing_dropped(batch: dict[str, Any]) -> dict[str, Any]:
    def counts(value: Any) -> dict[str, int]:
        if not isinstance(value, dict):
            return {}
        return {str(k): v for k, v in value.items() if isinstance(v, int) and not isinstance(v, bool) and v > 0}

    reasons = counts(load_listing_counts(batch.get("listing_dropped_reasons")).get("unexpected"))
    partial = counts(load_listing_counts(batch.get("listing_dropped_partial")))
    for reason, amount in partial.items():
        reasons[reason] = reasons.get(reason, 0) + amount
    pending = sum(partial.values())
    return {"unexpected_total": int(batch.get("listing_dropped_unexpected") or 0) + pending, "reasons": reasons,
            "pending": pending}


def _last_export(export: dict[str, Any] | None) -> dict[str, Any] | None:
    if export is None:
        return None
    return {key: export[key] for key in ("id", "dest_path", "status", "total", "copied", "skipped_existing",
                                         "renamed", "missing", "failed", "finished_at")} | {
        "mode": export.get("mode") or "folder"}


def _group_state(batch: dict[str, Any]) -> str | None:
    """Estado apresentado de um filho de grupo (mesma regra do agregado em BatchRepository.group_aggregates)."""
    if batch.get("group_id") is None:
        return None
    status = batch["status"]
    if status in _CLOSED_BATCH_STATUSES:
        return status
    if status == "listing":
        listing_state = batch.get("listing_state") or "pending"
        if listing_state == "needs_attention":
            return "attention"
        if listing_state == "pending" and batch.get("listing_next_page") is None:
            return "waiting"   # na fila do grupo: a listagem ainda não começou
    return "active"


def groups_out(ctx: AppContext, groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Grupos de importação com agregados derivados dos filhos (duas consultas para a lista inteira)."""
    stats = ctx.batches.group_aggregates([g["id"] for g in groups])
    exports = ctx.exports.last_completed("group_id", [g["id"] for g in groups])
    result = []
    for group in groups:
        row = stats.get(group["id"], {})
        total = int(row.get("total", 0))
        counts = {key: int(row.get(key, 0)) for key in ("waiting", "completed", "attention", "cancelled")}
        counts["failed"] = int(row.get("failed_profiles", 0))
        counts["active"] = total - sum(counts.values())
        open_children = counts["waiting"] + counts["active"] + counts["attention"]
        if counts["waiting"] + counts["active"] > 0:
            status = "active"
        elif counts["attention"] > 0:
            status = "attention"
        elif total > 0 and counts["cancelled"] == total:
            status = "cancelled"
        else:
            status = "completed"
        found = int(row.get("found", 0))
        result.append({
            **{key: group[key] for key in ("id", "content_type", "requested_limit", "preset_id", "preset_name",
                                           "created_at")},
            "process": bool(group["process"]),
            "finished_at": row.get("finished_at") if total > 0 and open_children == 0 else None,
            "status": status,
            "attention": counts["attention"] + counts["failed"] > 0,
            "profiles": {"total": total, **counts},
            "profile_end_count": int(row.get("profile_end", 0)),
            "progress": {"found": found, **{key: int(row.get(key, 0)) for key in _PROGRESS_KEYS}},
            "listing_dropped_total": int(row.get("dropped", 0)),
            "last_export": _last_export(exports.get(group["id"])),
        })
    return result


def group_detail(ctx: AppContext, group: dict[str, Any], *, children_limit: int,
                 after_position: int | None = None) -> dict[str, Any]:
    children, next_after = ctx.batches.group_children(group["id"], limit=children_limit,
                                                      after_position=after_position)
    return {"group": groups_out(ctx, [group])[0], "children": batches_out(ctx, children),
            "next_after_position": next_after}


def batches_out(ctx: AppContext, batches: list[dict[str, Any]], *, with_jobs: bool = False) -> list[dict[str, Any]]:
    # Lote encerrado com contadores congelados não agrega os jobs; lote aberto agrega pelo índice coberto.
    stats = ctx.jobs.batch_stats([b["id"] for b in batches if not _frozen(b)])
    exports = ctx.exports.last_completed("batch_id", [b["id"] for b in batches])
    # Lotes por link: contagem dos links numa consulta agregada para a página inteira (nenhuma se não há lote assim).
    link_counts = ctx.batches.link_counts([b["id"] for b in batches if b.get("import_mode") == "links"])
    result = []
    for batch in batches:
        row = stats.get(batch["id"], {})
        data = {
            key: batch[key]
            for key in (
                "id", "source", "source_profile", "content_type", "requested_limit", "preset_id", "preset_name",
                "status", "found", "error_code", "error_message", "created_at", "updated_at", "finished_at",
            )
        }
        data["process"] = bool(batch["process"])
        data["listing_state"] = batch.get("listing_state")
        # Espera pelo teto de páginas por hora: o trecho continua `running` no banco e a API apresenta
        # `waiting_rate_limit` só enquanto a retomada prevista está no futuro (valor antigo nunca aparece).
        resume_at = batch.get("rate_limit_resume_at")
        waiting = (batch["status"] == "listing" and data["listing_state"] == "running"
                   and bool(resume_at) and resume_at > utc_now())
        if waiting:
            data["listing_state"] = "waiting_rate_limit"
        data["rate_limit_resume_at"] = resume_at if waiting else None
        data["rate_limit_pages_used"] = batch.get("rate_limit_pages_used") if waiting else None
        data["rate_limit_pages_max"] = batch.get("rate_limit_pages_max") if waiting else None
        # Descartes inesperados da listagem (migration 0011): total e motivo -> quantidade; zero em lotes antigos.
        # `pending`: parte do total que vem da página ainda sem checkpoint (`listing_dropped_partial`; ex.: lote em
        # needs_attention): já aparece no total para não ficar invisível, e vira definitiva (ou é substituída pela
        # contagem da página refeita) sem somar duas vezes.
        data["listing_dropped"] = _listing_dropped(batch)
        data["listing_end"] = batch.get("listing_end")
        if batch.get("import_mode") == "links":
            data["import_mode"] = "links"
            data["links"] = link_counts.get(batch["id"], {"total": 0, "pending": 0, "resolved": 0, "failed": 0})
        else:
            data["import_mode"] = "profile" if batch["source"] == "instagram_import" else None
            data["links"] = None
        data["group_id"] = batch.get("group_id")
        data["group_position"] = batch.get("group_position")
        data["group_state"] = _group_state(batch)
        # Última exportação concluída do lote (define o "Abrir pasta" da importação); None se nunca exportou.
        data["last_export"] = _last_export(exports.get(batch["id"]))
        # Pausa do disjuntor só enquanto vigente; paused_reason é o código que repetiu (o frontend o descreve).
        until = batch.get("paused_until")
        active = until is not None and until > utc_now()
        data["paused_reason"] = (batch.get("paused_code") or batch.get("paused_reason")) if active else None
        data["paused_until"] = until if active else None
        # Durante a listagem, batches.found é sincronizado depois da criação dos jobs: nunca menor que o real.
        if _frozen(batch):
            # Lote encerrado sem job aberto: contadores congelados no fim (a retenção pode ter apagado os jobs).
            found = int(batch["final_found"])
            progress = {key: 0 for key in _PROGRESS_KEYS}
            progress.update({key: int(batch[f"final_{key}"] or 0) for key in _FROZEN_KEYS})
            counts = {status: 0 for status in JOB_STATUSES}
            counts.update({s: int(batch[f"final_{s}"] or 0) for s in ("completed", "failed", "cancelled")})
        else:
            found = max(int(batch["found"] or 0), row.get("total", 0))
            progress = {key: row.get(key, 0) for key in _PROGRESS_KEYS}
            counts = {status: row.get(f"c_{status}", 0) for status in JOB_STATUSES}
        data["found"] = found
        data["progress"] = {"found": found, **progress}
        data["counts"] = counts
        if with_jobs:
            jobs, truncated = ctx.jobs.recent_for_batch(batch["id"], MAX_EMBEDDED_JOBS)
            data["jobs"] = jobs_out(ctx, jobs)
            data["jobs_truncated"] = truncated
        result.append(data)
    return result


def batch_links_out(batch: dict[str, Any], links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Links de um lote por link. Link ainda pendente de um lote cancelado é apresentado como `cancelled` (o estado
    gravado continua `pending`; nada mais será pedido ao Instagram)."""
    cancelled = batch["status"] == "cancelled"
    return [
        {
            **{key: link[key] for key in ("position", "url", "shortcode", "error_code", "error_message",
                                          "items_found", "profile")},
            "status": "cancelled" if cancelled and link["status"] == "pending" else link["status"],
        }
        for link in links
    ]


def batch_single(ctx: AppContext, batch_id: int, *, with_jobs: bool = False) -> dict[str, Any] | None:
    batch = ctx.batches.get(batch_id)
    return None if batch is None else batches_out(ctx, [batch], with_jobs=with_jobs)[0]


def rendition_out(rendition: dict[str, Any]) -> dict[str, Any]:
    has_audio = rendition["has_audio"]
    return {
        "id": rendition["id"],
        "media_id": rendition["media_id"],
        "preset_id": rendition["preset_id"],
        "preset_name": rendition["preset_name"],
        "job_id": rendition["job_id"],
        "processed_sha256": rendition["processed_sha256"],
        "file_size": rendition["file_size"],
        "duration": rendition["duration"],
        "width": rendition["width"],
        "height": rendition["height"],
        "fps": rendition["fps"],
        "has_audio": None if has_audio is None else bool(has_audio),
        "created_at": rendition["created_at"],
        "file_url": f"/api/renditions/{rendition['id']}/file",
    }


def media_out(ctx: AppContext, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = [m["id"] for m in items]
    renditions = ctx.media.renditions_for(ids)
    last_jobs = ctx.media.last_jobs(ids)
    thumbs = {m["id"] for m in items if m["thumbnail_path"]}
    result = []
    for media in items:
        has_audio = media["has_audio"]
        last_job = last_jobs.get(media["id"])
        if last_job is not None:
            last_job = job_out(last_job, thumbs)
        result.append(
            {
                "id": media["id"],
                "source_platform": media["source_platform"],
                "source_profile": media["source_profile"],
                "source_url": media["source_url"],
                "source_post_id": media["source_post_id"],
                "source_media_index": media["source_media_index"],
                "media_type": media["media_type"],
                "original_filename": media["original_filename"],
                "original_sha256": media["original_sha256"],
                "file_size": media["file_size"],
                "duration": media["duration"],
                "width": media["width"],
                "height": media["height"],
                "fps": media["fps"],
                "has_audio": None if has_audio is None else bool(has_audio),
                "mime_type": media["mime_type"],
                "taken_at": media["taken_at"],
                "created_at": media["created_at"],
                "thumbnail_url": f"/api/media/{media['id']}/thumbnail" if media["thumbnail_path"] else None,
                "original_url": f"/api/media/{media['id']}/original",
                "renditions": [rendition_out(r) for r in renditions.get(media["id"], [])],
                "last_job": last_job,
            }
        )
    return result
