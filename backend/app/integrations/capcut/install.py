"""Detecção da instalação do CapCut Desktop (Windows) — só leitura.

Layout verificado no CapCut 9.7.1 (Windows): `%LOCALAPPDATA%\\CapCut\\Apps\\CapCut.exe` (lançador),
`Apps\\<versão>\\CapCut.exe`, `Apps\\ProductInfo.xml` (`<full_appver value="..."/>`), `User Data\\Projects\\
com.lveditor.draft\\` (rascunhos; ou `currentCustomDraftPath` do `User Data\\Config\\globalSetting`) e
`%LOCALAPPDATA%\\CapCut\\Videos` (pasta padrão da exportação).

`MEDIA_TOOL_CAPCUT_ROOT` (testes) aponta para a pasta-base equivalente a `%LOCALAPPDATA%\\CapCut`.
"""

from __future__ import annotations

import csv
import io
import os
import re
import subprocess
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from app.integrations.capcut.errors import CapCutError
from app.integrations.capcut.media import read_json

PROCESS_NAME = "CapCut.exe"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class CapCutInstall:
    root: Path                 # User Data
    launcher_exe: Path
    version: str | None
    drafts_dir: Path
    default_videos_dir: Path
    global_setting_path: Path
    app_dir: Path | None = None  # Apps\<versão> (fontes do sistema do CapCut)

    @property
    def root_meta_path(self) -> Path:
        return self.drafts_dir / "root_meta_info.json"


def _version(apps: Path) -> str | None:
    info = apps / "ProductInfo.xml"
    try:
        text = info.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        text = ""
    match = re.search(r'<full_appver\s+value="([^"]+)"', text) or re.search(r'<appver\s+value="([^"]+)"', text)
    if match:
        return match.group(1)
    versions = sorted((p.name for p in apps.glob("*") if p.is_dir() and re.fullmatch(r"\d+(\.\d+)+", p.name)),
                      key=lambda v: [int(x) for x in v.split(".")])
    return versions[-1] if versions else None


def read_ini_value(path: Path, key: str) -> str | None:
    """Valor de `key=` num INI do QSettings (sem interpretar seções; o globalSetting só tem [General])."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            return value.strip()
    return None


def _unescape_qsettings(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return value.replace("\\\\", "\\")


def _drafts_dir(user_data: Path, global_setting: Path) -> Path:
    default = user_data / "Projects" / "com.lveditor.draft"
    candidates: list[Path] = []
    custom = read_ini_value(global_setting, "currentCustomDraftPath")
    if custom:
        candidates.append(Path(_unescape_qsettings(custom)))
    candidates.append(default)
    for cand in candidates:
        meta = cand / "root_meta_info.json"
        if meta.is_file():
            try:
                root_path = read_json(meta).get("root_path")
            except (OSError, ValueError, AttributeError):
                root_path = None
            if root_path and Path(root_path).is_dir():
                return Path(root_path)
        if cand.is_dir():
            return cand
    return default


def detect(env: Mapping[str, str] | None = None) -> CapCutInstall | None:
    """Instalação do CapCut ou None (sem lançador)."""
    env = os.environ if env is None else env
    override = (env.get("MEDIA_TOOL_CAPCUT_ROOT") or "").strip()
    if override:
        base = Path(override)
    else:
        local = (env.get("LOCALAPPDATA") or "").strip()
        if not local:
            return None
        base = Path(local) / "CapCut"
    apps = base / "Apps"
    launcher = apps / PROCESS_NAME
    if not launcher.is_file():
        return None
    version = _version(apps)
    app_dir = apps / version if version and (apps / version).is_dir() else None
    user_data = base / "User Data"
    global_setting = user_data / "Config" / "globalSetting"
    return CapCutInstall(
        root=user_data, launcher_exe=launcher, version=version,
        drafts_dir=_drafts_dir(user_data, global_setting), default_videos_dir=base / "Videos",
        global_setting_path=global_setting, app_dir=app_dir,
    )


MSG_PROCESS_QUERY = "Não foi possível consultar os processos do Windows. Nova tentativa em breve."


def is_running() -> list[int]:
    """PIDs dos processos CapCut.exe (vazio fora do Windows). Se a consulta falhar: CapCutError transitório
    `process_query_failed` (nunca "fechado", que liberaria gravar o índice e abrir o CapCut)."""
    if os.name != "nt":
        return []
    try:
        result = subprocess.run(["tasklist", "/FO", "CSV", "/NH", "/FI", f"IMAGENAME eq {PROCESS_NAME}"],
                                capture_output=True, text=True, errors="replace", timeout=15,
                                creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CapCutError("process_query_failed", MSG_PROCESS_QUERY, transient=True) from exc
    if result.returncode != 0:
        raise CapCutError("process_query_failed", MSG_PROCESS_QUERY, transient=True)
    out = result.stdout or ""
    pids: list[int] = []
    for row in csv.reader(io.StringIO(out)):
        if len(row) >= 2 and row[0].strip().lower() == PROCESS_NAME.lower():
            try:
                pids.append(int(row[1]))
            except ValueError:
                continue
    return pids


# --------------------------------------------------------------------------------------------- árvore de processos

@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int
    name: str
    created: int | None  # horário de criação (FILETIME, 100 ns desde 1601); None se não deu para ler


_EPOCH_AS_FILETIME = 116444736000000000


def filetime_now() -> int:
    return time.time_ns() // 100 + _EPOCH_AS_FILETIME


def process_table() -> list[ProcessInfo]:
    """Todos os processos (PID, PID pai, executável, horário de criação) via Toolhelp32 + GetProcessTimes. Vazio fora
    do Windows; falha da consulta: CapCutError transitório `process_query_failed`."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        raise CapCutError("process_query_failed", MSG_PROCESS_QUERY, transient=True)

    def created(pid: int) -> int | None:
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        finally:
            k32.CloseHandle(handle)

    out: list[ProcessInfo] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snapshot, ctypes.byref(entry))
        if not ok:
            raise CapCutError("process_query_failed", MSG_PROCESS_QUERY, transient=True)
        while ok:
            pid = int(entry.th32ProcessID)
            out.append(ProcessInfo(pid=pid, ppid=int(entry.th32ParentProcessID), name=entry.szExeFile,
                                   created=created(pid) if pid else None))
            ok = k32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)
    return out


def track_tree(table: Iterable[ProcessInfo], known: Mapping[int, int]) -> dict[int, int]:
    """Amplia `known` ({pid: horário de criação}, a partir do processo que NÓS iniciamos) com os descendentes vistos
    em `table`. Um processo é filho de um membro se o PID pai bate E ele foi criado depois do membro; se o PID do
    membro já foi reaproveitado por outro processo, os filhos criados depois desse outro não entram."""
    table = list(table)
    current = {p.pid: p.created for p in table}
    tree = dict(known)
    changed = True
    while changed:
        changed = False
        for proc in table:
            if proc.pid in tree or proc.created is None or proc.ppid not in tree:
                continue
            parent_created = tree[proc.ppid]
            if proc.created < parent_created:
                continue
            reused = current.get(proc.ppid)
            if proc.ppid in current and reused != parent_created and (reused is None or proc.created >= reused):
                continue
            tree[proc.pid] = proc.created
            changed = True
    return tree


def alive_members(table: Iterable[ProcessInfo], tree: Mapping[int, int]) -> list[ProcessInfo]:
    """Processos de `table` que são membros vivos da árvore (mesmo PID e mesmo horário de criação)."""
    return [p for p in table if p.pid in tree and p.created is not None and p.created == tree[p.pid]]
