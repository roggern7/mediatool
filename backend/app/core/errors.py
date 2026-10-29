"""Erro de aplicação com status HTTP e código estável; vira `{detail, code}` na API."""

from __future__ import annotations


class AppError(Exception):
    def __init__(self, status_code: int, code: str, detail: str, extra: dict | None = None):
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        # Campos a mais no corpo do erro (ex.: lista de entradas inválidas); nunca substituem `detail`/`code`.
        self.extra = extra


def not_found(what: str, code: str = "not_found", *, feminine: bool = False) -> AppError:
    return AppError(404, code, f"{what} não {'encontrada' if feminine else 'encontrado'}.")
