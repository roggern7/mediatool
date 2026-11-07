"""Modo de exportação ISOLADO: o CapCut roda numa área de trabalho do Windows separada ("MediaToolCapCut"), que o
dono não vê, e é controlado só por mensagens de janela. Só Windows.

Verificado no CapCut 9.7.1 (Windows 11, 2026-11-07; docs/CAPCUT.md "Modo de exportação isolado"):
- `CreateDesktop` + `CreateProcess(STARTUPINFO.lpDesktop="WinSta0\\MediaToolCapCut")` abre o CapCut nessa área; os
  processos filhos herdam a área.
- uma thread que chama `SetThreadDesktop` ANTES de qualquer janela/COM e depois `CoInitialize` usa UI Automation nas
  janelas dessa área; `EnumDesktopWindows` lista as janelas.
- cliques por `PostMessage` (WM_MOUSEMOVE/WM_LBUTTONDOWN/WM_LBUTTONUP, coordenadas da área cliente), rolagem por
  `PostMessage(WM_MOUSEWHEEL)` (coordenadas de tela) e `ShowWindow(SW_MAXIMIZE)` bastam para o pipeline inteiro.

Nada aqui usa entrada sintética (SetCursorPos, mouse_event, keybd_event), foco (SetForegroundWindow) nem espera de PC
livre. Todas as operações do controlador rodam numa thread dedicada presa à área (`DesktopThread`); quem chama (o
runner) usa um proxy (`IsolatedControllerProxy`) que encaminha cada chamada para ela. `uiautomation`/`pywin32` são
importados só dentro das funções.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import queue
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.integrations.capcut import install as install_mod
from app.integrations.capcut.controller import CapCutController
from app.integrations.capcut.errors import (
    AccessibilityUnavailable,
    CapCutBusy,
    CapCutError,
    ForegroundLost,
    IsolatedUnavailable,
    UiTimeout,
)
from app.integrations.capcut.install import CapCutInstall

logger = logging.getLogger("media_tool.capcut")

DESKTOP_NAME = "MediaToolCapCut"
EXPORT_MODES = ("isolated", "interactive")
DEFAULT_EXPORT_MODE = "isolated"
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEWHEEL = 0x0200, 0x0201, 0x0202, 0x020A
MK_LBUTTON = 0x0001
SW_MAXIMIZE = 3
GENERIC_ALL = 0x10000000
DESKTOP_READOBJECTS, DESKTOP_ENUMERATE = 0x0001, 0x0040
CALL_POLL_SECONDS = 0.1
REQUIRED_MODULES = ("win32service", "win32process", "win32gui", "win32con", "pythoncom", "uiautomation")


def _hint(problem: str) -> str:
    return (f"{problem} Nas configurações das Legendas CapCut, troque o modo de exportação para \"Interativo\" "
            "(usa a tela e precisa do PC livre) e tente de novo.")


def _lparam(x: int, y: int) -> int:
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


# --------------------------------------------------------------------------------------------- área de trabalho

class IsolatedDesktop:
    """Handle de uma área de trabalho do Windows (PyHDESK do pywin32)."""

    def __init__(self, handle: Any, name: str = DESKTOP_NAME):
        self.handle = handle
        self.name = name
        self._closed = False

    @property
    def path(self) -> str:
        """Valor de `STARTUPINFO.lpDesktop` para abrir processos nesta área."""
        return f"WinSta0\\{self.name}"

    def bind_thread(self) -> None:
        """Prende a thread atual a esta área (só funciona antes de a thread ter janelas/ganchos)."""
        self.handle.SetThreadDesktop()

    def windows(self) -> list[int]:
        """Todas as janelas de nível superior desta área (visíveis ou não)."""
        import win32gui

        handles: list[int] = []
        win32gui.EnumDesktopWindows(self.handle, lambda h, _arg: handles.append(h) or True, None)
        return handles

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.handle.CloseDesktop()
        except Exception:  # noqa: BLE001 - a área some sozinha quando o último handle/processo sai
            logger.info("CapCut isolado: a área de trabalho não pôde ser fechada agora", exc_info=True)


def open_desktop(name: str = DESKTOP_NAME) -> IsolatedDesktop:
    """Cria (ou abre, se já existir) a área isolada. IsolatedUnavailable se o Windows recusar."""
    if os.name != "nt":
        raise IsolatedUnavailable(_hint("O modo isolado só funciona no Windows."))
    try:
        import win32service
    except ImportError as exc:
        raise IsolatedUnavailable(_hint("O componente pywin32 não está instalado.")) from exc
    try:
        handle = win32service.CreateDesktop(name, 0, GENERIC_ALL, None)
    except Exception as exc:  # noqa: BLE001 - pywintypes.error: acesso negado, sessão sem estação interativa etc.
        logger.warning("CapCut isolado: CreateDesktop falhou", exc_info=True)
        raise IsolatedUnavailable(_hint("O Windows não permitiu criar a área de trabalho isolada.")) from exc
    return IsolatedDesktop(handle, name)


def open_existing_desktop(name: str = DESKTOP_NAME) -> IsolatedDesktop | None:
    """Abre a área isolada só se ela já existir (sobra de uma execução anterior); None se não existe/não abre."""
    if os.name != "nt":
        return None
    try:
        import win32service

        handle = win32service.OpenDesktop(name, 0, False, GENERIC_ALL)
    except Exception:  # noqa: BLE001 - não existe (ou sem acesso): não há sobra nossa nela
        return None
    return IsolatedDesktop(handle, name)


def default_desktop_windows() -> list[int]:
    """Janelas de nível superior da área de trabalho normal do dono ("Default")."""
    import win32gui
    import win32service

    handle = win32service.OpenDesktop("Default", 0, False, DESKTOP_READOBJECTS | DESKTOP_ENUMERATE)
    try:
        handles: list[int] = []
        win32gui.EnumDesktopWindows(handle, lambda h, _arg: handles.append(h) or True, None)
        return handles
    finally:
        try:
            handle.CloseDesktop()
        except Exception:  # noqa: BLE001
            pass


def availability() -> dict[str, Any]:
    """Pré-requisitos do modo isolado neste computador (só leitura; não cria a área). A prova real acontece no job:
    se o Windows recusar, o job falha com `isolated_unavailable`."""
    if os.name != "nt":
        return {"available": False, "reason": "O modo isolado só funciona no Windows."}
    missing = []
    for module in REQUIRED_MODULES:
        try:
            if importlib.util.find_spec(module) is None:
                missing.append(module)
        except (ImportError, ValueError):
            missing.append(module)
    if missing:
        return {"available": False, "reason": "Componentes ausentes: " + ", ".join(missing) + "."}
    return {"available": True, "reason": None}


# --------------------------------------------------------------------------------------------- thread presa à área

def _reset_uia_client() -> None:
    """O `uiautomation` guarda UM objeto IUIAutomation por processo, criado na thread que o usou primeiro. Zerado ao
    entrar e ao sair da thread isolada: dentro dela o objeto nasce no apartamento COM dela; fora, quem usar depois cria
    o seu."""
    try:
        from uiautomation import uiautomation as uia_module

        uia_module._AutomationClient._instance = None
    except Exception:  # noqa: BLE001 - versão sem esse singleton: nada a zerar
        pass


def init_com() -> Callable[[], None]:
    """Inicializa COM e UI Automation na thread atual (já presa à área). Devolve a função que desfaz."""
    import pythoncom

    pythoncom.CoInitialize()
    _reset_uia_client()
    try:
        import uiautomation as auto

        auto.GetRootControl()
    except Exception as exc:  # noqa: BLE001 - UI Automation sem acesso nesta área
        _reset_uia_client()
        pythoncom.CoUninitialize()
        raise IsolatedUnavailable(_hint("A UI Automation não ficou acessível na área de trabalho isolada.")) from exc

    def done() -> None:
        import gc

        _reset_uia_client()
        gc.collect()  # libera os ponteiros COM da UI Automation ainda nesta thread, antes do CoUninitialize
        try:
            pythoncom.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass

    return done


class _Call:
    __slots__ = ("fn", "args", "kwargs", "done", "result", "error")

    def __init__(self, fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]):
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.done = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None


class DesktopThread:
    """Thread dedicada presa à área isolada: `SetThreadDesktop` antes de qualquer janela/COM, depois COM/UIA; executa
    as funções enviadas por `call` (de qualquer outra thread), uma por vez, e devolve o resultado ou a exceção.

    Avisos de andamento gerados dentro dela (`post`) rodam na thread de quem está esperando em `call`, para o
    andamento do job continuar sendo gravado pela thread do runner."""

    def __init__(self, desktop: Any, *, com_init: Callable[[], Callable[[], None] | None] | None = None,
                 name: str = "capcut-isolated"):
        self.desktop = desktop
        self._com_init = com_init or init_com
        self._name = name
        self._calls: queue.Queue[_Call | None] = queue.Queue()
        self._events: queue.Queue[tuple[Callable[..., Any], tuple[Any, ...]]] = queue.Queue()
        self._ready = threading.Event()
        self._init_error: BaseException | None = None
        self._thread: threading.Thread | None = None

    @property
    def ident(self) -> int | None:
        return self._thread.ident if self._thread is not None else None

    def start(self) -> DesktopThread:
        self._thread = threading.Thread(target=self._main, name=self._name, daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._init_error is not None:
            self._thread.join(5)
            error = self._init_error
            if isinstance(error, IsolatedUnavailable):
                raise error
            raise IsolatedUnavailable(_hint("Não foi possível preparar a automação na área de trabalho isolada.")) \
                from error
        return self

    def _main(self) -> None:
        try:
            self.desktop.bind_thread()
        except Exception as exc:  # noqa: BLE001
            logger.warning("CapCut isolado: SetThreadDesktop falhou", exc_info=True)
            self._init_error = IsolatedUnavailable(_hint("O Windows não permitiu usar a área de trabalho isolada."))
            self._init_error.__cause__ = exc
            self._ready.set()
            return
        try:
            undo = self._com_init()
        except BaseException as exc:  # noqa: BLE001 - devolvido a quem iniciou
            self._init_error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            while True:
                item = self._calls.get()
                if item is None:
                    break
                try:
                    item.result = item.fn(*item.args, **item.kwargs)
                except BaseException as exc:  # noqa: BLE001 - devolvido a quem chamou
                    item.error = exc
                finally:
                    item.done.set()
        finally:
            if callable(undo):
                undo()

    def post(self, fn: Callable[..., Any], *args: Any) -> None:
        """Agenda `fn(*args)` na thread de quem está esperando em `call` (ex.: andamento do job)."""
        self._events.put((fn, args))

    def pump(self) -> None:
        while True:
            try:
                fn, args = self._events.get_nowait()
            except queue.Empty:
                return
            try:
                fn(*args)
            except Exception:  # noqa: BLE001 - andamento é informativo
                logger.warning("CapCut isolado: falha ao repassar o andamento", exc_info=True)

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Executa `fn` na thread presa à área e espera (repassando o andamento)."""
        if self._thread is None or not self._thread.is_alive():
            raise CapCutError("isolated_thread_stopped", "A automação isolada do CapCut não está mais ativa.")
        if threading.current_thread() is self._thread:
            return fn(*args, **kwargs)
        item = _Call(fn, args, kwargs)
        self._calls.put(item)
        while not item.done.wait(CALL_POLL_SECONDS):
            self.pump()
            if not self._thread.is_alive():
                raise CapCutError("isolated_thread_stopped", "A automação isolada do CapCut parou.")
        self.pump()
        if item.error is not None:
            raise item.error
        return item.result

    def stop(self, timeout: float = 30.0) -> None:
        if self._thread is None:
            return
        if self._thread.is_alive():
            self._calls.put(None)
            self._thread.join(timeout)
        self.pump()


# --------------------------------------------------------------------------------------------- controlador

class IsolatedCapCutController(CapCutController):
    """O mesmo controlador (escolha de opções, conferência do MP4, fechamento seguro com o aviso "Confirmar saída",
    árvore de processos), mas com o CapCut na área isolada e cliques/rolagem/maximizar por mensagem. Precisa rodar numa
    thread presa à área (`DesktopThread`)."""

    def __init__(self, install: CapCutInstall | None, *, desktop: Any, **kwargs: Any):
        super().__init__(install, **kwargs)
        self.desktop = desktop
        self._handles: tuple[Any, Any] | None = None  # (processo, thread) do lançador: impede reaproveitar o PID

    # -- sem uso do PC do dono
    def ensure_desktop_free(self) -> None:
        """Não se aplica: o modo isolado não usa mouse, teclado nem a tela do dono."""
        return None

    def _lost(self, message: str) -> CapCutError:
        return ForegroundLost(message)

    def _bring_front(self, hwnd: int) -> bool:
        return True  # nada de primeiro plano: as mensagens vão direto para a janela

    def snapshot(self, what: str) -> Path | None:
        # A captura de tela mostraria a tela do dono, não a área isolada: só registra.
        logger.warning("CapCut isolado: falha em '%s' (sem captura de tela no modo isolado)", what)
        return None

    # -- ciclo de vida
    def launch(self, timeout: float = 30) -> int:
        if self._running():
            raise CapCutBusy()
        self.prepare_accessibility()
        self.progress("Abrindo o CapCut (área de trabalho isolada)")
        L = self.L
        exe = self.install.launcher_exe
        startup = L.win32process.STARTUPINFO()
        startup.lpDesktop = self.desktop.path
        started = install_mod.filetime_now()
        hprocess, hthread, pid, _tid = L.win32process.CreateProcess(
            str(exe), None, None, None, False, 0, None, str(exe.parent), startup)
        self._handles = (hprocess, hthread)
        self._launched = True
        root = next((p for p in self._processes() if p.pid == pid), None)
        self._tree = {pid: root.created if root is not None and root.created is not None else started}
        pids = self._wait(lambda: self._owned(capcut_only=True), timeout, "iniciar o CapCut")
        return min(pids)

    def adopt(self, pids: set[int] | list[int]) -> None:
        """Assume como nossos os processos `pids` (sobra de uma queda do Media Tool com todas as janelas na área
        isolada), para fechá-los com o fechamento seguro."""
        wanted = set(pids)
        self._tree = {p.pid: p.created for p in self._processes() if p.pid in wanted and p.created is not None}
        self._launched = bool(self._tree)

    def wait_home(self, timeout: float = 90, accessibility_grace: float = 25) -> int:
        try:
            return super().wait_home(timeout, accessibility_grace)
        except AccessibilityUnavailable as exc:
            raise IsolatedUnavailable(_hint(
                "A interface do CapCut não ficou acessível na área de trabalho isolada.")) from exc

    # -- janelas e entrada por mensagem
    def _windows(self, pids: set[int] | None = None) -> list[tuple[int, str, str]]:
        L = self.L
        if pids is None:
            pids = self._owned(capcut_only=True) if self._launched else set(self._running())
        if not pids:
            return []
        out = []
        for h in self.desktop.windows():
            try:
                if not L.win32gui.IsWindowVisible(h):
                    continue
                _tid, pid = L.win32process.GetWindowThreadProcessId(h)
                if pid not in pids:
                    continue
                out.append((h, L.win32gui.GetWindowText(h), L.auto.ControlFromHandle(h).ClassName or ""))
            except Exception:
                continue
        return out

    def _maximize(self, hwnd: int) -> None:
        try:
            self.L.win32gui.ShowWindow(hwnd, SW_MAXIMIZE)  # na área isolada não afeta o dono
            self._sleep(1.0)
        except CapCutError:
            raise
        except Exception:  # noqa: BLE001 - maximizar é só ajuda; a checagem de visibilidade decide
            pass

    def _wheel(self, hwnd: int, notches: int) -> None:
        x0, y0, x1, y1 = self._client_rect(hwnd)
        x, y = (x0 + x1) // 2, (y0 + y1) // 2
        self.L.win32gui.PostMessage(hwnd, WM_MOUSEWHEEL, ((120 * notches) & 0xFFFF) << 16, _lparam(x, y))

    def _safe_click(self, ctrl: Any, hwnd: int) -> None:
        """Clique por mensagem no centro do controle (coordenadas da área cliente da janela `hwnd`)."""
        self._check_cancel()
        self._ensure_visible(ctrl, hwnd)
        rect = ctrl.BoundingRectangle
        if rect.right - rect.left <= 0 or rect.bottom - rect.top <= 0:
            raise UiTimeout("Controle do CapCut sem área visível.")
        x, y = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
        gui = self.L.win32gui
        cx, cy = gui.ScreenToClient(hwnd, (x, y))
        lparam = _lparam(cx, cy)
        gui.PostMessage(hwnd, WM_MOUSEMOVE, 0, lparam)
        gui.PostMessage(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
        gui.PostMessage(hwnd, WM_LBUTTONUP, 0, lparam)
        self._sleep(0.15)


# --------------------------------------------------------------------------------------------- proxy e fábrica

class IsolatedControllerProxy:
    """Encaminha cada operação do controlador para a thread presa à área isolada. `close_session` encerra a thread e
    fecha a área (depois do `shutdown`)."""

    export_mode = "isolated"

    def __init__(self, desktop: Any, worker: DesktopThread, controller: Any):
        self._desktop = desktop
        self._worker = worker
        self._controller = controller
        self._closed = False

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        return self._worker.call(getattr(self._controller, name), *args, **kwargs)

    def launch(self, timeout: float = 30) -> int:
        return self._call("launch", timeout)

    def wait_home(self, timeout: float = 90) -> int:
        return self._call("wait_home", timeout)

    def open_draft(self, name: str, timeout: float = 60) -> int:
        return self._call("open_draft", name, timeout)

    def export(self, options: Any, timeout: float = 1800) -> Path:
        return self._call("export", options, timeout)

    def close_project(self, timeout: float = 30) -> None:
        return self._call("close_project", timeout)

    def shutdown(self, timeout: float = 40) -> None:
        if self._closed:
            return None
        return self._call("shutdown", timeout)

    @property
    def warnings(self) -> list[str]:
        return self._controller.warnings

    @property
    def last_export_created(self) -> bool:
        return bool(self._controller.last_export_created)

    @property
    def export_target(self) -> Path | None:
        return self._controller.export_target

    def close_session(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._worker.stop()
        self._desktop.close()


def open_controller(install: CapCutInstall | None, *, state_dir: Path, progress: Callable[[str], None] | None = None,
                    cancel: threading.Event | None = None, desktop_factory: Callable[[], Any] | None = None,
                    com_init: Callable[[], Callable[[], None] | None] | None = None,
                    controller_cls: Callable[..., Any] | None = None, **kwargs: Any) -> IsolatedControllerProxy:
    """Cria a área isolada, a thread presa a ela e o controlador DENTRO dessa thread. Mesma assinatura das outras
    fábricas de controlador do pipeline. IsolatedUnavailable se algo disso não der (nunca cai para o interativo)."""
    report = progress or (lambda _detail: None)
    desktop = (desktop_factory or open_desktop)()
    worker = DesktopThread(desktop, com_init=com_init)
    try:
        worker.start()
        controller = worker.call(controller_cls or IsolatedCapCutController, install, desktop=desktop,
                                 state_dir=state_dir, progress=lambda detail: worker.post(report, detail),
                                 cancel=cancel, **kwargs)
    except BaseException:
        worker.stop()
        desktop.close()
        raise
    return IsolatedControllerProxy(desktop, worker, controller)


def recover_leftover(install: CapCutInstall | None, *, state_dir: Path, cancel: threading.Event | None = None,
                     running: Callable[[], list[int]] | None = None,
                     processes: Callable[[], list[install_mod.ProcessInfo]] | None = None,
                     progress: Callable[[str], None] | None = None,
                     desktop_factory: Callable[[], Any] | None = None,
                     default_windows: Callable[[], list[int]] | None = None,
                     window_pid: Callable[[int], int] | None = None,
                     com_init: Callable[[], Callable[[], None] | None] | None = None,
                     controller_cls: Callable[..., Any] | None = None) -> list[str]:
    """CapCut aberto cujas janelas estão TODAS na área isolada (sobra de uma queda do Media Tool) é nosso: fecha com o
    fechamento seguro (WM_CLOSE, aviso "Confirmar saída" cancelado, sem forçar). Devolve os avisos do fechamento.

    Não faz nada (lista vazia) se não há CapCut aberto, se a área não existe ou se algum CapCut tem janela na área
    normal do dono: esse continua sendo `capcut_busy` para quem chama. Se a sobra não fechar (tarefa em andamento),
    CapCutBusy (espera sem gastar tentativa)."""
    running = running or install_mod.is_running
    pids = set(running())
    if not pids:
        return []
    desktop = (desktop_factory or open_existing_desktop)()
    if desktop is None:
        return []
    try:
        pid_of = window_pid or _window_pid
        try:
            iso = {pid_of(h) for h in desktop.windows()} & pids
            normal = {pid_of(h) for h in (default_windows or default_desktop_windows)()} & pids
        except Exception:  # noqa: BLE001 - sem como provar que é nosso: não toca
            logger.info("CapCut isolado: não foi possível conferir as janelas do CapCut aberto", exc_info=True)
            return []
        if not iso or normal:
            return []
        logger.info("CapCut isolado: CapCut com todas as janelas na área isolada (sobra); fechando com segurança")
        if progress is not None:
            progress("Fechando o CapCut que ficou aberto na área isolada")
        worker = DesktopThread(desktop, com_init=com_init, name="capcut-isolated-recovery")
        try:
            worker.start()
            controller = worker.call(controller_cls or IsolatedCapCutController, install, desktop=desktop,
                                     state_dir=state_dir, cancel=cancel, running=running, processes=processes,
                                     progress=(lambda d: worker.post(progress, d)) if progress else None)
            worker.call(controller.adopt, pids)
            worker.call(controller.shutdown)
            warnings = list(controller.warnings)
        finally:
            worker.stop()
        if set(running()) & pids:
            raise CapCutBusy("O CapCut que ficou aberto na área isolada ainda não fechou (tarefa em andamento). "
                             "Nova tentativa em breve; ele não foi forçado a fechar.")
        return warnings
    finally:
        desktop.close()


def _window_pid(hwnd: int) -> int:
    import win32process

    return int(win32process.GetWindowThreadProcessId(hwnd)[1])
