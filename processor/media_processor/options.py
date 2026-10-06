from __future__ import annotations

import math
from dataclasses import dataclass, fields


@dataclass(frozen=True)
class ProcessingOptions:
    metadata_cleanup: bool = True
    trim_start: float = 0.5
    mirror: bool = True
    speed: float = 1.05
    auto_adjust: bool = True
    reencode: bool = True

    def __post_init__(self) -> None:
        for name in ("metadata_cleanup", "mirror", "auto_adjust", "reencode"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} deve ser booleano")
        for name in ("trim_start", "speed"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} deve ser numérico")
            object.__setattr__(self, name, float(value))
        if self.trim_start < 0:
            raise ValueError("trim_start deve ser >= 0")
        if self.speed <= 0:
            raise ValueError("speed deve ser > 0")

    @classmethod
    def from_params(cls, params: dict) -> "ProcessingOptions":
        """Cria a partir de PresetParams. Chaves ausentes usam o padrão; chaves desconhecidas -> ValueError."""
        known = {f.name for f in fields(cls)}
        extra = set(params) - known
        if extra:
            raise ValueError(f"parâmetros desconhecidos: {sorted(extra)}")
        return cls(**{k: params[k] for k in known if k in params})
