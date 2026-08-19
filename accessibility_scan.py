#!/usr/bin/env python3
"""
Automated accessibility / WCAG compliance scanner (standalone module).

Runs axe-core (via the pinned `@axe-core/cli` npm package) against every
discovered page, then rolls the raw rule violations up into:

  1. a per-standard panel - each legal framework is mapped to the WCAG scope
     it points at, and its verdict is computed by filtering the violations to
     that framework's axe tag set;
  2. findings grouped by impact (critical / serious / moderate / minor), each
     with its WCAG success criteria, a plain-language fix, and the affected
     pages + element selectors;
  3. an axe "incomplete" list surfaced as "Needs manual review".

IMPORTANT - this reports AUTOMATED CHECKS ONLY. Automated tooling can detect
roughly a third of WCAG failures; a clean run is not a statement of legal
compliance, and nothing in the report should be presented as one.

Used by dashboard.py; also runnable directly:
    python accessibility_scan.py https://example.com -o accessibility.html
"""

import argparse
import html
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------

# Pinned so a run is reproducible and the report can state exactly what ran.
AXE_CORE_VERSION = "4.13.0"
AXE_CLI_PACKAGE = f"@axe-core/cli@{AXE_CORE_VERSION}"

NPX = "npx.cmd" if os.name == "nt" else "npx"
PER_PAGE_TIMEOUT = 150  # seconds per page

# Tags axe is always asked to evaluate (a selected standard may add to these).
RUN_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa",
            "best-practice", "section508", "EN-301-549"]

IMPACT_ORDER = ["critical", "serious", "moderate", "minor"]


def _axe_cmd():
    """Prefer a globally installed `axe`; fall back to the pinned npx package."""
    import shutil
    exe = shutil.which("axe") or shutil.which("axe.cmd")
    return [exe] if exe else [NPX, "--yes", AXE_CLI_PACKAGE]


AXE = _axe_cmd()


def scan_env():
    """Env for scan subprocesses: relax Node TLS so sites with an incomplete
    certificate chain (which browsers tolerate but Node rejects) still scan."""
    env = os.environ.copy()
    env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
    return env


# --------------------------------------------------------------------------
# standards mapping
# --------------------------------------------------------------------------
# Each legal framework points at a WCAG version/level; we express that scope as
# the set of axe tags that belong to it, then filter violations by those tags.

W20A = ("wcag2a",)
W20AA = W20A + ("wcag2aa",)
W20AAA = W20AA + ("wcag2aaa",)
W21A = W20A + ("wcag21a",)
W21AA = W20AA + ("wcag21a", "wcag21aa")
W21AAA = W20AAA + ("wcag21a", "wcag21aa", "wcag21aaa")
W22A = W21A + ("wcag22a",)
W22AA = W21AA + ("wcag22a", "wcag22aa")
W22AAA = W21AAA + ("wcag22a", "wcag22aa", "wcag22aaa")

# id -> name, region, the WCAG scope in words, the axe tags that define it.
STANDARDS = [
    {"id": "wcag20a", "name": "WCAG 2.0 Level A", "region": "International",
     "basis": "WCAG 2.0 Level A", "tags": W20A, "applicable": True},
    {"id": "wcag20aa", "name": "WCAG 2.0 Level AA", "region": "International",
     "basis": "WCAG 2.0 Levels A + AA", "tags": W20AA, "applicable": True},
    {"id": "wcag20aaa", "name": "WCAG 2.0 Level AAA", "region": "International",
     "basis": "WCAG 2.0 Levels A + AA + AAA", "tags": W20AAA, "applicable": True},
    {"id": "wcag21a", "name": "WCAG 2.1 Level A", "region": "International",
     "basis": "WCAG 2.1 Level A", "tags": W21A, "applicable": True},
    {"id": "wcag21aa", "name": "WCAG 2.1 Level AA", "region": "International",
     "basis": "WCAG 2.1 Levels A + AA", "tags": W21AA, "applicable": True},
    {"id": "wcag21aaa", "name": "WCAG 2.1 Level AAA", "region": "International",
     "basis": "WCAG 2.1 Levels A + AA + AAA", "tags": W21AAA, "applicable": True},
    {"id": "wcag22a", "name": "WCAG 2.2 Level A", "region": "International",
     "basis": "WCAG 2.2 Level A", "tags": W22A, "applicable": True},
    {"id": "wcag22aa", "name": "WCAG 2.2 Level AA", "region": "International",
     "basis": "WCAG 2.2 Levels A + AA", "tags": W22AA, "applicable": True},
    {"id": "wcag22aaa", "name": "WCAG 2.2 Level AAA", "region": "International",
     "basis": "WCAG 2.2 Levels A + AA + AAA", "tags": W22AAA, "applicable": True},

    {"id": "section508", "name": "Section 508 (Revised)", "region": "United States",
     "basis": "WCAG 2.0 Level AA (+ Section 508 specific rules)",
     "tags": W20AA + ("section508",), "applicable": True},
    {"id": "en301549", "name": "EN 301 549", "region": "European Union",
     "basis": "WCAG 2.1 Level AA (+ EN 301 549 specific rules)",
     "tags": W21AA + ("EN-301-549",), "applicable": True},
    {"id": "ada", "name": "ADA Title III", "region": "United States",
     "basis": "WCAG 2.1 Level AA (DOJ-referenced benchmark)",
     "tags": W21AA, "applicable": True},
    {"id": "unruh", "name": "California Unruh Civil Rights Act", "region": "United States (CA)",
     "basis": "WCAG 2.1 Level AA", "tags": W21AA, "applicable": True},
    {"id": "aoda", "name": "Ontario AODA", "region": "Canada (ON)",
     "basis": "WCAG 2.0 Level AA", "tags": W20AA, "applicable": True},
    {"id": "uk_equality", "name": "UK Equality Act 2010", "region": "United Kingdom",
     "basis": "WCAG 2.1 Level AA", "tags": W21AA, "applicable": True},
    {"id": "au_dda", "name": "Australian DDA", "region": "Australia",
     "basis": "WCAG 2.1 Level AA", "tags": W21AA, "applicable": True},
    {"id": "il_5568", "name": "Israeli Standard 5568", "region": "Israel",
     "basis": "WCAG 2.0 Level AA", "tags": W20AA, "applicable": True},
    {"id": "ca_aca", "name": "Accessible Canada Act (ACA)", "region": "Canada",
     "basis": "WCAG 2.1 Level AA", "tags": W21AA, "applicable": True},

    {"id": "atag20", "name": "ATAG 2.0", "region": "International",
     "basis": "Authoring-tool guideline - not testable against a rendered page",
     "tags": (), "applicable": False,
     "note": "ATAG 2.0 governs authoring tools (how content is produced), not the "
             "delivered page, so no page-level automated check applies."},
]

STANDARDS_BY_ID = {s["id"]: s for s in STANDARDS}

DEFAULT_STANDARDS = ["wcag21aa", "section508", "en301549", "ada"]


def normalize_standards(selected):
    """Keep known ids in canonical order, drop junk; empty selection -> defaults."""
    if not selected:
        return list(DEFAULT_STANDARDS)
    if isinstance(selected, str):
        selected = [s.strip() for s in selected.split(",")]
    wanted = {str(s).strip() for s in selected if str(s).strip()}
    out = [s["id"] for s in STANDARDS if s["id"] in wanted]
    return out or list(DEFAULT_STANDARDS)


def run_tags(selected=None):
    """axe tag list for a run: always RUN_TAGS, plus anything a selected
    standard needs that the base list doesn't already cover (e.g. AAA)."""
    tags = list(RUN_TAGS)
    for sid in normalize_standards(selected):
        std = STANDARDS_BY_ID[sid]
        for t in std["tags"]:
            if t not in tags:
                tags.append(t)
    return tags


# --------------------------------------------------------------------------
# WCAG success criteria + plain-language fixes
# --------------------------------------------------------------------------

SC_TAG_RE = re.compile(r"^wcag(\d)(\d)(\d{1,2})$")

SC_NAMES = {
    "1.1.1": "Non-text Content", "1.2.1": "Audio-only and Video-only",
    "1.2.2": "Captions (Prerecorded)", "1.3.1": "Info and Relationships",
    "1.3.2": "Meaningful Sequence", "1.3.4": "Orientation",
    "1.3.5": "Identify Input Purpose", "1.4.1": "Use of Color",
    "1.4.2": "Audio Control", "1.4.3": "Contrast (Minimum)",
    "1.4.4": "Resize Text", "1.4.6": "Contrast (Enhanced)",
    "1.4.10": "Reflow", "1.4.11": "Non-text Contrast",
    "1.4.12": "Text Spacing", "1.4.13": "Content on Hover or Focus",
    "2.1.1": "Keyboard", "2.1.2": "No Keyboard Trap",
    "2.2.1": "Timing Adjustable", "2.2.2": "Pause, Stop, Hide",
    "2.4.1": "Bypass Blocks", "2.4.2": "Page Titled",
    "2.4.3": "Focus Order", "2.4.4": "Link Purpose (In Context)",
    "2.4.6": "Headings and Labels", "2.4.7": "Focus Visible",
    "2.5.3": "Label in Name", "2.5.8": "Target Size (Minimum)",
    "3.1.1": "Language of Page", "3.1.2": "Language of Parts",
    "3.2.1": "On Focus", "3.2.2": "On Input", "3.2.6": "Consistent Help",
    "3.3.1": "Error Identification", "3.3.2": "Labels or Instructions",
    "4.1.1": "Parsing", "4.1.2": "Name, Role, Value", "4.1.3": "Status Messages",
}

# Plain-language remediation for the rules that fire most often. Anything not
# listed falls back to axe's own help text.
FIX_HINTS = {
    "color-contrast":
        "Darken the text or lighten the background until the contrast ratio is at "
        "least 4.5:1 for normal text (3:1 for text 18pt/14pt-bold and larger).",
    "color-contrast-enhanced":
        "For AAA, push the contrast ratio to at least 7:1 for normal text (4.5:1 for large text).",
    "image-alt":
        "Give every meaningful image an alt attribute that describes what it conveys; "
        "use alt=\"\" for purely decorative images so screen readers skip them.",
    "input-image-alt":
        "Add an alt attribute to image buttons describing the action they perform.",
    "area-alt":
        "Give each image-map area an alt attribute describing its destination.",
    "link-name":
        "Make sure every link has text (or an aria-label / visually-hidden span) that "
        "says where it goes - icon-only links need an accessible name.",
    "button-name":
        "Give every button visible text or an aria-label; icon-only buttons are "
        "announced as just \"button\" without one.",
    "label":
        "Attach a <label for=\"...\"> to every form control (or use aria-label / "
        "aria-labelledby) so its purpose is announced.",
    "form-field-multiple-labels":
        "Leave exactly one label per form field - multiple labels are announced inconsistently.",
    "select-name":
        "Give each <select> an associated label so its purpose is announced.",
    "aria-required-attr":
        "Add the ARIA attributes the element's role requires, or drop the role and use "
        "the native HTML element instead.",
    "aria-required-children":
        "Nest the child roles the parent role requires (e.g. a role=\"list\" needs role=\"listitem\" children).",
    "aria-required-parent":
        "Place the element inside the parent role its ARIA role requires.",
    "aria-valid-attr-value":
        "Fix the ARIA attribute value - aria-labelledby/aria-describedby must point at "
        "ids that actually exist on the page.",
    "aria-hidden-focus":
        "Never put aria-hidden=\"true\" on something focusable; remove the attribute or "
        "take the element out of the tab order.",
    "aria-allowed-attr":
        "Remove ARIA attributes that aren't allowed on this element's role.",
    "aria-roles":
        "Use a valid ARIA role, or remove the role and rely on the native element semantics.",
    "html-has-lang":
        "Add a lang attribute to the <html> element (e.g. lang=\"en\") so screen readers "
        "use the right pronunciation.",
    "html-lang-valid":
        "Correct the <html> lang value to a valid BCP-47 language tag.",
    "valid-lang":
        "Correct the lang attribute on the element to a valid BCP-47 language tag.",
    "document-title":
        "Give the page a unique, descriptive <title> - it is the first thing announced.",
    "heading-order":
        "Use heading levels in order (h1 then h2 then h3) without skipping levels.",
    "empty-heading":
        "Remove empty headings or give them text - a heading with no content is announced as noise.",
    "page-has-heading-one":
        "Add a single <h1> that names what the page is about.",
    "landmark-one-main":
        "Wrap the primary content in one <main> element so keyboard users can jump to it.",
    "region":
        "Put all page content inside landmarks (header/nav/main/footer) so it can be navigated by region.",
    "bypass":
        "Add a \"skip to main content\" link (or proper landmarks) so keyboard users can "
        "bypass the repeated navigation.",
    "duplicate-id":
        "Make every id on the page unique - duplicates break label and ARIA references.",
    "duplicate-id-active":
        "Make ids on interactive elements unique so labels and ARIA references resolve.",
    "duplicate-id-aria":
        "Make ids referenced by ARIA attributes unique so they resolve to one element.",
    "list":
        "Only <li> (and script/template) may be direct children of <ul>/<ol> - move other markup inside an <li>.",
    "listitem":
        "Put every <li> inside a <ul> or <ol> so it is announced as part of a list.",
    "definition-list":
        "Structure <dl> as <dt>/<dd> pairs only.",
    "td-headers-attr":
        "Point each headers attribute at ids of cells in the same table.",
    "th-has-data-cells":
        "Make sure each header cell describes data cells, and mark headers with <th>/scope.",
    "table-fake-caption":
        "Use a real <caption> element instead of a styled row for the table caption.",
    "frame-title":
        "Give every <iframe> a title attribute describing the embedded content.",
    "meta-viewport":
        "Remove user-scalable=no and allow a maximum-scale of at least 5 so the page can be zoomed.",
    "meta-refresh":
        "Remove the auto-refresh/redirect, or make the delay adjustable by the user.",
    "tabindex":
        "Avoid tabindex greater than 0 - it makes the tab order unpredictable; use DOM order instead.",
    "target-size":
        "Make interactive targets at least 24x24 CSS pixels, or space them so they don't overlap.",
    "scrollable-region-focusable":
        "Make scrollable regions keyboard-reachable (tabindex=\"0\") so they can be scrolled without a mouse.",
    "nested-interactive":
        "Don't nest interactive controls (e.g. a button inside a link) - screen readers announce only one.",
    "video-caption":
        "Add captions to video so the audio content is available to deaf and hard-of-hearing users.",
    "object-alt":
        "Give <object> elements alternative text describing the embedded content.",
    "server-side-image-map":
        "Replace server-side image maps with client-side maps that expose alt text per area.",
    "blink": "Remove <blink> - blinking content is a distraction and a seizure risk.",
    "marquee": "Replace <marquee> with static content the user controls.",
}


def sc_from_tags(tags):
    """axe tags like 'wcag143' -> WCAG success criterion '1.4.3'."""
    out = set()
    for t in tags or []:
        m = SC_TAG_RE.match(str(t).strip())
        if m:
            out.add(".".join(m.groups()))
    return sorted(out, key=lambda s: [int(x) for x in s.split(".")])


def sc_label(sc):
    name = SC_NAMES.get(sc)
    return f"{sc} {name}" if name else sc


def fix_for(finding):
    """Plain-language remediation: curated where we have one, axe's help otherwise."""
    hint = FIX_HINTS.get(finding.get("id"))
    if hint:
        return hint
    helptext = (finding.get("help") or "").strip().rstrip(".")
    desc = (finding.get("description") or "").strip()
    if helptext and desc:
        return f"{helptext}. {desc}"
    return helptext or desc or "Review this element against the referenced success criteria."


# --------------------------------------------------------------------------
# axe invocation + aggregation
# --------------------------------------------------------------------------

def normalize_axe(data, url=""):
    """@axe-core/cli saves an ARRAY of per-URL result objects; a raw axe.run()
    result is a single object. Accept either shape."""
    if isinstance(data, list):
        data = next((d for d in data if isinstance(d, dict)), {})
    if not isinstance(data, dict):
        data = {}

    def bucket(key):
        return [v for v in (data.get(key) or []) if isinstance(v, dict)]

    engine = data.get("testEngine")
    version = engine.get("version") if isinstance(engine, dict) else ""
    return {
        "url": data.get("url") or url,
        "violations": bucket("violations"),
        "incomplete": bucket("incomplete"),
        "engine": version or "",
    }


def run_axe(url, out_dir, tags=None, index=0, timeout=PER_PAGE_TIMEOUT):
    """Run axe-core against one URL. Returns (normalized_result|None, secs, why)."""
    tags = list(tags or RUN_TAGS)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"axe_{index:03d}.json"
    out_file = out_dir / name
    cmd = AXE + [url,
                 "--tags", ",".join(tags),
                 "--dir", str(out_dir),
                 "--save", name,
                 "--timeout", str(timeout),
                 # the CLI already runs headless chrome; these only relax the sandbox
                 # and cert handling so odd hosts still audit (cf. scan_env()).
                 "--chrome-options", "no-sandbox,disable-gpu,ignore-certificate-errors"]
    t0 = time.time()
    try:
        # Without --exit axe returns 0 even when rules fail, so the saved result
        # file - not the return code - is what says the page was really audited.
        subprocess.run(cmd, cwd=str(out_dir), check=False, timeout=timeout + 30,
                       env=scan_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, int(time.time() - t0), "timeout"
    except FileNotFoundError:
        return None, int(time.time() - t0), "axe CLI not found (Node.js required)"
    except Exception as e:
        return None, int(time.time() - t0), str(e)
    secs = int(time.time() - t0)
    if not (out_file.exists() and out_file.stat().st_size > 0):
        return None, secs, "no result file"
    try:
        data = json.loads(out_file.read_text(encoding="utf-8"))
    except Exception as e:
        return None, secs, f"unreadable result ({e})"
    return normalize_axe(data, url), secs, ""


def _norm_impact(impact):
    """axe impact -> one of IMPACT_ORDER (anything unknown/absent is 'minor')."""
    imp = (impact or "").strip().lower()
    return imp if imp in IMPACT_ORDER else "minor"


def _impact_rank(impact):
    return IMPACT_ORDER.index(_norm_impact(impact))


def _collect(bucket, url, items):
    """Fold one page's axe items into the cross-page rule bucket."""
    for it in items:
        rid = it.get("id") or "unknown"
        entry = bucket.get(rid)
        if entry is None:
            entry = bucket[rid] = {
                "id": rid,
                "impact": _norm_impact(it.get("impact")),
                "help": it.get("help") or rid,
                "description": it.get("description") or "",
                "helpUrl": it.get("helpUrl") or "",
                "tags": list(it.get("tags") or []),
                "pages": {},
                "nodes": 0,
            }
        elif _impact_rank(it.get("impact")) < _impact_rank(entry["impact"]):
            entry["impact"] = _norm_impact(it.get("impact"))  # keep the most severe seen
        for t in it.get("tags") or []:
            if t not in entry["tags"]:
                entry["tags"].append(t)
        elements = entry["pages"].setdefault(url, [])
        for n in it.get("nodes") or []:
            if not isinstance(n, dict):
                continue
            target = n.get("target") or []
            elements.append({
                "target": ", ".join(str(t) for t in target) if isinstance(target, list) else str(target),
                "html": n.get("html") or "",
            })
            entry["nodes"] += 1


def aggregate(results):
    """results: list of normalized axe results.
    Returns (violations, incomplete, engine_version) with rules deduplicated
    across pages and sorted most-severe first."""
    viol, incomp, engine = {}, {}, ""
    for r in results:
        url = r.get("url") or ""
        engine = engine or (r.get("engine") or "")
        _collect(viol, url, r.get("violations") or [])
        _collect(incomp, url, r.get("incomplete") or [])

    def finish(bucket):
        out = list(bucket.values())
        for f in out:
            f["sc"] = sc_from_tags(f["tags"])
        out.sort(key=lambda f: (_impact_rank(f["impact"]), f["id"]))
        return out

    return finish(viol), finish(incomp), engine


def evaluate_standards(findings, selected=None):
    """Per-standard verdict: filter the violations to the standard's tag set."""
    out = []
    for sid in normalize_standards(selected):
        std = STANDARDS_BY_ID[sid]
        row = {"id": sid, "name": std["name"], "region": std["region"],
               "basis": std["basis"], "note": std.get("note", ""),
               "rules": 0, "elements": 0, "pages": 0, "rule_ids": []}
        if not std["applicable"]:
            row["verdict"] = "n/a"
            out.append(row)
            continue
        scope = set(std["tags"])
        matched = [f for f in findings if scope & set(f.get("tags") or [])]
        pages = set()
        for f in matched:
            pages |= set(f.get("pages") or {})
            row["elements"] += f.get("nodes", 0)
        row["rules"] = len(matched)
        row["pages"] = len(pages)
        row["rule_ids"] = [f["id"] for f in matched]
        row["verdict"] = "fail" if matched else "pass"
        out.append(row)
    return out


def scan_site(site_url, urls, standards=None, concurrency=3, work_dir=None, log=print):
    """Run axe over every URL and return the accessibility report HTML."""
    standards = normalize_standards(standards)
    tags = run_tags(standards)
    urls = list(urls) or [site_url]
    tmp_holder = None
    if work_dir is None:
        tmp_holder = tempfile.mkdtemp(prefix="axe-")
        work_dir = tmp_holder
    work_dir = Path(work_dir)

    log(f"Accessibility: axe-core {AXE_CORE_VERSION} on {len(urls)} page(s), "
        f"{concurrency} in parallel...")
    results = []
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=max(1, int(concurrency or 1))) as pool:
        futures = {pool.submit(run_axe, u, work_dir, tags, i): u
                   for i, u in enumerate(urls, 1)}
        for n, fut in enumerate(as_completed(futures), 1):
            u = futures[fut]
            res, secs, why = fut.result()
            if res:
                results.append(res)
                log(f"[{n}/{len(urls)}] a11y OK ({secs}s) {u}")
            else:
                log(f"[{n}/{len(urls)}] a11y skipped ({why}) {u}")

    violations, incomplete, engine = aggregate(results)
    log(f"Accessibility: {len(violations)} failing rule(s), "
        f"{len(incomplete)} needing manual review.")
    panel = evaluate_standards(violations, standards)
    return build_accessibility_html(
        site_url, panel, violations, incomplete,
        scanned=len(results), total=len(urls),
        engine_version=engine or AXE_CORE_VERSION, tags=tags)


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

VERDICT_LABEL = {
    "pass": "No violations (automated)",
    "fail": "Violations found (automated)",
    "n/a": "Not applicable",
}
VERDICT_CLASS = {"pass": "ok", "fail": "bad", "n/a": "na"}

IMPACT_BLURB = {
    "critical": "Blocks access outright for some users.",
    "serious": "Makes content very hard to use with assistive technology.",
    "moderate": "Causes friction or confusion for some users.",
    "minor": "Minor annoyance or inconsistency.",
}


def _pages_html(pages, scanned):
    """Affected pages + a few offending element selectors/snippets each."""
    if not pages:
        return '<div class="scope">No element detail captured.</div>'
    items = []
    for url in sorted(pages)[:8]:
        elements = pages[url]
        path = urlparse(url).path or "/"
        snips = []
        for el in elements[:3]:
            tgt = html.escape(el.get("target") or "")
            snip = html.escape((el.get("html") or "")[:220])
            block = f'<code class="sel">{tgt}</code>' if tgt else ""
            if snip:
                block += f'<pre class="snip">{snip}</pre>'
            if block:
                snips.append(f"<li>{block}</li>")
        more = (f'<li class="more">+{len(elements) - 3} more element(s) on this page</li>'
                if len(elements) > 3 else "")
        items.append(f'<li class="pg"><b>{html.escape(path)}</b>'
                     f'<span class="cnt">{len(elements)} element(s)</span>'
                     f'<ul class="els">{"".join(snips)}{more}</ul></li>')
    extra = (f'<li class="more">+{len(pages) - 8} more page(s)</li>'
             if len(pages) > 8 else "")
    label = ("all scanned pages" if scanned and len(pages) >= scanned
             else f"{len(pages)} of {scanned} pages" if scanned else f"{len(pages)} page(s)")
    return (f'<div class="scope">Affects {label}</div>'
            f'<ul class="pages">{"".join(items)}{extra}</ul>')


def _finding_card(f, scanned, manual=False):
    sc = ", ".join(sc_label(s) for s in f.get("sc") or []) or "-"
    tags = ", ".join(t for t in (f.get("tags") or []) if not t.startswith("cat."))
    impact = _norm_impact(f.get("impact"))
    helpurl = f.get("helpUrl") or ""
    link = (f'<a class="hu" href="{html.escape(helpurl)}" target="_blank" rel="noopener">axe rule reference &#8599;</a>'
            if helpurl else "")
    fix_label = "What to check" if manual else "Fix"
    return (
        f'<div class="finding {impact}">'
        f'<div class="fh"><span class="imp {impact}">{impact}</span>'
        f'<span class="ft">{html.escape(f.get("help") or f.get("id"))}</span>'
        f'<code class="rid">{html.escape(f.get("id"))}</code></div>'
        f'<div class="sc"><b>WCAG success criteria:</b> {html.escape(sc)}</div>'
        f'<div class="tags">{html.escape(tags)}</div>'
        f'<div class="rec"><b>{fix_label}:</b> {html.escape(fix_for(f))} {link}</div>'
        f'{_pages_html(f.get("pages") or {}, scanned)}'
        f'</div>')


def build_accessibility_html(site_url, panel, violations, incomplete,
                             scanned=0, total=0, engine_version=AXE_CORE_VERSION,
                             tags=None):
    gen = datetime.now().strftime("%Y-%m-%d %H:%M")
    tags = list(tags or RUN_TAGS)

    # --- per-standard compliance panel ---
    cards = []
    for row in panel:
        cls = VERDICT_CLASS[row["verdict"]]
        if row["verdict"] == "n/a":
            detail = html.escape(row.get("note") or "Not testable by automated page checks.")
        elif row["verdict"] == "pass":
            detail = "No violations of this standard's success criteria were detected by the automated checks."
        else:
            detail = (f'{row["rules"]} failing rule(s) &middot; {row["elements"]} element(s) '
                      f'&middot; {row["pages"]} page(s)')
        rules = (f'<div class="srules">Failing rules: '
                 f'{html.escape(", ".join(row["rule_ids"][:12]))}'
                 f'{" +%d more" % (len(row["rule_ids"]) - 12) if len(row["rule_ids"]) > 12 else ""}</div>'
                 if row["rule_ids"] else "")
        cards.append(
            f'<div class="std {cls}">'
            f'<div class="sh"><span class="verdict {cls}">{VERDICT_LABEL[row["verdict"]]}</span>'
            f'<b>{html.escape(row["name"])}</b>'
            f'<span class="reg">{html.escape(row["region"])}</span></div>'
            f'<div class="basis">Scope: {html.escape(row["basis"])}</div>'
            f'<div class="sdetail">{detail}</div>{rules}</div>')
    panel_html = "\n".join(cards) or '<p class="note">No standards selected.</p>'

    # --- findings grouped by impact ---
    counts = {i: 0 for i in IMPACT_ORDER}
    for f in violations:
        counts[_norm_impact(f.get("impact"))] += 1

    groups = []
    for impact in IMPACT_ORDER:
        group = [f for f in violations if _norm_impact(f.get("impact")) == impact]
        if not group:
            continue
        body = "\n".join(_finding_card(f, scanned) for f in group)
        groups.append(
            f'<h3 class="grp {impact}">{impact.title()} '
            f'<span class="gc">{len(group)} rule(s)</span>'
            f'<span class="gb">{IMPACT_BLURB[impact]}</span></h3>{body}')
    findings_html = "\n".join(groups) or \
        '<p class="clean">No violations detected by the automated checks on the scanned pages.</p>'

    manual_html = "\n".join(_finding_card(f, scanned, manual=True) for f in incomplete) or \
        '<p class="clean">Nothing flagged for manual review on the scanned pages.</p>'

    stat_cards = "".join(
        f'<div class="stat {i}"><b>{counts[i]}</b><span>{i}</span></div>'
        for i in IMPACT_ORDER)
    stat_cards += (f'<div class="stat manual"><b>{len(incomplete)}</b>'
                   f'<span>manual review</span></div>')

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Accessibility Report (Automated checks) - {html.escape(site_url)}</title>
<style>
  :root {{ --ink:#1c2430; --line:#e3e6ea; --bg:#f6f7f9; --card:#fff; --muted:#5b6672;
           --critical:#b3122b; --serious:#d6521f; --moderate:#b06f00; --minor:#3b7dbf;
           --ok:#0a7d34; --bad:#b3122b; --na:#5b6672; --manual:#6b4fc4; }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; margin:0;
          background:var(--bg); color:var(--ink); line-height:1.5; }}
  .wrap {{ max-width:1040px; margin:0 auto; padding:32px 20px 64px; }}
  h1 {{ font-size:23px; margin:0 0 4px; }}
  h2 {{ margin:36px 0 10px; font-size:18px; }}
  .meta {{ color:var(--muted); font-size:13px; margin-bottom:14px; }}
  .banner {{ background:#fbf6e6; border:1px solid #efe1b0; color:#7a5c00; border-radius:10px;
             padding:12px 16px; font-size:13px; margin:14px 0 4px; }}
  .stats {{ display:flex; gap:12px; margin:18px 0 8px; flex-wrap:wrap; }}
  .stat {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
           padding:12px 16px; min-width:104px; text-align:center; }}
  .stat b {{ display:block; font-size:24px; }}
  .stat span {{ font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }}
  .stat.critical b {{ color:var(--critical); }} .stat.serious b {{ color:var(--serious); }}
  .stat.moderate b {{ color:var(--moderate); }} .stat.minor b {{ color:var(--minor); }}
  .stat.manual b {{ color:var(--manual); }}
  .stds {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:12px; }}
  .std {{ background:var(--card); border:1px solid var(--line); border-left:5px solid var(--line);
          border-radius:10px; padding:13px 16px; }}
  .std.ok {{ border-left-color:var(--ok); }} .std.bad {{ border-left-color:var(--bad); }}
  .std.na {{ border-left-color:var(--na); }}
  .sh {{ display:flex; align-items:center; gap:9px; flex-wrap:wrap; }}
  .sh b {{ font-size:15px; }}
  .verdict {{ font-size:10.5px; font-weight:700; text-transform:uppercase; letter-spacing:.04em;
              padding:2px 9px; border-radius:20px; color:#fff; }}
  .verdict.ok {{ background:var(--ok); }} .verdict.bad {{ background:var(--bad); }}
  .verdict.na {{ background:var(--na); }}
  .reg {{ font-size:11px; background:#eef1f4; color:var(--muted); padding:1px 8px; border-radius:20px; }}
  .basis {{ font-size:12.5px; color:var(--muted); margin-top:6px; }}
  .sdetail {{ font-size:13px; margin-top:4px; }}
  .srules {{ font-size:12px; color:var(--muted); margin-top:6px; word-break:break-word; }}
  .grp {{ margin:26px 0 8px; font-size:15px; text-transform:capitalize; }}
  .grp.critical {{ color:var(--critical); }} .grp.serious {{ color:var(--serious); }}
  .grp.moderate {{ color:var(--moderate); }} .grp.minor {{ color:var(--minor); }}
  .gc {{ font-size:12px; font-weight:600; color:var(--muted); margin-left:8px; }}
  .gb {{ display:block; font-size:12px; font-weight:400; color:var(--muted); }}
  .finding {{ background:var(--card); border:1px solid var(--line); border-left:5px solid var(--line);
              border-radius:10px; padding:14px 18px; margin:12px 0; }}
  .finding.critical {{ border-left-color:var(--critical); }}
  .finding.serious {{ border-left-color:var(--serious); }}
  .finding.moderate {{ border-left-color:var(--moderate); }}
  .finding.minor {{ border-left-color:var(--minor); }}
  .fh {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
  .imp {{ font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.04em;
          padding:2px 9px; border-radius:20px; color:#fff; }}
  .imp.critical {{ background:var(--critical); }} .imp.serious {{ background:var(--serious); }}
  .imp.moderate {{ background:var(--moderate); }} .imp.minor {{ background:var(--minor); }}
  .ft {{ font-weight:600; font-size:15px; }}
  .rid {{ font-size:11.5px; background:#eef1f4; color:var(--muted); padding:1px 7px; border-radius:5px; }}
  .sc {{ font-size:13px; margin-top:8px; }}
  .tags {{ font-size:11.5px; color:var(--muted); margin-top:2px; word-break:break-word; }}
  .rec {{ font-size:14px; margin-top:8px; }}
  .hu {{ font-size:12px; color:#0a58ca; text-decoration:none; white-space:nowrap; }}
  .scope {{ font-size:13px; color:var(--muted); margin-top:10px; }}
  ul.pages {{ margin:6px 0 0; padding-left:18px; font-size:12.5px; }}
  ul.pages .pg {{ margin-bottom:6px; }}
  ul.pages .cnt {{ color:var(--muted); margin-left:8px; font-size:11.5px; }}
  ul.els {{ list-style:none; margin:4px 0 0; padding:0; }}
  ul.els li {{ margin:3px 0; }}
  .sel {{ font-size:11.5px; background:#eef1f4; padding:1px 6px; border-radius:5px; word-break:break-all; }}
  .snip {{ margin:3px 0 0; padding:6px 8px; background:#0f172a; color:#c7d2e6; border-radius:6px;
           font-size:11px; white-space:pre-wrap; word-break:break-all; overflow:auto; }}
  .more {{ color:var(--muted); }}
  .clean {{ color:var(--ok); font-size:15px; }}
  .note {{ color:var(--muted); font-size:13px; }}
  @media print {{ body {{ background:#fff; }} .finding, .std {{ break-inside:avoid; }} }}
</style></head>
<body><div class="wrap">
  <h1>Accessibility Report &mdash; Automated checks</h1>
  <div class="meta">{html.escape(site_url)} &middot; {scanned} of {total} page(s) analyzed
    &middot; axe-core {html.escape(str(engine_version))} &middot; Generated {gen}</div>
  <div class="banner"><b>Automated checks only.</b> These results come from axe-core rules run
    against the rendered pages. Automated testing can surface only a portion of WCAG failures
    (many criteria require human judgement), so this report describes automated check results
    &mdash; it is not a legal compliance determination or a certification for any standard listed below.</div>
  <div class="meta">axe tags evaluated: {html.escape(", ".join(tags))}</div>
  <div class="stats">{stat_cards}</div>

  <h2>Standards &mdash; automated check results</h2>
  <div class="stds">{panel_html}</div>

  <h2>Findings by impact</h2>
  {findings_html}

  <h2>Needs manual review</h2>
  <p class="note">axe could not decide these automatically &mdash; a person has to confirm them.</p>
  {manual_html}

  <p class="meta" style="margin-top:26px">Generated with axe-core
    {html.escape(str(engine_version))} (pinned {AXE_CORE_VERSION}). Automated checks only &mdash;
    pair them with manual keyboard, screen-reader and cognitive-load testing before making any
    accessibility conformance claim.</p>
</div></body></html>"""


def main():
    ap = argparse.ArgumentParser(description="Automated accessibility (axe-core) scan.")
    ap.add_argument("site", nargs="?")
    ap.add_argument("-o", "--output", default="accessibility.html")
    ap.add_argument("--standards", default=",".join(DEFAULT_STANDARDS),
                    help="comma-separated standard ids (see --list-standards)")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--list-standards", action="store_true")
    args = ap.parse_args()

    if args.list_standards:
        for s in STANDARDS:
            flag = "" if s["applicable"] else "  (not applicable)"
            print(f'{s["id"]:<12} {s["name"]} - {s["basis"]}{flag}')
        return
    if not args.site:
        ap.error("a site URL is required")

    site = args.site if urlparse(args.site).scheme else "https://" + args.site
    out = scan_site(site, [site], standards=args.standards, concurrency=args.concurrency)
    Path(args.output).write_text(out, encoding="utf-8")
    print(f"Accessibility report -> {args.output}")


if __name__ == "__main__":
    main()
