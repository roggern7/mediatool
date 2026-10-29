from __future__ import annotations

import math
from dataclasses import dataclass, fields


# Faixas aceitas dos ajustes opcionais (as mesmas do PresetParams do backend).
OPTIONAL_RANGES = {
    "zoom": (0.0, 10.0),
    "brightness": (-0.10, 0.10),
    "contrast": (0.90, 1.10),
    "saturation": (0.80, 1.20),
    "trim_end": (0.0, 30.0),
    "pitch_shift": (-3.0, 3.0),
    "grain": (0.0, 4.0),
    "max_duration": (0.0, 86400.0),
    "cut_end": (0.0, 86400.0),
}


@dataclass(frozen=True)
class ProcessingOptions:
    metadata_cleanup: bool = True
    trim_start: float = 0.5
    mirror: bool = True
    speed: float = 1.05
    auto_adjust: bool = True
    reencode: bool = True
    # Ajustes opcionais (neutro = desligado). Com todos neutros o comando gerado é o mesmo de antes de existirem.
    zoom: float = 0.0  # % de zoom central
    brightness: float = 0.0
    contrast: float = 1.0
    saturation: float = 1.0
    trim_end: float = 0.0  # segundos cortados do final (ignorado em vídeos curtos e em imagens)
    pitch_shift: float = 0.0  # % de tom do áudio
    grain: float = 0.0  # intensidade da granulação
    # Duração alvo da saída em segundos (0 = desligado): corta na linha do tempo de saída, depois da velocidade.
    # Vídeo cuja saída natural já cabe no alvo não é cortado nem prolongado.
    max_duration: float = 0.0
    sharpen: bool = False  # nitidez leve (cas) só na luma
    # Com velocidade, escala também o início do áudio (o atempo mantém o PTS inicial): áudio que começa depois do
    # vídeo continua sincronizado. Desligado = comando idêntico ao de antes (presets).
    audio_sync_start: bool = False
    # Recorte manual (edição): fim do trecho na linha do tempo da origem, em segundos (0 = desligado). O início do
    # trecho é o próprio trim_start; a saída é [trim_start, cut_end) / speed, sem margem.
    cut_end: float = 0.0

    def __post_init__(self) -> None:
        for name in ("metadata_cleanup", "mirror", "auto_adjust", "reencode", "sharpen", "audio_sync_start"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} deve ser booleano")
        for name in ("trim_start", "speed", *OPTIONAL_RANGES):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} deve ser numérico")
            object.__setattr__(self, name, float(value))
        if self.trim_start < 0:
            raise ValueError("trim_start deve ser >= 0")
        if self.speed <= 0:
            raise ValueError("speed deve ser > 0")
        for name, (low, high) in OPTIONAL_RANGES.items():
            if not low <= getattr(self, name) <= high:
                raise ValueError(f"{name} deve ficar entre {low:g} e {high:g}")
        if self.cut_end > 0 and self.cut_end <= self.trim_start:
            raise ValueError("cut_end deve ser maior que trim_start")

    @classmethod
    def from_params(cls, params: dict) -> "ProcessingOptions":
        """Cria a partir de PresetParams. Chaves ausentes usam o padrão; chaves desconhecidas -> ValueError."""
        known = {f.name for f in fields(cls)}
        extra = set(params) - known
        if extra:
            raise ValueError(f"parâmetros desconhecidos: {sorted(extra)}")
        return cls(**{k: params[k] for k in known if k in params})
