"""Server-rendered SVG charts for the dashboard (no JS chart library).

Marks follow the data-viz specs: columns at most 24 px thick with a 4 px rounded
data end and a square baseline, hairline solid gridlines, one series colour,
clean y ticks. Colours come from CSS variables (see style.css), so light and dark
mode are handled there. Only numbers and dates are written into the SVG;
user-controlled text stays in the HTML templates, where Jinja escapes it.
"""

import html
import json
from datetime import date

from markupsafe import Markup

W, H = 300, 150
LEFT, RIGHT, TOP, BOTTOM = 34, 6, 8, 22
PLOT_W, PLOT_H = W - LEFT - RIGHT, H - TOP - BOTTOM
MAX_BAR = 24
CORNER = 4
GAP = 2


def nice_max(value: int) -> int:
    """Smallest 'round' number >= value whose half is also a whole number (1, 2, 4, 6, 8 x 10^k)."""
    if value <= 1:
        return 1
    magnitude = 1
    while True:
        for m in (1, 2, 4, 6, 8):
            if m * magnitude >= value and (m * magnitude) % 2 == 0:
                return m * magnitude
        magnitude *= 10


def ticks_for(top: int) -> list[int]:
    return [0, top] if top < 2 else [0, top // 2, top]


def format_int(n: int) -> str:
    return f"{n:,}"


def tick_label(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:g}M"
    if n >= 10_000:
        return f"{n / 1_000:g}K"
    return format_int(n)


def short_day(d: date) -> str:
    return f"{d.day} {d.strftime('%b')}"


def _bar_path(x: float, width: float, base: float, top: float) -> str:
    """Column with rounded top corners and a square bottom."""
    height = base - top
    r = min(CORNER, width / 2, height)
    return (f"M{x:.2f},{base:.2f} V{top + r:.2f} a{r:.2f},{r:.2f} 0 0 1 {r:.2f},{-r:.2f} "
            f"H{x + width - r:.2f} a{r:.2f},{r:.2f} 0 0 1 {r:.2f},{r:.2f} V{base:.2f} Z")


def column_chart(days: list[date], values: list[int], *, label: str, unit: str) -> Markup:
    """One-series column chart. ``label`` names it for assistive tech, ``unit`` for the tooltip."""
    n = len(days)
    top_value = nice_max(max(values, default=0))
    slot = PLOT_W / n
    width = min(MAX_BAR, slot - GAP)
    base = TOP + PLOT_H
    parts = []
    for t in ticks_for(top_value):
        y = base - PLOT_H * t / top_value
        parts.append(f'<line class="{"axis" if t == 0 else "grid"}" x1="{LEFT}" x2="{W - RIGHT}" y1="{y:.2f}" y2="{y:.2f}"/>')
        parts.append(f'<text class="tick" x="{LEFT - 4}" y="{y + 3:.2f}" text-anchor="end">{tick_label(t)}</text>')
    for i, v in enumerate(values):
        if v > 0:
            x = LEFT + i * slot + (slot - width) / 2
            top = base - PLOT_H * v / top_value
            parts.append(f'<path class="bar" data-i="{i}" d="{_bar_path(x, width, base, top)}"/>')
    for i in sorted({0, n // 3, 2 * n // 3, n - 1}):
        anchor = "start" if i == 0 else "end" if i == n - 1 else "middle"
        x = LEFT + (0 if i == 0 else PLOT_W if i == n - 1 else i * slot + slot / 2)
        parts.append(f'<text class="tick" x="{x:.2f}" y="{H - 6}" text-anchor="{anchor}">{short_day(days[i])}</text>')
    parts.append(f'<line class="cursor" x1="0" x2="0" y1="{TOP}" y2="{base}"/>')
    parts.append(f'<rect class="hit" x="{LEFT}" y="{TOP}" width="{PLOT_W}" height="{PLOT_H}" fill="none" pointer-events="all"/>')
    data = {"days": [d.isoformat() for d in days], "values": values}
    attrs = (f'viewBox="0 0 {W} {H}" role="img" tabindex="0" aria-label="{html.escape(label)}" '
             f'data-points="{html.escape(json.dumps(data), quote=True)}" data-unit="{html.escape(unit)}" '
             f'data-left="{LEFT}" data-plot-width="{PLOT_W}" data-view-width="{W}"')
    return Markup(f'<svg class="col-chart" {attrs}>{"".join(parts)}</svg>')


def rank_bar(share: float) -> Markup:
    """Thin horizontal bar, ``share`` in 0..1 of the available width. Rounded data end, square baseline."""
    pct = max(1.5, min(100.0, share * 100))
    return Markup(
        f'<svg class="rank-bar" width="100%" height="8" aria-hidden="true">'
        f'<rect class="fill" x="0" y="0" width="{pct:.2f}%" height="8" rx="4"/>'
        f'<rect class="fill" x="0" y="0" width="4" height="8"/></svg>')


def meter(used_fraction: float) -> Markup:
    """Meter: unfilled track is a lighter step of the same ramp; the fill shows severity."""
    pct = max(0.0, min(100.0, used_fraction * 100))
    severity = "critical" if pct >= 95 else "warn" if pct >= 85 else "ok"
    return Markup(
        f'<svg class="meter" width="100%" height="8" aria-hidden="true">'
        f'<rect class="track" x="0" y="0" width="100%" height="8" rx="4"/>'
        f'<rect class="fill {severity}" x="0" y="0" width="{pct:.2f}%" height="8" rx="4"/></svg>')
