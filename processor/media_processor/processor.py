"""Orquestração: probe, comando, execução com progresso/cancelamento/timeout, arquivo temporário e rename."""

from __future__ import annotations

import collections
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import commands
from .errors import (
    FFmpegNotFoundError,
    InvalidMediaError,
    ProcessingCancelledError,
    ProcessingFailedError,
    ProcessorError,
)
from .hashing import sha256_file
from .options import ProcessingOptions
from .probe import ProbeResult, run_probe, video_stream
from .tools import ToolInfo, locate, popen_kwargs, tool_info

STDERR_LIMIT = 64 * 1024  # bytes de stderr mantidos (cauda)
STDERR_SUMMARY = 1500  # caracteres de stderr incluídos na mensagem de erro
POLL_INTERVAL = 0.1
THUMBNAIL_TIMEOUT = 120.0
MIN_REMAINING = 0.1  # duração mínima (s) que precisa sobrar depois do trim


@dataclass
class ProcessResult:
    output_path: Path
    probe: ProbeResult
    sha256: str
    size: int
    command: list[str]
    forced_reencode: bool
    elapsed: float


# Job Object do Windows com KILL_ON_JOB_CLOSE: se o processo do Media Tool morrer (inclusive abruptamente), o
# sistema fecha o handle e encerra os FFmpeg filhos. Um job por processo, criado sob demanda; no-op fora do Windows.
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_job_lock = threading.Lock()
_job_handle: int | None | bool = None  # None: ainda não criado; False: indisponível


def _kernel32():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


def create_kill_on_close_job() -> int | None:
    """Cria um Job Object com JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. None fora do Windows ou se o sistema recusar."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount",
                "WriteTransferCount", "OtherTransferCount")]

        class _BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
            ]

        class _ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = _kernel32()
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info),
                                                ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            return None
        return int(job)
    except (AttributeError, OSError, ValueError):
        return None


def assign_to_job(job: int, proc: subprocess.Popen) -> bool:
    """Coloca o processo no Job Object. False se não foi possível (o processo segue sem a proteção)."""
    if sys.platform != "win32" or not job:
        return False
    try:
        return bool(_kernel32().AssignProcessToJobObject(job, int(proc._handle)))  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        return False


def _guard_child(proc: subprocess.Popen) -> bool:
    """FFmpeg no Job Object do processo (KILL_ON_JOB_CLOSE): morre junto com o Media Tool."""
    global _job_handle
    if sys.platform != "win32":
        return False
    with _job_lock:
        if _job_handle is None:
            _job_handle = create_kill_on_close_job() or False
        job = _job_handle
    return bool(job) and assign_to_job(job, proc)


def _kill_tree(proc: subprocess.Popen) -> None:
    """Mata o ffmpeg e eventuais filhos."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, **popen_kwargs(),
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    if proc.poll() is None:
        proc.kill()


def _summary(stderr: str) -> str:
    text = stderr.strip()
    return text[-STDERR_SUMMARY:] if text else "(sem saída de erro)"


def run_ffmpeg(
    cmd: list[str],
    *,
    expected_duration: float | None = None,
    progress: Callable[[float], None] | None = None,
    cancel: threading.Event | None = None,
    timeout: float | None = None,
) -> None:
    """Executa o ffmpeg lendo stdout (-progress) e stderr em threads; chama `progress` na thread do chamador."""
    if cancel is not None and cancel.is_set():
        raise ProcessingCancelledError("processamento cancelado")
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **popen_kwargs()
        )
    except OSError as exc:
        raise FFmpegNotFoundError(f"não foi possível executar o ffmpeg: {exc}") from exc
    _guard_child(proc)

    updates: queue.Queue[float] = queue.Queue()
    stderr_tail: collections.deque[bytes] = collections.deque()
    stderr_size = [0]

    def read_stdout() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            key, _, value = line.partition("=")
            if key == "out_time_us" and expected_duration:
                try:
                    us = int(value)
                except ValueError:
                    continue
                updates.put(min(max(us / 1_000_000 / expected_duration, 0.0), 1.0))

    def read_stderr() -> None:
        assert proc.stderr is not None
        for chunk in iter(lambda: proc.stderr.read(4096), b""):
            stderr_tail.append(chunk)
            stderr_size[0] += len(chunk)
            while stderr_size[0] > STDERR_LIMIT and len(stderr_tail) > 1:
                stderr_size[0] -= len(stderr_tail.popleft())

    readers = [threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=read_stderr, daemon=True)]
    for t in readers:
        t.start()

    started = time.monotonic()
    last = -1.0
    outcome = None
    try:
        while True:
            try:
                value = updates.get(timeout=POLL_INTERVAL)
                if progress is not None and value > last:
                    last = value
                    progress(value)
                continue
            except queue.Empty:
                pass
            if proc.poll() is not None and updates.empty():
                break
            if cancel is not None and cancel.is_set():
                outcome = "cancelled"
                break
            if timeout is not None and time.monotonic() - started > timeout:
                outcome = "timeout"
                break
    finally:
        if proc.poll() is None:
            _kill_tree(proc)
        proc.wait()
        for t in readers:
            t.join(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()

    stderr = b"".join(stderr_tail).decode("utf-8", "replace")
    if outcome == "cancelled":
        raise ProcessingCancelledError("processamento cancelado")
    if outcome == "timeout":
        raise ProcessingFailedError(
            f"ffmpeg excedeu o tempo limite de {timeout:.0f}s", returncode=proc.returncode, stderr=stderr
        )
    if proc.returncode != 0:
        raise ProcessingFailedError(
            f"ffmpeg falhou (código {proc.returncode}): {_summary(stderr)}", returncode=proc.returncode, stderr=stderr
        )


def _temp_path(dst: Path) -> Path:
    return dst.parent / f".{dst.stem}.{uuid.uuid4().hex[:12]}.part{dst.suffix}"


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # Windows pode segurar o handle por um instante após matar o processo
        time.sleep(0.5)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


class MediaProcessor:
    def __init__(self, ffmpeg_path: str | None = None, ffprobe_path: str | None = None):
        self._ffmpeg_arg = ffmpeg_path
        self._ffprobe_arg = ffprobe_path

    # ---- ferramentas ----
    def ffmpeg_info(self) -> ToolInfo:
        return tool_info("ffmpeg", self._ffmpeg_arg)

    def ffprobe_info(self) -> ToolInfo:
        return tool_info("ffprobe", self._ffprobe_arg)

    def _require(self, tool: str) -> str:
        path, error = locate(tool, self._ffmpeg_arg if tool == "ffmpeg" else self._ffprobe_arg)
        if path is None:
            raise FFmpegNotFoundError(error or f"{tool} não encontrado")
        return path

    # ---- probe ----
    def probe(self, path: Path) -> ProbeResult:
        ffprobe = self._require("ffprobe")
        path = Path(path)
        if not path.is_file():
            raise InvalidMediaError(f"arquivo não encontrado: {path}")
        return run_probe(ffprobe, path)

    # ---- comando ----
    def build_command(self, src: Path, dst: Path, probe: ProbeResult, opts: ProcessingOptions) -> list[str]:
        path, _ = locate("ffmpeg", self._ffmpeg_arg)
        return commands.build(path or self._ffmpeg_arg or "ffmpeg", Path(src), Path(dst), probe, opts)

    def output_suffix(self, probe: ProbeResult, opts: ProcessingOptions) -> str:
        return commands.plan(probe, opts).suffix

    # ---- processamento ----
    def process(
        self,
        src: Path,
        dst: Path,
        opts: ProcessingOptions,
        *,
        progress: Callable[[float], None] | None = None,
        cancel: threading.Event | None = None,
        timeout: float | None = None,
    ) -> ProcessResult:
        started = time.monotonic()
        self._require("ffmpeg")
        self._require("ffprobe")
        src = Path(os.path.abspath(src))
        dst = Path(os.path.abspath(dst))
        if not src.exists():
            raise InvalidMediaError(f"arquivo de entrada não existe: {src}")
        if not src.is_file():
            raise InvalidMediaError(f"entrada não é um arquivo: {src}")
        if os.path.normcase(str(src)) == os.path.normcase(str(dst)) or (dst.exists() and os.path.samefile(src, dst)):
            raise ProcessorError("entrada e saída são o mesmo arquivo")
        if os.path.lexists(dst):
            raise ProcessorError(f"o arquivo de saída já existe e não será sobrescrito: {dst}")

        src_probe = self.probe(src)
        if src_probe.media_type == "video" and src_probe.duration is not None and opts.trim_start > 0:
            remaining = src_probe.duration - opts.trim_start
            min_remaining = max(MIN_REMAINING, 1.0 / src_probe.fps if src_probe.fps else 0.0)
            if remaining < min_remaining:
                raise InvalidMediaError(
                    f"O corte inicial deixa o vídeo sem duração útil (corte de {opts.trim_start:g}s "
                    f"em um vídeo de {src_probe.duration:.2f}s)"
                )
        p = commands.plan(src_probe, opts)

        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = _temp_path(dst)
        cmd = self.build_command(src, tmp, src_probe, opts)
        try:
            run_ffmpeg(
                cmd,
                expected_duration=p.expected_duration if src_probe.media_type == "video" else None,
                progress=progress,
                cancel=cancel,
                timeout=timeout,
            )
            if not tmp.is_file() or tmp.stat().st_size == 0:
                raise ProcessingFailedError("ffmpeg terminou sem gerar o arquivo de saída")
            try:
                out_probe = self.probe(tmp)
            except InvalidMediaError as exc:
                raise ProcessingFailedError(f"saída inválida: {exc.message}") from exc
            if out_probe.media_type != src_probe.media_type:
                raise ProcessingFailedError("saída inválida: tipo de mídia diferente da entrada")
            try:
                os.replace(tmp, dst)
            except OSError as exc:
                raise ProcessingFailedError(
                    f"não foi possível gravar o arquivo de saída {dst}: {exc.strerror or exc}"
                ) from exc
        except BaseException:
            _remove(tmp)
            raise
        out_probe.raw.get("format", {})["filename"] = str(dst)
        if progress is not None:
            progress(1.0)
        return ProcessResult(
            output_path=dst,
            probe=out_probe,
            sha256=sha256_file(dst),
            size=dst.stat().st_size,
            command=cmd,
            forced_reencode=p.forced_reencode,
            elapsed=time.monotonic() - started,
        )

    # ---- miniatura ----
    def make_thumbnail(self, src: Path, dst: Path, *, width: int = 320) -> Path:
        ffmpeg = self._require("ffmpeg")
        src = Path(os.path.abspath(src))
        dst = Path(os.path.abspath(dst))
        if os.path.lexists(dst):
            raise ProcessorError(f"a miniatura de saída já existe e não será sobrescrita: {dst}")
        probe = self.probe(src)
        vs = video_stream(probe.raw)
        seek = 1.0 if probe.media_type == "video" and (probe.duration or 0) > 1.5 else 0.0
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = _temp_path(dst)
        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error"]
        if seek:
            cmd += ["-ss", "1"]
        cmd += [
            "-i", commands.abs_path(src), "-map", f"0:{vs['index']}", "-frames:v", "1", "-update", "1",
            "-filter:v", f"scale={int(width)}:-2", "-map_metadata", "-1", "-fflags", "+bitexact",
            "-flags:v", "+bitexact", "-c:v", "mjpeg", "-q:v", "4", "-f", "image2", commands.abs_path(tmp),
        ]
        try:
            run_ffmpeg(cmd, timeout=THUMBNAIL_TIMEOUT)
            if not tmp.is_file() or tmp.stat().st_size == 0:
                raise ProcessingFailedError("ffmpeg não gerou a miniatura")
            os.replace(tmp, dst)
        except BaseException:
            _remove(tmp)
            raise
        return dst
