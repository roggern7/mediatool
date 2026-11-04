"""Padrões de publicação por conta (app.services.post_patterns): métricas, narrativa, fuso e ZIP."""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.services.export_time import utc_zone, zone_from_tzinfo
from app.services.post_patterns import (
    PostTime,
    analyze_account,
    build_report_zip,
    format_duration,
    narrative,
    report_filename,
)

MINUS_3 = zone_from_tzinfo(timezone(timedelta(hours=-3)))
START = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)   # 08:00 em UTC-3 (terça-feira)


def series(username: str, gaps: list[float], *, start: datetime = START, kind: str | None = "reel",
           prefix: str = "C") -> list[PostTime]:
    """Posts com os intervalos dados (minutos), a partir de `start`."""
    moments = [start]
    for gap in gaps:
        moments.append(moments[-1] + timedelta(minutes=gap))
    return [PostTime(username, f"{prefix}{i}", m.strftime("%Y-%m-%dT%H:%M:%SZ"), kind, False)
            for i, m in enumerate(moments)]


def test_intervals_mean_median_and_duration_text():
    account = analyze_account("a", series("a", [60, 120, 30, 90]), utc_zone)
    assert account.intervals == [60, 120, 30, 90]
    assert account.mean == 75 and account.median == 75
    assert (account.minimum, account.maximum) == (30, 120)
    assert account.days == 1 and account.posts_per_day == 5
    assert "Posta em média a cada 1 h 15 min (mediana 1 h 15 min)." in account.patterns
    assert [format_duration(m) for m in (5, 72, 120, 3060, 0.2)] == [
        "5 min", "1 h 12 min", "2 h", "2 dias 3 h", "menos de 1 min"]


def test_narrative_steady_burst_and_pause():
    # 5 posts a cada 72 min, depois um de 5 min (rajada), pausa de 19 h 30 min, mais 2 posts.
    account = analyze_account("a", series("a", [72, 72, 72, 72, 5, 19 * 60 + 30, 72]), MINUS_3)
    lines = narrative(account)
    assert lines[0] == "03/11 08:00 – 1 post a cada ~1 h 12 min (5 posts, 4 intervalos)"
    assert lines[1] == "03/11 12:48 – depois 2 posts com 5 min de intervalo (rajada)"
    assert lines[2] == "03/11 12:53 – depois pausa de 19 h 30 min"
    assert lines[3] == "04/11 08:23 – depois 2 posts com 1 h 12 min de intervalo"
    assert lines[-1] == "04/11 09:35 – último post analisado"
    assert account.bursts == [2] and len(account.pauses) == 1
    assert "Rajadas: 1 vez postou 2 posts em até 15 min." in account.patterns


def test_pinned_older_than_the_sequence_stays_outside():
    posts = series("a", [60, 60, 60], start=START)
    old_pinned = PostTime("a", "OLD", "2025-01-01T10:00:00Z", "photo", True)
    inside_pinned = PostTime("a", "IN", (START + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"), "reel", True)
    account = analyze_account("a", [old_pinned, inside_pinned, *posts, posts[0]], utc_zone)
    assert [item.post.code for item in account.pinned_outside] == ["OLD"]
    assert [item.post.code for item in account.sequence] == ["C0", "IN", "C1", "C2", "C3"]
    assert account.intervals == [30, 30, 60, 60]
    assert account.duplicates == 1


def test_local_time_differs_from_utc():
    posts = [PostTime("a", "X", "2026-11-04T01:30:00Z", None, None),
             PostTime("a", "Y", "2026-11-04T02:30:00+00:00", None, None)]
    local = analyze_account("a", posts, MINUS_3)
    assert local.sequence[0].local == datetime(2026, 11, 3, 22, 30)
    assert local.hours[22] == 1 and local.hours[23] == 1 and local.weekdays[1] == 2   # terça
    assert analyze_account("a", posts, utc_zone).hours[1] == 1


@pytest.mark.parametrize("count", [0, 1])
def test_account_with_zero_or_one_post(count):
    posts = series("a", [])[:count] + [PostTime("a", "BAD", "lixo", None, None)]
    account = analyze_account("a", posts, utc_zone)
    assert len(account.undated) == 1 and account.intervals == [] and account.mean is None
    assert account.patterns[0].startswith("Não há intervalos suficientes")
    assert len(narrative(account)) == count


def read_zip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_zip_has_three_files_with_bom_and_semicolon():
    files = read_zip(build_report_zip(series("a", [72, 5, 30]), title="Teste", zone=MINUS_3))
    assert sorted(files) == ["posts.csv", "relatorio.txt", "resumo.csv"]
    text = files["relatorio.txt"].decode("utf-8")
    assert "Padrões de publicação – Teste" in text and "Fuso: UTC-03:00" in text and "Comparativo" not in text
    for name in ("posts.csv", "resumo.csv"):
        assert files[name].startswith(b"\xef\xbb\xbf")
    posts = list(csv.reader(io.StringIO(files["posts.csv"].decode("utf-8-sig")), delimiter=";"))
    assert posts[0][:3] == ["perfil", "codigo", "link"]
    assert posts[2][2] == "https://www.instagram.com/p/C1/" and posts[2][6] == "72,0"
    assert posts[1][3:6] == ["03/11/2026", "08:00", "terça"]
    summary = list(csv.reader(io.StringIO(files["resumo.csv"].decode("utf-8-sig")), delimiter=";"))
    row = dict(zip(summary[0], summary[1], strict=True))
    assert row["intervalo_medio_min"] == "35,7" and row["rajadas"] == "1"


def test_several_accounts_produce_a_comparison():
    posts = series("rapida", [5, 5, 5, 5]) + series("lenta", [180, 200, 190], prefix="L")
    text = read_zip(build_report_zip(posts, title="Duas", zone=MINUS_3))["relatorio.txt"].decode("utf-8")
    assert "=== @rapida ===" in text and "=== @lenta ===" in text and "=== Comparativo ===" in text
    assert "Quem posta mais: @rapida" in text and "Mais regular:" in text


def test_report_filename():
    assert report_filename("Meu: teste/1", datetime(2026, 11, 4, 9, 5)) == "padroes_Meu_teste_1_2026-11-04_0905.zip"
