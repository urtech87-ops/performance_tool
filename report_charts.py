#!/usr/bin/env python3
"""
Inline SVG for the report's data visualisation.

Everything here is a pure function returning an SVG string: no chart library,
no canvas, no JavaScript. That is deliberate - the same markup has to survive
three trips (browser, browser print dialog, and the paged.js PDF pass) and
render identically in all three. Colour comes from CSS custom properties so
the brand and the status palette stay in one place; every shape also carries a
`fill`/`stroke` fallback for renderers that resolve `var()` late.

Geometry uses stroke-dasharray on a circle rather than arc paths: it is
sub-pixel accurate at any size and never produces the sliver artefacts arcs
give at 0% and 100%.
"""

import html
import math

import design_tokens
from design_tokens import var as _var

# The literal fallbacks are the design system's own values, read from it rather
# than copied - a chart cannot drift away from the palette the stylesheet uses.
BAND_VAR = {band: f"--{band}" for band in design_tokens.STATUS_COLORS}
BAND_FALLBACK = dict(design_tokens.STATUS_COLORS)

SEVERITY_VAR = {sev: f"--sev-{sev}"
                for sev in ("critical", "serious", "moderate", "minor")}
SEVERITY_FALLBACK = {sev: design_tokens.SEVERITY_COLORS[sev] for sev in SEVERITY_VAR}


def _color(band):
    """var() with a literal fallback - safe in every renderer we target."""
    fallback = BAND_FALLBACK.get(band, BAND_FALLBACK["na"])
    return f"var({BAND_VAR.get(band, '--na')},{fallback})"


def _sev_color(severity):
    fallback = SEVERITY_FALLBACK.get(severity, SEVERITY_FALLBACK["minor"])
    return f"var({SEVERITY_VAR.get(severity, '--sev-minor')},{fallback})"


def _band(score):
    if score is None:
        return "na"
    if score >= 90:
        return "good"
    if score >= 50:
        return "avg"
    return "poor"


def _esc(text):
    return html.escape(str(text), quote=True)


# --------------------------------------------------------------------------
# score ring
# --------------------------------------------------------------------------

def score_ring(score, label="", size=104, stroke=9, band=None, sublabel=""):
    """A donut gauge: track + arc + the number in the middle.

    `score` is 0-100 or None ("not run" draws the track only)."""
    band = band or _band(score)
    color = _color(band)
    radius = (size - stroke) / 2.0
    circumference = 2 * math.pi * radius
    fraction = 0.0 if score is None else max(0.0, min(1.0, score / 100.0))
    dash = circumference * fraction
    center = size / 2.0
    text = "-" if score is None else str(int(round(score)))
    font = max(15, int(size * 0.29))

    label_html = ""
    if label:
        label_html = (f'<text class="ring-label" x="{center}" y="{center + font * 0.78:.1f}" '
                      f'text-anchor="middle" font-size="{max(8, int(size * 0.115))}">'
                      f'{_esc(label)}</text>')

    return (
        f'<svg class="ring ring-{band}" viewBox="0 0 {size} {size}" width="{size}" '
        f'height="{size}" role="img" aria-label="{_esc(sublabel or label)} '
        f'{"not measured" if score is None else str(text) + " out of 100"}">'
        f'<circle cx="{center}" cy="{center}" r="{radius:.2f}" fill="none" '
        f'stroke="{_var("--line")}" stroke-width="{stroke}"/>'
        f'<circle cx="{center}" cy="{center}" r="{radius:.2f}" fill="none" '
        f'stroke="{color}" stroke-width="{stroke}" stroke-linecap="round" '
        f'stroke-dasharray="{dash:.2f} {circumference - dash:.2f}" '
        f'transform="rotate(-90 {center} {center})"/>'
        f'<text class="ring-value" x="{center}" y="{center + font * 0.34:.1f}" '
        f'text-anchor="middle" font-size="{font}" font-weight="700" fill="{color}">{text}</text>'
        f'{label_html}</svg>'
    )


# --------------------------------------------------------------------------
# overall health gauge
# --------------------------------------------------------------------------

def health_gauge(score, label="Overall health", width=260, thickness=16):
    """A 240-degree open dial for the single headline number."""
    band = _band(score)
    color = _color(band)
    sweep = 240.0
    start = 150.0                       # degrees, clockwise from 3 o'clock
    radius = (width - thickness) / 2.0
    cx = width / 2.0
    cy = width / 2.0

    def point(deg):
        rad = math.radians(deg)
        return cx + radius * math.cos(rad), cy + radius * math.sin(rad)

    def arc(from_deg, to_deg):
        x1, y1 = point(from_deg)
        x2, y2 = point(to_deg)
        large = 1 if abs(to_deg - from_deg) > 180 else 0
        return (f'M {x1:.2f} {y1:.2f} A {radius:.2f} {radius:.2f} 0 {large} 1 '
                f'{x2:.2f} {y2:.2f}')

    fraction = 0.0 if score is None else max(0.0, min(1.0, score / 100.0))
    height = int(width * 0.74)
    value = "-" if score is None else str(int(round(score)))

    ticks = []
    for mark in (0, 50, 90, 100):
        deg = start + sweep * (mark / 100.0)
        x1, y1 = point(deg)
        rad = math.radians(deg)
        x2 = cx + (radius - thickness * 0.95) * math.cos(rad)
        y2 = cy + (radius - thickness * 0.95) * math.sin(rad)
        ticks.append(f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
                     f'stroke="{_var("--paper")}" stroke-width="2" opacity=".55"/>')

    needle = ""
    if score is not None:
        deg = start + sweep * fraction
        x, y = point(deg)
        needle = (f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{thickness * 0.42:.1f}" '
                  f'fill="{_var("--paper")}" stroke="{color}" stroke-width="3"/>')

    return (
        f'<svg class="gauge gauge-{band}" viewBox="0 0 {width} {height}" width="{width}" '
        f'height="{height}" role="img" aria-label="{_esc(label)} '
        f'{"not determined" if score is None else value + " out of 100"}">'
        f'<path d="{arc(start, start + sweep)}" fill="none" stroke="{_var("--line")}" '
        f'stroke-width="{thickness}" stroke-linecap="round"/>'
        + (f'<path d="{arc(start, start + sweep * fraction)}" fill="none" stroke="{color}" '
           f'stroke-width="{thickness}" stroke-linecap="round"/>' if fraction > 0 else "")
        + "".join(ticks) + needle +
        f'<text class="gauge-value" x="{cx}" y="{cy + width * 0.05:.1f}" text-anchor="middle" '
        f'font-size="{int(width * 0.28)}" font-weight="800" fill="{color}">{value}</text>'
        f'<text class="gauge-cap" x="{cx}" y="{cy + width * 0.16:.1f}" text-anchor="middle" '
        f'font-size="{int(width * 0.055)}" fill="{_var("--ink-3")}" '
        f'letter-spacing=".08em">OUT OF 100</text>'
        f'</svg>'
    )


# --------------------------------------------------------------------------
# severity distribution
# --------------------------------------------------------------------------

def severity_bar(distribution, order=("critical", "serious", "moderate", "minor"),
                 width=640, height=34, radius=8, labels=None, uid="sev"):
    """One horizontal bar split by severity, with the count written into any
    segment wide enough to hold it."""
    labels = labels or {k: k.title() for k in order}
    total = sum(max(0, int(distribution.get(k, 0))) for k in order)
    if total <= 0:
        return (f'<svg class="sevbar empty" viewBox="0 0 {width} {height}" width="100%" '
                f'height="{height}" role="img" aria-label="No findings">'
                f'<rect x="0" y="0" width="{width}" height="{height}" rx="{radius}" '
                f'fill="{_var("--good-soft")}"/>'
                f'</svg>')

    parts, x = [], 0.0
    for key in order:
        count = max(0, int(distribution.get(key, 0)))
        if not count:
            continue
        w = width * count / total
        # No text inside the bar: it is stretched to the container width, and
        # non-uniform scaling would squash the glyphs. The counts live in the
        # legend underneath, where they scale with the body type.
        parts.append(
            f'<rect x="{x:.2f}" y="0" width="{w:.2f}" height="{height}" '
            f'fill="{_sev_color(key)}"><title>{_esc(labels.get(key, key))}: {count}</title></rect>')
        x += w

    clip = f"clip-{_esc(uid)}"
    return (f'<svg class="sevbar" viewBox="0 0 {width} {height}" width="100%" height="{height}" '
            f'preserveAspectRatio="none" role="img" '
            f'aria-label="Findings by severity, {total} in total">'
            f'<defs><clipPath id="{clip}"><rect x="0" y="0" width="{width}" height="{height}" '
            f'rx="{radius}"/></clipPath></defs>'
            f'<g clip-path="url(#{clip})">{"".join(parts)}</g></svg>')


def severity_columns(distribution, order=("critical", "serious", "moderate", "minor"),
                     width=420, height=150, labels=None):
    """The same numbers as a column chart, for the deep-dive pages where the
    reader is comparing categories rather than reading one total."""
    labels = labels or {k: k.title() for k in order}
    counts = [max(0, int(distribution.get(k, 0))) for k in order]
    peak = max(counts + [1])
    pad_bottom, pad_top = 30, 14
    plot = height - pad_bottom - pad_top
    slot = width / float(len(order))
    bar_w = min(56.0, slot * 0.54)

    bars = []
    for i, (key, count) in enumerate(zip(order, counts)):
        h = plot * (count / peak) if count else 0
        x = slot * i + (slot - bar_w) / 2
        y = pad_top + (plot - h)
        if count:
            bars.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
                        f'rx="5" fill="{_sev_color(key)}"/>')
            bars.append(f'<text x="{x + bar_w / 2:.1f}" y="{y - 5:.1f}" text-anchor="middle" '
                        f'font-size="12" font-weight="700" fill="{_sev_color(key)}">{count}</text>')
        else:
            bars.append(f'<rect x="{x:.1f}" y="{pad_top + plot - 3:.1f}" width="{bar_w:.1f}" '
                        f'height="3" rx="1.5" fill="{_var("--line")}"/>')
        bars.append(f'<text x="{x + bar_w / 2:.1f}" y="{height - 10}" text-anchor="middle" '
                    f'font-size="11" fill="{_var("--ink-3")}">'
                    f'{_esc(labels.get(key, key))}</text>')

    baseline = (f'<line x1="0" y1="{pad_top + plot:.1f}" x2="{width}" y2="{pad_top + plot:.1f}" '
                f'stroke="{_var("--line")}" stroke-width="1"/>')
    return (f'<svg class="sevcols" viewBox="0 0 {width} {height}" width="100%" '
            f'height="{height}" role="img" aria-label="Findings by severity">'
            f'{baseline}{"".join(bars)}</svg>')


# --------------------------------------------------------------------------
# pages x categories heatmap
# --------------------------------------------------------------------------

def heatmap(matrix, max_rows=26, cell=38, gap=4, label_width=250, row_height=26):
    """Pages down the side, categories across the top, one coloured cell each.

    Score cells carry the score; finding-count cells carry the count. Both use
    the same three-step palette so the eye can scan a column for trouble
    without reading any numbers.
    """
    cols = matrix.get("cols") or []
    rows = (matrix.get("rows") or [])[:max_rows]
    if not cols or not rows:
        return ""

    head = 30
    width = label_width + len(cols) * (cell + gap)
    height = head + len(rows) * (row_height + gap) + 6

    out = [f'<svg class="heatmap" viewBox="0 0 {width} {height}" width="100%" '
           f'height="{height}" role="img" '
           f'aria-label="Score heatmap, {len(rows)} page{"" if len(rows) == 1 else "s"} '
           f'by {len(cols)} categor{"y" if len(cols) == 1 else "ies"}">']

    for c, col in enumerate(cols):
        x = label_width + c * (cell + gap) + cell / 2
        out.append(f'<text x="{x:.1f}" y="{head - 12}" text-anchor="middle" font-size="9.5" '
                   f'font-weight="700" letter-spacing=".04em" fill="{_var("--ink-3")}">'
                   f'{_esc(_abbrev(col["label"]))}</text>')

    for r, row in enumerate(rows):
        y = head + r * (row_height + gap)
        path = row.get("path") or "/"
        shown = path if len(path) <= 38 else path[:36] + "..."
        device = f' ({row["device"]})' if row.get("device") else ""
        out.append(f'<text x="0" y="{y + row_height * 0.68:.1f}" font-size="11" '
                   f'fill="{_var("--ink-2")}">{_esc(shown + device)}</text>')
        for c, cellv in enumerate(row.get("cells") or []):
            x = label_width + c * (cell + gap)
            band = cellv.get("band", "na")
            text = cellv.get("text", "-")
            fill = _color(band)
            out.append(
                f'<rect x="{x}" y="{y}" width="{cell}" height="{row_height}" rx="5" '
                f'fill="{fill}" fill-opacity="{0.16 if band == "na" else 0.92}">'
                f'<title>{_esc(path)} - {_esc(cellv.get("key", ""))}: {_esc(text)}</title></rect>'
                f'<text x="{x + cell / 2:.1f}" y="{y + row_height * 0.68:.1f}" '
                f'text-anchor="middle" font-size="11" font-weight="700" '
                f'fill="{_var("--ink-3") if band == "na" else "#fff"}">{_esc(text)}</text>')

    out.append("</svg>")
    return "".join(out)


_ABBREV = {
    "Performance": "PERF",
    "Accessibility": "A11Y",
    "Best Practices": "BEST",
    "SEO": "SEO",
    "Security": "SEC",
    "WCAG Standards": "WCAG",
}


def abbrev(label):
    """The short column label used by the heatmap and the page table, so the
    two grids read as the same grid."""
    return _ABBREV.get(label, str(label)[:4].upper())


_abbrev = abbrev


# --------------------------------------------------------------------------
# small inline marks
# --------------------------------------------------------------------------

def effort_pips(effort):
    """Three pips, filled to show how much work an issue is."""
    filled = {"quick": 1, "moderate": 2, "involved": 3}.get(effort, 2)
    pips = "".join(
        f'<circle cx="{6 + i * 11}" cy="6" r="4.2" '
        f'fill="{"currentColor" if i < filled else _var("--line")}"/>'
        for i in range(3))
    return (f'<svg class="pips" viewBox="0 0 34 12" width="34" height="12" '
            f'role="img" aria-label="Effort: {_esc(effort)}">{pips}</svg>')


def spark_ratio(value, total, width=54, height=8):
    """A tiny proportion bar used in table cells."""
    frac = 0.0 if not total else max(0.0, min(1.0, value / float(total)))
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" '
            f'height="{height}" aria-hidden="true">'
            f'<rect x="0" y="0" width="{width}" height="{height}" rx="{height / 2}" '
            f'fill="{_var("--line-2")}"/>'
            f'<rect x="0" y="0" width="{width * frac:.1f}" height="{height}" '
            f'rx="{height / 2}" fill="currentColor"/></svg>')
