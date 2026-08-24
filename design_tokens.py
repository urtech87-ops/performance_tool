#!/usr/bin/env python3
"""
The design system, as data.
===========================

One module owns every value the product's two surfaces are drawn from: the
dashboard app (`ui_css.py`) and the generated report (`report_css.py`). Both
emit the same `:root` block, so a card in the app and a card in the report are
the same card, and re-skinning the product is editing this file (or the brand
JSON that feeds it) and nothing else.

    brand.json ─► report_brand.Brand ─┬─► design_tokens.all_tokens ─┬─► ui_css
                  (names, logos,      │   (the :root custom props)  │
                   3 colours, fonts)  │                             └─► report_css
                                      └─► report_render / dashboard (logos, names)

What is brandable and what is not
---------------------------------
Brandable: `primary`, `secondary`, `ink`, `font_stack`, `mono_stack`, the logos
and the names. Everything else in the palette is *derived* from those - tints,
shades, lines, papers, gradients, shadows, the focus ring.

Not brandable, on purpose: the status palette (good / needs work / critical)
and the severity palette. A failing score has to look like a failing score
whatever colour the agency picked, so those are fixed, contrast-checked values
and no brand input can reach them.

The scales
----------
Type is a 1.25 minor third off a 14.5px base; spacing is a 4px scale; radii and
shadows are small closed sets. Nothing in either stylesheet may hard-code a
colour, a font size, a spacing step, a radius or a shadow - it names a token.
`tests/test_design_system.py` enforces that.
"""

# --------------------------------------------------------------------------
# colour maths
# --------------------------------------------------------------------------

import re

HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

WHITE = "#ffffff"
BLACK = "#000000"

# The out-of-the-box skin. `report_brand.DEFAULTS` reads these, so "the default
# brand colour" is written once.
DEFAULT_PRIMARY = "#2f5bea"     # a confident, unfussy blue
DEFAULT_SECONDARY = "#7c3aed"   # the gradient's far end, used sparingly
DEFAULT_INK = "#0f172a"         # the neutral ramp's darkest step
DEFAULT_FONT = ("'Inter','Segoe UI',-apple-system,BlinkMacSystemFont,"
                "Roboto,Helvetica,Arial,sans-serif")
DEFAULT_MONO = ("ui-monospace,SFMono-Regular,Menlo,Consolas,"
                "'Liberation Mono',monospace")


def clean_hex(value, fallback="#000000"):
    """A normalised `#rrggbb`, or `fallback` when the input is not a hex colour."""
    v = str(value or "").strip()
    if not HEX_RE.match(v):
        return fallback
    if len(v) == 4:  # #abc -> #aabbcc
        v = "#" + "".join(c * 2 for c in v[1:])
    return v.lower()


def hex_to_rgb(value):
    v = clean_hex(value).lstrip("#")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb):
    return "#" + "".join(f"{max(0, min(255, int(round(c)))):02x}" for c in rgb)


def mix(color, other, amount):
    """Blend `amount` (0..1) of `other` into `color`."""
    a, b = hex_to_rgb(color), hex_to_rgb(other)
    amount = max(0.0, min(1.0, float(amount)))
    return rgb_to_hex(tuple(x + (y - x) * amount for x, y in zip(a, b)))


def tint(color, amount):
    """Toward white."""
    return mix(color, WHITE, amount)


def shade(color, amount):
    """Toward black."""
    return mix(color, BLACK, amount)


def relative_luminance(color):
    def channel(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in hex_to_rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg, bg):
    """WCAG contrast ratio between two colours, 1.0 .. 21.0."""
    a, b = relative_luminance(fg), relative_luminance(bg)
    lo, hi = sorted((a, b))
    return (hi + 0.05) / (lo + 0.05)


def readable_ink(background, light=WHITE, dark="#0f172a"):
    """Whichever of light/dark reads better on `background`."""
    return light if contrast_ratio(light, background) >= contrast_ratio(dark, background) else dark


def rgba(color, alpha):
    r, g, b = hex_to_rgb(color)
    return f"rgba({r},{g},{b},{round(float(alpha), 3)})"


# --------------------------------------------------------------------------
# fixed palettes - the part no brand may touch
# --------------------------------------------------------------------------

# Score bands. Every one of these clears 4.5:1 on white and on its own soft
# tint, so a verdict is legible as text, not only as a colour.
STATUS_COLORS = {
    "good": "#0b7a42",   # good
    "avg": "#a16207",    # needs work
    "poor": "#cf2f3f",   # critical
    "na": "#5f6c80",     # not measured
}

STATUS_LABEL = {
    "good": "Good",
    "avg": "Needs work",
    "poor": "Critical",
    "na": "Not measured",
}

SEVERITY_COLORS = {
    "critical": "#a8123b",
    "serious": "#c2410c",
    "moderate": "#a16207",
    "minor": "#2f6fae",
    "info": "#5b6b83",
}

# The audit categories, so a chip in the app and a ring in the report agree on
# what "SEO" looks like. Hues are spread far enough apart to stay separable,
# and each is dark enough to carry white text on its filled state.
CATEGORY_COLORS = {
    "performance": "#b45309",
    "accessibility": "#0369a1",
    "best-practices": "#047857",
    "seo": "#6d28d9",
    "security": "#be123c",
    "a11y-standards": "#5b3fbf",
}


# --------------------------------------------------------------------------
# the scales - type, space, radius, motion, layout
# --------------------------------------------------------------------------

# 1.25 minor third off a 14.5px base. `--t-base` is body copy; everything else
# steps away from it in one direction only.
TYPE_SCALE = {
    "--t-xs": "11px",
    "--t-sm": "12.5px",
    "--t-base": "14.5px",
    "--t-md": "16px",
    "--t-lg": "20px",
    "--t-xl": "25px",
    "--t-2xl": "31px",
    "--t-3xl": "39px",
    "--t-4xl": "49px",
}

# Line heights and tracking, so headings and body copy are set once.
TYPE_RHYTHM = {
    "--lh-tight": "1.2",
    "--lh-snug": "1.35",
    "--lh": "1.55",
    "--lh-loose": "1.65",
    "--track-tight": "-0.015em",
    "--track-wide": "0.08em",
    "--track-caps": "0.1em",
}

# 4px scale. Gaps, padding and margins come from here and nowhere else.
SPACE_SCALE = {
    "--sp-1": "4px",
    "--sp-2": "8px",
    "--sp-3": "12px",
    "--sp-4": "16px",
    "--sp-5": "24px",
    "--sp-6": "32px",
    "--sp-7": "48px",
    "--sp-8": "64px",
}

RADII = {
    "--r-xs": "4px",
    "--r-sm": "6px",
    "--r-md": "10px",
    "--r-lg": "16px",
    "--r-xl": "22px",
    "--r-pill": "999px",
}

MOTION = {
    "--dur-fast": "120ms",
    "--dur": "180ms",
    "--ease": "cubic-bezier(.32,.72,0,1)",
}

LAYOUT = {
    "--page-w": "1120px",   # the report's text column
    "--app-w": "1180px",    # the app's shell
    "--nav-h": "58px",      # shared between the app's and the report's top bar
}


def scale_tokens():
    """Everything that has no colour in it - identical on both surfaces."""
    out = {}
    for group in (TYPE_SCALE, TYPE_RHYTHM, SPACE_SCALE, RADII, MOTION, LAYOUT):
        out.update(group)
    return out


# --------------------------------------------------------------------------
# the brand-derived colour tokens
# --------------------------------------------------------------------------

def color_tokens(primary, secondary, ink, font_stack, mono_stack):
    """The whole palette, derived from three colours and two font stacks.

    Change `primary` and every accent, gradient, tint, line and focus ring
    moves with it. Change `ink` and the neutral ramp - text, borders, papers,
    shadows - moves with it. Nothing below is written twice.
    """
    p = clean_hex(primary, DEFAULT_PRIMARY)
    s = clean_hex(secondary, DEFAULT_SECONDARY)
    k = clean_hex(ink, DEFAULT_INK)

    t = {
        # -- brand ---------------------------------------------------------
        "--brand": p,
        "--brand-ink": readable_ink(p),          # text that sits on --brand
        "--brand-hover": shade(p, 0.12),
        "--brand-active": shade(p, 0.24),
        "--brand-dark": shade(p, 0.32),          # brand text on a light ground
        "--brand-deep": shade(p, 0.55),
        "--brand-soft": tint(p, 0.88),
        "--brand-softer": tint(p, 0.95),
        "--brand-line": tint(p, 0.72),
        "--brand2": s,
        "--brand2-soft": tint(s, 0.9),
        "--grad": f"linear-gradient(135deg,{p},{s})",
        "--grad-soft": f"linear-gradient(135deg,{tint(p, 0.9)},{tint(s, 0.92)})",
        "--cover-bg": f"linear-gradient(150deg,{shade(p, 0.55)},{shade(s, 0.35)})",

        # -- neutrals ------------------------------------------------------
        # One ink, five steps. --ink is body text, --ink-2 secondary, --ink-3
        # captions and labels, --ink-4 the faintest thing still readable.
        "--ink": k,
        "--ink-2": mix(k, WHITE, 0.32),
        "--ink-3": mix(k, WHITE, 0.38),
        "--ink-4": mix(k, WHITE, 0.55),
        "--line": mix(k, WHITE, 0.88),
        "--line-2": mix(k, WHITE, 0.94),
        "--paper": WHITE,
        "--paper-2": mix(k, WHITE, 0.975),       # the page behind the cards
        "--paper-3": mix(k, WHITE, 0.955),       # hover / inset fills

        # -- depth ---------------------------------------------------------
        # Four steps, all tinted with the ink rather than pure black, so a
        # shadow belongs to the palette instead of greying it.
        "--shadow-xs": f"0 1px 2px {rgba(k, .05)}",
        "--shadow-sm": f"0 1px 2px {rgba(k, .05)}, 0 2px 6px -2px {rgba(k, .10)}",
        "--shadow": f"0 1px 2px {rgba(k, .06)}, 0 8px 24px -12px {rgba(k, .18)}",
        "--shadow-lg": f"0 24px 60px -28px {rgba(k, .35)}",

        # -- on a dark ground ----------------------------------------------
        # The report cover and the log panel print white text over the brand's
        # deep end. Naming those four values means a light-cover skin is a
        # token change, not a rewrite of the cover's rules.
        "--on-dark": WHITE,
        "--on-dark-2": rgba(WHITE, 0.74),
        "--on-dark-3": rgba(WHITE, 0.55),
        "--on-dark-line": rgba(WHITE, 0.22),
        "--on-dark-fill": rgba(WHITE, 0.14),
        "--on-dark-sheen": rgba(WHITE, 0.20),

        # -- interaction ---------------------------------------------------
        # One focus ring for every focusable thing on both surfaces.
        "--focus": rgba(p, 0.35),
        "--focus-ring": f"0 0 0 3px {rgba(p, .32)}",
        "--overlay": rgba(k, 0.45),

        # -- typography ----------------------------------------------------
        "--font": font_stack,
        "--mono": mono_stack,
    }

    for name, color in STATUS_COLORS.items():
        t[f"--{name}"] = color
        t[f"--{name}-soft"] = tint(color, 0.9)
        t[f"--{name}-line"] = tint(color, 0.7)
    for name, color in SEVERITY_COLORS.items():
        t[f"--sev-{name}"] = color
        t[f"--sev-{name}-soft"] = tint(color, 0.9)
    for name, color in CATEGORY_COLORS.items():
        t[f"--cat-{name}"] = color
        t[f"--cat-{name}-soft"] = tint(color, 0.9)
        t[f"--cat-{name}-line"] = tint(color, 0.7)
    return t


def all_tokens(brand):
    """Every custom property, for a `report_brand.Brand`."""
    out = color_tokens(brand.primary, brand.secondary, brand.ink,
                       brand.font_stack, brand.mono_stack)
    out.update(scale_tokens())
    return out


def css_variables(brand, indent="  "):
    """The declarations, one per line, ready to drop inside a `:root {}`."""
    return "\n".join(f"{indent}{k}: {v};" for k, v in all_tokens(brand).items())


def css_root(brand, selector=":root", indent="  "):
    """A complete `:root { ... }` block."""
    return f"{selector} {{\n{css_variables(brand, indent)}\n}}"


# --------------------------------------------------------------------------
# inline fallbacks
# --------------------------------------------------------------------------
# SVG generated by `report_charts` carries `var(--token, literal)` so it still
# draws in a renderer that resolves custom properties late. The literal is read
# from the default skin here rather than typed at the call site, so a chart
# cannot drift away from the palette the stylesheets use.

FALLBACK_TOKENS = dict(
    color_tokens(DEFAULT_PRIMARY, DEFAULT_SECONDARY, DEFAULT_INK,
                 DEFAULT_FONT, DEFAULT_MONO),
    **scale_tokens())


def var(name, fallback=None):
    """`var(--name, literal)`, with the literal taken from the default skin."""
    return f"var({name},{fallback or FALLBACK_TOKENS.get(name, 'currentColor')})"
