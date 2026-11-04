"""Padrões de publicação por conta do Instagram a partir dos horários dos posts (funções puras).

Entrada: `PostTime` (horário UTC de cada publicação). Saída: um ZIP em memória com `relatorio.txt` (especificações,
padrões identificados e sequência narrativa por conta, mais um comparativo quando há 2+ contas), `resumo.csv` (uma
linha por conta) e `posts.csv` (todas as publicações). CSVs com `;`, BOM UTF-8, CRLF e decimal com vírgula.

Regras (limiares nas constantes abaixo):
- Por conta: deduplica por código (fica a primeira ocorrência), ignora datas inválidas (contadas à parte) e ordena por
  `taken_at`. Fixados (`pinned=True`) mais antigos que o post NÃO fixado mais antigo ficam fora da sequência.
- Rajada: 2+ posts seguidos com intervalos de até `BURST_MINUTES`.
- Pausa: intervalo maior que `max(PAUSE_MIN_MINUTES, PAUSE_MEDIAN_FACTOR × mediana)`.
- Trechos da narrativa: intervalos seguidos que não são rajada nem pausa entram no mesmo trecho enquanto cada novo
  intervalo fica a até `max(SIMILAR_MIN_MINUTES, SIMILAR_RATIO × média do trecho)` da média do trecho.
- Regularidade pelo coeficiente de variação dos intervalos (desvio padrão populacional / média).
"""

from __future__ import annotations

import csv
import io
import os
import re
import statistics
import time as clock
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import quote

from app.services import export_time
from app.services.export_names import sanitize_component
from app.services.export_time import Zone
from app.services.profile_report import neutralize

BURST_MINUTES = 15.0
PAUSE_MIN_MINUTES = 6 * 60.0
PAUSE_MEDIAN_FACTOR = 3.0
SIMILAR_MIN_MINUTES = 10.0
SIMILAR_RATIO = 0.25
CV_REGULAR = 0.5
CV_VARIABLE = 1.0
MAX_NARRATIVE_LINES = 200
QUIET_MIN_HOURS = 5        # janela de horas sem nenhum post para virar padrão
QUIET_MIN_POSTS = 5
WEEKDAY_MIN_POSTS = 7
TITLE_MAX_LENGTH = 60
# Faixas do "intervalo mais comum" (minutos; limite superior inclusivo).
BUCKETS = ((15, "até 15 min"), (30, "15 a 30 min"), (60, "30 min a 1 h"), (120, "1 h a 2 h"), (240, "2 h a 4 h"),
           (480, "4 h a 8 h"), (1440, "8 h a 24 h"), (4320, "1 a 3 dias"), (float("inf"), "mais de 3 dias"))

WEEKDAYS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")
WEEKDAYS_SHORT = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")
WEEKDAYS_PLURAL = ("às segundas", "às terças", "às quartas", "às quintas", "às sextas", "aos sábados", "aos domingos")
KIND_LABELS = {"photo": "fotos", "video": "vídeos", "reel": "reels", "carousel": "carrosséis"}
REGULARITY_UNKNOWN = "indeterminada"


@dataclass(frozen=True)
class PostTime:
    username: str
    code: str            # shortcode; link = https://www.instagram.com/p/<code>/
    taken_at: str        # ISO 8601 UTC (ex. "2026-11-03T13:05:00Z" ou com +00:00)
    kind: str | None     # "photo" | "video" | "reel" | "carousel" | None
    pinned: bool | None


@dataclass(frozen=True)
class DatedPost:
    post: PostTime
    moment: datetime          # instante (com fuso)
    local: datetime           # horário local (sem fuso)
    in_sequence: bool
    interval: float | None    # minutos desde o post anterior da sequência


@dataclass(frozen=True)
class Segment:
    kind: str                 # "steady" | "burst" | "pause"
    start: int                # índice (na sequência) do primeiro post do trecho
    intervals: tuple[float, ...]

    @property
    def posts(self) -> int:
        return len(self.intervals) + 1


@dataclass
class AccountAnalysis:
    username: str
    posts: list[DatedPost] = field(default_factory=list)      # sequência + fixados fora, em ordem cronológica
    sequence: list[DatedPost] = field(default_factory=list)
    pinned_outside: list[DatedPost] = field(default_factory=list)
    undated: list[PostTime] = field(default_factory=list)
    duplicates: int = 0
    intervals: list[float] = field(default_factory=list)
    mean: float | None = None
    median: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    days: int = 0
    posts_per_day: float | None = None
    cv: float | None = None
    regularity: str = REGULARITY_UNKNOWN
    bursts: list[int] = field(default_factory=list)          # tamanho (posts) de cada rajada
    pause_threshold: float | None = None
    pauses: list[float] = field(default_factory=list)
    hours: list[int] = field(default_factory=lambda: [0] * 24)
    weekdays: list[int] = field(default_factory=lambda: [0] * 7)
    top_hours: list[int] = field(default_factory=list)
    top_hours_share: float = 0.0
    typical_bucket: str | None = None
    typical_bucket_share: float = 0.0
    kinds: Counter = field(default_factory=Counter)
    segments: list[Segment] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- formatação

def format_duration(minutes: float) -> str:
    """"5 min", "1 h 12 min", "2 h", "1 dia 3 h", "2 dias"."""
    total = int(round(minutes))
    if total < 1:
        return "menos de 1 min"
    if total < 60:
        return f"{total} min"
    if total < 1440:
        hours, rest = divmod(total, 60)
        return f"{hours} h" if rest == 0 else f"{hours} h {rest} min"
    days, rest = divmod(total, 1440)
    hours = int(round(rest / 60))
    if hours == 24:
        days, hours = days + 1, 0
    day_text = f"{days} dia" if days == 1 else f"{days} dias"
    return day_text if hours == 0 else f"{day_text} {hours} h"


def _decimal(value: float | None, places: int = 1) -> str:
    return "" if value is None else f"{value:.{places}f}".replace(".", ",")


def _percent(part: float, total: float) -> int:
    return int(round(100 * part / total)) if total else 0


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " e " + items[-1]


def _hour(hour: int) -> str:
    return f"{hour}h"


def _date_time(local: datetime, with_year: bool) -> str:
    date = f"{local.day:02d}/{local.month:02d}" + (f"/{local.year:04d}" if with_year else "")
    return f"{date} {local.hour:02d}:{local.minute:02d}"


def _to_local(moment: datetime, zone: Zone) -> datetime:
    try:
        return zone(moment)
    except Exception:  # noqa: BLE001 - uma zona com problema nunca quebra o relatório (mesma regra da exportação)
        return export_time.utc_zone(moment)


def default_zone() -> Zone:
    """Fuso padrão do Media Tool (`MEDIA_TOOL_TIMEZONE`, senão o do sistema; ver `export_time.resolve_zone`)."""
    return export_time.resolve_zone(os.environ.get("MEDIA_TOOL_TIMEZONE"))


def zone_label(zone: Zone, now: datetime | None = None) -> str:
    """Deslocamento do fuso agora, ex. "UTC-03:00"."""
    now = now or datetime.now(UTC)
    offset = int(round((_to_local(now, zone) - now.astimezone(UTC).replace(tzinfo=None)).total_seconds() / 60))
    sign = "-" if offset < 0 else "+"
    hours, minutes = divmod(abs(offset), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


# ---------------------------------------------------------------- análise

def _regularity(cv: float | None) -> str:
    if cv is None:
        return REGULARITY_UNKNOWN
    if cv < CV_REGULAR:
        return "regular"
    if cv < CV_VARIABLE:
        return "variável"
    return "irregular"


def _segments(intervals: list[float], pause_threshold: float) -> list[Segment]:
    segments: list[Segment] = []
    current: list[float] = []
    current_kind = ""
    start = 0

    def close(next_start: int) -> None:
        nonlocal current, current_kind, start
        if current:
            segments.append(Segment(current_kind, start, tuple(current)))
        current, current_kind, start = [], "", next_start

    for index, value in enumerate(intervals):
        if value <= BURST_MINUTES:
            kind = "burst"
        elif value > pause_threshold:
            kind = "pause"
        else:
            kind = "steady"
        if current and kind == current_kind and kind != "pause":
            if kind == "burst":
                current.append(value)
                continue
            mean = sum(current) / len(current)
            if abs(value - mean) <= max(SIMILAR_MIN_MINUTES, SIMILAR_RATIO * mean):
                current.append(value)
                continue
        close(index)
        current, current_kind = [value], kind
    close(len(intervals))
    return segments


def _quiet_window(hours: list[int]) -> tuple[int, int] | None:
    """Maior janela circular de horas sem posts: (hora inicial, quantidade de horas)."""
    if not any(hours):
        return None
    best: tuple[int, int] | None = None
    for begin in range(24):
        if hours[begin] or not hours[(begin - 1) % 24]:
            continue
        length = 0
        while not hours[(begin + length) % 24]:
            length += 1
        if best is None or length > best[1]:
            best = (begin, length)
    return best


def _patterns(account: AccountAnalysis) -> list[str]:
    count = len(account.sequence)
    lines: list[str] = []
    if count < 2:
        lines.append(f"Não há intervalos suficientes para identificar padrões ({count} post"
                     f"{'' if count == 1 else 's'} na sequência).")
        return lines
    lines.append(f"Posta em média a cada {format_duration(account.mean)} (mediana {format_duration(account.median)}).")
    lines.append(f"Média de {_decimal(account.posts_per_day)} posts por dia no período ({account.days} "
                 f"dia{'' if account.days == 1 else 's'}).")
    if len(account.intervals) >= 3 and account.typical_bucket:
        lines.append(f"Intervalo mais comum: {account.typical_bucket} ({account.typical_bucket_share:.0f}% dos "
                     "intervalos).")
    if account.bursts:
        smallest, largest = min(account.bursts), max(account.bursts)
        size = f"{smallest}" if smallest == largest else f"{smallest}-{largest}"
        times = "1 vez" if len(account.bursts) == 1 else f"{len(account.bursts)} vezes"
        lines.append(f"Rajadas: {times} postou {size} posts em até {BURST_MINUTES:.0f} min.")
    elif len(account.intervals) >= 3:
        lines.append(f"Sem rajadas (nenhum intervalo de até {BURST_MINUTES:.0f} min).")
    if account.pauses:
        lines.append(f"Pausas longas (mais de {format_duration(account.pause_threshold)}): {len(account.pauses)}, "
                     f"a maior de {format_duration(max(account.pauses))}.")
    if count >= 3 and account.top_hours:
        lines.append(f"Horários preferidos: {_join([_hour(h) for h in account.top_hours])} "
                     f"({account.top_hours_share:.0f}% dos posts).")
    if count >= WEEKDAY_MIN_POSTS:
        peak = max(account.weekdays)
        active = [day for day in range(7) if account.weekdays[day] >= 0.8 * peak]
        if len(active) <= 3:
            lines.append(f"Mais ativo {_join([WEEKDAYS_PLURAL[day] for day in active])}.")
    if account.cv is not None:
        lines.append(f"Ritmo {account.regularity} (coeficiente de variação {_decimal(account.cv, 2)}).")
    quiet = _quiet_window(account.hours)
    if count >= QUIET_MIN_POSTS and quiet and quiet[1] >= QUIET_MIN_HOURS:
        begin, length = quiet
        lines.append(f"Costuma pausar entre {_hour(begin)} e {_hour((begin + length) % 24)} (nenhum post nesse "
                     "horário).")
    if account.kinds:
        kind, amount = account.kinds.most_common(1)[0]
        lines.append(f"Tipo mais postado: {KIND_LABELS.get(kind, kind)} "
                     f"({_percent(amount, sum(account.kinds.values()))}%).")
    return lines


def analyze_account(username: str, posts: list[PostTime], zone: Zone) -> AccountAnalysis:
    account = AccountAnalysis(username)
    seen: set[str] = set()
    dated: list[tuple[datetime, PostTime]] = []
    for post in posts:
        if post.code:
            if post.code in seen:
                account.duplicates += 1
                continue
            seen.add(post.code)
        moment = export_time.publication_moment(post.taken_at)
        if moment is None:
            account.undated.append(post)
        else:
            dated.append((moment, post))
    dated.sort(key=lambda item: (item[0], item[1].code))
    unpinned = [moment for moment, post in dated if post.pinned is not True]
    oldest_unpinned = unpinned[0] if unpinned else None
    previous: datetime | None = None
    for moment, post in dated:
        local = _to_local(moment, zone)
        outside = post.pinned is True and oldest_unpinned is not None and moment < oldest_unpinned
        if outside:
            item = DatedPost(post, moment, local, False, None)
            account.pinned_outside.append(item)
        else:
            interval = None if previous is None else (moment - previous).total_seconds() / 60
            previous = moment
            item = DatedPost(post, moment, local, True, interval)
            account.sequence.append(item)
            if interval is not None:
                account.intervals.append(interval)
            account.hours[local.hour] += 1
            account.weekdays[local.weekday()] += 1
            if post.kind:
                account.kinds[post.kind] += 1
        account.posts.append(item)

    sequence, intervals = account.sequence, account.intervals
    if sequence:
        account.days = (sequence[-1].local.date() - sequence[0].local.date()).days + 1
        account.posts_per_day = len(sequence) / account.days
        ranked = sorted((h for h in range(24) if account.hours[h]), key=lambda h: (-account.hours[h], h))[:3]
        account.top_hours = sorted(ranked)
        account.top_hours_share = 100 * sum(account.hours[h] for h in ranked) / len(sequence)
    if intervals:
        account.mean = statistics.fmean(intervals)
        account.median = statistics.median(intervals)
        account.minimum, account.maximum = min(intervals), max(intervals)
        if len(intervals) >= 2 and account.mean > 0:
            account.cv = statistics.pstdev(intervals) / account.mean
        account.regularity = _regularity(account.cv)
        account.pause_threshold = max(PAUSE_MIN_MINUTES, PAUSE_MEDIAN_FACTOR * account.median)
        account.pauses = [value for value in intervals if value > account.pause_threshold]
        buckets = Counter(next(label for limit, label in BUCKETS if value <= limit) for value in intervals)
        label, amount = max(buckets.items(), key=lambda item: (item[1], -[b[1] for b in BUCKETS].index(item[0])))
        account.typical_bucket, account.typical_bucket_share = label, 100 * amount / len(intervals)
        account.segments = _segments(intervals, account.pause_threshold)
        account.bursts = [segment.posts for segment in account.segments if segment.kind == "burst"]
    account.patterns = _patterns(account)
    return account


def analyze(posts: list[PostTime], zone: Zone) -> list[AccountAnalysis]:
    """Uma análise por conta, na ordem da primeira aparição."""
    groups: dict[str, list[PostTime]] = {}
    for post in posts:
        groups.setdefault(post.username, []).append(post)
    return [analyze_account(username, items, zone) for username, items in groups.items()]


# ---------------------------------------------------------------- narrativa

def _segment_text(segment: Segment) -> str:
    values = segment.intervals
    if segment.kind == "pause":
        return f"pausa de {format_duration(values[0])}"
    if segment.kind == "burst":
        low, high = format_duration(min(values)), format_duration(max(values))
        gap = f"com {low} de intervalo" if low == high else f"com intervalos de {low} a {high}"
        return f"{segment.posts} posts {gap} (rajada)"
    if len(values) == 1:
        return f"2 posts com {format_duration(values[0])} de intervalo"
    mean = sum(values) / len(values)
    return f"1 post a cada ~{format_duration(mean)} ({segment.posts} posts, {len(values)} intervalos)"


def narrative(account: AccountAnalysis) -> list[str]:
    """Sequência em ordem cronológica, uma linha por trecho (o post que fecha um trecho abre o seguinte)."""
    sequence = account.sequence
    if not sequence:
        return []
    with_year = sequence[0].local.year != sequence[-1].local.year
    if len(sequence) == 1:
        return [f"{_date_time(sequence[0].local, with_year)} – apenas 1 post, sem intervalos"]
    lines = []
    for number, segment in enumerate(account.segments):
        text = _segment_text(segment)
        lines.append(f"{_date_time(sequence[segment.start].local, with_year)} – "
                     f"{text if number == 0 else 'depois ' + text}")
    lines.append(f"{_date_time(sequence[-1].local, with_year)} – último post analisado")
    return lines


# ---------------------------------------------------------------- relatório

def _distribution_lines(account: AccountAnalysis) -> list[str]:
    hours = " | ".join(f"{h:02d}h {account.hours[h]}" for h in range(24) if account.hours[h])
    days = " | ".join(f"{WEEKDAYS_SHORT[d]} {account.weekdays[d]}" for d in range(7))
    return [f"  Posts por hora (local): {hours}", f"  Posts por dia da semana: {days}"]


def _account_section(account: AccountAnalysis) -> list[str]:
    sequence = account.sequence
    lines = [f"=== @{account.username} ===", "", "Especificações"]
    lines.append(f"  Posts analisados: {len(sequence)}")
    if account.pinned_outside:
        lines.append(f"  Fixados fora da sequência: {len(account.pinned_outside)}")
    if account.undated:
        lines.append(f"  Ignorados sem data válida: {len(account.undated)}")
    if account.duplicates:
        lines.append(f"  Duplicados ignorados (mesmo código): {account.duplicates}")
    if sequence:
        lines.append(f"  Período: {_date_time(sequence[0].local, True)} → {_date_time(sequence[-1].local, True)} "
                     f"({account.days} dia{'' if account.days == 1 else 's'})")
        lines.append(f"  Posts por dia: {_decimal(account.posts_per_day)}")
    if account.intervals:
        lines.append(f"  Intervalo médio: {format_duration(account.mean)}")
        lines.append(f"  Intervalo mediano: {format_duration(account.median)}")
        lines.append(f"  Intervalo mínimo: {format_duration(account.minimum)}")
        lines.append(f"  Intervalo máximo: {format_duration(account.maximum)}")
        cv = "" if account.cv is None else f" (coeficiente de variação {_decimal(account.cv, 2)})"
        lines.append(f"  Regularidade: {account.regularity}{cv}")
        largest = f", maior com {max(account.bursts)} posts" if account.bursts else ""
        lines.append(f"  Rajadas (até {BURST_MINUTES:.0f} min): {len(account.bursts)}{largest}")
        if account.typical_bucket:
            lines.append(f"  Intervalo típico: mediana {format_duration(account.median)}; faixa mais frequente "
                         f"{account.typical_bucket} ({account.typical_bucket_share:.0f}%)")
    if sequence:
        lines.append(f"  Horários preferidos: {_join([_hour(h) for h in account.top_hours])} "
                     f"({account.top_hours_share:.0f}% dos posts)")
        lines.extend(_distribution_lines(account))
    lines += ["", "Padrões identificados"]
    lines += [f"  - {line}" for line in account.patterns]
    story = narrative(account)
    if story:
        lines += ["", "Sequência"]
        if len(story) > MAX_NARRATIVE_LINES:
            lines.append(f"  (mostrando as últimas {MAX_NARRATIVE_LINES} de {len(story)} linhas; posts.csv traz "
                         "todas as publicações)")
            story = story[-MAX_NARRATIVE_LINES:]
        lines += [f"  {line}" for line in story]
    if account.pinned_outside:
        lines += ["", "Fixados fora da sequência"]
        lines += [f"  {_date_time(item.local, True)} – {_link(item.post.code) or item.post.code}"
                  for item in account.pinned_outside]
    lines.append("")
    return lines


def _comparison(accounts: list[AccountAnalysis]) -> list[str]:
    header = ("Conta", "Posts", "Int. médio", "Mediano", "Posts/dia", "Rajadas", "Horário pref.", "Regularidade")
    rows = []
    for account in accounts:
        rows.append((f"@{account.username}", str(len(account.sequence)),
                     format_duration(account.mean) if account.mean is not None else "-",
                     format_duration(account.median) if account.median is not None else "-",
                     _decimal(account.posts_per_day) or "-", str(len(account.bursts)),
                     _hour(max(range(24), key=lambda h: (account.hours[h], -h))) if account.sequence else "-",
                     account.regularity))
    widths = [max(len(row[i]) for row in (header, *rows)) for i in range(len(header))]
    lines = ["=== Comparativo ===", ""]
    for row in (header, *rows):
        lines.append("  " + "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())
    lines.append("")
    measured = [account for account in accounts if account.intervals]
    if len(measured) >= 2:
        busiest = max(measured, key=lambda a: a.posts_per_day)
        lines.append(f"  - Quem posta mais: @{busiest.username} ({_decimal(busiest.posts_per_day)} posts/dia).")
        fastest = min(measured, key=lambda a: a.median)
        lines.append(f"  - Menor intervalo mediano: @{fastest.username} ({format_duration(fastest.median)}).")
        with_cv = [account for account in measured if account.cv is not None]
        if len(with_cv) >= 2:
            steady = min(with_cv, key=lambda a: a.cv)
            lines.append(f"  - Mais regular: @{steady.username} ({steady.regularity}, coeficiente de variação "
                         f"{_decimal(steady.cv, 2)}).")
        bursty = max(measured, key=lambda a: len(a.bursts))
        if bursty.bursts:
            lines.append(f"  - Mais rajadas: @{bursty.username} ({len(bursty.bursts)}).")
    else:
        lines.append("  - Menos de 2 contas com intervalos: sem frases comparativas.")
    lines.append("")
    return lines


def build_text(accounts: list[AccountAnalysis], *, title: str, generated_at: datetime, zone_text: str) -> str:
    lines = [f"Padrões de publicação – {title}",
             f"Gerado em: {_date_time(generated_at, True)}",
             f"Fuso: {zone_text} (todos os horários deste relatório são locais)",
             f"Contas ({len(accounts)}): {', '.join('@' + a.username for a in accounts) or '-'}",
             f"Rajada: posts com até {BURST_MINUTES:.0f} min de intervalo. Pausa: intervalo maior que "
             f"{format_duration(PAUSE_MIN_MINUTES)} e que {PAUSE_MEDIAN_FACTOR:.0f}x a mediana da conta.",
             ""]
    for account in accounts:
        lines += _account_section(account)
    if len(accounts) >= 2:
        lines += _comparison(accounts)
    return "\n".join(lines)


def _link(code: str) -> str:
    return f"https://www.instagram.com/p/{quote(code, safe='')}/" if code else ""


SUMMARY_COLUMNS = ("perfil", "posts_analisados", "fixados_fora", "sem_data_valida", "duplicados", "primeiro_post",
                   "ultimo_post", "dias_no_periodo", "posts_por_dia", "intervalo_medio_min", "intervalo_mediano_min",
                   "intervalo_minimo_min", "intervalo_maximo_min", "coef_variacao", "regularidade", "rajadas",
                   "maior_rajada_posts", "pausas_longas", "maior_pausa_min", "horarios_preferidos",
                   "faixa_intervalo_mais_comum", "tipo_mais_postado")
POST_COLUMNS = ("perfil", "codigo", "link", "data_local", "hora_local", "dia_semana", "intervalo_desde_anterior_min",
                "tipo", "fixado", "na_sequencia")


def _csv_text(header: tuple[str, ...], rows: list[list[str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def summary_rows(accounts: list[AccountAnalysis]) -> list[list[str]]:
    rows = []
    for a in accounts:
        first = _date_time(a.sequence[0].local, True) if a.sequence else ""
        last = _date_time(a.sequence[-1].local, True) if a.sequence else ""
        kind = ""
        if a.kinds:
            name, amount = a.kinds.most_common(1)[0]
            kind = f"{KIND_LABELS.get(name, name)} ({_percent(amount, sum(a.kinds.values()))}%)"
        rows.append([neutralize(a.username), str(len(a.sequence)), str(len(a.pinned_outside)), str(len(a.undated)),
                     str(a.duplicates), first, last, str(a.days) if a.sequence else "", _decimal(a.posts_per_day, 2),
                     _decimal(a.mean), _decimal(a.median), _decimal(a.minimum), _decimal(a.maximum),
                     _decimal(a.cv, 2), a.regularity, str(len(a.bursts)), str(max(a.bursts, default=0) or ""),
                     str(len(a.pauses)), _decimal(max(a.pauses)) if a.pauses else "",
                     ", ".join(_hour(h) for h in a.top_hours), a.typical_bucket or "", kind])
    return rows


def post_rows(accounts: list[AccountAnalysis]) -> list[list[str]]:
    rows = []
    flag = {True: "Sim", False: "Não", None: ""}
    for a in accounts:
        for item in a.posts:
            local, post = item.local, item.post
            rows.append([neutralize(a.username), neutralize(post.code), _link(post.code),
                         f"{local.day:02d}/{local.month:02d}/{local.year:04d}", f"{local.hour:02d}:{local.minute:02d}",
                         WEEKDAYS[local.weekday()], _decimal(item.interval), post.kind or "", flag[post.pinned],
                         flag[item.in_sequence]])
        for post in a.undated:
            rows.append([neutralize(a.username), neutralize(post.code), _link(post.code), "", "", "", "",
                         post.kind or "", flag[post.pinned], "Não"])
    return rows


def _entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=clock.localtime()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def build_report_zip(posts: list[PostTime], *, title: str, zone: Zone | None = None) -> bytes:
    """ZIP em memória com relatorio.txt, resumo.csv e posts.csv. `zone`: fuso de apresentação (`export_time.Zone`);
    None = fuso padrão do Media Tool."""
    zone = zone or default_zone()
    now = datetime.now(UTC)
    accounts = analyze(posts, zone)
    text = build_text(accounts, title=title, generated_at=_to_local(now, zone), zone_text=zone_label(zone, now))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(_entry("relatorio.txt"), text.encode("utf-8"))
        archive.writestr(_entry("resumo.csv"), _csv_text(SUMMARY_COLUMNS, summary_rows(accounts)))
        archive.writestr(_entry("posts.csv"), _csv_text(POST_COLUMNS, post_rows(accounts)))
    return buffer.getvalue()


def report_filename(title: str, generated_at: datetime) -> str:
    """"padroes_<título saneado>_AAAA-MM-DD_HHMM.zip". `generated_at` sem fuso é usado como está (já local); com fuso
    é convertido para o fuso padrão do Media Tool."""
    if generated_at.tzinfo is not None:
        generated_at = _to_local(generated_at, default_zone())
    name = re.sub(r"[\s_]+", "_", sanitize_component(title).replace(".", "_"))[:TITLE_MAX_LENGTH].strip("_ ")
    stamp = generated_at.strftime("%Y-%m-%d_%H%M")
    return f"padroes_{name}_{stamp}.zip" if name else f"padroes_{stamp}.zip"
