"""Orçamento de paginação da listagem do Instagram Web (docs/BATCH_ROBUSTNESS.md, seções 0 e 1 revisadas).

Único lugar dos números. A listagem para por evidência de improdutividade (páginas seguidas sem item compatível,
rendimento baixo), não por um teto fixo pequeno; o teto proporcional é só rede de proteção. Variáveis de ambiente
opcionais (lidas do mapeamento ``env`` do provider, ex.: Settings.env; valores inválidos são ignorados com aviso e os
válidos são limitados à faixa permitida):

- ``MEDIA_TOOL_IG_UNPRODUCTIVE_PAGES`` (all/posts) e ``MEDIA_TOOL_IG_UNPRODUCTIVE_PAGES_FILTERED`` (reels/videos/
  images): 1..100.
- ``MEDIA_TOOL_IG_MIN_PAGES_FOR_YIELD``: 1..10000.
- ``MEDIA_TOOL_IG_MIN_YIELD``: 0..12 (itens compatíveis por página).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass

log = logging.getLogger(__name__)

PAGE_SIZE = 12                       # count observado no navegador e usado hoje
UNPRODUCTIVE_PAGES_LIMIT = 3         # all/posts: páginas seguidas sem item compatível -> filter_exhausted
UNPRODUCTIVE_PAGES_LIMIT_FILTERED = 6  # reels/videos/images
MIN_PAGES_FOR_YIELD = 20             # só avalia o rendimento depois de tantas páginas (total, entre retomadas)
MIN_YIELD = 0.25                     # itens compatíveis por página abaixo disso -> low_yield
SAFETY_FACTOR = {"all": 2, "posts": 2, "images": 4, "reels": 8, "videos": 8}
SLACK_PAGES = 2

FILTERED_CONTENT = frozenset({"reels", "videos", "images"})

ENV_UNPRODUCTIVE = "MEDIA_TOOL_IG_UNPRODUCTIVE_PAGES"
ENV_UNPRODUCTIVE_FILTERED = "MEDIA_TOOL_IG_UNPRODUCTIVE_PAGES_FILTERED"
ENV_MIN_PAGES_FOR_YIELD = "MEDIA_TOOL_IG_MIN_PAGES_FOR_YIELD"
ENV_MIN_YIELD = "MEDIA_TOOL_IG_MIN_YIELD"

STOP_FILTER_EXHAUSTED = "filter_exhausted"
STOP_LOW_YIELD = "low_yield"
STOP_BUDGET_EXHAUSTED = "budget_exhausted"


def max_pages_for(limit: int, content: str) -> int:
    """Teto de segurança proporcional: ceil(limit / PAGE_SIZE) * SAFETY_FACTOR[content] + SLACK_PAGES."""
    factor = SAFETY_FACTOR.get(content, max(SAFETY_FACTOR.values()))
    return math.ceil(max(1, int(limit)) / PAGE_SIZE) * factor + SLACK_PAGES


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


@dataclass(frozen=True)
class ListingBudget:
    unproductive_pages: int = UNPRODUCTIVE_PAGES_LIMIT
    unproductive_pages_filtered: int = UNPRODUCTIVE_PAGES_LIMIT_FILTERED
    min_pages_for_yield: int = MIN_PAGES_FOR_YIELD
    min_yield: float = MIN_YIELD

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None) -> ListingBudget:
        return cls(
            unproductive_pages=int(_env_number(env, ENV_UNPRODUCTIVE, UNPRODUCTIVE_PAGES_LIMIT, 1, 100)),
            unproductive_pages_filtered=int(_env_number(env, ENV_UNPRODUCTIVE_FILTERED,
                                                        UNPRODUCTIVE_PAGES_LIMIT_FILTERED, 1, 100)),
            min_pages_for_yield=int(_env_number(env, ENV_MIN_PAGES_FOR_YIELD, MIN_PAGES_FOR_YIELD, 1, 10000)),
            min_yield=float(_env_number(env, ENV_MIN_YIELD, MIN_YIELD, 0.0, float(PAGE_SIZE), kind=float)),
        )

    def unproductive_limit(self, content: str) -> int:
        return self.unproductive_pages_filtered if content in FILTERED_CONTENT else self.unproductive_pages

    def stop_reason(self, content: str, *, pages: int, items_compatible: int, unproductive_streak: int) -> str | None:
        """Parada adaptativa ao fim de uma página (contadores totais, valem entre retomadas); None = continuar."""
        if unproductive_streak >= self.unproductive_limit(content):
            return STOP_FILTER_EXHAUSTED
        if pages >= self.min_pages_for_yield and items_compatible < self.min_yield * pages:
            return STOP_LOW_YIELD
        return None
