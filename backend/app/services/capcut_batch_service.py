"""Lotes das Legendas CapCut (docs/CAPCUT.md, "Lotes"): criação (por ids ou por filtro), apresentação com contagens,
status derivado e estimativa, pausa/retomada, cancelamento, reprocessamento das falhas, ZIP, pasta, o seletor de
vídeos da página e a visão da lista do seletor ("Limpar lista", sem apagar nada; `app_state`).

Um lote é um `capcut_batches` com um `capcut_jobs` por vídeo, na mesma fila e no mesmo executor dos jobs avulsos
(`app.jobs.capcut_runner`). Nos modos com edição, a edição é um lote `batches` comum de jobs `process` sem preset
(a mesma forma de `edit_service.start`), feito pelo worker em paralelo à fila do CapCut."""

from __future__ import annotations

import json
import logging
import threading
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from app.core.errors import AppError, not_found
from app.core.security import UnsafePathError, ensure_within
from app.db.repositories import edit_folders
from app.db.repositories.capcut import OPEN_STATUSES
from app.schemas.presets import PresetParams
from app.services import capcut_service, export_time, export_zip, import_service, system_open
from app.services.edit_service import EDIT_DURATION_MARGIN, EDIT_PRESET_NAME, duration_limit
from app.services.presenters import media_out
from app.services.system_open import SystemOpenError

if TYPE_CHECKING:
    from app.schemas.capcut import BatchCreate, BatchEdit
    from app.services.context import AppContext

logger = logging.getLogger("media_tool.capcut")

MAX_BATCH_VIDEOS = 1000
EDIT_MODES = ("edit", "edit_caption")
CAPTION_MODES = ("caption", "edit_caption")
ESTIMATE_SAMPLES = 20
ZIP_TEMP_PREFIX = "capcut-lote-"   # temporários do ZIP do lote em `<data_dir>/tmp` (sobras apagadas no startup)
_CAPTIONED_SQL = ("EXISTS (SELECT 1 FROM capcut_jobs c WHERE c.media_id = m.id AND c.status = 'completed'"
                  " AND c.mode IN ('caption', 'edit_caption'))")


def repo(ctx: AppContext):
    return capcut_service.repo(ctx)


# ---- seleção --------------------------------------------------------------------------------------------------------
def _scope(folder: str | None, captioned: str | None) -> tuple[str, list[Any]] | None:
    """Condição extra (sobre `media m`): pasta do Editar e "já legendado" (job concluído com legenda)."""
    clauses: list[str] = []
    params: list[Any] = []
    if folder:
        clause, values = edit_folders.folder_clause(folder)
        clauses.append(f"({clause})")
        params.extend(values)
    if captioned == "yes":
        clauses.append(_CAPTIONED_SQL)
    elif captioned == "no":
        clauses.append(f"NOT {_CAPTIONED_SQL}")
    return (" AND ".join(clauses), params) if clauses else None


# ---- visão da lista do seletor ("Limpar lista") --------------------------------------------------------------------
LIBRARY_VIEW_KEY = "capcut_library_view"
LIBRARY_VIEW_MAX_IDS = 20_000   # teto de `hidden` e de `shown` (sem repetir; acima dele ficam os mais recentes)
_view_lock = threading.Lock()


def _empty_view() -> dict[str, Any]:
    return {"cleared_before_id": None, "hidden": [], "shown": []}


def _id_list(value: Any) -> list[int]:
    """Ids positivos sem repetir (vale a última ocorrência), no máximo LIBRARY_VIEW_MAX_IDS (os mais recentes)."""
    if not isinstance(value, list):
        return []
    ids = [item for item in value if isinstance(item, int) and not isinstance(item, bool) and item > 0]
    unique = list(reversed(dict.fromkeys(reversed(ids))))
    return unique[-LIBRARY_VIEW_MAX_IDS:]


def library_view(ctx: AppContext) -> dict[str, Any]:
    """`{"cleared_before_id": int|None, "hidden": [ids], "shown": [ids]}`; valor ausente ou inválido = visão vazia."""
    raw = ctx.app_settings.get_state(LIBRARY_VIEW_KEY)
    if not raw:
        return _empty_view()
    try:
        data = json.loads(raw)
    except ValueError:
        return _empty_view()
    if not isinstance(data, dict):
        return _empty_view()
    cleared = data.get("cleared_before_id")
    valid = isinstance(cleared, int) and not isinstance(cleared, bool) and cleared > 0
    return {"cleared_before_id": cleared if valid else None, "hidden": _id_list(data.get("hidden")),
            "shown": _id_list(data.get("shown"))}


def _save_view(ctx: AppContext, view: dict[str, Any]) -> None:
    empty = view["cleared_before_id"] is None and not view["hidden"] and not view["shown"]
    ctx.app_settings.set_state(LIBRARY_VIEW_KEY, None if empty else json.dumps(view, separators=(",", ":")))


def view_summary(view: dict[str, Any]) -> dict[str, Any]:
    return {"cleared": view["cleared_before_id"] is not None, "hidden_count": len(view["hidden"])}


def _visible_scope(view: dict[str, Any]) -> tuple[str, list[Any]] | None:
    """Condição (sobre `media m`) dos vídeos visíveis: (sem limpeza OU id > cleared_before_id OU id em shown) E id fora
    de hidden. As listas vão como JSON para `json_each` (sem um parâmetro por id)."""
    clauses: list[str] = []
    params: list[Any] = []
    cleared = view["cleared_before_id"]
    if cleared is not None:
        if view["shown"]:
            clauses.append("(m.id > ? OR m.id IN (SELECT value FROM json_each(?)))")
            params.extend([cleared, json.dumps(view["shown"])])
        else:
            clauses.append("m.id > ?")
            params.append(cleared)
    if view["hidden"]:
        clauses.append("m.id NOT IN (SELECT value FROM json_each(?))")
        params.append(json.dumps(view["hidden"]))
    return (" AND ".join(clauses), params) if clauses else None


def _merge_scopes(*scopes: tuple[str, list[Any]] | None) -> tuple[str, list[Any]] | None:
    present = [scope for scope in scopes if scope is not None]
    if not present:
        return None
    return " AND ".join(f"({clause})" for clause, _ in present), [value for _, values in present for value in values]


def get_library_view(ctx: AppContext) -> dict[str, Any]:
    return view_summary(library_view(ctx))


def clear_library_view(ctx: AppContext, media_ids: list[int] | None) -> dict[str, Any]:
    """Sem ids: os vídeos atuais saem da lista (`cleared_before_id` = maior id de mídia) e as listas zeram. Com ids:
    entram em `hidden` e saem de `shown`. Nada é apagado."""
    with _view_lock:
        if media_ids is None:
            top = ctx.db.scalar("SELECT MAX(id) FROM media")
            view = {"cleared_before_id": int(top) if top else None, "hidden": [], "shown": []}
        else:
            view = library_view(ctx)
            gone = set(media_ids)
            view["hidden"] = _id_list([*view["hidden"], *media_ids])
            view["shown"] = [item for item in view["shown"] if item not in gone]
        _save_view(ctx, view)
    logger.info("Legendas CapCut: lista do seletor limpa (%s)",
                "todos" if media_ids is None else f"{len(media_ids)} vídeo(s)")
    return view_summary(view)


def show_in_library_view(ctx: AppContext, media_ids: list[int]) -> dict[str, Any]:
    """Os ids voltam à lista (entram em `shown`, saem de `hidden`), ex.: envio do computador que é duplicata de um
    vídeo antigo que saiu da lista."""
    with _view_lock:
        view = library_view(ctx)
        back = set(media_ids)
        view["hidden"] = [item for item in view["hidden"] if item not in back]
        view["shown"] = _id_list([*view["shown"], *media_ids])
        _save_view(ctx, view)
    return view_summary(view)


def restore_library_view(ctx: AppContext) -> dict[str, Any]:
    """Zera a visão: todos os vídeos voltam à lista."""
    with _view_lock:
        _save_view(ctx, _empty_view())
    logger.info("Legendas CapCut: lista do seletor restaurada")
    return view_summary(_empty_view())


def _clean(text: str | None) -> str | None:
    return (text or "").strip().lstrip("@") or None


def _filter_where(ctx: AppContext, selection: dict[str, Any]) -> tuple[str, list[Any]]:
    where, params = ctx.media.filter_where(media_type="video", role="original", profile=_clean(selection.get("profile")),
                                           q=(selection.get("q") or "").strip() or None)
    scope = _merge_scopes(_scope(selection.get("folder"), selection.get("captioned")), _visible_scope(library_view(ctx)))
    if scope is not None:
        where = f"{where} AND ({scope[0]})"
        params = [*params, *scope[1]]
    return where, params


def _too_large(count: int) -> AppError:
    return AppError(422, "batch_too_large",
                    f"Um lote aceita no máximo {MAX_BATCH_VIDEOS} vídeos ({count} selecionados).")


def _filter_ids(ctx: AppContext, selection: dict[str, Any]) -> list[int]:
    where, params = _filter_where(ctx, selection)
    count = int(ctx.db.scalar(f"SELECT COUNT(*) FROM media m {where}", tuple(params)))
    if count > MAX_BATCH_VIDEOS:
        raise _too_large(count)
    rows = ctx.db.fetchall(f"SELECT m.id FROM media m {where} ORDER BY m.id", tuple(params))
    return [int(row["id"]) for row in rows]


def library(ctx: AppContext, *, q: str | None, profile: str | None, folder: str | None, captioned: str | None,
            limit: int, before_id: int | None) -> dict[str, Any]:
    """Seletor da página: vídeos originais (sem capas) visíveis na visão da lista, id DESC, keyset, com o resumo das
    Legendas CapCut."""
    items, total, next_before_id = ctx.media.list(
        view="all", media_type="video", role="original", profile=_clean(profile), q=(q or "").strip() or None,
        limit=limit, before_id=before_id,
        scope=_merge_scopes(_scope(folder, captioned), _visible_scope(library_view(ctx))))
    info = repo(ctx).media_capcut_info([item["id"] for item in items])
    out = media_out(ctx, items)
    for item in out:
        row = info.get(item["id"]) or {}
        last = row.get("last_output")
        item["capcut"] = {"done": int(row.get("done") or 0), "last_output_name": Path(last).name if last else None,
                          "open": bool(row.get("open"))}
    return {"items": out, "total": total, "next_before_id": next_before_id}


# ---- edição ---------------------------------------------------------------------------------------------------------
def _number(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def _speed_text(speed: float) -> str:
    return f"{speed:.1f}".replace(".", ",") + "x" if round(speed, 1) == speed else f"{speed:.2f}".replace(".", ",") + "x"


def edit_plan(ctx: AppContext, edit: BatchEdit) -> dict[str, Any]:
    """Params do processor para a edição do lote + resumo. Base: os params do preset escolhido ou, sem preset, os da
    edição (sem limpeza de metadados, espelho e auto_adjust, como `edit_service.edit_params`); por cima: velocidade,
    nitidez, limite de duração (+0,03 s), MP4 (`reencode`, o CapCut precisa) e áudio sincronizado. O início do corte
    passa do teto de 30 s dos presets, por isso é gravado depois da validação (como no `edit_service`)."""
    preset = None
    if edit.preset_id is not None:
        preset = ctx.presets.get(edit.preset_id)
        if preset is None:
            raise not_found("Preset", "preset_not_found")
    limit = duration_limit(edit)
    start = round(float(edit.start_seconds), 3)
    if (preset is None and limit is None and edit.speed == 1.0 and not edit.sharpen and start == 0
            and not edit.mirror):
        raise AppError(422, "edit_no_changes", "Nenhuma edição selecionada.")
    base = dict(preset["params"]) if preset is not None else {
        "metadata_cleanup": False, "mirror": False, "auto_adjust": False}
    merged = {**base, "trim_start": 0.0, "speed": float(edit.speed), "sharpen": bool(edit.sharpen),
              "max_duration": 0.0 if limit is None else round(limit + EDIT_DURATION_MARGIN, 6), "reencode": True,
              "audio_sync_start": True}
    if edit.mirror is not None:
        merged["mirror"] = bool(edit.mirror)   # escolha do lote vale mais que a do preset
    try:
        params = PresetParams(**merged).model_dump()
    except ValidationError:
        raise AppError(422, "invalid_preset", "Os ajustes do preset escolhido não são válidos.") from None
    params["trim_start"] = start
    parts = [preset["name"]] if preset is not None else []
    parts.append("duração original" if limit is None else f"{_number(limit)} s")
    parts.append(_speed_text(float(edit.speed)))
    if edit.sharpen:
        parts.append("nitidez")
    if params.get("mirror"):
        parts.append("espelhado")
    if start > 0:
        parts.append(f"início em {_number(start)} s")
    return {"params": params, "preset_id": edit.preset_id, "preset_name": preset["name"] if preset else None,
            "duration_mode": edit.duration_mode, "custom_seconds": edit.custom_seconds, "start_seconds": start,
            "speed": float(edit.speed), "sharpen": bool(edit.sharpen), "mirror": bool(params.get("mirror")),
            "summary": " · ".join(parts)}


# ---- criação --------------------------------------------------------------------------------------------------------
def _default_name(ctx: AppContext, batch_id: int) -> str:
    local = export_time.resolve_zone(ctx.settings.timezone)(datetime.now(UTC))
    return f"Lote {batch_id} · {local:%d/%m %H:%M}"


def prepare(ctx: AppContext, mode: str, caption: Any, edit: BatchEdit | None) -> dict[str, Any]:
    """Configuração validada de um lote (mesmas regras e erros para a página e para a importação): plano da edição,
    opções dos jobs (com o modelo de legenda), instantâneo da legenda e resumo."""
    if mode in EDIT_MODES and edit is None:
        raise AppError(422, "edit_required", "Escolha as configurações da edição.")
    plan = edit_plan(ctx, edit) if mode in EDIT_MODES else None
    overrides = caption.overrides.model_dump(exclude_unset=True) if caption and caption.overrides else None
    style_id = caption.style_id if caption is not None and mode in CAPTION_MODES else None
    options = capcut_service._job_options(ctx, style_id, overrides)
    job_options = dict(options)
    if plan is not None:
        job_options["edit_params"] = plan["params"]
    summary = {
        "style_name": options["style_name"] if mode in CAPTION_MODES else None,
        "edit": plan["summary"] if plan is not None else None,
        "resolution": options["resolution"] if mode in CAPTION_MODES else None,
        "fps": options["fps"] if mode in CAPTION_MODES else None,
        "bitrate": options["bitrate"] if mode in CAPTION_MODES else None,
        "keep_audio": bool(options["keep_original_audio"]) if mode in CAPTION_MODES else None,
        "highlight": bool(options["highlight"]) if mode in CAPTION_MODES else None,
    }
    return {"mode": mode, "plan": plan, "options": options, "job_options": job_options, "summary": summary,
            "caption": {key: value for key, value in options.items() if key != "style"}}


def _batch_options(prepared: dict[str, Any], folder: str, **extra: Any) -> dict[str, Any]:
    mode = prepared["mode"]
    return {"mode": mode, **extra, "output_dir": folder,
            "caption": prepared["caption"] if mode in CAPTION_MODES else None,
            "keep_original_audio": bool(prepared["options"]["keep_original_audio"]),
            "edit": prepared["plan"], "summary": prepared["summary"]}


def _create_edit_batch(ctx: AppContext, plan: dict[str, Any]) -> int:
    return ctx.batches.create(source="library", status="running", preset_id=None, preset_name=EDIT_PRESET_NAME,
                              params=plan["params"], process=True)


def _add_video(ctx: AppContext, *, batch_id: int, media: dict[str, Any], mode: str, plan: dict[str, Any] | None,
               job_options: dict[str, Any], edit_batch_id: int | None, max_attempts: int, origin: str = "manual",
               import_batch_id: int | None = None) -> int:
    """Um vídeo no lote: o job `process` da edição (modos com edição) e o job do CapCut ligado a ele."""
    edit_job_id = None
    if plan is not None:
        edit_job_id = ctx.jobs.create_process(media=media, batch_id=edit_batch_id, preset_id=None,
                                              preset_name=EDIT_PRESET_NAME, params=plan["params"])
    return repo(ctx).create_job(media_id=media["id"], source_path=media["original_path"], options=job_options,
                                max_attempts=max_attempts, origin=origin, batch_id=import_batch_id,
                                capcut_batch_id=batch_id, mode=mode, edit_job_id=edit_job_id)


def create_batch(ctx: AppContext, body: BatchCreate) -> dict[str, Any]:
    """Um job por vídeo; pula o que já está na fila e (sem `include_done`) o que já foi concluído no mesmo modo.
    Nada é gravado quando nenhum vídeo entra (422 `batch_empty`)."""
    mode = body.mode
    prepared = prepare(ctx, mode, body.caption, body.edit)
    plan = prepared["plan"]
    max_attempts = int(capcut_service._stored(ctx)["max_attempts"])
    if body.media_ids is not None:
        wanted = list(dict.fromkeys(body.media_ids))
        if len(wanted) > MAX_BATCH_VIDEOS:
            raise _too_large(len(wanted))
    else:
        wanted = _filter_ids(ctx, body.filter.model_dump())
    skipped: list[dict[str, Any]] = []
    chosen: list[dict[str, Any]] = []
    with capcut_service._create_lock:
        medias = {media["id"]: media for media in ctx.media.get_many(wanted)}
        busy = repo(ctx).open_media_ids(wanted)
        done = set() if body.include_done else repo(ctx).done_media_ids(wanted, mode)
        for media_id in wanted:
            media = medias.get(media_id)
            reason = (
                "media_not_found" if media is None
                else "not_video" if media["media_type"] != "video"
                else "cover" if (media.get("role") or "original") == "cover"
                else "already_queued" if media_id in busy
                else "already_done" if media_id in done
                else None
            )
            if reason is not None:
                skipped.append({"media_id": media_id, "reason": reason})
            else:
                chosen.append(media)
        if not chosen:
            raise AppError(422, "batch_empty", "Nenhum vídeo da seleção entrou no lote (todos foram pulados).")
        job_options = dict(prepared["job_options"])
        with ctx.db.transaction():
            edit_batch_id = _create_edit_batch(ctx, plan) if plan is not None else None
            batch_id = repo(ctx).create_batch(name="", mode=mode, options={}, output_dir="",
                                              edit_batch_id=edit_batch_id)
            folder = str(Path(prepared["options"]["output_dir"]) / f"Lote {batch_id}")
            name = (body.name or "").strip() or _default_name(ctx, batch_id)
            repo(ctx).update_batch(batch_id, name=name, output_dir=folder, options=_batch_options(
                prepared, folder, include_done=body.include_done,
                selection=({"media_ids": len(wanted)} if body.media_ids is not None
                           else {"filter": body.filter.model_dump()})))
            job_options["output_dir"] = folder
            for media in chosen:
                _add_video(ctx, batch_id=batch_id, media=media, mode=mode, plan=plan, job_options=job_options,
                           edit_batch_id=edit_batch_id, max_attempts=max_attempts)
            if edit_batch_id is not None:
                ctx.batches.sync_found(edit_batch_id)
    logger.info("Legendas CapCut: lote %s criado (%s, %s vídeo(s), %s pulado(s))", batch_id, mode, len(chosen),
                len(skipped))
    if plan is not None:
        ctx.wake_worker()
    capcut_service._wake(ctx)
    return {"batch": get_batch(ctx, batch_id), "skipped": skipped}


# ---- lote da importação ("Edição CapCut" após baixar) ---------------------------------------------------------------
IMPORT_LINK_PREFIX = "capcut_import_link:"


def _link_key(kind: str, ref: int) -> str:
    return f"{IMPORT_LINK_PREFIX}{kind}:{int(ref)}"


# Índice em memória dos vínculos importação -> lote CapCut ({"batch:<id>"|"group:<id>": id do lote}), lido do banco
# uma vez por processo e atualizado na criação: a lista do Importar mostra o andamento sem consulta extra por página
# (as rotas de grupo têm orçamento fixo de consultas).
_link_index: dict[str, dict[str, int]] = {}
_link_index_lock = threading.Lock()


def import_links(ctx: AppContext) -> dict[str, int]:
    scope = str(ctx.settings.data_dir)
    with _link_index_lock:
        if scope not in _link_index:
            index: dict[str, int] = {}
            for row in ctx.db.fetchall("SELECT key, value FROM app_state WHERE key LIKE ?", (f"{IMPORT_LINK_PREFIX}%",)):
                try:
                    index[row["key"][len(IMPORT_LINK_PREFIX):]] = int(row["value"])
                except (TypeError, ValueError):
                    continue
            _link_index[scope] = index
        return _link_index[scope]


def _remember_link(ctx: AppContext, kind: str, ref: int, batch_id: int) -> None:
    with _link_index_lock:
        index = _link_index.get(str(ctx.settings.data_dir))
        if index is not None:
            index[f"{kind}:{int(ref)}"] = int(batch_id)


def create_import_batch(ctx: AppContext, prepared: dict[str, Any], *, name: str | None, batch_ids: list[int],
                        group_id: int | None) -> int:
    """Lote vazio vinculado à importação (chamado dentro da transação da criação dela): o instantâneo das opções e a
    pasta de saída como em `create_batch`, sem jobs. O vínculo vai para `app_state` (um por lote de importação e um
    pelo grupo, para os filhos). O lote de edição (`batches`) só é criado no primeiro vídeo (`add_import_video`)."""
    mode = prepared["mode"]
    with ctx.db.transaction():
        batch_id = repo(ctx).create_batch(name="", mode=mode, options={}, output_dir="", edit_batch_id=None)
        folder = str(Path(prepared["options"]["output_dir"]) / f"Lote {batch_id}")
        local = export_time.resolve_zone(ctx.settings.timezone)(datetime.now(UTC))
        source = {"kind": "import", "batch_ids": [int(item) for item in batch_ids], "group_id": group_id}
        repo(ctx).update_batch(batch_id, name=(name or "").strip() or f"Importação {local:%d/%m %H:%M}",
                               output_dir=folder, options=_batch_options(
                                   prepared, folder, include_done=False, selection={"import": True}, source=source,
                                   job_options={**prepared["job_options"], "output_dir": folder}))
        for import_batch_id in batch_ids:
            ctx.app_settings.set_state(_link_key("batch", import_batch_id), str(batch_id))
            _remember_link(ctx, "batch", import_batch_id, batch_id)
        if group_id is not None:
            ctx.app_settings.set_state(_link_key("group", group_id), str(batch_id))
            _remember_link(ctx, "group", group_id, batch_id)
    logger.info("Legendas CapCut: lote %s criado vazio para a importação (%s; lotes %s, grupo %s)", batch_id, mode,
                batch_ids, group_id)
    return batch_id


def _linked_id(ctx: AppContext, key: str) -> int | None:
    raw = ctx.app_settings.get_state(key)
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def linked_batch_id(ctx: AppContext, *, import_batch_id: int | None = None,
                    group_id: int | None = None) -> int | None:
    """Lote CapCut vinculado ao lote de importação (ou ao grupo dele), ou None."""
    if import_batch_id is not None:
        found = _linked_id(ctx, _link_key("batch", import_batch_id))
        if found is not None:
            return found
        if group_id is None:
            batch = ctx.batches.get(import_batch_id)
            group_id = batch.get("group_id") if batch is not None else None
    return _linked_id(ctx, _link_key("group", group_id)) if group_id is not None else None


def add_import_video(ctx: AppContext, capcut_batch_id: int, media_id: int, import_batch_id: int | None) -> int | None:
    """Vídeo novo baixado por uma importação vinculada entra no lote (o mesmo caminho de `create_batch` por vídeo).
    Pula: lote inexistente, removido ou cancelado; mídia que não é vídeo original; mídia com job CapCut aberto.
    Retorna o id do job criado (ou None)."""
    with capcut_service._create_lock:
        batch = repo(ctx).get_batch(capcut_batch_id)
        if batch is None or batch["hidden"] or batch["cancel_requested"]:
            return None
        media = ctx.media.get(media_id)
        # Já na fila de OUTRO lote: entra aqui também e é editado nos dois (escolha do dono em 2026-11-08); só o mesmo
        # vídeo duas vezes no MESMO lote é pulado.
        if (media is None or media["media_type"] != "video" or (media.get("role") or "original") == "cover"
                or repo(ctx).open_in_batch(capcut_batch_id, media_id)):
            return None
        options = batch["options"] or {}
        plan = options.get("edit") if batch["mode"] in EDIT_MODES else None
        with ctx.db.transaction():
            edit_batch_id = batch["edit_batch_id"]
            if plan is not None:
                if edit_batch_id is None:
                    edit_batch_id = _create_edit_batch(ctx, plan)
                    repo(ctx).update_batch(capcut_batch_id, edit_batch_id=edit_batch_id)
                else:
                    ctx.batches.reopen(edit_batch_id)   # a edição dos vídeos anteriores pode já ter terminado
            job_id = _add_video(ctx, batch_id=capcut_batch_id, media=media, mode=batch["mode"], plan=plan,
                                job_options=dict(options.get("job_options") or {}), edit_batch_id=edit_batch_id,
                                max_attempts=int(capcut_service._stored(ctx)["max_attempts"]), origin="auto",
                                import_batch_id=import_batch_id)
            if edit_batch_id is not None:
                ctx.batches.sync_found(edit_batch_id)
            repo(ctx).refresh_batch_finished(capcut_batch_id)
    logger.info("Legendas CapCut: mídia %s entrou no lote %s (importação %s)", media_id, capcut_batch_id,
                import_batch_id)
    if plan is not None:
        ctx.wake_worker()
    capcut_service._wake(ctx)
    return job_id


# ---- apresentação ---------------------------------------------------------------------------------------------------
def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _seconds(start: str | None, end: datetime) -> float | None:
    begin = _parse(start)
    return None if begin is None else max(0.0, (end - begin).total_seconds())


def batch_out(ctx: AppContext, batch: dict[str, Any]) -> dict[str, Any]:
    counts = batch["counts"]
    total = counts["total"]
    open_count = counts["pending"] + counts["running"] + counts["waiting"] + counts["held"] + counts["review"]
    per_video, _ = repo(ctx).average_seconds(batch_id=batch["id"])
    if per_video is None:
        per_video, _ = repo(ctx).average_seconds(mode=batch["mode"], limit=ESTIMATE_SAMPLES)
    now = datetime.now(UTC)
    current = repo(ctx).running_in_batch(batch["id"]) if counts["running"] else None
    eta = None
    # Na fila atrás de outros lotes: quantos vídeos vêm antes e quanto falta para começar (para não parecer travado).
    queue_ahead = 0
    if not counts["running"] and counts["pending"] + counts["waiting"] > 0 and not batch["paused"]:
        queue_ahead = repo(ctx).queue_ahead(batch["id"])
    queue_wait = None
    if queue_ahead:
        any_per_video, _ = repo(ctx).average_seconds(limit=ESTIMATE_SAMPLES)
        base = any_per_video if any_per_video is not None else per_video
        queue_wait = None if base is None else round(base * queue_ahead, 1)
    if per_video is not None:
        running_for = (_seconds(current["started_at"], now) or 0.0) if current is not None else 0.0
        eta = max(0.0, per_video * open_count - running_for) + (queue_wait or 0.0)
    finished = _parse(batch["finished_at"]) if open_count == 0 else None
    options = dict(batch["options"] or {})
    options.pop("job_options", None)   # opções completas dos jobs do lote da importação (com o modelo de legenda)
    source = options.get("source")
    return {
        **{key: batch[key] for key in ("id", "name", "mode", "status", "created_at", "output_dir")},
        "finished_at": batch["finished_at"] if open_count == 0 else None,
        "paused": bool(batch["paused"]),
        "cancel_requested": bool(batch["cancel_requested"]),
        "output_exists": bool(batch["output_dir"]) and Path(batch["output_dir"]).is_dir(),
        "counts": counts,
        "progress": max(0, min(100, int(round(batch["progress_sum"] / total)))) if total else 0,
        "per_video_seconds": None if per_video is None else round(per_video, 1),
        "eta_seconds": None if eta is None else round(eta, 1),
        "queue_ahead": queue_ahead,
        "queue_wait_seconds": queue_wait,
        "elapsed_seconds": _seconds(batch["created_at"], finished or now),
        "current_job": capcut_service.job_out(current) if current is not None else None,
        "summary": dict(options.get("summary") or {}),
        "options": options,
        "source": source if isinstance(source, dict) and source.get("kind") == "import" else None,
    }


def _batch_or_404(ctx: AppContext, batch_id: int) -> dict[str, Any]:
    batch = repo(ctx).get_batch(batch_id)
    if batch is None or batch["hidden"]:
        raise not_found("Lote de legendas", "capcut_batch_not_found")
    return batch


def get_batch(ctx: AppContext, batch_id: int) -> dict[str, Any]:
    return batch_out(ctx, _batch_or_404(ctx, batch_id))


def list_batches(ctx: AppContext, *, status: str | None, mode: str | None, q: str | None, limit: int,
                 offset: int) -> dict[str, Any]:
    items, total = repo(ctx).list_batches(status=status, mode=mode, q=(q or "").strip() or None, limit=limit,
                                          offset=offset)
    return {"items": [batch_out(ctx, item) for item in items], "total": total}


def list_batch_jobs(ctx: AppContext, batch_id: int, *, status: str | None, limit: int, offset: int) -> dict[str, Any]:
    batch = _batch_or_404(ctx, batch_id)
    items, total = repo(ctx).list_jobs(status=status, media_id=None, limit=limit, offset=offset,
                                       capcut_batch_id=batch_id)
    return {"items": [capcut_service.job_out(job) for job in items], "total": total, "counts": batch["counts"]}


def estimate(ctx: AppContext, mode: str, count: int) -> dict[str, Any]:
    """Média real por vídeo dos últimos concluídos do modo (qualquer lote); sem histórico, nulos."""
    per_video, samples = repo(ctx).average_seconds(mode=mode, limit=ESTIMATE_SAMPLES)
    if per_video is None:
        return {"per_video_seconds": None, "total_seconds": None, "samples": samples}
    return {"per_video_seconds": round(per_video, 1), "total_seconds": round(per_video * count, 1),
            "samples": samples}


# ---- ações ----------------------------------------------------------------------------------------------------------
def set_paused(ctx: AppContext, batch_id: int, paused: bool) -> dict[str, Any]:
    """Pausado: o executor não pega jobs do lote (o que está rodando termina; a edição no worker segue)."""
    _batch_or_404(ctx, batch_id)
    repo(ctx).update_batch(batch_id, paused=1 if paused else 0)
    if not paused:
        capcut_service._wake(ctx)
    return get_batch(ctx, batch_id)


def cancel_batch(ctx: AppContext, batch_id: int) -> dict[str, Any]:
    """Cancela os jobs abertos do lote (o em execução pelo cancelamento individual, com o fechamento seguro do
    núcleo) e os jobs de edição ainda abertos pelo cancelamento de lote do worker. Concluídos ficam."""
    batch = _batch_or_404(ctx, batch_id)
    counts = batch["counts"]
    open_count = counts["pending"] + counts["running"] + counts["waiting"] + counts["held"] + counts["review"]
    # Lote da importação ainda aguardando downloads: cancelar fecha a entrada de vídeos (a importação continua).
    if not open_count and batch["status"] != "importing":
        raise AppError(409, "capcut_batch_finished", "O lote não tem vídeos na fila nem em andamento.")
    repo(ctx).update_batch(batch_id, cancel_requested=1)   # o executor para de pegar jobs do lote
    repo(ctx).cancel_idle_in_batch(batch_id)
    for job in repo(ctx).batch_jobs(batch_id, ("running", "pending", "waiting")):
        capcut_service.cancel_job(ctx, job["id"])
    edit_batch_id = batch["edit_batch_id"]
    if edit_batch_id is not None:
        edit_batch = ctx.batches.get(edit_batch_id)
        if edit_batch is not None and edit_batch["status"] not in ("completed", "failed", "cancelled"):
            import_service.cancel_batch(ctx, edit_batch_id)
    repo(ctx).refresh_batch_finished(batch_id)
    logger.info("Legendas CapCut: lote %s cancelado", batch_id)
    return get_batch(ctx, batch_id)


def retry_failed(ctx: AppContext, batch_id: int) -> dict[str, Any]:
    """Jobs `failed`/`cancelled` do lote voltam para a fila (tentativas zeradas, transcrição mantida); edição com erro
    ou cancelada é refeita. Concluídos nunca são refeitos."""
    _batch_or_404(ctx, batch_id)
    jobs = repo(ctx).batch_jobs(batch_id, ("failed", "cancelled"))
    if not jobs:
        raise AppError(409, "nothing_to_retry", "O lote não tem vídeos com erro ou cancelados.")
    with ctx.db.transaction():
        repo(ctx).update_batch(batch_id, cancel_requested=0)
        for job in jobs:
            full = repo(ctx).get_job(job["id"])
            if full is not None:
                capcut_service.retry_with_edit(ctx, full)
        repo(ctx).refresh_batch_finished(batch_id)
    ctx.wake_worker()
    capcut_service._wake(ctx)
    return get_batch(ctx, batch_id)


def _still_active(ctx: AppContext, batch_id: int) -> bool:
    """Job aberto ou lote de importação ainda aguardando downloads (removê-lo faria os próximos vídeos se perderem)."""
    if repo(ctx).batch_jobs(batch_id, OPEN_STATUSES):
        return True
    return get_batch(ctx, batch_id)["status"] == "importing"


def hide_batch(ctx: AppContext, batch_id: int) -> None:
    """Remove do histórico (os arquivos ficam); 409 com job aberto ou aguardando downloads."""
    _batch_or_404(ctx, batch_id)
    if _still_active(ctx, batch_id):
        raise AppError(409, "capcut_batch_active", "O lote ainda tem vídeos na fila ou em andamento. Cancele antes.")
    repo(ctx).update_batch(batch_id, hidden=1)


def hide_batches(ctx: AppContext, batch_ids: list[int], everything: bool) -> dict[str, Any]:
    """Remove vários lotes do histórico (ou todos, com `everything`); os arquivos ficam. Lote com vídeo na fila, em
    andamento ou aguardando revisão fica e volta em `active`. Id inexistente ou já removido é ignorado."""
    if everything:
        ids = [int(r["id"]) for r in ctx.db.fetchall("SELECT id FROM capcut_batches WHERE hidden = 0 ORDER BY id")]
    else:
        ids = list(dict.fromkeys(batch_ids))
    hidden, active = 0, []
    for batch_id in ids:
        batch = repo(ctx).get_batch(batch_id)
        if batch is None or batch.get("hidden"):
            continue
        if _still_active(ctx, batch_id):
            active.append(batch_id)
            continue
        repo(ctx).update_batch(batch_id, hidden=1)
        hidden += 1
    return {"hidden": hidden, "active": active}


def hold_job(ctx: AppContext, job_id: int, held: bool) -> dict[str, Any]:
    """Pausa individual (só `pending`/`waiting`; 409 em execução ou encerrado)."""
    capcut_service.get_job(ctx, job_id)
    if not repo(ctx).hold(job_id, held):
        raise AppError(409, "capcut_job_not_holdable", "Só é possível pausar ou retomar um vídeo que está na fila.")
    if not held:
        capcut_service._wake(ctx)
    return capcut_service.get_job(ctx, job_id)


def _results(ctx: AppContext, batch_id: int) -> list[Path]:
    """MP4 dos jobs concluídos do lote que ainda existem (só dentro da pasta registrada no job)."""
    files: list[Path] = []
    for job in repo(ctx).batch_jobs(batch_id, ("completed",)):
        output_dir = (job["options"] or {}).get("output_dir")
        if not job["output_path"] or not output_dir:
            continue
        try:
            path = ensure_within(Path(job["output_path"]), [Path(output_dir)])
        except UnsafePathError:
            continue
        if path.is_file():
            files.append(path)
    return files


def build_zip(ctx: AppContext, batch_id: int) -> Path:
    """ZIP (sem compressão) dos resultados do lote num arquivo temporário em `<data_dir>/tmp` (quem serve apaga)."""
    _batch_or_404(ctx, batch_id)
    files = _results(ctx, batch_id)
    if not files:
        raise AppError(404, "batch_no_results", "O lote ainda não tem vídeos prontos.")
    folder = zip_temp_dir(ctx)
    problem = _zip_space_problem(ctx, folder, files)
    if problem is not None:
        raise AppError(507, "export_no_space", problem)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{ZIP_TEMP_PREFIX}{int(batch_id)}-{uuid.uuid4().hex[:12]}.zip"
    used: set[str] = set()
    try:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for path in files:
                name, counter = path.name, 2
                while name.casefold() in used:
                    name = f"{path.stem} ({counter}){path.suffix}"
                    counter += 1
                used.add(name.casefold())
                archive.write(path, arcname=name)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def zip_temp_dir(ctx: AppContext) -> Path:
    return ctx.settings.data_dir / "tmp"


def _zip_space_problem(ctx: AppContext, folder: Path, files: list[Path]) -> str | None:
    """Mesma conta do ZIP da exportação (`services/export_zip.py`): soma dos arquivos + margem contra o espaço livre
    menos a reserva dos downloads e o que os ZIPs da fila ainda vão escrever. None = há espaço ou não deu para medir."""
    free = export_zip.free_bytes(folder)
    if free < 0:
        return None
    total = 0
    for path in files:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    needed = export_zip.required_bytes(total)
    available = max(0, free - export_zip.reserve_bytes(ctx.settings) - export_zip.queued_bytes(ctx))
    if available >= needed:
        return None
    return (f"Sem espaço em disco para preparar o ZIP do lote: são necessários {export_zip.format_bytes(needed)} e há"
            f" {export_zip.format_bytes(available)} disponíveis. Libere espaço ou use \"Abrir pasta\".")


def sweep_zip_temps(ctx: AppContext) -> int:
    """Startup: apaga os temporários de ZIP de lote que sobraram (só os arquivos com o prefixo do lote)."""
    folder = zip_temp_dir(ctx)
    removed = 0
    try:
        entries = list(folder.glob(f"{ZIP_TEMP_PREFIX}*.zip"))
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_file() and not entry.is_symlink():
                entry.unlink()
                removed += 1
        except OSError:
            logger.warning("Legendas CapCut: temporário de ZIP não pôde ser apagado: %s", entry.name)
    return removed


def reveal_batch(ctx: AppContext, batch_id: int) -> None:
    batch = _batch_or_404(ctx, batch_id)
    folder = Path(batch["output_dir"])
    if not folder.is_dir():
        raise AppError(404, "batch_folder_not_found", "A pasta do lote ainda não existe (nenhum vídeo terminou).")
    try:
        system_open.open_folder(folder, allowed_roots=[folder])
    except SystemOpenError as exc:
        raise AppError(capcut_service._OPEN_ERROR_STATUS.get(exc.code, 500), exc.code, exc.message) from exc
