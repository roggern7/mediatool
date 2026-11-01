"""App de teste da Análise de Perfis como processo real, com fetcher falso: nada aqui acessa a rede.

No teste: `uvicorn profile_restart_app:app` (ver test_profile_restart_real.py). Na tela, dentro de `backend`:

    set PYTHONPATH=..\\processor;.;tests
    set PROFILE_FAKE_DELAY=0.5
    python tests\\profile_restart_app.py

e, em outro terminal, `set MEDIA_TOOL_PORT=8793` seguido de `npm run dev` em `frontend` (origens do Vite liberadas).

RT_ROOT: raiz de dados (sem ela, uma pasta temporária nova, mostrada ao subir); nunca o banco real. RT_PORT: porta
(8793). PROFILE_FAKE_DELAY: segundos por perfil (0). PROFILE_ZERO_INTERVALS: "0" mantém o ritmo real do executor;
qualquer outro valor zera intervalos e descansos. PROFILE_CALL_LOG: arquivo com uma linha por chamada ao fetcher,
gravada antes da resposta. A resposta é uma mistura determinística de status, derivada do nome do perfil.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import zlib
from pathlib import Path

from app.core.config import Settings
from app.integrations.instagram.web.profile_snapshot import ProfileSnapshot
from app.jobs import profile_analysis_runner
from app.main import create_app
from media_fakes import FakeInstagramDownloader, MemorySessionProvider

ROOT = Path(os.environ.get("RT_ROOT") or tempfile.mkdtemp(prefix="media-tool-perfis-"))
PORT = int(os.environ.get("RT_PORT", "8793"))
DELAY = float(os.environ.get("PROFILE_FAKE_DELAY", "0"))
CANARY = profile_analysis_runner.CANARY_PROFILE
_CALL_LOG = os.environ.get("PROFILE_CALL_LOG")
_calls = open(_CALL_LOG, "a", encoding="utf-8", buffering=1) if _CALL_LOG else None


def fake_snapshot(username: str) -> ProfileSnapshot:
    """72% ativa, 15% privada, 10% indisponível, 3% não verificada: poucas o bastante para não abrir o disjuntor, que
    esperaria 15 minutos de verdade."""
    code = zlib.crc32(username.encode("utf-8"))
    bucket = code % 100
    if username == CANARY or bucket < 72:
        reels = code % 7
        views = {"views_state": "known", "views_total": code % 90_000, "views_reels": reels} if reels else {
            "views_state": "none"}
        posts = {"posts_exact": 1} if code % 5 else {"posts_exact": 0, "posts_text": "33K"}
        return ProfileSnapshot(username, "active", display_name=f"Nome {username}", posts_state="known",
                               posts_count=33_000 if "posts_text" in posts else code % 5_000, **posts, **views,
                               stories="yes" if code % 2 else "no", highlights="yes" if code % 3 else "no")
    if bucket < 87:
        return ProfileSnapshot(username, "private", display_name=f"Nome {username}", posts_state="known",
                               posts_count=code % 500, posts_exact=1, views_state="unverified", stories="unverified",
                               highlights="unverified")
    if bucket < 97:
        return ProfileSnapshot(username, "unavailable", posts_state="unverified", views_state="unverified",
                               stories="unverified", highlights="unverified", error_code="profile_unavailable",
                               error_message="Perfil não encontrado, removido ou restrito.")
    return ProfileSnapshot(username, "unverified", posts_state="unverified", views_state="unverified",
                           stories="unverified", highlights="unverified", error_code="profile_unreadable",
                           error_message="Não foi possível confirmar este perfil.")


def fake_fetcher(username: str, _cancel: threading.Event | None) -> ProfileSnapshot:
    if _calls is not None:
        _calls.write(f"{username}\n")
    if DELAY > 0:
        time.sleep(DELAY)
    return fake_snapshot(username)


if os.environ.get("PROFILE_ZERO_INTERVALS") != "0":
    profile_analysis_runner.PROFILE_DELAY = (0.0, 0.0)
    profile_analysis_runner.SLOW_PROFILE_DELAY = (0.0, 0.0)
    profile_analysis_runner.REST_DELAY = (0.0, 0.0)
    profile_analysis_runner.FAST_REST_DELAY = (0.0, 0.0)

settings = Settings(
    root=ROOT,
    data_dir=ROOT / "data",
    storage_dir=ROOT / "storage",
    log_dir=ROOT / "logs",
    secrets_dir=ROOT / "data" / "secrets",
    frontend_dist=ROOT / "frontend" / "dist",
    port=PORT,
    env={},
    dev_mode=True,
    poll_interval=0.1,
    heartbeat_interval=0.5,
    instagram_request_interval=(0.0, 0.0),
)
app = create_app(settings, session_provider=MemorySessionProvider(), downloader_factory=FakeInstagramDownloader,
                 profile_fetcher=fake_fetcher)

if __name__ == "__main__":
    import uvicorn

    print(f"Análise de Perfis com fetcher falso em http://127.0.0.1:{PORT} (dados em {ROOT})")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
