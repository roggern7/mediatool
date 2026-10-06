"""Fuso da data usada nos nomes da exportação (pasta e ZIP) e na data de modificação das entradas do ZIP.

`taken_at` é guardado em UTC (ISO 8601 com `Z`) e NÃO é alterado: a conversão é só de apresentação. Uma "zona" aqui é
uma função `instante (datetime com fuso) -> data/hora LOCAL (datetime sem fuso)`.

Resolução do fuso, nesta ordem (`resolve_zone`):
1. `MEDIA_TOOL_TIMEZONE` (nome IANA, ex.: `America/Sao_Paulo`), via `zoneinfo`. No Windows o Python não traz a base
   IANA: ela vem do pacote `tzdata` (backend/requirements.txt). Nome inválido ou base ausente: aviso no log (uma vez)
   e segue para o passo 2. `UTC` funciona sempre, sem base.
2. Fuso do sistema operacional. No Windows, pela API do sistema com as regras de horário de verão DO ANO da
   publicação (`GetDynamicTimeZoneInformation` + `SystemTimeToTzSpecificLocalTimeEx`): datas antigas saem certas (por
   exemplo o horário de verão do Brasil até 2019), sem depender de `tzdata`. `datetime.fromtimestamp` não serve para
   isso: no Windows ele aplica só as regras ATUAIS. Fora do Windows: o fuso local do processo.
3. UTC, se nada for determinável.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime, tzinfo
from functools import lru_cache

from app.db.connection import parse_utc

logger = logging.getLogger("media_tool.exports")

Zone = Callable[[datetime], datetime]
_UTC_NAMES = frozenset({"utc", "etc/utc", "z", "gmt", "etc/gmt"})


def utc_zone(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(tzinfo=None)


def zone_from_tzinfo(zone: tzinfo) -> Zone:
    return lambda moment: moment.astimezone(zone).replace(tzinfo=None)


def publication_moment(taken_at: object) -> datetime | None:
    """`taken_at` como instante. Valor sem fuso é tratado como UTC (é assim que é guardado). Nulo/inválido: None."""
    if not isinstance(taken_at, str):
        return None
    moment = parse_utc(taken_at.strip())
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def iana_zone(name: str) -> Zone | None:
    """Zona IANA pelo nome; None se o nome é inválido ou a base de fusos (tzdata) não está disponível."""
    if name.strip().lower() in _UTC_NAMES:
        return utc_zone
    try:
        from zoneinfo import ZoneInfo

        return zone_from_tzinfo(ZoneInfo(name.strip()))
    except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError, ValueError, ImportError, OSError...: cai para o passo 2
        return None


def windows_system_zone() -> Zone | None:
    """Fuso do Windows com as regras de horário de verão do ano de cada instante (histórico correto). None fora do
    Windows ou se a API não estiver disponível."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class SystemTime(ctypes.Structure):
            _fields_ = [(name, wintypes.WORD) for name in (
                "year", "month", "weekday", "day", "hour", "minute", "second", "milliseconds")]

        class TimeZoneInformation(ctypes.Structure):
            _fields_ = [("bias", wintypes.LONG), ("standard_name", wintypes.WCHAR * 32),
                        ("standard_date", SystemTime), ("standard_bias", wintypes.LONG),
                        ("daylight_name", wintypes.WCHAR * 32), ("daylight_date", SystemTime),
                        ("daylight_bias", wintypes.LONG)]

        class DynamicTimeZoneInformation(ctypes.Structure):
            _fields_ = [*TimeZoneInformation._fields_, ("key_name", wintypes.WCHAR * 128),
                        ("dynamic_daylight_disabled", wintypes.BOOLEAN)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        dynamic = DynamicTimeZoneInformation()
        if kernel32.GetDynamicTimeZoneInformation(ctypes.byref(dynamic)) == 0xFFFFFFFF:
            return None
        # A variante Ex recebe o fuso DINÂMICO e aplica as regras do ano certo, inclusive na virada do ano (o
        # horário de verão do hemisfério sul atravessa 31/12).
        to_local = kernel32.SystemTimeToTzSpecificLocalTimeEx
        to_local.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        to_local.restype = wintypes.BOOL
    except (OSError, AttributeError, ValueError):
        return None

    def convert(moment: datetime) -> datetime:
        utc = moment.astimezone(UTC)
        source = SystemTime(utc.year, utc.month, 0, utc.day, utc.hour, utc.minute, utc.second, 0)
        target = SystemTime()
        if not (1601 <= utc.year <= 30827) or not to_local(
                ctypes.byref(dynamic), ctypes.byref(source), ctypes.byref(target)):
            return _process_local(moment)
        return datetime(target.year, target.month, target.day, target.hour, target.minute, target.second)

    try:
        convert(datetime(2020, 1, 1, tzinfo=UTC))
    except (OSError, ValueError, OverflowError):
        return None
    return convert


def _process_local(moment: datetime) -> datetime:
    try:
        return moment.astimezone().replace(tzinfo=None)
    except (OSError, ValueError, OverflowError):
        return utc_zone(moment)


def system_zone() -> Zone:
    """Fuso do sistema operacional; UTC se não for determinável."""
    zone = windows_system_zone()
    if zone is not None:
        return zone
    try:
        datetime.now(UTC).astimezone()
    except (OSError, ValueError):
        return utc_zone
    return _process_local


@lru_cache(maxsize=8)
def resolve_zone(configured: str | None) -> Zone:
    """Zona padrão do Media Tool, resolvida uma vez por valor configurado (ordem no cabeçalho do módulo)."""
    name = (configured or "").strip()
    if name:
        zone = iana_zone(name)
        if zone is not None:
            return zone
        logger.warning(
            "MEDIA_TOOL_TIMEZONE=%r não pôde ser usado (nome inválido ou pacote tzdata ausente); usando o fuso do"
            " sistema nos nomes da exportação", name[:64])
    return system_zone()


def local_date_text(taken_at: object, zone: Zone | None) -> str | None:
    """`AAAA-MM-DD` da publicação no fuso dado (None = UTC). None se `taken_at` é nulo ou inválido."""
    try:
        moment = publication_moment(taken_at)
        if moment is None:
            return None
        try:
            local = (zone or utc_zone)(moment)
        except Exception:  # noqa: BLE001 - uma zona com problema nunca quebra a exportação
            local = utc_zone(moment)
        return f"{local.year:04d}-{local.month:02d}-{local.day:02d}"
    except Exception:  # noqa: BLE001 - data nos limites do calendário (ano 1 ou 9999 com deslocamento): nome sem data
        return None
