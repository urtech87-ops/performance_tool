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

import design_tokens
# The colour maths and the palettes live in the design system, not here. This
# module's job is to resolve *who* the report belongs to; `design_tokens` turns
# the three colours it resolves into the full token set both surfaces read.
from design_tokens import (  # noqa: F401  (re-exported for callers)
    hex_to_rgb, mix, rgb_to_hex, rgba, readable_ink, relative_luminance, shade, tint,
)

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
    # The dashboard app's own name and strapline. Empty means "use the agency
    # name and the built-in strapline", so a brand.json written for the report
    # alone still re-skins the app.
    "app_name": "",
    "app_tagline": "",
    "contact": "",
    "footer_note": "",
    "font_stack": ("'Inter','Segoe UI',-apple-system,BlinkMacSystemFont,"
                   "Roboto,Helvetica,Arial,sans-serif"),
    "mono_stack": "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace",
}

# Scores and severities keep their own semantic palette: a brand colour never
# gets to decide whether a failure looks like a failure. Both live in
# design_tokens, which is the one place either surface reads colour from.
STATUS_COLORS = design_tokens.STATUS_COLORS
SEVERITY_COLORS = design_tokens.SEVERITY_COLORS
CATEGORY_COLORS = design_tokens.CATEGORY_COLORS

# What the dashboard says under its own name when a brand does not replace it.
DEFAULT_TAGLINE = ("Whole-site audits - performance, accessibility, SEO, "
                   "best practices, security and WCAG standards.")

HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
IMAGE_SUFFIXES = {".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif"}
MAX_LOGO_BYTES = 2_000_000


# --------------------------------------------------------------------------
# colour helpers
# --------------------------------------------------------------------------

def _clean_hex(value, fallback):
    return design_tokens.clean_hex(value, fallback)


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
        self.app_name = str(cfg["app_name"]).strip() or self.agency_name
        self.app_tagline = str(cfg["app_tagline"]).strip() or DEFAULT_TAGLINE
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
        """Every CSS custom property, from `design_tokens`.

        The app and the report call this same method, so the two surfaces can
        only ever draw from one palette - re-skinning either means re-skinning
        both."""
        return design_tokens.all_tokens(self)

    def css_variables(self, indent="    "):
        return design_tokens.css_variables(self, indent)

    def as_dict(self):
        return {
            "agency_name": self.agency_name,
            "agency_url": self.agency_url,
            "client_name": self.client_name,
            "report_title": self.report_title,
            "report_subtitle": self.report_subtitle,
            "app_name": self.app_name,
            "app_tagline": self.app_tagline,
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
