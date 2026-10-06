"""Prova de conceito do adapter Instagram (gallery-dl + sessão local).

Uso: ``.venv\\Scripts\\python.exe backend\\tools\\poc_instagram.py --profile instagram --content reels``

Passos: obtém a sessão via LocalInstagramSessionProvider, testa a sessão, lê o perfil, lista no máximo
1 mídia, baixa SÓ esse arquivo para storage\\temp\\poc-<timestamp> e valida (tamanho, magic bytes, ffprobe).
Imprime um relatório JSON sem segredos. Nunca imprime cookies; nunca apaga arquivos do usuário.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.integrations.instagram.downloader import CONTENT_TYPES, InstagramError  # noqa: E402
from app.integrations.instagram.gallerydl import GalleryDlInstagramDownloader  # noqa: E402
from app.integrations.instagram.session import LocalInstagramSessionProvider  # noqa: E402

MAGIC = (
    (4, b"ftyp", "mp4/mov/heic"),
    (0, b"\xff\xd8\xff", "jpeg"),
    (0, b"\x89PNG\r\n\x1a\n", "png"),
    (0, b"\x1a\x45\xdf\xa3", "webm"),
)


def detect_magic(path: Path) -> str | None:
    with path.open("rb") as fh:
        head = fh.read(16)
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    for offset, signature, name in MAGIC:
        if head[offset:offset + len(signature)] == signature:
            return name
    return None


def run_ffprobe(path: Path) -> dict | None:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    args = [ffprobe, "-v", "error", "-show_entries",
            "format=format_name,duration:stream=codec_type,codec_name,width,height", "-of", "json", str(path)]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=60, shell=False)
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip()[:300]}
    return {"ok": True, **json.loads(proc.stdout)}


def strip_query(url: str | None) -> str | None:
    return url.split("?", 1)[0] if url else url


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", required=True)
    parser.add_argument("--content", default="reels", choices=CONTENT_TYPES)
    parser.add_argument("--secrets-dir", default=str(ROOT_DIR / "data" / "secrets"))
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    temp_root = ROOT_DIR / "storage" / "temp"
    report: dict = {"profile": args.profile, "content": args.content, "limit": 1, "started_at": stamp, "steps": []}

    def step(name: str, **data) -> None:
        report["steps"].append({"step": name, **data})

    provider = LocalInstagramSessionProvider(Path(args.secrets_dir))
    status = provider.status()
    report["session"] = {"configured": status.configured, "source": status.source}
    downloader = GalleryDlInstagramDownloader(provider, python_executable=sys.executable, temp_dir=temp_root)

    result = downloader.test_session()
    step("test_session", ok=result.ok, code=result.code, message=result.message, username=result.username)
    if not result.ok:
        if result.code == "session_missing":
            # Sem sessão nenhuma requisição é feita: o adapter recusa antes de iniciar o runner.
            try:
                downloader.get_profile(args.profile)
            except InstagramError as exc:
                step("get_profile", ok=False, code=exc.code, message=exc.message, retryable=exc.retryable)
        report["outcome"] = result.code
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 2

    try:
        profile = downloader.get_profile(args.profile)
        step("get_profile", ok=True, username=profile.username, user_id=profile.user_id,
             post_count=profile.post_count, is_private=profile.is_private, is_verified=profile.is_verified)

        items = list(downloader.iter_profile_media(args.profile, args.content, 1))
        step("iter_profile_media", ok=True, count=len(items))
        if not items:
            report["outcome"] = "no_items"
            print(json.dumps(report, ensure_ascii=True, indent=2))
            return 3
        item = items[0]
        step("item", source_post_id=item.source_post_id, source_media_index=item.source_media_index,
             media_type=item.media_type, is_reel=item.is_reel, source_url=item.source_url,
             media_url=strip_query(item.media_url), width=item.width, height=item.height,
             duration=item.duration, taken_at=item.taken_at, extension=item.extension)

        dest_dir = temp_root / f"poc-{stamp}"
        dest_dir.mkdir(parents=True, exist_ok=False)
        path = downloader.download(item, dest_dir)
        size = path.stat().st_size
        step("download", ok=size > 0, path=str(path), size=size, magic=detect_magic(path), ffprobe=run_ffprobe(path))
    except InstagramError as exc:
        step("error", code=exc.code, message=exc.message, retryable=exc.retryable)
        report["outcome"] = exc.code
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 1

    report["outcome"] = "ok"
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
