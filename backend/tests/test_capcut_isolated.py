"""Modo de exportação isolado (sem CapCut real): controlador com janelas/UI Automation falsas (só mensagens; nenhuma
entrada sintética nem foco), thread presa à área de trabalho, proxy, recuperação de sobra na área isolada e escolha
do modo no pipeline (sem fallback para o interativo)."""

from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.integrations.capcut as capcut
from app.integrations.capcut import controller as ctl
from app.integrations.capcut import install as install_mod
from app.integrations.capcut import isolated
from app.integrations.capcut import pipeline as pipeline_mod
from app.integrations.capcut.errors import CapCutBusy, IsolatedUnavailable, UiTimeout
from app.integrations.capcut.install import ProcessInfo as P

OX, OY, W, H = 100, 50, 1000, 800   # área cliente de todas as janelas falsas, em coordenadas de tela
FORBIDDEN = ("SetCursorPos", "GetCursorPos", "mouse_event", "keybd_event", "SetForegroundWindow",
             "GetForegroundWindow", "BringWindowToTop", "SetFocus", "WindowFromPoint", "EnumWindows",
             "AttachThreadInput")


def fake_install(tmp_path: Path):
    base = tmp_path / "CapCut"
    (base / "Apps").mkdir(parents=True)
    (base / "Apps" / "CapCut.exe").write_bytes(b"MZ")
    (base / "User Data" / "Config").mkdir(parents=True)
    (base / "User Data" / "Config" / "globalSetting").write_bytes(b"[General]\r\n")
    (base / "Videos").mkdir()
    return install_mod.detect({"MEDIA_TOOL_CAPCUT_ROOT": str(base)})


class Forbidden(AssertionError):
    pass


def _forbid(name):
    def call(*_a, **_k):
        raise Forbidden(f"{name} não pode ser usado no modo isolado")
    return call


class NoWaitCancel:
    """Cancelamento que nunca dispara e não espera (os prazos do controlador correm sem dormir)."""

    def is_set(self):
        return False

    def wait(self, _seconds=None):
        return False

    def set(self):
        raise AssertionError("cancelamento inesperado")


class Rect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class Missing:
    Name = ""
    ClassName = ""

    def Exists(self, *_a):
        return False


class Ctrl:
    def __init__(self, name, rect=None, *, cls="", children=(), on_click=None):
        self.Name, self.ClassName = name, cls
        self.BoundingRectangle = rect or Rect(OX, OY, OX + W, OY + H)
        self.children = list(children)
        self.parent = None
        self.on_click = on_click
        self.ControlTypeName = "ButtonControl" if on_click else "Control"
        for child in self.children:
            child.parent = self

    def add(self, child):
        child.parent = self
        self.children.append(child)

    def Exists(self, *_a):
        return True

    def GetChildren(self):
        return list(self.children)

    def GetParentControl(self):
        return self.parent

    def walk(self):
        for child in self.children:
            yield child
            yield from child.walk()

    def Control(self, searchDepth=0, Name=None, RegexName=None):
        if RegexName is not None:
            return Missing()   # nenhum aviso "Confirmar saída" neste mundo
        return next((c for c in self.walk() if c.Name == Name), Missing())


def button(name, x, y, on_click):
    return Ctrl(name, Rect(OX + x, OY + y, OX + x + 40, OY + y + 20), on_click=on_click)


class World:
    """CapCut falso: processos, janelas da área isolada e reação aos cliques (por mensagem)."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.table = [P(4, 0, "explorer.exe", 10)]
        self.windows: dict[int, tuple[int, Ctrl]] = {}   # hwnd -> (pid, raiz UIA)
        self.posted: list[tuple[int, int, int, int]] = []
        self.created: list[dict] = []
        self.threads: set[int] = set()
        self.chosen: list[str] = []
        self.target = tmp_path / "Videos" / "MT_1.mp4"

    # -- processos
    def create_process(self, app, cmd, _pa, _ta, _inherit, _flags, _env, cwd, startup):
        self.created.append({"app": app, "cmd": cmd, "cwd": cwd, "desktop": startup.lpDesktop})
        self.table.append(P(500, 4, "CapCut.exe", 1000))
        self.open_home()
        return ("hp", "ht", 500, 1)

    # -- janelas
    def open_home(self):
        area = button("HomePageDraftMouseArea", 10, 10, self.open_editor)
        card = Ctrl("card", children=[Ctrl("HomePageDraftTitle:MT_1", Rect(OX + 10, OY + 40, OX + 90, OY + 60)),
                                      area])
        self.windows[10] = (500, Ctrl("home", cls="HomePageX_QMLTYPE_1", children=[card]))

    def open_editor(self):
        self.windows[20] = (500, Ctrl("editor", cls="MainWindow_QMLTYPE_2", children=[
            button("MainWindowTitleBarExportBtn", 900, 5, self.open_export)]))

    def open_export(self):
        root = Ctrl("export", cls="ExportWindow_QMLTYPE_3")
        for i, combo in enumerate(("ExportSharpnessInput", "ExportbitRateInput", "ExportEncoderInput",
                                   "ExportFormatInput", "FrameRateInput")):
            root.add(button(combo, 100, 50 + 40 * i, self.open_popup))
        root.add(Ctrl("ExportPathInput", Rect(OX + 100, OY + 300, OX + 500, OY + 320),
                      children=[Ctrl(str(self.target))]))
        root.add(button("ExportOkBtn", 800, 700, lambda: self.finish_export(root)))
        self.windows[30] = (500, root)

    def open_popup(self):
        items = [button(value, 200, 100 + 25 * i, (lambda v=value: self.pick(v)))
                 for i, value in enumerate(("1080P", "Recomendado", "H.264", "mp4", "60fps"))]
        self.windows[40] = (500, Ctrl("popup", cls="LVPopupWindow_QMLTYPE_4", children=items))

    def pick(self, value):
        self.chosen.append(value)
        self.windows.pop(40, None)

    def finish_export(self, root):
        self.target.write_bytes(b"MP4" * 100)
        root.add(Ctrl("automationcloseBtn"))

    def close(self, hwnd):
        self.windows.pop(hwnd, None)
        if not self.windows:
            self.table = [p for p in self.table if p.pid != 500]

    def control_at(self, hwnd, cx, cy):
        x, y = cx + OX, cy + OY
        for ctrl in self.windows[hwnd][1].walk():
            r = ctrl.BoundingRectangle
            if ctrl.on_click and r.left <= x < r.right and r.top <= y < r.bottom:
                return ctrl
        return None

    def post(self, hwnd, msg, wparam, lparam):
        self.threads.add(threading.get_ident())
        self.posted.append((hwnd, msg, wparam, lparam))
        if msg == isolated.WM_LBUTTONUP and hwnd in self.windows:
            target = self.control_at(hwnd, lparam & 0xFFFF, lparam >> 16)
            assert target is not None, "clique por mensagem fora de qualquer controle"
            target.on_click()
        elif msg == ctl.WM_CLOSE:
            self.close(hwnd)

    def libs(self):
        world = self

        def traced(fn):
            def call(*a, **k):
                world.threads.add(threading.get_ident())
                return fn(*a, **k)
            return call

        gui = SimpleNamespace(
            IsWindowVisible=traced(lambda h: True), GetWindowText=traced(lambda h: f"janela {h}"),
            PostMessage=self.post, ShowWindow=traced(lambda h, cmd: world.posted.append((h, -1, cmd, 0))),
            ScreenToClient=traced(lambda h, p: (p[0] - OX, p[1] - OY)),
            ClientToScreen=traced(lambda h, p: (p[0] + OX, p[1] + OY)),
            GetClientRect=traced(lambda h: (0, 0, W, H)), IsWindow=traced(lambda h: h in world.windows),
            IsIconic=traced(lambda h: False), GetClassName=traced(lambda h: "Qt"),
            GetWindowPlacement=_forbid("GetWindowPlacement"),
            **{name: _forbid(name) for name in FORBIDDEN})
        api = SimpleNamespace(**{name: _forbid(name) for name in FORBIDDEN})
        proc = SimpleNamespace(GetWindowThreadProcessId=traced(lambda h: (1, world.windows[h][0])),
                               STARTUPINFO=lambda: SimpleNamespace(lpDesktop=None),
                               CreateProcess=traced(self.create_process))
        auto = SimpleNamespace(ControlFromHandle=traced(
            lambda h: world.windows[h][1] if h in world.windows else Missing()),
            PatternId=SimpleNamespace(InvokePattern=1))
        con = SimpleNamespace(SW_RESTORE=9, SW_MAXIMIZE=3, SW_SHOWMAXIMIZED=3, MOUSEEVENTF_WHEEL=0x800,
                              MOUSEEVENTF_LEFTDOWN=2, MOUSEEVENTF_LEFTUP=4)
        return SimpleNamespace(win32gui=gui, win32api=api, win32process=proc, auto=auto, win32con=con)


class FakeDesktop:
    def __init__(self, world: World | None = None, *, bind_error: Exception | None = None):
        self.world = world
        self.name = isolated.DESKTOP_NAME
        self.path = f"WinSta0\\{isolated.DESKTOP_NAME}"
        self.bound: list[int] = []
        self.closed = 0
        self.bind_error = bind_error

    def bind_thread(self):
        if self.bind_error is not None:
            raise self.bind_error
        self.bound.append(threading.get_ident())

    def windows(self):
        return list(self.world.windows) if self.world else []

    def close(self):
        self.closed += 1


@pytest.fixture
def no_real_input(monkeypatch):
    """Qualquer caminho para entrada sintética/foco fora das libs falsas também falha o teste."""
    monkeypatch.setattr(ctl, "_attach_and_activate", _forbid("_attach_and_activate"))
    monkeypatch.setattr(ctl, "user_idle_seconds", lambda: 0.0)   # o dono está usando o PC agora
    monkeypatch.setattr(ctl.subprocess, "Popen", _forbid("Popen"))
    monkeypatch.setattr(ctl.subprocess, "run", _forbid("taskkill"))


def make_controller_cls(world: World):
    class Controller(isolated.IsolatedCapCutController):
        def __init__(self, install, **kw):
            kw["running"] = kw.get("running") or (lambda: [])
            kw["processes"] = kw.get("processes") or (lambda: list(world.table))
            super().__init__(install, **kw)
            self._libs = world.libs()
            self.cancel = NoWaitCancel()
    return Controller


def test_full_flow_through_proxy_uses_only_messages_on_the_desktop_thread(tmp_path, no_real_input):
    inst = fake_install(tmp_path)
    world = World(tmp_path)
    world.target = inst.default_videos_dir / "MT_1.mp4"
    desktop = FakeDesktop(world)
    progress: list[tuple[str, int]] = []
    proxy = isolated.open_controller(
        inst, state_dir=tmp_path / "state", progress=lambda d: progress.append((d, threading.get_ident())),
        cancel=threading.Event(), desktop_factory=lambda: desktop, com_init=lambda: None,
        controller_cls=make_controller_cls(world))
    assert not hasattr(proxy, "ensure_desktop_free"), "no modo isolado não há espera de PC livre"
    assert proxy._controller.ensure_desktop_free() is None, "dono usando o PC agora: mesmo assim não espera"

    assert proxy.launch() == 500
    assert world.created == [{"app": str(inst.launcher_exe), "cmd": None, "cwd": str(inst.launcher_exe.parent),
                              "desktop": "WinSta0\\MediaToolCapCut"}]
    assert proxy.wait_home() == 10
    assert proxy.open_draft("MT_1") == 20
    out = proxy.export(ctl.ExportOptions(resolution="1080P", fps="60", source_width=1080, source_height=1920))
    assert out == world.target and proxy.last_export_created is True and proxy.export_target == world.target
    assert world.chosen == ["1080P", "Recomendado", "H.264", "mp4", "60fps"]
    proxy.close_project()
    proxy.shutdown()
    assert not world.windows and all(p.pid != 500 for p in world.table), "fechado só por WM_CLOSE"
    proxy.close_session()
    assert desktop.closed == 1 and not proxy._worker._thread.is_alive()
    proxy.close_session()
    assert desktop.closed == 1, "idempotente"

    # Tudo na thread presa à área (que fez SetThreadDesktop), nunca na thread de quem chamou.
    [bound] = desktop.bound
    assert bound != threading.get_ident() and world.threads == {bound}
    # Andamento repassado na thread de quem chamou (o runner grava no banco).
    assert ("Abrindo o CapCut (área de trabalho isolada)", threading.get_ident()) in progress
    # Cliques = MOVE/DOWN/UP por mensagem em coordenadas da área CLIENTE; maximizar por ShowWindow.
    clicks = [(h, m, w, lp) for h, m, w, lp in world.posted if m in (isolated.WM_MOUSEMOVE, isolated.WM_LBUTTONDOWN,
                                                                    isolated.WM_LBUTTONUP)]
    assert clicks[:3] == [(10, isolated.WM_MOUSEMOVE, 0, (20 << 16) | 30),
                          (10, isolated.WM_LBUTTONDOWN, isolated.MK_LBUTTON, (20 << 16) | 30),
                          (10, isolated.WM_LBUTTONUP, 0, (20 << 16) | 30)]
    assert (10, -1, isolated.SW_MAXIMIZE, 0) in world.posted


def test_fakes_catch_synthetic_input_of_the_interactive_controller(tmp_path, no_real_input):
    """Prova de que a trava dos testes funciona: o controlador interativo, com os mesmos fakes, é pego."""
    inst = fake_install(tmp_path)
    world = World(tmp_path)
    world.open_home()
    c = ctl.CapCutController(inst, state_dir=tmp_path / "s", running=lambda: [], processes=lambda: [])
    c._libs = world.libs()
    c.cancel = NoWaitCancel()
    area = world.windows[10][1].Control(Name="HomePageDraftMouseArea")
    with pytest.raises(Forbidden):
        c._safe_click(area, 10)
    with pytest.raises(Forbidden):
        c._wheel(10, -1)
    with pytest.raises(ctl.DesktopBusy):
        c.ensure_desktop_free()  # dono usando o PC: o interativo espera; o isolado não (ver o teste do fluxo)


def test_wheel_is_a_posted_message_with_screen_coordinates(tmp_path, no_real_input):
    inst = fake_install(tmp_path)
    world = World(tmp_path)
    c = make_controller_cls(world)(inst, desktop=FakeDesktop(world), state_dir=tmp_path / "s")
    c._wheel(77, -2)
    x, y = OX + W // 2, OY + H // 2
    assert world.posted == [(77, isolated.WM_MOUSEWHEEL, ((-240) & 0xFFFF) << 16, (y << 16) | x)]
    assert c._bring_front(77) is True and c.snapshot("x") is None
    assert isinstance(c._lost("x"), ctl.ForegroundLost), "nunca DesktopBusy no modo isolado"


def test_accessibility_failure_in_isolated_mode_is_isolated_unavailable(tmp_path, no_real_input, monkeypatch):
    inst = fake_install(tmp_path)
    world = World(tmp_path)
    c = make_controller_cls(world)(inst, desktop=FakeDesktop(world), state_dir=tmp_path / "s")

    def no_tree(self, timeout=90, accessibility_grace=25):
        raise ctl.AccessibilityUnavailable()

    monkeypatch.setattr(ctl.CapCutController, "wait_home", no_tree)
    with pytest.raises(IsolatedUnavailable) as err:
        c.wait_home()
    assert err.value.code == "isolated_unavailable" and err.value.transient is False
    assert "Interativo" in err.value.message


def test_desktop_thread_forwards_calls_errors_and_events():
    desktop = FakeDesktop()
    worker = isolated.DesktopThread(desktop, com_init=lambda: None).start()
    try:
        assert worker.call(threading.get_ident) == desktop.bound[0] != threading.get_ident()
        with pytest.raises(UiTimeout):
            worker.call(lambda: (_ for _ in ()).throw(UiTimeout()))
        seen = []
        worker.call(lambda: worker.post(lambda: seen.append(threading.get_ident())))
        assert seen == [threading.get_ident()]
    finally:
        worker.stop()
    assert not worker._thread.is_alive()
    with pytest.raises(capcut.CapCutError) as err:
        worker.call(lambda: 1)
    assert err.value.code == "isolated_thread_stopped"


def test_desktop_or_thread_failure_is_isolated_unavailable_and_closes_desktop():
    bad = FakeDesktop(bind_error=OSError("acesso negado"))
    with pytest.raises(IsolatedUnavailable):
        isolated.open_controller(None, state_dir=Path("."), desktop_factory=lambda: bad, com_init=lambda: None)
    assert bad.closed == 1

    def no_uia():
        raise IsolatedUnavailable("A UI Automation não ficou acessível.")

    desktop = FakeDesktop()
    with pytest.raises(IsolatedUnavailable, match="UI Automation"):
        isolated.open_controller(None, state_dir=Path("."), desktop_factory=lambda: desktop, com_init=no_uia)
    assert desktop.closed == 1

    def refuse():
        raise IsolatedUnavailable()

    with pytest.raises(IsolatedUnavailable):
        isolated.open_controller(None, state_dir=Path("."), desktop_factory=refuse)


def test_open_desktop_failure_maps_to_isolated_unavailable(monkeypatch):
    if isolated.os.name != "nt":
        pytest.skip("só Windows")
    import win32service

    def boom(*_a):
        raise OSError("Acesso negado")

    monkeypatch.setattr(win32service, "CreateDesktop", boom)
    with pytest.raises(IsolatedUnavailable) as err:
        isolated.open_desktop()
    assert err.value.code == "isolated_unavailable" and "Interativo" in err.value.message


# --------------------------------------------------------------------------------------------- sobra da queda

def leftover_world(tmp_path):
    world = World(tmp_path)
    world.table.append(P(500, 4, "CapCut.exe", 1000))
    world.open_home()
    world.open_editor()
    return world


def test_recover_leftover_with_all_windows_on_isolated_desktop_closes_it_safely(tmp_path, no_real_input):
    inst = fake_install(tmp_path)
    world = leftover_world(tmp_path)
    desktop = FakeDesktop(world)
    running = lambda: [p.pid for p in world.table if p.name == "CapCut.exe"]  # noqa: E731
    warnings = isolated.recover_leftover(
        inst, state_dir=tmp_path / "s", running=running, processes=lambda: list(world.table),
        desktop_factory=lambda: desktop, default_windows=lambda: [], window_pid=lambda h: world.windows[h][0],
        com_init=lambda: None, controller_cls=make_controller_cls(world))
    assert warnings == [] and running() == []
    assert sorted(h for h, m, _w, _l in world.posted if m == ctl.WM_CLOSE) == [10, 20]
    assert desktop.closed == 1 and desktop.bound, "fechamento feito na thread presa à área"


def test_owner_capcut_on_normal_desktop_is_never_touched(tmp_path, no_real_input):
    inst = fake_install(tmp_path)
    world = leftover_world(tmp_path)
    desktop = FakeDesktop(world)
    owner_hwnd = 99   # janela na área normal do dono, do mesmo PID
    pids = {**{h: v[0] for h, v in world.windows.items()}, owner_hwnd: 500}
    warnings = isolated.recover_leftover(
        inst, state_dir=tmp_path / "s", running=lambda: [500], processes=lambda: list(world.table),
        desktop_factory=lambda: desktop, default_windows=lambda: [owner_hwnd], window_pid=pids.__getitem__,
        com_init=lambda: None, controller_cls=_forbid("controlador"))
    assert warnings == [] and world.posted == [] and desktop.closed == 1 and not desktop.bound
    # nenhum CapCut aberto / área inexistente: nada a fazer
    assert isolated.recover_leftover(inst, state_dir=tmp_path, running=lambda: [],
                                     desktop_factory=_forbid("área")) == []
    assert isolated.recover_leftover(inst, state_dir=tmp_path, running=lambda: [500],
                                     desktop_factory=lambda: None) == []


def test_leftover_that_does_not_close_is_capcut_busy(tmp_path):
    world = leftover_world(tmp_path)

    class Stubborn:
        def __init__(self, *_a, **_k):
            self.warnings = ["O CapCut ficou aberto: tarefa em andamento."]

        def adopt(self, pids):
            self.pids = pids

        def shutdown(self):
            pass

    with pytest.raises(CapCutBusy):
        isolated.recover_leftover(None, state_dir=tmp_path, running=lambda: [500],
                                  processes=lambda: list(world.table),
                                  desktop_factory=lambda: FakeDesktop(world), default_windows=lambda: [],
                                  window_pid=lambda h: world.windows[h][0], com_init=lambda: None,
                                  controller_cls=Stubborn)


def test_leftover_is_not_touched_when_another_capcut_outside_the_tree_runs(tmp_path, no_real_input):
    """Sobra na área isolada + OUTRO CapCut (ex. do dono) fora da árvore dela, mesmo sem janela na área normal: nada é
    adotado nem fechado; quem chama segue para capcut_busy."""
    inst = fake_install(tmp_path)
    world = leftover_world(tmp_path)
    world.table.append(P(700, 4, "CapCut.exe", 3000))  # CapCut de outra pessoa/área, filho do explorer
    created: list = []

    class Spy:
        def __init__(self, *_a, **_k):
            created.append(1)

    warnings = isolated.recover_leftover(
        inst, state_dir=tmp_path / "s", running=lambda: [500, 700], processes=lambda: list(world.table),
        desktop_factory=lambda: FakeDesktop(world), default_windows=lambda: [], window_pid=lambda h: world.windows[h][0],
        com_init=lambda: None, controller_cls=Spy)
    assert warnings == [] and created == [], "nenhum controlador criado: nada foi fechado"
    assert not any(m == ctl.WM_CLOSE for _h, m, _w, _l in world.posted)


def test_discard_job_exports_removes_only_this_job_files(tmp_path):
    """Exportação terminada por um CapCut órfão depois da queda: apagada no começo da tentativa seguinte do MESMO job;
    arquivos de outros jobs e do dono ficam."""
    from app.integrations.capcut.finalize import discard_job_exports

    videos = tmp_path / "Videos"
    videos.mkdir()
    for name in ("MT_17_m2.mp4", "MT_17_m2(1).mp4", "MT_17_m21.mp4", "MT_18_m2.mp4", "0713.mp4", "c1b0d8f7.mp4"):
        (videos / name).write_bytes(b"x")
    assert discard_job_exports(videos, "MT_17_m2") == []
    assert sorted(x.name for x in videos.iterdir()) == ["0713.mp4", "MT_17_m21.mp4", "MT_18_m2.mp4", "c1b0d8f7.mp4"]
    assert discard_job_exports(videos, "0713") == [] and (videos / "0713.mp4").exists(), "só nomes MT_"


# --------------------------------------------------------------------------------------------- escolha do modo

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg ausente")


@pytest.fixture
def video_env(tmp_path, monkeypatch):
    inst = fake_install(tmp_path)
    (inst.drafts_dir).mkdir(parents=True, exist_ok=True)
    video = tmp_path / "origem.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=30:duration=2", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2:sample_rate=48000", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(video)], check=True)
    monkeypatch.setattr(pipeline_mod, "_deps_ui_ok", lambda: True)
    monkeypatch.setattr(isolated, "recover_leftover", lambda *_a, **_k: [])
    spec = capcut.CapCutJobSpec(job_id=7, video_path=video, draft_name="MT_7_m3", output_dir=tmp_path / "saida",
                                output_name="saida", work_dir=tmp_path / "data" / "capcut" / "work" / "7",
                                models_dir=tmp_path / "models",
                                transcript=[{"start": 0.1, "end": 0.6, "text": "Bom", "probability": 0.9}])
    return inst, spec


@needs_ffmpeg
def test_isolated_unavailable_fails_without_falling_back_to_interactive(video_env, monkeypatch):
    inst, spec = video_env
    assert spec.export_mode == "isolated", "padrão = isolado"

    def unavailable(*_a, **_k):
        raise IsolatedUnavailable()

    monkeypatch.setattr(isolated, "open_controller", unavailable)
    monkeypatch.setattr(pipeline_mod, "CapCutController", _forbid("CapCutController (interativo)"))
    with pytest.raises(IsolatedUnavailable) as err:
        pipeline_mod.process_video(spec, cancel=threading.Event(), on_stage=lambda *_a: None, install=inst,
                                   running=lambda: [])
    assert err.value.code == "isolated_unavailable" and err.value.transient is False
    assert not (inst.drafts_dir / "MT_7_m3").exists(), "rascunho temporário removido"


@needs_ffmpeg
def test_interactive_mode_uses_the_current_controller(video_env, monkeypatch):
    inst, spec = video_env
    spec.export_mode = "interactive"
    made = []

    class Interactive:
        def __init__(self, install, **kw):
            made.append(kw)

        def ensure_desktop_free(self):
            raise capcut.DesktopBusy()

        def shutdown(self):
            pass

    monkeypatch.setattr(isolated, "open_controller", _forbid("modo isolado"))
    monkeypatch.setattr(pipeline_mod, "CapCutController", Interactive)
    with pytest.raises(capcut.DesktopBusy):
        pipeline_mod.process_video(spec, cancel=threading.Event(), on_stage=lambda *_a: None, install=inst,
                                   running=lambda: [])
    assert len(made) == 1


@needs_ffmpeg
def test_unknown_export_mode_is_rejected(video_env):
    inst, spec = video_env
    spec.export_mode = "fantasma"
    with pytest.raises(capcut.CapCutError) as err:
        pipeline_mod.process_video(spec, cancel=threading.Event(), on_stage=lambda *_a: None, install=inst,
                                   running=lambda: [])
    assert err.value.code == "invalid_export_mode"


@needs_ffmpeg
def test_isolated_session_is_closed_after_the_job(video_env, monkeypatch):
    inst, spec = video_env
    events = []

    class Proxy:
        warnings: list[str] = []
        last_export_created = False
        export_target = None

        def launch(self):
            events.append("launch")
            raise UiTimeout()

        def shutdown(self):
            events.append("shutdown")

        def close_session(self):
            events.append("close_session")

    monkeypatch.setattr(isolated, "open_controller", lambda *_a, **_k: Proxy())
    with pytest.raises(UiTimeout):
        pipeline_mod.process_video(spec, cancel=threading.Event(), on_stage=lambda *_a: None, install=inst,
                                   running=lambda: [])
    assert events == ["launch", "shutdown", "close_session"]


def test_leftover_recovery_runs_before_the_busy_check(tmp_path, monkeypatch):
    """Com CapCut aberto: primeiro a recuperação da sobra; se ainda houver CapCut (do dono), capcut_busy."""
    calls = []
    monkeypatch.setattr(isolated, "recover_leftover", lambda *_a, **_k: calls.append("recover") or [])
    monkeypatch.setattr(pipeline_mod, "_deps_ui_ok", lambda: True)
    monkeypatch.setattr(pipeline_mod, "probe", lambda _v: SimpleNamespace(duration=2.0, has_audio=False, width=64,
                                                                          height=48, fps=30.0))
    inst = fake_install(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    spec = capcut.CapCutJobSpec(job_id=1, video_path=video, draft_name="MT_1_m1", output_dir=tmp_path / "o",
                                output_name="o", work_dir=tmp_path / "d" / "capcut" / "work" / "1",
                                models_dir=tmp_path / "m", transcript=[{"start": 0.1, "end": 0.6, "text": "Oi",
                                                                        "probability": 0.9}])
    with pytest.raises(CapCutBusy):
        pipeline_mod.process_video(spec, cancel=threading.Event(), on_stage=lambda *_a: None, install=inst,
                                   running=lambda: [4242])
    assert calls == ["recover"]
