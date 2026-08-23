#!/usr/bin/env python3
"""
Consolidate a scan into ONE standalone HTML report.

This module owns the reading side: it finds whatever the scanners left in a run
folder and normalizes it into the page list the report is built from. What the
report says and how it looks live in report_model / report_render; the PDF pass
lives in report_pdf.

Reads either / both of these, whichever it finds under the folder(s) you pass:
  - ci-result.json          (jsonExpanded export: scores + core metrics per page)
  - per-page lighthouse.json (full LHR: adds the itemized failing-audit list)

Usage:
    python consolidate_report.py
    python consolidate_report.py .unlighthouse
    python consolidate_report.py .unlighthouse-desktop .unlighthouse-mobile -o danah-report.html

Pure standard library - no pip installs.
"""

import argparse
import html
import json
import os
import sys
from pathlib import Path

SKIP_MODES = {"notApplicable", "informative", "manual", "error"}

CATEGORY_KEYS = [
    ("performance", "Performance"),
    ("accessibility", "Accessibility"),
    ("best-practices", "Best Practices"),
    ("seo", "SEO"),
]

METRIC_LABELS = {
    "first-contentful-paint": "FCP",
    "largest-contentful-paint": "LCP",
    "speed-index": "Speed Index",
    "interactive": "TTI",
    "total-blocking-time": "TBT",
    "cumulative-layout-shift": "CLS",
    "max-potential-fid": "Max FID",
}


def get_score(val):
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, dict):
        s = val.get("score")
        return float(s) if isinstance(s, (int, float)) else None
    return None


def metric_display(key, val):
    if isinstance(val, dict):
        if val.get("displayValue"):
            return str(val["displayValue"])
        num = val.get("numericValue", val.get("value"))
    else:
        num = val
    if not isinstance(num, (int, float)):
        return "-"
    if key == "cumulative-layout-shift":
        return f"{num:.3f}"
    return f"{num/1000:.1f} s" if num >= 1000 else f"{int(num)} ms"


def category_of_audits(categories):
    """audit id -> the Lighthouse category that lists it.

    The LHR already states this in each category's auditRefs; carrying it
    through is what lets the report group findings by category instead of
    presenting one flat list. It reads the scan output - it does not change
    any of it."""
    out = {}
    for key, _ in CATEGORY_KEYS:
        cat = categories.get(key)
        if not isinstance(cat, dict):
            continue
        for ref in cat.get("auditRefs") or []:
            if isinstance(ref, dict) and ref.get("id"):
                out.setdefault(ref["id"], key)
    return out


def load_ci_results(roots):
    pages = []
    for root in roots:
        root = Path(root)
        if not root.exists():
            print(f"  ! skipped (not found): {root}", file=sys.stderr)
            continue
        device = ("Mobile" if "mobile" in root.name.lower()
                  else "Desktop" if "desktop" in root.name.lower() else "")
        for jf in sorted(root.rglob("*.json")):
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                continue
            if not (isinstance(data, dict) and isinstance(data.get("routes"), list)):
                continue
            for r in data["routes"]:
                if not isinstance(r, dict):
                    continue
                cats = r.get("categories", {}) or {}
                scores = {k: get_score(cats.get(k)) for k, _ in CATEGORY_KEYS}
                if scores.get("performance") is None and isinstance(r.get("score"), (int, float)):
                    scores["performance"] = float(r["score"])
                metrics = {}
                for mk, mv in (r.get("metrics", {}) or {}).items():
                    metrics[mk] = {"display": metric_display(mk, mv), "score": get_score(mv)}
                pages.append({
                    "path": html.unescape(str(r.get("path", "(unknown)"))),
                    "device": device,
                    "scores": scores,
                    "metrics": metrics,
                    "issues": None,
                    "report_html": None,
                })
    return pages


def load_lhr_pages(roots):
    pages = []
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        device = ("Mobile" if "mobile" in root.name.lower()
                  else "Desktop" if "desktop" in root.name.lower() else "")
        for jf in sorted(root.rglob("*.json")):
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                continue
            if not (isinstance(data, dict)
                    and isinstance(data.get("categories"), dict)
                    and isinstance(data.get("audits"), dict)):
                continue
            url = ""
            for k in ("finalDisplayedUrl", "finalUrl", "requestedUrl", "mainDocumentUrl"):
                if isinstance(data.get(k), str) and data[k]:
                    url = data[k]
                    break
            scores = {k: get_score(data["categories"].get(k)) for k, _ in CATEGORY_KEYS}
            audit_category = category_of_audits(data["categories"])
            issues = []
            for audit_id, audit in data["audits"].items():
                if not isinstance(audit, dict):
                    continue
                if audit.get("scoreDisplayMode") in SKIP_MODES:
                    continue
                s = audit.get("score")
                if s is None or s >= 1:
                    continue
                issues.append({
                    "id": audit.get("id", audit_id),
                    "category": audit_category.get(audit.get("id", audit_id), ""),
                    "title": audit.get("title", audit.get("id", "audit")),
                    "description": audit.get("description", ""),
                    "displayValue": audit.get("displayValue", ""),
                    "score": s,
                })
            issues.sort(key=lambda i: i["score"])
            metrics = {}
            for mk in METRIC_LABELS:
                a = data["audits"].get(mk)
                if isinstance(a, dict):
                    metrics[mk] = {"display": metric_display(mk, a), "score": get_score(a)}
            # sibling full Lighthouse HTML report, if lighthouse wrote one
            report_html = None
            name = jf.name
            for cand in (name[:-5] + ".html",                      # page_001.json -> page_001.html
                         name.replace(".report.json", ".report.html")):
                sib = jf.with_name(cand)
                if sib.exists():
                    report_html = str(sib)
                    break
            pages.append({
                "path": html.unescape(url),
                "device": device,
                "scores": scores,
                "metrics": metrics,
                "issues": issues,
                "report_html": report_html,
            })
    return pages


def merge(ci_pages, lhr_pages):
    from urllib.parse import urlparse as _urlparse

    def norm(s):
        if "://" in s:
            u = _urlparse(s)
            s = u.path + (("?" + u.query) if u.query else "")
        s = s.split("#")[0].rstrip("/").lower()
        return s or "/"

    by_key = {}
    for p in ci_pages:
        by_key[(norm(p["path"]), p["device"])] = p

    used = set()
    for lp in lhr_pages:
        target = by_key.get((norm(lp["path"]), lp["device"]))
        if target is not None and (lp["issues"] or lp.get("report_html")):
            if lp["issues"]:
                target["issues"] = lp["issues"]
            if lp.get("report_html"):
                target["report_html"] = lp["report_html"]
            used.add(id(lp))

    extras = [lp for lp in lhr_pages if id(lp) not in used and lp["issues"] is not None]
    result = list(ci_pages) + extras if ci_pages else lhr_pages
    result.sort(key=lambda p: (p["device"], p["path"]))
    return result


def build_html(pages, roots, out_path=None, categories=None, *,
               site_url="", device="", accessibility=None, security=None,
               brand=None, scope=None, tools=None, standards=None, generated=None,
               coverage=None):
    """The consolidated report as one standalone HTML document.

    Signature and behaviour for the original four arguments are unchanged, so
    every existing caller keeps working. The keyword arguments are what the
    dashboard adds when a run also produced accessibility-standards or security
    findings: with them, one report covers every category instead of three
    separate pages. `coverage` is the run's own record of which pages were
    measured and which were not - it is printed, never scored.

    The rendering itself lives in report_model (what the report says) and
    report_render (how it looks). Nothing here re-scores anything - the pages
    handed in are the scanners' own output.
    """
    import report_model
    import report_render
    from report_brand import load_brand

    out_dir = Path(out_path).parent if out_path else Path(".")

    def report_href(page):
        """Keep the existing link to each page's full Lighthouse report,
        relative to wherever this HTML is being written."""
        raw = page.get("report_html")
        if not raw:
            return None
        try:
            return os.path.relpath(raw, out_dir).replace("\\", "/")
        except ValueError:
            return None

    resolved = load_brand(brand, near=out_dir)

    if not site_url:
        for page in pages:
            if "://" in str(page.get("path", "")):
                from urllib.parse import urlparse as _u
                parsed = _u(page["path"])
                site_url = f"{parsed.scheme}://{parsed.netloc}"
                break
    if not device:
        device = next((p.get("device") for p in pages if p.get("device")), "")

    report = report_model.build_report(
        pages,
        site_url=site_url,
        device=device,
        roots=roots,
        categories=categories,
        accessibility=accessibility,
        security=security,
        brand=resolved.as_dict(),
        generated=generated,
        scope=scope,
        tools=tools or default_tools(pages, accessibility, security),
        standards=standards,
        coverage=coverage,
        report_href=report_href,
    )
    return report_render.render_report(report, resolved)


def run_coverage(roots):
    """The coverage ledger a scan left in one of these folders, if any.

    Written after every stage of a run, so pointing this at a run folder that
    was interrupted still produces a report that states its own coverage
    instead of quietly presenting partial data as the whole site.
    """
    import runstate

    for root in roots:
        for path in (Path(root), Path(root).parent):
            saved = runstate.RunState.load(path)
            if saved:
                return runstate.report_coverage_from(saved)
    return None


def default_tools(pages, accessibility=None, security=None):
    """What ran, for the methodology page. Versions are only claimed where a
    scanner actually reported one."""
    tools = [
        {"name": "Unlighthouse", "version": "", "role": "site crawl and page discovery"},
        {"name": "Google Lighthouse", "version": "", "role": "per-page category audits"},
    ]
    if accessibility:
        tools.append({"name": "axe-core",
                      "version": accessibility.get("engine_version", ""),
                      "role": "accessibility rules and standards mapping"})
    if security:
        tools.append({"name": "Passive security scan", "version": "1.0",
                      "role": "headers, cookies, TLS and redirects"})
    tools.append({"name": "paged.js", "version": "", "role": "PDF pagination"})
    return tools


def find_chrome():
    """Kept for callers that used to locate the browser through this module;
    the search itself lives in report_pdf now."""
    import report_pdf
    return report_pdf.find_chrome()


def html_to_pdf(html_path, pdf_path, log=None):
    """Render the report to PDF.

    Delegates to report_pdf, which paginates with paged.js so the PDF gets the
    cover sheet, running header, page numbers and break control the plain
    "print to PDF" could not produce. Falls back to Chrome's own pagination
    when the paged.js polyfill cannot be resolved.
    """
    import report_pdf
    return report_pdf.html_to_pdf(html_path, pdf_path, log=log)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="*", default=["."])
    ap.add_argument("-o", "--output", default="consolidated-report.html")
    ap.add_argument("--pdf", action="store_true", help="Also write a PDF next to the HTML")
    ap.add_argument("--brand", default="",
                    help="brand.json to white-label the report with "
                         "(default: brand.json next to the output, then next to this file)")
    args = ap.parse_args()
    roots = args.roots if args.roots else ["."]

    print(f"Scanning: {', '.join(roots)}")
    coverage = run_coverage(roots)
    ci_pages = load_ci_results(roots)
    lhr_pages = load_lhr_pages(roots)
    pages = merge(ci_pages, lhr_pages)

    if not pages:
        print("No Unlighthouse data found (no ci-result.json or lighthouse.json). "
              "Point this at your .unlighthouse folder.", file=sys.stderr)
        sys.exit(1)

    Path(args.output).write_text(
        build_html(pages, roots, out_path=args.output, brand=args.brand or None,
                   coverage=coverage),
        encoding="utf-8")
    if coverage and coverage.get("status") != "complete":
        print(f"  ! partial run: {coverage['pages_ok']} of "
              f"{coverage['pages_attempted']} page(s) measured "
              f"({coverage['percent']}%) - the report says so on its cover.",
              file=sys.stderr)
    src = []
    if ci_pages:
        src.append("ci-result.json")
    if any(p["issues"] for p in pages):
        src.append("full audits")
    print(f"Done: {len(pages)} page(s) [{', '.join(src) or 'scores only'}] -> {args.output}")

    if args.pdf:
        pdf_path = str(Path(args.output).with_suffix(".pdf"))
        try:
            info = html_to_pdf(args.output, pdf_path, log=lambda m: print(m, file=sys.stderr))
            print(f"PDF: {pdf_path} (paginated by {info['engine']})")
        except Exception as e:
            print(f"PDF export failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()