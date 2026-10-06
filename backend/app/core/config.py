"""Configuração do backend (contrato, seção 2).

Lê variáveis de ambiente e, opcionalmente, um arquivo `.env` na raiz do projeto. O `.env` é mesclado em um
mapeamento próprio (`Settings.env`) e nunca é copiado para `os.environ`; valores lidos nunca são logados.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
# Pool de processamento FFmpeg (CPU): configuração `concurrency` (docs/BATCH_ROBUSTNESS.md, seção 9).
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 2
# Pool de download (I/O): configuração `download_concurrency`.
DEFAULT_DOWNLOAD_CONCURRENCY = 2
MAX_DOWNLOAD_CONCURRENCY = 4
# Reserva fixa de disco livre exigida antes de cada download (soma-se a 2 x max_download_bytes x downloads).
DEFAULT_DISK_RESERVE_BYTES = 5 * 1024 * 1024 * 1024
DATABASE_FILENAME = "media-tool.db"
# Segredos que nunca ficam em os.environ (subprocessos como ffmpeg/explorer herdariam o valor).
PRIVATE_ENV_KEYS = ("INSTAGRAM_SESSIONID",)
# Estratégia do provider do Instagram (setting `instagram_provider`; env MEDIA_TOOL_INSTAGRAM_PROVIDER).
INSTAGRAM_PROVIDERS = ("auto", "web", "gallery_dl")


def read_dotenv(path: Path) -> dict[str, str]:
    """Parser mínimo de `.env`: linhas `CHAVE=valor`, `export` opcional, aspas simples/duplas e comentários."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return values
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            comment = value.find(" #")
            if comment != -1:
                value = value[:comment].rstrip()
        values[key] = value
    return values


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        result = int((value or "").strip())
    except ValueError:
        return default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _choice(value: str | None, allowed: tuple[str, ...], default: str) -> str:
    value = (value or "").strip().lower()
    return value if value in allowed else default


def _path(value: str | None, default: Path, root: Path) -> Path:
    if value and value.strip():
        candidate = Path(value.strip())
        return (candidate if candidate.is_absolute() else root / candidate).resolve()
    return default.resolve()


@dataclass(frozen=True)
class Settings:
    root: Path
    data_dir: Path
    storage_dir: Path
    log_dir: Path
    secrets_dir: Path
    frontend_dist: Path
    host: str = "127.0.0.1"
    port: int = 8765
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    concurrency: int = 1  # processamento FFmpeg
    download_concurrency: int = DEFAULT_DOWNLOAD_CONCURRENCY
    disk_reserve_bytes: int = DEFAULT_DISK_RESERVE_BYTES
    max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES
    # Backpressure de processamento (seção 9): lote com processamento para de reivindicar NOVOS downloads (e de
    # listar) com `max_process_backlog` ingests baixando/baixados/processando e retoma abaixo de
    # `resume_process_backlog`; o teto global soma todos os lotes com processamento.
    max_process_backlog: int = 50
    resume_process_backlog: int = 20
    max_process_backlog_global: int = 150
    resume_process_backlog_global: int = 60
    disable_worker: bool = False
    dev_mode: bool = False  # MEDIA_TOOL_DEV=1: libera as origens do Vite (porta 5173)
    # Ambiente efetivo (os.environ + .env). Pode conter segredos: nunca aparece em repr.
    env: Mapping[str, str] = field(default_factory=dict, repr=False, compare=False)
    # Ajustes internos da fila (testes reduzem para acelerar).
    retry_delays: tuple[float, ...] = (30.0, 120.0, 600.0)
    rate_limit_pause_seconds: float = 600.0
    # Campos explícitos de restrição (action_blocked): pausa protetora do Media Tool, 1 hora.
    action_blocked_pause_seconds: float = 3600.0
    poll_interval: float = 1.0
    heartbeat_interval: float = 5.0
    stale_job_seconds: float = 600.0  # job com lease e sem heartbeat por mais que isso é considerado travado
    maintenance_interval: float = 30.0
    # Seção 8: intervalo mínimo (s, sorteado no intervalo) entre requisições de listagem/renovação ao Instagram.
    instagram_request_interval: tuple[float, float] = (2.0, 4.0)
    # Prazos duros das chamadas síncronas ao Instagram feitas pela API.
    session_test_timeout: float = 40.0
    profile_preview_timeout: float = 50.0
    # Padrão da estratégia do provider do Instagram; o setting `instagram_provider` gravado tem precedência.
    # "web" (sem fallback automático): os dois caminhos de listagem do gallery-dl já foram recusados para a conta.
    instagram_provider: str = "web"

    @property
    def database_path(self) -> Path:
        return self.data_dir / DATABASE_FILENAME

    @property
    def originals_dir(self) -> Path:
        return self.storage_dir / "originals"

    @property
    def processed_dir(self) -> Path:
        return self.storage_dir / "processed"

    @property
    def temp_dir(self) -> Path:
        return self.storage_dir / "temp"

    @property
    def thumbs_dir(self) -> Path:
        return self.storage_dir / "thumbs"


def load_settings(environ: Mapping[str, str] | None = None, *, root: Path | None = None) -> Settings:
    """Monta `Settings` a partir do ambiente. Precedência: ambiente do processo > `.env` > defaults.

    Lendo do ambiente real, variáveis secretas (INSTAGRAM_SESSIONID) são movidas para o mapeamento privado
    `Settings.env` e removidas de `os.environ`.
    """
    base_env = dict(os.environ if environ is None else environ)
    if environ is None:
        for key in PRIVATE_ENV_KEYS:
            os.environ.pop(key, None)
    root_path = Path(root or base_env.get("MEDIA_TOOL_ROOT") or DEFAULT_ROOT).resolve()
    env: dict[str, str] = {**read_dotenv(root_path / ".env"), **base_env}

    data_dir = _path(env.get("MEDIA_TOOL_DATA_DIR"), root_path / "data", root_path)
    return Settings(
        root=root_path,
        data_dir=data_dir,
        storage_dir=_path(env.get("MEDIA_TOOL_STORAGE_DIR"), root_path / "storage", root_path),
        log_dir=_path(env.get("MEDIA_TOOL_LOG_DIR"), root_path / "logs", root_path),
        secrets_dir=_path(env.get("MEDIA_TOOL_SECRETS_DIR"), data_dir / "secrets", root_path),
        frontend_dist=(root_path / "frontend" / "dist").resolve(),
        host=(env.get("MEDIA_TOOL_HOST") or "127.0.0.1").strip(),
        port=_int(env.get("MEDIA_TOOL_PORT"), 8765, minimum=1, maximum=65535),
        ffmpeg_path=(env.get("MEDIA_TOOL_FFMPEG") or "").strip() or None,
        ffprobe_path=(env.get("MEDIA_TOOL_FFPROBE") or "").strip() or None,
        concurrency=_int(env.get("MEDIA_TOOL_CONCURRENCY"), 1, minimum=MIN_CONCURRENCY, maximum=MAX_CONCURRENCY),
        download_concurrency=_int(env.get("MEDIA_TOOL_DOWNLOAD_CONCURRENCY"), DEFAULT_DOWNLOAD_CONCURRENCY,
                                  minimum=MIN_CONCURRENCY, maximum=MAX_DOWNLOAD_CONCURRENCY),
        disk_reserve_bytes=_int(env.get("MEDIA_TOOL_DISK_RESERVE_BYTES"), DEFAULT_DISK_RESERVE_BYTES, minimum=0),
        max_download_bytes=_int(env.get("MEDIA_TOOL_MAX_DOWNLOAD_BYTES"), DEFAULT_MAX_DOWNLOAD_BYTES, minimum=1),
        max_process_backlog=_int(env.get("MEDIA_TOOL_MAX_PROCESS_BACKLOG"), 50, minimum=1),
        resume_process_backlog=_int(env.get("MEDIA_TOOL_RESUME_PROCESS_BACKLOG"), 20, minimum=0),
        max_process_backlog_global=_int(env.get("MEDIA_TOOL_MAX_PROCESS_BACKLOG_GLOBAL"), 150, minimum=1),
        resume_process_backlog_global=_int(env.get("MEDIA_TOOL_RESUME_PROCESS_BACKLOG_GLOBAL"), 60, minimum=0),
        disable_worker=_bool(env.get("MEDIA_TOOL_DISABLE_WORKER")),
        dev_mode=_bool(env.get("MEDIA_TOOL_DEV")),
        instagram_provider=_choice(env.get("MEDIA_TOOL_INSTAGRAM_PROVIDER"), INSTAGRAM_PROVIDERS, "web"),
        env=env,
    )
