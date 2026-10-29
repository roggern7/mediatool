"""Regras puras da exportação para pasta: nomes de arquivo, validação do destino e tokens de destino."""

from __future__ import annotations

import os
import unicodedata
from datetime import datetime

import pytest

from app.services import export_destination as ed
from app.services.export_destination import DestinationError, DestinationTokens, validate_destination
from app.services.export_names import (
    MAX_NAME_LENGTH,
    export_extension,
    export_stem,
    file_name,
    rendition_tag,
    safe_stem,
    sanitize_component,
)
from conftest import make_settings

INVALID = set('<>:"/\\|?*')
# Capturada na importação do módulo: o conftest troca `pick_folder_native` por uma função que falha em todo teste.
REAL_PICKER = ed.pick_folder_native
REAL_RUN_PICKER_CHILD = ed._run_picker_child


def media(**fields) -> dict:
    base = {"id": 7, "source_profile": "perfil", "source_post_id": "ABC123", "source_media_index": 0,
            "taken_at": "2026-03-04T23:59:59Z", "original_sha256": "f" * 64}
    return {**base, **fields}


def test_friendly_name_with_profile_date_and_code():
    assert export_stem(media()) == "perfil_2026-03-04_ABC123"
    assert file_name(export_stem(media()), ".mp4") == "perfil_2026-03-04_ABC123.mp4"


def test_carousel_items_are_numbered_from_one():
    assert export_stem(media(), carousel=True) == "perfil_2026-03-04_ABC123_1"
    assert export_stem(media(source_media_index=2), carousel=True) == "perfil_2026-03-04_ABC123_3"
    # Item que não é o primeiro sempre leva o número, mesmo sem a marca de carrossel.
    assert export_stem(media(source_media_index=1)) == "perfil_2026-03-04_ABC123_2"


def test_fallbacks_when_data_is_missing():
    assert export_stem(media(taken_at=None)) == "perfil_ABC123"
    assert export_stem(media(taken_at="lixo")) == "perfil_ABC123"
    assert export_stem(media(source_post_id=None)) == "perfil_2026-03-04_7"
    assert export_stem(media(source_profile=None)) == "midia_2026-03-04_ABC123"
    assert export_stem(media(source_profile=None, source_post_id=None, taken_at=None)) == "midia_ffffffffffff"


def test_invalid_characters_and_control_characters_are_replaced():
    name = sanitize_component('a<b>c:d"e/f\\g|h?i*j\x00k\x1fl\u202em')
    assert not INVALID & set(name)
    assert all(not unicodedata.category(char).startswith("C") for char in name)
    assert name == "a_b_c_d_e_f_g_h_i_j_k_l_m"
    stem = export_stem(media(source_profile="../../etc", source_post_id="..\\..\\x"))
    assert "/" not in stem and "\\" not in stem and not stem.startswith(".")


def test_no_trailing_dot_or_space_and_never_empty():
    assert sanitize_component("  nome. . ") == "nome"
    assert safe_stem(" . ") == "midia"
    assert file_name("nome", ".jpg") == "nome.jpg"


@pytest.mark.parametrize("reserved", ["CON", "prn", "Aux", "NUL", "COM1", "com9", "LPT1", "lpt9", "NUL.txt",
                                      "con.tar.gz", "COM1 "])
def test_windows_reserved_names_are_never_produced(reserved):
    stem = safe_stem(reserved)
    assert stem.split(".", 1)[0].strip().upper() != reserved.split(".", 1)[0].strip().upper()
    assert stem.startswith("_")


def test_unicode_is_normalized_to_nfc():
    decomposed = "cafe\u0301_日本"
    stem = export_stem(media(source_profile=decomposed, source_post_id="CÓDIGO"))
    assert stem == unicodedata.normalize("NFC", stem)
    assert stem.startswith("café_日本_2026-03-04_CÓDIGO")


def test_long_names_are_cut_keeping_extension_and_collision_suffix():
    stem = export_stem(media(source_profile="p" * 300, source_post_id="c" * 300))
    first = file_name(stem, ".mp4")
    second = file_name(stem, ".mp4", 2)
    many = file_name(stem, ".mp4", 137)
    assert len(first) == MAX_NAME_LENGTH and first.endswith(".mp4")
    assert len(second) == MAX_NAME_LENGTH and second.endswith(" (2).mp4")
    assert len(many) == MAX_NAME_LENGTH and many.endswith(" (137).mp4")
    assert len({first, second, many}) == 3


def test_extension_comes_from_the_stored_original():
    assert export_extension("originals/2026/01/ab/abc.MP4") == ".mp4"
    assert export_extension("originals/2026/01/ab/abc.jpg") == ".jpg"
    assert export_extension("originals/2026/01/ab/abc") == ""
    assert export_extension("originals/x.muito-longa") == ""
    assert export_extension(None) == ""


# --------------------------------------------------------------------------- destino


@pytest.fixture
def settings(tmp_path):
    root = tmp_path / "app"
    for name in ("storage/originals", "storage/processed", "storage/temp", "storage/thumbs", "data/secrets",
                 "data/backups", "logs", "backend"):
        (root / name).mkdir(parents=True)
    return make_settings(root)


def code_of(raw, settings, **kwargs) -> str:
    with pytest.raises(DestinationError) as info:
        validate_destination(raw, settings, **kwargs)
    assert info.value.message
    return info.value.code


def test_valid_destination_is_resolved(tmp_path, settings):
    dest = tmp_path / "Minha pasta"
    dest.mkdir()
    assert validate_destination(str(dest), settings) == dest.resolve()
    assert list(dest.iterdir()) == []   # a checagem de escrita não deixa nada para trás


def test_destination_inside_managed_folders_is_refused(tmp_path, settings):
    root = settings.root
    for inside in (root, root / "storage", root / "storage" / "originals", root / "storage" / "processed",
                   root / "storage" / "temp", root / "storage" / "thumbs", root / "data", root / "data" / "secrets",
                   root / "data" / "backups", root / "logs", root / "backend"):
        assert code_of(str(inside), settings) == "export_dest_forbidden", inside
    assert code_of(str(tmp_path), settings) == "export_dest_forbidden"   # pai do storage/projeto


def test_managed_folders_configured_outside_the_project_are_refused_too(tmp_path, settings):
    from dataclasses import replace

    elsewhere = tmp_path / "outro-disco"
    for name in ("midias/originals", "banco", "registros"):
        (elsewhere / name).mkdir(parents=True)
    moved = replace(settings, storage_dir=elsewhere / "midias", data_dir=elsewhere / "banco",
                    secrets_dir=elsewhere / "banco" / "secrets", log_dir=elsewhere / "registros")
    for inside in (elsewhere / "midias" / "originals", elsewhere / "banco", elsewhere / "registros"):
        assert code_of(str(inside), moved) == "export_dest_forbidden", inside
    assert code_of(str(elsewhere), moved) == "export_dest_forbidden"   # pai do storage configurado


def test_system_folders_and_drive_root_are_refused(tmp_path, settings, monkeypatch):
    system = tmp_path / "Windows"
    (system / "System32").mkdir(parents=True)
    monkeypatch.setattr(ed, "system_roots", lambda env=None: [system])
    assert code_of(str(system), settings) == "export_dest_forbidden"
    assert code_of(str(system / "System32"), settings) == "export_dest_forbidden"
    assert code_of(tmp_path.anchor, settings) == "export_dest_drive_root"


def test_system_roots_come_from_the_environment():
    roots = ed.system_roots({"SystemRoot": r"C:\Windows", "ProgramFiles": r"C:\Program Files",
                             "ProgramFiles(x86)": r"C:\Program Files (x86)", "ProgramData": r"C:\ProgramData",
                             "OUTRA": r"C:\Qualquer", "windir": "relativo"})
    assert {str(root) for root in roots} == {r"C:\Windows", r"C:\Program Files", r"C:\Program Files (x86)",
                                             r"C:\ProgramData"}


@pytest.mark.skipif(os.name != "nt", reason="pastas reais do Windows")
def test_real_windows_folder_is_refused_without_writing(settings):
    assert code_of(os.environ["SystemRoot"], settings) == "export_dest_forbidden"


def test_relative_traversal_missing_and_file_destinations(tmp_path, settings):
    dest = tmp_path / "saida"
    dest.mkdir()
    (tmp_path / "arquivo.txt").write_text("x", encoding="utf-8")
    assert code_of("saida", settings) == "export_dest_invalid"
    assert code_of("", settings) == "export_dest_invalid"
    assert code_of(str(dest / ".." / "app" / "storage"), settings) == "export_dest_invalid"
    assert code_of(str(tmp_path / "nao-existe"), settings) == "export_dest_missing"
    assert code_of(str(tmp_path / "arquivo.txt"), settings) == "export_dest_missing"


def test_link_pointing_into_the_storage_is_refused(tmp_path, settings):
    link = tmp_path / "atalho"
    target = settings.root / "storage" / "originals"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)
    assert code_of(str(link), settings) == "export_dest_forbidden"
    inner = target / "2026"
    inner.mkdir()
    assert code_of(str(link / "2026"), settings) == "export_dest_forbidden"


def test_folder_without_write_permission(tmp_path, settings, monkeypatch):
    dest = tmp_path / "somente-leitura"
    dest.mkdir()

    def denied(*args, **kwargs):
        raise PermissionError(13, "acesso negado")

    monkeypatch.setattr(ed.tempfile, "TemporaryFile", denied)
    assert code_of(str(dest), settings) == "export_dest_not_writable"
    assert validate_destination(str(dest), settings, check_writable=False) == dest.resolve()


def test_default_folder_is_created_by_the_server_with_suffixes(tmp_path):
    documents = tmp_path / "Documentos"
    moment = datetime(2026, 10, 6, 14, 5)
    first = ed.create_default_folder(documents, now=moment)
    second = ed.create_default_folder(documents, now=moment)
    third = ed.create_default_folder(documents, now=moment)
    base = documents / "Media Tool" / "Exports"
    assert first == base / "Importacao-2026-10-06-1405"
    assert second == base / "Importacao-2026-10-06-1405-2"
    assert third == base / "Importacao-2026-10-06-1405-3"
    assert all(path.is_dir() for path in (first, second, third))


def test_tokens_are_random_single_use_and_expire(tmp_path):
    now = [1000.0]
    tokens = DestinationTokens(ttl=600, clock=lambda: now[0])
    first, second = tokens.issue(tmp_path), tokens.issue(tmp_path)
    assert first != second and len(first) >= 32
    assert tokens.consume(first) == tmp_path
    with pytest.raises(DestinationError) as reused:
        tokens.consume(first)
    assert reused.value.code == "export_token_invalid" and reused.value.status == 409
    now[0] += 601
    with pytest.raises(DestinationError):
        tokens.consume(second)
    with pytest.raises(DestinationError):
        tokens.consume("token-que-nunca-existiu")


def test_token_registry_is_bounded(tmp_path):
    tokens = DestinationTokens(max_entries=3)
    issued = [tokens.issue(tmp_path / str(i)) for i in range(5)]
    with pytest.raises(DestinationError):
        tokens.consume(issued[0])
    assert tokens.consume(issued[4]) == tmp_path / "4"


# --------------------------------------------------------------------------- seletor nativo (processos filhos FALSOS)


class FakePickerProcess:
    """Substitui subprocess.Popen: nenhuma janela é aberta. `outputs[método]` = bytes do stdout, "timeout" ou
    OSError (o processo nem inicia). Método: "tk" (python -m ...) ou "ps" (powershell.exe)."""

    calls: list[tuple[str, list, dict]] = []
    sent: dict[str, bytes] = {}
    outputs: dict[str, object] = {}

    def __init__(self, args, **kwargs):
        self.method = "ps" if args[0].lower().endswith("powershell.exe") else "tk"
        type(self).calls.append((self.method, args, kwargs))
        self.killed = False
        if isinstance(type(self).outputs.get(self.method), OSError):
            raise type(self).outputs[self.method]

    def communicate(self, data=None, timeout=None):
        if data is not None:
            type(self).sent[self.method] = data
            type(self).timeout_used = timeout
        output = type(self).outputs.get(self.method, b"")
        if output == "timeout":
            if not self.killed:
                raise ed.subprocess.TimeoutExpired("picker", timeout)
            return b"", b""
        return output, b""

    def kill(self):
        self.killed = True


def ps_ok(path: str) -> bytes:
    import base64

    return b"OK:" + base64.b64encode(path.encode("utf-8"))


@pytest.fixture
def picker(monkeypatch):
    FakePickerProcess.calls = []
    FakePickerProcess.sent = {}
    FakePickerProcess.outputs = {"tk": b'{"path": null}', "ps": b"CANCEL"}
    monkeypatch.setattr(ed.subprocess, "Popen", FakePickerProcess)
    monkeypatch.setattr(ed, "_run_picker_child", REAL_RUN_PICKER_CHILD)   # função real sobre o Popen FALSO
    monkeypatch.setattr(ed.os, "name", "nt")
    return FakePickerProcess


def test_tk_child_is_started_without_shell_with_fixed_arguments(picker, tmp_path, monkeypatch):
    import json
    import sys

    monkeypatch.setenv("INSTAGRAM_SESSIONID", "FAKE-SESSION-0000000000")
    monkeypatch.setenv("API_TOKEN", "x")
    chosen = tmp_path / "Minha pasta çãõ 日本"
    picker.outputs["tk"] = json.dumps({"path": chosen.as_posix()}).encode("ascii")   # como o filho real responde
    initial = tmp_path / "inicial; & calc.exe é"
    assert REAL_PICKER(initial) == chosen        # acentos, Unicode e espaços chegam íntegros
    assert [call[0] for call in picker.calls] == ["tk"]   # funcionou: o segundo método nem é tentado
    _, args, kwargs = picker.calls[0]
    assert args == [sys.executable, "-m", "app.services.folder_picker_child"]   # nada do pedido vai para argv
    assert kwargs["shell"] is False
    assert "INSTAGRAM_SESSIONID" not in kwargs["env"] and "API_TOKEN" not in kwargs["env"]
    assert "FAKE-SESSION-0000000000" not in kwargs["env"].values()
    assert json.loads(picker.sent["tk"]) == {"initial": str(initial)}   # diretório inicial só pelo stdin
    assert picker.sent["tk"].isascii()
    assert picker.timeout_used == ed.PICKER_TIMEOUT_SECONDS >= 300


def test_cancel_and_timeout_are_not_failures(picker):
    assert REAL_PICKER(None) is None                      # usuário cancelou o diálogo
    picker.outputs["tk"] = "timeout"
    assert REAL_PICKER(None) is None                      # prazo estourado: filho encerrado, tratado como cancelado
    assert [call[0] for call in picker.calls] == ["tk", "tk"]   # cancelar não cai para o segundo método


@pytest.mark.parametrize("broken", [b'{"error": "TclError"}', b'{"error": "ModuleNotFoundError"}', b"lixo", b"",
                                    OSError("sem python")])
def test_powershell_is_used_when_tk_does_not_work(picker, tmp_path, broken):
    chosen = tmp_path / "Pasta do usuário ñ 日本"
    initial = tmp_path / "inicial"
    initial.mkdir()
    picker.outputs.update(tk=broken, ps=ps_ok(str(chosen)))
    assert REAL_PICKER(initial) == chosen
    assert [call[0] for call in picker.calls] == ["tk", "ps"]
    _, args, kwargs = picker.calls[1]
    assert args[1:5] == ["-NoProfile", "-NonInteractive", "-STA", "-Command"]
    assert args[5] == ed.POWERSHELL_PICKER_SCRIPT and len(args) == 6   # script fixo: nenhum dado no comando
    assert str(initial) not in " ".join(args) and str(tmp_path) not in ed.POWERSHELL_PICKER_SCRIPT
    assert kwargs["shell"] is False
    assert kwargs["env"][ed.PICKER_INITIAL_ENV] == str(initial)   # diretório inicial validado, por variável do filho
    assert "System.Windows.Forms.FolderBrowserDialog" in ed.POWERSHELL_PICKER_SCRIPT
    assert "ToBase64String" in ed.POWERSHELL_PICKER_SCRIPT       # saída independente do codepage


def test_powershell_initial_dir_is_only_passed_when_valid(picker, tmp_path, monkeypatch):
    monkeypatch.setenv(ed.PICKER_INITIAL_ENV, "valor herdado do ambiente")
    picker.outputs.update(tk=b"", ps=b"CANCEL")
    for initial in (None, tmp_path / "nao-existe", type(tmp_path)("relativa")):
        assert REAL_PICKER(initial) is None
        assert ed.PICKER_INITIAL_ENV not in picker.calls[-1][2]["env"]


@pytest.mark.parametrize("ps_output", [b"", b"erro qualquer", b"OK:###", b"OK:", OSError("sem powershell")])
def test_both_methods_failing_is_picker_unavailable(picker, ps_output):
    picker.outputs.update(tk=b'{"error": "TclError"}', ps=ps_output)
    with pytest.raises(DestinationError) as info:
        REAL_PICKER(None)
    assert info.value.code == "picker_unavailable" and info.value.status == 501
    assert "Usar pasta padrão" in info.value.message
    assert [call[0] for call in picker.calls] == ["tk", "ps"]


def test_picker_is_unavailable_outside_windows(monkeypatch):
    monkeypatch.setattr(ed.os, "name", "posix")
    with pytest.raises(DestinationError) as info:
        REAL_PICKER(None)
    assert info.value.code == "picker_unavailable" and info.value.status == 501


def test_tk_child_answers_in_ascii_json_whatever_the_console_codepage(monkeypatch, capsysbinary):
    """O módulo filho real, com o tkinter trocado por um falso (nenhuma janela): caminho com acentos sai íntegro."""
    import io
    import json
    import sys
    import types

    from app.services import folder_picker_child as child

    seen = {}

    class FakeRoot:
        def __getattr__(self, name):
            return lambda *args, **kwargs: seen.setdefault("calls", []).append(name)

    def askdirectory(**kwargs):
        seen["dialog"] = kwargs
        seen["before"] = list(seen["calls"])
        return "C:/Users/José/Área de Trabalho/日本 fotos"

    fake_tk = types.ModuleType("tkinter")
    fake_tk.Tk = FakeRoot
    fake_dialog = types.ModuleType("tkinter.filedialog")
    fake_dialog.askdirectory = askdirectory
    fake_tk.filedialog = fake_dialog
    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", fake_dialog)
    request = json.dumps({"initial": "D:\\Início é"}).encode("ascii") + b"\n"
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(request)))

    assert child.main() == 0
    output = capsysbinary.readouterr().out
    assert output.isascii() and json.loads(output.decode("utf-8")) == {
        "path": "C:/Users/José/Área de Trabalho/日本 fotos"}
    # Janela dona no topo e com foco ANTES do diálogo; diálogo preso a ela, só pastas existentes.
    assert seen["before"] == ["withdraw", "attributes", "lift", "focus_force", "update"]
    assert seen["dialog"]["mustexist"] is True and seen["dialog"]["initialdir"] == "D:\\Início é"
    assert seen["dialog"]["title"] == "Media Tool: escolher pasta de exportação"
    assert isinstance(seen["dialog"]["parent"], FakeRoot)

    def broken():
        raise RuntimeError("Tcl não inicializou")

    fake_tk.Tk = broken
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"lixo\n")))
    assert child.main() == 1
    assert json.loads(capsysbinary.readouterr().out) == {"error": "RuntimeError"}


# --------------------------------------------------------------------------- pasta gerenciada por caminho alternativo


def test_long_path_prefix_to_managed_folders_is_refused(tmp_path, settings):
    root = settings.root
    for inside in (root / "storage" / "originals", root / "data", root, root / "backend"):
        assert code_of("\\\\?\\" + str(inside), settings) == "export_dest_forbidden", inside
    assert code_of("\\\\?\\" + str(tmp_path), settings) == "export_dest_forbidden"   # pai, com prefixo
    assert code_of("\\\\.\\" + str(root / "storage"), settings) == "export_dest_invalid"
    dest = tmp_path / "Minha pasta"
    dest.mkdir()
    if os.name == "nt":
        assert validate_destination("\\\\?\\" + str(dest), settings) == dest.resolve()   # devolvido sem o prefixo


@pytest.mark.skipif(os.name != "nt", reason="compartilhamento administrativo do Windows")
def test_administrative_unc_path_to_managed_folders_is_refused(tmp_path, settings):
    def unc(path):
        resolved = path.resolve()
        return "\\\\localhost\\" + resolved.drive[0] + "$" + str(resolved)[2:]

    if not os.path.isdir(unc(tmp_path)):
        pytest.skip("compartilhamento administrativo (\\\\localhost\\<letra>$) inacessível neste ambiente")
    root = settings.root
    for inside in (root / "storage" / "originals", root / "storage", root / "data" / "secrets", root / "logs", root,
                   root / "backend"):
        assert code_of(unc(inside), settings) == "export_dest_forbidden", inside
    assert code_of(unc(tmp_path), settings) == "export_dest_forbidden"   # pai das pastas gerenciadas, via UNC
    assert code_of("\\\\?\\UNC\\" + unc(root / "storage")[2:], settings) == "export_dest_forbidden"
    dest = tmp_path / "saida"
    dest.mkdir()
    accepted = validate_destination(unc(dest), settings)   # pasta do usuário por UNC continua aceita
    assert os.path.samefile(accepted, dest)


def test_alias_with_the_same_file_identity_is_refused(tmp_path, settings, monkeypatch):
    """Caminho alternativo simulado: o texto não tem relação com o storage, mas a identidade do arquivo é a mesma
    (como numa unidade mapeada para um compartilhamento desta máquina)."""
    root = settings.root
    alias_inside = tmp_path / "Z_unidade_mapeada"
    (alias_inside / "sub").mkdir(parents=True)
    alias_parent = tmp_path / "Y_unidade_mapeada"
    alias_parent.mkdir()
    system = tmp_path / "Windows"
    system.mkdir()
    alias_system = tmp_path / "X_unidade_mapeada"
    alias_system.mkdir()
    monkeypatch.setattr(ed, "system_roots", lambda env=None: [system])
    real = ed.file_identity
    aliases = {alias_inside.resolve(): root / "storage", alias_parent.resolve(): tmp_path,
               alias_system.resolve(): system}

    def identity(path):
        target = aliases.get(type(tmp_path)(ed.strip_long_prefix(os.fspath(path))))
        return real(target if target is not None else path)

    monkeypatch.setattr(ed, "file_identity", identity)
    assert code_of(str(alias_inside), settings) == "export_dest_forbidden"            # É o storage
    assert code_of(str(alias_inside / "sub"), settings) == "export_dest_forbidden"    # um ancestral É o storage
    assert code_of(str(alias_parent), settings) == "export_dest_forbidden"            # É um ancestral do projeto
    assert code_of(str(alias_system), settings) == "export_dest_forbidden"            # É a pasta do sistema
    free = tmp_path / "livre"
    free.mkdir()
    assert validate_destination(str(free), settings) == free.resolve()


def test_file_identity_is_the_same_through_a_link(tmp_path, settings):
    target = settings.root / "storage" / "originals"
    link = tmp_path / "atalho-identidade"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)
    assert ed.file_identity(link) == ed.file_identity(target) is not None
    assert ed.file_identity(tmp_path / "nao-existe") is None
    assert ed.file_identity(tmp_path) != ed.file_identity(target)


def test_long_path_destination_and_too_long_code(tmp_path, settings):
    import shutil

    top = tmp_path / ("a" * 110)
    long_dest = top / ("b" * 110) / ("c" * 110)
    assert len(str(long_dest)) > 330
    os.makedirs(ed.io_path(long_dest))
    try:
        resolved = validate_destination(str(long_dest), settings)
        assert str(resolved) == str(long_dest.resolve()) or os.path.samefile(ed.io_path(resolved), ed.io_path(long_dest))
        assert os.listdir(ed.io_path(long_dest)) == []
        # Caminho longo que não pôde ser usado: código próprio, não "a pasta não existe".
        assert code_of(str(long_dest / ("d" * 60)), settings) == "export_dest_too_long"
    finally:
        shutil.rmtree(ed.io_path(top), ignore_errors=True)
    assert code_of(str(tmp_path / "curto-que-nao-existe"), settings) == "export_dest_missing"


# --------------------------------------------------------------------------- versões editadas


EDIT = {"metadata_cleanup": False, "trim_start": 0.0, "mirror": False, "speed": 1.0, "auto_adjust": False,
        "reencode": True, "max_duration": 0.0, "sharpen": False, "audio_sync_start": True}


@pytest.mark.parametrize("changes, expected", [
    ({"max_duration": 15.03, "speed": 1.5}, "_editado_15s_1.5x"),
    ({"max_duration": 12.53}, "_editado_12.5s"),
    ({"max_duration": 60.03, "speed": 1.25, "sharpen": True}, "_editado_60s_1.25x_nitido"),
    ({"sharpen": True}, "_editado_nitido"),
    ({}, "_editado"),   # só converter para MP4, sem outra mudança
])
def test_rendition_tag_for_edits(changes, expected):
    assert rendition_tag({"preset_id": None, "preset_name": "Edição", "params": {**EDIT, **changes}}) == expected


def test_rendition_tag_for_presets():
    assert rendition_tag({"preset_id": 2, "preset_name": "Anti-duplicidade", "params": {"speed": 1.05}}) == \
        "_Anti-duplicidade"
    assert rendition_tag({"preset_id": 3, "preset_name": None, "params": {}}) == "_processado"
    assert rendition_tag({"preset_id": 4, "preset_name": 'a/b:c*' + "x" * 80, "params": {}}) == (
        "_a_b_c_" + "x" * 33)
    for tag in (rendition_tag({"preset_id": 5, "preset_name": "Meu preset", "params": {}}),
                rendition_tag({"preset_id": None, "preset_name": "Edição", "params": {**EDIT, "speed": 1.5}})):
        assert len(tag) <= 40 and not set(tag) & INVALID


def test_file_name_with_tag_keeps_the_tag_and_without_tag_is_unchanged():
    stem = "p" * 200
    for attempt in (1, 2, 137):
        assert file_name(stem, ".mp4", attempt, tag="") == file_name(stem, ".mp4", attempt)
        tagged = file_name(stem, ".mp4", attempt, tag="_editado_15s_1.5x")
        suffix = "" if attempt == 1 else f" ({attempt})"
        assert len(tagged) == MAX_NAME_LENGTH and tagged.endswith(f"_editado_15s_1.5x{suffix}.mp4")
    assert file_name("nome", ".mp4", tag="_editado") == "nome_editado.mp4"
    assert file_name("nome", ".mp4", 2, tag="_editado") == "nome_editado (2).mp4"


def test_rendition_tag_of_a_deleted_preset_is_not_an_edit():
    # Preset excluído: preset_id vira NULL (ON DELETE SET NULL), mas o nome continua o do preset.
    assert rendition_tag({"preset_id": None, "preset_name": "Anti-duplicidade", "params": {"speed": 1.05}}) ==         "_Anti-duplicidade"
    assert rendition_tag({"preset_id": None, "preset_name": None, "params": {"sharpen": True}}) == "_processado"
    assert rendition_tag({"preset_id": 9, "preset_name": "Edição", "params": {"speed": 1.5}}) == "_Edição"


def test_local_upload_stem_is_the_uploaded_name():
    local = {"id": 3, "source_platform": "local", "source_profile": None, "source_post_id": None,
             "source_media_index": 0, "original_sha256": "f" * 64, "taken_at": "2026-03-04T10:00:00Z"}
    assert export_stem({**local, "original_filename": "clip.mp4"}) == "clip"
    assert export_stem({**local, "original_filename": "meu vídeo.final.MOV"}) == "meu vídeo.final"
    assert export_stem({**local, "original_filename": "CON.mp4"}) == "_CON"
    assert export_stem({**local, "original_filename": "a:b?.mp4"}) == "a_b_"
    # Sem nome enviado: o nome de sempre; mídia do Instagram nunca usa o nome do arquivo.
    assert export_stem({**local, "original_filename": None}) == "midia_2026-03-04_ffffffffffff"
    assert export_stem(media(original_filename="clip.mp4")) == export_stem(media())
