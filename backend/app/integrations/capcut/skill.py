"""Carrega o capcut-agent-skill (cópia sem modificação em third_party/capcut-agent-skill/).

Reaproveitado: `create_native_text_material` (material de texto nativo: branco com contorno preto, check_flag 15,
usado como estilo padrão) e `parse_time_us`. O motor de rascunho do skill (draft_info.json, layout macOS) não
serve para o CapCut 9.x do Windows; o motor próprio está em `draft.py`.
"""

from __future__ import annotations

import importlib.util
import re
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

from app.integrations.capcut.errors import CapCutError

REPO_ROOT = Path(__file__).resolve().parents[4]
SKILL_DIR = REPO_ROOT / "third_party" / "capcut-agent-skill"
SCRIPTS_DIR = SKILL_DIR / ".agents" / "skills" / "capcut-editor" / "scripts"

# Invariantes documentadas pelo skill (references/draft_schema.md §8), aplicadas pelo motor próprio.
MIN_GAP_S = 0.020          # fim[i] <= início[i+1] - 20 ms
PAUSE_CLEAR_S = 0.180      # pausa >= 180 ms limpa a legenda
TAIL_S = 0.050             # fim do bloco = fim da última palavra + 50 ms
DEFAULT_SCALE = 1.919363623500053
DEFAULT_Y = -0.70

_lock = threading.Lock()
_modules: dict[str, ModuleType] = {}


def _load(file_name: str) -> ModuleType:
    with _lock:
        if file_name in _modules:
            return _modules[file_name]
        path = SCRIPTS_DIR / file_name
        if not path.is_file():
            raise CapCutError("deps_missing", "capcut-agent-skill não encontrado em third_party/.")
        spec = importlib.util.spec_from_file_location(f"_mt_capcut_skill_{path.stem}", path)
        if spec is None or spec.loader is None:  # pragma: no cover - defensivo
            raise CapCutError("deps_missing", "capcut-agent-skill não pôde ser carregado.")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _modules[file_name] = module
        return module


def native_text_material(text_id: str, text: str) -> dict[str, Any]:
    """Material de texto nativo do skill (estilo padrão)."""
    return _load("update_scribe_subtitles.py").create_native_text_material(text_id, text)


def parse_time_us(value: Any) -> int:
    return _load("capcut_draft.py").parse_time_us(value)


def info() -> dict[str, Any]:
    """{installed, path, commit} — só leitura."""
    installed = (SCRIPTS_DIR / "update_scribe_subtitles.py").is_file() and (SCRIPTS_DIR / "capcut_draft.py").is_file()
    commit = None
    upstream = SKILL_DIR / "UPSTREAM.md"
    if upstream.is_file():
        match = re.search(r"\b([0-9a-f]{40})\b", upstream.read_text(encoding="utf-8", errors="replace"))
        commit = match.group(1) if match else None
    return {"installed": installed, "path": str(SKILL_DIR), "commit": commit}
