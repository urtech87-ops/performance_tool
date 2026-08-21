#!/usr/bin/env python3
"""
White-label branding for the consolidated report.

A small JSON file decides who the report belongs to and what it looks like:

    {
      "agency_name":   "Northwind Digital",
      "agency_logo":   "assets/northwind.svg",
      "agency_url":    "https://northwind.example",
      "client_name":   "Acme Retail",
      "client_logo":   "assets/acme.png",
      "primary":       "#2f5bea",
      "secondary":     "#7c3aed",
      "report_title":  "Website Health Report",
      "contact":       "hello@northwind.example",
      "footer_note":   "Confidential - prepared for Acme Retail"
    }

Everything is optional; anything missing falls back to a neutral default so the
report always renders. Logos are inlined as data URIs, which keeps report.html
a single portable file and makes the PDF pass independent of the filesystem.

Resolution order for the config (first hit wins):
    1. what the caller passes in (path or dict)
    2. $PERF_REPORT_BRAND
    3. brand.json next to the report being written
    4. brand.json next to this file
    5. built-in defaults
"""

import base64
import json
import mimetypes
import os
import re
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
BRAND_ENV = "PERF_REPORT_BRAND"
BRAND_FILENAME = "brand.json"

DEFAULTS = {
    "agency_name": "Site Quality Audit",
    "agency_logo": "",
    "agency_url": "",
    "client_name": "",
    "client_logo": "",
    "primary": "#2f5bea",
    "secondary": "#7c3aed",
    "ink": "#0f172a",
    "report_title": "Website Health Report",
    "report_subtitle": "Performance, accessibility, SEO and security",
    "contact": "",
    "footer_note": "",
    "font_stack": ("'Inter','Segoe UI',-apple-system,BlinkMacSystemFont,"
                   "Roboto,Helvetica,Arial,sans-serif"),
    "mono_stack": "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace",
}

# Scores and severities keep their own semantic palette: a brand colour never
# gets to decide whether a failure looks like a failure.
STATUS_COLORS = {
    "good": "#0f8a4d",
    "avg": "#c07a00",
    "poor": "#cf2f3f",
    "na": "#94a3b8",
}

SEVERITY_COLORS = {
    "critical": "#a8123b",
    "serious": "#d1451b",
    "moderate": "#c07a00",
    "minor": "#3b7dbf",
    "info": "#64748b",
}

HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
IMAGE_SUFFIXES = {".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif"}
MAX_LOGO_BYTES = 2_000_000


# --------------------------------------------------------------------------
# colour helpers
# --------------------------------------------------------------------------

def _clean_hex(value, fallback):
    v = str(value or "").strip()
    if not HEX_RE.match(v):
        return fallback
    if len(v) == 4:  # #abc -> #aabbcc
        v = "#" + "".join(c * 2 for c in v[1:])
    return v.lower()


def hex_to_rgb(value):
    v = _clean_hex(value, "#000000").lstrip("#")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb):
    return "#" + "".join(f"{max(0, min(255, int(round(c)))):02x}" for c in rgb)


def mix(color, other, amount):
    """Blend `amount` (0..1) of `other` into `color`."""
    a, b = hex_to_rgb(color), hex_to_rgb(other)
    amount = max(0.0, min(1.0, float(amount)))
    return rgb_to_hex(tuple(x + (y - x) * amount for x, y in zip(a, b)))


def tint(color, amount):
    return mix(color, "#ffffff", amount)


def shade(color, amount):
    return mix(color, "#000000", amount)


def relative_luminance(color):
    def channel(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in hex_to_rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def readable_ink(background, light="#ffffff", dark="#0f172a"):
    """Pick whichever of light/dark reads better on `background`."""
    lum = relative_luminance(background)
    contrast_light = (relative_luminance(light) + 0.05) / (lum + 0.05)
    contrast_dark = (lum + 0.05) / (relative_luminance(dark) + 0.05)
    return light if contrast_light >= contrast_dark else dark


def rgba(color, alpha):
    r, g, b = hex_to_rgb(color)
    return f"rgba({r},{g},{b},{round(float(alpha), 3)})"


# --------------------------------------------------------------------------
# logos
# --------------------------------------------------------------------------

def _data_uri(path, base_dir=None):
    """Inline an image file as a data: URI. Returns "" when unusable."""
    if not path:
        return ""
    raw = str(path).strip()
    if raw.startswith("data:"):
        return raw
    p = Path(raw)
    if not p.is_absolute():
        for root in [d for d in (base_dir, APP_DIR, Path.cwd()) if d]:
            cand = Path(root) / p
            if cand.is_file():
                p = cand
                break
    if not p.is_file() or p.suffix.lower() not in IMAGE_SUFFIXES:
        return ""
    try:
        if p.stat().st_size > MAX_LOGO_BYTES:
            return ""
        blob = p.read_bytes()
    except OSError:
        return ""
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    if p.suffix.lower() == ".svg":
        mime = "image/svg+xml"
    return f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"


def monogram(name):
    """Initials for the fallback logo mark - "Northwind Digital" -> "ND"."""
    words = [w for w in re.split(r"[^A-Za-z0-9]+", str(name or "")) if w]
    if not words:
        return "SQ"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


# --------------------------------------------------------------------------
# the brand itself
# --------------------------------------------------------------------------

class Brand:
    """Resolved brand: names, inlined logos, and the colour tokens derived
    from the primary/secondary pair."""

    def __init__(self, config=None, base_dir=None):
        cfg = dict(DEFAULTS)
        for k, v in (config or {}).items():
            if k in DEFAULTS and v not in (None, ""):
                cfg[k] = v
        self.base_dir = Path(base_dir) if base_dir else None

        self.agency_name = str(cfg["agency_name"]).strip() or DEFAULTS["agency_name"]
        self.agency_url = str(cfg["agency_url"]).strip()
        self.client_name = str(cfg["client_name"]).strip()
        self.report_title = str(cfg["report_title"]).strip() or DEFAULTS["report_title"]
        self.report_subtitle = str(cfg["report_subtitle"]).strip()
        self.contact = str(cfg["contact"]).strip()
        self.footer_note = str(cfg["footer_note"]).strip()
        self.font_stack = str(cfg["font_stack"]).strip() or DEFAULTS["font_stack"]
        self.mono_stack = str(cfg["mono_stack"]).strip() or DEFAULTS["mono_stack"]

        self.primary = _clean_hex(cfg["primary"], DEFAULTS["primary"])
        self.secondary = _clean_hex(cfg["secondary"], DEFAULTS["secondary"])
        self.ink = _clean_hex(cfg["ink"], DEFAULTS["ink"])

        self.agency_logo = _data_uri(cfg["agency_logo"], self.base_dir)
        self.client_logo = _data_uri(cfg["client_logo"], self.base_dir)
        self.agency_monogram = monogram(self.agency_name)
        self.client_monogram = monogram(self.client_name) if self.client_name else ""

    # -- derived palette ---------------------------------------------------

    @property
    def on_primary(self):
        return readable_ink(self.primary)

    def tokens(self):
        """CSS custom properties: one place the whole report reads colour,
        type and spacing from."""
        p, s, ink = self.primary, self.secondary, self.ink
        t = {
            "--brand": p,
            "--brand-ink": self.on_primary,
            "--brand-dark": shade(p, 0.32),
            "--brand-deep": shade(p, 0.55),
            "--brand-soft": tint(p, 0.88),
            "--brand-softer": tint(p, 0.95),
            "--brand-line": tint(p, 0.72),
            "--brand2": s,
            "--brand2-soft": tint(s, 0.9),
            "--grad": f"linear-gradient(135deg,{p},{s})",
            "--grad-soft": f"linear-gradient(135deg,{tint(p, 0.9)},{tint(s, 0.92)})",
            "--cover-bg": f"linear-gradient(150deg,{shade(p, 0.55)},{shade(s, 0.35)})",
            "--ink": ink,
            "--ink-2": mix(ink, "#ffffff", 0.32),
            "--ink-3": mix(ink, "#ffffff", 0.52),
            "--line": mix(ink, "#ffffff", 0.88),
            "--line-2": mix(ink, "#ffffff", 0.94),
            "--paper": "#ffffff",
            "--paper-2": mix(ink, "#ffffff", 0.975),
            "--shadow": f"0 1px 2px {rgba(ink, .06)}, 0 8px 24px -12px {rgba(ink, .18)}",
            "--shadow-lg": f"0 24px 60px -28px {rgba(ink, .35)}",
            "--font": self.font_stack,
            "--mono": self.mono_stack,
        }
        for name, color in STATUS_COLORS.items():
            t[f"--{name}"] = color
            t[f"--{name}-soft"] = tint(color, 0.9)
        for name, color in SEVERITY_COLORS.items():
            t[f"--sev-{name}"] = color
            t[f"--sev-{name}-soft"] = tint(color, 0.9)
        return t

    def css_variables(self, indent="    "):
        return "\n".join(f"{indent}{k}: {v};" for k, v in self.tokens().items())

    def as_dict(self):
        return {
            "agency_name": self.agency_name,
            "agency_url": self.agency_url,
            "client_name": self.client_name,
            "report_title": self.report_title,
            "report_subtitle": self.report_subtitle,
            "contact": self.contact,
            "footer_note": self.footer_note,
            "primary": self.primary,
            "secondary": self.secondary,
            "has_agency_logo": bool(self.agency_logo),
            "has_client_logo": bool(self.client_logo),
        }


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def load_brand(source=None, near=None):
    """Resolve a Brand. `source` is a dict, a path, or None; `near` is the
    directory the report is being written to (searched for brand.json)."""
    near_dir = Path(near) if near else None
    if isinstance(source, Brand):
        return source
    if isinstance(source, dict):
        return Brand(source, base_dir=near_dir)

    candidates = []
    if source:
        candidates.append(Path(source))
    env = os.environ.get(BRAND_ENV, "").strip()
    if env:
        candidates.append(Path(env))
    if near_dir:
        candidates.append(near_dir / BRAND_FILENAME)
    candidates.append(APP_DIR / BRAND_FILENAME)

    for cand in candidates:
        if cand.is_file():
            cfg = _read_json(cand)
            if cfg is not None:
                return Brand(cfg, base_dir=cand.parent)
    return Brand({}, base_dir=near_dir)
