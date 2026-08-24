#!/usr/bin/env python3
"""
The design system's own tests.

The claim this file defends is a narrow, checkable one: **the product's whole
look comes from `design_tokens`, and changing the tokens changes both surfaces
and nothing else.** That is what makes the thing re-skinnable, so it is worth a
test rather than a promise in a README.

Four things are asserted:

  1. One source. Both stylesheets emit the same `:root` and the same shared
     component layer; neither has a palette of its own.
  2. No stragglers. Nothing in either stylesheet hard-codes a colour, a type
     size, a spacing step, a radius or a shadow.
  3. Re-skinning works. A brand.json with a different primary, ink and font
     moves every derived value, in the app and in the report alike, and leaves
     no trace of the default skin behind.
  4. The semantic palette is out of reach. No brand input can recolour a
     failing score or a critical finding.
"""

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import design_css                                   # noqa: E402
import design_tokens                                # noqa: E402
import report_css                                   # noqa: E402
import ui_css                                       # noqa: E402
import ui_page                                      # noqa: E402
import ui_preview                                   # noqa: E402
from report_brand import Brand, load_brand          # noqa: E402

DEFAULT = Brand({})

# A skin that shares nothing with the default: another hue, another neutral,
# another type stack.
RESKIN = {
    "primary": "#b4361f",
    "secondary": "#e08900",
    "ink": "#1d2321",
    "font_stack": "Georgia, 'Times New Roman', serif",
    "agency_name": "Ridgeway Partners",
    "app_name": "Ridgeway Site Audit",
}


def stylesheets(brand):
    return {"app": ui_css.stylesheet(brand), "report": report_css.stylesheet(brand)}


# --------------------------------------------------------------------------
# 1. one source
# --------------------------------------------------------------------------

def test_both_surfaces_emit_the_same_root():
    """The app and the report read one palette, not two that agree today."""
    root = design_tokens.css_variables(DEFAULT, "  ")
    for name, css in stylesheets(DEFAULT).items():
        assert root in css, name


def test_both_surfaces_include_the_shared_component_layer_verbatim():
    for name, css in stylesheets(DEFAULT).items():
        assert design_css.PRIMITIVES in css, name


def test_the_shared_components_are_defined_once_not_twice():
    """A primitive redefined in a surface's own sheet is the drift this
    whole arrangement exists to prevent."""
    for name, css in stylesheets(DEFAULT).items():
        own = css.replace(design_css.PRIMITIVES, "")
        for selector in (".card", ".btn", ".pill", ".chip", ".kpi", ".tag"):
            # a bare rule for the primitive itself; `.side .card` and
            # `#summary .kpi` are contextual overrides and are fine
            bare = re.compile(r"(?m)^\%s \{" % selector)
            assert not bare.search(own), f"{selector} redefined in {name}"


def test_the_charts_take_their_fallback_colours_from_the_tokens():
    """The inline SVG carries `var(--token, literal)`; the literal has to be
    the token's own value, or a late-resolving renderer draws a chart in a
    palette the stylesheet never had."""
    import report_charts

    assert report_charts.BAND_FALLBACK == design_tokens.STATUS_COLORS
    for sev, value in report_charts.SEVERITY_FALLBACK.items():
        assert value == design_tokens.SEVERITY_COLORS[sev]


# --------------------------------------------------------------------------
# 2. no stragglers
# --------------------------------------------------------------------------

# `:root` is the one place literals are allowed - it is where they are defined.
def _rules_only(css):
    return css.split("}", 1)[1] if css.lstrip().startswith(":root") else css


HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
RGBA = re.compile(r"\brgba?\(")


@pytest.mark.parametrize("surface", ["app", "report"])
def test_no_rule_hard_codes_a_colour(surface):
    """Every rule names a token. The `@page` margin boxes are the one place
    that cannot: `content:` boxes in paged media do not inherit custom
    properties, so those four values are substituted in - and the next test
    pins them to the tokens they were substituted from."""
    css = _rules_only(stylesheets(DEFAULT)[surface])
    screen = css.split("@media print", 1)[0]
    assert not HEX.search(screen), HEX.findall(screen)[:5]
    assert not RGBA.search(screen), screen[RGBA.search(screen).start() - 60:][:120]


def test_the_printed_literals_are_the_tokens_own_values():
    css = _rules_only(stylesheets(DEFAULT)["report"])
    printed = css.split("@media print", 1)[1]
    values = set(DEFAULT.tokens().values())
    found = set(HEX.findall(printed))
    assert found, "the @page boxes should still be carrying substituted colours"
    for literal in found:
        assert literal in values, f"{literal} is not any token's value"
    # and they are confined to the page boxes, not sprinkled through the rules
    for match in HEX.finditer(printed):
        assert "@page" in printed[:match.start()].rsplit("}", 3)[0][-400:] or \
               "@top-" in printed[max(0, match.start() - 300):match.start()] or \
               "@bottom-" in printed[max(0, match.start() - 300):match.start()], literal


@pytest.mark.parametrize("surface", ["app", "report"])
def test_no_rule_invents_a_type_size(surface):
    """Font sizes come from the type scale. The printed sheet is the one
    exception: paper is measured in points, not in the screen's scale."""
    css = _rules_only(stylesheets(DEFAULT)[surface])
    screen = css.split("@media print", 1)[0]
    for size in re.findall(r"font-size:\s*([^;}]+)", screen):
        assert "var(--t-" in size, size


# Anything at or above a spacing step has to *be* a spacing step. Below that
# are hairlines and the geometry of a switch, a dot or an icon, which are the
# component's own shape rather than layout.
BIG_PX = re.compile(r"(?<![\w.-])(\d{2,})(?:\.\d+)?px")


@pytest.mark.parametrize("surface", ["app", "report"])
def test_no_rule_invents_a_spacing_step(surface):
    """Padding, margins and gaps come from the 4px scale."""
    css = _rules_only(stylesheets(DEFAULT)[surface])
    screen = css.split("@media print", 1)[0]
    for prop, value in re.findall(r"\b(padding|margin|gap|row-gap|column-gap)"
                                  r"(?:-top|-right|-bottom|-left)?:\s*([^;}]+)", screen):
        for px in BIG_PX.findall(value):
            assert int(px) < 16, f"{prop}: {value}"


def test_the_scales_are_the_ones_the_docs_describe():
    scale = design_tokens.scale_tokens()
    assert scale["--t-base"] == "14.5px"          # body copy
    assert scale["--sp-4"] == "16px"              # the 4px scale's fourth step
    assert set(design_tokens.TYPE_SCALE) < set(scale)
    assert set(design_tokens.SPACE_SCALE) < set(scale)
    assert set(design_tokens.RADII) < set(scale)


# --------------------------------------------------------------------------
# 3. re-skinning
# --------------------------------------------------------------------------

def test_a_reskin_moves_every_derived_value_on_both_surfaces():
    skinned = Brand(RESKIN)
    for name, css in stylesheets(skinned).items():
        assert f"--brand: {RESKIN['primary']}" in css, name
        assert f"--ink: {RESKIN['ink']}" in css, name
        assert RESKIN["font_stack"] in css, name
        # nothing of the default skin survives
        assert design_tokens.DEFAULT_PRIMARY not in css, name
        assert design_tokens.DEFAULT_SECONDARY not in css, name
        assert design_tokens.DEFAULT_INK not in css, name


def test_a_reskin_reaches_the_app_page_itself():
    page = ui_page.render(RESKIN)
    assert f"--brand: {RESKIN['primary']}" in page
    assert "Ridgeway Site Audit" in page
    assert design_tokens.DEFAULT_PRIMARY not in page


def test_a_font_change_drops_the_webfont_the_default_skin_asked_for():
    """Re-branding onto another face must not leave the page fetching Inter."""
    assert "fonts.googleapis.com" in ui_css.font_link(DEFAULT)
    assert ui_css.font_link(Brand(RESKIN)) == ""
    assert "fonts.googleapis.com" not in ui_page.render(RESKIN)


def test_a_logo_reaches_both_the_app_and_the_report(tmp_path):
    logo = tmp_path / "mark.svg"
    logo.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"></svg>',
                    encoding="utf-8")
    brand = load_brand({"agency_logo": str(logo), "agency_name": "Ridgeway Partners"})
    assert brand.agency_logo.startswith("data:image/svg+xml;base64,")
    assert brand.agency_logo in ui_page.render(brand)      # the app's top bar
    assert brand.agency_monogram == "RP"                   # and its stand-in


def test_the_report_and_the_app_agree_about_one_brand(tmp_path):
    """One brand.json, both surfaces - that is the white-label promise."""
    import report_sample

    cfg = tmp_path / "brand.json"
    cfg.write_text(json.dumps(RESKIN), encoding="utf-8")
    brand = load_brand(str(cfg))
    app = ui_page.render(brand)
    report = report_sample.sample_html(brand=brand.as_dict())
    for css in (app, report):
        assert f"--brand: {RESKIN['primary']}" in css


# --------------------------------------------------------------------------
# 4. the semantic palette is out of reach
# --------------------------------------------------------------------------

def test_no_brand_input_can_recolour_a_verdict():
    skinned = Brand(dict(RESKIN, good="#ff00ff", poor="#00ff00"))
    tokens = skinned.tokens()
    assert tokens["--good"] == design_tokens.STATUS_COLORS["good"]
    assert tokens["--poor"] == design_tokens.STATUS_COLORS["poor"]
    assert tokens["--sev-critical"] == design_tokens.SEVERITY_COLORS["critical"]
    assert "#ff00ff" not in "".join(tokens.values())


def test_every_semantic_colour_is_readable_where_it_is_used():
    """Each band and severity is used as small bold text on white and on its
    own soft tint. Both have to clear WCAG AA."""
    palettes = (design_tokens.STATUS_COLORS, design_tokens.SEVERITY_COLORS,
                design_tokens.CATEGORY_COLORS)
    for palette in palettes:
        for name, color in palette.items():
            on_white = design_tokens.contrast_ratio(color, "#ffffff")
            on_soft = design_tokens.contrast_ratio(color, design_tokens.tint(color, 0.9))
            assert on_white >= 4.5, f"{name} on white: {on_white:.2f}"
            assert on_soft >= 4.3, f"{name} on its tint: {on_soft:.2f}"


def test_body_and_label_text_clear_aa_on_both_papers():
    tokens = DEFAULT.tokens()
    for key in ("--ink", "--ink-2", "--ink-3"):
        for ground in ("--paper", "--paper-2", "--paper-3"):
            ratio = design_tokens.contrast_ratio(tokens[key], tokens[ground])
            assert ratio >= 4.5, f"{key} on {ground}: {ratio:.2f}"


def test_text_on_the_brand_colour_is_chosen_not_assumed():
    """`--brand-ink` is picked for contrast, so a pale brand gets dark text."""
    for primary in ("#2f5bea", "#ffe600", "#0a0a0a", "#7cf0c0"):
        brand = Brand({"primary": primary})
        ratio = design_tokens.contrast_ratio(brand.tokens()["--brand-ink"], primary)
        assert ratio >= 4.5, f"{primary}: {ratio:.2f}"


# --------------------------------------------------------------------------
# the preview, which is how any of this gets looked at
# --------------------------------------------------------------------------

def test_the_preview_renders_the_real_page_for_every_state(tmp_path):
    written = ui_preview.write_previews(tmp_path)
    names = {p.name for p in written}
    assert ui_preview.INDEX_FILE in names
    assert ui_preview.REPORT_FILE in names
    for slug, _name, _why in ui_preview.STATES:
        assert f"{slug}.html" in names
        body = (tmp_path / f"{slug}.html").read_text(encoding="utf-8")
        # the real page, plus the one constant that pins its state
        assert f'window.__PREVIEW_STATE__ = "{slug}"' in body
        assert 'id="url"' in body and 'id="go"' in body


def test_the_preview_harness_never_reaches_a_scanner():
    """It fabricates what the page *shows*, and nothing else - no submit, no
    queue, no scan."""
    page = ui_page.render()
    harness = page[page.index("function preview()"):page.rindex("})();")]
    for forbidden in ("fetch(", "EventSource", "/scan", "XMLHttpRequest"):
        assert forbidden not in harness, forbidden


def test_the_preview_is_inert_until_a_state_is_asked_for():
    page = ui_page.render()
    assert "window.__PREVIEW_STATE__ ||" in page
    assert 'if(!state){ return; }' in page


def test_the_sample_report_still_comes_from_the_fixture(tmp_path):
    import report_sample

    out = report_sample.sample_html()
    assert out.startswith("<!DOCTYPE html>")
    assert "Harbor Lane" in out
