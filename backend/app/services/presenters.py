"""Converte linhas do banco nos formatos de resposta da API (contrato, seção 8)."""

from __future__ import annotations

from typing import Any

from app.db.connection import parse_utc, utc_now
from app.db.repositories.batches import TYPE_FILTER_REASON, VIEWS_KEYS, VIEWS_KEYS_MAX, load_listing_counts
from app.db.repositories.common import JOB_STATUSES
from app.services.auto_export import auto_export_out, exports_for, with_requests
from app.services.context import AppContext

_PROGRESS_KEYS = ("downloaded", "processed", "skipped", "failed", "pending", "active", "duplicates")
# Contadores congelados em `batches.final_*` quando o lote termina sem job aberto (migrations 0005/0006; `duplicates`
# na 0021, NULL = 0 em lotes encerrados antes dela).
_FROZEN_KEYS = ("downloaded", "processed", "skipped", "failed", "duplicates")
# Vereditos que NÃO baixam (contador `filtered`, com os itens ignorados pelo tipo de mídia).
_FILTERED_VERDICTS = ("below", "no_count", "out_of_period", "above")


def media_choice(row: dict[str, Any]) -> str | None:
    """Tipo de mídia do pedido (importação unificada): o `media_set` gravado (migration 0023); nos lotes anteriores a
    ela, "covers" (migration 0021), "videos" (Reels/vídeos) ou "all"; None nos lotes antigos com outro conteúdo
    (fotos, publicações do feed) e nos da Biblioteca."""
    if row.get("media_set"):
        return row["media_set"]
    if row.get("media_role") == "cover":
        return "covers"
    content = row.get("content_type")
    if content in ("videos", "reels"):
        return "videos"
    return "all" if content == "all" else None


def _type_filtered(batch: dict[str, Any]) -> int:
    expected = load_listing_counts(batch.get("listing_dropped_reasons")).get("expected")
    value = expected.get(TYPE_FILTER_REASON) if isinstance(expected, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
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


def _filters_on(row: dict[str, Any]) -> bool:
    return (row.get("min_views") is not None or row.get("max_views") is not None
            or row.get("posted_after") is not None)


def _filter_settings(row: dict[str, Any]) -> dict[str, Any]:
    return {"min_views": None if row.get("min_views") is None else int(row["min_views"]),
            "max_views": None if row.get("max_views") is None else int(row["max_views"]),
            "posted_within_days": None if row.get("posted_within_days") is None else int(row["posted_within_days"]),
            "posted_after": row.get("posted_after")}


def _views_filter(batch: dict[str, Any]) -> dict[str, Any] | None:
    """Filtros da importação por perfil (migrations 0017 e 0019): totais por veredito + a página aberta (`pending`),
    como em `_listing_dropped`. None = lote sem filtro."""
    if not _filters_on(batch):
        return None

    def counts(value: Any) -> dict[str, int]:
        data = load_listing_counts(value)
        return {key: v if isinstance(v := data.get(key), int) and not isinstance(v, bool) and v > 0 else 0
                for key in VIEWS_KEYS_MAX}

    total, partial = counts(batch.get("listing_views")), counts(batch.get("listing_views_partial"))
    merged = {key: total[key] + partial[key] for key in VIEWS_KEYS_MAX}
    return {**_filter_settings(batch), "analyzed": sum(merged.values()), **merged, "pending": sum(partial.values())}


def _group_views_filter(group: dict[str, Any], row: dict[str, Any]) -> dict[str, Any] | None:
    """Soma dos filhos (group_aggregates); None = grupo sem filtro."""
    if not _filters_on(group):
        return None
    merged = {key: int(row.get(f"views_{key}") or 0) for key in VIEWS_KEYS_MAX}
    return {**_filter_settings(group), "analyzed": sum(merged.values()), **merged, "pending": 0}


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
    # Grupo parte de um pedido com links (migration 0024): mostra a exportação automática do pedido.
    auto_rows = {row["id"]: row for row in with_requests(ctx, groups)}
    autos = exports_for(ctx, list(auto_rows.values()))
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
            "profile_end_count": int(row.get("profile_end") or 0),
            "period_end_count": int(row.get("period_end") or 0),
            "media": media_choice(group),
            "auto_export": auto_export_out(auto_rows[group["id"]], autos),
            "progress": {"found": found, **{key: int(row.get(key, 0)) for key in _PROGRESS_KEYS},
                         "filtered": int(row.get("type_filtered") or 0) + sum(
                             int(row.get(f"views_{key}") or 0) for key in _FILTERED_VERDICTS)},
            "listing_dropped_total": int(row.get("dropped", 0)),
            "views_filter": _group_views_filter(group, row),
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
    auto_rows = {row["id"]: row for row in with_requests(ctx, batches)}
    autos = exports_for(ctx, list(auto_rows.values()))
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
        # Listagem encerrada pelo corte do período (migration 0019): a coluna da 0012 guarda 'profile_end'.
        # 1a página vazia sem próxima página (migration 0022): idem, apresentado como "empty_first_page".
        data["listing_end"] = batch.get("listing_end")
        if data["listing_end"] == "profile_end" and batch.get("listing_period_end"):
            data["listing_end"] = "period_end"
        elif data["listing_end"] == "profile_end" and batch.get("listing_empty_first_page"):
            data["listing_end"] = "empty_first_page"
        data["views_filter"] = _views_filter(batch)
        data["media"] = media_choice(batch) if batch["source"] == "instagram_import" else None
        data["auto_export"] = auto_export_out(auto_rows[batch["id"]], autos)
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
        views = data["views_filter"] or {}
        data["progress"] = {"found": found, **progress,
                            "filtered": _type_filtered(batch) + sum(int(views.get(k) or 0) for k in _FILTERED_VERDICTS)}
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
                "role": media.get("role") or "original",
                "original_filename": media["original_filename"],
                "original_sha256": media["original_sha256"],
                "file_size": media["file_size"],
                "duration": media["duration"],
                "width": media["width"],
                "height": media["height"],
                "fps": media["fps"],
                "has_audio": None if has_audio is None else bool(has_audio),
                "video_codec": media.get("video_codec"),
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
