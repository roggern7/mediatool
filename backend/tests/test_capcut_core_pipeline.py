"""Núcleo CapCut: sequência completa (process_video) com controlador e transcritor falsos.

O "CapCut" falso confere que o rascunho está registrado e válido quando é aberto e "exporta" recodificando o vídeo
com outro áudio na pasta padrão de vídeos da instalação falsa.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

import app.integrations.capcut as capcut
from app.integrations.capcut import draft as draft_mod
from app.integrations.capcut import install as install_mod
from app.integrations.capcut.errors import CapCutBusy, CapCutCancelled, CapCutError, UiTimeout
from app.integrations.capcut.pipeline import process_video
from app.integrations.capcut.transcribe import Word

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg ausente")

WORDS = [Word(0.1, 0.4, "Bom", 0.9), Word(0.4, 0.8, "dia", 0.9), Word(1.2, 1.5, "gente", 0.9)]


def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)


def audio_hash(path: Path) -> str:
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a", "-c", "copy",
                          "-f", "data", "-"], capture_output=True, check=True).stdout
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def env(tmp_path):
    base = tmp_path / "CapCut"
    (base / "Apps").mkdir(parents=True)
    (base / "Apps" / "CapCut.exe").write_bytes(b"MZ")
    (base / "User Data" / "Projects" / "com.lveditor.draft").mkdir(parents=True)
    (base / "User Data" / "Config").mkdir(parents=True)
    (base / "Videos").mkdir()
    inst = install_mod.detect({"MEDIA_TOOL_CAPCUT_ROOT": str(base)})
    video = tmp_path / "storage" / "origem.mp4"
    video.parent.mkdir()
    ff("-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=2", "-f", "lavfi", "-i",
       "sine=frequency=440:duration=2:sample_rate=48000", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
       "-shortest", str(video))
    return inst, video, tmp_path


def make_spec(tmp_path: Path, video: Path, **kw) -> capcut.CapCutJobSpec:
    data = tmp_path / "data"
    return capcut.CapCutJobSpec(job_id=7, video_path=video, draft_name="MT_7_m3", output_dir=tmp_path / "saida",
                                output_name="origem legendado", work_dir=data / "capcut" / "work" / "7",
                                models_dir=data / "models" / "whisper", **kw)


class FakeController:
    instances: list[FakeController] = []

    def __init__(self, install, *, state_dir, progress, cancel, fail_on=None, cancel_on=None):
        self.install, self.state_dir, self.progress, self.cancel = install, state_dir, progress, cancel
        self.calls: list[str] = []
        self.fail_on, self.cancel_on = fail_on, cancel_on
        self.warnings: list[str] = []
        self.last_export_created = False
        FakeController.instances.append(self)

    def _step(self, name):
        self.calls.append(name)
        if self.cancel_on == name:
            self.cancel.set()
        if self.cancel.is_set():
            raise CapCutCancelled()
        if self.fail_on == name:
            raise UiTimeout()

    def launch(self):
        self._step("launch")
        return 1234

    def wait_home(self, timeout=90):
        self._step("wait_home")

    def open_draft(self, name, timeout=60):
        self._step("open_draft")
        folder = self.install.drafts_dir / name
        assert draft_mod.validate_draft(folder, expected_name=name) == []
        store = json.loads(self.install.root_meta_path.read_text(encoding="utf-8"))["all_draft_store"]
        assert store[0]["draft_name"] == name

    def export(self, options, timeout=1800):
        self._step("export")
        self.progress("Exportando")
        content = json.loads((self.install.drafts_dir / "MT_7_m3" / "draft_content.json").read_text(encoding="utf-8"))
        src = content["materials"]["videos"][0]["path"]
        out = self.install.default_videos_dir / "MT_7_m3.mp4"
        ff("-i", src, "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100", "-map", "0:v", "-map", "1:a",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out))
        self.last_export_created = True
        self.options = options
        return out

    def close_project(self, timeout=30):
        self._step("close_project")

    def shutdown(self, timeout=40):
        self.calls.append("shutdown")


def factory(**extra):
    def make(install, **kw):
        return FakeController(install, **kw, **extra)
    FakeController.instances = []
    return make


class Recorder:
    def __init__(self):
        self.stages: list[tuple[str, float | None, str | None]] = []
        self.transcripts: list[list[dict]] = []

    def on_stage(self, stage, progress, detail):
        assert stage in capcut.STAGES
        self.stages.append((stage, progress, detail))

    def on_transcript(self, words):
        self.transcripts.append(words)

    def names(self):
        out = []
        for s, _p, _d in self.stages:
            if not out or out[-1] != s:
                out.append(s)
        return out


def fake_transcriber(calls):
    def run(video, *, model, language, models_dir, cancel, progress):
        calls.append((Path(video), model, language, Path(models_dir)))
        progress(0.5)
        return list(WORDS)
    return run


def test_full_sequence_with_cleanup(env):
    inst, video, tmp = env
    calls: list = []
    rec = Recorder()
    spec = make_spec(tmp, video)
    res = process_video(spec, cancel=threading.Event(), on_stage=rec.on_stage, on_transcript=rec.on_transcript,
                        install=inst, controller_factory=factory(), transcriber=fake_transcriber(calls),
                        running=lambda: [])
    assert rec.names() == ["transcribing", "drafting", "launching", "opening", "exporting", "finalizing", "cleaning"]
    assert calls == [(video, "large-v3-turbo", "pt", spec.models_dir)]
    assert rec.transcripts == [[w.to_dict() for w in WORDS]]
    ctl = FakeController.instances[0]
    assert ctl.calls[:5] == ["launch", "wait_home", "open_draft", "export", "close_project"]
    assert "shutdown" in ctl.calls
    assert ctl.state_dir == tmp / "data" / "capcut"
    assert (ctl.options.resolution_label(), ctl.options.fps_label()) == ("480P", "60fps")  # padrão: 60 fps fixo
    assert res.output_path == tmp / "saida" / "origem legendado.mp4" and res.output_path.is_file()
    assert audio_hash(res.output_path) == audio_hash(video)
    assert res.chunks == 2 and res.words == [w.to_dict() for w in WORDS]
    assert res.draft_kept is False and res.draft_name == "MT_7_m3"
    assert res.probe["sample_rate"] == 48000
    assert not (inst.drafts_dir / "MT_7_m3").exists()
    assert all(e["draft_name"] != "MT_7_m3"
               for e in json.loads(inst.root_meta_path.read_text(encoding="utf-8"))["all_draft_store"])
    assert not (inst.default_videos_dir / "MT_7_m3.mp4").exists()  # intermediário criado pelo job: apagado
    assert any(d == "Exportando" for s, _p, d in rec.stages if s == "exporting")


def test_keep_draft_and_reused_transcript(env):
    inst, video, tmp = env
    calls: list = []
    rec = Recorder()
    spec = make_spec(tmp, video, keep_draft=True, highlight=False,
                     transcript=[w.to_dict() for w in WORDS])
    res = process_video(spec, cancel=threading.Event(), on_stage=rec.on_stage, on_transcript=rec.on_transcript,
                        install=inst, controller_factory=factory(), transcriber=fake_transcriber(calls),
                        running=lambda: [])
    assert calls == [] and rec.transcripts == []  # retentativa: não transcreve de novo
    assert res.draft_kept is True and (inst.drafts_dir / "MT_7_m3").is_dir()
    assert "cleaning" not in rec.names()
    store = json.loads(inst.root_meta_path.read_text(encoding="utf-8"))["all_draft_store"]
    assert store[0]["draft_name"] == "MT_7_m3"


def test_busy_capcut_waits_after_saving_transcript(env):
    inst, video, tmp = env
    rec = Recorder()
    with pytest.raises(CapCutBusy) as exc:
        process_video(make_spec(tmp, video), cancel=threading.Event(), on_stage=rec.on_stage,
                      on_transcript=rec.on_transcript, install=inst, controller_factory=factory(),
                      transcriber=fake_transcriber([]), running=lambda: [555])
    assert exc.value.transient is True and exc.value.code == "capcut_busy"
    assert rec.transcripts and rec.names()[-1] == "waiting_capcut"
    assert not (inst.drafts_dir / "MT_7_m3").exists() and FakeController.instances == []


def test_no_speech_copies_without_captions(env):
    inst, video, tmp = env
    rec = Recorder()
    res = process_video(make_spec(tmp, video), cancel=threading.Event(), on_stage=rec.on_stage,
                        install=inst, controller_factory=factory(), transcriber=lambda *a, **k: [],
                        running=lambda: [])
    assert res.chunks == 0 and res.words == [] and res.output_path.is_file()
    assert res.warnings and "Nenhuma fala" in res.warnings[0]
    assert FakeController.instances == [] and not (inst.drafts_dir / "MT_7_m3").exists()


@pytest.mark.parametrize(("kind", "error"), [("cancel_on", CapCutCancelled), ("fail_on", UiTimeout)])
def test_failure_or_cancel_cleans_draft_and_closes_capcut(env, kind, error):
    inst, video, tmp = env
    with pytest.raises(error):
        process_video(make_spec(tmp, video), cancel=threading.Event(), on_stage=lambda *a: None, install=inst,
                      controller_factory=factory(**{kind: "export"}), transcriber=fake_transcriber([]),
                      running=lambda: [])
    ctl = FakeController.instances[0]
    assert ctl.calls[-1] == "shutdown"
    assert not (inst.drafts_dir / "MT_7_m3").exists()
    assert not list((tmp / "saida").glob("*.mp4"))


def test_cancel_before_start_and_missing_source(env):
    inst, video, tmp = env
    ev = threading.Event()
    ev.set()
    with pytest.raises(CapCutCancelled):
        process_video(make_spec(tmp, video), cancel=ev, on_stage=lambda *a: None, install=inst,
                      controller_factory=factory(), transcriber=fake_transcriber([]), running=lambda: [])
    with pytest.raises(CapCutError) as exc:
        process_video(make_spec(tmp, tmp / "sumiu.mp4"), cancel=threading.Event(), on_stage=lambda *a: None,
                      install=inst, controller_factory=factory(), transcriber=fake_transcriber([]),
                      running=lambda: [])
    assert exc.value.code == "source_missing" and exc.value.transient is False


def test_style_from_owner_draft_is_used_and_owner_draft_untouched(env):
    inst, video, tmp = env
    owner = inst.drafts_dir / "Modelo"
    shutil.copytree(draft_mod.ASSETS_BASE_DRAFT, owner)
    from app.integrations.capcut import style as style_mod

    st = style_mod.default_style()
    st.source_draft = "Modelo"
    st.material["font_title"] = "Fonte do Dono"
    before = {p: p.read_bytes() for p in owner.rglob("*") if p.is_file()}
    seen = {}

    class Spy(FakeController):
        def open_draft(self, name, timeout=60):
            super().open_draft(name, timeout)
            d = json.loads((self.install.drafts_dir / name / "draft_content.json").read_text(encoding="utf-8"))
            seen["fonts"] = {m["font_title"] for m in d["materials"]["texts"]}
            seen["highlight"] = [json.loads(m["content"])["styles"] for m in d["materials"]["texts"]]

    process_video(make_spec(tmp, video, style=st.to_dict(), highlight_color="#FF0000"), cancel=threading.Event(),
                  on_stage=lambda *a: None, install=inst, controller_factory=lambda i, **kw: Spy(i, **kw),
                  transcriber=fake_transcriber([]), running=lambda: [])
    assert seen["fonts"] == {"Fonte do Dono"}
    assert len(seen["highlight"]) == 3  # destaque: uma peça por palavra
    assert {p: p.read_bytes() for p in owner.rglob("*") if p.is_file()} == before


def test_contract_signatures(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_TOOL_CAPCUT_ROOT", str(tmp_path / "sem-capcut"))  # não lê a instalação real
    params = inspect.signature(capcut.process_video).parameters
    assert list(params) == ["spec", "cancel", "on_stage", "on_transcript"]
    assert params["on_transcript"].default is None
    assert capcut.STAGES == ("transcribing", "drafting", "waiting_capcut", "launching", "opening", "exporting",
                             "finalizing", "cleaning")
    assert issubclass(capcut.CapCutBusy, capcut.CapCutError) and capcut.CapCutBusy().transient is True
    assert capcut.CapCutCancelled().code == "cancelled" and capcut.CapCutCancelled().transient is False
    style = capcut.default_style()
    assert json.loads(json.dumps(style)) == style
    assert capcut.style_summary(style)["color"] == "#ffffff"


class TargetController(FakeController):
    """Como o controlador real: anota o caminho de exportação antes de exportar."""

    def launch(self):
        self.intermediate_at_launch = (self.install.default_videos_dir / "MT_7_m3.mp4").exists()
        return super().launch()

    def export(self, options, timeout=1800):
        self.export_target = self.install.default_videos_dir / "MT_7_m3.mp4"
        return super().export(options, timeout)


def test_work_dir_removed_on_success(env):
    inst, video, tmp = env
    spec = make_spec(tmp, video)
    process_video(spec, cancel=threading.Event(), on_stage=lambda *a: None, install=inst,
                  controller_factory=factory(), transcriber=fake_transcriber([]), running=lambda: [])
    assert not Path(spec.work_dir).exists()


def test_failure_after_export_records_intermediate_and_next_attempt_cleans_it(env):
    inst, video, tmp = env
    spec = make_spec(tmp, video)
    intermediate = inst.default_videos_dir / "MT_7_m3.mp4"
    FakeController.instances = []
    with pytest.raises(UiTimeout):
        process_video(spec, cancel=threading.Event(), on_stage=lambda *a: None, install=inst,
                      controller_factory=lambda install, **kw: TargetController(install, **kw,
                                                                                fail_on="close_project"),
                      transcriber=fake_transcriber([]), running=lambda: [])
    assert intermediate.is_file(), "falhou depois de exportar: o intermediário ficou"
    record = json.loads((Path(spec.work_dir) / "exports.json").read_text(encoding="utf-8"))
    assert record == {"draft_name": "MT_7_m3", "exports": [str(intermediate)]}
    assert [p.name for p in Path(spec.work_dir).iterdir()] == ["exports.json"], "em falha só fica o registro"
    process_video(spec, cancel=threading.Event(), on_stage=lambda *a: None, install=inst,
                  controller_factory=lambda install, **kw: TargetController(install, **kw),
                  transcriber=fake_transcriber([]), running=lambda: [])
    assert FakeController.instances[-1].intermediate_at_launch is False, "apagado antes de exportar de novo"
    assert not intermediate.exists() and not Path(spec.work_dir).exists()


def test_failure_without_export_leaves_no_work_dir(env):
    inst, video, tmp = env
    spec = make_spec(tmp, video)
    with pytest.raises(UiTimeout):
        process_video(spec, cancel=threading.Event(), on_stage=lambda *a: None, install=inst,
                      controller_factory=factory(fail_on="open_draft"), transcriber=fake_transcriber([]),
                      running=lambda: [])
    assert not Path(spec.work_dir).exists()


def test_no_speech_copy_failure_is_transient(env, monkeypatch):
    from app.integrations.capcut import pipeline as pipeline_mod

    inst, video, tmp = env

    def denied(*_a, **_k):
        raise PermissionError("sem permissão")

    monkeypatch.setattr(pipeline_mod.shutil, "copy2", denied)
    with pytest.raises(CapCutError) as exc:
        process_video(make_spec(tmp, video), cancel=threading.Event(), on_stage=lambda *a: None, install=inst,
                      controller_factory=factory(), transcriber=lambda *a, **k: [], running=lambda: [])
    assert exc.value.code == "output_write_failed" and exc.value.transient is True


def test_desktop_busy_before_any_draft_or_capcut(env):
    """PC em uso: DesktopBusy antes de criar rascunho ou abrir o CapCut (transcrição já entregue ao runner)."""
    from app.integrations.capcut.errors import DesktopBusy

    inst, video, tmp = env
    rec = Recorder()

    class BusyController(FakeController):
        def ensure_desktop_free(self):
            self.calls.append("ensure_desktop_free")
            raise DesktopBusy()

    def make(install, **kw):
        return BusyController(install, **kw)

    FakeController.instances = []
    spec = make_spec(tmp, video)
    with pytest.raises(DesktopBusy):
        process_video(spec, cancel=threading.Event(), on_stage=rec.on_stage, on_transcript=rec.on_transcript,
                      install=inst, controller_factory=make, transcriber=fake_transcriber([]), running=lambda: [])
    ctl = FakeController.instances[0]
    assert ctl.calls[0] == "ensure_desktop_free" and "launch" not in ctl.calls, "nada de abrir o CapCut"
    assert rec.transcripts, "a transcrição vai para o runner antes da espera"
    assert not (inst.drafts_dir / spec.draft_name).exists()
    if inst.root_meta_path.exists():  # sem índice = nada foi registrado
        store = json.loads(inst.root_meta_path.read_text(encoding="utf-8"))["all_draft_store"]
        assert all(e["draft_name"] != spec.draft_name for e in store)
