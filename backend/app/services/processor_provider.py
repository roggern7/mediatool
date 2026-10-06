"""Entrega o `MediaProcessor` com os caminhos efetivos de FFmpeg/ffprobe (configurações > ambiente > PATH).

As informações de ferramenta (`-version`) ficam em cache por caminho configurado, para que GETs de
configurações/diagnóstico não executem o FFmpeg a cada requisição.
"""

from __future__ import annotations

import threading
import time

from media_processor import MediaProcessor, ToolInfo

from app.core.config import Settings
from app.db.repositories.settings import SettingsRepository

TOOL_INFO_TTL = 60.0
# Caminhos configurados relidos do banco no máximo a cada PATHS_TTL s (antes: várias leituras por job);
# salvar ffmpeg_path/ffprobe_path chama invalidate() e vale na hora.
PATHS_TTL = 5.0


class ProcessorProvider:
    def __init__(self, settings: Settings, settings_repo: SettingsRepository, injected: MediaProcessor | None = None):
        self._settings = settings
        self._repo = settings_repo
        self._injected = injected
        self._lock = threading.Lock()
        self._cache: tuple[tuple[str | None, str | None], MediaProcessor] | None = None
        self._info: dict[tuple[str, str | None], tuple[float, ToolInfo]] = {}
        self._paths: tuple[float, tuple[str | None, str | None]] | None = None

    def configured_paths(self) -> tuple[str | None, str | None]:
        now = time.monotonic()
        with self._lock:
            if self._paths is not None and now - self._paths[0] < PATHS_TTL:
                return self._paths[1]
        stored = self._repo.all()
        paths = (stored.get("ffmpeg_path") or self._settings.ffmpeg_path,
                 stored.get("ffprobe_path") or self._settings.ffprobe_path)
        with self._lock:
            self._paths = (now, paths)
        return paths

    def get(self) -> MediaProcessor:
        if self._injected is not None:
            return self._injected
        key = self.configured_paths()
        with self._lock:
            if self._cache is None or self._cache[0] != key:
                self._cache = (key, MediaProcessor(ffmpeg_path=key[0], ffprobe_path=key[1]))
            return self._cache[1]

    def _tool_info(self, tool: str) -> ToolInfo:
        paths = self.configured_paths()
        key = (tool, paths[0] if tool == "ffmpeg" else paths[1])
        now = time.monotonic()
        with self._lock:
            cached = self._info.get(key)
            if cached is not None and now - cached[0] < TOOL_INFO_TTL:
                return cached[1]
        processor = self.get()
        info = processor.ffmpeg_info() if tool == "ffmpeg" else processor.ffprobe_info()
        with self._lock:
            self._info = {k: v for k, v in self._info.items() if k[0] != tool}
            self._info[key] = (now, info)
        return info

    def ffmpeg_info(self) -> ToolInfo:
        return self._tool_info("ffmpeg")

    def ffprobe_info(self) -> ToolInfo:
        return self._tool_info("ffprobe")

    def invalidate(self) -> None:
        with self._lock:
            self._info.clear()
            self._paths = None
