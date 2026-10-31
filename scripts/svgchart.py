"""
Server-side SVG charts (no JavaScript library), in the site's retro palette.
Adapted from the NFL site's svgchart.py.

  line_chart                 one or more series on a shared x-axis, optional
                             dashed reference lines (50%, break-even)
  bar_chart_with_threshold   one bar per game vs the line: green = above,
                             red = below, white tick = the line that game had,
                             dashed hollow bar = today's projection
"""

from __future__ import annotations

import math
from html import escape

GREEN, RED, YELLOW, CYAN, PINK = "#3dff8f", "#ff3d57", "#ffc531", "#4f86ff", "#ff6a13"
GRID, AXIS, TEXT = "rgba(255,255,255,0.07)", "#ab9cc8", "#fff3e3"
FONT = 'font-family="VT323, ui-monospace, monospace"'
FONTB = 'font-family="Oxanium, ui-sans-serif, sans-serif" font-weight="700"'


def _fmt(v: float) -> str:
    return f"{v:.0f}" if abs(v) >= 10 or float(v).is_integer() else f"{v:.1f}"


def line_chart(series: list[dict], width: int = 640, height: int = 220, y_min: float = 0, y_max: float = 100,
               references: list[dict] | None = None, x_labels: tuple[str, str] | None = None,
               y_suffix: str = "", label: str = "", point_labels: list[str] | None = None,
               show_values=False, value_fmt: str = "{:.0f}", fill_zero: bool = False, big: bool = False) -> str:
    """series: [{"points": [y or None, ...], "color": "#...", "name": "..."}]
    references: [{"y": 50, "color": "#...", "label": "coin flip"}]
    point_labels: one x label per point (drawn when there are 14 or fewer).
    show_values: print each point's value above it (14 or fewer points).
    fill_zero: shade above/below 0 green/red (for running units)."""
    pad_l, pad_r, pad_t, pad_b = (58, 56, 26, 30) if big else (46, 18, 22 if show_values else 14, 26)
    fs = 15 if big else 13
    F = FONTB if big else FONT
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b

    def x_at(i, n):
        return pad_l + (pw * i / (n - 1) if n > 1 else pw / 2)

    def y_at(v):
        v = max(y_min, min(y_max, v))
        return pad_t + ph - (ph * (v - y_min) / (y_max - y_min) if y_max > y_min else 0)

    out = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img" '
           f'aria-label="{escape(label)}" style="width:100%;height:auto;display:block">']
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        v = y_min + (y_max - y_min) * frac
        y = y_at(v)
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" stroke="{GRID}"/>')
        out.append(f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end" font-size="{fs}" fill="{AXIS}" {F}>{_fmt(v)}{y_suffix}</text>')
    for ref in references or []:
        ry = y_at(ref["y"])
        out.append(f'<line x1="{pad_l}" y1="{ry:.1f}" x2="{width - pad_r}" y2="{ry:.1f}" stroke="{ref.get("color", RED)}" '
                   f'stroke-width="1.5" stroke-dasharray="5,4" opacity="0.8"/>')
        if ref.get("label"):
            ly = ry + 13 if ref.get("below") else ry - 4
            anchor, lx = ("start", pad_l + 4) if ref.get("left") else ("end", width - pad_r)
            out.append(f'<text x="{lx}" y="{ly:.1f}" text-anchor="{anchor}" font-size="{fs}" fill="{ref.get("color", RED)}" {F}>{escape(ref["label"])}</text>')
    if fill_zero and y_min < 0 < y_max:
        zy = y_at(0)
        out.append(f'<rect x="{pad_l}" y="{pad_t}" width="{pw}" height="{zy - pad_t:.1f}" fill="{GREEN}" opacity="0.06"/>')
        out.append(f'<rect x="{pad_l}" y="{zy:.1f}" width="{pw}" height="{pad_t + ph - zy:.1f}" fill="{RED}" opacity="0.06"/>')
        out.append(f'<line x1="{pad_l}" y1="{zy:.1f}" x2="{width - pad_r}" y2="{zy:.1f}" stroke="{AXIS}" stroke-width="1.5"/>')
    for s in series:
        pts = s.get("points") or []
        n, color = len(pts), s.get("color", CYAN)
        seg = []
        for i, v in enumerate(pts):
            if v is None:
                seg.append(None)
            else:
                seg.append((x_at(i, n), y_at(v)))
        d, pen = [], False
        for p in seg:
            if p is None:
                pen = False
                continue
            d.append(f'{"L" if pen else "M"}{p[0]:.1f},{p[1]:.1f}')
            pen = True
        if d:
            out.append(f'<path d="{" ".join(d)}" fill="none" stroke="{color}" stroke-width="2"/>')
        r = 4 if n <= 14 else 3 if n <= 60 else 2
        for i, p in enumerate(seg):
            if p:
                out.append(f'<circle cx="{p[0]:.1f}" cy="{p[1]:.1f}" r="{r}" fill="{color}"/>')
                if (show_values == "last" and i == max(j for j, q in enumerate(seg) if q)) or (show_values is True and n <= 14):
                    last = show_values == "last"
                    out.append(f'<text x="{p[0] + (8 if last else 0):.1f}" y="{p[1] + (6 if last else -9):.1f}" text-anchor="{"start" if last else "middle"}" font-size="{fs + 3}" fill="{color}" '
                               f'{F}>{escape(value_fmt.format(pts[i]))}</text>')
    if point_labels and len(point_labels) <= 14:
        n = len(point_labels)
        for i, lab in enumerate(point_labels):
            out.append(f'<text x="{x_at(i, n):.1f}" y="{height - 6}" text-anchor="middle" font-size="{fs}" fill="{AXIS}" {F}>{escape(lab)}</text>')
    elif x_labels:
        out.append(f'<text x="{pad_l}" y="{height - 6}" font-size="{fs}" fill="{AXIS}" {F}>{escape(x_labels[0])}</text>')
        out.append(f'<text x="{width - pad_r}" y="{height - 6}" text-anchor="end" font-size="{fs}" fill="{AXIS}" {F}>{escape(x_labels[1])}</text>')
    out.append("</svg>")
    return "".join(out)


def bar_chart_with_threshold(bars: list[dict], width: int = 640, height: int = 190, line: float | None = None,
                             baseline: float | None = None, label: str = "") -> str:
    """bars: [{"label": "10/22", "value": 24, "threshold": 22.5 | None,
               "status": "over" | "under" | "push" | "projection"}]
    line: today's line, drawn as a dashed horizontal across the chart.
    baseline: dotted season-average line."""
    pad_l, pad_r, pad_t, pad_b = 34, 10, 16, 24
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b
    n = max(1, len(bars))
    vals = [b["value"] for b in bars if b.get("value") is not None]
    vals += [b["threshold"] for b in bars if b.get("threshold") is not None]
    vals += [v for v in (line, baseline) if v is not None]
    y_max = max(vals) * 1.18 if vals else 10
    y_max = max(y_max, 1)

    def y_at(v):
        v = max(0, min(y_max, v))
        return pad_t + ph - ph * v / y_max

    colors = {"over": GREEN, "under": RED, "push": AXIS, "projection": YELLOW}
    gap = pw / n
    bw = gap * 0.62
    out = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img" '
           f'aria-label="{escape(label)}" style="width:100%;height:auto;display:block">']
    for frac in (0, 0.5, 1.0):
        v = y_max * frac
        y = y_at(v)
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" stroke="{GRID}"/>')
        out.append(f'<text x="{pad_l - 5}" y="{y + 4:.1f}" text-anchor="end" font-size="13" fill="{AXIS}" {FONT}>{_fmt(v)}</text>')
    if baseline is not None:
        by = y_at(baseline)
        out.append(f'<line x1="{pad_l}" y1="{by:.1f}" x2="{width - pad_r}" y2="{by:.1f}" stroke="{TEXT}" stroke-width="1.5" stroke-dasharray="2,4" opacity="0.7"/>')
    if line is not None:
        ly = y_at(line)
        out.append(f'<line x1="{pad_l}" y1="{ly:.1f}" x2="{width - pad_r}" y2="{ly:.1f}" stroke="{YELLOW}" stroke-width="1.5" stroke-dasharray="6,4" opacity="0.9"/>')
    for i, b in enumerate(bars):
        cx = pad_l + gap * i + gap / 2
        v = b.get("value")
        color = colors.get(b.get("status"), AXIS)
        if v is not None:
            top = y_at(v)
            h = max(pad_t + ph - top, 1.5)
            if b.get("status") == "projection":
                out.append(f'<rect x="{cx - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{h:.1f}" fill="none" '
                           f'stroke="{color}" stroke-width="2" stroke-dasharray="4,3"/>')
            else:
                out.append(f'<rect x="{cx - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{h:.1f}" fill="{color}" opacity="0.85"/>')
            if n <= 12:
                out.append(f'<text x="{cx:.1f}" y="{top - 4:.1f}" text-anchor="middle" font-size="13" fill="{TEXT}" {FONT}>{_fmt(v)}</text>')
        t = b.get("threshold")
        if t is not None:
            ty = y_at(t)
            out.append(f'<line x1="{cx - bw / 2 - 3:.1f}" y1="{ty:.1f}" x2="{cx + bw / 2 + 3:.1f}" y2="{ty:.1f}" stroke="{TEXT}" stroke-width="2.5"/>')
        out.append(f'<text x="{cx:.1f}" y="{height - 6}" text-anchor="middle" font-size="13" fill="{AXIS}" {FONT}>{escape(str(b.get("label", "")))}</text>')
    out.append("</svg>")
    return "".join(out)


def hbar_vs_need(rows: list[dict], width: int = 640, label: str = "", need_label: str = "needed",
                 lo: float = 30, hi: float = 75) -> str:
    """One horizontal bar per row: its hit rate, green when it beats what it needs, red when it doesn't,
    with a white tick at the needed rate. rows: [{"name", "value", "need", "n"}] (percents)."""
    rh, pad_l, pad_r, pad_t = 34, 150, 118, 22
    height = pad_t + rh * len(rows) + 22
    pw = width - pad_l - pad_r

    def x_at(v):
        return pad_l + pw * (max(lo, min(hi, v)) - lo) / (hi - lo)

    out = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img" '
           f'aria-label="{escape(label)}" style="width:100%;height:auto;display:block">']
    for v in range(int(lo), int(hi) + 1, 5 if hi - lo <= 50 else 10):
        x = x_at(v)
        out.append(f'<line x1="{x:.1f}" y1="{pad_t - 6}" x2="{x:.1f}" y2="{height - 20}" stroke="{GRID}"/>')
        if v % 10 == 0:
            out.append(f'<text x="{x:.1f}" y="{height - 6}" text-anchor="middle" font-size="13" fill="{AXIS}" {FONT}>{v}%</text>')
    for i, r in enumerate(rows):
        y = pad_t + rh * i
        good = r["value"] >= r["need"]
        c = GREEN if good else RED
        out.append(f'<text x="{pad_l - 8}" y="{y + rh / 2 + 4:.1f}" text-anchor="end" font-size="14" fill="{TEXT}" {FONTB}>{escape(r["name"])}</text>')
        out.append(f'<rect x="{pad_l}" y="{y + 7}" width="{max(x_at(r["value"]) - pad_l, 2):.1f}" height="{rh - 14}" fill="{c}" opacity="0.85"/>')
        nx = x_at(r["need"])
        out.append(f'<line x1="{nx:.1f}" y1="{y + 3}" x2="{nx:.1f}" y2="{y + rh - 3}" stroke="{TEXT}" stroke-width="3"/>')
        out.append(f'<text x="{width - pad_r + 6}" y="{y + rh / 2 + 5:.1f}" font-size="16" fill="{c}" {FONTB}>{r["value"]:.0f}%</text>')
        if r.get("rec"):
            out.append(f'<text x="{width - pad_r + 52}" y="{y + rh / 2 + 5:.1f}" font-size="14" fill="{AXIS}" {FONT}>{escape(r["rec"])}</text>')
    out.append(f'<text x="{width - 4}" y="{12}" text-anchor="end" font-size="13" fill="{AXIS}" {FONT}>white tick = {escape(need_label)}</text>')
    out.append("</svg>")
    return "".join(out)


def running_units(dates: list[str], daily: list[float], width: int = 640, height: int = 200, label: str = "",
                  color: str = YELLOW) -> str:
    """Cumulative units by day, shaded green above 0 and red below."""
    run, t = [], 0.0
    for v in daily:
        t += v
        run.append(round(t, 2))
    mn, mx = min(run + [0]), max(run + [0])
    # a clean scale: 4 equal steps of a round size, with 0 on a gridline
    for step in (0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100, 200, 500):
        neg = math.ceil(-mn / step - 1e-9) if mn < 0 else 0
        pos = math.ceil(mx / step - 1e-9) if mx > 0 else 0
        if neg + pos <= 4:
            while neg + pos < 4:
                if mx >= -mn:
                    pos += 1
                else:
                    neg += 1
            break
    lo, hi = -neg * step, pos * step
    labs = [d[5:].replace("-", "/") for d in dates]
    return line_chart([{"points": run, "color": color}], width=width, height=height, y_min=lo, y_max=hi,
                      y_suffix="U", label=label, fill_zero=True, show_values="last", value_fmt="{:+.1f}U", big=True,
                      point_labels=labs if len(labs) <= 14 else None,
                      x_labels=(labs[0], labs[-1]) if labs else None)
