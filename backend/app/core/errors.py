"""Erro de aplicação com status HTTP e código estável; vira `{detail, code}` na API."""

from __future__ import annotations


class AppError(Exception):
    def __init__(self, status_code: int, code: str, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


def not_found(what: str, code: str = "not_found") -> AppError:
    return AppError(404, code, f"{what} não encontrado.")
