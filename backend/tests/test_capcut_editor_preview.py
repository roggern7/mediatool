"""Editor visual das Legendas CapCut: prévia pelo pipeline real (process_video) com recorte do trecho, transcrição só do
trecho, texto de exemplo sem fala, 720P e sem remux do áudio original. Controlador e transcritor falsos; FFmpeg real
para o vídeo sintético. Sem CapCut real."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

import app.integrations.capcut as capcut
from app.integrations.capcut import install as install_mod
from app.integrations.capcut.pipeline import process_video
from app.integrations.capcut.transcribe import Word

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg ausente")
SAMPLE = "Exemplo de legenda no seu estilo"


def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)


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
    ff("-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=10", "-f", "lavfi", "-i",
       "sine=frequency=440:duration=10:sample_rate=48000", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
       "-shortest", str(video))
    return inst, video, tmp_path


class ClipController:
    """'Exporta' só o trecho do rascunho (source_timerange do vídeo), como o CapCut faria."""

    def __init__(self, install, *, state_dir, progress, cancel, draft_name):
        self.install, self.cancel, self.draft_name = install, cancel, draft_name
        self.content = None
        self.options = None
        self.last_export_created = False
        self.warnings: list[str] = []

    def launch(self):
        return 1

    def wait_home(self, timeout=90):
        pass

    def open_draft(self, name, timeout=60):
        assert name == self.draft_name

    def export(self, options, timeout=1800):
        self.options = options
        self.content = json.loads((self.install.drafts_dir / self.draft_name / "draft_content.json")
                                  .read_text(encoding="utf-8"))
        main = next(t for t in self.content["tracks"] if t["type"] == "video")["segments"][0]
        src = self.content["materials"]["videos"][0]["path"]
        start = main["source_timerange"]["start"] / 1e6
        dur = main["source_timerange"]["duration"] / 1e6
        out = self.install.default_videos_dir / f"{self.draft_name}.mp4"
        ff("-ss", f"{start}", "-t", f"{dur}", "-i", src, "-vf", "scale=-2:720", "-c:v", "libx264", "-pix_fmt",
           "yuv420p", "-c:a", "aac", str(out))
        self.last_export_created = True
        return out

    def close_project(self, timeout=30):
        pass

    def shutdown(self, timeout=40):
        pass


def preview_spec(tmp: Path, video: Path, **kw) -> capcut.CapCutJobSpec:
    data = tmp / "data"
    fields = dict(job_id=0, video_path=video, draft_name="MT_preview_abcdef123456",
                  output_dir=data / "capcut" / "previews", output_name="abcdef123456",
                  work_dir=data / "capcut" / "work" / "preview_abcdef123456", models_dir=data / "models" / "whisper",
                  resolution="720P", fps="30", keep_original_audio=False, clip_start=2.0, clip_duration=4.0,
                  sample_text=SAMPLE)
    fields.update(kw)
    return capcut.CapCutJobSpec(**fields)


def run(inst, spec, transcriber):
    made = []

    def factory(install, **kw):
        controller = ClipController(install, draft_name=spec.draft_name, **kw)
        made.append(controller)
        return controller

    result = process_video(spec, cancel=threading.Event(), on_stage=lambda *a: None, install=inst,
                           controller_factory=factory, transcriber=transcriber, running=lambda: [])
    return result, made[0]


def test_preview_clip_without_speech_uses_sample_text(env):
    inst, video, tmp = env
    heard = []

    def silent(path, **kw):
        seconds = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                  str(path)], capture_output=True, text=True, check=True).stdout
        heard.append((Path(path).name, round(float(seconds), 1)))
        return []

    spec = preview_spec(tmp, video)
    result, controller = run(inst, spec, silent)
    assert heard == [("clip.wav", 4.0)], "transcreve só o áudio do trecho"
    content = controller.content
    main = next(t for t in content["tracks"] if t["type"] == "video")["segments"][0]
    assert main["source_timerange"] == {"start": 2_000_000, "duration": 4_000_000}
    assert main["target_timerange"] == {"start": 0, "duration": 4_000_000} and content["duration"] == 4_000_000
    assert content["materials"]["videos"][0]["duration"] == 10_000_000, "o material continua sendo o arquivo inteiro"
    texts = [json.loads(m["content"])["text"] for m in content["materials"]["texts"]]
    assert " ".join(dict.fromkeys(texts)) == SAMPLE, "texto de exemplo nos blocos"
    segs = next(t for t in content["tracks"] if t["type"] == "text")["segments"]
    assert all(s["target_timerange"]["start"] + s["target_timerange"]["duration"] <= 4_000_000 for s in segs)
    assert controller.options.resolution == "720P"
    assert result.output_path == spec.output_dir / "abcdef123456.mp4" and result.output_path.is_file()
    assert abs(result.probe["duration"] - 4.0) < 0.3 and result.probe["height"] == 720
    assert "Sem fala no trecho: usado o texto de exemplo." in result.warnings
    assert not (inst.drafts_dir / spec.draft_name).exists(), "rascunho temporário removido"
    assert not spec.work_dir.exists()


def test_preview_transcribed_words_are_relative_to_clip(env):
    inst, video, tmp = env

    def speech(path, **kw):
        return [Word(0.2, 0.6, "Bom"), Word(0.6, 1.0, "dia"), Word(1.5, 2.0, "gente")]

    spec = preview_spec(tmp, video, clip_start=8.0, clip_duration=6.0)
    result, controller = run(inst, spec, speech)
    main = next(t for t in controller.content["tracks"] if t["type"] == "video")["segments"][0]
    assert main["source_timerange"] == {"start": 8_000_000, "duration": 2_000_000}, "trecho limitado ao fim do vídeo"
    first = next(t for t in controller.content["tracks"] if t["type"] == "text")["segments"][0]
    assert first["target_timerange"]["start"] == 200_000
    assert "texto de exemplo" not in " ".join(result.warnings)


def test_clip_out_of_range_starts_at_the_end(env):
    inst, video, tmp = env
    spec = preview_spec(tmp, video, clip_start=500.0, clip_duration=4.0)
    _, controller = run(inst, spec, lambda path, **kw: [])
    main = next(t for t in controller.content["tracks"] if t["type"] == "video")["segments"][0]
    assert main["source_timerange"] == {"start": 6_000_000, "duration": 4_000_000}, "os últimos segundos do vídeo"
