#!/usr/bin/env python3
"""
Consolidate Unlighthouse output into ONE standalone HTML report.

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
import subprocess
import sys
from datetime import datetime
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


def score_class(s):
    if s is None:
        return "na"
    if s >= 0.9:
        return "good"
    if s >= 0.5:
        return "avg"
    return "poor"


def fmt_score(s):
    return "-" if s is None else str(round(s * 100))


def anchor_for(page):
    return f'page-{abs(hash((page["path"], page["device"])))}'


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
            issues = []
            for audit in data["audits"].values():
                if not isinstance(audit, dict):
                    continue
                if audit.get("scoreDisplayMode") in SKIP_MODES:
                    continue
                s = audit.get("score")
                if s is None or s >= 1:
                    continue
                issues.append({
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


def build_html(pages, roots, out_path=None, categories=None):
    gen = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = len(pages)
    has_any_issue_data = any(p["issues"] is not None for p in pages)
    total_issues = sum(len(p["issues"]) for p in pages if p["issues"])
    out_dir = Path(out_path).parent if out_path else Path(".")

    # Which category columns/chips to show. None -> all four (unchanged default).
    cat_keys = [(k, l) for (k, l) in CATEGORY_KEYS if categories is None or k in categories]
    show_metrics = categories is None or "performance" in categories

    def report_href(p):
        rh = p.get("report_html")
        if not rh:
            return None
        try:
            return os.path.relpath(rh, out_dir).replace("\\", "/")
        except Exception:
            return None

    rows = []
    for p in pages:
        cells = "".join(
            f'<td class="sc {score_class(p["scores"].get(k))}">{fmt_score(p["scores"].get(k))}</td>'
            for k, _ in cat_keys)
        dev = f'<span class="dev">{html.escape(p["device"])}</span>' if p["device"] else ""
        issue_cell = (str(len(p["issues"])) if p["issues"] is not None else "-")
        href = report_href(p)
        rep_cell = f'<a class="rep" href="{href}" target="_blank">Open &#8599;</a>' if href else "-"
        rows.append(
            f'<tr><td class="url"><a href="#{anchor_for(p)}">{html.escape(p["path"])}</a> {dev}</td>'
            f'{cells}<td class="ic">{issue_cell}</td><td class="rc">{rep_cell}</td></tr>')
    summary_rows = "\n".join(rows)

    sections = []
    for p in pages:
        dev = f' <span class="dev">{html.escape(p["device"])}</span>' if p["device"] else ""
        chips = "".join(
            f'<span class="chip {score_class(p["scores"].get(k))}">{lbl}: {fmt_score(p["scores"].get(k))}</span>'
            for k, lbl in cat_keys)

        href = report_href(p)
        report_link = (f'<a class="fullrep" href="{href}" target="_blank">Open full Lighthouse report &#8599;</a>'
                       if href else "")

        metric_html = ""
        if show_metrics and p["metrics"]:
            cells = "".join(
                f'<div class="m {score_class(m["score"])}"><span>{METRIC_LABELS.get(mk, mk)}</span><b>{html.escape(m["display"])}</b></div>'
                for mk, m in p["metrics"].items())
            metric_html = f'<div class="metrics">{cells}</div>'

        if p["issues"]:
            lis = []
            for i in p["issues"]:
                dv = f'<span class="dv">{html.escape(i["displayValue"])}</span>' if i["displayValue"] else ""
                desc = html.escape(i["description"]).replace("\n", " ")
                lis.append(f'<li><div class="it">{html.escape(i["title"])} {dv}</div><div class="id">{desc}</div></li>')
            issue_html = f'<ul class="issues">{"".join(lis)}</ul>'
        elif p["issues"] == []:
            issue_html = '<p class="clean">No failing audits on this page.</p>'
        else:
            issue_html = '<p class="note">Full audit not run for this page. Increase "Max pages" to include it in the deep audit.</p>'

        sections.append(
            f'<section id="{anchor_for(p)}"><h3>{html.escape(p["path"])}{dev} {report_link}</h3>'
            f'<div class="chips">{chips}</div>{metric_html}{issue_html}</section>')
    section_html = "\n".join(sections)

    header_cells = "".join(f"<th>{lbl}</th>" for _, lbl in cat_keys)
    scanned = ", ".join(str(Path(p)) for p in roots)
    issue_stat = (f'<div class="stat"><b>{total_issues}</b><span>Issues found</span></div>'
                  if has_any_issue_data else "")

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Danah - Consolidated Performance Report</title>
<style>
  :root {{ --good:#0a7d34; --avg:#b06f00; --poor:#c0392b; --na:#999; --bg:#f6f7f9; --card:#fff; --line:#e3e6ea; --ink:#1c2430; }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; margin:0; background:var(--bg); color:var(--ink); line-height:1.5; }}
  .wrap {{ max-width:1040px; margin:0 auto; padding:32px 20px 64px; }}
  h1 {{ font-size:24px; margin:0 0 4px; }}
  .meta {{ color:#5b6672; font-size:13px; margin-bottom:22px; }}
  .stats {{ display:flex; gap:16px; margin-bottom:28px; flex-wrap:wrap; }}
  .stat {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 18px; min-width:120px; }}
  .stat b {{ display:block; font-size:26px; }}
  .stat span {{ font-size:12px; color:#5b6672; text-transform:uppercase; letter-spacing:.04em; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
  th, td {{ padding:9px 12px; text-align:left; border-bottom:1px solid var(--line); font-size:14px; }}
  th {{ background:#eef1f4; font-size:12px; text-transform:uppercase; letter-spacing:.03em; color:#5b6672; }}
  td.sc, th:nth-child(n+2) {{ text-align:center; }}
  td.ic {{ text-align:center; font-weight:600; }}
  .sc {{ font-weight:700; }}
  .good {{ color:var(--good); }} .avg {{ color:var(--avg); }} .poor {{ color:var(--poor); }} .na {{ color:var(--na); }}
  .url a {{ color:#0a58ca; text-decoration:none; word-break:break-all; }}
  .dev {{ font-size:11px; background:#eef1f4; color:#5b6672; padding:1px 7px; border-radius:20px; }}
  h2 {{ margin:40px 0 8px; font-size:18px; }}
  section {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:18px 20px; margin:16px 0; }}
  section h3 {{ margin:0 0 12px; font-size:15px; word-break:break-all; }}
  .chips {{ margin-bottom:14px; display:flex; gap:8px; flex-wrap:wrap; }}
  .chip {{ font-size:12px; padding:3px 10px; border-radius:20px; border:1px solid var(--line); font-weight:600; }}
  .chip.good {{ background:#e7f4ec; }} .chip.avg {{ background:#fbf1de; }} .chip.poor {{ background:#fbe9e7; }} .chip.na {{ background:#f0f0f0; }}
  .metrics {{ display:flex; flex-wrap:wrap; gap:10px; margin-bottom:14px; }}
  .m {{ border:1px solid var(--line); border-radius:8px; padding:8px 12px; min-width:96px; }}
  .m span {{ display:block; font-size:11px; color:#5b6672; text-transform:uppercase; }}
  .m b {{ font-size:15px; }}
  .m.good b {{ color:var(--good); }} .m.avg b {{ color:var(--avg); }} .m.poor b {{ color:var(--poor); }}
  ul.issues {{ list-style:none; margin:0; padding:0; }}
  ul.issues li {{ padding:10px 0; border-top:1px solid var(--line); }}
  .it {{ font-weight:600; font-size:14px; }}
  .dv {{ font-weight:600; color:var(--poor); margin-left:8px; }}
  .id {{ font-size:13px; color:#5b6672; margin-top:2px; }}
  .clean {{ color:var(--good); font-size:14px; margin:0; }}
  .note {{ color:#8a6d00; background:#fbf6e6; border:1px solid #efe1b0; padding:8px 12px; border-radius:8px; font-size:13px; margin:0; }}
  td.rc {{ text-align:center; }}
  a.rep {{ color:#0a58ca; text-decoration:none; font-size:13px; font-weight:600; white-space:nowrap; }}
  a.fullrep {{ float:right; font-size:12px; font-weight:600; color:#fff; background:#3887BE; padding:4px 10px; border-radius:6px; text-decoration:none; }}
  @media print {{ body {{ background:#fff; }} section, table {{ break-inside:avoid; }} .wrap {{ max-width:none; }} }}
</style></head>
<body><div class="wrap">
  <h1>Danah - Consolidated Performance Report</h1>
  <div class="meta">Generated {gen} - Source: {html.escape(scanned)}</div>
  <div class="stats">
    <div class="stat"><b>{total}</b><span>Pages</span></div>
    {issue_stat}
  </div>
  <h2>Summary - all pages</h2>
  <table>
    <thead><tr><th>Page</th>{header_cells}<th>Issues</th><th>Report</th></tr></thead>
    <tbody>{summary_rows}</tbody>
  </table>
  <p class="meta" style="margin-top:10px">Scores 0-100.
    <span class="good">&ge;90 good</span> - <span class="avg">50-89 needs work</span> - <span class="poor">&lt;50 poor</span>.</p>
  <h2>Detail by page</h2>
  {section_html}
</div></body></html>"""


def find_chrome():
    """Locate a Chromium-based browser for headless PDF printing."""
    import shutil
    la = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    candidates = [
        rf"{pf}\Google\Chrome\Application\chrome.exe",
        rf"{pf86}\Google\Chrome\Application\chrome.exe",
        rf"{la}\Google\Chrome\Application\chrome.exe",
        rf"{pf}\Microsoft\Edge\Application\msedge.exe",
        rf"{pf86}\Microsoft\Edge\Application\msedge.exe",
        rf"{pf}\BraveSoftware\Brave-Browser\Application\brave.exe",
        rf"{pf86}\BraveSoftware\Brave-Browser\Application\brave.exe",
        "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    for name in ("chrome", "google-chrome", "chromium", "chromium-browser", "msedge", "brave"):
        p = shutil.which(name)
        if p:
            return p
    return None


def html_to_pdf(html_path, pdf_path):
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError("No Chrome/Edge/Brave found for PDF export. Install one, or skip --pdf.")
    file_url = "file:///" + os.path.abspath(html_path).replace("\\", "/")
    subprocess.run(
        [chrome, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
         f"--print-to-pdf={os.path.abspath(pdf_path)}", file_url],
        check=True, timeout=180,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="*", default=["."])
    ap.add_argument("-o", "--output", default="consolidated-report.html")
    ap.add_argument("--pdf", action="store_true", help="Also write a PDF next to the HTML")
    args = ap.parse_args()
    roots = args.roots if args.roots else ["."]

    print(f"Scanning: {', '.join(roots)}")
    ci_pages = load_ci_results(roots)
    lhr_pages = load_lhr_pages(roots)
    pages = merge(ci_pages, lhr_pages)

    if not pages:
        print("No Unlighthouse data found (no ci-result.json or lighthouse.json). "
              "Point this at your .unlighthouse folder.", file=sys.stderr)
        sys.exit(1)

    Path(args.output).write_text(build_html(pages, roots, out_path=args.output), encoding="utf-8")
    src = []
    if ci_pages:
        src.append("ci-result.json")
    if any(p["issues"] for p in pages):
        src.append("full audits")
    print(f"Done: {len(pages)} page(s) [{', '.join(src) or 'scores only'}] -> {args.output}")

    if args.pdf:
        pdf_path = str(Path(args.output).with_suffix(".pdf"))
        try:
            html_to_pdf(args.output, pdf_path)
            print(f"PDF: {pdf_path}")
        except Exception as e:
            print(f"PDF export failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()