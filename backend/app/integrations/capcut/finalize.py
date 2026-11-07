"""Conferência e entrega do vídeo exportado pelo CapCut.

Confere o arquivo com ffprobe, devolve o áudio ORIGINAL sem recodificar (remux `-c copy`, conferido depois) e move
para a pasta final com nome seguro e sem sobrescrever. O intermediário do CapCut só é apagado se estiver na pasta
padrão de vídeos do CapCut e tiver sido criado por este job.
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.integrations.capcut.errors import CapCutError
from app.integrations.capcut.media import MediaProbe, atomic_write_json, probe, read_json, run_tool, tool_path

DURATION_TOLERANCE_S = 0.5
EXPORTS_RECORD = "exports.json"
MSG_OUTPUT_WRITE = ("Não foi possível gravar o vídeo legendado na pasta de saída (arquivo em uso ou sem permissão). "
                    "Nova tentativa em breve.")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


@dataclass
class FinalReport:
    output_path: Path
    probe: dict[str, Any]
    audio_from_original: bool
    intermediate_deleted: bool
    warnings: list[str] = field(default_factory=list)


def safe_file_name(name: str, *, default: str = "video", max_len: int = 120) -> str:
    """Nome base seguro no Windows (sem extensão)."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".").strip()
    if not cleaned:
        cleaned = default
    if cleaned.split(".")[0].upper() in _RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned[:max_len].rstrip(" .") or default


def unique_path(dest_dir: Path, base: str, ext: str = ".mp4") -> Path:
    candidate = dest_dir / f"{base}{ext}"
    n = 2
    while candidate.exists():
        candidate = dest_dir / f"{base} ({n}){ext}"
        n += 1
    return candidate


def _is_inside(path: Path, folder: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(folder).resolve())
        return True
    except (ValueError, OSError):
        return False


def _is_job_file(name: str, draft_name: str) -> bool:
    """Arquivo exportado deste job: começa pelo nome do rascunho (MT_...) seguido de `.` ou `(` (ex. `nome(1).mp4`)."""
    return (bool(draft_name) and draft_name.startswith("MT_") and name.startswith(draft_name)
            and name[len(draft_name):len(draft_name) + 1] in (".", "("))


def record_export(work_dir: Path, draft_name: str, path: Path) -> None:
    """Anota em `<work_dir>/exports.json` um arquivo exportado pelo CapCut para este job (limpo depois)."""
    record = Path(work_dir) / EXPORTS_RECORD
    try:
        data = read_json(record) if record.is_file() else {}
    except (OSError, ValueError):
        data = {}
    paths = [str(p) for p in (data.get("exports") or []) if isinstance(p, str)] if isinstance(data, dict) else []
    if str(path) not in paths:
        paths.append(str(path))
    atomic_write_json(record, {"draft_name": draft_name, "exports": paths})


def discard_recorded_exports(work_dir: Path, capcut_videos_dir: Path | None) -> list[str]:
    """Apaga os exportados anotados em `exports.json`, só os que estão na pasta padrão de vídeos do CapCut e cujo nome
    começa pelo rascunho do job. Devolve os que ficaram (o registro só guarda esses)."""
    record = Path(work_dir) / EXPORTS_RECORD
    if not record.is_file():
        return []
    try:
        data = read_json(record)
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        record.unlink(missing_ok=True)
        return []
    draft_name = str(data.get("draft_name") or "")
    remaining: list[str] = []
    for raw in data.get("exports") or []:
        if not isinstance(raw, str):
            continue
        path = Path(raw)
        if not (capcut_videos_dir and _is_inside(path, capcut_videos_dir) and _is_job_file(path.name, draft_name)):
            continue  # fora da pasta do CapCut ou de outro nome: nunca apagado
        try:
            path.unlink(missing_ok=True)
        except OSError:
            remaining.append(raw)
    if remaining:
        atomic_write_json(record, {"draft_name": draft_name, "exports": remaining})
    else:
        record.unlink(missing_ok=True)
    return remaining


def probe_dict(info: MediaProbe) -> dict[str, Any]:
    return {"width": info.width, "height": info.height, "fps": info.fps, "duration": round(info.duration, 3),
            "video_codec": info.video_codec, "audio_codec": info.audio_codec, "sample_rate": info.sample_rate,
            "channels": info.channels, "size": info.size}


def _remux(exported: Path, original: Path, out: Path) -> None:
    result = run_tool([tool_path("ffmpeg"), "-nostdin", "-v", "error", "-y", "-i", str(exported), "-i", str(original),
                       "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-movflags", "+faststart",
                       str(out)], timeout=1800)
    if result.returncode != 0 or not out.is_file():
        out.unlink(missing_ok=True)
        raise CapCutError("output_invalid", "Não foi possível juntar o áudio original ao vídeo exportado.")


def finalize_output(exported: Path, original: Path, dest_dir: Path, file_name: str, keep_original_audio: bool = True,
                    *, expected_short_side: int | None = None, expected_fps: float | None = None,
                    capcut_videos_dir: Path | None = None, created_by_job: bool = False) -> FinalReport:
    exported, original, dest_dir = Path(exported), Path(original), Path(dest_dir)
    warnings: list[str] = []
    if not exported.is_file() or exported.stat().st_size == 0:
        raise CapCutError("output_invalid", "O CapCut não gerou o arquivo exportado.", transient=True)
    try:
        exp = probe(exported)
    except CapCutError as exc:
        raise CapCutError("output_invalid", "O vídeo exportado pelo CapCut é inválido.", transient=True) from exc
    src = probe(original)
    if abs(exp.duration - src.duration) > DURATION_TOLERANCE_S:
        raise CapCutError("output_invalid", f"Duração exportada ({exp.duration:.2f} s) difere da original "
                                            f"({src.duration:.2f} s).", transient=True)
    if expected_short_side and min(exp.width, exp.height) != expected_short_side:
        warnings.append(f"Resolução exportada {exp.width}x{exp.height} diferente da pedida.")
    if expected_fps and exp.fps and abs(exp.fps - expected_fps) > 0.6:
        warnings.append(f"Taxa de quadros exportada {exp.fps:g} fps diferente da pedida ({expected_fps:g}).")
    if exp.video_codec != "h264":
        warnings.append(f"Codec de vídeo exportado: {exp.video_codec}.")

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CapCutError("output_write_failed", MSG_OUTPUT_WRITE, transient=True) from exc
    final = unique_path(dest_dir, safe_file_name(file_name))
    part = dest_dir / f".{final.stem}.{uuid.uuid4().hex[:8]}.part.mp4"
    may_delete = bool(created_by_job and capcut_videos_dir and _is_inside(exported, capcut_videos_dir))
    audio_from_original = False
    try:
        if keep_original_audio and src.has_audio:
            try:
                _remux(exported, original, part)
                out = probe(part)
                if (out.audio_codec, out.sample_rate, out.channels) != (src.audio_codec, src.sample_rate, src.channels):
                    raise CapCutError("output_invalid", "O áudio final não confere com o original.")
                audio_from_original = True
            except CapCutError as exc:
                part.unlink(missing_ok=True)
                warnings.append(f"{exc.message} Mantido o áudio exportado pelo CapCut.")
        if not audio_from_original:
            if may_delete:
                shutil.move(str(exported), str(part))
            else:
                shutil.copy2(exported, part)
        if final.exists():  # corrida improvável: escolhe outro nome
            final = unique_path(dest_dir, safe_file_name(file_name))
        os.replace(part, final)
    except OSError as exc:  # arquivo em uso, sem permissão, disco cheio: vale nova tentativa
        part.unlink(missing_ok=True)
        raise CapCutError("output_write_failed", MSG_OUTPUT_WRITE, transient=True) from exc
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    deleted = False
    if may_delete and exported.exists():
        try:
            exported.unlink()
            deleted = True
        except OSError:
            warnings.append("Não foi possível apagar o arquivo intermediário do CapCut.")
    elif may_delete:
        deleted = True
    return FinalReport(output_path=final, probe=probe_dict(probe(final)), audio_from_original=audio_from_original,
                       intermediate_deleted=deleted, warnings=warnings)
