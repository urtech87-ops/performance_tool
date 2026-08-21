#!/usr/bin/env python3
"""
Render the report from a fixture instead of a live scan.

The point is layout iteration: a full scan takes minutes and needs Node, a
browser and a live site, none of which you want in the loop while moving a
card around. `fixtures/sample_scan.json` holds one fabricated but structurally
faithful run - the same shapes the three scanners emit - so the renderer can
be exercised in milliseconds, and the test suite can assert on the result.

    python report_sample.py                      -> sample-report.html
    python report_sample.py --pdf                -> ... and sample-report.pdf
    python report_sample.py --brand brand.json   -> white-labelled
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import report_model
import report_render
from report_brand import load_brand

APP_DIR = Path(__file__).resolve().parent
FIXTURE = APP_DIR / "fixtures" / "sample_scan.json"

SAMPLE_BRAND = {
    "agency_name": "Northbeam Studio",
    "agency_url": "https://northbeam.example",
    "client_name": "Harbor Lane",
    "primary": "#1f4fd8",
    "secondary": "#7a3ff2",
    "report_title": "Website Health Report",
    "report_subtitle": "Performance, accessibility, SEO and security",
    "contact": "audits@northbeam.example",
    "footer_note": "Confidential - prepared for Harbor Lane",
}


def load_fixture(path=None):
    """The raw fixture, as the scanners would have handed it over."""
    return json.loads(Path(path or FIXTURE).read_text(encoding="utf-8"))


def sample_report(path=None, brand=None, generated=None):
    """Build the report structure from the fixture."""
    data = load_fixture(path)
    meta = data.get("meta", {})
    stamp = generated or datetime.fromisoformat(
        meta.get("generated") or datetime.now().isoformat(timespec="seconds"))

    return report_model.build_report(
        data.get("pages") or [],
        site_url=meta.get("site_url", ""),
        device=meta.get("device", ""),
        categories=meta.get("categories"),
        accessibility=data.get("accessibility"),
        security=data.get("security"),
        generated=stamp,
        scope=meta.get("scope"),
        tools=meta.get("tools"),
        standards=meta.get("standards"),
        report_href=lambda page: page.get("report_html"),
        brand=brand,
    )


def sample_html(path=None, brand=None, generated=None):
    resolved = load_brand(brand if brand is not None else SAMPLE_BRAND)
    return report_render.render_report(
        sample_report(path, brand=resolved.as_dict(), generated=generated), resolved)


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("-o", "--output", default="sample-report.html")
    ap.add_argument("-f", "--fixture", default=str(FIXTURE))
    ap.add_argument("-b", "--brand", default="", help="brand.json to apply")
    ap.add_argument("--pdf", action="store_true", help="also render the PDF via paged.js")
    ap.add_argument("--keep-print-html", action="store_true",
                    help="keep the paginated intermediate document")
    args = ap.parse_args()

    brand = args.brand or SAMPLE_BRAND
    out = Path(args.output)
    out.write_text(sample_html(args.fixture, brand), encoding="utf-8")
    print(f"HTML: {out}")

    if args.pdf:
        import report_pdf
        pdf = out.with_suffix(".pdf")
        info = report_pdf.html_to_pdf(str(out), str(pdf),
                                      log=lambda m: print(m, file=sys.stderr),
                                      keep_print_html=args.keep_print_html)
        print(f"PDF:  {pdf} (paginated by {info['engine']})")


if __name__ == "__main__":
    main()
