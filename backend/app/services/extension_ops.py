"""Operações da extensão como central (docs/EXTENSION.md; docs/CONTRACTS.md, seção 8.7).

Funções finas sobre os serviços e repositórios existentes: nada aqui baixa, processa, edita ou exporta por conta
própria. As respostas trazem só o necessário para a extensão: nenhum caminho de arquivo, URL de mídia, cookie ou token
(a única exceção é o `download_url` de um ZIP pronto, que já é o link de download do app com o token próprio dele).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any

from app.core.errors import AppError, not_found
from app.db.connection import utc_now
from app.db.repositories import edit_folders
from app.db.repositories.common import placeholders
from app.services import edit_service
from app.services.context import AppContext
from app.services.instagram_pause import REASON_LABELS, local_time_label
from app.services.presenters import _group_state, batches_out, groups_out

# --------------------------------------------------------------------------- caches em memória (TTL de 10 min)
CACHE_TTL_SECONDS = 600.0
_CACHE_MAX = 500


class TtlCache:
    """Cache pequeno em memória com validade; o mais antigo sai quando passa do teto."""

    def __init__(self, ttl: float = CACHE_TTL_SECONDS, max_items: int = _CACHE_MAX) -> None:
        self.ttl = ttl
        self.max_items = max_items
        self._data: dict[Any, tuple[float, Any]] = {}
        self.lock = threading.Lock()

    def get(self, key: Any) -> Any | None:
        now = time.monotonic()
        with self.lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if now - entry[0] > self.ttl:
                self._data.pop(key, None)
                return None
            return entry[1]

    def put(self, key: Any, value: Any) -> None:
        now = time.monotonic()
        with self.lock:
            self._data[key] = (now, value)
            if len(self._data) > self.max_items:
                for old in sorted(self._data, key=lambda k: self._data[k][0])[: len(self._data) - self.max_items]:
                    self._data.pop(old, None)

    def clear(self) -> None:
        with self.lock:
            self._data.clear()


# --------------------------------------------------------------------------- idempotência do start (persistida)
# `client_request_id` → resposta pública do start, por dispositivo, em `app_state` (sem migração): sobrevive a um
# reinício do app. Uma chave por dispositivo; cada entrada expira em 10 min. Guarda só o id do pedido, a validade e a
# resposta pública (ids de lote/grupo, contagens e o `open_url`): nenhum token, cookie ou caminho de arquivo.
START_REPLAY_PREFIX = "extension_start_requests:"
START_REPLAY_TTL_SECONDS = 600
START_REPLAY_MAX_PER_DEVICE = 200


def _replay_key(device_id: str) -> str:
    return f"{START_REPLAY_PREFIX}{device_id}"


def _live_replays(ctx: AppContext, device_id: str, now: float) -> tuple[dict[str, Any], bool]:
    """(entradas válidas do dispositivo, se havia alguma expirada ou inválida para limpar)."""
    raw = ctx.app_settings.get_state(_replay_key(device_id))
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    live = {key: entry for key, entry in data.items()
            if isinstance(entry, dict) and isinstance(entry.get("expires_at"), (int, float))
            and entry["expires_at"] > now and isinstance(entry.get("response"), dict)}
    return live, len(live) != len(data)


def _save_replays(ctx: AppContext, device_id: str, entries: dict[str, Any]) -> None:
    if entries:
        ctx.app_settings.set_state(_replay_key(device_id), json.dumps(entries, separators=(",", ":")))
    else:
        ctx.db.execute("DELETE FROM app_state WHERE key = ?", (_replay_key(device_id),))


def start_replay_get(ctx: AppContext, device_id: str, request_id: str, *,
                     now: float | None = None) -> dict[str, Any] | None:
    """Resposta guardada para este `client_request_id` do dispositivo (dentro dos 10 min), ou None. Remove as
    entradas expiradas do dispositivo. Chamar sob a trava do start."""
    now = time.time() if now is None else now
    live, dirty = _live_replays(ctx, device_id, now)
    if dirty:
        _save_replays(ctx, device_id, live)
    entry = live.get(request_id)
    return entry["response"] if entry is not None else None


def start_replay_put(ctx: AppContext, device_id: str, request_id: str, response: dict[str, Any], *,
                     now: float | None = None) -> None:
    """Grava a resposta do start (validade de 10 min), limpa as expiradas do dispositivo e as chaves de dispositivos
    sem gravação há mais de 10 min (todas as entradas delas já expiraram). Chamar sob a trava do start."""
    now = time.time() if now is None else now
    live, _ = _live_replays(ctx, device_id, now)
    live[request_id] = {"expires_at": now + START_REPLAY_TTL_SECONDS, "response": response}
    if len(live) > START_REPLAY_MAX_PER_DEVICE:
        for old in sorted(live, key=lambda key: live[key]["expires_at"])[: len(live) - START_REPLAY_MAX_PER_DEVICE]:
            live.pop(old, None)
    with ctx.db.transaction():
        _save_replays(ctx, device_id, live)
        ctx.db.execute("DELETE FROM app_state WHERE substr(key, 1, ?) = ? AND key <> ? AND updated_at < ?",
                       (len(START_REPLAY_PREFIX), START_REPLAY_PREFIX, _replay_key(device_id),
                        utc_now(-START_REPLAY_TTL_SECONDS)))


# --------------------------------------------------------------------------- page-info: o que já está na biblioteca
def post_library(ctx: AppContext, shortcode: str) -> dict[str, Any]:
    rows = ctx.db.fetchall(
        "SELECT m.id, m.media_type, m.role, m.source_media_index, m.source_profile,"
        " EXISTS (SELECT 1 FROM renditions r WHERE r.media_id = m.id) AS has_edited"
        " FROM media m WHERE m.source_post_id = ? ORDER BY m.source_media_index, m.role DESC, m.id",
        (shortcode,),
    )
    items = [{"media_id": int(r["id"]), "media_type": r["media_type"], "role": r["role"] or "original",
              "index": int(r["source_media_index"] or 0), "has_edited": bool(r["has_edited"])} for r in rows]
    # Dono da publicação quando já está na Biblioteca (a página de um Reel não traz o @ na URL).
    owner = next((r["source_profile"] for r in rows if r["source_profile"]), None)
    return {"in_library": bool(items), "items": items, "source_profile": owner.lower() if owner else None}


def profile_library(ctx: AppContext, username: str) -> dict[str, int]:
    row = ctx.db.fetchone(
        "SELECT COUNT(*) AS total,"
        " SUM(role = 'original' AND media_type = 'video') AS videos,"
        " SUM(role = 'original' AND media_type = 'image') AS photos,"
        " SUM(role = 'cover') AS covers"
        " FROM media WHERE source_profile = ? COLLATE NOCASE",
        (username,),
    )
    return {"media_count": int(row["total"] or 0), "videos": int(row["videos"] or 0),
            "photos": int(row["photos"] or 0), "covers": int(row["covers"] or 0)}


# --------------------------------------------------------------------------- post-preview
def preview_out(shortcode: str, items: list[Any]) -> dict[str, Any]:
    """Resumo de uma publicação resolvida (sem URL de mídia)."""
    ordered = sorted(items, key=lambda item: int(item.source_media_index or 0))
    if len(ordered) > 1:
        kind = "carousel"
    elif ordered and ordered[0].media_type == "video":
        kind = "reel" if ordered[0].is_reel else "video"
    else:
        kind = "photo"
    out_items = [{"index": int(item.source_media_index or 0), "media_type": item.media_type,
                  "has_cover": item.media_type == "video" and bool(item.thumbnail_url),
                  "width": item.width, "height": item.height} for item in ordered]
    owner = next((item.source_profile for item in ordered if item.source_profile), None)
    return {
        "shortcode": shortcode,
        "username": owner.lower() if owner else None,
        "kind": kind,
        "items": out_items,
        "counts": {"videos": sum(1 for i in out_items if i["media_type"] == "video"),
                   "photos": sum(1 for i in out_items if i["media_type"] == "image"),
                   "covers": sum(1 for i in out_items if i["has_cover"])},
    }


# --------------------------------------------------------------------------- activity
_VIEWS_ZERO = {"approved": 0, "below": 0, "no_count": 0, "out_of_period": 0, "above": 0}


def _progress(data: dict[str, Any], counts: dict[str, int], analyzed: int) -> dict[str, int]:
    prog = data["progress"]
    views = data.get("views_filter") or _VIEWS_ZERO
    files = int(prog.get("found", 0))
    filtered = int(prog.get("filtered", 0))
    return {
        "found": files + filtered,
        "analyzed": analyzed,
        "files": files,
        "downloaded": int(prog.get("downloaded", 0)),
        "duplicates": int(prog.get("duplicates", 0)),
        "filtered_period": int(views.get("out_of_period") or 0),
        "filtered_views": int(views.get("below") or 0) + int(views.get("above") or 0),
        "no_count": int(views.get("no_count") or 0),
        "failed": int(prog.get("failed", 0)),
        "processed": int(prog.get("processed", 0)),
        "processing": int(counts.get("downloaded", 0)) + int(counts.get("processing", 0)),
        "pending": int(counts.get("pending", 0)) + int(counts.get("downloading", 0)),
    }


def _finished_state(status: str, progress: dict[str, int]) -> str:
    if status == "cancelled":
        return "cancelled"
    if status == "failed":
        return "failed"
    ok = progress["downloaded"] + progress["duplicates"] + progress["processed"]
    if progress["failed"] > 0:
        return "partial" if ok > 0 else "failed"
    return "done"


def _batch_state(batch: dict[str, Any], data: dict[str, Any], progress: dict[str, int]) -> str:
    status = batch["status"]
    if status in ("completed", "failed", "cancelled"):
        return _finished_state(status, progress)
    if status == "listing":
        state = data.get("listing_state") or "pending"
        if state == "running" or (state == "pending" and batch.get("listing_next_page") is not None):
            return "analyzing" if progress["pending"] == 0 else "downloading"
        if progress["pending"] > 0:
            return "downloading"
        return "waiting"
    if progress["pending"] > 0:
        return "downloading"
    return "processing"


# Textos do aviso do item da Atividade (os mesmos do app: lib/batchState.ts e lib/importGroup.ts; a 1a página vazia
# usa o texto que o import runner grava no lote, com este como reserva).
EMPTY_FIRST_PAGE_MESSAGE = (
    "O Instagram não devolveu nenhuma publicação deste perfil agora (perfil sem posts, privado ou limite temporário). "
    "Tente de novo mais tarde."
)
LISTING_PAUSED_MESSAGE = "A coleta da lista está em pausa. O Media Tool tenta de novo sozinho e continua de onde parou."
_MAX_GROUP_MESSAGES = 3
ACTIVE_STATES = frozenset({"waiting", "analyzing", "downloading", "processing"})


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _row_message(batch: dict[str, Any], now: str) -> str | None:
    """Aviso de um lote (linha do banco) para a Atividade: 1a página vazia, atenção, falha ou pausa; None se nada."""
    if (batch.get("listing_end") == "profile_end" and batch.get("listing_empty_first_page")
            and not batch.get("listing_period_end")):
        return batch.get("error_message") or EMPTY_FIRST_PAGE_MESSAGE
    if batch.get("listing_state") == "needs_attention":
        reason = batch.get("error_message")
        return ("A importação parou e precisa de você." + (f" Motivo: {_sentence(reason)}" if reason else "")
                + " O que já foi baixado continua salvo; ao retomar, ela continua de onde parou.")
    if batch["status"] == "failed":
        return _sentence(batch["error_message"]) if batch.get("error_message") else "A importação falhou."
    if batch["status"] in ("completed", "cancelled"):
        return None
    until = batch.get("paused_until")
    if until and until > now:
        return ("Importação pausada depois de várias falhas seguidas. Retomada automática prevista para "
                f"{local_time_label(until)}.")
    resume_at = batch.get("rate_limit_resume_at")
    if batch.get("listing_state") == "running" and resume_at and resume_at > now:
        return ("Aguardando limite de requisições. O lote continua ativo e retoma automaticamente em "
                f"{local_time_label(resume_at)}.")
    if batch.get("listing_state") == "paused":
        return LISTING_PAUSED_MESSAGE
    return None


def _instagram_pause_message(ctx: AppContext) -> str | None:
    """Pausa geral do acesso automático ao Instagram (o mesmo texto de `ensure_not_paused`)."""
    if ctx.worker is None:
        return None
    until, reason = ctx.worker.pause_state()
    if until is None:
        return None
    label = REASON_LABELS.get(reason or "", "pausa preventiva")
    return (f"O acesso automático ao Instagram está pausado até {local_time_label(until)} (motivo: {label}). "
            "Aguarde ou libere a pausa em Configurações.")


def _batch_title(batch: dict[str, Any], links: dict[str, int] | None) -> str:
    if batch["source"] == "library":
        if edit_service.is_edit(batch):
            return f"Edição de {int(batch['found'] or 0)} vídeo(s)"
        return f"Processamento: {batch.get('preset_name') or 'preset'}"
    if batch.get("import_mode") == "links":
        total = int((links or {}).get("total", 0))
        return f"{total} publicação(ões)" if total != 1 else "1 publicação"
    return f"@{batch['source_profile']}" if batch.get("source_profile") else "Importação"


def _filters(row: dict[str, Any]) -> dict[str, Any]:
    return {"limit": row.get("requested_limit"), "posted_within_days": row.get("posted_within_days"),
            "min_views": row.get("min_views"), "max_views": row.get("max_views")}


def app_url(ctx: AppContext, path: str) -> str:
    return f"http://127.0.0.1:{ctx.settings.port}{path}"


def _auto_export(ctx: AppContext, auto: dict[str, Any] | None) -> dict[str, Any] | None:
    if auto is None:
        return None
    url = auto.get("download_url")
    return {"content": auto["content"], "status": auto["status"], "export_id": auto.get("export_id"),
            "error_message": auto.get("error_message"), "request_id": auto.get("request_id"),
            "download_url": app_url(ctx, url) if url else None}


def batch_activity(ctx: AppContext, batch: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    links = data.get("links")
    if batch.get("import_mode") == "links":
        analyzed = int((links or {}).get("resolved", 0)) + int((links or {}).get("failed", 0))
    else:
        analyzed = int(batch.get("listing_posts_seen") or 0)
    progress = _progress(data, data["counts"], analyzed)
    state = _batch_state(batch, data, progress)
    message = _row_message(batch, utc_now())
    if message is None and state in ACTIVE_STATES and batch["source"] == "instagram_import":
        message = _instagram_pause_message(ctx)
    return {
        "kind": "batch",
        "id": batch["id"],
        "title": _batch_title(batch, links),
        "created_at": batch["created_at"],
        "status": batch["status"],
        "state": state,
        "message": message,
        "media_set": data.get("media"),
        "filters": _filters(batch),
        "progress": progress,
        "auto_export": _auto_export(ctx, data.get("auto_export")),
        "open_url": app_url(ctx, f"/fila?lote={batch['id']}"),
    }


def group_activity(ctx: AppContext, group: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    children = ctx.db.fetchall(
        "SELECT * FROM batches WHERE group_id = ? ORDER BY group_position, id", (group["id"],))
    child_rows = [dict(row) for row in children]
    counts = {"pending": 0, "downloading": 0, "downloaded": 0, "processing": 0}
    open_ids = [row["id"] for row in child_rows if row["status"] not in ("completed", "failed", "cancelled")]
    if open_ids:
        for row in ctx.db.fetchall(
            f"SELECT status, COUNT(*) AS n FROM jobs WHERE batch_id IN ({placeholders(len(open_ids))})"
            " AND status IN ('pending', 'downloading', 'downloaded', 'processing') GROUP BY status",
            tuple(open_ids),
        ):
            counts[row["status"]] = int(row["n"])
    analyzed = sum(int(row.get("listing_posts_seen") or 0) for row in child_rows)
    progress = _progress(data, counts, analyzed)
    profiles = data["profiles"]
    if data["status"] in ("completed", "cancelled"):
        if data["status"] == "cancelled":
            state = "cancelled"
        elif profiles["completed"] == 0:
            state = "failed"
        else:
            state = _finished_state("completed", progress)
            if state == "done" and (profiles["failed"] or profiles["cancelled"]):
                state = "partial"
    elif progress["pending"] > 0:
        state = "downloading"
    elif any(_group_state(row) == "active" and row["status"] == "listing" for row in child_rows):
        state = "analyzing"
    elif progress["processing"] > 0:
        state = "processing"
    else:
        state = "waiting"
    now = utc_now()
    notes = [f"@{row['source_profile']}: {text}" for row in child_rows
             if (text := _row_message(row, now)) is not None]
    message = "; ".join(notes[:_MAX_GROUP_MESSAGES]) or None
    if len(notes) > _MAX_GROUP_MESSAGES:
        message += f" (e mais {len(notes) - _MAX_GROUP_MESSAGES} perfis com aviso)"
    if message is None and state in ACTIVE_STATES:
        message = _instagram_pause_message(ctx)
    title = ", ".join(f"@{row['source_profile']}" for row in child_rows[:3] if row.get("source_profile"))
    if len(child_rows) > 3:
        title += f" e mais {len(child_rows) - 3}"
    return {
        "kind": "group",
        "id": group["id"],
        "title": title or f"{profiles['total']} perfis",
        "created_at": group["created_at"],
        "status": data["status"],
        "state": state,
        "message": message,
        "media_set": data.get("media"),
        "filters": _filters(group),
        "progress": progress,
        "auto_export": _auto_export(ctx, data.get("auto_export")),
        "open_url": app_url(ctx, "/"),
    }


def group_children(ctx: AppContext, group_id: int, *, after_position: int | None, limit: int) -> dict[str, Any]:
    """Lotes filhos (um por perfil) de um grupo, em ordem de `group_position` (keyset `after_position`), cada um no
    mesmo formato de item de lote da Atividade (estado, progresso e aviso)."""
    group = ctx.batches.get_group(group_id)
    if group is None:
        raise not_found("Grupo de importação", "group_not_found")
    rows, next_after = ctx.batches.group_children(group_id, limit=limit, after_position=after_position)
    total = int(ctx.db.scalar("SELECT COUNT(*) FROM batches WHERE group_id = ?", (group_id,)))
    items = [{**batch_activity(ctx, row, data), "position": row.get("group_position")}
             for row, data in zip(rows, batches_out(ctx, rows))]
    return {"group_id": group_id, "items": items, "total": total, "next_after_position": next_after}


def activity(ctx: AppContext, limit: int) -> list[dict[str, Any]]:
    """Importações de nível superior (grupos e lotes avulsos, inclusive Edição/processamento), mais recentes primeiro."""
    batches, _ = ctx.batches.list(limit=limit, offset=0, ungrouped=True)
    groups, _, _ = ctx.batches.list_groups(limit=limit)
    entries: list[tuple[str, int, dict[str, Any]]] = []
    for batch, data in zip(batches, batches_out(ctx, batches)):
        entries.append((batch["created_at"], batch["id"], batch_activity(ctx, batch, data)))
    for group, data in zip(groups, groups_out(ctx, groups)):
        entries.append((group["created_at"], group["id"], group_activity(ctx, group, data)))
    entries.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    return [entry[2] for entry in entries[:limit]]


# --------------------------------------------------------------------------- itens de um lote
def stage_label(job: dict[str, Any]) -> str:
    status = job["status"]
    if status == "pending":
        return "aguardando"
    if status == "downloading":
        if job.get("stage") in ("hashing", "probing", "thumbnail", "finalizing"):
            return "analisando"
        if (job.get("media_role") or "original") == "cover":
            return "baixando capa"
        return "baixando vídeo" if job.get("media_type") == "video" else "baixando foto"
    if status in ("downloaded", "processing"):
        return "processando"
    if status == "completed":
        return "duplicado" if job.get("is_duplicate") else "concluído"
    return "falhou" if status == "failed" else "cancelado"


def batch_items(ctx: AppContext, batch_id: int, *, before_id: int | None, limit: int) -> dict[str, Any]:
    if ctx.batches.get(batch_id) is None:
        raise not_found("Lote", "batch_not_found")
    params: list[Any] = [batch_id]
    where = "j.batch_id = ?"
    if before_id is not None:
        where += " AND j.id < ?"
        params.append(before_id)
    rows = ctx.db.fetchall(
        "SELECT j.id, j.media_id, COALESCE(j.media_type, m.media_type) AS media_type, j.media_role, j.kind,"
        " COALESCE(j.source_media_index, m.source_media_index) AS idx,"
        " COALESCE(j.source_post_id, m.source_post_id) AS post, j.status, j.stage, j.is_duplicate, j.error_message"
        f" FROM jobs j LEFT JOIN media m ON m.id = j.media_id WHERE {where} ORDER BY j.id DESC LIMIT ?",
        (*params, limit + 1),
    )
    items = []
    for row in rows[:limit]:
        job = dict(row)
        role = job.get("media_role") or "original"
        item = {"id": job["id"], "media_id": job["media_id"],
                "media_type": "image" if role == "cover" else job["media_type"], "role": role,
                "index": None if job["idx"] is None else int(job["idx"]), "source_post_id": job["post"],
                "status": job["status"], "stage_label": stage_label(job)}
        if job["status"] == "failed" and job.get("error_message"):
            item["error_message"] = job["error_message"]
        items.append(item)
    total = int(ctx.db.scalar("SELECT COUNT(*) FROM jobs WHERE batch_id = ?", (batch_id,)))
    return {"items": items, "total": total, "next_before_id": rows[limit - 1]["id"] if len(rows) > limit else None}


# --------------------------------------------------------------------------- biblioteca
def library_scope(batch_id: int | None, group_id: int | None) -> tuple[str, list[Any]] | None:
    if batch_id is not None and group_id is not None:
        raise AppError(422, "validation_error", "Informe batch_id ou group_id (um dos dois).")
    if batch_id is not None:
        return edit_folders.folder_clause(f"batch:{int(batch_id)}")
    if group_id is not None:
        return edit_folders.folder_clause(f"group:{int(group_id)}")
    return None


def _processing_state(job: dict[str, Any] | None) -> str:
    if job is None or job.get("kind") != "process":
        return "none"
    if job["status"] == "pending":
        return "pending"
    if job["status"] in ("downloading", "downloaded", "processing"):
        return "processing"
    return "failed" if job["status"] == "failed" else "none"


def library_items(ctx: AppContext, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = [m["id"] for m in items]
    renditions = ctx.media.renditions_for(ids)
    last_jobs = ctx.media.last_jobs(ids)
    out = []
    for media in items:
        count = len(renditions.get(media["id"], []))
        out.append({
            "id": media["id"], "media_type": media["media_type"], "role": media.get("role") or "original",
            "source_profile": media["source_profile"], "source_post_id": media["source_post_id"],
            "index": media["source_media_index"], "taken_at": media["taken_at"], "created_at": media["created_at"],
            "width": media["width"], "height": media["height"], "duration": media["duration"],
            "has_edited": count > 0, "renditions": count,
            "processing": _processing_state(last_jobs.get(media["id"])),
            "has_thumbnail": bool(media["thumbnail_path"]) or media["media_type"] == "image",
        })
    return out


MAX_LIBRARY_IDS = 5000


def library_ids(ctx: AppContext, *, scope: tuple[str, list[Any]], media_type: str | None,
                role: str | None) -> dict[str, Any]:
    where, params = ctx.media.filter_where(media_type=media_type, role=role)
    where = f"{where} AND ({scope[0]})" if where else f"WHERE ({scope[0]})"
    rows = ctx.db.fetchall(f"SELECT m.id, m.media_type, m.role FROM media m {where} ORDER BY m.id DESC LIMIT ?",
                           (*params, *scope[1], MAX_LIBRARY_IDS + 1))
    truncated = len(rows) > MAX_LIBRARY_IDS
    rows = rows[:MAX_LIBRARY_IDS]
    # `items`: tipo e papel de cada id (o resumo da seleção e o aviso de "não-vídeos" da extensão dependem deles).
    return {"ids": [int(row["id"]) for row in rows],
            "items": [{"id": int(row["id"]), "media_type": row["media_type"], "role": row["role"] or "original"}
                      for row in rows],
            "truncated": truncated}


# --------------------------------------------------------------------------- miniatura
def thumbnail_file(ctx: AppContext, media: dict[str, Any]):
    """(caminho, mime) da miniatura: a existente; senão gera (FFmpeg) e grava como a pipeline; imagem sem FFmpeg: a
    própria imagem. Vídeo sem miniatura possível: 404 (nunca devolve o vídeo)."""
    from app.services.media_service import stored_file

    storage = ctx.storage
    if media.get("thumbnail_path"):
        try:
            return stored_file(ctx, media["thumbnail_path"], what="Miniatura"), "image/jpeg"
        except AppError:
            pass
    original = None
    if media.get("original_path"):
        try:
            original = stored_file(ctx, media["original_path"], what="Arquivo original")
        except AppError:
            original = None
    if original is not None and ctx.processors.ffmpeg_info().found:
        target = storage.thumb_path(media["id"])
        if not target.is_file():
            temp = storage.temp / f"ext-thumb-{media['id']}-{uuid.uuid4().hex}.jpg"
            try:
                storage.temp.mkdir(parents=True, exist_ok=True)
                ctx.processors.get().make_thumbnail(original, temp)
                storage.promote_thumbnail(temp, media["id"])
            except Exception:  # noqa: BLE001 - sem miniatura: cai no caso abaixo
                temp.unlink(missing_ok=True)
        if target.is_file():
            ctx.media.update(media["id"], thumbnail_path=storage.relative(target))
            return target, "image/jpeg"
    if original is not None and media["media_type"] == "image":
        mime = media.get("mime_type") or "image/jpeg"
        if mime.startswith("image/"):
            return original, mime
    raise AppError(404, "thumbnail_unavailable", "Miniatura indisponível para esta mídia.")


# --------------------------------------------------------------------------- edição e presets
def preset_summary(params: dict[str, Any]) -> str:
    parts = []
    speed = params.get("speed")
    if isinstance(speed, (int, float)) and speed != 1:
        parts.append(f"velocidade {speed:.2f}x".replace(".", ","))
    if params.get("mirror"):
        parts.append("espelhado")
    if params.get("metadata_cleanup"):
        parts.append("sem metadados")
    if params.get("trim_start"):
        parts.append(f"corta {params['trim_start']:g} s do início".replace(".", ","))
    if params.get("trim_end"):
        parts.append(f"corta {params['trim_end']:g} s do fim".replace(".", ","))
    if params.get("max_duration"):
        parts.append(f"até {params['max_duration']:g} s".replace(".", ","))
    if params.get("zoom"):
        parts.append("zoom")
    if params.get("sharpen"):
        parts.append("nitidez")
    if params.get("auto_adjust"):
        parts.append("ajuste automático")
    if params.get("reencode"):
        parts.append("recodifica")
    return ", ".join(parts) or "sem ajustes"


def presets_out(ctx: AppContext) -> list[dict[str, Any]]:
    return [{"id": p["id"], "name": p["name"], "summary": preset_summary(p.get("params") or {})}
            for p in ctx.presets.list()]


def split_videos(ctx: AppContext, media_ids: list[int]) -> tuple[list[int], list[dict[str, Any]]]:
    """(vídeos originais, pulados com motivo). Só vídeo original é editado/processado; foto e capa ficam de fora."""
    unique = list(dict.fromkeys(media_ids))
    found: dict[int, dict[str, Any]] = {}
    if unique:
        for row in ctx.db.fetchall(
            f"SELECT id, media_type, role FROM media WHERE id IN ({placeholders(len(unique))})", tuple(unique)
        ):
            found[int(row["id"])] = dict(row)
    videos: list[int] = []
    skipped: list[dict[str, Any]] = []
    for media_id in unique:
        media = found.get(media_id)
        if media is None:
            skipped.append({"media_id": media_id, "reason": "not_found"})
        elif media.get("role") == "cover":
            skipped.append({"media_id": media_id, "reason": "cover"})
        elif media["media_type"] != "video":
            skipped.append({"media_id": media_id, "reason": "not_video"})
        else:
            videos.append(media_id)
    return videos, skipped


def export_status(ctx: AppContext, export: dict[str, Any]) -> dict[str, Any]:
    from app.services.export_service import export_out

    data = export_out(export)
    return {"id": data["id"], "status": data["status"], "content": data["content"], "processed": data["processed"],
            "total": data["total"], "zip_ready": data["zip_ready"], "zip_bytes": data["zip_bytes"],
            "download_url": app_url(ctx, data["download_url"]) if data["download_url"] else None,
            "error_message": data["error_message"]}

