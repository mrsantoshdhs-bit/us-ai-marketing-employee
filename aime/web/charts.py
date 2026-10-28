"""Small server-drawn charts for the dashboard. The dashboard runs no JavaScript (its CSP is script-src 'none'), so
charts are inline SVG and HTML, styled by style.css. Each one is drawn only from the numbers passed in; with no data
it says so instead of drawing empty axes. Every fragment is a literal template filled by Markup.format, which
escapes each value."""

from __future__ import annotations

import math
import re
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


def nice_ticks(top: float, count: int = 4) -> list[int]:
    """0 and evenly spaced round numbers up to at least `top`, for an axis: 159 gives 0, 50, 100, 150, 200."""
    if top <= 0:
        return [0, 1]
    raw = top / count
    magnitude = 10 ** math.floor(math.log10(raw)) if raw >= 1 else 1
    step = next(int(m * magnitude) for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw and int(m * magnitude) == m * magnitude)
    return [step * i for i in range(math.ceil(top / step) + 1)]


def smooth_path(points: list[tuple[float, float]], low: float, high: float) -> str:
    """An SVG path through the points as a smooth curve, never bending above `high` or below `low` (y grows down),
    so a spike to zero never dips under the axis."""
    if len(points) < 2:
        x, y = points[0] if points else (0.0, low)
        return f"M{x:.1f},{y:.1f}"
    clamp = lambda y: min(max(y, high), low)  # noqa: E731
    d = [f"M{points[0][0]:.1f},{points[0][1]:.1f}"]
    for i in range(len(points) - 1):
        p0, p1, p2 = points[max(i - 1, 0)], points[i], points[i + 1]
        p3 = points[min(i + 2, len(points) - 1)]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, clamp(p1[1] + (p2[1] - p0[1]) / 6))
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, clamp(p2[1] - (p3[1] - p1[1]) / 6))
        d.append(f"C{c1[0]:.1f},{c1[1]:.1f} {c2[0]:.1f},{c2[1]:.1f} {p2[0]:.1f},{p2[1]:.1f}")
    return " ".join(d)


def sparkline(values: list[int], tone: str, label: str) -> Markup:
    """A small smooth trend line with a soft gradient under it, for a KPI card; flat when there is nothing to show."""
    width, height, pad = 200, 48, 4
    top = max(values) if values and max(values) > 0 else 1
    step = (width - 2 * pad) / max(1, len(values) - 1)
    points = [(pad + i * step, height - pad - (height - 2 * pad) * v / top) for i, v in enumerate(values)] or [(pad, height - pad)]
    line = smooth_path(points, height - pad, pad)
    area = f"{line} L{points[-1][0]:.1f},{height} L{points[0][0]:.1f},{height} Z"
    gid = "spark-" + re.sub(r"[^a-z0-9]+", "-", f"{tone} {label}".lower()).strip("-")
    return Markup('<svg class="spark {}" viewBox="0 0 {} {}" preserveAspectRatio="none" role="img" aria-label="{}">'
                  '<defs><linearGradient id="{}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" class="stop-a"/>'
                  '<stop offset="1" class="stop-b"/></linearGradient></defs>'
                  '<path class="area" d="{}" fill="url(#{})"/><path class="line" d="{}"/></svg>').format(
        tone, width, height, label, gid, area, gid, line)


def bar_path(x: float, y: float, w: float, h: float, r: float = 6) -> str:
    """A bar standing on the axis with rounded top corners."""
    r = min(r, w / 2, h)
    return (f"M{x:.1f},{y + h:.1f}V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}H{x + w - r:.1f}"
            f"Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f}V{y + h:.1f}Z")


def combo_chart(labels: list[str], bars: list[tuple[str, list[int]]], line: tuple[str, list[int]], title: str,
                empty_text: str, height: int = 300, uid: str = "combo") -> Markup:
    """Grouped bars per day for one or two series (e.g. emails, leads) and a smooth line with markers for another
    (e.g. meetings), on round-number gridlines. Hovering a day highlights it and shows its values (CSS only)."""
    if not labels or (sum(sum(v) for _, v in bars) + sum(line[1])) == 0:
        return EMPTY.format(empty_text)
    width, left, right, bottom, top = 400, 38, 8, 30, 18
    plot = height - bottom - top
    base = top + plot
    ticks = nice_ticks(max([max(v) for _, v in bars] + [max(line[1])]))
    top_value = ticks[-1]
    step = (width - left - right) / len(labels)
    bar = min(24.0, step * .58 / max(1, len(bars)))
    totals = "; ".join(f"{name} {sum(v)}" for name, v in [*bars, line])
    parts = [Markup('<svg class="chart combo" viewBox="0 0 {} {}" role="img" aria-label="{}"><title>{}</title><desc>{}</desc>'
                    '<defs><linearGradient id="{}-s0" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#6a9bff"/>'
                    '<stop offset="1" stop-color="#2f6bff"/></linearGradient>'
                    '<linearGradient id="{}-s1" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#4be3a6"/>'
                    '<stop offset="1" stop-color="#12b76a"/></linearGradient>'
                    '<linearGradient id="{}-area" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#7c4dff" stop-opacity=".22"/>'
                    '<stop offset="1" stop-color="#7c4dff" stop-opacity="0"/></linearGradient></defs>').format(
        width, height, title, title, f"Totals: {totals}", uid, uid, uid)]
    for t in ticks:
        y = base - plot * t / top_value
        parts.append(Markup('<line class="gridline{}" x1="{}" x2="{}" y1="{:.1f}" y2="{:.1f}"/>'
                            '<text class="axis" x="{}" y="{:.1f}" text-anchor="end">{}</text>').format(
            " base" if t == 0 else "", left, width - right, y, y, left - 10, y + 4, f"{t:,}"))
    every = math.ceil(len(labels) / 10)
    name, values = line
    pts = [(left + step * i + step / 2, base - plot * v / top_value) for i, v in enumerate(values)]
    for i, label in enumerate(labels):
        x0 = left + step * i + (step - bar * len(bars)) / 2
        summary = " · ".join(f"{n} {v[i]}" for n, v in [*bars, line])
        parts.append(Markup('<g class="col"><title>{}: {}</title><rect class="col-hit" x="{:.1f}" y="{}" width="{:.1f}" '
                            'height="{}" rx="10"/>').format(label, summary, left + step * i + 2, top - 6, step - 4, plot + 6))
        for j, (_series, vals) in enumerate(bars):
            if vals[i]:
                h = plot * vals[i] / top_value
                parts.append(Markup('<path class="bar s{}" d="{}" fill="url(#{}-s{})"/>'
                                    '<text class="val" x="{:.1f}" y="{:.1f}" text-anchor="middle">{}</text>').format(
                    j, bar_path(x0 + j * bar + 1.5, base - h, bar - 3, h), uid, j, x0 + j * bar + bar / 2, base - h - 7,
                    vals[i]))
        if values[i]:
            parts.append(Markup('<text class="val line-val" x="{:.1f}" y="{:.1f}" text-anchor="middle">{}</text>').format(
                pts[i][0], pts[i][1] - 12, values[i]))
        parts.append(Markup("</g>"))
        if i % every == 0:
            parts.append(Markup('<text class="axis" x="{:.1f}" y="{}" text-anchor="middle">{}</text>').format(
                left + step * i + step / 2, height - 10, label))
    curve = smooth_path(pts, base, top)
    parts.append(Markup('<path class="trend-area" d="{} L{:.1f},{} L{:.1f},{} Z" fill="url(#{}-area)"/>'
                        '<path class="trend" d="{}"/>').format(curve, pts[-1][0], base, pts[0][0], base, uid, curve))
    for (x, y), v, label in zip(pts, values, labels, strict=False):
        parts.append(Markup('<circle class="trend-dot" cx="{:.1f}" cy="{:.1f}" r="5"><title>{}, {}: {}</title></circle>').format(
            x, y, name, label, v))
    parts.append(Markup("</svg>"))
    return Markup("").join(parts)
