"""Controlador único de recuperação das pausas do Instagram (docs/BATCH_ROBUSTNESS.md, seções 7 e 8).

- Estado persistido em app_state (``instagram_recovery_state``): degrau de backoff por motivo, ``since`` e
  ``next_attempt_at``. Sobrevive a reinícios: uma instância nova retoma do estado salvo.
- Degraus: ``request_auth_failed`` 15m, 30m, 1h, 2h, 4h (teto 4h); ``rate_limited`` 15m, 30m, 1h, 2h, 4h (teto 6h);
  ``action_blocked`` 1h, 2h, 4h, 8h (teto 12h); ``checkpoint_required`` e ``session_invalid`` forte: 6h fixo e
  "ação necessária".
- Seção 7: o 1o degrau de ``request_auth_failed`` aparece como ``auth_check``; a 2a verificação seguida com recusa
  de autenticação, sem sucesso autenticado entre elas, aparece como ``session_invalid`` (fraca: segue os degraus,
  sem "ação necessária"). O contador fica em ``instagram_auth_fail_streak`` e é zerado por qualquer sucesso.
- Durante a pausa nada vai ao Instagram; no vencimento SÓ o controlador faz UMA verificação mínima (a do teste de
  sessão: bootstrap + 1 consulta count=1, sem download). Sucesso: limpa a pausa, zera o backoff e o contador e acorda
  a listagem (retoma do checkpoint). Falha do mesmo tipo (ou mais leve): próximo degrau. Falha mais grave: troca o
  motivo. Rede indisponível: reagenda no mesmo degrau.
- Nada aqui apaga a sessão salva, remove cookies ou chama logout; nada tenta contornar checkpoint/desafio.
- Seção 9 (A2), janela de prova: depois de limpar uma pausa por verificação, o degrau fica guardado
  (``instagram_recovery_probation``) por 2x a última pausa; nova falha do mesmo motivo nessa janela sobe um degrau em
  vez de voltar ao 1o. Fora da janela, zera. Vale também para ``request_auth_failed``: o degrau só define a duração;
  virar ``session_invalid`` (fraca) segue só a seção 7 (``escalated``: 2 recusas seguidas sem sucesso entre elas).
  Cada ``rate_limited`` liga o ritmo lento da listagem por 1 h no portão (prazo persistido em
  ``instagram_slow_pace_until`` e reaplicado em ``reload``).
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.db.connection import parse_utc
from app.integrations.instagram.downloader import InstagramError
from app.services.deadline import CallInProgress, DeadlineExceeded, run_with_deadline
from app.services.instagram_gate import InstagramPaused

if TYPE_CHECKING:
    from app.jobs.worker import Worker
    from app.services.context import AppContext

logger = logging.getLogger("media_tool.instagram_recovery")

STATE_KEY = "instagram_recovery_state"
AUTH_FAIL_STREAK_KEY = "instagram_auth_fail_streak"
PROBATION_KEY = "instagram_recovery_probation"
SLOW_PACE_KEY = "instagram_slow_pace_until"
PROBATION_FACTOR = 2.0          # janela de prova = 2x a última pausa
SLOW_PACE_SECONDS = 3600.0      # ritmo lento da listagem depois de um 429

_MIN = 60.0
_HOUR = 3600.0
LADDERS: dict[str, tuple[float, ...]] = {
    "request_auth_failed": (15 * _MIN, 30 * _MIN, _HOUR, 2 * _HOUR, 4 * _HOUR),
    "rate_limited": (15 * _MIN, 30 * _MIN, _HOUR, 2 * _HOUR, 4 * _HOUR, 6 * _HOUR),
    "action_blocked": (_HOUR, 2 * _HOUR, 4 * _HOUR, 8 * _HOUR, 12 * _HOUR),
    "checkpoint_required": (6 * _HOUR,),
    "session_invalid": (6 * _HOUR,),
}
# Motivos que pedem ação do usuário (a verificação automática continua a cada 6 h, sem contornar nada).
ACTION_REQUIRED_KEYS = ("checkpoint_required", "session_invalid")
SEVERITY = {"rate_limited": 1, "request_auth_failed": 2, "action_blocked": 3, "session_invalid": 4,
            "checkpoint_required": 5}
# Código de erro -> motivo da recuperação. session_expired (documento redireciona para login) é sessão inválida forte.
_CODE_KEYS = {
    "request_auth_failed": "request_auth_failed",
    "rate_limited": "rate_limited",
    "action_blocked": "action_blocked",
    "checkpoint_required": "checkpoint_required",
    "session_invalid": "session_invalid",
    "session_expired": "session_invalid",
}
RECOVERY_CODES = frozenset(_CODE_KEYS)
# `result` do teste de sessão -> motivo (quando o `code` não é um dos códigos acima).
_RESULT_KEYS = {
    "rate_limited": "rate_limited",
    "restricted": "action_blocked",
    "checkpoint_required": "checkpoint_required",
    "invalid": "session_invalid",
    "expired": "session_invalid",
}
_NETWORK = ("timeout", "network_error")

# Escopo das pausas: "session" bloqueia só o que usa a sessão (listagem, prévia de perfil, renovação de URL, teste
# automático); "instagram" bloqueia também novos downloads de ingest. Nenhuma bloqueia processamento local.
_SESSION_SCOPE = ("request_auth_failed", "auth_check", "session_invalid", "session_expired", "checkpoint_required")
_INSTAGRAM_SCOPE = ("rate_limited", "action_blocked")


def pause_scope(reason: str | None) -> str | None:
    """Escopo da pausa: ``"session"``, ``"instagram"`` ou ``None`` (motivo desconhecido/sem pausa)."""
    if reason in _SESSION_SCOPE:
        return "session"
    if reason in _INSTAGRAM_SCOPE:
        return "instagram"
    return None


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def step_seconds(key: str, level: int) -> float:
    ladder = LADDERS[key]
    return ladder[min(max(0, level), len(ladder) - 1)]


@dataclass(frozen=True)
class RecoveryState:
    key: str              # motivo da recuperação (chave de LADDERS)
    level: int            # degrau atual (0-based)
    since: str            # início da sequência de pausas (ISO UTC)
    next_attempt_at: str  # próxima verificação automática (ISO UTC)
    # request_auth_failed: 2a recusa seguida sem sucesso entre elas (seção 7) -> aparece como session_invalid fraca.
    escalated: bool = False

    @property
    def pause_reason(self) -> str:
        if self.key == "request_auth_failed":
            return "session_invalid" if self.escalated else "auth_check"
        return self.key

    @property
    def scope(self) -> str | None:
        return pause_scope(self.pause_reason)

    @property
    def action_required(self) -> bool:
        return self.key in ACTION_REQUIRED_KEYS

    def to_json(self) -> str:
        return json.dumps({"key": self.key, "level": self.level, "since": self.since,
                           "next_attempt_at": self.next_attempt_at, "reason": self.pause_reason,
                           "scope": self.scope, "escalated": self.escalated})


class InstagramRecoveryController:
    """Um por worker. ``clock`` (UTC aware) e ``verify`` são injetáveis; sem eles usa ``worker.clock`` e o teste de
    sessão do downloader com prazo duro."""

    def __init__(self, ctx: AppContext, worker: Worker, *, clock: Callable[[], datetime] | None = None,
                 verify: Callable[[], Any] | None = None) -> None:
        self.ctx = ctx
        self.worker = worker
        self._clock = clock
        self.verify = verify
        self._lock = threading.RLock()
        self._verify_epoch: int | None = None   # época da pausa em que a verificação corrente foi concedida
        # Carregado em reload() (worker.load_pause_state, depois das migrations do startup).
        self._state: RecoveryState | None = None

    # ---- estado ------------------------------------------------------------------------------------------------
    def now(self) -> datetime:
        return (self._clock or self.worker.clock)()

    def _load(self) -> RecoveryState | None:
        raw = self.ctx.app_settings.get_state(STATE_KEY)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            key, level = data["key"], int(data["level"])
            since, next_at = str(data["since"]), str(data["next_attempt_at"])
        except (ValueError, TypeError, KeyError):
            return None
        if key not in LADDERS or level < 0 or parse_utc(next_at) is None:
            return None
        escalated = data.get("escalated")
        if not isinstance(escalated, bool):   # estado salvo antes do campo: degrau > 0 era a escalada
            escalated = key == "request_auth_failed" and level > 0
        return RecoveryState(key, level, since, next_at, escalated)

    def reload(self) -> RecoveryState | None:
        with self._lock:
            self._state = self._load()
            # Ritmo lento depois de um 429 sobrevive a reinício: reaplica o restante no portão.
            until = parse_utc(self.ctx.app_settings.get_state(SLOW_PACE_KEY))
            if until is not None:
                left = (until - self.now()).total_seconds()
                if left > 0:
                    self._slow_gate(left)
                else:
                    self.ctx.app_settings.set_state(SLOW_PACE_KEY, None)
            return self._state

    def _slow_gate(self, seconds: float) -> None:
        gate = getattr(self.ctx, "instagram_gate", None)
        slow = getattr(gate, "slow_listing", None)
        if slow is not None:
            slow(seconds)

    def _start_slow_pace(self, now: datetime) -> None:
        self.ctx.app_settings.set_state(SLOW_PACE_KEY, _iso(now + timedelta(seconds=SLOW_PACE_SECONDS)))
        self._slow_gate(SLOW_PACE_SECONDS)

    def _probation(self, now: datetime) -> tuple[str, int, str] | None:
        """(motivo, degrau, since) da janela de prova vigente; vencida ou inválida, apaga e devolve None."""
        raw = self.ctx.app_settings.get_state(PROBATION_KEY)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            key, level, since, until = data["key"], int(data["level"]), str(data["since"]), parse_utc(data["until"])
        except (ValueError, TypeError, KeyError):
            key, level, since, until = None, -1, "", None
        if key not in LADDERS or level < 0 or until is None or now >= until:
            self.ctx.app_settings.set_state(PROBATION_KEY, None)
            return None
        return key, level, since

    def _start_probation(self, state: RecoveryState, now: datetime) -> None:
        until = now + timedelta(seconds=PROBATION_FACTOR * step_seconds(state.key, state.level))
        self.ctx.app_settings.set_state(PROBATION_KEY, json.dumps(
            {"key": state.key, "level": state.level, "since": state.since, "until": _iso(until)}))

    def state(self) -> RecoveryState | None:
        return self._state

    def awaiting_verification(self) -> bool:
        state = self._state
        if state is None:
            return False
        next_at = parse_utc(state.next_attempt_at)
        return next_at is None or next_at <= self.now()

    def reset(self) -> None:
        """Esquece o backoff (pausa liberada). O contador de recusas de autenticação só zera com sucesso."""
        with self._lock:
            self._state = None
            self.ctx.app_settings.set_state(STATE_KEY, None)
            self.ctx.app_settings.set_state(PROBATION_KEY, None)

    def _auth_streak(self) -> int:
        raw = self.ctx.app_settings.get_state(AUTH_FAIL_STREAK_KEY)
        try:
            return max(0, int(raw)) if raw else 0
        except (TypeError, ValueError):
            return 0

    # ---- eventos -----------------------------------------------------------------------------------------------
    def on_failure(self, code: str | None, *, verification: bool = False) -> bool:
        """Falha classificada de uma requisição (ou da verificação). Retorna True se o código pausa."""
        key = _CODE_KEYS.get(code or "")
        if key is None:
            return False
        with self._lock:
            now = self.now()
            if key == "rate_limited":
                self._start_slow_pace(now)
            streak = self._auth_streak()   # recusas de autenticação seguidas sem sucesso entre elas (seção 7)
            state = self._state
            escalated_before = False
            next_at = parse_utc(state.next_attempt_at) if state is not None else None
            in_cooldown = state is not None and not verification and next_at is not None and now < next_at
            if state is not None and in_cooldown:
                # Requisições que já estavam em voo quando a pausa começou: mesma verificação, sem subir degrau.
                if SEVERITY[key] <= SEVERITY[state.key]:
                    logger.info("Recusa do Instagram (%s) durante a pausa vigente (%s); mantida", key,
                                state.pause_reason)
                    return True
                level, since = self._start_level(key), state.since
            elif state is not None:
                # Verificação após o vencimento: mesmo tipo (ou mais leve) sobe um degrau; mais grave troca o motivo.
                if key == state.key or SEVERITY[key] < SEVERITY[state.key]:
                    key, level = state.key, state.level + 1
                    escalated_before = state.escalated
                else:
                    level = self._start_level(key)
                since = state.since
            else:
                probation = self._probation(now)
                if probation is not None and probation[0] == key:
                    # Janela de prova: mesmo motivo logo depois de uma verificação ok sobe um degrau.
                    level, since = probation[1] + 1, probation[2]
                else:
                    level, since = self._start_level(key), _iso(now)
                self.ctx.app_settings.set_state(PROBATION_KEY, None)
            escalated = key == "request_auth_failed" and (streak >= 1 or escalated_before)
            self._apply(key, level, since, now, keep_longer=in_cooldown, escalated=escalated,
                        auth_streak=streak + 1 if key == "request_auth_failed" else None)
        return True

    def _start_level(self, key: str) -> int:
        # Seção 7 (c): recusa de autenticação depois de outra verificação recusada, sem sucesso entre elas.
        return self._auth_streak() if key == "request_auth_failed" else 0

    def _apply(self, key: str, level: int, since: str, now: datetime, *, keep_longer: bool,
               escalated: bool = False, auth_streak: int | None = None) -> None:
        until = now + timedelta(seconds=step_seconds(key, level))
        if keep_longer:
            current = parse_utc(self.ctx.app_settings.get_state("instagram_downloads_paused_until"))
            if current is not None and current > until:
                until = current
        if auth_streak is not None:
            self.ctx.app_settings.set_state(AUTH_FAIL_STREAK_KEY, str(auth_streak))
        state = RecoveryState(key, level, since, _iso(until), escalated)
        self.ctx.app_settings.set_state(STATE_KEY, state.to_json())
        self.worker.set_pause(until, state.pause_reason)
        # Só depois de a pausa nova estar publicada: quem consulta ``pause_state`` sem este lock vê, até aqui, o
        # estado antigo (verificação pendente => pausa vigente) e nunca uma janela "vencida e sem verificação".
        self._state = state
        logger.warning("Instagram em pausa (motivo: %s, degrau %d); próxima verificação automática em %s",
                       state.pause_reason, level + 1, state.next_attempt_at)

    # ---- verificação no vencimento -----------------------------------------------------------------------------
    def tick(self) -> str | None:
        """Chamado periodicamente. No vencimento faz UMA verificação. Retorna None (nada a fazer), "ok",
        "failed", "network" ou "busy"."""
        with self._lock:
            # Estado e época do portão lidos juntos: toda publicação de pausa pelo controlador passa por este lock.
            state = self._state
            if state is None or not self.awaiting_verification() or self.worker.stopping:
                return None
            self._verify_epoch = self._gate_epoch()
        outcome, code = self._verify()   # fora do lock: a verificação pode levar até o prazo do teste de sessão
        with self._lock:
            if self._state != state:
                return None   # liberada/alterada durante a verificação (ex.: sessão salva)
            if outcome == "busy":
                return "busy"   # outro teste de sessão em andamento; tenta no próximo ciclo
            if outcome == "ok":
                logger.warning("Verificação automática do Instagram bem-sucedida; pausa encerrada")
                self.worker.note_auth_success()
                self.worker.clear_pause(only_reason=state.pause_reason)
                self._start_probation(state, self.now())   # depois do clear_pause (reset apaga a janela)
                self.ctx.wake_import_runner()
                return "ok"
            if outcome == "network":
                logger.warning("Verificação automática sem rede; reagendada no mesmo degrau")
                self._apply(state.key, state.level, state.since, self.now(), keep_longer=False,
                            escalated=state.escalated)
                return "network"
            self.on_failure(code or state.key, verification=True)
            return "failed"

    def _gate_epoch(self) -> int | None:
        epoch = getattr(getattr(self.ctx, "instagram_gate", None), "pause_epoch", None)
        return epoch() if epoch is not None else None

    def _verification_pass(self):
        """Passagem própria da verificação no portão (só ela é admitida durante a pausa vencida), presa à época lida
        em ``tick``; uma pausa publicada depois invalida a passagem."""
        gate = getattr(self.ctx, "instagram_gate", None)
        grant = getattr(gate, "verification_pass", None)
        epoch = self._verify_epoch
        return grant(epoch) if grant is not None and epoch is not None else nullcontext()

    def _verify(self) -> tuple[str, str | None]:
        # Sem segurar o portão aqui: cada requisição ao Instagram ocupa a própria vaga (portão não reentrante).
        try:
            with self._verification_pass():
                result = (self.verify or self._default_verify)()
        except CallInProgress:
            return "busy", None
        except InstagramPaused:
            return "busy", None   # a pausa mudou durante a verificação; o próximo ciclo decide
        except DeadlineExceeded:
            return "network", None
        except InstagramError as exc:
            if exc.code in _NETWORK:
                return "network", None
            return "failed", exc.code if exc.code in RECOVERY_CODES else None
        except Exception:  # noqa: BLE001 - a verificação nunca derruba o worker
            logger.exception("Erro inesperado na verificação automática do Instagram")
            return "network", None
        result_name = getattr(result, "result", None)
        code = getattr(result, "code", None)
        if getattr(result, "ok", False) and result_name in (None, "valid"):
            return "ok", None
        if result_name in _NETWORK or code in _NETWORK:
            return "network", None
        if code in RECOVERY_CODES:
            return "failed", code
        return "failed", _RESULT_KEYS.get(result_name or "")

    def _default_verify(self) -> Any:
        def call() -> Any:
            with self._verification_pass():   # a thread do prazo também recebe a passagem
                return self.ctx.downloader_factory().test_session()

        return run_with_deadline(call, self.ctx.settings.session_test_timeout, name="instagram-recovery-check",
                                 lock=self.ctx.session_test_lock)
