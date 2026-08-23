#!/usr/bin/env python3
"""
The report's data model.

Everything the renderer needs, in one normalized structure, assembled from the
three scanners that already exist:

    Lighthouse  ->  consolidate_report.load_ci_results / load_lhr_pages
    axe-core    ->  accessibility_scan.collect_site
    passive sec ->  security_scan.collect_site

This module reads those results; it never re-scores them. Lighthouse category
scores, axe impacts and the security severities are carried through exactly as
the scanners produced them. What is added here is presentation only, and each
addition is stated in the report's own methodology page:

  * severity      - one scale across three tools, so a "fix first" list can be
                    ordered at all (Lighthouse audit score / axe impact /
                    security severity all map onto critical..minor).
  * effort        - a coarse, editorial estimate per issue (quick / moderate /
                    involved). It orders work; it does not judge the site.
  * priority      - severity x reach / effort, used purely for sorting.
  * health score  - a weighted roll-up of the category scores that were run.
  * security and WCAG "scores" - the two scanners emit findings, not 0-100
                    scores, so a score is derived from their finding counts to
                    put them on the same scorecard.
"""

import re
from datetime import datetime
from urllib.parse import urlparse

# --------------------------------------------------------------------------
# categories
# --------------------------------------------------------------------------

# key, label, lighthouse?, weight in the health roll-up, one-line "what it is"
CATEGORY_DEFS = [
    ("performance", "Performance", True, 0.22,
     "How fast pages feel to a real visitor on a real connection."),
    ("accessibility", "Accessibility", True, 0.18,
     "Whether the pages can be used with a screen reader, keyboard or magnifier."),
    ("seo", "SEO", True, 0.15,
     "Whether search engines can crawl, understand and list the pages."),
    ("best-practices", "Best Practices", True, 0.13,
     "Modern web hygiene: safe APIs, correct images, no console errors."),
    ("security", "Security", False, 0.18,
     "Transport security, headers and cookie hardening on the live site."),
    ("wcag", "WCAG Standards", False, 0.14,
     "Automated conformance against the accessibility laws and standards selected."),
]

CATEGORY_LABEL = {k: label for k, label, _, _, _ in CATEGORY_DEFS}
CATEGORY_WEIGHT = {k: w for k, _, _, w, _ in CATEGORY_DEFS}
CATEGORY_BLURB = {k: b for k, _, _, _, b in CATEGORY_DEFS}
CATEGORY_ORDER = [k for k, _, _, _, _ in CATEGORY_DEFS]

# Lighthouse category key -> our key (only "best-practices" differs in casing)
LH_CATEGORY_KEYS = ["performance", "accessibility", "best-practices", "seo"]

SEVERITIES = ["critical", "serious", "moderate", "minor"]
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}
SEVERITY_LABEL = {
    "critical": "Critical",
    "serious": "Serious",
    "moderate": "Moderate",
    "minor": "Minor",
}
SEVERITY_WEIGHT = {"critical": 10.0, "serious": 6.0, "moderate": 3.0, "minor": 1.0}

# security_scan's five-step scale folded onto the shared four
SECURITY_SEVERITY_MAP = {
    "Critical": "critical",
    "High": "serious",
    "Medium": "moderate",
    "Low": "minor",
    "Info": "minor",
}

EFFORTS = ["quick", "moderate", "involved"]
EFFORT_LABEL = {
    "quick": "Quick win",
    "moderate": "Moderate",
    "involved": "Involved",
}
EFFORT_HINT = {
    "quick": "A config or markup change - usually under an hour.",
    "moderate": "A focused change to templates or assets - part of a day.",
    "involved": "Needs design, content or engineering time across templates.",
}
EFFORT_COST = {"quick": 1.0, "moderate": 1.7, "involved": 2.6}

HEALTH_BANDS = [
    (90, "strong", "Strong",
     "The site is in good shape. Keep the current standard and monitor."),
    (75, "solid", "Solid",
     "Fundamentals are sound with a short list of fixes worth scheduling."),
    (50, "needs-work", "Needs work",
     "Several issues are affecting visitors today and should be planned in now."),
    (0, "at-risk", "At risk",
     "Problems are widespread and are costing traffic, usability or trust."),
]


# --------------------------------------------------------------------------
# effort estimates (editorial, presentation-layer only)
# --------------------------------------------------------------------------

# Anything not listed falls back to the per-category default below.
EFFORT_BY_ID = {
    # --- Lighthouse: config-level wins
    "uses-text-compression": "quick",
    "uses-long-cache-ttl": "quick",
    "server-response-time": "moderate",
    "redirects": "quick",
    "uses-http2": "quick",
    "efficient-animated-content": "moderate",
    "duplicated-javascript": "moderate",
    "legacy-javascript": "moderate",
    "render-blocking-resources": "moderate",
    "unminified-css": "quick",
    "unminified-javascript": "quick",
    "unused-css-rules": "moderate",
    "unused-javascript": "involved",
    "modern-image-formats": "moderate",
    "uses-optimized-images": "moderate",
    "uses-responsive-images": "moderate",
    "offscreen-images": "moderate",
    "total-byte-weight": "involved",
    "dom-size": "involved",
    "bootup-time": "involved",
    "mainthread-work-breakdown": "involved",
    "third-party-summary": "involved",
    "largest-contentful-paint-element": "involved",
    "layout-shift-elements": "moderate",
    "font-display": "quick",
    "preload-lcp-image": "quick",
    "viewport": "quick",
    "document-title": "quick",
    "meta-description": "quick",
    "link-text": "quick",
    "crawlable-anchors": "moderate",
    "robots-txt": "quick",
    "hreflang": "moderate",
    "canonical": "quick",
    "is-crawlable": "quick",
    "image-alt": "moderate",
    "html-has-lang": "quick",
    "color-contrast": "moderate",
    "heading-order": "moderate",
    "label": "moderate",
    "link-name": "moderate",
    "button-name": "moderate",
    "aria-required-attr": "moderate",
    "csp-xss": "involved",
    "errors-in-console": "moderate",
    "no-vulnerable-libraries": "moderate",
    "deprecations": "involved",
    # --- security rules
    "hsts": "quick",
    "csp": "involved",
    "xfo": "quick",
    "nosniff": "quick",
    "referrer": "quick",
    "permissions": "quick",
    "server_banner": "quick",
    "powered_by": "quick",
    "cors_wildcard": "moderate",
    "cookie_secure": "quick",
    "cookie_httponly": "quick",
    "cookie_samesite": "quick",
    "mixed_content": "moderate",
    "no_https_redirect": "quick",
    "cert_expired": "quick",
    "cert_expiring": "quick",
    "cert_chain": "quick",
    "cert_selfsigned": "moderate",
    "no_securitytxt": "quick",
    # --- axe rules that are usually template-wide
    "target-size": "moderate",
    "landmark-one-main": "moderate",
    "region": "moderate",
    "aria-allowed-attr": "moderate",
    "nested-interactive": "involved",
    "scrollable-region-focusable": "involved",
    "frame-title": "quick",
    "document-title-axe": "quick",
}

# Lighthouse scores many audits as pass/fail, so a missing meta description and
# a five-second Largest Contentful Paint both arrive as "score 0". Ranking on
# that alone puts hygiene above breakage. This table states what each
# well-known audit actually costs a visitor; anything not listed still falls
# back to the score-derived severity below.
SEVERITY_BY_ID = {
    # performance: the ones a visitor feels
    "largest-contentful-paint": "critical",
    "server-response-time": "critical",
    "render-blocking-resources": "serious",
    "total-blocking-time": "serious",
    "cumulative-layout-shift": "serious",
    "interactive": "serious",
    "unused-javascript": "serious",
    "modern-image-formats": "serious",
    "uses-responsive-images": "serious",
    "uses-text-compression": "serious",
    "offscreen-images": "moderate",
    "unminified-css": "moderate",
    "unminified-javascript": "moderate",
    "unused-css-rules": "moderate",
    "uses-long-cache-ttl": "moderate",
    "efficient-animated-content": "moderate",
    "duplicated-javascript": "moderate",
    "legacy-javascript": "moderate",
    "font-display": "minor",
    "total-byte-weight": "moderate",
    "dom-size": "moderate",
    "third-party-summary": "moderate",
    # accessibility: barriers, not hygiene
    "image-alt": "critical",
    "label": "critical",
    "button-name": "critical",
    "link-name": "serious",
    "color-contrast": "serious",
    "html-has-lang": "serious",
    "frame-title": "serious",
    "heading-order": "moderate",
    "aria-required-attr": "serious",
    # best practices
    "no-vulnerable-libraries": "critical",
    "is-on-https": "critical",
    "csp-xss": "serious",
    "errors-in-console": "moderate",
    "deprecations": "moderate",
    "image-aspect-ratio": "minor",
    # SEO: real visibility loss vs. tidy-up
    "is-crawlable": "critical",
    "robots-txt": "serious",
    "http-status-code": "critical",
    "canonical": "moderate",
    "hreflang": "moderate",
    "meta-description": "moderate",
    "document-title": "serious",
    "link-text": "moderate",
    "crawlable-anchors": "moderate",
    "viewport": "serious",
}

EFFORT_DEFAULT_BY_CATEGORY = {
    "performance": "moderate",
    "accessibility": "moderate",
    "seo": "quick",
    "best-practices": "moderate",
    "security": "quick",
    "wcag": "moderate",
}


def effort_for(issue_id, category):
    return EFFORT_BY_ID.get(str(issue_id or "").strip(),
                            EFFORT_DEFAULT_BY_CATEGORY.get(category, "moderate"))


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def pct(score):
    """Lighthouse 0..1 -> 0..100 int, None stays None."""
    if score is None:
        return None
    try:
        return int(round(float(score) * 100))
    except (TypeError, ValueError):
        return None


def score_band(score100):
    if score100 is None:
        return "na"
    if score100 >= 90:
        return "good"
    if score100 >= 50:
        return "avg"
    return "poor"


BAND_VERDICT = {
    "good": "Good",
    "avg": "Needs work",
    "poor": "Poor",
    "na": "Not run",
}


def path_of(url):
    if not url:
        return "/"
    if "://" in str(url):
        parsed = urlparse(str(url))
        p = parsed.path or "/"
        if parsed.query:
            p += "?" + parsed.query
        return p
    return str(url)


def slug(text, prefix="s"):
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return f"{prefix}-{s}" if s else prefix


def severity_for_audit(audit_id, score):
    """Curated severity where we have one, score-derived otherwise."""
    known = SEVERITY_BY_ID.get(str(audit_id or "").strip())
    if known:
        # a curated audit that only just failed is not still "critical"
        try:
            if float(score) >= 0.5 and SEVERITY_RANK[known] < SEVERITY_RANK["moderate"]:
                return "moderate"
        except (TypeError, ValueError):
            pass
        return known
    return _severity_from_lh_score(score)


def _severity_from_lh_score(score):
    """Lighthouse audit score -> shared severity scale."""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "moderate"
    if s <= 0:
        return "critical"
    if s < 0.5:
        return "serious"
    if s < 0.9:
        return "moderate"
    return "minor"


def _plain(text, limit=None):
    """Collapse whitespace and strip the markdown links Lighthouse embeds in
    its descriptions, so the summary text reads as prose."""
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1", str(text or ""))
    t = re.sub(r"\s+", " ", t).strip()
    if limit and len(t) > limit:
        cut = t[:limit].rsplit(" ", 1)[0]
        t = cut + "..."
    return t


# --------------------------------------------------------------------------
# issue normalization
# --------------------------------------------------------------------------

def make_issue(issue_id, title, category, severity, fix, *, detail="",
               pages=None, page_count=None, elements=0, evidence=None,
               help_url="", metric="", site_wide=False, standards=None):
    pages = sorted({path_of(p) for p in (pages or [])})
    severity = severity if severity in SEVERITY_RANK else "moderate"
    effort = effort_for(issue_id, category)
    count = page_count if page_count is not None else len(pages)
    return {
        "id": str(issue_id or slug(title, "issue")),
        "title": _plain(title) or str(issue_id),
        "category": category,
        "category_label": CATEGORY_LABEL.get(category, category.title()),
        "severity": severity,
        "severity_label": SEVERITY_LABEL[severity],
        "severity_rank": SEVERITY_RANK[severity],
        "effort": effort,
        "effort_label": EFFORT_LABEL[effort],
        "effort_hint": EFFORT_HINT[effort],
        "fix": _plain(fix) or "Review this finding against the linked reference.",
        "detail": _plain(detail),
        "pages": pages,
        "page_count": count,
        "elements": int(elements or 0),
        "evidence": list(evidence or []),
        "help_url": help_url or "",
        "metric": metric or "",
        "site_wide": bool(site_wide),
        "standards": list(standards or []),
    }


def priority_of(issue, total_pages):
    """Ordering weight inside a severity band: how much of the site a finding
    touches, against how much work the fix is. Ordering only - never a score
    shown as a measurement."""
    sev = SEVERITY_WEIGHT[issue["severity"]]
    if issue["site_wide"] or not total_pages:
        reach = 1.0
    else:
        reach = min(1.0, issue["page_count"] / float(total_pages))
    return round(sev * (0.55 + 0.45 * reach) / EFFORT_COST[issue["effort"]], 4)


def scope_label(issue, total_pages):
    if issue["site_wide"]:
        return "Site-wide"
    n = issue["page_count"]
    if not n:
        return "Site-wide"
    if total_pages and n >= total_pages:
        return f"All {total_pages} audited pages"
    if total_pages:
        return f"{n} of {total_pages} pages"
    return f"{n} page{'s' if n != 1 else ''}"


# --------------------------------------------------------------------------
# source adapters: Lighthouse
# --------------------------------------------------------------------------

def issues_from_pages(pages, categories=None):
    """Fold per-page Lighthouse audits into one issue per audit, remembering
    which pages it fired on. Audits carry their category when the loader could
    resolve one; the rest land in Best Practices, which is where Lighthouse
    itself files the general hygiene audits."""
    wanted = set(categories) if categories else None
    bucket = {}
    for page in pages:
        page_path = path_of(page.get("path"))
        for audit in page.get("issues") or []:
            aid = audit.get("id") or slug(audit.get("title"), "audit")
            cat = audit.get("category") or "best-practices"
            if cat not in CATEGORY_LABEL:
                cat = "best-practices"
            if wanted is not None and cat not in wanted:
                continue
            entry = bucket.get((aid, cat))
            if entry is None:
                entry = bucket[(aid, cat)] = {
                    "id": aid,
                    "title": audit.get("title") or aid,
                    "category": cat,
                    "description": audit.get("description") or "",
                    "score": audit.get("score"),
                    "pages": set(),
                    "values": [],
                }
            entry["pages"].add(page_path)
            if audit.get("displayValue"):
                entry["values"].append(str(audit["displayValue"]))
            # keep the worst score seen across pages
            try:
                if float(audit.get("score")) < float(entry["score"]):
                    entry["score"] = audit.get("score")
            except (TypeError, ValueError):
                pass

    issues = []
    for entry in bucket.values():
        worst = _plain(entry["values"][0]) if entry["values"] else ""
        issues.append(make_issue(
            entry["id"], entry["title"], entry["category"],
            severity_for_audit(entry["id"], entry["score"]),
            fix=entry["description"] or entry["title"],
            detail=entry["description"],
            pages=entry["pages"],
            metric=worst,
            evidence=[{"label": "Reported", "value": v} for v in sorted(set(entry["values"]))[:6]],
        ))
    return issues


def category_scores_from_pages(pages, categories=None):
    """Mean of the per-page Lighthouse scores, exactly as the scanner reported
    them - no re-weighting, pages with no score for a category are skipped."""
    out = {}
    for key in LH_CATEGORY_KEYS:
        if categories and key not in categories:
            continue
        values = [p["scores"].get(key) for p in pages
                  if isinstance(p.get("scores"), dict)]
        values = [v for v in values if isinstance(v, (int, float))]
        out[key] = pct(sum(values) / len(values)) if values else None
    return out


# --------------------------------------------------------------------------
# source adapters: accessibility (axe) and security
# --------------------------------------------------------------------------

def issues_from_accessibility(a11y):
    """axe violations -> issues. `a11y` is accessibility_scan.collect_site()'s
    result; impacts and fixes come straight from that module."""
    if not a11y:
        return []
    try:
        import accessibility_scan as a11y_mod
        fix_for = a11y_mod.fix_for
        sc_label = a11y_mod.sc_label
    except Exception:                                   # pragma: no cover
        fix_for = lambda f: f.get("help") or ""         # noqa: E731
        sc_label = lambda s: s                          # noqa: E731

    # which selected standards each failing rule breaks
    standards_by_rule = {}
    for row in a11y.get("panel") or []:
        if row.get("verdict") != "fail":
            continue
        for rid in row.get("rule_ids") or []:
            standards_by_rule.setdefault(rid, []).append(row.get("name") or row.get("id"))

    issues = []
    for finding in a11y.get("violations") or []:
        pages = finding.get("pages") or {}
        evidence = []
        for url, elements in list(pages.items())[:4]:
            for el in (elements or [])[:2]:
                evidence.append({
                    "label": path_of(url),
                    "value": el.get("target") or "",
                    "code": (el.get("html") or "")[:220],
                })
        sc = ", ".join(sc_label(s) for s in finding.get("sc") or [])
        issues.append(make_issue(
            finding.get("id"), finding.get("help") or finding.get("id"),
            "wcag", finding.get("impact") or "moderate",
            fix=fix_for(finding),
            detail=finding.get("description") or "",
            pages=pages.keys(),
            elements=finding.get("nodes", 0),
            evidence=evidence,
            help_url=finding.get("helpUrl") or "",
            metric=(f"{finding.get('nodes', 0)} element(s)"
                    if finding.get("nodes") else ""),
            standards=standards_by_rule.get(finding.get("id"), []),
        ))
        if sc:
            issues[-1]["success_criteria"] = sc
    return issues


def issues_from_security(security):
    """Passive security findings -> issues. `security` is
    security_scan.collect_site()'s result; RULES owns titles and severities."""
    if not security:
        return []
    try:
        import security_scan as sec
        rules = sec.RULES
    except Exception:                                   # pragma: no cover
        rules = {}

    issues = []
    for rule_id, where in (security.get("hits") or {}).items():
        if rule_id not in rules:
            continue
        title, sev, rec = rules[rule_id]
        where = set(where or [])
        site_wide = "__site__" in where
        pages = [w for w in where if w != "__site__"]
        issues.append(make_issue(
            rule_id, title, "security",
            SECURITY_SEVERITY_MAP.get(sev, "moderate"),
            fix=rec,
            pages=pages,
            site_wide=site_wide and not pages,
        ))
        issues[-1]["source_severity"] = sev
    return issues


def security_score(issues):
    """Findings -> a 0-100 figure so security sits on the same scorecard.
    Deductions are fixed per severity; stated on the methodology page."""
    if issues is None:
        return None
    deduction = {"critical": 30, "serious": 15, "moderate": 7, "minor": 2}
    total = sum(deduction[i["severity"]] for i in issues)
    return max(0, 100 - total)


def wcag_score(a11y):
    """Share of the selected, applicable standards that pass automated checks.
    "Not determined" (the scan never ran) yields no score rather than a green."""
    if not a11y:
        return None
    panel = a11y.get("panel") or []
    verdicts = [row.get("verdict") for row in panel if row.get("verdict") != "n/a"]
    if not verdicts or any(v == "unknown" for v in verdicts):
        return None
    passed = sum(1 for v in verdicts if v == "pass")
    return int(round(100 * passed / len(verdicts)))


def merge_duplicate_accessibility(lh_issues, axe_issues):
    """Lighthouse's accessibility category is axe-core underneath, so the same
    rule arrives twice - once per tool, under the same id. Reporting it twice
    double-counts the work and inflates the severity histogram.

    The axe record wins (it carries the failing elements, the success criteria
    and the standards the rule breaks); the pages Lighthouse saw are folded
    into it so no coverage is lost, and the Lighthouse copy is dropped.
    """
    by_id = {i["id"]: i for i in axe_issues}
    kept = []
    for issue in lh_issues:
        twin = by_id.get(issue["id"]) if issue["category"] == "accessibility" else None
        if twin is None:
            kept.append(issue)
            continue
        merged = sorted(set(twin["pages"]) | set(issue["pages"]))
        twin["pages"] = merged
        twin["page_count"] = len(merged)
        twin.setdefault("also_reported_by", []).append("Lighthouse")
    return kept


# --------------------------------------------------------------------------
# roll-ups
# --------------------------------------------------------------------------

def health_score(scores):
    """Weighted mean of the category scores that were actually produced."""
    parts = [(CATEGORY_WEIGHT[k], v) for k, v in scores.items()
             if v is not None and k in CATEGORY_WEIGHT]
    if not parts:
        return None
    total_weight = sum(w for w, _ in parts)
    return int(round(sum(w * v for w, v in parts) / total_weight))


def health_band(score):
    if score is None:
        return HEALTH_BANDS[-1][1], "Not determined", (
            "No category produced a score, so no overall verdict can be given.")
    for floor, key, label, summary in HEALTH_BANDS:
        if score >= floor:
            return key, label, summary
    return HEALTH_BANDS[-1][1], HEALTH_BANDS[-1][2], HEALTH_BANDS[-1][3]


def severity_distribution(issues):
    dist = {s: 0 for s in SEVERITIES}
    for i in issues:
        dist[i["severity"]] += 1
    return dist


def build_heatmap(pages, category_keys, page_issue_index=None):
    """pages x categories grid. Lighthouse categories use the page's own
    score; the two finding-based categories use the count of issues touching
    that page, so a reader can see where problems cluster."""
    cols = [k for k in category_keys if k in CATEGORY_LABEL]
    rows = []
    for page in pages:
        cells = []
        for key in cols:
            if key in LH_CATEGORY_KEYS:
                value = pct((page.get("scores") or {}).get(key))
                cells.append({"key": key, "kind": "score", "value": value,
                              "band": score_band(value),
                              "text": "-" if value is None else str(value)})
            else:
                found = (page_issue_index or {}).get(key, {}).get(path_of(page.get("path")), 0)
                band = "good" if found == 0 else "avg" if found <= 2 else "poor"
                cells.append({"key": key, "kind": "count", "value": found,
                              "band": band, "text": str(found)})
        rows.append({"path": path_of(page.get("path")),
                     "device": page.get("device") or "",
                     "cells": cells})
    return {"cols": [{"key": k, "label": CATEGORY_LABEL[k]} for k in cols], "rows": rows}


def _page_issue_index(issues, keys):
    """category -> page path -> number of issues, for the heatmap."""
    index = {k: {} for k in keys}
    for issue in issues:
        if issue["category"] not in index:
            continue
        for p in issue["pages"]:
            index[issue["category"]][p] = index[issue["category"]].get(p, 0) + 1
    return index


# --------------------------------------------------------------------------
# executive summary
# --------------------------------------------------------------------------

VERDICT_SENTENCE = {
    "performance": {
        "good": "Pages load quickly for visitors.",
        "avg": "Pages are slower than visitors expect on at least some templates.",
        "poor": "Pages are slow enough that visitors are likely leaving before they load.",
    },
    "accessibility": {
        "good": "Automated accessibility checks are largely clean.",
        "avg": "Some visitors using assistive technology will hit friction.",
        "poor": "Parts of the site are unusable with a screen reader or keyboard.",
    },
    "seo": {
        "good": "Search engines can crawl and understand the pages.",
        "avg": "Some pages are missing the basics search engines rely on.",
        "poor": "Search visibility is being held back by fixable technical gaps.",
    },
    "best-practices": {
        "good": "The site follows current web platform conventions.",
        "avg": "A few outdated or unsafe patterns are still in use.",
        "poor": "The site relies on patterns browsers now warn about or block.",
    },
    "security": {
        "good": "The public-facing security posture is solid.",
        "avg": "Standard hardening headers or cookie flags are missing.",
        "poor": "Visitors and their sessions are exposed to avoidable risk.",
    },
    "wcag": {
        "good": "The selected standards pass every automated check we can run.",
        "avg": "The selected standards fail on a small number of automated checks.",
        "poor": "The selected standards fail on automated checks across the site.",
    },
}


def build_executive(scores, issues, total_pages, health, band_label, band_summary):
    """Plain-language read-out: one line per category, the handful of issues
    that matter most, and what the whole thing means in business terms."""
    verdicts = []
    for key in CATEGORY_ORDER:
        if key not in scores:
            continue
        value = scores[key]
        band = score_band(value)
        verdicts.append({
            "key": key,
            "label": CATEGORY_LABEL[key],
            "score": value,
            "band": band,
            "verdict": BAND_VERDICT[band],
            "sentence": VERDICT_SENTENCE.get(key, {}).get(band, CATEGORY_BLURB[key]),
        })

    top = [i for i in issues if i["severity"] in ("critical", "serious")][:5]
    if not top:
        top = issues[:3]

    counts = severity_distribution(issues)
    blocking = counts["critical"] + counts["serious"]
    quick_wins = sum(1 for i in issues
                     if i["effort"] == "quick" and i["severity"] in ("critical", "serious"))

    total = len(issues)
    finding_word = "finding" if total == 1 else "findings"
    if not issues:
        meaning = ("Nothing actionable came out of this scan. The automated checks "
                   "found no failing audits on the pages covered.")
    elif blocking == 0:
        meaning = (f"None of the {total} {finding_word} are severe. They are worth "
                   f"clearing during normal maintenance rather than as a project.")
    else:
        scale = ("The single finding is" if total == 1 else
                 f"All {total} findings are" if blocking == total else
                 f"{blocking} of the {total} findings are")
        meaning = f"{scale} severe enough to affect visitors today"
        if quick_wins:
            meaning += (f", and {quick_wins} of those are configuration-level "
                        f"{'fix' if quick_wins == 1 else 'fixes'} that can be done immediately")
        meaning += ". Working the list below in order clears the highest-impact items first."

    return {
        "verdicts": verdicts,
        "top_issues": top,
        "counts": counts,
        "blocking": blocking,
        "quick_wins": quick_wins,
        "health": health,
        "band_label": band_label,
        "band_summary": band_summary,
        "what_this_means": meaning,
    }


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------

RUN_STATUS_LABEL = {
    "complete": "Complete",
    "partial": "Partial coverage",
    "failed": "Failed",
}

RUN_STATUS_BLURB = {
    "complete": "Every page this run set out to measure was measured.",
    "partial": "Some pages could not be measured. They are unmeasured, not clean - "
               "nothing below speaks for them.",
    "failed": "No page could be measured. Nothing below is a result.",
}


def normalize_coverage(coverage):
    """The run's coverage record, in the shape the report renders.

    Counts are copied, never recomputed, and no score anywhere in the report is
    derived from them. Returns None when a run reported nothing (an older run
    folder, or a caller that does not track coverage).
    """
    if not isinstance(coverage, dict):
        return None
    attempted = int(coverage.get("pages_attempted") or 0)
    ok = int(coverage.get("pages_ok") or 0)
    status = str(coverage.get("status") or "").lower()
    if status not in RUN_STATUS_LABEL:
        status = "complete" if attempted and ok == attempted else "partial"
    percent = coverage.get("percent")
    if not isinstance(percent, int):
        percent = int(round(100.0 * ok / attempted)) if attempted else 0
    failures = [f for f in (coverage.get("failures") or []) if isinstance(f, dict)]
    return {
        "status": status,
        "label": RUN_STATUS_LABEL[status],
        "blurb": RUN_STATUS_BLURB[status],
        "pages_ok": ok,
        "pages_attempted": attempted,
        "pages_failed": max(0, attempted - ok),
        "percent": percent,
        "sentence": (f"{ok} of {attempted} page(s) measured ({percent}%)"
                     if attempted else "no pages were measured"),
        "notes": [str(n) for n in (coverage.get("notes") or [])],
        "failures": [{"url": str(f.get("url", "")),
                      "status": str(f.get("status", "error")),
                      "reason": str(f.get("reason", ""))} for f in failures],
        "notice": coverage.get("notice") or None,
    }


DEFAULT_TOOLS = [
    {"name": "Unlighthouse", "version": "", "role": "site crawl and scoring"},
    {"name": "Google Lighthouse", "version": "", "role": "per-page audits"},
]


def build_report(pages=None, *, site_url="", device="", roots=None,
                 categories=None, accessibility=None, security=None,
                 brand=None, generated=None, scope=None, tools=None,
                 report_href=None, standards=None, coverage=None):
    """Assemble the whole report structure.

    pages          - consolidate_report.merge() output (may be empty)
    categories     - Lighthouse categories the run selected (None = all four)
    accessibility  - accessibility_scan.collect_site() result, or None
    security       - security_scan.collect_site() result, or None
    report_href    - callable(page) -> relative link to the full Lighthouse
                     report, so the appendix keeps the existing links
    coverage       - the run's own record of which pages were measured and
                     which were not (runstate.RunState.report_coverage()).
                     It is reported verbatim: nothing here scores it, and no
                     number in this report is derived from it.
    """
    pages = list(pages or [])
    lh_categories = list(categories) if categories else (list(LH_CATEGORY_KEYS) if pages else [])
    generated = generated or datetime.now()

    scores = category_scores_from_pages(pages, lh_categories)

    lh_issues = issues_from_pages(pages, lh_categories)
    a11y_issues = issues_from_accessibility(accessibility)
    sec_issues = issues_from_security(security)
    if a11y_issues:
        lh_issues = merge_duplicate_accessibility(lh_issues, a11y_issues)
    issues = lh_issues + a11y_issues + sec_issues

    if security is not None:
        scores["security"] = security_score(sec_issues)
    if accessibility is not None:
        scores["wcag"] = wcag_score(accessibility)

    total_pages = len(pages) or (accessibility or {}).get("pages_analyzed") \
        or (security or {}).get("scanned") or 0

    for issue in issues:
        issue["priority"] = priority_of(issue, total_pages)
        issue["scope_label"] = scope_label(issue, total_pages)
    # Severity leads: a client reading "fix these first" expects the critical
    # items at the top, not a cheap site-wide win. Reach and effort then order
    # the work inside each severity band.
    issues.sort(key=lambda i: (i["severity_rank"], -i["priority"],
                               -i["page_count"], i["title"]))
    for n, issue in enumerate(issues, 1):
        issue["rank"] = n
        issue["anchor"] = f"issue-{n}-{slug(issue['id'], 'i')}"

    health = health_score(scores)
    band_key, band_label, band_summary = health_band(health)

    active_categories = [k for k in CATEGORY_ORDER if k in scores]
    index = _page_issue_index(issues, ["security", "wcag"])

    per_page = []
    for page in pages:
        path = path_of(page.get("path"))
        page_issues = [i for i in issues if path in i["pages"]]
        per_page.append({
            "path": path,
            "url": page.get("path"),
            "device": page.get("device") or "",
            "anchor": "page-" + slug(f"{path}-{page.get('device') or ''}", "p"),
            "scores": {k: pct((page.get("scores") or {}).get(k)) for k in LH_CATEGORY_KEYS},
            "metrics": page.get("metrics") or {},
            "issue_count": len(page_issues),
            "severity_counts": severity_distribution(page_issues),
            "top_issues": page_issues[:6],
            "report_href": report_href(page) if report_href else None,
            "audited": page.get("issues") is not None,
        })

    deep_dives = []
    for key in active_categories:
        cat_issues = [i for i in issues if i["category"] == key]
        deep_dives.append({
            "key": key,
            "label": CATEGORY_LABEL[key],
            "blurb": CATEGORY_BLURB[key],
            "score": scores.get(key),
            "band": score_band(scores.get(key)),
            "verdict": BAND_VERDICT[score_band(scores.get(key))],
            "issues": cat_issues,
            "counts": severity_distribution(cat_issues),
            "anchor": slug(key, "cat"),
        })

    host = urlparse(site_url).netloc or site_url
    scope = dict(scope or {})
    scope.setdefault("pages_reported", len(pages))
    if accessibility:
        scope.setdefault("pages_accessibility", accessibility.get("pages_analyzed", 0))
    if security:
        scope.setdefault("pages_security", security.get("scanned", 0))

    coverage = normalize_coverage(coverage)
    if coverage:
        # Stated in the scope table too, so a partial run cannot be read as a
        # clean one from any page of the report.
        scope.setdefault("pages_measured", coverage["pages_ok"])
        scope.setdefault("pages_attempted", coverage["pages_attempted"])
        scope.setdefault("coverage", f"{coverage['percent']}%")
        scope.setdefault("run_status", coverage["status"])

    return {
        "meta": {
            "site_url": site_url,
            "host": host,
            "generated": generated,
            "generated_text": generated.strftime("%d %B %Y"),
            "generated_full": generated.strftime("%Y-%m-%d %H:%M"),
            "device": (device or "").title(),
            "scope": scope,
            "roots": [str(r) for r in (roots or [])],
            "categories": active_categories,
            "lh_categories": lh_categories,
            "standards": list(standards or []),
            "tools": list(tools or DEFAULT_TOOLS),
            "coverage": coverage,
        },
        "brand": brand,
        "scores": scores,
        "health": {
            "score": health,
            "band": band_key,
            "label": band_label,
            "summary": band_summary,
        },
        "categories": [
            {"key": k, "label": CATEGORY_LABEL[k], "blurb": CATEGORY_BLURB[k],
             "score": scores.get(k), "band": score_band(scores.get(k)),
             "verdict": BAND_VERDICT[score_band(scores.get(k))],
             "weight": CATEGORY_WEIGHT[k]}
            for k in active_categories
        ],
        "issues": issues,
        "severity_distribution": severity_distribution(issues),
        "effort_distribution": {e: sum(1 for i in issues if i["effort"] == e) for e in EFFORTS},
        "pages": per_page,
        "heatmap": build_heatmap(pages, active_categories, index),
        "deep_dives": deep_dives,
        "executive": build_executive(scores, issues, total_pages, health,
                                     band_label, band_summary),
        "accessibility": accessibility,
        "security": security,
        "totals": {
            "pages": len(pages),
            "issues": len(issues),
            "critical": severity_distribution(issues)["critical"],
        },
    }
