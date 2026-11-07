"""Sequência completa de um job: transcrever -> blocos -> rascunho -> registrar -> abrir o CapCut -> exportar ->
fechar -> finalizar -> limpar o rascunho. Etapas nomeadas (STAGES) para o runner.
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.integrations.capcut import install as install_mod
from app.integrations.capcut import isolated
from app.integrations.capcut.controller import CapCutController, ExportOptions
from app.integrations.capcut.draft import build_draft, delete_draft, register_draft
from app.integrations.capcut.errors import CapCutBusy, CapCutCancelled, CapCutError, CapCutNotInstalled
from app.integrations.capcut.finalize import (
    MSG_OUTPUT_WRITE,
    discard_job_exports,
    discard_recorded_exports,
    export_problems,
    finalize_output,
    probe_dict,
    record_export,
    safe_file_name,
    unique_path,
)
from app.integrations.capcut.install import CapCutInstall
from app.integrations.capcut.jobspec import USER_CAPCUT_NOTICE, CapCutJobSpec, CapCutResult
from app.integrations.capcut.media import probe, run_tool, tool_path
from app.integrations.capcut.style import (
    DEFAULT_HIGHLIGHT,
    CaptionStyle,
    HighlightOptions,
    default_style,
    find_draft_dir,
)
from app.integrations.capcut.transcribe import Chunk, Word, chunk_words, transcribe_file

logger = logging.getLogger(__name__)

StageCallback = Callable[[str, float | None, str | None], None]


def _deps_ui_ok() -> bool:
    return all(importlib.util.find_spec(m) is not None for m in ("uiautomation", "win32gui", "win32api"))


def _finish_work_dir(work_dir: Path, videos_dir: Path | None) -> None:
    """Job concluído: apaga os exportados anotados de tentativas anteriores e a pasta de trabalho."""
    remaining = discard_recorded_exports(work_dir, videos_dir)
    if remaining:
        logger.warning("Job CapCut: %s arquivo(s) intermediário(s) não puderam ser apagados", len(remaining))
        return
    shutil.rmtree(work_dir, ignore_errors=True)


def _prune_work_dir(work_dir: Path) -> None:
    """Falha: a pasta de trabalho só fica se tiver o registro dos exportados (limpo na próxima tentativa)."""
    try:
        work_dir.rmdir()
    except OSError:
        pass


def clip_range(spec: CapCutJobSpec, duration: float) -> tuple[float, float] | None:
    """(início, duração) do trecho da prévia dentro do vídeo; None = vídeo inteiro. Início além do fim: os últimos
    segundos do vídeo."""
    if spec.clip_duration is None:
        return None
    length = float(spec.clip_duration)
    start = max(0.0, float(spec.clip_start or 0.0))
    if start + 0.5 > duration:
        start = max(0.0, duration - length)
    length = min(length, duration - start)
    if length < 0.5:
        raise CapCutError("preview_out_of_range", "O trecho pedido para a prévia está fora do vídeo.")
    return start, length


def _clip_audio(video: Path, clip: tuple[float, float], out: Path) -> Path:
    """Só o áudio do trecho (WAV), para transcrever apenas ele (tempos relativos ao início do trecho)."""
    result = run_tool([tool_path("ffmpeg"), "-nostdin", "-v", "error", "-y", "-ss", f"{clip[0]:.3f}", "-t",
                       f"{clip[1]:.3f}", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(out)], timeout=300)
    if result.returncode != 0 or not out.is_file():
        out.unlink(missing_ok=True)
        raise CapCutError("transcription_failed", "Não foi possível separar o áudio do trecho da prévia.")
    return out


def sample_chunks(text: str, duration: float, *, max_words: int, max_chars: int) -> list[Chunk]:
    """Sem fala na prévia: o texto de exemplo distribuído no trecho com tempos sintéticos."""
    parts = text.split()
    if not parts or duration <= 0.6:
        return []
    step = (duration - 0.6) / len(parts)
    words = [Word(0.3 + i * step, 0.3 + (i + 1) * step - 0.02, w) for i, w in enumerate(parts)]
    return chunk_words(words, max_words=max_words, max_chars=max_chars)


def wait_export_complete(path: Path, *, expected_duration: float, cancel: threading.Event,
                         timeout: float = 180.0, interval: float = 2.0) -> None:
    """Espera o MP4 exportado ficar finalizado e íntegro (`export_problems` vazio). Não ficou no prazo:
    CapCutError('output_invalid', transitório) sem fechar nada às pressas."""
    deadline = time.monotonic() + timeout
    problems: list[str] = []
    while True:
        if cancel.is_set():
            raise CapCutCancelled()
        problems = export_problems(path, expected_duration=expected_duration)
        if not problems:
            return
        if time.monotonic() >= deadline:
            raise CapCutError("output_invalid", "O vídeo exportado pelo CapCut não ficou íntegro: "
                              + "; ".join(problems) + ".", transient=True)
        if cancel.wait(interval):
            raise CapCutCancelled()


def _transcribe(spec: CapCutJobSpec, video: Path, has_audio: bool, clip: tuple[float, float] | None, *,
                cancel: threading.Event, stage: StageCallback,
                transcriber: Callable[..., list[Word]] | None) -> list[Word]:
    """Palavras do vídeo (só do trecho `clip`, quando há), com o andamento da etapa `transcribing`."""
    stage("transcribing", 0.0, f"Transcrevendo (modelo {spec.whisper_model})")
    if has_audio:
        source = video
        if clip is not None:
            Path(spec.work_dir).mkdir(parents=True, exist_ok=True)
            source = _clip_audio(video, clip, Path(spec.work_dir) / "clip.wav")
        try:
            words = (transcriber or transcribe_file)(
                source, model=spec.whisper_model, language=spec.language, models_dir=Path(spec.models_dir),
                cancel=cancel, progress=lambda p: stage("transcribing", p, None))
        finally:
            if source != video:
                source.unlink(missing_ok=True)
    else:
        words = []
    stage("transcribing", 1.0, f"{len(words)} palavras")
    return words


def transcribe_only(spec: CapCutJobSpec, *, cancel: threading.Event, on_stage: StageCallback,
                    transcriber: Callable[..., list[Word]] | None = None) -> list[dict[str, Any]]:
    """Só a transcrição (revisão das legendas): mesmo tratamento de trecho e andamento do `process_video`, sem
    CapCut. Palavras em dicionário (com `seg` e `doubt`)."""
    if cancel.is_set():
        raise CapCutCancelled()
    video = Path(spec.video_path)
    if not video.is_file():
        raise CapCutError("source_missing", "O vídeo original não foi encontrado.")
    info = probe(video)
    clip = clip_range(spec, info.duration)
    words = _transcribe(spec, video, info.has_audio, clip, cancel=cancel, stage=on_stage, transcriber=transcriber)
    if cancel.is_set():
        raise CapCutCancelled()
    return [w.to_dict() for w in words]


def process_video(spec: CapCutJobSpec, *, cancel: threading.Event, on_stage: StageCallback,
                  on_transcript: Callable[[list[dict[str, Any]]], None] | None = None,
                  install: CapCutInstall | None = None, controller_factory: Callable[..., Any] | None = None,
                  transcriber: Callable[..., list[Word]] | None = None,
                  running: Callable[[], list[int]] | None = None) -> CapCutResult:
    current = {"stage": "transcribing"}

    def stage(name: str, progress: float | None = None, detail: str | None = None) -> None:
        current["stage"] = name
        on_stage(name, progress, detail)

    def check() -> None:
        if cancel.is_set():
            raise CapCutCancelled()

    check()
    video = Path(spec.video_path)
    if not video.is_file():
        raise CapCutError("source_missing", "O vídeo original não foi encontrado.")
    info = probe(video)
    install = install or install_mod.detect()
    if install is None:
        raise CapCutNotInstalled()
    if controller_factory is None and not _deps_ui_ok():
        raise CapCutError("deps_missing", "uiautomation/pywin32 não estão instalados.")
    running = running or install_mod.is_running
    # Modo de exportação: `isolated` (padrão: área de trabalho separada, sem mouse/teclado/tela) ou `interactive`.
    # Se o isolado não puder ser usado, o job falha com `isolated_unavailable` (nunca cai para o interativo).
    export_mode = str(getattr(spec, "export_mode", None) or isolated.DEFAULT_EXPORT_MODE)
    if export_mode not in isolated.EXPORT_MODES:
        raise CapCutError("invalid_export_mode", "Modo de exportação desconhecido. Escolha \"Isolado\" ou "
                          "\"Interativo\" nas configurações da Edição CapCut.")
    warnings: list[str] = []
    clip = clip_range(spec, info.duration)
    expected_duration = clip[1] if clip else info.duration

    # 1. transcrição (reaproveitada nas retentativas)
    if spec.transcript is not None:
        words = [Word.from_dict(w) for w in spec.transcript]
        stage("transcribing", 1.0, "Transcrição reaproveitada")
    else:
        words = _transcribe(spec, video, info.has_audio, clip, cancel=cancel, stage=stage, transcriber=transcriber)
        if on_transcript is not None:
            on_transcript([w.to_dict() for w in words])
    word_dicts = [w.to_dict() for w in words]
    check()

    chunks = chunk_words(words, max_words=spec.max_words, max_chars=spec.max_chars)
    if not chunks and spec.sample_text:
        chunks = sample_chunks(spec.sample_text, expected_duration, max_words=spec.max_words, max_chars=spec.max_chars)
        if chunks:
            warnings.append("Sem fala no trecho: usado o texto de exemplo.")
    output_dir = Path(spec.output_dir)
    work_dir = Path(spec.work_dir)
    if not chunks:
        stage("finalizing", 0.0, "Sem fala: copiando o vídeo sem legendas")
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            final = unique_path(output_dir, safe_file_name(spec.output_name))
            shutil.copy2(video, final)
        except OSError as exc:
            raise CapCutError("output_write_failed", MSG_OUTPUT_WRITE, transient=True) from exc
        warnings.append("Nenhuma fala detectada: o vídeo foi copiado sem legendas.")
        stage("finalizing", 1.0, None)
        _finish_work_dir(work_dir, install.default_videos_dir)
        return CapCutResult(output_path=final, draft_name=spec.draft_name, draft_kept=False, words=word_dicts,
                            chunks=0, probe=probe_dict(probe(final)), warnings=warnings)

    # 2. rascunho (só com o CapCut fechado). Antes: CapCut que sobrou na área isolada (queda do Media Tool) é nosso e
    # é fechado com o fechamento seguro; um CapCut com janela na área normal do dono continua sendo `capcut_busy`.
    if controller_factory is None:
        try:
            warnings.extend(isolated.recover_leftover(
                install, state_dir=Path(spec.work_dir).parent.parent, cancel=cancel, running=running,
                progress=lambda d: stage(current["stage"], None, d)))
        except CapCutError:
            stage("waiting_capcut", None, "CapCut em uso")
            raise
    if running():
        stage("waiting_capcut", None, "CapCut em uso")
        raise CapCutBusy()
    stage("drafting", 0.0, f"{len(chunks)} legendas")
    style = CaptionStyle.from_dict(spec.style) if spec.style else default_style(install)
    base_draft = find_draft_dir(install, style.source_draft) if style.source_draft else None
    if style.source_draft and base_draft is None:
        warnings.append("O rascunho do modelo não existe mais; usado o rascunho-base padrão.")
    highlight = (HighlightOptions(color=spec.highlight_color or style.highlight_color or DEFAULT_HIGHLIGHT)
                 if spec.highlight and style.highlight_enabled is not False else None)
    work_dir.mkdir(parents=True, exist_ok=True)
    state_dir = work_dir.parent.parent  # <data_dir>/capcut
    # Exportados de uma tentativa anterior deste job (falhou depois de exportar): apagados antes de exportar de novo.
    if discard_recorded_exports(work_dir, install.default_videos_dir):
        warnings.append("Um arquivo intermediário de uma tentativa anterior não pôde ser apagado.")
    # Exportação que um CapCut órfão terminou depois de uma queda (não anotada): também é deste job (mesmo MT_...).
    if discard_job_exports(install.default_videos_dir, spec.draft_name):
        warnings.append("Um arquivo intermediário de uma tentativa anterior não pôde ser apagado.")

    draft_dir: Path | None = None
    controller: Any = None
    success = False
    try:
        if controller_factory is not None:
            factory = controller_factory
        elif export_mode == "isolated":
            factory = isolated.open_controller
        else:
            factory = CapCutController
        controller = factory(install, state_dir=state_dir, progress=lambda d: stage(current["stage"], None, d),
                             cancel=cancel)
        # O PC precisa estar livre (sem teclado/mouse do dono há alguns segundos) antes de mexer no CapCut;
        # senão DesktopBusy: o runner põe o job em espera sem gastar tentativa e sem criar rascunho.
        ensure_free = getattr(controller, "ensure_desktop_free", None)
        if callable(ensure_free):
            try:
                ensure_free()
            except CapCutError:
                stage("waiting_capcut", None, "PC em uso")
                raise
        options = ExportOptions(resolution=spec.resolution, bitrate=spec.bitrate, fps=spec.fps,
                                source_width=info.width, source_height=info.height, source_fps=info.fps)
        draft_dir = build_draft(install, name=spec.draft_name, video=video, chunks=chunks, style=style,
                                highlight=highlight, base_draft=base_draft, info=info, running=running,
                                fps=options.fps_value(), clip=clip)
        register_draft(install, draft_dir, template_name=style.source_draft, running=running)
        stage("drafting", 1.0, None)
        check()

        stage("launching", 0.0, "Abrindo o CapCut")
        controller.launch()
        controller.wait_home()
        stage("opening", 0.0, "Abrindo o rascunho")
        controller.open_draft(spec.draft_name)
        stage("exporting", 0.0, "Exportando no CapCut")
        exported = Path(controller.export(options))
        warnings.extend(getattr(controller, "warnings", []) or [])
        created = bool(getattr(controller, "last_export_created", False))
        # Só fecha o projeto/CapCut depois de o MP4 estar FINALIZADO e íntegro (o CapCut pode ainda estar gravando).
        stage("exporting", None, "Conferindo se o MP4 foi finalizado")
        wait_export_complete(exported, expected_duration=expected_duration, cancel=cancel)
        controller.close_project()
        controller.shutdown()
        stage("exporting", 1.0, None)

        stage("finalizing", 0.0, "Conferindo o vídeo exportado")
        report = finalize_output(exported, video, output_dir, spec.output_name, spec.keep_original_audio,
                                 expected_short_side=options.expected_short_side(), expected_fps=options.fps_value(),
                                 capcut_videos_dir=install.default_videos_dir, created_by_job=created,
                                 expected_duration=clip[1] if clip else None)
        warnings.extend(report.warnings)
        stage("finalizing", 1.0, None)
        success = True
    finally:
        if controller is not None:
            try:
                controller.shutdown()
            except Exception:  # noqa: BLE001 - limpeza não pode esconder o erro original
                logger.warning("Job CapCut %s: falha ao fechar o CapCut", spec.job_id, exc_info=True)
            close_session = getattr(controller, "close_session", None)
            if callable(close_session):
                try:
                    close_session()  # modo isolado: encerra a thread presa à área e fecha a área de trabalho
                except Exception:  # noqa: BLE001
                    logger.warning("Job CapCut %s: falha ao fechar a área isolada", spec.job_id, exc_info=True)
            if getattr(controller, "user_capcut_attempt", False) is True:
                warnings.append(USER_CAPCUT_NOTICE)
                pending = sys.exc_info()[1]
                if pending is not None:
                    try:
                        pending.user_capcut_attempt = True  # o runner pausa a fila também em falha/cancelamento
                    except Exception:  # noqa: BLE001
                        pass
            target = getattr(controller, "export_target", None)
            if not success and target:
                try:
                    record_export(work_dir, spec.draft_name, Path(target))
                except Exception:  # noqa: BLE001
                    logger.warning("Job CapCut %s: falha ao anotar o exportado", spec.job_id, exc_info=True)
        if not success:
            # Cancelado/falhou: com o CapCut já fechado, o vídeo intermediário deste job (MT_...) é apagado agora,
            # não só na próxima tentativa (um job cancelado e nunca retomado deixaria a sobra para sempre). Se ainda
            # estiver preso, fica anotado em exports.json para a próxima tentativa.
            try:
                if not discard_job_exports(install.default_videos_dir, spec.draft_name):
                    discard_recorded_exports(work_dir, install.default_videos_dir)
            except Exception:  # noqa: BLE001 - limpeza não pode esconder o erro original
                logger.warning("Job CapCut %s: falha ao apagar o intermediário", spec.job_id, exc_info=True)
            _prune_work_dir(work_dir)
        if draft_dir is not None and (not success or not spec.keep_draft):
            if success:
                stage("cleaning", 0.0, "Removendo o rascunho temporário")
            try:
                delete_draft(install, spec.draft_name, running=running)
            except CapCutBusy:
                warnings.append("O rascunho não pôde ser removido (CapCut aberto).")
            except Exception:  # noqa: BLE001
                logger.warning("Job CapCut %s: falha ao remover o rascunho", spec.job_id, exc_info=True)
            if success:
                stage("cleaning", 1.0, None)
    _finish_work_dir(work_dir, install.default_videos_dir)
    return CapCutResult(output_path=report.output_path, draft_name=spec.draft_name,
                        draft_kept=bool(spec.keep_draft), words=word_dicts, chunks=len(chunks),
                        probe=report.probe, warnings=warnings)
