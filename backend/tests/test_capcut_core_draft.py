"""Núcleo CapCut: motor de rascunho 9.x, registro no índice global, extração/aplicação de estilo e destaque.

Sem CapCut real: instalação falsa em tmp_path (MEDIA_TOOL_CAPCUT_ROOT) com o rascunho-base sanitizado da fixture;
vídeo sintético curto gerado pelo FFmpeg real.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.integrations.capcut import draft as draft_mod
from app.integrations.capcut import install as install_mod
from app.integrations.capcut import style as style_mod
from app.integrations.capcut.errors import CapCutBusy, CapCutError
from app.integrations.capcut.media import probe
from app.integrations.capcut.transcribe import Chunk, Word

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg ausente")

NOT_RUNNING = lambda: []  # noqa: E731
RUNNING = lambda: [4242]  # noqa: E731


def make_install(tmp_path: Path) -> install_mod.CapCutInstall:
    base = tmp_path / "CapCut"
    (base / "Apps" / "9.7.1.4203").mkdir(parents=True)
    (base / "Apps" / "CapCut.exe").write_bytes(b"MZ")
    (base / "Apps" / "ProductInfo.xml").write_text('<packet><full_appver value="9.7.1.4203" /></packet>',
                                                   encoding="utf-8")
    drafts = base / "User Data" / "Projects" / "com.lveditor.draft"
    drafts.mkdir(parents=True)
    (base / "User Data" / "Config").mkdir(parents=True)
    (base / "Videos").mkdir()
    inst = install_mod.detect({"MEDIA_TOOL_CAPCUT_ROOT": str(base)})
    assert inst is not None
    return inst


def make_video(path: Path, *, seconds: float = 2.0, size: str = "320x240", rate: int = 30,
               freq: int = 440) -> Path:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc=size={size}:rate={rate}:duration={seconds}", "-f", "lavfi", "-i",
                    f"sine=frequency={freq}:duration={seconds}:sample_rate=48000", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def words(*items: tuple[float, float, str]) -> list[Word]:
    return [Word(a, b, t, 0.9) for a, b, t in items]


def chunk(*items: tuple[float, float, str]) -> Chunk:
    ws = words(*items)
    return Chunk(start=ws[0].start, end=ws[-1].end + 0.05, text=" ".join(w.text for w in ws), words=ws)


@pytest.fixture
def inst(tmp_path):
    return make_install(tmp_path)


@pytest.fixture
def video(tmp_path):
    return make_video(tmp_path / "origem.mp4")


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def text_track(content):
    tracks = [t for t in content["tracks"] if t["type"] == "text"]
    assert len(tracks) == 1
    return tracks[0]


def contents_of(material):
    return json.loads(material["content"])


# --------------------------------------------------------------------------------------------- build_draft

def test_build_draft_from_fixture(inst, video):
    chunks = [chunk((0.1, 0.5, "Onde"), (0.5, 0.7, "é")), chunk((0.9, 1.2, "a"), (1.2, 1.6, "rua?"))]
    out = draft_mod.build_draft(inst, name="MT_1_m7", video=video, chunks=chunks, style=style_mod.default_style(inst),
                                highlight=None, base_draft=None, running=NOT_RUNNING)
    assert out == inst.drafts_dir / "MT_1_m7"
    assert draft_mod.validate_draft(out, expected_name="MT_1_m7") == []
    root_raw = (out / "draft_content.json").read_bytes()
    d = json.loads(root_raw)
    fixture = read(draft_mod.ASSETS_BASE_DRAFT / "draft_content.json")
    # ids novos (linha do tempo, projeto, trilha/segmento/material do vídeo, meta)
    assert d["id"] != fixture["id"]
    assert (out / "Timelines" / d["id"] / "draft_content.json").read_bytes() == root_raw
    assert not (out / "Timelines" / fixture["id"]).exists()
    pj = read(out / "Timelines" / "project.json")
    assert pj["main_timeline_id"] == d["id"] and [t["id"] for t in pj["timelines"]] == [d["id"]]
    assert pj["id"] != read(draft_mod.ASSETS_BASE_DRAFT / "Timelines" / "project.json")["id"]
    assert read(out / "timeline_layout.json")["dockItems"][0]["timelineIds"] == [d["id"]]
    vtrack = [t for t in d["tracks"] if t["type"] == "video"]
    assert len(vtrack) == 1 and len(vtrack[0]["segments"]) == 1
    vseg = vtrack[0]["segments"][0]
    fseg = fixture["tracks"][0]["segments"][0]
    assert vseg["id"] != fseg["id"] and vseg["material_id"] != fseg["material_id"]
    assert set(vseg["extra_material_refs"]).isdisjoint(fseg["extra_material_refs"])
    assert len(vseg["extra_material_refs"]) == len(fseg["extra_material_refs"])
    # vídeo trocado, duração/fps/canvas da origem
    info = probe(video)
    dur = int(round(info.duration * 1e6))
    vm = d["materials"]["videos"][0]
    assert vm["path"] == video.resolve().as_posix() and vm["width"] == 320 and vm["height"] == 240
    assert vm["has_audio"] is True and vm["duration"] == dur and vm["material_name"] == "origem.mp4"
    assert vseg["source_timerange"] == {"start": 0, "duration": dur} == vseg["target_timerange"]
    assert d["duration"] == dur and d["fps"] == 30.0
    assert d["canvas_config"]["width"] == 320 and d["canvas_config"]["height"] == 240
    assert d["canvas_config"]["ratio"] == "original"
    # uma trilha de texto, sem sobreposição, dentro da duração
    segs = text_track(d)["segments"]
    assert len(segs) == 2
    assert segs[0]["target_timerange"]["start"] == 100000
    a, b = segs[0]["target_timerange"], segs[1]["target_timerange"]
    assert a["start"] + a["duration"] <= b["start"] and b["start"] + b["duration"] <= dur
    texts = {m["id"]: m for m in d["materials"]["texts"]}
    assert contents_of(texts[segs[0]["material_id"]])["text"] == "Onde é"
    assert texts[segs[0]["material_id"]]["text"] == "Onde é"
    # meta e transitórios
    meta = read(out / "draft_meta_info.json")
    assert meta["draft_name"] == "MT_1_m7" and meta["draft_fold_path"] == out.as_posix()
    assert meta["tm_duration"] == dur
    assert meta["draft_materials"][0]["type"] == 0
    assert meta["draft_materials"][0]["value"][0]["file_Path"] == video.resolve().as_posix()
    for leftover in ("draft_content.json.bak", "template-2.tmp", ".locked"):
        assert not (out / leftover).exists()
    assert not list(inst.drafts_dir.glob(".*building"))


def test_build_draft_from_owner_style_draft_keeps_only_main_video(inst, video, tmp_path):
    base = copy_fixture_as(inst, "estilo")
    d = read(base / "draft_content.json")
    # simula rascunho do dono: 2 segmentos de vídeo, trilha de áudio, texto, sobras transitórias
    vtrack = d["tracks"][0]
    extra_seg = copy.deepcopy(vtrack["segments"][0])
    extra_seg.update(id="SEG-EXTRA", material_id="VID-EXTRA", extra_material_refs=["SPEED-EXTRA"])
    vtrack["segments"].append(extra_seg)
    d["materials"]["videos"].append({"id": "VID-EXTRA", "type": "video", "path": "C:/dono/outro.mp4"})
    d["materials"]["speeds"].append({"id": "SPEED-EXTRA", "type": "speed", "speed": 2.0, "mode": 0})
    d["materials"]["audios"] = [{"id": "AUD-1", "type": "music", "path": "C:/dono/musica.mp3"}]
    d["tracks"].append({"id": "TRK-A", "type": "audio", "segments": [{"id": "SA", "material_id": "AUD-1",
                        "extra_material_refs": [], "target_timerange": {"start": 0, "duration": 1}}]})
    write_content(base, d)
    (base / "draft_content.json.bak").write_text("{}", encoding="utf-8")
    (base / ".locked").write_text("", encoding="utf-8")
    out = draft_mod.build_draft(inst, name="MT_2_m8", video=video, chunks=[chunk((0.2, 0.6, "Oi"))],
                                style=style_mod.default_style(), highlight=None, base_draft=base, running=NOT_RUNNING)
    nd = read(out / "draft_content.json")
    assert [t["type"] for t in nd["tracks"]] == ["video", "text"]
    assert len(nd["materials"]["videos"]) == 1
    raw = (out / "draft_content.json").read_text(encoding="utf-8")
    assert "outro.mp4" not in raw and "musica.mp3" not in raw and "SPEED-EXTRA" not in raw
    assert not (out / ".locked").exists() and not (out / "draft_content.json.bak").exists()
    assert draft_mod.validate_draft(out) == []
    # o rascunho-base do dono não foi tocado
    assert (base / ".locked").exists() and read(base / "draft_content.json") == d


def copy_fixture_as(inst, name: str) -> Path:
    dst = inst.drafts_dir / name
    shutil.copytree(draft_mod.ASSETS_BASE_DRAFT, dst)
    meta = read(dst / "draft_meta_info.json")
    meta["draft_name"] = name
    (dst / "draft_meta_info.json").write_text(json.dumps(meta), encoding="utf-8")
    return dst


def write_content(folder: Path, d) -> None:
    text = json.dumps(d, ensure_ascii=False)
    (folder / "draft_content.json").write_text(text, encoding="utf-8")
    (folder / "Timelines" / d["id"] / "draft_content.json").write_text(text, encoding="utf-8")


def test_build_draft_refuses_bad_name_and_running_capcut(inst, video):
    st = style_mod.default_style()
    with pytest.raises(CapCutError) as exc:
        draft_mod.build_draft(inst, name="0923", video=video, chunks=[], style=st, highlight=None, base_draft=None,
                              running=NOT_RUNNING)
    assert exc.value.code == "invalid_draft_name"
    with pytest.raises(CapCutBusy):
        draft_mod.build_draft(inst, name="MT_3_m1", video=video, chunks=[], style=st, highlight=None,
                              base_draft=None, running=RUNNING)
    assert not (inst.drafts_dir / "MT_3_m1").exists()


def test_build_draft_clamps_to_video_duration_and_replaces_leftover(inst, video):
    st = style_mod.default_style()
    leftover = inst.drafts_dir / "MT_4_m1"
    leftover.mkdir()
    (leftover / "lixo.txt").write_text("x", encoding="utf-8")
    chunks = [chunk((1.5, 1.9, "fim")), chunk((5.0, 5.5, "depois"))]  # o 2º começa depois do vídeo (2 s)
    out = draft_mod.build_draft(inst, name="MT_4_m1", video=video, chunks=chunks, style=st, highlight=None,
                                base_draft=None, running=NOT_RUNNING)
    d = read(out / "draft_content.json")
    segs = text_track(d)["segments"]
    assert len(segs) == 1
    assert segs[0]["target_timerange"]["start"] + segs[0]["target_timerange"]["duration"] <= d["duration"]
    assert not (out / "lixo.txt").exists()


def test_build_draft_uses_chosen_export_fps(inst, video):
    """fps fixo da exportação (padrão 60) vira o fps da linha do tempo; sem fps, vale o da origem (30)."""
    st = style_mod.default_style()
    out = draft_mod.build_draft(inst, name="MT_6_m1", video=video, chunks=[chunk((0.2, 0.6, "Oi"))], style=st,
                                highlight=None, base_draft=None, running=NOT_RUNNING, fps=60.0)
    d = read(out / "draft_content.json")
    assert d["fps"] == 60.0
    tl = out / "Timelines" / d["id"] / "draft_content.json"
    assert read(tl)["fps"] == 60.0


def test_validate_draft_detects_problems(inst, video):
    out = draft_mod.build_draft(inst, name="MT_5_m1", video=video,
                                chunks=[chunk((0.1, 0.4, "um")), chunk((0.6, 0.9, "dois"))],
                                style=style_mod.default_style(), highlight=None, base_draft=None,
                                running=NOT_RUNNING)
    d = read(out / "draft_content.json")
    segs = text_track(d)["segments"]
    segs[1]["target_timerange"]["start"] = segs[0]["target_timerange"]["start"]  # sobreposição
    segs[0]["material_id"] = "NAO-EXISTE"
    (out / "draft_content.json").write_text(json.dumps(d), encoding="utf-8")
    problems = draft_mod.validate_draft(out)
    assert any("espelho" in p for p in problems)
    assert any("sobreposição" in p for p in problems)
    assert any("material_id inexistente" in p for p in problems)


# --------------------------------------------------------------------------------------------- root_meta_info

def seed_root_meta(inst) -> dict:
    rm = {"all_draft_store": [dict(draft_mod.MINIMAL_ROOT_ENTRY, draft_name="0923", draft_id="OWNER-1",
                                   draft_fold_path=(inst.drafts_dir / "0923").as_posix(), draft_type="x-dono")],
          "draft_ids": 1, "root_path": inst.drafts_dir.as_posix()}
    inst.root_meta_path.write_text(json.dumps(rm), encoding="utf-8")
    return rm


def test_register_unregister_delete_atomic_with_single_backup(inst, video):
    original = seed_root_meta(inst)
    original_bytes = inst.root_meta_path.read_bytes()
    out = draft_mod.build_draft(inst, name="MT_6_m2", video=video, chunks=[chunk((0.1, 0.4, "olá"))],
                                style=style_mod.default_style(), highlight=None, base_draft=None,
                                running=NOT_RUNNING)
    entry = draft_mod.register_draft(inst, out, template_name="0923", running=NOT_RUNNING)
    backup = inst.drafts_dir / draft_mod.ROOT_META_BACKUP
    assert backup.read_bytes() == original_bytes
    rm = read(inst.root_meta_path)
    assert [e["draft_name"] for e in rm["all_draft_store"]] == ["MT_6_m2", "0923"]
    assert rm["draft_ids"] == 2
    assert entry["draft_type"] == "x-dono"  # campos do rascunho-modelo
    assert entry["draft_fold_path"] == out.as_posix()
    assert entry["draft_json_file"].endswith("draft_content.json")
    assert entry["draft_id"] == read(out / "draft_meta_info.json")["draft_id"]
    # registrar de novo não duplica
    draft_mod.register_draft(inst, out, running=NOT_RUNNING)
    assert [e["draft_name"] for e in read(inst.root_meta_path)["all_draft_store"]] == ["MT_6_m2", "0923"]
    assert backup.read_bytes() == original_bytes  # backup único
    assert not list(inst.drafts_dir.glob(".mt*.tmp"))
    # recusa com o CapCut aberto, sem mexer em nada
    before = inst.root_meta_path.read_bytes()
    with pytest.raises(CapCutBusy):
        draft_mod.unregister_draft(inst, "MT_6_m2", running=RUNNING)
    with pytest.raises(CapCutBusy):
        draft_mod.register_draft(inst, out, running=RUNNING)
    with pytest.raises(CapCutBusy):
        draft_mod.delete_draft(inst, "MT_6_m2", running=RUNNING)
    assert inst.root_meta_path.read_bytes() == before and out.is_dir()
    # apagar: só MT_, tira do índice e apaga a pasta
    with pytest.raises(CapCutError):
        draft_mod.delete_draft(inst, "0923", running=NOT_RUNNING)
    draft_mod.delete_draft(inst, "MT_6_m2", running=NOT_RUNNING)
    assert not out.exists()
    assert read(inst.root_meta_path)["all_draft_store"] == original["all_draft_store"]
    assert draft_mod.unregister_draft(inst, "MT_6_m2", running=NOT_RUNNING) is False


def test_register_without_root_meta_uses_minimal_fields(inst, video):
    out = draft_mod.build_draft(inst, name="MT_7_m2", video=video, chunks=[chunk((0.1, 0.4, "olá"))],
                                style=style_mod.default_style(), highlight=None, base_draft=None,
                                running=NOT_RUNNING)
    draft_mod.register_draft(inst, out, running=NOT_RUNNING)
    rm = read(inst.root_meta_path)
    entry = rm["all_draft_store"][0]
    assert set(draft_mod.MINIMAL_ROOT_ENTRY) <= set(entry)
    assert rm["root_path"] == inst.drafts_dir.as_posix() and rm["draft_ids"] == 1


# --------------------------------------------------------------------------------------------- estilo

def styled_draft(inst, name: str = "Meu modelo") -> Path:
    """Rascunho "do dono" com legenda estilizada: 2 cores, animações in/out/loop, bolha de texto."""
    folder = copy_fixture_as(inst, name)
    d = read(folder / "draft_content.json")
    mat = style_mod.default_style().material
    mat.update(id="TXT-1", font_title="Montserrat", font_path="C:/fontes/montserrat.ttf",
               fonts=[{"title": "Montserrat", "path": "C:/fontes/montserrat.ttf"}], text="Olá mundo bonito",
               caption_template_info={"resource_name": "Pop", "resource_id": "123"},
               words={"start_time": [0, 400], "end_time": [400, 900], "text": ["Olá", "mundo"]})
    white = contents_of(mat)["styles"][0]
    yellow = copy.deepcopy(white)
    yellow["fill"]["content"]["solid"]["color"] = [1.0, 0.8313725490196079, 0.0]
    yellow["size"] = 9
    content = {"styles": [dict(copy.deepcopy(white), range=[0, 4]), dict(yellow, range=[4, 9]),
                          dict(copy.deepcopy(white), range=[9, 16])], "text": "Olá mundo bonito"}
    mat["content"] = json.dumps(content, ensure_ascii=False)
    d["materials"]["texts"] = [mat]
    d["materials"]["material_animations"] = [{"id": "ANIM-1", "type": "sticker_animation", "animations": [
        {"type": "in", "start": 0, "duration": 300000, "name": "Surgir"},
        {"type": "out", "start": 700000, "duration": 300000, "name": "Sumir"},
        {"type": "loop", "start": 0, "duration": 1000000, "name": "Pulsar"}]}]
    d["materials"]["effects"] = [{"id": "FX-1", "type": "text_shape", "name": "Bolha"}]
    seg = copy.deepcopy(style_mod.DEFAULT_SEGMENT)
    seg.update(id="SEGT-1", material_id="TXT-1", extra_material_refs=["ANIM-1", "FX-1"],
               target_timerange={"start": 500000, "duration": 1000000})
    seg["clip"]["transform"]["y"] = -0.4
    seg["clip"]["scale"] = {"x": 1.3, "y": 1.3}
    d["tracks"].append({"id": "TRK-T", "type": "text", "segments": [seg], "flag": 0, "attribute": 0})
    write_content(folder, d)
    return folder


def test_list_and_extract_style_with_extras_and_highlight(inst):
    styled_draft(inst)
    copy_fixture_as(inst, "sem texto")
    gen = styled_draft(inst, "MT_99_m1")
    drafts = style_mod.list_caption_drafts(inst)
    assert [x.name for x in drafts] == ["Meu modelo"]  # sem texto e MT_ ficam fora
    assert drafts[0].text_segments == 1 and drafts[0].preview == "Olá mundo bonito"
    assert gen.is_dir()
    before = {p: p.read_bytes() for p in (inst.drafts_dir / "Meu modelo").rglob("*") if p.is_file()}

    st = style_mod.extract_style(inst, "Meu modelo", 0)
    assert st.source_draft == "Meu modelo"
    assert st.material["id"] == "TXT-1" and st.material["font_title"] == "Montserrat"
    assert [e["array"] for e in st.extras] == ["material_animations", "effects"]
    assert st.highlight_color == "#ffd400"
    assert st.clip["transform"]["y"] == -0.4 and st.uniform_scale == {"on": True, "value": 1.0}
    assert st.caption_template_info["resource_name"] == "Pop"
    assert st.font_path == "C:/fontes/montserrat.ttf" and st.fonts[0]["title"] == "Montserrat"
    assert st.words_convention["unit"] == "ms" and st.words_convention["relative"] is True
    data = json.loads(json.dumps(st.to_dict()))  # serializável
    assert style_mod.CaptionStyle.from_dict(data).to_dict() == st.to_dict()
    summary = style_mod.style_summary(data)
    assert summary == {"font": "Montserrat", "color": "#ffffff", "highlight_color": "#ffd400",
                       "has_animation": True, "has_effect": True, "caption_template": "Pop",
                       "position_y": -0.4, "scale": 1.3}
    # só leitura
    assert {p: p.read_bytes() for p in (inst.drafts_dir / "Meu modelo").rglob("*") if p.is_file()} == before
    with pytest.raises(CapCutError) as exc:
        style_mod.extract_style(inst, "não existe")
    assert exc.value.code == "draft_not_found"
    with pytest.raises(CapCutError) as exc:
        style_mod.extract_style(inst, "Meu modelo", 3)
    assert exc.value.code == "no_text_segment"


def test_apply_style_plain_preserves_style_and_clones_extras(inst):
    styled_draft(inst)
    st = style_mod.extract_style(inst, "Meu modelo")
    c = chunk((1.0, 1.3, "Ação"), (1.3, 1.8, "rápida"))
    mats, segs, extras = style_mod.apply_style(st, c, 1_000_000, 850_000)
    assert len(mats) == len(segs) == 1
    content = contents_of(mats[0])
    assert content["text"] == "Ação rápida"
    assert len(content["styles"]) == 1 and content["styles"][0]["range"] == [0, 11]
    assert style_mod._style_color(content["styles"][0]) == "#ffffff"  # 1º estilo (não o destaque)
    assert mats[0]["id"] != "TXT-1" and mats[0]["font_title"] == "Montserrat"
    assert mats[0]["text"] == "Ação rápida"
    assert mats[0]["words"] == {"start_time": [0, 300], "end_time": [300, 800], "text": ["Ação", "rápida"]}
    seg = segs[0]
    assert seg["material_id"] == mats[0]["id"] and seg["target_timerange"] == {"start": 1_000_000,
                                                                               "duration": 850_000}
    assert seg["clip"]["transform"]["y"] == -0.4
    ids = [e["object"]["id"] for e in extras]
    assert seg["extra_material_refs"] == ids and "ANIM-1" not in ids and "FX-1" not in ids
    anim = next(e["object"] for e in extras if e["array"] == "material_animations")
    kinds = {a["type"]: a for a in anim["animations"]}
    assert set(kinds) == {"in", "out", "loop"}
    assert kinds["out"]["start"] + kinds["out"]["duration"] == 850_000
    assert kinds["loop"]["duration"] <= 850_000


def test_highlight_per_word_covers_block_without_gaps(inst):
    styled_draft(inst)
    st = style_mod.extract_style(inst, "Meu modelo")
    c = chunk((2.0, 2.3, "Onde"), (2.3, 2.45, "é"), (2.5, 3.0, "Conceição?"))
    start, dur = 2_000_000, 1_050_000
    mats, segs, extras = style_mod.apply_style(st, c, start, dur, style_mod.HighlightOptions(color="#00FF00"))
    assert len(segs) == 3
    spans = [(s["target_timerange"]["start"], s["target_timerange"]["duration"]) for s in segs]
    assert spans[0][0] == start
    for (a, da), (b, _db) in zip(spans, spans[1:]):
        assert a + da == b  # sem buraco nem sobreposição
    assert spans[-1][0] + spans[-1][1] == start + dur
    assert spans[1][0] == 2_300_000 and spans[2][0] == 2_500_000
    expected_ranges = [[0, 4], [5, 6], [7, 17]]  # acentos: contagem UTF-16 == caracteres no BMP
    by_id = {m["id"]: m for m in mats}
    for seg, rng in zip(segs, expected_ranges):
        content = contents_of(by_id[seg["material_id"]])
        assert content["text"] == "Onde é Conceição?"
        hl = [s for s in content["styles"] if style_mod._style_color(s) == "#00ff00"]
        assert len(hl) == 1 and hl[0]["range"] == rng
        others = [s for s in content["styles"] if s is not hl[0]]
        assert all(style_mod._style_color(s) == "#ffffff" for s in others)
        covered = sorted(r for s in content["styles"] for r in [tuple(s["range"])])
        assert covered[0][0] == 0 and covered[-1][1] == 17
        assert all(a[1] == b[0] for a, b in zip(covered, covered[1:]))
    # animações: 'in' só no 1º, 'out' só no último, 'loop' em todos
    extra_by_id = {e["object"]["id"]: e for e in extras}
    kinds = []
    for seg in segs:
        anims = [extra_by_id[r]["object"] for r in seg["extra_material_refs"]
                 if extra_by_id[r]["array"] == "material_animations"]
        kinds.append(sorted(a["type"] for a in anims[0]["animations"]))
        assert any(extra_by_id[r]["array"] == "effects" for r in seg["extra_material_refs"])
    assert kinds == [["in", "loop"], ["loop"], ["loop", "out"]]
    assert len({r for s in segs for r in s["extra_material_refs"]}) == sum(len(s["extra_material_refs"]) for s in segs)


def test_highlight_uses_model_highlight_style_and_utf16_ranges():
    st = style_mod.default_style()
    c = chunk((0.0, 0.4, "Oi"), (0.4, 0.8, "😀"), (0.8, 1.0, "tchau"))
    mats, segs, _ = style_mod.apply_style(st, c, 0, 1_050_000, style_mod.HighlightOptions(color="#FFD400"))
    content = [contents_of(m) for m in mats]
    ranges = [[s["range"] for s in cont["styles"] if style_mod._style_color(s) == "#ffd400"][0] for cont in content]
    assert ranges == [[0, 2], [3, 5], [6, 11]]  # emoji conta 2 unidades UTF-16
    assert all(cont["styles"][-1]["range"][1] == 11 for cont in content)


def test_default_style_is_the_skill_native_material(inst):
    st = style_mod.default_style(inst)
    assert st.material["check_flag"] == 15
    styles = contents_of(st.material)["styles"]
    assert style_mod._style_color(styles[0]) == "#ffffff"
    assert styles[0]["strokes"][0]["content"]["solid"]["color"] == [0.0, 0.0, 0.0]
    assert st.clip["transform"]["y"] == -0.70 and abs(st.clip["scale"]["x"] - 1.919) < 0.001
    assert st.source_draft is None and st.extras == []


def test_words_convention_inference():
    seg = {"target_timerange": {"start": 5_000_000, "duration": 1_000_000}}
    rel_ms = {"words": {"start_time": [0, 500], "end_time": [500, 1000], "text": ["a", "b"]}}
    abs_ms = {"words": {"start_time": [5000, 5500], "end_time": [5500, 6000], "text": ["a", "b"]}}
    rel_us = {"words": {"start_time": [0, 500000], "end_time": [500000, 1000000], "text": ["a", "b"]}}
    abs_us = {"words": {"start_time": [5000000], "end_time": [6000000], "text": ["a"]}}
    empty = {"words": {"start_time": [], "end_time": [], "text": []}}
    f = style_mod.infer_words_convention
    assert (f(rel_ms, seg)["unit"], f(rel_ms, seg)["relative"]) == ("ms", True)
    assert (f(abs_ms, seg)["unit"], f(abs_ms, seg)["relative"]) == ("ms", False)
    assert (f(rel_us, seg)["unit"], f(rel_us, seg)["relative"]) == ("us", True)
    assert (f(abs_us, seg)["unit"], f(abs_us, seg)["relative"]) == ("us", False)
    assert f(empty, seg)["inferred"] is False


def test_build_draft_with_highlight_and_style_draft_end_to_end(inst, video):
    styled_draft(inst)
    st = style_mod.extract_style(inst, "Meu modelo")
    chunks = [chunk((0.1, 0.4, "Bom"), (0.4, 0.9, "dia")), chunk((1.1, 1.4, "gente"))]
    base = style_mod.find_draft_dir(inst, "Meu modelo")
    out = draft_mod.build_draft(inst, name="MT_8_m3", video=video, chunks=chunks, style=st,
                                highlight=style_mod.HighlightOptions(color=st.highlight_color), base_draft=base,
                                running=NOT_RUNNING)
    assert draft_mod.validate_draft(out) == []
    d = read(out / "draft_content.json")
    segs = text_track(d)["segments"]
    assert len(segs) == 3
    assert [s["render_index"] for s in segs] == [14000, 14001, 14002]
    raw = json.dumps(d)
    assert "TXT-1" not in raw and "ANIM-1" not in raw and "SEGT-1" not in raw  # o texto do modelo não vai junto
    assert len(d["materials"]["material_animations"]) == 3


CLOUD_OWNER = {"cloud_draft_sync": True, "cloud_draft_cover": True, "tm_draft_cloud_entry_id": 98765,
               "tm_draft_cloud_parent_entry_id": 555, "tm_draft_cloud_space_id": 4321, "tm_draft_cloud_user_id": 777,
               "tm_draft_cloud_modified": 1730000000, "tm_draft_cloud_completed": "sim",
               "draft_cloud_template_id": "TPL-DONO", "draft_cloud_purchase_info": "{\"x\":1}",
               "draft_is_cloud_temp_draft": True, "draft_is_pippit_draft": True, "pippit_id": "PIP-1",
               "pippit_user_name": "dono", "cloud_package_completed_time": "2026-11-01"}


def test_cloud_fields_of_owner_template_never_reach_generated_draft(inst, video):
    """Nada de nuvem/compartilhamento do projeto-modelo do dono vai para o MT_ (índice global e draft_meta_info)."""
    base = copy_fixture_as(inst, "0923")
    meta = read(base / "draft_meta_info.json")
    meta.update(CLOUD_OWNER, draft_enterprise_info={"draft_enterprise_extra": "e", "draft_enterprise_id": "ENT-9",
                                                    "draft_enterprise_name": "Empresa", "enterprise_material": [1]})
    (base / "draft_meta_info.json").write_text(json.dumps(meta), encoding="utf-8")
    rm = seed_root_meta(inst)
    rm["all_draft_store"][0].update(CLOUD_OWNER)
    inst.root_meta_path.write_text(json.dumps(rm), encoding="utf-8")

    out = draft_mod.build_draft(inst, name="MT_9_m1", video=video, chunks=[chunk((0.1, 0.4, "olá"))],
                                style=style_mod.default_style(), highlight=None, base_draft=base,
                                running=NOT_RUNNING)
    entry = draft_mod.register_draft(inst, out, template_name="0923", running=NOT_RUNNING)
    new_meta = read(out / "draft_meta_info.json")
    for data in (entry, read(inst.root_meta_path)["all_draft_store"][0], new_meta):
        assert data["cloud_draft_sync"] is False and data["cloud_draft_cover"] is False
        assert data["tm_draft_cloud_entry_id"] == -1 and data["tm_draft_cloud_parent_entry_id"] == -1
        assert data["tm_draft_cloud_space_id"] == -1 and data["tm_draft_cloud_user_id"] == -1
        assert data["tm_draft_cloud_modified"] == 0 and data["tm_draft_cloud_completed"] == ""
        assert data["draft_cloud_template_id"] == "" and data["draft_cloud_purchase_info"] == ""
        assert data["draft_is_cloud_temp_draft"] is False and data["draft_is_pippit_draft"] is False
        assert data["pippit_id"] == "" and data["pippit_user_name"] == ""
    assert new_meta["draft_enterprise_info"] == draft_mod.NEUTRAL_ENTERPRISE_INFO
    raw = (out / "draft_meta_info.json").read_text(encoding="utf-8") + inst.root_meta_path.read_text(encoding="utf-8")
    for owner_id in ("98765", "4321", "TPL-DONO", "PIP-1", "ENT-9"):
        assert raw.count(owner_id) == (1 if owner_id in ("98765", "4321", "TPL-DONO", "PIP-1") else 0), owner_id
    # a única ocorrência é a entrada do próprio dono (0923), intocada
    assert read(inst.root_meta_path)["all_draft_store"][1]["tm_draft_cloud_entry_id"] == 98765
    assert entry["draft_type"] == "x-dono"  # campos que não são de nuvem continuam vindo do modelo


@pytest.mark.parametrize("name", ["..", ".", "../fora", "a/b", "a\\b", "x..y", "C:nome", ""])
def test_find_draft_dir_rejects_unsafe_names(inst, name):
    outside = inst.drafts_dir.parent
    (outside / "draft_content.json").write_text("{}", encoding="utf-8")
    (outside / "fora").mkdir(exist_ok=True)
    (outside / "fora" / "draft_content.json").write_text("{}", encoding="utf-8")
    assert style_mod.find_draft_dir(inst, name) is None


def test_find_draft_dir_accepts_plain_name(inst):
    copy_fixture_as(inst, "Meu modelo")
    assert style_mod.find_draft_dir(inst, "Meu modelo") == inst.drafts_dir / "Meu modelo"
