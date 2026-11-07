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
    IsolatedUnavailable,
    UiTimeout,
)
from app.integrations.capcut.jobspec import STAGES, CapCutJobSpec, CapCutResult

__all__ = [
    "STAGES", "CapCutError", "CapCutBusy", "CapCutCancelled", "CapCutNotInstalled", "AccessibilityUnavailable", "DesktopBusy",
    "ForegroundLost", "IsolatedUnavailable", "UiTimeout", "ExportFailed", "CapCutJobSpec", "CapCutResult", "process_video", "status",
    "list_caption_drafts", "extract_style", "default_style", "style_summary", "discard_job_work", "catalog_fonts",
    "catalog_animations", "font_file", "default_design", "compile_design", "design_warnings", "EXPORT_MODES",
    "transcribe_only",
]

EXPORT_MODES = ("isolated", "interactive")  # igual a isolated.EXPORT_MODES (este pacote não importa o pywin32)


def process_video(spec: CapCutJobSpec, *, cancel: threading.Event,
                  on_stage: Callable[[str, float | None, str | None], None],
                  on_transcript: Callable[[list[dict[str, Any]]], None] | None = None) -> CapCutResult:
    """Processa um vídeo de ponta a ponta (bloqueante; o runner chama numa thread própria)."""
    from app.integrations.capcut.pipeline import process_video as run

    return run(spec, cancel=cancel, on_stage=on_stage, on_transcript=on_transcript)


def transcribe_only(spec: CapCutJobSpec, *, cancel: threading.Event,
                    on_stage: Callable[[str, float | None, str | None], None]) -> list[dict[str, Any]]:
    """Só a transcrição do vídeo do job (sem CapCut), para a revisão das legendas. Palavras em dicionário."""
    from app.integrations.capcut.pipeline import transcribe_only as run

    return run(spec, cancel=cancel, on_stage=on_stage)


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
    from app.integrations.capcut import isolated, skill
    from app.integrations.capcut.transcribe import MODELS, aligner_available, model_downloaded

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
        "isolated": isolated.availability(),
        "models": {size: model_downloaded(models_dir, size) for size in MODELS},
        "aligner": aligner_available(models_dir),
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


# --------------------------------------------------------------------------------------------- editor visual

def _catalog_paths() -> Any:
    from app.integrations.capcut import catalog
    from app.integrations.capcut import install as install_mod

    return catalog.paths_for(install_mod.detect())


def _catalog_cache(data_dir: Path) -> Path:
    return Path(data_dir) / "capcut" / "catalog_cache.json"


def catalog_fonts(data_dir: Path, *, q: str | None = None, source: str | None = None, include_pro: bool = False,
                  limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Fontes embutidas do CapCut, do Windows e do catálogo local do CapCut: `{items, total}`."""
    from app.integrations.capcut import catalog

    items, total = catalog.list_fonts(_catalog_paths(), q=q, source=source, include_pro=include_pro, limit=limit,
                                      offset=offset, cache_file=_catalog_cache(data_dir))
    return {"items": items, "total": total}


def catalog_animations(data_dir: Path, *, type: str | None = None, q: str | None = None,  # noqa: A002
                       include_pro: bool = False, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Animações de texto classificadas (entrada/saída/loop) do catálogo local: `{items, total}`."""
    from app.integrations.capcut import catalog

    items, total = catalog.list_animations(_catalog_paths(), type=type, q=q, include_pro=include_pro, limit=limit,
                                           offset=offset, cache_file=_catalog_cache(data_dir))
    return {"items": items, "total": total}


def font_file(key: str) -> Path | None:
    """Arquivo local da fonte (só das pastas permitidas) ou None."""
    from app.integrations.capcut import catalog

    return catalog.font_file(_catalog_paths(), key)


def default_design() -> dict[str, Any]:
    from app.integrations.capcut import design
    from app.integrations.capcut import install as install_mod

    return design.default_design(install_mod.detect())


def compile_design(data_dir: Path, design_data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(design normalizado, estilo serializado) de um design já validado no formato; CapCutError `invalid_design`
    se a fonte ou uma animação não for permitida."""
    from app.integrations.capcut import design

    normalized = design.validate_design(design_data, _catalog_paths(), _catalog_cache(data_dir))
    return normalized, design.style_from_design(normalized).to_dict()


def design_warnings(design_data: dict[str, Any]) -> list[str]:
    """Avisos (não bloqueiam) de combinações do design que o CapCut não renderiza como o editor mostra."""
    from app.integrations.capcut import design

    return design.design_warnings(design_data)
