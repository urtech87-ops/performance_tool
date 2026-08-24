#!/usr/bin/env python3
"""
Look at the app without running a scan.

A real scan needs Node, a browser, a live site and several minutes - none of
which you want in the loop while moving a card around. This writes the actual
dashboard page (`ui_page.render()`, the same string the server serves) once per
state it can be in, with a constant that drives the page's own preview harness
into that state. What you open is the real UI, not a mock-up of it.

    python ui_preview.py                     -> preview/index.html + a file per state
    python ui_preview.py -o /tmp/ui          -> somewhere else
    python ui_preview.py --brand brand.json  -> white-labelled

The report is rendered alongside from `fixtures/sample_scan.json` (the same
fixture `report_sample.py` and the test suite use), so the results view previews
a real report in its iframe and the two surfaces can be compared side by side.

On a running server the same harness is reachable without any of this:

    http://127.0.0.1:5000/?preview=partial       any state, live
    http://127.0.0.1:5000/preview                the index of them
"""

import argparse
import json
from pathlib import Path

import report_sample
import ui_page
from report_brand import load_brand

APP_DIR = Path(__file__).resolve().parent

# Every state the app can be in, in the order they happen. `slug` is what the
# harness in ui_page answers to - `?preview=<slug>` on a live server.
STATES = [
    ("idle", "Empty", "First visit: the form, and an empty-state panel where the report will go."),
    ("advanced", "Advanced options", "The Phase A/B panel open: presets, depth, connection, device, requests, sign-in."),
    ("queued", "Queued", "Submitted, waiting for a free worker; the queue position is shown."),
    ("running", "Scanning", "A run in progress: the step rail, the live status and the technical log."),
    ("complete", "Complete", "Full coverage: the report, its downloads and the coverage ring."),
    ("partial", "Partial", "Some pages could not be measured - said plainly, before the report."),
    ("failed", "Failed", "Nothing could be measured; the banner explains it, the log keeps the detail."),
    ("blocked", "Blocked site", "A multi-page audit refused until the domain is verified, with both tokens."),
    ("error", "Refused", "Rejected before anything was queued - a full queue, a rate limit, a bad URL."),
]

REPORT_FILE = "sample-report.html"
INDEX_FILE = "index.html"


def _report_card(report_href):
    """The link to the fixture-rendered report, when there is one to link to."""
    if not report_href:
        return ""
    return f"""<div class="card" style="margin-top:var(--sp-5)">
    <h3>The report, from the same fixture</h3>
    <p class="hint">Rendered by <code>report_sample.py</code> out of
       <code>fixtures/sample_scan.json</code> - no scan, no Node, no live site.</p>
    <p style="margin-top:var(--sp-3)"><a class="btn primary" href="{report_href}">Open the sample report</a></p>
  </div>"""


def page_for(state, brand=None, report_href=REPORT_FILE):
    """The dashboard page, pinned to one preview state.

    The constant is injected as its own `<script>` ahead of the page's, which
    is the whole of the difference between this and what the server serves.
    """
    page = ui_page.render(brand)
    inject = ("<script>window.__PREVIEW_STATE__ = %s;\n"
              "window.__PREVIEW_REPORT__ = %s;</script>\n"
              % (json.dumps(state), json.dumps(report_href)))
    return page.replace("</head>", inject + "</head>", 1)


def index_page(brand=None, href=lambda slug: f"{slug}.html", report_href=REPORT_FILE):
    """A contact sheet linking every state, styled from the same tokens.

    `href` decides where a card points: at a sibling file for the written
    previews, at `/?preview=<slug>` for the live route.
    """
    import ui_css

    brand = load_brand(brand)
    cards = "".join(
        f'<a class="card pv" href="{href(slug)}">'
        f'<div class="pill brand">{slug}</div>'
        f'<h3>{name}</h3><p>{why}</p></a>'
        for slug, name, why in STATES)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{brand.app_name} &middot; UI preview</title>
{ui_css.font_link(brand)}
<style>{ui_css.stylesheet(brand)}
.pvgrid {{ display: grid; gap: var(--sp-4);
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); }}
.pv {{ text-decoration: none; color: inherit; display: block;
  transition: box-shadow var(--dur) var(--ease), border-color var(--dur) var(--ease); }}
.pv:hover {{ box-shadow: var(--shadow-lg); border-color: var(--brand-line); }}
.pv h3 {{ margin: var(--sp-3) 0 var(--sp-2); }}
.pv p {{ color: var(--ink-3); font-size: var(--t-sm); margin: 0; }}
</style></head>
<body>
<header class="topnav">
  <div class="brandmark"><span class="dot">{brand.agency_monogram}</span>
    <div class="names"><div class="n">{brand.app_name}</div><div class="r">UI preview</div></div></div>
</header>
<div class="shell">
  <div class="hero">
    <h1>Every state, without a scan</h1>
    <p>The real dashboard page, driven into each state it can be in. Edit the tokens in
       <code>design_tokens.py</code> or the values in <code>brand.json</code>, re-run
       <code>python ui_preview.py</code>, and every card below changes with them.</p>
  </div>
  <div class="pvgrid">{cards}</div>
  {_report_card(report_href)}
  <div class="foot">Preview build &middot; not a scan result.</div>
</div>
</body></html>
"""


def write_previews(out_dir, brand=None, fixture=None):
    """Write the index, one file per state, and the fixture report. Returns
    the paths written, in order."""
    resolved = load_brand(brand)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []

    report = out / REPORT_FILE
    report.write_text(report_sample.sample_html(fixture), encoding="utf-8")
    written.append(report)

    for slug, _name, _why in STATES:
        target = out / f"{slug}.html"
        target.write_text(page_for(slug, resolved), encoding="utf-8")
        written.append(target)

    index = out / INDEX_FILE
    index.write_text(index_page(resolved), encoding="utf-8")
    written.append(index)
    return written


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("-o", "--output", default="preview",
                    help="directory to write into (default: ./preview)")
    ap.add_argument("-b", "--brand", default="", help="brand.json to apply")
    ap.add_argument("-f", "--fixture", default="",
                    help="scan fixture for the sample report")
    args = ap.parse_args()

    written = write_previews(args.output, args.brand or None, args.fixture or None)
    print(f"Wrote {len(written)} files to {Path(args.output).resolve()}")
    print(f"Open {(Path(args.output) / INDEX_FILE).resolve()}")


if __name__ == "__main__":
    main()
