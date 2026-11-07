"""Núcleo CapCut: detecção da instalação, status, skill, globalSetting (acessibilidade) e opções de exportação.

Nada aqui toca o CapCut real: instalações falsas em tmp_path via MEDIA_TOOL_CAPCUT_ROOT.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import app.integrations.capcut as capcut
from app.integrations.capcut import controller as ctl
from app.integrations.capcut import install as install_mod
from app.integrations.capcut import skill
from app.integrations.capcut.errors import CapCutBusy, CapCutNotInstalled


def fake_capcut(tmp_path: Path, *, custom_drafts: Path | None = None) -> Path:
    base = tmp_path / "CapCut"
    (base / "Apps" / "9.7.1.4203" / "Resources" / "Font" / "SystemFont").mkdir(parents=True)
    (base / "Apps" / "9.7.1.4203" / "Resources" / "Font" / "SystemFont" / "en.ttf").write_bytes(b"font")
    (base / "Apps" / "CapCut.exe").write_bytes(b"MZ")
    (base / "Apps" / "ProductInfo.xml").write_text(
        '﻿<?xml version="1.0"?>\n<packet>\n<appver value="9.7.1" />\n<full_appver value="9.7.1.4203" />\n'
        '</packet>\n', encoding="utf-8")
    (base / "User Data" / "Config").mkdir(parents=True)
    (base / "User Data" / "Projects" / "com.lveditor.draft").mkdir(parents=True)
    lines = ["[General]", "foo=1"]
    if custom_drafts is not None:
        custom_drafts.mkdir(parents=True, exist_ok=True)
        lines.append("currentCustomDraftPath=" + str(custom_drafts).replace("\\", "\\\\"))
    (base / "User Data" / "Config" / "globalSetting").write_bytes(("\r\n".join(lines) + "\r\n").encode())
    return base


def test_detect_by_env_override(tmp_path):
    base = fake_capcut(tmp_path)
    inst = install_mod.detect({"MEDIA_TOOL_CAPCUT_ROOT": str(base)})
    assert inst is not None
    assert inst.version == "9.7.1.4203"
    assert inst.launcher_exe == base / "Apps" / "CapCut.exe"
    assert inst.root == base / "User Data"
    assert inst.drafts_dir == base / "User Data" / "Projects" / "com.lveditor.draft"
    assert inst.default_videos_dir == base / "Videos"
    assert inst.global_setting_path == base / "User Data" / "Config" / "globalSetting"
    assert inst.app_dir == base / "Apps" / "9.7.1.4203"


def test_detect_custom_drafts_dir_and_missing_install(tmp_path):
    custom = tmp_path / "Rascunhos Outros"
    base = fake_capcut(tmp_path, custom_drafts=custom)
    inst = install_mod.detect({"MEDIA_TOOL_CAPCUT_ROOT": str(base)})
    assert inst.drafts_dir == custom
    assert install_mod.detect({"MEDIA_TOOL_CAPCUT_ROOT": str(tmp_path / "nada")}) is None
    assert install_mod.detect({}) is None


def test_status_shape_without_heavy_imports(tmp_path, monkeypatch):
    base = fake_capcut(tmp_path)
    monkeypatch.setenv("MEDIA_TOOL_CAPCUT_ROOT", str(base))
    monkeypatch.setattr(install_mod, "is_running", lambda: [])
    snap = tmp_path / "data" / "models" / "whisper" / "models--Systran--faster-whisper-small" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "model.bin").write_bytes(b"x")
    st = capcut.status(tmp_path / "data")
    assert st["installed"] is True and st["version"] == "9.7.1.4203"
    assert st["drafts_dir"] == str(base / "User Data" / "Projects" / "com.lveditor.draft")
    assert st["running_pids"] == []
    assert set(st["deps"]) == {"faster_whisper", "uiautomation", "pywin32"}
    assert st["skill"]["installed"] is True and st["skill"]["commit"] == "bd8b88f912ba1e6c4c17776881559c7151fd63ba"
    assert st["models"] == {"small": True, "medium": False, "large-v3-turbo": False}
    monkeypatch.setenv("MEDIA_TOOL_CAPCUT_ROOT", str(tmp_path / "nada"))
    st = capcut.status(tmp_path / "data")
    assert st["installed"] is False and st["version"] is None
    with pytest.raises(CapCutNotInstalled):
        capcut.list_caption_drafts()


def test_package_import_does_not_load_optional_libs():
    code = ("import sys; import app.integrations.capcut as c; from app.integrations.capcut import pipeline; "
            "print(','.join(m for m in ('faster_whisper','uiautomation','win32gui','numpy','ctranslate2') "
            "if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(Path(__file__).resolve().parents[1]), check=True,
                         env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert out.stdout.strip() == ""


def test_skill_is_unmodified_upstream_copy():
    info = skill.info()
    assert info["installed"] is True
    assert (skill.SKILL_DIR / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")
    assert not (skill.SKILL_DIR / ".git").exists()
    mat = skill.native_text_material("ID-1", "Olá")
    assert mat["id"] == "ID-1" and mat["check_flag"] == 15 and mat["text"] == "Olá"
    assert skill.parse_time_us("1.5s") == 1_500_000 and skill.parse_time_us(2) == 2


# --------------------------------------------------------------------------------------------- globalSetting

ORIGINAL = ("[General]\r\nfoo=bar\r\naccessibleDisableUnignoredChildren=1\r\nx=\\\\caminho\\\\ç\r\n"
            "disable_qt_access=1\r\ndisable_qt_access_event=false\r\n")


def test_patch_global_setting_preserves_crlf_lines_and_backup_once(tmp_path):
    gs = tmp_path / "globalSetting"
    gs.write_bytes(ORIGINAL.encode("utf-8"))
    backup = tmp_path / "state" / "globalSetting.orig"
    assert ctl.patch_accessibility_flags(gs, backup) is True
    data = gs.read_bytes().decode("utf-8")
    assert data == ORIGINAL.replace("Children=1", "Children=0").replace("qt_access=1", "qt_access=0")
    assert "\n" not in data.replace("\r\n", "")
    assert backup.read_bytes() == ORIGINAL.encode("utf-8")
    # o CapCut regrava 1 ao subir; o próximo launch grava 0 de novo e o backup continua o original
    gs.write_bytes(ORIGINAL.replace("qt_access=1", "qt_access=1").encode("utf-8"))
    assert ctl.patch_accessibility_flags(gs, backup) is True
    assert backup.read_bytes() == ORIGINAL.encode("utf-8")
    assert ctl.patch_accessibility_flags(gs, backup) is False  # já está 0: nada muda


def test_patch_global_setting_adds_missing_flags_lf_and_clears_readonly(tmp_path):
    gs = tmp_path / "globalSetting"
    gs.write_bytes(b"[General]\nfoo=1\n")
    os.chmod(gs, stat.S_IREAD)
    try:
        ctl.patch_accessibility_flags(gs, tmp_path / "b.orig")
        text = gs.read_bytes().decode()
        assert text == "[General]\ndisable_qt_access=0\naccessibleDisableUnignoredChildren=0\nfoo=1\n"
        assert os.access(gs, os.W_OK)
    finally:
        os.chmod(gs, stat.S_IWRITE | stat.S_IREAD)


def test_controller_prepare_refuses_when_capcut_open(tmp_path):
    base = fake_capcut(tmp_path)
    inst = install_mod.detect({"MEDIA_TOOL_CAPCUT_ROOT": str(base)})
    before = inst.global_setting_path.read_bytes()
    c = ctl.CapCutController(inst, state_dir=tmp_path / "state", running=lambda: [99])
    with pytest.raises(CapCutBusy):
        c.prepare_accessibility()
    with pytest.raises(CapCutBusy):
        c.launch()
    assert inst.global_setting_path.read_bytes() == before
    c = ctl.CapCutController(inst, state_dir=tmp_path / "state", running=lambda: [])
    c.prepare_accessibility()
    assert b"disable_qt_access=0\r\n" in inst.global_setting_path.read_bytes()
    assert (tmp_path / "state" / "globalSetting.orig").read_bytes() == before
    c.shutdown()  # não abrimos nada: não faz nada
    with pytest.raises(CapCutNotInstalled):
        ctl.CapCutController(None, state_dir=tmp_path)


# --------------------------------------------------------------------------------------------- opções de exportação

@pytest.mark.parametrize(("w", "h", "label"), [(1080, 1920, "1080P"), (720, 1280, "720P"), (320, 240, "480P"),
                                              (1440, 2560, "2K"), (2160, 3840, "4K"), (1000, 1000, "720P"),
                                              (4320, 7680, "4K")])
def test_original_resolution_label(w, h, label):
    assert ctl.ExportOptions(source_width=w, source_height=h).resolution_label() == label


def test_fixed_options_and_fps_mapping():
    o = ctl.ExportOptions(resolution="2K", bitrate="high", fps="25", source_fps=59.94)
    assert (o.resolution_label(), o.bitrate_label(), o.fps_label()) == ("2K", "Superior", "25fps")
    assert ctl.ExportOptions(bitrate="low").bitrate_label() == "Abaixar"
    assert ctl.ExportOptions(source_fps=29.97).fps_label() == "29.97fps"
    assert ctl.ExportOptions(source_fps=30.0).fps_label() == "30fps"
    assert ctl.ExportOptions(source_fps=59.94).fps_label() == "59.94fps"
    assert ctl.ExportOptions(source_fps=23.976).fps_label() == "24fps"
    assert ctl.ExportOptions(source_fps=0).fps_label() == "30fps"
    assert ctl.ExportOptions(source_width=1080, source_height=1920).expected_short_side() == 1080


# --------------------------------------------------------------------------------------------- correções da verificação

def test_patch_global_setting_with_utf8_bom_keeps_bom_and_single_general(tmp_path):
    import codecs

    gs = tmp_path / "globalSetting"
    gs.write_bytes(codecs.BOM_UTF8 + ORIGINAL.encode("utf-8"))
    assert ctl.patch_accessibility_flags(gs, tmp_path / "b.orig") is True
    data = gs.read_bytes()
    assert data.startswith(codecs.BOM_UTF8) and not data[3:].startswith(codecs.BOM_UTF8)
    text = data[3:].decode("utf-8")
    assert text.count("[General]") == 1
    assert "disable_qt_access=0" in text and "accessibleDisableUnignoredChildren=0" in text
    gs.write_bytes(codecs.BOM_UTF8 + b"[General]\r\nfoo=1\r\n")  # flags ausentes: entram logo depois do [General]
    ctl.patch_accessibility_flags(gs, tmp_path / "b.orig")
    assert gs.read_bytes() == codecs.BOM_UTF8 + (
        b"[General]\r\ndisable_qt_access=0\r\naccessibleDisableUnignoredChildren=0\r\nfoo=1\r\n")


@pytest.mark.skipif(os.name != "nt", reason="tasklist só no Windows")
@pytest.mark.parametrize("mode", ["oserror", "timeout", "returncode"])
def test_is_running_query_failure_is_transient_error_not_closed(monkeypatch, mode):
    from app.integrations.capcut.errors import CapCutError

    def fake_run(*_a, **_k):
        if mode == "oserror":
            raise OSError("sem tasklist")
        if mode == "timeout":
            raise subprocess.TimeoutExpired(["tasklist"], 15)
        return subprocess.CompletedProcess(["tasklist"], 1, stdout="", stderr="erro")

    monkeypatch.setattr(install_mod.subprocess, "run", fake_run)
    with pytest.raises(CapCutError) as exc:
        install_mod.is_running()
    assert exc.value.code == "process_query_failed" and exc.value.transient is True


P = install_mod.ProcessInfo


def test_track_tree_follows_children_and_grandchildren_after_launcher_exits():
    root = {100: 1000}
    table = [P(4, 0, "explorer.exe", 10), P(200, 100, "CapCut.exe", 1001), P(300, 200, "VEDetector.exe", 1002),
             P(400, 4, "CapCut.exe", 1003)]  # 400: CapCut aberto pelo dono por fora
    tree = install_mod.track_tree(table, root)
    assert set(tree) == {100, 200, 300}
    assert {p.pid for p in install_mod.alive_members(table, tree)} == {200, 300}  # lançador já saiu


def test_track_tree_ignores_reused_pids():
    root = {100: 1000}
    table = [P(100, 4, "outro.exe", 2000),        # o PID do lançador foi reaproveitado por outro processo
             P(600, 100, "CapCut.exe", 2001),     # filho desse outro processo: não é nosso
             P(700, 100, "CapCut.exe", 900),      # criado antes do nosso lançador: não é nosso
             P(200, 100, "CapCut.exe", 1500)]     # filho do nosso lançador antes de o PID ser reaproveitado
    tree = install_mod.track_tree(table, root)
    assert set(tree) == {100, 200}
    assert {p.pid for p in install_mod.alive_members(table, tree)} == {200}, "o 100 de agora é outro processo"


def test_process_table_reads_this_process():
    table = install_mod.process_table()
    if os.name != "nt":
        assert table == []
        return
    me = next(p for p in table if p.pid == os.getpid())
    assert me.ppid == os.getppid() and me.created and me.name.lower().startswith("python")


class FakeLibs:
    """win32gui/win32process/uiautomation falsos: janelas por PID e mensagens enviadas."""

    def __init__(self, windows):
        self.windows = windows  # {hwnd: pid}
        self.posted: list[tuple[int, int]] = []
        libs = self

        class Gui:
            @staticmethod
            def EnumWindows(cb, arg):
                for h in list(libs.windows):
                    cb(h, arg)

            IsWindowVisible = staticmethod(lambda h: True)
            GetWindowText = staticmethod(lambda h: f"janela {h}")
            PostMessage = staticmethod(lambda h, msg, *_a: libs.posted.append((h, msg)))

        class Proc:
            GetWindowThreadProcessId = staticmethod(lambda h: (1, libs.windows[h]))

        class Auto:
            @staticmethod
            def ControlFromHandle(h):
                return type("C", (), {"ClassName": "HomePageX_QMLTYPE_1"})()

        self.win32gui, self.win32process, self.auto = Gui, Proc, Auto


def test_shutdown_only_closes_our_process_tree_and_never_kills(tmp_path, monkeypatch):
    base = fake_capcut(tmp_path)
    inst = install_mod.detect({"MEDIA_TOOL_CAPCUT_ROOT": str(base)})
    table = [P(4, 0, "explorer.exe", 10), P(100, 4, "CapCut.exe", 1000)]
    killed: list[list[str]] = []

    class FakePopen:
        pid = 100

        def __init__(self, *_a, **_k):
            pass

    monkeypatch.setattr(ctl.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(ctl.subprocess, "run", lambda args, **_k: killed.append(args))
    c = ctl.CapCutController(inst, state_dir=tmp_path / "state", running=lambda: [], processes=lambda: list(table))
    assert c.launch() == 100
    # o lançador abre o executável versionado e sai; depois o nosso cai e o DONO abre outro CapCut
    table[:] = [P(4, 0, "explorer.exe", 10), P(200, 100, "CapCut.exe", 1001), P(300, 200, "VEDetector.exe", 1002)]
    assert c._owned(capcut_only=True) == {200}
    table[:] = [P(4, 0, "explorer.exe", 10), P(200, 100, "CapCut.exe", 1001), P(300, 200, "VEDetector.exe", 1002),
                P(400, 4, "CapCut.exe", 5000)]
    c._libs = FakeLibs({11: 200, 22: 400})
    c.shutdown(timeout=0)
    assert c._libs.posted == [(11, ctl.WM_CLOSE)], "WM_CLOSE só na janela do nosso processo"
    assert killed == [], "nunca força o fechamento (sem taskkill)"
    assert any("ficou aberto" in w and "não foi forçado" in w for w in c.warnings)
    # o nosso já saiu e só o do dono está aberto: nada a fechar
    killed.clear()
    c._launched = True
    table[:] = [P(4, 0, "explorer.exe", 10), P(400, 4, "CapCut.exe", 5000)]
    c._libs = FakeLibs({22: 400})
    c.shutdown(timeout=0)
    assert c._libs.posted == [] and killed == []


def test_user_input_since_injection_and_desktop_free(tmp_path, monkeypatch):
    """Entrada do dono depois da nossa última entrada sintética = PC em uso (espera); só a nossa = não."""
    base = fake_capcut(tmp_path)
    inst = install_mod.detect({"MEDIA_TOOL_CAPCUT_ROOT": str(base)})
    c = ctl.CapCutController(inst, state_dir=tmp_path / "state", running=lambda: [], processes=lambda: [])
    now = {"tick": 1_000_000, "last": 1_000_000}
    monkeypatch.setattr(ctl, "_tick_count", lambda: now["tick"])
    monkeypatch.setattr(ctl, "_last_input_tick", lambda: now["last"])
    c._mark_injected()
    now["last"] = now["tick"] + 50  # a própria entrada sintética registrada pelo Windows
    assert c.user_input_since_injection() is False
    assert isinstance(c._lost("x"), ctl.ForegroundLost)
    now["last"] = now["tick"] + 2_000  # o dono mexeu depois
    assert c.user_input_since_injection() is True
    assert isinstance(c._lost("x"), ctl.DesktopBusy)
    # antes de abrir: ocioso há 5 s -> espera; há 20 s -> livre
    now["tick"], now["last"] = 2_000_000, 2_000_000 - 5_000
    with pytest.raises(ctl.DesktopBusy):
        c.ensure_desktop_free()
    now["last"] = 2_000_000 - 20_000
    c.ensure_desktop_free()
    # GetTickCount dá a volta: continua correto
    c._last_injected_tick = 0xFFFFFF00
    now["last"] = 0x00000100  # 512 ms depois, já do outro lado da volta
    assert c.user_input_since_injection() is True


def test_click_until_accepts_window_that_opened_late(tmp_path):
    """Janela de exportação abre depois do prazo e, modal, faz o 2º clique ser recusado: segue sem erro."""
    base = fake_capcut(tmp_path)
    inst = install_mod.detect({"MEDIA_TOOL_CAPCUT_ROOT": str(base)})
    c = ctl.CapCutController(inst, state_dir=tmp_path / "state", running=lambda: [], processes=lambda: [])
    clicks: list[int] = []
    state = {"open": False}

    def click(_ctrl, _hwnd):
        clicks.append(1)
        if len(clicks) == 1:
            return  # 1º clique "não fez nada" no prazo...
        state["open"] = True  # ...mas a janela abriu atrasada e cobre o botão: clique recusado
        raise ctl.ForegroundLost("Outra janela (Qt622QWindowIcon) está sobre o CapCut")

    c._safe_click = click
    assert c._click_until(object(), 1, lambda: 77 if state["open"] else None, "abrir a exportação", per_try=0.3) == 77
    assert len(clicks) == 2
    # se nada abriu de verdade, o erro do clique recusado continua subindo
    state["open"] = False
    clicks.clear()
    c._safe_click = lambda *_a: (_ for _ in ()).throw(ctl.ForegroundLost("x"))
    with pytest.raises(ctl.ForegroundLost):
        c._click_until(object(), 1, lambda: None, "abrir a exportação", per_try=0.2)


def _prompt_controller(tmp_path, monkeypatch, *, task_seconds):
    """Controlador com CapCut 'aberto' que mostra 'Confirmar saída? Uma tarefa está em andamento' enquanto a tarefa
    dura `task_seconds` (relógio falso). Registra cancelamentos e pedidos de fechamento; nada de confirmar."""
    base = fake_capcut(tmp_path)
    inst = install_mod.detect({"MEDIA_TOOL_CAPCUT_ROOT": str(base)})
    c = ctl.CapCutController(inst, state_dir=tmp_path / "state", running=lambda: [], processes=lambda: [])
    clock = {"t": 0.0}
    monkeypatch.setattr(ctl.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(ctl.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(c, "_sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(ctl, "EXIT_RETRY_SECONDS", 5.0)
    c.task_timeout = 60.0
    state = {"open": True, "prompt": False, "closes": 0, "cancels": 0}

    def post_close():
        state["closes"] += 1
        if clock["t"] < task_seconds:
            state["prompt"] = True  # tarefa em andamento: o CapCut pergunta
        else:
            state["open"] = False  # tarefa acabou: fecha normalmente

    def cancel(_hwnd, _root):
        state["cancels"] += 1
        state["prompt"] = False
        return True

    monkeypatch.setattr(c, "_exit_prompt",
                        lambda: (7, "Confirmar saída?", "Uma tarefa está em andamento. Deseja sair?", None)
                        if state["prompt"] else None)
    monkeypatch.setattr(c, "_cancel_prompt", cancel)
    return c, state, post_close, clock


def test_exit_prompt_is_cancelled_and_close_retried_after_task_ends(tmp_path, monkeypatch):
    c, state, post_close, clock = _prompt_controller(tmp_path, monkeypatch, task_seconds=20)
    ok = c._close_gracefully(post_close, lambda: not state["open"], "fechar o CapCut", timeout=40)
    assert ok is True and not state["open"]
    assert state["cancels"] >= 1, "o aviso foi respondido com CANCELAR (nunca confirmar a saída)"
    assert state["closes"] >= 2, "pediu para fechar de novo depois que a tarefa terminou"
    assert clock["t"] >= 20, "esperou a tarefa terminar"
    assert "Uma tarefa está em andamento" in c.exit_prompt_seen


def test_close_is_repeated_without_prompt_until_capcut_exits(tmp_path, monkeypatch):
    """Sem aviso: o 1º WM_CLOSE só leva do editor à tela inicial; o pedido é repetido até o CapCut sair."""
    c, state, _post_close, clock = _prompt_controller(tmp_path, monkeypatch, task_seconds=0)
    monkeypatch.setattr(ctl, "CLOSE_REPEAT_SECONDS", 5.0)
    calls = {"n": 0}

    def post_close():
        calls["n"] += 1
        if calls["n"] >= 2:
            state["open"] = False  # 1º pedido: editor -> tela inicial; 2º: sai

    ok = c._close_gracefully(post_close, lambda: not state["open"], "fechar o CapCut", timeout=40)
    assert ok is True and calls["n"] == 2 and clock["t"] < 40


def test_exit_prompt_task_never_ends_leaves_capcut_open_without_forcing(tmp_path, monkeypatch):
    killed: list = []
    monkeypatch.setattr(ctl.subprocess, "run", lambda args, **_k: killed.append(args))
    c, state, post_close, clock = _prompt_controller(tmp_path, monkeypatch, task_seconds=10_000)
    ok = c._close_gracefully(post_close, lambda: not state["open"], "fechar o CapCut", timeout=40)
    assert ok is False and state["open"], "tarefa não terminou no prazo: CapCut continua aberto"
    assert 60 <= clock["t"] < 70, "respeita o prazo da tarefa, não o prazo curto do fechamento"
    assert killed == []
