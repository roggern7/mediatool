"""CLI local da sessão do Instagram.

Uso (cwd = backend):
    python -m app.integrations.instagram.session_cli set      # sessionid pelo STDIN (nunca por argv)
    python -m app.integrations.instagram.session_cli status
    python -m app.integrations.instagram.session_cli clear

``set`` lê a primeira linha não vazia do STDIN como sessionid; linhas seguintes opcionais no formato
``chave=valor`` (csrftoken, ds_user_id, mid, ig_did, rur). Com terminal interativo, pede sem eco.
Nenhum valor é ecoado. Diretório de segredos: MEDIA_TOOL_SECRETS_DIR, senão MEDIA_TOOL_DATA_DIR/secrets,
senão MEDIA_TOOL_ROOT/data/secrets, senão <raiz do projeto>/data/secrets.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from app.integrations.instagram.session import (
    LocalInstagramSessionProvider,
    SessionStatus,
    SessionStorageError,
    SessionValidationError,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INVALID = 2

_PROJECT_ROOT = Path(__file__).resolve().parents[4]


def resolve_secrets_dir(env: Mapping[str, str] = os.environ) -> Path:
    if env.get("MEDIA_TOOL_SECRETS_DIR"):
        return Path(env["MEDIA_TOOL_SECRETS_DIR"])
    if env.get("MEDIA_TOOL_DATA_DIR"):
        return Path(env["MEDIA_TOOL_DATA_DIR"]) / "secrets"
    root = Path(env["MEDIA_TOOL_ROOT"]) if env.get("MEDIA_TOOL_ROOT") else _PROJECT_ROOT
    return root / "data" / "secrets"


def _read_input_lines() -> list[str]:
    if sys.stdin is None:
        return []
    if sys.stdin.isatty():
        value = getpass.getpass("sessionid (não será exibido): ")
        return [value]
    raw = sys.stdin.buffer.read()
    text = raw.decode("utf-8-sig", errors="strict")
    raw = b""
    return text.splitlines()


def _parse_input(lines: list[str]) -> tuple[str, dict[str, str]]:
    content = [line.strip("\r\n\t ﻿") for line in lines]
    content = [line for line in content if line]
    if not content:
        raise SessionValidationError("sessionid", "Nenhum sessionid recebido pelo STDIN.")
    sessionid = content[0]
    extra: dict[str, str] = {}
    for line in content[1:]:
        key, sep, value = line.partition("=")
        if not sep:
            raise SessionValidationError("extra", "Linha extra inválida: use o formato chave=valor.")
        extra[key.strip()] = value.strip()
    return sessionid, extra


def _print_status(status: SessionStatus) -> None:
    if status.configured:
        origem = "arquivo protegido" if status.source == "file" else "variável de ambiente"
        quando = f", atualizada em {status.updated_at}" if status.updated_at else ""
        print(f"Sessão configurada ({origem}{quando}).")
    else:
        print("Sessão não configurada.")
    if status.error:
        print(f"Aviso: {status.error}")


class _QuietArgumentParser(argparse.ArgumentParser):
    """Não ecoa argumentos inválidos (alguém pode ter passado o sessionid por engano na linha de comando)."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(
            "Erro: argumentos inválidos. Use apenas set, status ou clear; o sessionid é lido somente do STDIN.",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_INVALID)


def main(argv: list[str] | None = None) -> int:
    parser = _QuietArgumentParser(
        prog="python -m app.integrations.instagram.session_cli",
        description="Gerencia a sessão local do Instagram (o valor é lido somente do STDIN).",
    )
    parser.add_argument("command", choices=["set", "status", "clear"])
    args = parser.parse_args(argv)
    provider = LocalInstagramSessionProvider(resolve_secrets_dir())

    try:
        if args.command == "set":
            try:
                lines = _read_input_lines()
            except UnicodeDecodeError:
                print("Erro: entrada não está em UTF-8.", file=sys.stderr)
                return EXIT_INVALID
            sessionid, extra = _parse_input(lines)
            lines = []
            provider.save(sessionid, extra)
            sessionid = ""
            extra = {}
            print("Sessão salva.")
            _print_status(provider.status())
        elif args.command == "clear":
            provider.clear()
            print("Sessão removida do arquivo protegido.")
            _print_status(provider.status())
        else:
            _print_status(provider.status())
    except SessionValidationError as exc:
        print(f"Erro: {exc.message}", file=sys.stderr)
        return EXIT_INVALID
    except SessionStorageError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
