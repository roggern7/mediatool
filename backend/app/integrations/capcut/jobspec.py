"""Tipos públicos do contrato com o runner (CapCutJobSpec, CapCutResult, STAGES)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STAGES = ("transcribing", "drafting", "waiting_capcut", "launching", "opening", "exporting", "finalizing", "cleaning")


@dataclass
class CapCutJobSpec:
    job_id: int
    video_path: Path                 # original absoluto (storage.resolve)
    draft_name: str                  # 'MT_<job_id>_m<media_id>' (o runner gera; único)
    output_dir: Path                 # pasta final
    output_name: str                 # nome base sem extensão (o núcleo sanitiza e evita sobrescrever)
    work_dir: Path                   # <data_dir>/capcut/work/<job_id> (temporários do job)
    models_dir: Path                 # <data_dir>/models/whisper
    style: dict[str, Any] | None = None
    highlight: bool = True
    highlight_color: str | None = None
    max_words: int = 3
    max_chars: int = 24
    whisper_model: str = "large-v3-turbo"
    language: str = "pt"
    resolution: str = "original"
    bitrate: str = "recommended"
    fps: str = "60"
    keep_original_audio: bool = True
    keep_draft: bool = False
    transcript: list[dict[str, Any]] | None = None


@dataclass
class CapCutResult:
    output_path: Path
    draft_name: str
    draft_kept: bool
    words: list[dict[str, Any]]
    chunks: int
    probe: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
