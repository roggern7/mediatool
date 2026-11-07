"""Fakes das Legendas CapCut (integração): núcleo falso, processador falso e mídias no storage. Nenhum toca o CapCut
real, o whisper ou o pacote `app.integrations.capcut`."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any


class FakeCapCutError(Exception):
    def __init__(self, code: str, message: str = "Falha falsa.", transient: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.transient = transient


def fake_core(**overrides: Any) -> SimpleNamespace:
    def status(data_dir: Path) -> dict[str, Any]:
        return {"installed": True, "version": "8.0.0", "drafts_dir": "C:/drafts", "running_pids": [],
                "deps": {"faster_whisper": True, "uiautomation": True, "pywin32": True},
                "skill": {"installed": True, "path": "third_party/x", "commit": "abc"},
                "models": {"small": False, "medium": False, "large-v3-turbo": True}}

    def extract_style(draft_name: str, segment_index: int = 0) -> dict[str, Any]:
        if draft_name == "sumiu":
            raise FakeCapCutError("draft_not_found", "Projeto não encontrado no CapCut.")
        return {"font": "Montserrat", "color": "#ffffff", "from": draft_name, "index": segment_index}

    def style_summary(style: dict[str, Any]) -> dict[str, Any]:
        return {"font": style.get("font"), "color": style.get("color", "#ffffff"), "highlight_color": None,
                "has_animation": False, "has_effect": False, "caption_template": None, "position_y": -0.7,
                "scale": 1.0}

    core = SimpleNamespace(
        status=status,
        list_caption_drafts=lambda: [{"name": "Meu modelo", "modified_at": "2026-11-01T10:00:00Z",
                                      "text_segments": 2, "preview": "Olá"}],
        extract_style=extract_style,
        default_style=lambda: {"font": "Padrão", "color": "#ffffff"},
        style_summary=style_summary,
    )
    for key, value in overrides.items():
        setattr(core, key, value)
    return core


def add_media(harness, *, media_type: str = "video", role: str = "original", name: str = "video1") -> int:
    """Mídia com original real no storage (bytes sintéticos)."""
    data = f"FAKE-{media_type}-{name}".encode()
    relative = f"originals/test/{name}.{'mp4' if media_type == 'video' else 'jpg'}"
    path = harness.ctx.storage.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return harness.ctx.media.insert({
        "source_platform": "local", "media_type": media_type, "original_filename": path.name,
        "original_path": relative, "original_sha256": hashlib.sha256(data).hexdigest(), "file_size": len(data),
        "role": role,
    })


@dataclass
class FakeCapCutProcessor:
    """`process_video` falso: chama as etapas, devolve a transcrição e escreve o arquivo final na pasta de saída."""

    errors: list[Exception] = field(default_factory=list)   # levantados em ordem, um por chamada
    specs: list[Any] = field(default_factory=list)
    during: Any = None                                       # chamado no meio (spec, cancel)
    words: list[dict[str, Any]] = field(default_factory=lambda: [{"start": 0.0, "end": 0.5, "text": "olá",
                                                                  "probability": 0.9}])

    transcribed: list[Any] = field(default_factory=list)      # specs da transcrição avulsa (revisão das legendas)
    transcribe_errors: list[Exception] = field(default_factory=list)

    def transcribe_only(self, spec, *, cancel, on_stage):
        """`transcribe_only` falso do núcleo: só a transcrição (sem CapCut), com as palavras de `words`."""
        self.transcribed.append(spec)
        on_stage("transcribing", 0.5, "Transcrevendo")
        if self.transcribe_errors:
            raise self.transcribe_errors.pop(0)
        return [dict(w) for w in self.words]

    def __call__(self, spec, *, cancel, on_stage, on_transcript=None):
        self.specs.append(spec)
        on_stage("transcribing", 0.5, "Transcrevendo")
        if spec.transcript is None and on_transcript is not None:
            on_transcript(self.words)
        on_stage("drafting", None, None)
        if self.during is not None:
            self.during(spec, cancel)
        if self.errors:
            raise self.errors.pop(0)
        on_stage("exporting", 0.5, "Exportando")
        spec.output_dir.mkdir(parents=True, exist_ok=True)
        output = spec.output_dir / f"{spec.output_name}.mp4"
        output.write_bytes(b"FAKE-video-legendado")
        on_stage("cleaning", 1.0, None)
        return SimpleNamespace(output_path=output, draft_name=spec.draft_name, draft_kept=False,
                               words=list(spec.transcript or self.words), chunks=1,
                               probe={"width": 64, "height": 48, "duration": 2.0}, warnings=["aviso falso"])
