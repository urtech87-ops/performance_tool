#!/usr/bin/env python3
"""
Renders the report structure from report_model into one HTML document.

One document serves both outputs. The browser shows the interactive version
(sticky section nav, severity filters, collapsible detail); paged.js turns the
same file into the paginated PDF, because it keeps `@media print` rules and
drops `@media screen` ones. Nothing here is written twice for the two media.

Document order is the narrative, not the data dump it replaces:

    cover -> executive summary -> scorecard -> what to fix first
          -> per-category deep dives -> per-page appendix -> methodology
"""

import html

import report_charts as charts
from report_brand import Brand, load_brand
from report_css import stylesheet
from report_model import (
    CATEGORY_LABEL, EFFORTS, EFFORT_LABEL, SEVERITIES, SEVERITY_LABEL, score_band,
)

E = html.escape


def esc(value):
    return E(str(value if value is not None else ""))


def _n(value, dash="-"):
    return dash if value is None else str(value)


# --------------------------------------------------------------------------
# small building blocks
# --------------------------------------------------------------------------

def _logo(src, name, monogram_text, role):
    if src:
        mark = f'<img src="{esc(src)}" alt="{esc(name)}">'
    else:
        mark = f'<div class="logo-mark">{esc(monogram_text)}</div>'
    label = (f'<div><div class="logo-role">{esc(role)}</div>'
             f'<div class="logo-name">{esc(name)}</div></div>') if not src else ""
    return f'<div class="logo-slot">{mark}{label}</div>'


def _fact(key, value):
    return (f'<div class="fact"><div class="k">{esc(key)}</div>'
            f'<div class="v">{esc(value)}</div></div>')


def _section_head(number, title, subtitle="", extra=""):
    sub = f"<p>{esc(subtitle)}</p>" if subtitle else ""
    return (f'<div class="section-head"><div class="grow">'
            f'<div class="num">{esc(number)}</div><h2>{esc(title)}</h2>{sub}</div>{extra}</div>')


def _sev_tag(issue):
    return (f'<span class="tag sev {issue["severity"]}">'
            f'{esc(issue["severity_label"])}</span>')


def _effort_tag(issue):
    return (f'<span class="tag effort" title="{esc(issue["effort_hint"])}">'
            f'{charts.effort_pips(issue["effort"])}{esc(issue["effort_label"])}</span>')


def _evidence_html(issue):
    if not issue["evidence"]:
        return ""
    only = issue["evidence"][0]
    if (len(issue["evidence"]) == 1 and not only.get("code")
            and str(only.get("value", "")) == issue["metric"]):
        return ""                       # already stated on the meta line
    items = []
    for ev in issue["evidence"][:4]:
        label = esc(ev.get("label", ""))
        value = esc(ev.get("value", ""))
        code = ev.get("code")
        body = f"<b>{label}</b>" if label else ""
        if value:
            body += f" <code>{value}</code>"
        if code:
            body += f"<pre>{esc(code)}</pre>"
        items.append(f"<li>{body}</li>")
    return f'<ul class="evidence">{"".join(items)}</ul>'


def _pages_html(issue, limit=6):
    if issue["site_wide"] or not issue["pages"]:
        return ""
    shown = "".join(f"<code>{esc(p)}</code>" for p in issue["pages"][:limit])
    more = (f'<span class="chip">+{len(issue["pages"]) - limit} more</span>'
            if len(issue["pages"]) > limit else "")
    return f'<div class="pagelist">{shown}{more}</div>'


def _action_card(issue, show_evidence=True):
    """One entry in the prioritized list: what, how bad, how widespread, how
    much work, and the fix - in that reading order."""
    meta = [f'<b>{esc(issue["scope_label"])}</b>']
    if issue["elements"]:
        meta.append(f'{issue["elements"]} element(s)')
    if issue["metric"]:
        meta.append(esc(issue["metric"]))
    if issue.get("success_criteria"):
        meta.append(f'WCAG {esc(issue["success_criteria"])}')
    if issue["standards"]:
        meta.append("Breaks: " + esc(", ".join(issue["standards"][:3])))

    link = (f' <a class="linkout" href="{esc(issue["help_url"])}" target="_blank" '
            f'rel="noopener">Reference &#8599;</a>' if issue["help_url"] else "")

    return (
        f'<article class="action {issue["severity"]} anchor-target" id="{esc(issue["anchor"])}" '
        f'data-severity="{issue["severity"]}" data-category="{issue["category"]}" '
        f'data-effort="{issue["effort"]}">'
        f'<div class="rank">{issue["rank"]}</div>'
        f'<div><div class="action-head"><h3>{esc(issue["title"])}</h3>'
        f'{_sev_tag(issue)}<span class="tag cat">{esc(issue["category_label"])}</span>'
        f'{_effort_tag(issue)}</div>'
        f'<div class="meta">{" &middot; ".join(meta)}</div>'
        f'<div class="fix"><b>Fix:</b> {esc(issue["fix"])}{link}</div>'
        f'{_pages_html(issue)}'
        f'{_evidence_html(issue) if show_evidence else ""}'
        f'</div></article>')


# --------------------------------------------------------------------------
# 0. cover
# --------------------------------------------------------------------------

def _cover_flag(coverage):
    """The one line that stops a partial run reading as a clean one.

    Printed on the cover, above everything else the report says, whenever the
    run did not measure every page it set out to.
    """
    if not coverage or coverage["status"] == "complete":
        return ""
    return (f'<div class="cover-flag"><b>{esc(coverage["label"])}</b> &mdash; '
            f'{esc(coverage["sentence"])}. {esc(coverage["blurb"])}</div>')


def render_cover(report, brand, running=""):
    meta = report["meta"]
    health = report["health"]
    scope = meta["scope"]

    client = (_logo(brand.client_logo, brand.client_name, brand.client_monogram, "Prepared for")
              if brand.client_name or brand.client_logo else "")

    coverage = meta.get("coverage")
    facts = [
        _fact("Report date", meta["generated_text"]),
        _fact("Device profile", meta["device"] or "Default"),
        _fact("Pages covered", scope.get("pages_reported", 0) or scope.get("pages_crawled", 0)),
        _fact("Audit scope", ", ".join(CATEGORY_LABEL[c] for c in meta["categories"]) or "-"),
    ]
    if coverage:
        facts.append(_fact("Run status",
                           f'{coverage["label"]} - {coverage["sentence"]}'))

    subtitle = brand.report_subtitle or ""
    return (
        f'<header class="cover">{running}'
        f'<div class="cover-top">'
        f'{_logo(brand.agency_logo, brand.agency_name, brand.agency_monogram, "Prepared by")}'
        f'{client}</div>'
        f'<div>'
        f'<div class="eyebrow" style="color:var(--on-dark-2)">{esc(subtitle)}</div>'
        f'<h1>{esc(brand.report_title)}</h1>'
        f'<div class="site">{esc(meta["site_url"] or meta["host"])}</div>'
        f'{_cover_flag(coverage)}'
        f'<div class="cover-body">'
        f'<div><p class="lede" style="color:var(--on-dark)">{esc(health["summary"])}</p>'
        f'<div class="cover-facts">{"".join(facts)}</div></div>'
        f'<div class="cover-score">{charts.health_gauge(health["score"], "Overall health")}'
        f'<div class="band">{esc(health["label"])}</div>'
        f'<div class="cap">Overall health</div></div>'
        f'</div></div>'
        f'<div class="cover-foot"><span>{esc(brand.footer_note or brand.agency_name)}</span>'
        f'<span>{esc(brand.contact or brand.agency_url)}</span></div>'
        f'</header>')


# --------------------------------------------------------------------------
# 1. executive summary
# --------------------------------------------------------------------------

def render_executive(report):
    ex = report["executive"]
    counts = ex["counts"]

    verdicts = "".join(
        f'<div class="verdict-row"><div class="cat">{esc(v["label"])}</div>'
        f'<div><span class="pill {v["band"]}">{_n(v["score"])}'
        f'{"" if v["score"] is None else "/100"}</span></div>'
        f'<div>{esc(v["sentence"])}</div></div>'
        for v in ex["verdicts"]) or '<p class="muted">No category produced a score.</p>'

    kpis = "".join([
        f'<div class="kpi"><b>{report["totals"]["issues"]}</b><span>Findings</span></div>',
        f'<div class="kpi critical"><b>{counts["critical"]}</b><span>Critical</span></div>',
        f'<div class="kpi serious"><b>{counts["serious"]}</b><span>Serious</span></div>',
        f'<div class="kpi quick"><b>{report["effort_distribution"]["quick"]}</b>'
        f'<span>Quick wins</span></div>',
        f'<div class="kpi"><b>{report["totals"]["pages"]}</b><span>Pages</span></div>',
    ])

    if ex["top_issues"]:
        top = "".join(
            f'<li><span class="tag sev {i["severity"]}">{esc(i["severity_label"])}</span> '
            f'<a class="tolist" href="#{esc(i["anchor"])}">{esc(i["title"])}</a> '
            f'<span class="muted">&mdash; {esc(i["scope_label"])}, '
            f'{esc(i["effort_label"].lower())}</span></li>'
            for i in ex["top_issues"])
        top_html = (f'<h4 class="card-title">The issues that matter most</h4>'
                    f'<ul class="evidence" style="font-size:var(--t-base);color:var(--ink)">'
                    f'{top}</ul>')
    else:
        top_html = ('<h4 class="card-title">The issues that matter most</h4>'
                    '<p class="muted">The automated checks found nothing to fix on the '
                    'pages covered.</p>')

    return (
        f'<section class="section" id="summary">'
        f'{_section_head("01", "Executive summary", "A plain-language read of the audit, written for a decision maker rather than a developer.")}'
        f'<div class="callout" style="margin-bottom:var(--sp-4)">'
        f'<h4>What this means</h4><p style="margin:0">{esc(ex["what_this_means"])}</p></div>'
        f'<div class="grid g-2" style="align-items:start">'
        f'<div class="card"><div class="eyebrow">Where the site stands</div>'
        f'<p class="lede">{esc(report["health"]["summary"])}</p>'
        f'<div class="kpis" style="margin-top:var(--sp-4)">{kpis}</div>'
        f'<div style="margin-top:var(--sp-4)">'
        f'{charts.severity_bar(report["severity_distribution"], uid="exec")}'
        f'{_severity_legend(report["severity_distribution"])}</div></div>'
        f'<div class="card">{top_html}</div>'
        f'</div>'
        f'<div class="card flat allow-break" style="margin-top:var(--sp-4)">'
        f'<h4 class="card-title keep-with-next">Verdict by category</h4>{verdicts}</div>'
        f'</section>')


def _severity_legend(distribution=None):
    """The bar's key. When counts are passed they are shown here rather than
    inside the bar: the bar is stretched to its container width, and text drawn
    in it would be squashed with it."""
    keys = []
    for sev in SEVERITIES:
        count = "" if distribution is None else f' <b>{distribution.get(sev, 0)}</b>'
        keys.append(f'<span class="k"><span class="swatch" '
                    f'style="background:var(--sev-{sev})"></span>'
                    f'{esc(SEVERITY_LABEL[sev])}{count}</span>')
    return f'<div class="legend">{"".join(keys)}</div>'


# --------------------------------------------------------------------------
# 2. scorecard
# --------------------------------------------------------------------------

def render_scorecard(report):
    tiles = []
    for cat in report["categories"]:
        tiles.append(
            f'<div class="score-tile {cat["band"]}">'
            f'{charts.score_ring(cat["score"], sublabel=cat["label"])}'
            f'<div class="name">{esc(cat["label"])}</div>'
            f'<div class="verdict">{esc(cat["verdict"])}</div>'
            f'<div class="blurb">{esc(cat["blurb"])}</div></div>')
    if not tiles:
        tiles.append('<p class="muted">No categories were scored in this run.</p>')

    heat = charts.heatmap(report["heatmap"])
    heat_block = ""
    if heat:
        heat_block = (
            f'<div class="card" style="margin-top:var(--sp-5)">'
            f'<h4 class="card-title">Where the problems are</h4>'
            f'<p class="muted" style="font-size:var(--t-sm)">Every audited page against every '
            f'category. Lighthouse columns show the page score; Security and WCAG columns show '
            f'how many findings touch that page.</p>{heat}'
            f'<div class="legend">'
            f'<span class="k"><span class="swatch" style="background:var(--good)"></span>'
            f'Good (90-100 / no findings)</span>'
            f'<span class="k"><span class="swatch" style="background:var(--avg)"></span>'
            f'Needs work (50-89 / 1-2 findings)</span>'
            f'<span class="k"><span class="swatch" style="background:var(--poor)"></span>'
            f'Poor (0-49 / 3+ findings)</span>'
            f'<span class="k"><span class="swatch" style="background:var(--na);opacity:.4">'
            f'</span>Not measured</span></div></div>')

    gauge = (f'<div class="aside" style="text-align:center">'
             f'{charts.health_gauge(report["health"]["score"], "Overall health", width=150)}'
             f'<div style="font-weight:800">{esc(report["health"]["label"])}</div>'
             f'<div class="muted" style="font-size:var(--t-xs);letter-spacing:.08em;'
             f'text-transform:uppercase">Overall health</div></div>')

    return (
        f'<section class="section" id="scorecard">'
        f'{_section_head("02", "Scorecard", "Each category scored 0-100. Green is 90 and above, amber 50-89, red below 50.", gauge)}'
        f'<div class="scorecard">{"".join(tiles)}</div>'
        f'{heat_block}</section>')


# --------------------------------------------------------------------------
# 3. prioritized actions
# --------------------------------------------------------------------------

def render_actions(report, top_n=12):
    issues = report["issues"]
    if not issues:
        return (f'<section class="section" id="actions">'
                f'{_section_head("03", "What to fix first", "Ranked by severity, reach and effort.")}'
                f'<div class="card"><p style="margin:0">No action is required from this scan: '
                f'the automated checks found no failing audits on the pages covered.</p></div>'
                f'</section>')

    cats = sorted({i["category"] for i in issues}, key=lambda c: list(CATEGORY_LABEL).index(c))
    filters = (
        '<div class="toolbar screen-only"><span class="lbl">Severity</span>'
        + "".join(f'<button class="filter" data-filter="severity" data-value="{s}">'
                  f'{esc(SEVERITY_LABEL[s])}</button>' for s in SEVERITIES)
        + '<span class="lbl" style="margin-left:var(--sp-3)">Effort</span>'
        + "".join(f'<button class="filter" data-filter="effort" data-value="{e}">'
                  f'{esc(EFFORT_LABEL[e])}</button>' for e in EFFORTS)
        + '<span class="lbl" style="margin-left:var(--sp-3)">Category</span>'
        + "".join(f'<button class="filter" data-filter="category" data-value="{c}">'
                  f'{esc(CATEGORY_LABEL[c])}</button>' for c in cats)
        + '<button class="btn" id="clearFilters" style="margin-left:auto">Clear</button></div>')

    head = issues[:top_n]
    rest = issues[top_n:]

    rest_html = ""
    if rest:
        rows = "".join(
            f'<tr><td class="num">{i["rank"]}</td>'
            f'<td><a href="#{esc(i["anchor"])}">{esc(i["title"])}</a></td>'
            f'<td><span class="tag sev {i["severity"]}">{esc(i["severity_label"])}</span></td>'
            f'<td>{esc(i["category_label"])}</td>'
            f'<td>{esc(i["effort_label"])}</td>'
            f'<td class="num">{esc(i["scope_label"])}</td></tr>' for i in rest)
        rest_html = (
            f'<details class="expand allow-break" open style="margin-top:var(--sp-4)">'
            f'<summary>The remaining {len(rest)} finding'
            f'{"" if len(rest) == 1 else "s"}, in the same order</summary>'
            f'<div class="body"><table><thead><tr><th class="num">#</th><th>Finding</th>'
            f'<th>Severity</th><th>Category</th><th>Effort</th><th class="num">Reach</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></div></details>')

    counts = report["severity_distribution"]
    summary = (f'<div class="card flat" style="margin-bottom:var(--sp-4)">'
               f'<div class="grid g-2" style="align-items:center">'
               f'<div>{charts.severity_bar(counts, uid="actions")}'
               f'{_severity_legend(counts)}</div>'
               f'<div class="kpis">'
               f'<div class="kpi quick"><b>{report["effort_distribution"]["quick"]}</b>'
               f'<span>Quick wins</span></div>'
               f'<div class="kpi"><b>{report["effort_distribution"]["moderate"]}</b>'
               f'<span>Moderate</span></div>'
               f'<div class="kpi"><b>{report["effort_distribution"]["involved"]}</b>'
               f'<span>Involved</span></div></div></div></div>')

    return (
        f'<section class="section" id="actions">'
        f'{_section_head("03", "What to fix first", "Ranked by how severe each finding is, how much of the site it touches, and how much work the fix is. Work top-down.")}'
        f'{summary}{filters}'
        f'<div id="actionList">{"".join(_action_card(i) for i in head)}</div>'
        f'<p class="muted screen-only" id="noMatches" hidden>Nothing matches those filters.</p>'
        f'{rest_html}</section>')


# --------------------------------------------------------------------------
# 4. per-category deep dives
# --------------------------------------------------------------------------

def _standards_panel(report):
    a11y = report.get("accessibility") or {}
    panel = a11y.get("panel") or []
    if not panel:
        return ""
    try:
        from accessibility_scan import VERDICT_CLASS, VERDICT_LABEL
    except Exception:                                            # pragma: no cover
        VERDICT_CLASS, VERDICT_LABEL = {}, {}
    rows = []
    for row in panel:
        verdict = row.get("verdict", "unknown")
        cls = VERDICT_CLASS.get(verdict, "unk")
        label = VERDICT_LABEL.get(verdict, verdict.title())
        detail = (row.get("note") if verdict == "n/a" else
                  f'{row.get("rules", 0)} failing rule(s) across {row.get("pages", 0)} page(s)'
                  if verdict == "fail" else row.get("basis", ""))
        rows.append(
            f'<div class="std-row"><div><b>{esc(row.get("name"))}</b> '
            f'<span class="region">{esc(row.get("region", ""))} &middot; '
            f'{esc(row.get("basis", ""))}</span>'
            f'<div class="muted" style="font-size:var(--t-sm)">{esc(detail)}</div></div>'
            f'<div><span class="pill {cls}">{label}</span></div></div>')
    return (f'<div class="card" style="margin-bottom:var(--sp-4)">'
            f'<h4 class="card-title">Standards checked</h4>'
            f'<p class="muted" style="font-size:var(--t-sm)">Automated axe-core results per '
            f'framework. A pass means no automated check failed &mdash; it is not a legal '
            f'compliance verdict.</p>{"".join(rows)}</div>')


def render_deep_dives(report, per_category=8):
    blocks = []
    for n, dive in enumerate(report["deep_dives"], 1):
        issues = dive["issues"]
        header = (
            f'<div class="dive-head">'
            f'{charts.score_ring(dive["score"], size=84, sublabel=dive["label"])}'
            f'<div><h3>{esc(dive["label"])}</h3>'
            f'<p class="muted" style="margin:2px 0 0;font-size:var(--t-sm)">'
            f'{esc(dive["blurb"])}</p>'
            f'<p style="margin:var(--sp-2) 0 0">'
            f'<span class="pill {dive["band"]}">{esc(dive["verdict"])}</span> '
            f'<span class="muted">{len(issues)} finding'
            f'{"" if len(issues) == 1 else "s"}</span></p></div>'
            f'<div>{charts.severity_columns(dive["counts"])}</div></div>')

        extra = _standards_panel(report) if dive["key"] == "wcag" else ""
        if dive["key"] == "performance":
            extra += _metrics_block(report)

        if issues:
            body = "".join(_action_card(i) for i in issues[:per_category])
            if len(issues) > per_category:
                body += (f'<p class="muted">+{len(issues) - per_category} further '
                         f'{esc(dive["label"].lower())} findings are listed in '
                         f'<a href="#actions">what to fix first</a>.</p>')
        else:
            body = ('<div class="card"><p style="margin:0">No failing checks in this '
                    'category on the pages covered.</p></div>')

        blocks.append(f'<div class="dive" id="{esc(dive["anchor"])}">{header}{extra}{body}</div>')

    if not blocks:
        return ""
    return (f'<section class="section" id="detail">'
            f'{_section_head("04", "Category deep dives", "Every finding, grouped by the category it belongs to.")}'
            f'{"".join(blocks)}</section>')


def _metrics_block(report):
    """Core Web Vitals as the scanner measured them, averaged for a quick read
    and shown per page in the appendix."""
    try:
        from consolidate_report import METRIC_LABELS
    except Exception:                                            # pragma: no cover
        return ""
    source = next((p for p in report["pages"] if p.get("metrics")), None)
    if source is None:
        return ""
    tiles = []
    for key, label in METRIC_LABELS.items():
        metric = (source["metrics"] or {}).get(key)
        if not metric:
            continue
        score = metric.get("score")
        band = score_band(int(round(100 * score))) if isinstance(score, (int, float)) else "na"
        tiles.append(f'<div class="metric {band}"><span class="k">{esc(label)}</span>'
                     f'<span class="v">{esc(metric.get("display", "-"))}</span></div>')
    if not tiles:
        return ""
    return (f'<div class="card" style="margin-bottom:var(--sp-4)">'
            f'<h4 class="card-title">Core metrics &mdash; {esc(source["path"])}</h4>'
            f'<p class="muted" style="font-size:var(--t-sm);margin-bottom:var(--sp-2)">'
            f'The first page audited. Every page&rsquo;s own metrics are in the appendix.</p>'
            f'<div class="metrics">{"".join(tiles)}</div></div>')


# --------------------------------------------------------------------------
# 5. per-page appendix
# --------------------------------------------------------------------------

def render_appendix(report):
    pages = report["pages"]
    if not pages:
        return ""
    cat_keys = [k for k in report["meta"]["lh_categories"]]
    head_cells = "".join(
        f'<th class="num" title="{esc(CATEGORY_LABEL[k])}">{esc(charts.abbrev(CATEGORY_LABEL[k]))}'
        f'</th>' for k in cat_keys)

    rows = []
    for page in pages:
        cells = ""
        for key in cat_keys:
            value = page["scores"].get(key)
            cells += f'<td class="score {score_band(value)}">{_n(value)}</td>'
        link = (f'<a class="linkout" href="{esc(page["report_href"])}" target="_blank" '
                f'rel="noopener">Full report &#8599;</a>' if page["report_href"] else
                '<span class="muted">-</span>')
        device = f' <span class="chip">{esc(page["device"])}</span>' if page["device"] else ""
        rows.append(
            f'<tr><td class="path"><a href="#{esc(page["anchor"])}">{esc(page["path"])}</a>'
            f'{device}</td>{cells}'
            f'<td class="num">{page["issue_count"]}</td><td class="num">{link}</td></tr>')

    cards = []
    for page in pages:
        minis = "".join(
            f'<span class="mini {score_band(page["scores"].get(k))}">'
            f'<b>{_n(page["scores"].get(k))}</b> {esc(CATEGORY_LABEL[k])}</span>'
            for k in cat_keys)
        metrics = "".join(
            f'<div class="metric {score_band(None if m.get("score") is None else int(round(m["score"] * 100)))}">'
            f'<span class="k">{esc(_metric_label(k))}</span>'
            f'<span class="v">{esc(m.get("display", "-"))}</span></div>'
            for k, m in (page["metrics"] or {}).items())
        link = (f'<a class="linkout" href="{esc(page["report_href"])}" target="_blank" '
                f'rel="noopener">Open the full Lighthouse report &#8599;</a>'
                if page["report_href"] else "")
        metrics_html = (f'<div class="metrics" style="margin-bottom:var(--sp-3)">{metrics}</div>'
                        if metrics else "")
        if page["top_issues"]:
            issue_list = "".join(
                f'<li><span class="tag sev {i["severity"]}">{esc(i["severity_label"])}</span> '
                f'<a class="tolist" href="#{esc(i["anchor"])}">{esc(i["title"])}</a></li>'
                for i in page["top_issues"])
            issues_html = (f'<ul class="evidence" style="font-size:var(--t-base);'
                           f'color:var(--ink)">{issue_list}</ul>')
        elif page["audited"]:
            issues_html = '<p class="muted" style="margin:0">No failing audits on this page.</p>'
        else:
            issues_html = ('<p class="muted" style="margin:0">Scores only &mdash; this page was '
                           'not part of the deep audit. Raise "Max pages" to include it.</p>')

        cards.append(
            f'<div class="page-card anchor-target" id="{esc(page["anchor"])}">'
            f'<h3>{esc(page["path"])}</h3>'
            f'<div class="row">{minis}{link}</div>'
            f'{metrics_html}{issues_html}</div>')

    return (
        f'<section class="section" id="pages">'
        f'{_section_head("05", "Page appendix", "Every page the crawl covered, with its scores and a link to the raw tool output.")}'
        f'<table class="allow-break"><thead><tr><th>Page</th>{head_cells}'
        f'<th class="num">Findings</th><th class="num">Raw report</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
        f'<h3 style="margin:var(--sp-5) 0 var(--sp-3)">Page detail</h3>'
        f'{"".join(cards)}</section>')


def _metric_label(key):
    try:
        from consolidate_report import METRIC_LABELS
        return METRIC_LABELS.get(key, key)
    except Exception:                                            # pragma: no cover
        return key


# --------------------------------------------------------------------------
# 6. methodology
# --------------------------------------------------------------------------

METHOD_ITEMS = [
    ("Crawl and scoring",
     "Pages were discovered by crawling the site and each was scored by Google Lighthouse. "
     "Category scores in this report are the scanner's own, averaged across the pages "
     "audited; nothing is re-weighted here."),
    ("Severity",
     "Three tools use three scales, so they are mapped onto one for ordering: a Lighthouse "
     "audit scoring 0 is critical, under 0.5 serious, under 0.9 moderate; axe-core impacts "
     "are used as-is; security findings map High to serious and Info to minor."),
    ("Effort",
     "A coarse editorial estimate - quick win, moderate, involved - based on the kind of "
     "change each finding needs. It orders the work; it is not a quote."),
    ("Priority order",
     "The action list is ordered by severity first, so the most damaging findings are always "
     "at the top. Within each severity band, findings that touch more of the site and cost "
     "less effort come first. Nothing here re-scores the site; it only decides reading order."),
    ("Findings reported twice",
     "Lighthouse's accessibility category runs axe-core, so when the standards scan is also "
     "on, the same rule is reported by both tools. The two are merged into one finding - the "
     "axe record, which carries the failing elements and success criteria - with the pages "
     "each tool saw combined."),
    ("Overall health",
     "A weighted average of the category scores that were produced in this run "
     "(Performance 22, Accessibility 18, Security 18, SEO 15, Best Practices 13, "
     "WCAG 14). It is a summary for the cover, not a scanner output."),
    ("Security and WCAG scores",
     "Those two scanners report findings rather than scores. Security starts at 100 and "
     "deducts per finding (critical 30, serious 15, moderate 7, minor 2); the WCAG figure "
     "is the share of the selected standards that pass every automated check."),
]


COVERAGE_STATE_LABEL = {"blocked": "Blocked", "timeout": "Timed out",
                        "error": "Error", "skipped": "Skipped"}

COVERAGE_ROWS_SHOWN = 25


def _coverage_card(coverage):
    """Which pages were not measured, and why - in the tools' own words.

    Only rendered for a run that fell short of its own scope. It reports the
    run's ledger; it computes nothing.
    """
    if not coverage or coverage["status"] == "complete":
        return ""
    failures = coverage["failures"]
    rows = "".join(
        f'<tr><td>{esc(f["url"])}</td>'
        f'<td class="state">{esc(COVERAGE_STATE_LABEL.get(f["status"], f["status"]))}</td>'
        f'<td class="why">{esc(f["reason"] or "no reason recorded")}</td></tr>'
        for f in failures[:COVERAGE_ROWS_SHOWN])
    more = (f'<p class="muted">+{len(failures) - COVERAGE_ROWS_SHOWN} more page(s) '
            f'not measured.</p>' if len(failures) > COVERAGE_ROWS_SHOWN else "")
    table = (f'<table class="covertable"><thead><tr><th>Page</th><th>Outcome</th>'
             f'<th>Why</th></tr></thead><tbody>{rows}</tbody></table>{more}'
             if rows else "")
    notes = ("".join(f"<li>{esc(n)}</li>" for n in coverage["notes"]))
    notes = f"<ul>{notes}</ul>" if notes else ""
    failed_class = " failed" if coverage["status"] == "failed" else ""
    return (
        f'<div class="coverage{failed_class}" style="margin-top:var(--sp-4)">'
        f'<h4>Coverage: {esc(coverage["sentence"])}</h4>'
        f'<p>{esc(coverage["blurb"])} A page listed below was not measured by this '
        f'run: it has no result here, and its absence is not a pass.</p>'
        f'{notes}{table}</div>')


def render_methodology(report, brand):
    meta = report["meta"]
    tools = "".join(
        f'<div class="tool"><div class="n">{esc(t.get("name"))}</div>'
        f'<div class="v">{esc(t.get("version") or "version not recorded")}</div>'
        f'<div class="r">{esc(t.get("role", ""))}</div></div>'
        for t in meta["tools"])

    items = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in METHOD_ITEMS)

    scope_rows = "".join(
        f'<tr><td>{esc(str(k).replace("_", " ").title())}</td>'
        f'<td class="num">{esc(v)}</td></tr>'
        for k, v in meta["scope"].items())

    standards = ""
    if meta["standards"]:
        standards = (f'<p class="muted"><b>Standards selected:</b> '
                     f'{esc(", ".join(meta["standards"]))}</p>')

    return (
        f'<section class="section" id="method">'
        f'{_section_head("06", "Methodology & disclaimer", "How this report was produced, and what it can and cannot tell you.")}'
        f'<div class="grid g-2" style="align-items:start">'
        f'<div class="card"><h4 class="card-title">Tools, pinned</h4>'
        f'<div class="toolgrid">{tools}</div>'
        f'<p class="muted" style="margin-top:var(--sp-3);font-size:var(--t-sm)">'
        f'Run on {esc(meta["generated_full"])} against '
        f'{esc(meta["site_url"] or meta["host"])} using the '
        f'{esc(meta["device"] or "default")} profile.</p>{standards}</div>'
        f'<div class="card"><h4 class="card-title">Scope of this run</h4>'
        f'<table><tbody>{scope_rows}</tbody></table></div></div>'
        f'{_coverage_card(meta.get("coverage"))}'
        f'<div class="card" style="margin-top:var(--sp-4)">'
        f'<h4 class="card-title">How the numbers were produced</h4>'
        f'<dl class="method">{items}</dl></div>'
        f'<div class="disclaimer" style="margin-top:var(--sp-4)">'
        f'<h4>Automated testing only</h4>'
        f'<p>Every result here comes from automated tooling. Automated checks catch a '
        f'minority of real accessibility barriers - typically around a third - and cannot '
        f'judge whether content is understandable, whether a keyboard journey makes sense, '
        f'or whether a page works with a real screen reader. Nothing in this report is a '
        f'statement of legal compliance with WCAG, the ADA, EN 301 549 or any other '
        f'framework; a conformance claim needs manual expert review and assistive-technology '
        f'testing.</p>'
        f'<p style="margin:0">The security section is a passive review of headers, cookies, '
        f'TLS and redirects on responses the site returned normally. Nothing was probed, '
        f'fuzzed or attacked, and it is not a penetration test. Performance figures are '
        f'lab measurements from this machine and this connection, not field data from real '
        f'visitors.</p></div>'
        f'<p class="muted" style="margin-top:var(--sp-4);font-size:var(--t-sm)">'
        f'{esc(brand.footer_note)}</p>'
        f'</section>')


# --------------------------------------------------------------------------
# chrome: nav + script
# --------------------------------------------------------------------------

NAV_ITEMS = [
    ("summary", "Summary"),
    ("scorecard", "Scorecard"),
    ("actions", "Fix first"),
    ("detail", "Deep dives"),
    ("pages", "Pages"),
    ("method", "Methodology"),
]


def render_nav(report, brand, present):
    mark = (f'<img src="{esc(brand.agency_logo)}" alt="{esc(brand.agency_name)}">'
            if brand.agency_logo else
            f'<span class="dot">{esc(brand.agency_monogram)}</span>')
    links = "".join(f'<a href="#{key}">{esc(label)}</a>'
                    for key, label in NAV_ITEMS if key in present)
    return (f'<div class="topnav screen-only">'
            f'<div class="brandmark">{mark}<span>{esc(brand.client_name or brand.agency_name)}'
            f'</span></div><nav>{links}</nav>'
            f'<button class="btn" id="expandAll">Expand all</button>'
            f'<button class="btn primary" onclick="window.print()">Print / PDF</button></div>')


SCRIPT = """
(function () {
  var byId = function (id) { return document.getElementById(id); };

  // --- section highlighting in the nav -----------------------------------
  var links = Array.prototype.slice.call(document.querySelectorAll('.topnav nav a'));
  var targets = links.map(function (a) { return document.querySelector(a.getAttribute('href')); });
  function markCurrent() {
    var best = 0;
    targets.forEach(function (el, i) {
      if (el && el.getBoundingClientRect().top - 120 <= 0) { best = i; }
    });
    links.forEach(function (a, i) { a.classList.toggle('on', i === best); });
  }
  if (links.length) {
    markCurrent();
    window.addEventListener('scroll', markCurrent, { passive: true });
  }

  // --- action list filters ------------------------------------------------
  var active = {};
  var cards = Array.prototype.slice.call(document.querySelectorAll('#actionList .action'));
  var empty = byId('noMatches');
  function applyFilters() {
    var shown = 0;
    cards.forEach(function (card) {
      var ok = Object.keys(active).every(function (key) {
        return !active[key] || card.dataset[key] === active[key];
      });
      card.hidden = !ok;
      if (ok) { shown++; }
    });
    if (empty) { empty.hidden = shown !== 0; }
  }
  document.querySelectorAll('.filter').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var key = btn.dataset.filter, value = btn.dataset.value;
      var turningOff = active[key] === value;
      active[key] = turningOff ? null : value;
      document.querySelectorAll('.filter[data-filter="' + key + '"]').forEach(function (other) {
        other.classList.toggle('on', !turningOff && other === btn);
      });
      applyFilters();
    });
  });
  var clear = byId('clearFilters');
  if (clear) {
    clear.addEventListener('click', function () {
      active = {};
      document.querySelectorAll('.filter').forEach(function (b) { b.classList.remove('on'); });
      applyFilters();
    });
  }

  // --- expand / collapse --------------------------------------------------
  var expand = byId('expandAll');
  if (expand) {
    expand.addEventListener('click', function () {
      var all = document.querySelectorAll('details.expand');
      var opening = expand.textContent.indexOf('Expand') === 0;
      all.forEach(function (d) { d.open = opening; });
      expand.textContent = opening ? 'Collapse all' : 'Expand all';
    });
  }

  // --- paper is always fully expanded, and filters never hide print output -
  function openEverything() {
    document.querySelectorAll('details').forEach(function (d) { d.open = true; });
    cards.forEach(function (card) { card.hidden = false; });
  }
  window.addEventListener('beforeprint', openEverything);
  openEverything.forPaged = true;
  window.__reportOpenAll = openEverything;
})();
"""


# --------------------------------------------------------------------------
# document
# --------------------------------------------------------------------------

def render_report(report, brand=None, *, embed_script=True):
    """The full standalone HTML document."""
    brand = brand if isinstance(brand, Brand) else load_brand(brand or report.get("brand"))

    running = (
        f'<span class="rh rh-title">{esc(brand.client_name or brand.agency_name)}'
        f' &middot; {esc(brand.report_title)}</span>'
        f'<span class="rh rh-client">{esc(brand.footer_note or brand.agency_name)}'
        f' &middot; {esc(report["meta"]["generated_text"])}</span>')

    body_parts = [render_cover(report, brand, running), render_executive(report),
                  render_scorecard(report), render_actions(report)]
    present = {"summary", "scorecard", "actions"}

    dives = render_deep_dives(report)
    if dives:
        body_parts.append(dives)
        present.add("detail")
    appendix = render_appendix(report)
    if appendix:
        body_parts.append(appendix)
        present.add("pages")
    body_parts.append(render_methodology(report, brand))
    present.add("method")

    title = f'{brand.report_title} - {report["meta"]["host"] or report["meta"]["site_url"]}'
    script = f"<script>{SCRIPT}</script>" if embed_script else ""

    return (
        f'<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">\n'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>{esc(title)}</title>\n'
        f'<meta name="generator" content="{esc(brand.agency_name)} report renderer">\n'
        f'<style>{stylesheet(brand)}</style>\n</head>\n<body>\n'
        f'{render_nav(report, brand, present)}\n'
        f'<div class="doc">\n'
        f'{chr(10).join(body_parts)}\n</div>\n{script}\n</body></html>\n')
