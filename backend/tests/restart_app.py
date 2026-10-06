"""App factory de teste para processos reais (uvicorn restart_app:app). Configurado por variáveis de ambiente.

RT_ROOT: raiz de dados temporária; RT_MANIFEST: JSON com os itens que o downloader falso "lista" e "baixa";
RT_PORT: porta (a guarda de Host do app usa a mesma). Processor real (FFmpeg do PATH); downloader falso.
Item do manifesto: {"code", "type": image|video, "file": caminho, "profile"?, "fail"?: true, "slow"?: true}.
`slow`: grava um parcial no temp do job e espera o arquivo `<RT_ROOT>/release` existir.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import replace
from pathlib import Path

from app.core.config import Settings
from app.integrations.instagram.downloader import InstagramError, ListingRef
from app.main import create_app
from media_fakes import FakeInstagramDownloader, MemorySessionProvider, remote

ROOT = Path(os.environ["RT_ROOT"])
MANIFEST = Path(os.environ["RT_MANIFEST"])
PORT = int(os.environ.get("RT_PORT", "8792"))


_PAGE = ListingRef(page_index=1, cursor=None, page_size=1000)


def _entries() -> list[dict]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


class ManifestDownloader(FakeInstagramDownloader):
    def iter_profile_media(self, username, content, limit, *, cancel=None, **kwargs):
        for entry in _entries():
            if entry.get("profile", "perfil_teste") != username:
                continue
            if cancel is not None and cancel.is_set():
                return
            yield replace(remote(entry["code"], 0, entry["type"], entry.get("profile", "perfil_teste")),
                          listing_ref=_PAGE)

    def refresh_page(self, profile, ref, *, cancel=None):
        # Lista de página única: job repetido (URL limpa ao terminar) renova o link por aqui.
        return [replace(remote(e["code"], 0, e["type"], profile), listing_ref=ref)
                for e in _entries() if e.get("profile", "perfil_teste") == profile]

    def download(self, item, dest_dir, *, cancel=None, progress=None):
        entry = next(e for e in _entries() if e["code"] == item.source_post_id)
        if entry.get("fail"):
            raise InstagramError("content_unavailable", "Conteúdo indisponível (teste).", retryable=False)
        dest_dir.mkdir(parents=True, exist_ok=True)
        if entry.get("slow"):
            (dest_dir / "download.part").write_bytes(b"parcial" * 1000)
            while not (ROOT / "release").exists():
                if cancel is not None and cancel.is_set():
                    raise InstagramError("download_failed", "cancelado", retryable=False)
                time.sleep(0.05)
        src = Path(entry["file"])
        target = dest_dir / f"download{src.suffix}"
        shutil.copyfile(src, target)
        (dest_dir / "download.part").unlink(missing_ok=True)
        return target


settings = Settings(
    root=ROOT,
    data_dir=ROOT / "data",
    storage_dir=ROOT / "storage",
    log_dir=ROOT / "logs",
    secrets_dir=ROOT / "data" / "secrets",
    frontend_dist=ROOT / "frontend" / "dist",
    port=PORT,
    env={},
    retry_delays=(1.0, 2.0, 3.0),
    disable_worker=bool(os.environ.get("RT_NO_WORKER")),
    poll_interval=0.1,
    heartbeat_interval=0.5,
    instagram_request_interval=(0.0, 0.0),  # portão sem espera nos testes
)
app = create_app(
    settings,
    session_provider=MemorySessionProvider(),
    downloader_factory=lambda: ManifestDownloader(),
)
