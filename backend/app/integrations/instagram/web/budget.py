"""Números da paginação da listagem do Instagram Web (docs/BATCH_ROBUSTNESS.md, seção 1).

A listagem NÃO tem mais parada por improdutividade (`filter_exhausted`, `low_yield`) nem teto proporcional de páginas
(`max_pages_for`): ela só termina na quantidade pedida ou no fim real do perfil (`has_next_page=false`). Ficam aqui o
tamanho de página e a leitura validada de números do ambiente (usada pelo teto de páginas por hora).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping

log = logging.getLogger(__name__)

PAGE_SIZE = 12                       # count observado no navegador e usado hoje


def _env_number(env: Mapping[str, str] | None, name: str, default: float, low: float, high: float,
                kind: type = int) -> float:
    raw = (env or {}).get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = kind(str(raw).strip())
    except ValueError:
        log.warning("Valor inválido em %s ignorado; usando o padrão", name)
        return default
    if isinstance(value, float) and not math.isfinite(value):
        log.warning("Valor inválido em %s ignorado; usando o padrão", name)
        return default
    clamped = min(max(value, low), high)
    if clamped != value:
        log.warning("%s fora da faixa permitida; usando %s", name, clamped)
    return clamped
