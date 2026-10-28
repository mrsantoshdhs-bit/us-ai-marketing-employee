"""Small server-drawn charts for the dashboard. The dashboard runs no JavaScript (its CSP is script-src 'none'), so
charts are inline SVG and HTML, styled by style.css. Each one is drawn only from the numbers passed in; with no data
it says so instead of drawing empty axes. Every fragment is a literal template filled by Markup.format, which
escapes each value."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta

from markupsafe import Markup

from aime.timeutil import utcnow

EMPTY = Markup('<div class="chart-empty">{}</div>')


def weekly_buckets(dates: list[datetime | date | None], weeks: int = 8, today: date | None = None) -> list[tuple[str, int]]:
    """Counts per week (Monday to Sunday) over the last `weeks` weeks, oldest first, labelled by the Monday."""
    today = today or utcnow().date()
    start = today - timedelta(days=today.weekday()) - timedelta(weeks=weeks - 1)
    counts = [0] * weeks
    for d in dates:
        if d is None:
            continue
        day = d.date() if isinstance(d, datetime) else d
        i = (day - start).days // 7
        if 0 <= i < weeks:
            counts[i] += 1
    return [((start + timedelta(weeks=i)).strftime("%b %d"), counts[i]) for i in range(weeks)]


def column_chart(labels: list[str], series: list[tuple[str, list[int]]], title: str, empty_text: str,
                 height: int = 190) -> Markup:
    """Vertical bars per label, one or two series side by side (for example emails sent and replies)."""
    if not labels or sum(sum(values) for _, values in series) == 0:
        return EMPTY.format(empty_text)
    width, left, bottom, top = 420, 30, 26, 12
    plot = height - bottom - top
    top_value = max(max(values) for _, values in series)
    step = (width - left) / len(labels)
    bar = min(28.0, (step - 10) / len(series))
    parts = [Markup('<svg class="chart" viewBox="0 0 {} {}" role="img" aria-label="{}"><title>{}</title>').format(
        width, height, title, title)]
    for frac in (0, 0.5, 1):
        y = top + plot * (1 - frac)
        parts.append(Markup('<line class="gridline" x1="{}" x2="{}" y1="{:.1f}" y2="{:.1f}"/>'
                            '<text class="axis" x="{}" y="{:.1f}" text-anchor="end">{}</text>').format(
            left, width, y, y, left - 6, y + 4, round(top_value * frac)))
    for i, label in enumerate(labels):
        x0 = left + step * i + (step - bar * len(series)) / 2
        for j, (name, values) in enumerate(series):
            h = plot * values[i] / top_value
            parts.append(Markup('<rect class="bar s{}" x="{:.1f}" y="{:.1f}" width="{:.1f}" height="{:.1f}" rx="3">'
                                '<title>{}, week of {}: {}</title></rect>').format(
                j, x0 + j * bar, top + plot - h, bar - 3, h, name, label, values[i]))
        parts.append(Markup('<text class="axis" x="{:.1f}" y="{}" text-anchor="middle">{}</text>').format(
            left + step * i + step / 2, height - 6, label))
    parts.append(Markup("</svg>"))
    if len(series) > 1:
        parts.append(Markup('<div class="legend">'))
        for j, (name, values) in enumerate(series):
            parts.append(Markup('<span class="legend-item"><i class="swatch s{}"></i>{} ({})</span>').format(
                j, name, sum(values)))
        parts.append(Markup("</div>"))
    return Markup("").join(parts)


def bar_list(rows: list[tuple[str, float, str | None]], title: str, empty_text: str,
             fmt: Callable[[float], str] | None = None) -> Markup:
    """Horizontal bars with labels and values (a funnel, pipeline by stage); a row's label links when it has a URL."""
    if not rows or not any(value for _, value, _ in rows):
        return EMPTY.format(empty_text)
    top_value = max(value for _, value, _ in rows) or 1
    parts = [Markup('<div class="barlist" role="list" aria-label="{}">').format(title)]
    for label, value, href in rows:
        name = Markup('<a href="{}">{}</a>').format(href, label) if href else Markup("{}").format(label)
        parts.append(Markup('<div class="barlist-row" role="listitem"><div class="barlist-label">{}</div>'
                            '<div class="barlist-track"><div class="barlist-fill" style="width:{:.1f}%"></div></div>'
                            '<div class="barlist-value">{}</div></div>').format(
            name, 100 * value / top_value, fmt(value) if fmt else f"{value:,.0f}"))
    parts.append(Markup("</div>"))
    return Markup("").join(parts)


def daily_counts(dates: list[datetime | date | None], days: int, today: date | None = None) -> list[int]:
    """Counts per day over the last `days` days, oldest first (today last)."""
    today = today or utcnow().date()
    start = today - timedelta(days=days - 1)
    counts = [0] * days
    for d in dates:
        if d is None:
            continue
        i = ((d.date() if isinstance(d, datetime) else d) - start).days
        if 0 <= i < days:
            counts[i] += 1
    return counts


def sparkline(values: list[int], tone: str, label: str) -> Markup:
    """A small trend line with a soft area under it, for a KPI card; flat when there is nothing to show."""
    width, height, pad = 160, 40, 3
    top = max(values) if values and max(values) > 0 else 1
    step = (width - 2 * pad) / max(1, len(values) - 1)
    points = [(pad + i * step, height - pad - (height - 2 * pad) * v / top) for i, v in enumerate(values)] or [(pad, height - pad)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    area = f"{points[0][0]:.1f},{height} {line} {points[-1][0]:.1f},{height}"
    return Markup('<svg class="spark {}" viewBox="0 0 {} {}" preserveAspectRatio="none" role="img" aria-label="{}">'
                  '<polygon class="area" points="{}"/><polyline class="line" points="{}"/></svg>').format(
        tone, width, height, label, area, line)


def combo_chart(labels: list[str], bars: list[tuple[str, list[int]]], line: tuple[str, list[int]], title: str,
                empty_text: str, height: int = 230) -> Markup:
    """Bars per day for one or two series (e.g. emails, leads) and a line with dots for another (e.g. meetings)."""
    if not labels or (sum(sum(v) for _, v in bars) + sum(line[1])) == 0:
        return EMPTY.format(empty_text)
    width, left, bottom, top = 520, 34, 26, 14
    plot = height - bottom - top
    top_value = max([max(v) for _, v in bars] + [max(line[1])]) or 1
    step = (width - left) / len(labels)
    bar = min(16.0, (step - 8) / max(1, len(bars)))
    parts = [Markup('<svg class="chart combo" viewBox="0 0 {} {}" role="img" aria-label="{}"><title>{}</title>').format(
        width, height, title, title)]
    for frac in (0, 0.5, 1):
        y = top + plot * (1 - frac)
        parts.append(Markup('<line class="gridline" x1="{}" x2="{}" y1="{:.1f}" y2="{:.1f}"/>'
                            '<text class="axis" x="{}" y="{:.1f}" text-anchor="end">{}</text>').format(
            left, width, y, y, left - 6, y + 4, round(top_value * frac)))
    every = max(1, len(labels) // 10)
    for i, label in enumerate(labels):
        x0 = left + step * i + (step - bar * len(bars)) / 2
        for j, (name, values) in enumerate(bars):
            h = plot * values[i] / top_value
            parts.append(Markup('<rect class="bar s{}" x="{:.1f}" y="{:.1f}" width="{:.1f}" height="{:.1f}" rx="4">'
                                '<title>{}, {}: {}</title></rect>').format(
                j, x0 + j * bar, top + plot - h, max(bar - 3, 2), h, name, label, values[i]))
        if i % every == 0:
            parts.append(Markup('<text class="axis" x="{:.1f}" y="{}" text-anchor="middle">{}</text>').format(
                left + step * i + step / 2, height - 7, label))
    name, values = line
    pts = [(left + step * i + step / 2, top + plot - plot * v / top_value) for i, v in enumerate(values)]
    parts.append(Markup('<polyline class="trend" points="{}"/>').format(" ".join(f"{x:.1f},{y:.1f}" for x, y in pts)))
    for (x, y), v, label in zip(pts, values, labels, strict=False):
        parts.append(Markup('<circle class="trend-dot" cx="{:.1f}" cy="{:.1f}" r="4"><title>{}, {}: {}</title></circle>').format(
            x, y, name, label, v))
    parts.append(Markup("</svg>"))
    return Markup("").join(parts)
