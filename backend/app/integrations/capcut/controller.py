"""Automação do CapCut Desktop pela interface (UI Automation + cliques reais com trava). Só Windows.

`uiautomation`/`pywin32` são importados dentro das funções. Cliques reais SEMPRE passam por `_safe_click`: a janela
alvo precisa estar em primeiro plano e o ponto do clique precisa pertencer a ela (senão `ForegroundLost`, sem clicar);
o cursor volta para onde estava. Nenhuma tecla é enviada ao CapCut. Fechamentos usam WM_CLOSE (sem mouse).
Nomes de controles verificados no CapCut 9.7.1 (interface em português): ver docs/CAPCUT.md.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.integrations.capcut import install as install_mod
from app.integrations.capcut.errors import (
    AccessibilityUnavailable,
    CapCutBusy,
    CapCutCancelled,
    CapCutError,
    CapCutNotInstalled,
    DesktopBusy,
    ExportFailed,
    ForegroundLost,
    UiTimeout,
)
from app.integrations.capcut.install import CapCutInstall
from app.integrations.capcut.media import atomic_write_bytes

logger = logging.getLogger("media_tool.capcut")

ACCESSIBILITY_FLAGS = {"disable_qt_access": "0", "accessibleDisableUnignoredChildren": "0"}
GLOBAL_SETTING_BACKUP = "globalSetting.orig"

CLS_HOME = "HomePageX_QMLTYPE"
CLS_EDITOR = "MainWindow_QMLTYPE"
CLS_EXPORT = "ExportWindow_QMLTYPE"
CLS_POPUP = "LVPopupWindow_QMLTYPE"
# Aviso modal (página web embutida) que o CapCut abre no lugar da exportação, ex.: recurso Pro sem assinatura.
CLS_BLOCKING_MODAL = "ModalRouterWebView_QMLTYPE"
# Fechamento: textos de botão que CANCELAM o aviso de saída (nunca o que confirma), intervalo entre novos pedidos
# de fechamento e espera máxima por uma tarefa em andamento.
EXIT_PROMPT_CANCEL = {"cancelar", "cancel", "não", "nao", "no", "voltar", "continuar", "continuar editando"}
EXIT_RETRY_SECONDS = 15.0
CLOSE_REPEAT_SECONDS = 5.0  # repetição do pedido de fechamento sem aviso (editor -> tela inicial -> sair)
EXIT_TASK_TIMEOUT = 600.0

RESOLUTIONS = (("480P", 480), ("720P", 720), ("1080P", 1080), ("2K", 1440), ("4K", 2160), ("8K", 4320))
BITRATES = {"low": "Abaixar", "recommended": "Recomendado", "high": "Superior"}
FPS_ITEMS = ((24.0, "24fps"), (25.0, "25fps"), (29.97, "29.97fps"), (30.0, "30fps"), (50.0, "50fps"),
             (59.94, "59.94fps"), (60.0, "60fps"), (120.0, "120fps"))
GA_ROOT = 2
WM_CLOSE = 0x0010
# Segundos sem teclado/mouse do dono antes de abrir o CapCut (a exportação usa cliques reais e o primeiro plano).
USER_IDLE_REQUIRED = 15.0


# --------------------------------------------------------------------------------------------- uso do PC

def _tick_count() -> int:
    import ctypes

    return int(ctypes.windll.kernel32.GetTickCount()) & 0xFFFFFFFF


def _last_input_tick() -> int | None:
    """GetTickCount da última entrada de teclado/mouse da sessão (inclui entradas sintéticas)."""
    try:
        import ctypes

        class LastInputInfo(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LastInputInfo()
        info.cbSize = ctypes.sizeof(LastInputInfo)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        return int(info.dwTime)
    except Exception:  # noqa: BLE001 - fora do Windows ou API indisponível
        return None


def user_idle_seconds() -> float:
    """Segundos desde a última entrada de teclado/mouse (infinito se não der para medir)."""
    last = _last_input_tick()
    if last is None:
        return float("inf")
    return ((_tick_count() - last) & 0xFFFFFFFF) / 1000.0


def _attach_and_activate(hwnd: int) -> None:
    """Traz `hwnd` para a frente anexando a entrada à thread da janela em primeiro plano (técnica padrão do Windows
    quando SetForegroundWindow sozinho é recusado)."""
    import ctypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    me = kernel32.GetCurrentThreadId()
    attached = bool(fg_thread) and fg_thread != me and bool(user32.AttachThreadInput(me, fg_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.ShowWindow(hwnd, 5)  # SW_SHOW
        user32.SetForegroundWindow(hwnd)
        user32.SetFocus(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(me, fg_thread, False)


# --------------------------------------------------------------------------------------------- opções de exportação

@dataclass
class ExportOptions:
    resolution: str = "original"     # 'original'|'480P'|'720P'|'1080P'|'2K'|'4K'
    bitrate: str = "recommended"     # 'low'|'recommended'|'high'
    fps: str = "original"            # 'original' ou '24'|'25'|'30'|'50'|'60'
    source_width: int = 0
    source_height: int = 0
    source_fps: float = 0.0
    codec: str = "H.264"
    file_format: str = "mp4"

    def resolution_label(self) -> str:
        if self.resolution != "original":
            return self.resolution
        short = min(self.source_width, self.source_height) if self.source_width and self.source_height else 1080
        label = RESOLUTIONS[0][0]
        for name, side in RESOLUTIONS:
            if side <= short and name != "8K":
                label = name
        return label

    def expected_short_side(self) -> int:
        return dict(RESOLUTIONS)[self.resolution_label()]

    def bitrate_label(self) -> str:
        return BITRATES.get(self.bitrate, BITRATES["recommended"])

    def fps_value(self) -> float:
        if self.fps != "original":
            return float(self.fps)
        src = self.source_fps or 30.0
        return min(FPS_ITEMS, key=lambda item: abs(item[0] - src))[0]

    def fps_label(self) -> str:
        value = self.fps_value()
        return min(FPS_ITEMS, key=lambda item: abs(item[0] - value))[1]


# --------------------------------------------------------------------------------------------- globalSetting

def patch_accessibility_flags(path: Path, backup: Path) -> bool:
    """Grava `disable_qt_access=0` e `accessibleDisableUnignoredChildren=0` no globalSetting (QSettings INI).

    Backup único do original em `backup`; preserva o fim de linha e as demais linhas. Devolve True se mudou algo.
    """
    path = Path(path)
    raw = path.read_bytes() if path.is_file() else b""
    bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""  # BOM UTF-8: preservado na gravação
    newline = "\r\n" if (b"\r\n" in raw or not raw) else "\n"
    text = raw[len(bom):].decode("utf-8", errors="surrogateescape")
    if raw and not Path(backup).exists():
        Path(backup).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
    lines = text.split(newline) if text else ["[General]", ""]
    pending = dict(ACCESSIBILITY_FLAGS)
    changed = False
    for i, line in enumerate(lines):
        key, sep, value = line.partition("=")
        if sep and key.strip() in pending:
            want = pending.pop(key.strip())
            if value.strip() != want:
                lines[i] = f"{key}={want}"
                changed = True
    if pending:
        header = next((i for i, line in enumerate(lines) if line.strip() == "[General]"), None)
        if header is None:
            lines[0:0] = ["[General]"]
            header = 0
        lines[header + 1:header + 1] = [f"{k}={v}" for k, v in pending.items()]
        changed = True
    if changed:
        if path.exists() and not os.access(path, os.W_OK):
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        atomic_write_bytes(path, bom + newline.join(lines).encode("utf-8", errors="surrogateescape"))
    return changed


# --------------------------------------------------------------------------------------------- controlador

def _libs() -> Any:
    try:
        import uiautomation as auto
        import win32api
        import win32con
        import win32gui
        import win32process
    except ImportError as exc:
        raise CapCutError("deps_missing", "uiautomation/pywin32 não estão instalados.") from exc

    class Libs:
        pass

    libs = Libs()
    libs.auto, libs.win32api, libs.win32con, libs.win32gui, libs.win32process = (
        auto, win32api, win32con, win32gui, win32process)
    return libs


class CapCutController:
    """Uma sessão do CapCut aberta por nós. Não mexe em CapCut aberto por outra pessoa/sessão."""

    def __init__(self, install: CapCutInstall | None, *, state_dir: Path,
                 progress: Callable[[str], None] | None = None, cancel: threading.Event | None = None,
                 running: Callable[[], list[int]] | None = None,
                 processes: Callable[[], list[install_mod.ProcessInfo]] | None = None):
        if install is None or not install.launcher_exe.is_file():
            raise CapCutNotInstalled()
        self.install = install
        self.state_dir = Path(state_dir)
        self.progress = progress or (lambda _detail: None)
        self.cancel = cancel or threading.Event()
        self._running = running or install_mod.is_running
        self._processes = processes or install_mod.process_table
        self._launched = False
        # Árvore do processo que NÓS iniciamos: {pid: horário de criação} (lançador e descendentes).
        self._tree: dict[int, int] = {}
        self._proc: subprocess.Popen | None = None  # manter o handle aberto impede o reaproveitamento do PID
        self.last_export_created = False
        self.export_target: Path | None = None
        self.warnings: list[str] = []
        self._libs: Any = None
        self._closing = False  # fechando (close_project/shutdown): o cancelamento não interrompe
        self._last_injected_tick = 0  # GetTickCount da última entrada sintética nossa (0 = nenhuma ainda)
        # Espera máxima por uma tarefa que o CapCut diz estar em andamento ao fechar (nunca forçamos).
        self.task_timeout = EXIT_TASK_TIMEOUT
        self.exit_prompt_seen = ""

    # ------------------------------------------------------------------ utilidades
    @property
    def L(self) -> Any:
        if self._libs is None:
            self._libs = _libs()
        return self._libs

    def _check_cancel(self) -> None:
        # Durante o fechamento o cancelamento NÃO interrompe: fechar com segurança é justamente o que o cancelamento
        # pede (verificado no real: antes, cancelar no meio da exportação abortava o fechamento e deixava o CapCut
        # aberto, com rascunho e vídeo intermediário sobrando).
        if self.cancel.is_set() and not self._closing:
            raise CapCutCancelled()

    def _sleep(self, seconds: float) -> None:
        if self._closing:
            time.sleep(seconds)
            return
        if self.cancel.wait(seconds):
            raise CapCutCancelled()

    def _wait(self, cond: Callable[[], Any], timeout: float, what: str, interval: float = 0.5) -> Any:
        deadline = time.monotonic() + timeout
        while True:
            self._check_cancel()
            result = cond()
            if result:
                return result
            if time.monotonic() >= deadline:
                raise UiTimeout(f"O CapCut não respondeu a tempo ({what}).")
            self._sleep(interval)

    def _owned(self, *, capcut_only: bool = False) -> set[int]:
        """PIDs vivos da árvore do processo que iniciamos (só `CapCut.exe` com `capcut_only`). Nunca inclui um CapCut
        aberto pelo dono por fora, mesmo depois de o nosso ter caído."""
        if not self._tree:
            return set()
        table = self._processes()
        self._tree = install_mod.track_tree(table, self._tree)
        return {p.pid for p in install_mod.alive_members(table, self._tree)
                if not capcut_only or p.name.lower() == install_mod.PROCESS_NAME.lower()}

    def _windows(self, pids: set[int] | None = None) -> list[tuple[int, str, str]]:
        """(hwnd, título, classe QML) das janelas visíveis dos processos CapCut: depois do launch, só os da nossa
        árvore."""
        L = self.L
        if pids is None:
            pids = self._owned(capcut_only=True) if self._launched else set(self._running())
        if not pids:
            return []
        handles: list[int] = []

        def collect(h: int, _arg: Any) -> bool:
            if L.win32gui.IsWindowVisible(h):
                handles.append(h)
            return True

        L.win32gui.EnumWindows(collect, None)
        out = []
        for h in handles:
            try:
                _tid, pid = L.win32process.GetWindowThreadProcessId(h)
                if pid not in pids:
                    continue
                out.append((h, L.win32gui.GetWindowText(h), L.auto.ControlFromHandle(h).ClassName or ""))
            except Exception:
                continue
        return out

    def _find_window(self, prefix: str) -> int | None:
        return next((h for h, _t, cls in self._windows() if cls.startswith(prefix)), None)

    def _control(self, hwnd: int, name: str, depth: int = 12, timeout: float = 5.0) -> Any:
        root = self.L.auto.ControlFromHandle(hwnd)
        ctrl = root.Control(searchDepth=depth, Name=name)
        return ctrl if ctrl.Exists(timeout, 0.25) else None

    # ------------------------------------------------------------------ uso do PC pelo dono
    def _mark_injected(self) -> None:
        """Anota o instante da última entrada sintética nossa (GetLastInputInfo também conta entradas injetadas)."""
        self._last_injected_tick = _tick_count()

    def user_input_since_injection(self) -> bool:
        """True se houve teclado/mouse DO DONO depois da nossa última entrada sintética (margem de 400 ms)."""
        last = _last_input_tick()
        if last is None:
            return False
        ref = self._last_injected_tick
        if not ref:
            return user_idle_seconds() < USER_IDLE_REQUIRED
        delta = (last - ref) & 0xFFFFFFFF  # GetTickCount dá a volta em ~49 dias
        return 400 < delta < 0x80000000

    def ensure_desktop_free(self) -> None:
        """Antes de abrir o CapCut: se o dono usou o PC há menos de USER_IDLE_REQUIRED s, esperar (DesktopBusy,
        sem gastar tentativa). A exportação precisa do CapCut em primeiro plano e de alguns cliques reais; o Windows
        também recusa trazer janela para a frente logo após o dono digitar/clicar em outro programa."""
        idle = user_idle_seconds()
        if idle < USER_IDLE_REQUIRED:
            raise DesktopBusy()

    def _lost(self, message: str) -> CapCutError:
        """Perda de foco: se veio de uso do PC pelo dono, é espera (DesktopBusy); senão, falha transitória."""
        if self.user_input_since_injection():
            return DesktopBusy()
        return ForegroundLost(message)

    def _bring_front(self, hwnd: int) -> bool:
        L = self.L
        gui = L.win32gui
        if gui.IsIconic(hwnd):
            gui.ShowWindow(hwnd, L.win32con.SW_RESTORE)
        for attempt in range(8):
            self._check_cancel()
            if gui.GetForegroundWindow() == hwnd:
                return True
            try:
                if attempt >= 1:
                    # Toque de Alt (verificado na prova): o Windows só permite SetForegroundWindow após entrada.
                    L.win32api.keybd_event(0x12, 0, 0, 0)
                    L.win32api.keybd_event(0x12, 0, 2, 0)
                    self._mark_injected()
                if attempt >= 3:
                    _attach_and_activate(hwnd)
                else:
                    gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
            self._sleep(0.3)
        return gui.GetForegroundWindow() == hwnd

    def _blocker(self, x: int, y: int) -> int:
        import ctypes

        return int(ctypes.windll.user32.GetAncestor(self.L.win32gui.WindowFromPoint((x, y)), GA_ROOT) or 0)

    def _dismiss_own_overlay(self, blocker: int, target: int) -> bool:
        """Se a janela que cobre o ponto é um balão/dica do PRÓPRIO CapCut que abrimos (não editor, exportação,
        tela inicial nem lista de opções), fecha com WM_CLOSE. True se fechou algo."""
        L = self.L
        if not blocker or blocker == target:
            return False
        try:
            _tid, pid = L.win32process.GetWindowThreadProcessId(blocker)
        except Exception:
            return False
        if pid not in self._owned(capcut_only=True):
            return False
        cls = L.auto.ControlFromHandle(blocker).ClassName or ""
        if cls.startswith((CLS_HOME, CLS_EDITOR, CLS_EXPORT, CLS_POPUP, CLS_BLOCKING_MODAL)):
            return False
        logger.info("CapCut: fechando a janela de dica %s que cobria o clique", cls.split("_QML")[0])
        L.win32gui.PostMessage(blocker, WM_CLOSE, 0, 0)
        self._sleep(0.6)
        return True

    def _blocker_name(self, blocker: int) -> str:
        """Classe da janela que bloqueou (só a classe; nunca o título de programa do dono)."""
        try:
            return self.L.win32gui.GetClassName(blocker) or "?"
        except Exception:
            return "?"

    def _client_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        """Área cliente da janela em coordenadas de tela (left, top, right, bottom)."""
        gui = self.L.win32gui
        left, top, right, bottom = gui.GetClientRect(hwnd)
        x0, y0 = gui.ClientToScreen(hwnd, (left, top))
        x1, y1 = gui.ClientToScreen(hwnd, (right, bottom))
        return x0, y0, x1, y1

    def _wheel(self, hwnd: int, notches: int) -> None:
        """Rola `notches` (negativo = para baixo) com o cursor no centro da janela, com a mesma trava do clique."""
        L = self.L
        import ctypes

        x0, y0, x1, y1 = self._client_rect(hwnd)
        x, y = (x0 + x1) // 2, (y0 + y1) // 2
        root = ctypes.windll.user32.GetAncestor(L.win32gui.WindowFromPoint((x, y)), GA_ROOT)
        if root != hwnd or L.win32gui.GetForegroundWindow() != hwnd:
            raise self._lost(f"Outra janela ({self._blocker_name(root)}) está sobre o CapCut; rolagem abortada.")
        old = L.win32api.GetCursorPos()
        try:
            L.win32api.SetCursorPos((x, y))
            time.sleep(0.05)
            L.win32api.mouse_event(L.win32con.MOUSEEVENTF_WHEEL, 0, 0, 120 * notches, 0)
            self._mark_injected()
        finally:
            L.win32api.SetCursorPos(old)

    def _ensure_visible(self, ctrl: Any, hwnd: int, tries: int = 15) -> None:
        """Garante o controle inteiro dentro da área visível da janela, rolando a lista sob ela se preciso
        (verificado: com a janela menor, os cartões de Projetos ficam abaixo da borda e o clique cairia em outra
        janela)."""
        for _ in range(tries):
            self._check_cancel()
            rect = ctrl.BoundingRectangle
            x0, y0, x1, y1 = self._client_rect(hwnd)
            inside = rect.top >= y0 and rect.bottom <= y1 and rect.left >= x0 and rect.right <= x1
            if rect.bottom - rect.top > 0 and inside:
                return
            below = rect.bottom > y1 or rect.bottom - rect.top <= 0
            self._wheel(hwnd, -2 if below else 2)
            self._sleep(0.4)
        raise UiTimeout("O controle do CapCut não ficou visível na janela.")

    def _maximize(self, hwnd: int) -> None:
        gui = self.L.win32gui
        try:
            if not gui.GetWindowPlacement(hwnd)[1] == self.L.win32con.SW_SHOWMAXIMIZED:
                gui.ShowWindow(hwnd, self.L.win32con.SW_MAXIMIZE)
                self._sleep(1.0)
        except Exception:  # noqa: BLE001 - maximizar é só ajuda; a checagem de visibilidade decide
            pass

    def _safe_click(self, ctrl: Any, hwnd: int) -> None:
        """Clique real com trava de primeiro plano e de alvo; ForegroundLost sem clicar se algo não bater."""
        L = self.L
        for _round in range(3):
            self._check_cancel()
            if not self._bring_front(hwnd):
                raise self._lost("O CapCut não ficou em primeiro plano; clique abortado.")
            self._ensure_visible(ctrl, hwnd)
            rect = ctrl.BoundingRectangle
            if rect.right - rect.left <= 0 or rect.bottom - rect.top <= 0:
                raise UiTimeout("Controle do CapCut sem área visível na tela.")
            x, y = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
            root = self._blocker(x, y)
            if root == hwnd and L.win32gui.GetForegroundWindow() == hwnd:
                break
            # Balão/dica do próprio CapCut por cima: fecha e tenta de novo; janela de outro programa: aborta.
            if not self._dismiss_own_overlay(root, hwnd):
                raise self._lost(f"Outra janela ({self._blocker_name(root)}) está sobre o CapCut no ponto do "
                                 "clique; clique abortado.")
        else:
            raise ForegroundLost("Janelas do CapCut continuam cobrindo o ponto do clique; clique abortado.")
        old = L.win32api.GetCursorPos()
        try:
            L.win32api.SetCursorPos((x, y))
            time.sleep(0.05)
            if L.win32gui.GetForegroundWindow() != hwnd:
                raise self._lost("O foco mudou antes do clique; clique abortado.")
            L.win32api.mouse_event(L.win32con.MOUSEEVENTF_LEFTDOWN, 0, 0)
            L.win32api.mouse_event(L.win32con.MOUSEEVENTF_LEFTUP, 0, 0)
            self._mark_injected()
            time.sleep(0.08)
        finally:
            L.win32api.SetCursorPos(old)

    def _click_until(self, ctrl: Any, hwnd: int, cond: Callable[[], Any], what: str, *, per_try: float,
                     tries: int = 3) -> Any:
        """Clica (com trava) e espera `cond`; repete o clique se nada acontecer (o 1º clique às vezes só ativa a
        janela do Qt). UiTimeout depois de `tries` cliques sem efeito.

        Verificado no CapCut 9.7.1: a janela de exportação às vezes abre DEPOIS do prazo; ela é modal e fica na frente,
        então um novo clique seria recusado pela trava. Por isso `cond` é conferida antes de cada novo clique e também
        quando o clique é recusado: se o efeito já aconteceu, segue sem clicar de novo."""
        for attempt in range(tries):
            if attempt:
                done = cond()
                if done:
                    return done
            try:
                self._safe_click(ctrl, hwnd)
            except (ForegroundLost, DesktopBusy):
                done = self._wait_quiet(cond, 3.0)
                if done:
                    return done
                raise
            try:
                return self._wait(cond, per_try, what, interval=0.3)
            except UiTimeout:
                if attempt == tries - 1:
                    self.snapshot(what)
                    raise
                logger.info("CapCut: sem resposta ao clique (%s); tentando de novo", what)
        raise UiTimeout(f"O CapCut não respondeu a tempo ({what}).")  # pragma: no cover

    def _wait_stable_rect(self, ctrl: Any, hwnd: int, timeout: float = 3.0) -> None:
        """Espera o retângulo do controle ficar igual em duas leituras seguidas e dentro da janela `hwnd`."""
        prev = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancel()
            r = ctrl.BoundingRectangle
            cur = (r.left, r.top, r.right, r.bottom)
            try:
                x0, y0, x1, y1 = self._client_rect(hwnd)
                inside = r.right - r.left > 0 and r.left >= x0 and r.right <= x1 and r.top >= y0 and r.bottom <= y1
            except Exception:  # noqa: BLE001 - janela fechou; o clique com trava decide
                return
            if inside and cur == prev:
                return
            prev = cur
            self._sleep(0.15)

    def _wait_quiet(self, cond: Callable[[], Any], seconds: float) -> Any:
        """Espera `cond` por até `seconds` sem levantar UiTimeout (None se não aconteceu)."""
        try:
            return self._wait(cond, seconds, "conferir", interval=0.3)
        except UiTimeout:
            return None

    def snapshot(self, what: str) -> Path | None:
        """Captura de tela para diagnóstico em <state_dir>/diag (só local; nunca enviada)."""
        try:
            from PIL import ImageGrab

            diag = self.state_dir / "diag"
            diag.mkdir(parents=True, exist_ok=True)
            safe = "".join(c if c.isalnum() else "_" for c in what)[:40]
            target = diag / f"{time.strftime('%Y%m%d-%H%M%S')}-{safe}.png"
            ImageGrab.grab(all_screens=True).save(target)
            logger.warning("CapCut: falha em '%s'; captura de diagnóstico em %s", what, target)
            return target
        except Exception:  # noqa: BLE001 - diagnóstico é opcional
            return None

    # ------------------------------------------------------------------ ciclo de vida
    def prepare_accessibility(self) -> None:
        """Liga a acessibilidade do Qt para a PRÓXIMA execução (CapCut fechado; repetir antes de cada launch)."""
        if self._running():
            raise CapCutBusy()
        patch_accessibility_flags(self.install.global_setting_path, self.state_dir / GLOBAL_SETTING_BACKUP)

    def restore_settings(self) -> None:
        """Nada a desfazer: o arquivo não fica somente leitura e o próprio CapCut regrava os valores ao subir."""

    def launch(self, timeout: float = 30) -> int:
        if self._running():
            raise CapCutBusy()
        self.prepare_accessibility()
        self.progress("Abrindo o CapCut")
        started = install_mod.filetime_now()
        self._proc = subprocess.Popen([str(self.install.launcher_exe)], cwd=str(self.install.launcher_exe.parent),
                                      close_fds=True)
        self._launched = True
        root = next((p for p in self._processes() if p.pid == self._proc.pid), None)
        self._tree = {self._proc.pid: root.created if root is not None and root.created is not None else started}
        pids = self._wait(lambda: self._owned(capcut_only=True), timeout, "iniciar o CapCut")
        return min(pids)

    def wait_home(self, timeout: float = 90, accessibility_grace: float = 25) -> int:
        """Espera a tela inicial com a árvore de UI Automation preenchida."""
        first_seen: float | None = None
        deadline = time.monotonic() + timeout
        while True:
            self._check_cancel()
            hwnd = self._find_window(CLS_HOME)
            if hwnd:
                first_seen = first_seen or time.monotonic()
                try:
                    if len(self.L.auto.ControlFromHandle(hwnd).GetChildren()) > 0:
                        return hwnd
                except Exception:
                    pass
                if time.monotonic() - first_seen > accessibility_grace:
                    raise AccessibilityUnavailable()
            if time.monotonic() >= deadline:
                raise UiTimeout("A tela inicial do CapCut não apareceu a tempo.")
            self._sleep(0.5)

    def open_draft(self, name: str, timeout: float = 60) -> int:
        home = self.wait_home(timeout)
        self._bring_front(home)
        self._maximize(home)
        title = self._control(home, f"HomePageDraftTitle:{name}", depth=14, timeout=15)
        if title is None:
            raise UiTimeout("O rascunho não apareceu na tela inicial do CapCut.")
        parent = title.GetParentControl()
        area = parent.Control(searchDepth=2, Name="HomePageDraftMouseArea") if parent else None
        target = area if area is not None and area.Exists(1, 0.2) else (parent or title)
        self.progress("Abrindo o rascunho")
        self._safe_click(target, home)
        return self._wait(lambda: self._find_window(CLS_EDITOR), timeout, "abrir o rascunho")

    def _choose(self, export_hwnd: int, combo_name: str, value: str) -> bool:
        """Escolhe `value` num combo da exportação (lista abre em LVPopupWindow). False se o item não existir."""
        combo = self._control(export_hwnd, combo_name, depth=8, timeout=5)
        if combo is None:
            self.warnings.append(f"Opção de exportação não encontrada: {combo_name}.")
            return False
        for attempt in range(2):
            popup = self._find_window(CLS_POPUP) if attempt else None
            if not popup:
                popup = self._click_until(combo, export_hwnd, lambda: self._find_window(CLS_POPUP),
                                          "abrir a lista de opções", per_try=3.0)
            item = self._control(popup, value, depth=8, timeout=3)
            if item is None:
                self._safe_click(combo, export_hwnd)  # fecha a lista sem mudar
                self.warnings.append(f"Valor de exportação indisponível no CapCut: {value}.")
                return False
            # Verificado: a lista abre com animação; clicar antes de ela parar faz o ponto cair na janela de
            # exportação (a trava recusa). Esperar a posição do item estabilizar dentro da lista.
            self._wait_stable_rect(item, popup)
            try:
                self._safe_click(item, popup)
                break
            except ForegroundLost:
                if attempt:
                    raise
                logger.info("CapCut: clique na lista recusado (%s); tentando de novo", value)
                self._sleep(0.8)
        try:
            self._wait(lambda: not self._find_window(CLS_POPUP), 5, "fechar a lista", interval=0.3)
        except UiTimeout:
            self._safe_click(combo, export_hwnd)
        return True

    def _export_path(self, export_hwnd: int) -> Path | None:
        box = self._control(export_hwnd, "ExportPathInput", depth=8, timeout=5)
        if box is None:
            return None
        stack = [(box, 0)]
        while stack:
            ctrl, depth = stack.pop()
            name = ctrl.Name or ""
            if ctrl is not box and ":" in name and name.lower().endswith((".mp4", ".mov")):
                return Path(name)
            if depth < 3:
                stack.extend((c, depth + 1) for c in ctrl.GetChildren())
        return None

    def export(self, options: ExportOptions, timeout: float = 1800) -> Path:
        editor = self._find_window(CLS_EDITOR)
        if not editor:
            raise UiTimeout("O editor do CapCut não está aberto.")
        button = self._control(editor, "MainWindowTitleBarExportBtn", depth=8, timeout=20)
        if button is None:
            raise UiTimeout("Botão Exportar do CapCut não encontrado.")
        self.progress("Abrindo a exportação")
        def opened() -> tuple[str, int] | None:
            export = self._find_window(CLS_EXPORT)
            if export:
                return ("export", export)
            modal = self._find_window(CLS_BLOCKING_MODAL)
            return ("modal", modal) if modal else None

        kind, export_hwnd = self._click_until(button, editor, opened, "abrir a exportação", per_try=20.0)
        if kind == "modal":
            # Verificado no CapCut 9.7.1: projeto com recurso Pro (ex.: efeito de texto com diamante) numa conta sem
            # acesso abre este aviso (página web embutida, sem texto acessível) no lugar da janela de exportação.
            self.snapshot("exportacao bloqueada")
            self.L.win32gui.PostMessage(export_hwnd, WM_CLOSE, 0, 0)
            raise CapCutError(
                "capcut_export_blocked",
                "O CapCut bloqueou a exportação com um aviso próprio (no teste, por recurso Pro: efeito de texto). "
                "Use um modelo de legenda sem recursos Pro (os marcados com diamante no CapCut) ou ative o Pro na "
                "conta do CapCut.", transient=False)
        # A janela aparece antes de a árvore de controles ficar pronta: esperar o botão de confirmar.
        self._wait(lambda: self._control(export_hwnd, "ExportOkBtn", depth=8, timeout=0) is not None, 20,
                   "carregar a janela de exportação")
        self._sleep(0.8)
        self.progress("Escolhendo as opções de exportação")
        for combo_name, value in (("ExportSharpnessInput", options.resolution_label()),
                                  ("ExportbitRateInput", options.bitrate_label()),
                                  ("ExportEncoderInput", options.codec),
                                  ("ExportFormatInput", options.file_format),
                                  ("FrameRateInput", options.fps_label())):
            self._choose(export_hwnd, combo_name, value)
            self._sleep(0.4)
        target = self._export_path(export_hwnd)
        if target is None:
            raise ExportFailed("Não foi possível ler o caminho de exportação do CapCut.")
        existed = target.exists()
        self.export_target = target
        ok = self._control(export_hwnd, "ExportOkBtn", depth=8, timeout=5)
        if ok is None:
            raise UiTimeout("Botão de confirmar a exportação não encontrado.")
        self.progress("Exportando")
        self._safe_click(ok, export_hwnd)

        def finished() -> bool:
            if not self.L.win32gui.IsWindow(export_hwnd):
                raise ExportFailed("A janela de exportação fechou antes de concluir.")
            return any(self._control(export_hwnd, n, depth=8, timeout=0) is not None
                       for n in ("automationcloseBtn", "automationopenFolderBtn"))

        self._wait(finished, timeout, "concluir a exportação", interval=2.0)
        last = -1

        def stable() -> bool:
            nonlocal last
            size = target.stat().st_size if target.is_file() else -1
            done = size > 0 and size == last
            last = size
            return done

        self._wait(stable, 120, "gravar o arquivo exportado", interval=1.5)
        self.L.win32gui.PostMessage(export_hwnd, WM_CLOSE, 0, 0)
        self.last_export_created = not existed
        return target

    # ------------------------------------------------------------------ fechamento seguro
    def _exit_prompt(self) -> tuple[int, str, str, Any] | None:
        """Diálogo modal do CapCut aberto (ex.: "Confirmar saída? Uma tarefa está em andamento."): (hwnd, título,
        detalhe, raiz UIA). O QML do CapCut expõe `mainText: <título>` e `subText: <detalhe>` como nomes acessíveis."""
        auto = self.L.auto
        for hwnd, _title, _cls in self._windows():
            try:
                root = auto.ControlFromHandle(hwnd)
                main = root.Control(searchDepth=12, RegexName=r"^mainText: ")
                if not main.Exists(0, 0):
                    continue
                sub = root.Control(searchDepth=12, RegexName=r"^subText: ")
                sub_text = sub.Name[len("subText: "):] if sub.Exists(0, 0) else ""
                return hwnd, main.Name[len("mainText: "):], sub_text, root
            except Exception:  # noqa: BLE001 - janela fechando no meio da leitura
                continue
        return None

    def _cancel_prompt(self, hwnd: int, root: Any) -> bool:
        """Responde o diálogo SEM confirmar a saída: botão de cancelar/voltar (Invoke; clique travado se preciso) ou,
        se o diálogo for uma janela própria, fecha só ele (fechar = cancelar no QML do CapCut). Nunca clica no botão de
        confirmar."""
        L = self.L
        buttons = []
        stack = [(root, 0)]
        while stack:
            ctrl, depth = stack.pop()
            for child in ctrl.GetChildren():
                if child.ControlTypeName == "ButtonControl" and (child.Name or "").strip():
                    buttons.append(child)
                if depth < 10:
                    stack.append((child, depth + 1))
        cancel = next((b for b in buttons if (b.Name or "").strip().casefold() in EXIT_PROMPT_CANCEL), None)
        if cancel is not None:
            try:
                cancel.GetPattern(L.auto.PatternId.InvokePattern).Invoke()
                self._sleep(1.0)
                if not self._exit_prompt():
                    return True
            except Exception:  # noqa: BLE001 - Invoke nem sempre funciona nos itens QML; tenta o clique travado
                pass
            try:
                self._safe_click(cancel, hwnd)
                self._sleep(1.0)
                if not self._exit_prompt():
                    return True
            except CapCutError:
                pass
        cls = L.auto.ControlFromHandle(hwnd).ClassName or ""
        if not cls.startswith((CLS_HOME, CLS_EDITOR)):
            L.win32gui.PostMessage(hwnd, WM_CLOSE, 0, 0)
            self._sleep(1.0)
            return not self._exit_prompt()
        return False

    def _close_gracefully(self, post_close: Callable[[], None], done: Callable[[], bool], what: str,
                          timeout: float) -> bool:
        """Pede o fechamento e espera `done`. Se o CapCut perguntar se quer sair com uma tarefa em andamento, NÃO
        confirma: cancela o aviso, espera e pede de novo a cada EXIT_RETRY_SECONDS, até `task_timeout`. Nunca força.
        False = o CapCut continua aberto (tarefa não terminou no prazo)."""
        post_close()
        deadline = time.monotonic() + timeout
        task_deadline: float | None = None
        next_retry = time.monotonic() + CLOSE_REPEAT_SECONDS
        while True:
            if done():
                return True
            prompt = self._exit_prompt()
            now = time.monotonic()
            if prompt:
                hwnd, title, detail, root = prompt
                if task_deadline is None:
                    task_deadline = now + self.task_timeout
                    logger.info("CapCut pediu confirmação ao %s (%s / %s): aguardando a tarefa terminar, sem forçar",
                                what, title, detail)
                self.exit_prompt_seen = f"{title} {detail}".strip()
                self._cancel_prompt(hwnd, root)
                next_retry = time.monotonic() + EXIT_RETRY_SECONDS
            elif now >= next_retry:
                # Pede de novo (sem confirmar nada): o 1º pedido pode só ter levado do editor à tela inicial, ou a
                # tarefa avisada pode ter terminado.
                post_close()
                next_retry = now + (EXIT_RETRY_SECONDS if task_deadline is not None else CLOSE_REPEAT_SECONDS)
            limit = task_deadline if task_deadline is not None else deadline
            if now >= limit:
                return False
            time.sleep(1.0)

    def close_project(self, timeout: float = 30) -> None:
        """Ver `_close_project_impl`; roda com o cancelamento desligado (o fechamento seguro sempre vai até o fim)."""
        previous, self._closing = self._closing, True
        try:
            self._close_project_impl(timeout)
        finally:
            self._closing = previous

    def _close_project_impl(self, timeout: float = 30) -> None:
        editor = self._find_window(CLS_EDITOR)
        if not editor:
            return

        def post_close() -> None:
            if self.L.win32gui.IsWindow(editor):
                self.L.win32gui.PostMessage(editor, WM_CLOSE, 0, 0)

        ok = self._close_gracefully(
            post_close, lambda: bool(self._find_window(CLS_HOME)) or not self._owned(capcut_only=True),
            "fechar o projeto", timeout)
        if not ok:
            if self.exit_prompt_seen:
                self.warnings.append("O CapCut ficou aberto: avisou que uma tarefa ainda estava em andamento "
                                     f"({self.exit_prompt_seen}). Ele não foi forçado a fechar.")
                return
            raise UiTimeout("O CapCut não respondeu a tempo (fechar o projeto).")

    def shutdown(self, timeout: float = 40) -> None:
        """Ver `_shutdown_impl`; roda com o cancelamento desligado (o fechamento seguro sempre vai até o fim)."""
        previous, self._closing = self._closing, True
        try:
            self._shutdown_impl(timeout)
        finally:
            self._closing = previous

    def _shutdown_impl(self, timeout: float = 40) -> None:
        """Fecha o CapCut que NÓS abrimos: só processos da árvore do lançador que iniciamos (PID pai + horário de
        criação), com WM_CLOSE nas janelas desses PIDs. NUNCA força (sem taskkill): se o CapCut avisar que há tarefa
        em andamento, espera ela terminar (até `task_timeout`) e, se não terminar, deixa o CapCut aberto com aviso. Um
        CapCut aberto pelo dono por fora nunca é tocado."""
        if not self._launched:
            return
        if not self._owned(capcut_only=True):
            self._launched = False
            return

        def post_close() -> None:
            try:
                prompt = self._exit_prompt()
                for hwnd, _title, _cls in self._windows(self._owned(capcut_only=True)):
                    if not (prompt and prompt[0] == hwnd):
                        self.L.win32gui.PostMessage(hwnd, WM_CLOSE, 0, 0)
            except CapCutError:
                pass

        closed = self._close_gracefully(post_close, lambda: not self._owned(capcut_only=True), "fechar o CapCut",
                                        timeout)
        if not closed:
            detail = (f"avisou que uma tarefa ainda estava em andamento ({self.exit_prompt_seen})"
                      if self.exit_prompt_seen else "não fechou no prazo")
            self.warnings.append(f"O CapCut ficou aberto: {detail}. Ele não foi forçado a fechar; a fila espera até "
                                 "ele ser fechado.")
            logger.warning("CapCut: não fechou (%s); deixado aberto, sem forçar", detail)
        self._launched = False
