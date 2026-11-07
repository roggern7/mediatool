"""Erros tipados do núcleo CapCut.

Toda falha que o runner precisa distinguir vira `CapCutError` com `code` estável, `transient` (vale nova tentativa)
e `message` em pt-BR sem caminhos de segredo. `CapCutBusy` é o caso especial em que o runner põe o job em espera
sem gastar tentativa.
"""

from __future__ import annotations


class CapCutError(Exception):
    """Erro do núcleo CapCut com código estável para o runner/API."""

    code: str = "capcut_error"
    transient: bool = False

    def __init__(self, code: str | None = None, message: str | None = None, *, transient: bool | None = None):
        if code is not None:
            self.code = code
        if transient is not None:
            self.transient = transient
        self.message = message or "Falha no processamento com o CapCut."
        super().__init__(self.message)

    def __repr__(self) -> str:  # pragma: no cover - só diagnóstico
        return f"{type(self).__name__}(code={self.code!r}, transient={self.transient!r}, message={self.message!r})"


class CapCutNotInstalled(CapCutError):
    code = "capcut_not_installed"
    transient = False

    def __init__(self, message: str | None = None):
        super().__init__(None, message or "CapCut Desktop não encontrado neste computador.")


class CapCutBusy(CapCutError):
    """O CapCut está aberto por outra pessoa/sessão: esperar sem gastar tentativa."""

    code = "capcut_busy"
    transient = True

    def __init__(self, message: str | None = None):
        super().__init__(None, message or "CapCut em uso: aguardando ele ser fechado.")


class DesktopBusy(CapCutError):
    """O dono está usando o PC (teclado/mouse recentes ou tomou o foco durante a automação): esperar sem gastar
    tentativa e retomar quando o PC ficar livre."""

    code = "desktop_busy"
    transient = True

    def __init__(self, message: str | None = None):
        super().__init__(None, message or "O PC está em uso: a exportação no CapCut espera ele ficar livre.")


class IsolatedUnavailable(CapCutError):
    """O modo de exportação isolado não pôde ser usado (área de trabalho, thread presa a ela ou UI Automation). Nunca
    cai para o modo interativo: o job falha e o dono escolhe o modo nas configurações."""

    code = "isolated_unavailable"
    transient = False

    def __init__(self, message: str | None = None):
        super().__init__(None, message or (
            "O modo de exportação isolado não pôde ser usado neste computador. Nas configurações das Legendas "
            "CapCut, troque o modo de exportação para \"Interativo\" e tente de novo."))


class CapCutCancelled(CapCutError):
    code = "cancelled"
    transient = False

    def __init__(self, message: str | None = None):
        super().__init__(None, message or "Processamento cancelado.")


class AccessibilityUnavailable(CapCutError):
    code = "accessibility_unavailable"
    transient = False

    def __init__(self, message: str | None = None):
        super().__init__(None, message or "A interface do CapCut não ficou acessível para automação.")


class ForegroundLost(CapCutError):
    """Outra janela tomou a frente na hora do clique: abortado sem clicar (transitório)."""

    code = "foreground_lost"
    transient = True

    def __init__(self, message: str | None = None):
        super().__init__(None, message or "Outra janela ficou na frente do CapCut; clique abortado por segurança.")


class UiTimeout(CapCutError):
    code = "ui_timeout"
    transient = True

    def __init__(self, message: str | None = None):
        super().__init__(None, message or "O CapCut não respondeu a tempo.")


class ExportFailed(CapCutError):
    code = "export_failed"
    transient = True

    def __init__(self, message: str | None = None):
        super().__init__(None, message or "A exportação no CapCut falhou.")
