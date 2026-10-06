"""Fuso da data nos nomes da exportação (pasta e ZIP) e no mtime das entradas do ZIP."""

from __future__ import annotations

import logging
import os
import zipfile
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.core.config import load_settings
from app.jobs import export_runner
from app.services import export_time
from app.services.export_names import export_stem
from app.services.export_time import iana_zone, local_date_text, publication_moment, zone_from_tzinfo
from export_helpers import export_to, files_in, prepare, run_exports, seed_media

MINUS_3 = zone_from_tzinfo(timezone(timedelta(hours=-3)))
PLUS_9 = zone_from_tzinfo(timezone(timedelta(hours=9)))
HAS_TZDATA = iana_zone("America/Sao_Paulo") is not None
needs_tzdata = pytest.mark.skipif(not HAS_TZDATA, reason="base IANA (pacote tzdata) ausente neste ambiente")


def media(taken_at) -> dict:
    return {"id": 7, "source_profile": "perfil", "source_post_id": "ABC", "source_media_index": 0,
            "taken_at": taken_at, "original_sha256": "f" * 64}


@pytest.mark.parametrize(("taken_at", "zone", "expected"), [
    ("2026-03-04T23:59:00Z", None, "2026-03-04"),            # sem zona = UTC
    ("2026-03-05T00:30:00Z", None, "2026-03-05"),
    ("2026-03-04T23:59:00Z", MINUS_3, "2026-03-04"),         # fuso negativo: 20:59 do mesmo dia
    ("2026-03-05T00:30:00Z", MINUS_3, "2026-03-04"),         # 21:30 do dia ANTERIOR
    ("2026-03-05T02:59:59Z", MINUS_3, "2026-03-04"),
    ("2026-03-05T03:00:00Z", MINUS_3, "2026-03-05"),
    ("2026-03-04T23:59:00Z", PLUS_9, "2026-03-05"),          # fuso positivo: 08:59 do dia SEGUINTE
    ("2026-03-05T00:30:00Z", PLUS_9, "2026-03-05"),
    ("2026-03-04T14:59:59Z", PLUS_9, "2026-03-04"),
])
def test_date_near_the_utc_day_boundary(taken_at, zone, expected):
    assert local_date_text(taken_at, zone) == expected
    assert export_stem(media(taken_at), zone=zone) == f"perfil_{expected}_ABC"


def test_null_invalid_and_naive_taken_at():
    for value in (None, "", "lixo", "2026-13-40T00:00:00Z", 12345):
        assert publication_moment(value) is None and local_date_text(value, MINUS_3) is None
        assert export_stem(media(value), zone=MINUS_3) == "perfil_ABC"
    # Sem fuso no texto: tratado como UTC (é assim que o banco guarda: ISO 8601 UTC com Z).
    assert publication_moment("2026-03-05 00:30:00") == datetime(2026, 3, 5, 0, 30, tzinfo=UTC)
    assert local_date_text("2026-03-05T00:30:00", MINUS_3) == "2026-03-04"
    assert local_date_text("2026-03-05T00:30:00+09:00", None) == "2026-03-04"   # deslocamento explícito respeitado
    broken = lambda moment: 1 / 0   # noqa: E731 - zona com defeito nunca quebra a exportação: cai para UTC
    assert local_date_text("2026-03-05T00:30:00Z", broken) == "2026-03-05"


@needs_tzdata
def test_iana_zones_with_historical_dst():
    sao_paulo, tokyo, lisbon = iana_zone("America/Sao_Paulo"), iana_zone("Asia/Tokyo"), iana_zone("Europe/Lisbon")
    assert sao_paulo(publication_moment("2018-12-31T01:30:00Z")) == datetime(2018, 12, 30, 23, 30)   # verão: -02
    assert sao_paulo(publication_moment("2018-07-01T02:30:00Z")) == datetime(2018, 6, 30, 23, 30)    # inverno: -03
    assert sao_paulo(publication_moment("2023-12-31T01:30:00Z")) == datetime(2023, 12, 30, 22, 30)   # sem verão
    assert local_date_text("2018-12-31T02:30:00Z", sao_paulo) == "2018-12-31"
    assert local_date_text("2023-12-31T02:30:00Z", sao_paulo) == "2023-12-30"
    assert local_date_text("2026-03-04T23:59:00Z", tokyo) == "2026-03-05"
    assert local_date_text("2021-03-27T23:30:00Z", lisbon) == "2021-03-27"    # véspera da mudança: WET (+00)
    assert local_date_text("2021-03-28T23:30:00Z", lisbon) == "2021-03-29"    # depois da mudança: WEST (+01)


@pytest.mark.skipif(os.name != "nt", reason="API de fuso do Windows")
def test_windows_system_zone_uses_the_rules_of_the_publication_year():
    import winreg

    zone = export_time.windows_system_zone()
    assert zone is not None and zone(datetime(2020, 6, 1, 12, tzinfo=UTC)).tzinfo is None
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation") as key:
        name = winreg.QueryValueEx(key, "TimeZoneKeyName")[0]
    if name != "E. South America Standard Time":
        pytest.skip(f"fuso do sistema é {name!r}; o histórico conferido aqui é o de Brasília")
    convert = lambda text: zone(publication_moment(text))   # noqa: E731
    assert convert("2018-12-31T01:30:00Z") == datetime(2018, 12, 30, 23, 30)   # horário de verão vigente: -02:00
    assert convert("2019-01-01T01:00:00Z") == datetime(2018, 12, 31, 23, 0)    # verão atravessando a virada do ano
    assert convert("2019-02-10T01:00:00Z") == datetime(2019, 2, 9, 23, 0)      # ainda verão (acabou em 17/02/2019)
    assert convert("2019-02-20T01:00:00Z") == datetime(2019, 2, 19, 22, 0)
    assert convert("2018-07-01T02:30:00Z") == datetime(2018, 6, 30, 23, 30)    # inverno: -03:00
    assert convert("2023-12-31T01:30:00Z") == datetime(2023, 12, 30, 22, 30)   # depois de 2019: sem verão
    assert local_date_text("2018-12-31T01:30:00Z", zone) == "2018-12-30"


def test_resolution_order_and_invalid_or_missing_configuration(monkeypatch, caplog):
    export_time.resolve_zone.cache_clear()
    marker = lambda moment: datetime(2001, 1, 1)   # noqa: E731
    monkeypatch.setattr(export_time, "system_zone", lambda: marker)
    assert export_time.resolve_zone(None) is marker and export_time.resolve_zone("  ") is marker   # sem configuração
    assert export_time.resolve_zone("UTC") is export_time.utc_zone                # UTC funciona sem base IANA
    with caplog.at_level(logging.WARNING, logger="media_tool.exports"):
        assert export_time.resolve_zone("Nao/Existe") is marker                   # inválido: avisa e usa o sistema
        assert export_time.resolve_zone("Nao/Existe") is marker
    assert len([r for r in caplog.records if "MEDIA_TOOL_TIMEZONE" in r.getMessage()]) == 1   # aviso uma vez

    # Base IANA ausente (tzdata não instalado): mesmo com nome válido, cai para o fuso do sistema.
    import zoneinfo

    def missing(name):
        raise zoneinfo.ZoneInfoNotFoundError(name)

    monkeypatch.setattr(zoneinfo, "ZoneInfo", missing)
    export_time.resolve_zone.cache_clear()
    assert iana_zone("America/Sao_Paulo") is None and iana_zone("UTC") is export_time.utc_zone
    assert export_time.resolve_zone("America/Sao_Paulo") is marker
    export_time.resolve_zone.cache_clear()


def test_system_zone_falls_back_to_utc_when_nothing_can_be_determined(monkeypatch):
    monkeypatch.setattr(export_time, "windows_system_zone", lambda: None)

    class NoLocalTime(datetime):
        def astimezone(self, tz=None):
            raise OSError("sem fuso local")

    monkeypatch.setattr(export_time, "datetime", NoLocalTime)
    assert export_time.system_zone() is export_time.utc_zone


def test_timezone_setting_comes_from_the_environment(tmp_path):
    assert load_settings({"MEDIA_TOOL_TIMEZONE": " America/Sao_Paulo "}, root=tmp_path).timezone == "America/Sao_Paulo"
    assert load_settings({}, root=tmp_path).timezone is None


def test_same_local_date_in_folder_names_zip_names_and_zip_mtime(harness, tmp_path, monkeypatch):
    dest = prepare(harness, tmp_path)
    monkeypatch.setattr(export_time, "resolve_zone", lambda configured: MINUS_3)
    ids = [*seed_media(harness, 1, profile="noite", code="N1", taken_at="2026-03-05T01:30:00Z"),
           *seed_media(harness, 1, profile="tarde", code="T1", taken_at="2026-03-05T18:00:00Z", start=1),
           *seed_media(harness, 1, profile="semdata", code="S1", taken_at=None, start=2)]
    stored = {i: harness.ctx.media.get(i)["taken_at"] for i in ids}
    source = {"kind": "library", "selection": {"media_ids": ids}}
    folder = export_to(harness, source, dest)
    created = harness.client.post("/api/exports", json={"source": source, "destination": {"zip": True}}).json()
    run_exports(harness)
    from app.services import export_zip

    with zipfile.ZipFile(export_zip.zip_file(harness.settings, harness.ctx.exports.get(created["id"]))) as archive:
        entries = {info.filename: info.date_time for info in archive.infolist()}
    expected = ["noite_2026-03-04_N1.jpg", "semdata_S1.jpg", "tarde_2026-03-05_T1.jpg"]
    assert folder["status"] == "completed" and files_in(dest) == expected == sorted(entries)   # mesmo nome nos dois
    assert entries["noite_2026-03-04_N1.jpg"] == (2026, 3, 4, 22, 30, 0)    # hora local do mesmo fuso
    assert entries["tarde_2026-03-05_T1.jpg"] == (2026, 3, 5, 15, 0, 0)
    assert {i: harness.ctx.media.get(i)["taken_at"] for i in ids} == stored   # nada regravado no banco
    assert export_runner._zip_time({"taken_at": "2026-03-05T01:30:00Z"}, tmp_path) == (2026, 3, 5, 1, 30, 0)   # UTC
