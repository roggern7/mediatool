"""Processo filho curto que mostra o diálogo nativo "Selecionar pasta" do Windows (via tkinter).

Chamado só por `app.services.export_destination` (método 1 do seletor), com lista de argumentos fixa. O diretório
inicial chega pelo stdin (uma linha JSON `{"initial": "..."}`), nunca por argv. A resposta sai pelo stdout em BYTES,
como JSON só com ASCII (`\\uXXXX` para acentos/Unicode), sem depender do codepage do console:
`{"path": "C:/..."}`, `{"path": null}` (cancelado) ou `{"error": "<tipo>"}` (tkinter/Tcl indisponível: o processo pai
tenta o segundo método).
"""

from __future__ import annotations

import json
import sys

TITLE = "Media Tool: escolher pasta de exportação"


def _answer(payload: dict) -> None:
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=True).encode("ascii"))
    sys.stdout.buffer.flush()


def main() -> int:
    try:
        request = json.loads(sys.stdin.buffer.readline().decode("utf-8") or "{}")
    except ValueError:
        request = {}
    initial = request.get("initial") if isinstance(request, dict) else None
    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        # O app roda sem console: a janela dona fica "sempre no topo" e recebe o foco ANTES do diálogo, para ele
        # aparecer em primeiro plano.
        root.attributes("-topmost", True)
        root.lift()
        root.focus_force()
        root.update()
        chosen = filedialog.askdirectory(
            parent=root, initialdir=initial if isinstance(initial, str) and initial else None, mustexist=True,
            title=TITLE,
        )
        root.destroy()
    except Exception as exc:  # noqa: BLE001 - qualquer falha vira uma resposta controlada para o processo pai
        _answer({"error": type(exc).__name__})
        return 1
    _answer({"path": chosen or None})
    return 0


if __name__ == "__main__":
    sys.exit(main())
