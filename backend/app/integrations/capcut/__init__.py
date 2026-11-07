"""Núcleo "Legendas CapCut": interface pública usada pelo runner/API (contrato em docs/CAPCUT.md).

Importar este pacote não importa faster-whisper, uiautomation nem pywin32 (só dentro das funções que os usam).
"""

from __future__ import annotations

import importlib.util
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.integrations.capcut.errors import (
    AccessibilityUnavailable,
    CapCutBusy,
    CapCutCancelled,
    CapCutError,
    CapCutNotInstalled,
    DesktopBusy,
    ExportFailed,
    ForegroundLost,
    UiTimeout,
)
from app.integrations.capcut.jobspec import STAGES, CapCutJobSpec, CapCutResult

__all__ = [
    "STAGES", "CapCutError", "CapCutBusy", "CapCutCancelled", "CapCutNotInstalled", "AccessibilityUnavailable", "DesktopBusy",
    "ForegroundLost", "UiTimeout", "ExportFailed", "CapCutJobSpec", "CapCutResult", "process_video", "status",
    "list_caption_drafts", "extract_style", "default_style", "style_summary", "discard_job_work",
]


def process_video(spec: CapCutJobSpec, *, cancel: threading.Event,
                  on_stage: Callable[[str, float | None, str | None], None],
                  on_transcript: Callable[[list[dict[str, Any]]], None] | None = None) -> CapCutResult:
    """Processa um vídeo de ponta a ponta (bloqueante; o runner chama numa thread própria)."""
    from app.integrations.capcut.pipeline import process_video as run

    return run(spec, cancel=cancel, on_stage=on_stage, on_transcript=on_transcript)


def discard_job_work(work_dir: Path) -> None:
    """Job excluído do histórico: apaga os intermediários anotados (só na pasta de vídeos do CapCut, com o nome MT_
    do job) e a pasta de trabalho."""
    import shutil

    from app.integrations.capcut import install as install_mod
    from app.integrations.capcut.finalize import discard_recorded_exports

    inst = install_mod.detect()
    discard_recorded_exports(Path(work_dir), inst.default_videos_dir if inst else None)
    shutil.rmtree(work_dir, ignore_errors=True)


def _has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def status(data_dir: Path) -> dict[str, Any]:
    """Situação do CapCut, das dependências, do skill e dos modelos (só leitura, sem abrir o CapCut)."""
    from app.integrations.capcut import install as install_mod
    from app.integrations.capcut import skill
    from app.integrations.capcut.transcribe import MODELS, model_downloaded

    inst = install_mod.detect()
    models_dir = Path(data_dir) / "models" / "whisper"
    try:
        running_pids = install_mod.is_running() if inst else []
    except CapCutError:  # só exibição; o executor trata a falha da consulta como erro transitório
        running_pids = []
    return {
        "installed": inst is not None,
        "version": inst.version if inst else None,
        "drafts_dir": str(inst.drafts_dir) if inst else None,
        "running_pids": running_pids,
        "deps": {"faster_whisper": _has("faster_whisper"), "uiautomation": _has("uiautomation"),
                 "pywin32": _has("win32gui") and _has("win32api")},
        "skill": skill.info(),
        "models": {size: model_downloaded(models_dir, size) for size in MODELS},
    }


def _install() -> Any:
    from app.integrations.capcut import install as install_mod

    inst = install_mod.detect()
    if inst is None:
        raise CapCutNotInstalled()
    return inst


def list_caption_drafts() -> list[dict[str, Any]]:
    """Rascunhos do CapCut com texto (sem os criados pelo Media Tool, prefixo MT_)."""
    from app.integrations.capcut import style as style_mod

    return [{"name": d.name, "modified_at": d.modified_at.isoformat(), "text_segments": d.text_segments,
             "preview": d.preview} for d in style_mod.list_caption_drafts(_install())]


def extract_style(draft_name: str, segment_index: int = 0) -> dict[str, Any]:
    from app.integrations.capcut import style as style_mod

    return style_mod.extract_style(_install(), draft_name, segment_index).to_dict()


def default_style() -> dict[str, Any]:
    from app.integrations.capcut import install as install_mod
    from app.integrations.capcut import style as style_mod

    return style_mod.default_style(install_mod.detect()).to_dict()


def style_summary(style: dict[str, Any]) -> dict[str, Any]:
    from app.integrations.capcut import style as style_mod

    return style_mod.style_summary(style)
