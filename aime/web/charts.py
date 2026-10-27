"""Small server-drawn charts for the dashboard. The dashboard runs no JavaScript (its CSP is script-src 'none'), so
charts are inline SVG and HTML, styled by style.css. Each one is drawn only from the numbers passed in; with no data
it says so instead of drawing empty axes. Every fragment is a literal template filled by Markup.format, which
escapes each value."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta

from markupsafe import Markup

EMPTY = Markup('<div class="chart-empty">{}</div>')


def weekly_buckets(dates: list[datetime | date | None], weeks: int = 8, today: date | None = None) -> list[tuple[str, int]]:
    """Counts per week (Monday to Sunday) over the last `weeks` weeks, oldest first, labelled by the Monday."""
    today = today or datetime.utcnow().date()
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
