"""The report layer, rendered from a fixture instead of a live scan.

Everything here runs off fixtures/sample_scan.json, so the layout can be
changed and re-checked in milliseconds with no Node, no browser and no site.
The assertions are about the report's structure and its promises - narrative
order, prioritisation, white-labelling, the links to the raw tool output, and
the paged-media rules the PDF depends on - not about exact markup, which is
meant to keep moving.
"""

import json
import re
from pathlib import Path

import pytest

import consolidate_report as cr
import report_brand
import report_charts as charts
import report_model as model
import report_render
import report_sample
from report_brand import Brand, load_brand

FIXTURE = Path(report_sample.FIXTURE)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def raw():
    return report_sample.load_fixture()


@pytest.fixture(scope="module")
def report(raw):
    return report_sample.sample_report()


@pytest.fixture(scope="module")
def html(report):
    return report_sample.sample_html()


# --------------------------------------------------------------------------
# the fixture itself
# --------------------------------------------------------------------------

def test_the_fixture_is_shaped_like_a_real_run(raw):
    """It has to match what the scanners emit, or iterating on it proves
    nothing about the real report."""
    assert FIXTURE.is_file()
    page = raw["pages"][0]
    assert set(page) >= {"path", "device", "scores", "metrics", "issues", "report_html"}
    assert set(page["scores"]) == {"performance", "accessibility", "best-practices", "seo"}
    assert all(0 <= v <= 1 for v in page["scores"].values())     # Lighthouse 0..1, not 0..100

    violation = raw["accessibility"]["violations"][0]
    assert set(violation) >= {"id", "impact", "help", "tags", "pages", "nodes"}
    assert violation["impact"] in model.SEVERITIES

    assert set(raw["security"]) >= {"hits", "scanned", "total"}
    import security_scan
    assert set(raw["security"]["hits"]) <= set(security_scan.RULES)

    # a page with issues=None (scores only, never deep-audited) must be covered
    assert any(p["issues"] is None for p in raw["pages"])


def test_the_fixture_renders_without_a_scan(tmp_path):
    out = tmp_path / "sample-report.html"
    out.write_text(report_sample.sample_html(), encoding="utf-8")
    assert out.stat().st_size > 40_000
    assert out.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


# --------------------------------------------------------------------------
# the model
# --------------------------------------------------------------------------

def test_scores_are_the_scanners_own_averages(report, raw):
    """The report must not re-score anything: each category is the mean of the
    per-page scores the scanner reported."""
    for key in ("performance", "accessibility", "seo", "best-practices"):
        values = [p["scores"][key] for p in raw["pages"]]
        assert report["scores"][key] == round(100 * sum(values) / len(values))


def test_every_category_that_ran_is_scored(report):
    assert set(report["scores"]) == {"performance", "accessibility", "seo",
                                     "best-practices", "security", "wcag"}
    assert report["health"]["score"] is not None
    assert 0 <= report["health"]["score"] <= 100


def test_health_is_the_weighted_mean_of_what_ran():
    scores = {"performance": 50, "accessibility": 100}
    expected = round((50 * model.CATEGORY_WEIGHT["performance"]
                      + 100 * model.CATEGORY_WEIGHT["accessibility"])
                     / (model.CATEGORY_WEIGHT["performance"]
                        + model.CATEGORY_WEIGHT["accessibility"]))
    assert model.health_score(scores) == expected
    assert model.health_score({}) is None          # nothing ran -> no verdict


def test_a_scan_that_never_completed_is_not_a_pass():
    """An empty violation list from a scan that never ran must not read as a
    green WCAG score."""
    assert model.wcag_score({"panel": [{"verdict": "unknown"}]}) is None
    assert model.wcag_score({"panel": [{"verdict": "pass"}, {"verdict": "fail"}]}) == 50
    assert model.wcag_score({"panel": [{"verdict": "n/a"}]}) is None


def test_findings_come_from_all_three_scanners(report):
    categories = {i["category"] for i in report["issues"]}
    assert {"performance", "seo", "security", "wcag"} <= categories
    assert report["totals"]["issues"] == len(report["issues"])


def test_the_same_audit_on_many_pages_is_one_finding(report):
    """A flat dump repeats an audit per page; this report states it once and
    counts the pages."""
    unused = next(i for i in report["issues"] if i["id"] == "unused-javascript")
    assert unused["page_count"] == 3
    assert sorted(unused["pages"]) == ["/", "/collections/outerwear",
                                       "/products/harbor-parka"]
    assert sum(1 for i in report["issues"] if i["id"] == "unused-javascript") == 1


def test_a_rule_both_tools_report_is_merged_once(report):
    """Lighthouse's accessibility category runs axe-core, so image-alt arrives
    twice. One finding must survive - the axe one, which carries the elements
    and success criteria - with both tools' pages folded in."""
    matches = [i for i in report["issues"] if i["id"] == "image-alt"]
    assert len(matches) == 1
    assert matches[0]["category"] == "wcag"
    assert matches[0]["elements"] == 11
    assert "Lighthouse" in matches[0].get("also_reported_by", [])
    assert "/" in matches[0]["pages"]


def test_binary_hygiene_audits_do_not_outrank_real_breakage(report):
    """Lighthouse scores a missing meta description 0, the same as a broken
    page. Ordering on that alone would put hygiene above breakage."""
    by_id = {i["id"]: i for i in report["issues"]}
    assert by_id["meta-description"]["severity"] == "moderate"
    assert by_id["largest-contentful-paint"]["severity"] == "critical"
    assert by_id["meta-description"]["rank"] > by_id["largest-contentful-paint"]["rank"]


def test_the_action_list_is_ordered_severity_first(report):
    ranks = [i["severity_rank"] for i in report["issues"]]
    assert ranks == sorted(ranks), "a lower-severity finding is ranked above a higher one"
    assert [i["rank"] for i in report["issues"]] == list(range(1, len(report["issues"]) + 1))


def test_reach_and_effort_order_findings_inside_a_severity_band(report):
    band = [i for i in report["issues"] if i["severity"] == "serious"]
    assert len(band) > 1
    assert [i["priority"] for i in band] == sorted((i["priority"] for i in band), reverse=True)
    # a site-wide quick win must beat a single-page involved fix of equal severity
    quick = model.make_issue("hsts", "t", "security", "serious", "f", site_wide=True)
    slow = model.make_issue("unused-javascript", "t", "performance", "serious", "f",
                            pages=["/only"])
    assert model.priority_of(quick, 8) > model.priority_of(slow, 8)


def test_every_finding_carries_severity_effort_reach_and_a_fix(report):
    for issue in report["issues"]:
        assert issue["severity"] in model.SEVERITIES
        assert issue["effort"] in model.EFFORTS
        assert issue["fix"] and len(issue["fix"]) > 10
        assert issue["scope_label"]
        assert issue["site_wide"] or issue["page_count"] >= 1


def test_security_findings_keep_the_scanners_own_severity(report):
    """security_scan owns the severities; the report only folds its five-step
    scale onto the shared four."""
    import security_scan
    for issue in report["issues"]:
        if issue["category"] != "security":
            continue
        _, source_severity, _ = security_scan.RULES[issue["id"]]
        assert issue["source_severity"] == source_severity
        assert issue["severity"] == model.SECURITY_SEVERITY_MAP[source_severity]


def test_the_heatmap_covers_every_page_and_category(report, raw):
    heat = report["heatmap"]
    assert len(heat["rows"]) == len(raw["pages"])
    assert [c["key"] for c in heat["cols"]] == report["meta"]["categories"]
    for row in heat["rows"]:
        assert len(row["cells"]) == len(heat["cols"])
        for cell in row["cells"]:
            assert cell["band"] in {"good", "avg", "poor", "na"}


def test_severity_distribution_matches_the_findings(report):
    dist = report["severity_distribution"]
    assert sum(dist.values()) == len(report["issues"])
    for severity in model.SEVERITIES:
        assert dist[severity] == sum(1 for i in report["issues"] if i["severity"] == severity)


def test_the_executive_summary_is_written_for_a_non_technical_reader(report):
    ex = report["executive"]
    assert len(ex["verdicts"]) == len(report["scores"])
    assert all(v["sentence"] and not v["sentence"].endswith(":") for v in ex["verdicts"])
    assert ex["top_issues"], "the summary must name the issues that matter most"
    assert all(i["severity"] in ("critical", "serious") for i in ex["top_issues"])
    assert "findings" in ex["what_this_means"] or "finding" in ex["what_this_means"]


def test_a_clean_site_produces_a_report_that_says_so():
    clean = model.build_report(
        [{"path": "https://ok.test/", "device": "Desktop", "metrics": {},
          "scores": {"performance": 0.98, "accessibility": 0.97,
                     "best-practices": 0.95, "seo": 1.0}, "issues": []}],
        site_url="https://ok.test")
    assert clean["issues"] == []
    assert clean["health"]["score"] >= 90
    assert "no failing audits" in clean["executive"]["what_this_means"]
    assert "No action is required" in report_render.render_actions(clean)


def test_a_run_with_no_pages_at_all_still_renders():
    empty = model.build_report([], site_url="https://x.test")
    assert empty["health"]["score"] is None
    out = report_render.render_report(empty)
    assert "<!DOCTYPE html>" in out and "Methodology" in out


# --------------------------------------------------------------------------
# the document
# --------------------------------------------------------------------------

def test_the_report_tells_a_story_in_order(html):
    """Cover, then the plain-language summary, then scores, then what to do,
    then the detail - not a flat table."""
    order = ["class=\"cover\"", 'id="summary"', 'id="scorecard"', 'id="actions"',
             'id="detail"', 'id="pages"', 'id="method"']
    positions = [html.index(marker) for marker in order]
    assert positions == sorted(positions)


def test_the_cover_carries_the_scan_identity(html, report):
    cover = html[html.index('class="cover"'):html.index('id="summary"')]
    assert report["meta"]["site_url"] in cover
    assert report["meta"]["generated_text"] in cover
    assert "Desktop" in cover                       # device
    assert "Pages covered" in cover and "Audit scope" in cover
    assert "gauge" in cover                         # the overall health score


def test_the_scorecard_draws_one_ring_per_category(html, report):
    card = html[html.index('id="scorecard"'):html.index('id="actions"')]
    assert card.count('class="ring ') == len(report["categories"])
    assert 'class="heatmap"' in card
    for category in report["categories"]:
        assert category["label"] in card


def test_the_action_list_leads_with_the_worst_findings(html, report):
    actions = html[html.index('id="actions"'):html.index('id="detail"')]
    top = report["issues"][0]
    assert top["title"] in actions
    assert 'data-severity="critical"' in actions
    assert 'class="rank">1<' in actions
    assert "Quick win" in actions                   # effort is shown, not implied


def test_the_appendix_keeps_the_links_to_the_raw_tool_output(html, raw):
    appendix = html[html.index('id="pages"'):html.index('id="method"')]
    for page in raw["pages"]:
        if page["report_html"]:
            assert page["report_html"] in appendix
    assert "Open the full Lighthouse report" in appendix


def test_a_page_that_was_never_deep_audited_says_so(html):
    assert "not part of the deep audit" in html


def test_the_methodology_page_pins_the_tools_and_disclaims(html, raw):
    method = html[html.index('id="method"'):]
    for tool in raw["meta"]["tools"]:
        assert tool["name"] in method
    assert "4.13.0" in method                       # a pinned version, not "latest"
    assert "Automated testing only" in method
    prose = " ".join(method.split())
    assert "Nothing in this report is a statement of legal compliance" in prose
    assert "not a penetration test" in method
    for heading, _ in report_render.METHOD_ITEMS:
        assert heading in method


def test_the_standards_panel_never_claims_legal_compliance(html):
    assert "not a legal" in html
    assert "ATAG 2.0" in html                       # reported as not applicable
    assert "Not applicable" in html


def test_everything_user_supplied_is_escaped():
    """Page paths, audit text and brand names all come from outside."""
    nasty = '<script>alert(1)</script>'
    out = report_render.render_report(model.build_report(
        [{"path": f"https://x.test/{nasty}", "device": "Desktop", "metrics": {},
          "scores": {"performance": 0.5},
          "issues": [{"id": "x", "category": "performance", "title": nasty,
                      "description": nasty, "displayValue": nasty, "score": 0.0}]}],
        site_url=f"https://x.test/{nasty}"),
        load_brand({"agency_name": nasty, "client_name": nasty}))
    assert "<script>alert(1)</script>" not in out.replace(report_render.SCRIPT, "")
    assert "&lt;script&gt;" in out


def test_the_document_is_standalone(html):
    """No CDN, no sibling assets: one file you can email."""
    assert "https://fonts.googleapis.com" not in html
    assert not re.search(r'<(?:script|link)[^>]+(?:src|href)="https?://', html)
    for match in re.finditer(r'<img[^>]+src="([^"]+)"', html):
        assert match.group(1).startswith("data:")


# --------------------------------------------------------------------------
# white-label
# --------------------------------------------------------------------------

def test_the_brand_config_reaches_every_corner(tmp_path):
    logo = tmp_path / "logo.svg"
    logo.write_text('<svg xmlns="http://www.w3.org/2000/svg"><rect width="8" height="8"/></svg>',
                    encoding="utf-8")
    config = tmp_path / "brand.json"
    config.write_text(json.dumps({
        "agency_name": "Northwind Digital", "agency_logo": "logo.svg",
        "client_name": "Acme Retail", "primary": "#0d7d5a", "secondary": "#0b5f8a",
        "report_title": "Quarterly Site Review", "footer_note": "Acme internal use only",
        "contact": "team@northwind.example",
    }), encoding="utf-8")

    brand = load_brand(str(config))
    out = report_sample.sample_html(brand=str(config))

    assert "Northwind Digital" in out and "Acme Retail" in out
    assert "Quarterly Site Review" in out
    assert "Acme internal use only" in out and "team@northwind.example" in out
    assert "--brand: #0d7d5a" in out                # tokens, not hard-coded colour
    assert "--brand2: #0b5f8a" in out
    assert brand.agency_logo.startswith("data:image/svg+xml;base64,")
    assert brand.agency_logo in out                 # inlined, not linked
    assert "#2f5bea" not in out                     # no default brand colour left over


def test_a_missing_brand_config_still_renders():
    brand = load_brand("/definitely/not/here.json")
    assert brand.agency_name == report_brand.DEFAULTS["agency_name"]
    assert brand.agency_logo == ""
    assert brand.agency_monogram                    # a monogram stands in for the logo
    assert "<!DOCTYPE html>" in report_sample.sample_html(brand={})


def test_brand_colour_input_is_sanitised():
    brand = load_brand({"primary": "javascript:alert(1)", "secondary": "#ABC"})
    assert brand.primary == report_brand.DEFAULTS["primary"]
    assert brand.secondary == "#aabbcc"             # shorthand expanded
    assert "javascript:" not in "".join(brand.tokens().values())


def test_status_colour_is_never_the_brand_colour():
    """A red finding has to look red whatever the agency's palette is."""
    brand = load_brand({"primary": "#0d7d5a"})
    tokens = brand.tokens()
    assert tokens["--poor"] == report_brand.STATUS_COLORS["poor"]
    assert tokens["--sev-critical"] == report_brand.SEVERITY_COLORS["critical"]


def test_a_logo_that_is_not_an_image_is_ignored(tmp_path):
    bad = tmp_path / "logo.exe"
    bad.write_bytes(b"MZ\x00\x00")
    assert load_brand({"agency_logo": str(bad)}).agency_logo == ""


# --------------------------------------------------------------------------
# charts
# --------------------------------------------------------------------------

def test_rings_are_drawn_to_scale():
    full = charts.score_ring(100)
    empty = charts.score_ring(0)
    assert "100" in full and ">0<" in empty
    assert 'stroke-dasharray="0.00' in empty        # nothing filled at zero
    blank = charts.score_ring(None)
    assert 'stroke-dasharray="0.00' in blank                # nothing filled when not run
    assert ">-<" in blank


def test_charts_never_collide_on_element_ids():
    """Two severity bars on one page must not share a clipPath id."""
    first = charts.severity_bar({"critical": 1}, uid="a")
    second = charts.severity_bar({"critical": 1}, uid="b")
    ids = re.findall(r'id="([^"]+)"', first + second)
    assert len(ids) == len(set(ids))


def test_charts_are_labelled_for_a_screen_reader():
    assert 'role="img"' in charts.score_ring(50, "Performance")
    assert "out of 100" in charts.health_gauge(50)
    assert "not measured" in charts.score_ring(None, "SEO")
    assert 'aria-label' in charts.heatmap(
        {"cols": [{"key": "performance", "label": "Performance"}],
         "rows": [{"path": "/", "cells": [{"band": "good", "text": "95", "key": "performance"}]}]})


def test_an_empty_severity_bar_does_not_pretend_to_have_data():
    assert "empty" in charts.severity_bar({})
    assert "No findings" in charts.severity_bar({})


# --------------------------------------------------------------------------
# paged media (what the PDF depends on)
# --------------------------------------------------------------------------

def test_one_document_serves_both_the_screen_and_the_page(html):
    """paged.js drops @media screen and keeps @media print, so the same file
    is the interactive report and the print master."""
    assert "@media screen {" in html and "@media print {" in html
    assert html.index("@media screen {") < html.index("@media print {")


def test_the_print_stylesheet_declares_the_page_furniture(html):
    printed = html[html.index("@media print"):]
    assert "@page" in printed
    assert "size: A4" in printed
    assert "counter(page)" in printed and "counter(pages)" in printed
    assert "@top-left" in printed and "@bottom-right" in printed
    assert "@page cover" in printed                       # the cover has no furniture
    assert "string-set: doc-title" in printed
    assert "string-set: doc-section" in printed


def test_the_running_header_sources_are_in_the_document_but_invisible(html):
    assert 'class="rh rh-title"' in html and 'class="rh rh-client"' in html
    assert re.search(r"\.rh \{[^}]*clip-path: inset\(50%\)", html)
    # they have to sit inside the cover: as the first thing in the flow they
    # would be laid out on a page of their own before the cover's named page
    cover = html[html.index('class="cover"'):html.index('class="cover-top"')]
    assert 'class="rh rh-title"' in cover


def test_cards_and_sections_control_their_own_breaks(html):
    printed = html[html.index("@media print"):]
    assert "break-inside: avoid" in printed
    assert "break-before: page" in printed                # each section starts fresh
    assert "break-after: avoid" in printed                # headings stay with their content
    assert "display: table-header-group" in printed       # table headers repeat


def test_tables_can_fragment_across_pages(html):
    """`overflow: hidden` rounds the corners on screen but makes the table an
    unbreakable box, which pushes it whole to the next page and leaves a blank
    one behind."""
    printed = html[html.index("@media print"):]
    assert "table { overflow: visible" in printed


def test_collapsibles_and_filters_never_hide_printed_content(html):
    assert "beforeprint" in html                          # native print expands them
    assert "__reportOpenAll" in html                      # and so does the paged.js hook
    import report_pdf
    assert "__reportOpenAll" in report_pdf.READY_HOOK


def test_links_print_their_destination(html):
    printed = html[html.index("@media print"):]
    assert 'a.linkout::after' in printed and "attr(href)" in printed


# --------------------------------------------------------------------------
# the PDF pipeline
# --------------------------------------------------------------------------

def test_the_polyfill_is_inlined_into_the_print_document():
    import report_pdf
    out = report_pdf.build_print_document("<html><body><p>hi</p></body></html>",
                                          "/* paged.js */")
    assert out.index("/* paged.js */") < out.index("</body>")
    assert "window.PagedConfig" in out
    assert out.index("window.PagedConfig") < out.index("/* paged.js */")
    assert "pagedjs-ready" in out                         # the signal Chrome waits for


def test_a_document_without_a_body_tag_still_gets_the_polyfill():
    import report_pdf
    assert "/* paged.js */" in report_pdf.build_print_document("<p>hi</p>", "/* paged.js */")


def test_the_pdf_falls_back_when_pagedjs_cannot_be_had(tmp_path, monkeypatch):
    """A missing polyfill must cost the page furniture, not the PDF."""
    import report_pdf
    calls = []
    monkeypatch.setattr(report_pdf, "find_pagedjs", lambda log=None, auto_install=None: None)
    monkeypatch.setattr(report_pdf, "find_chrome", lambda: "/fake/chrome")
    monkeypatch.setattr(report_pdf, "_print_with_chrome",
                        lambda chrome, src, pdf, paged: calls.append(paged))
    source = tmp_path / "report.html"
    source.write_text("<html><body>x</body></html>", encoding="utf-8")

    logged = []
    info = report_pdf.html_to_pdf(str(source), str(tmp_path / "report.pdf"), log=logged.append)
    assert info["engine"] == "chrome" and calls == [False]
    assert any("paged.js not available" in line for line in logged)


def test_the_paged_pass_runs_beside_the_report_so_links_still_resolve(tmp_path, monkeypatch):
    """The intermediate document has to sit in the report's own folder, or the
    relative links to each page's Lighthouse report break."""
    import report_pdf
    polyfill = tmp_path / "paged.js"
    polyfill.write_text("/* paged.js */", encoding="utf-8")
    seen = {}

    def fake_print(chrome, source_path, pdf_path, paged):
        seen["dir"] = Path(source_path).parent
        seen["paged"] = paged
        seen["text"] = Path(source_path).read_text(encoding="utf-8")

    monkeypatch.setattr(report_pdf, "find_pagedjs", lambda log=None, auto_install=None: polyfill)
    monkeypatch.setattr(report_pdf, "find_chrome", lambda: "/fake/chrome")
    monkeypatch.setattr(report_pdf, "_print_with_chrome", fake_print)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    source = run_dir / "report.html"
    source.write_text('<html><body><a href="lhr/page_001.report.html">x</a></body></html>',
                      encoding="utf-8")

    info = report_pdf.html_to_pdf(str(source), str(run_dir / "report.pdf"))
    assert info["engine"] == "pagedjs"
    assert seen["dir"] == run_dir and seen["paged"] is True
    assert "lhr/page_001.report.html" in seen["text"]
    assert not (run_dir / "report.print.html").exists()   # cleaned up afterwards


def test_chrome_is_given_time_for_the_polyfill_to_finish():
    import report_pdf
    assert report_pdf.VIRTUAL_TIME_BUDGET_MS >= 30_000


# --------------------------------------------------------------------------
# the seam with the scan pipeline
# --------------------------------------------------------------------------

def test_build_html_keeps_its_original_signature(tmp_path):
    """Existing callers pass four positional/keyword arguments and nothing else."""
    pages = [{"path": "https://x.test/", "device": "Desktop", "metrics": {},
              "scores": {"performance": 0.5}, "issues": [], "report_html": None}]
    out = cr.build_html(pages, [tmp_path], out_path=str(tmp_path / "report.html"),
                        categories=["performance"])
    assert "<!DOCTYPE html>" in out
    assert "Performance" in out and "Accessibility" not in out.split("Methodology")[0]


def test_the_report_link_is_relative_to_where_the_report_is_written(tmp_path):
    run = tmp_path / "run"
    (run / "lhr").mkdir(parents=True)
    lhr = run / "lhr" / "page_001.report.html"
    lhr.write_text("x", encoding="utf-8")
    pages = [{"path": "https://x.test/", "device": "Desktop", "metrics": {},
              "scores": {"performance": 0.5}, "issues": [], "report_html": str(lhr)}]
    out = cr.build_html(pages, [run], out_path=str(run / "report.html"))
    assert 'href="lhr/page_001.report.html"' in out


def test_the_lhr_loader_carries_each_audit_id_and_category(tmp_path):
    """Grouping findings by category needs the category the LHR already states
    in its auditRefs."""
    (tmp_path / "page.report.json").write_text(json.dumps({
        "finalDisplayedUrl": "https://x.test/",
        "categories": {
            "performance": {"score": 0.5, "auditRefs": [{"id": "unused-javascript"}]},
            "seo": {"score": 0.9, "auditRefs": [{"id": "meta-description"}]},
        },
        "audits": {
            "unused-javascript": {"id": "unused-javascript", "title": "U",
                                  "score": 0.3, "scoreDisplayMode": "numeric"},
            "meta-description": {"id": "meta-description", "title": "M",
                                 "score": 0, "scoreDisplayMode": "binary"},
            "skipped": {"id": "skipped", "title": "S", "score": None,
                        "scoreDisplayMode": "notApplicable"},
        },
    }), encoding="utf-8")
    issues = cr.load_lhr_pages([tmp_path])[0]["issues"]
    assert {i["id"]: i["category"] for i in issues} == {
        "unused-javascript": "performance", "meta-description": "seo"}


def test_the_scanners_still_return_their_own_standalone_pages():
    """The split into collect + render must not change what scan_site does."""
    import accessibility_scan
    import security_scan

    sec_data = {"site_url": "https://x.test", "hits": {"hsts": {"__site__"}},
                "scanned": 1, "total": 1, "cert_note": ""}
    sec_page = security_scan.html_from(sec_data)
    assert sec_page.startswith("<!DOCTYPE html>") and "Security Report" in sec_page

    a11y_data = {"site_url": "https://x.test", "panel": [], "violations": [],
                 "incomplete": [], "pages_analyzed": 1, "pages_attempted": 1,
                 "engine_version": "4.13.0", "tags": [], "failures": [],
                 "checks_passed": 3}
    a11y_page = accessibility_scan.html_from(a11y_data)
    assert a11y_page.startswith("<!DOCTYPE html>") and "axe-core" in a11y_page


def test_the_consolidated_report_covers_all_three_scanners(tmp_path):
    """What the dashboard hands over after a full run."""
    raw = report_sample.load_fixture()
    out = cr.build_html(raw["pages"], [tmp_path], out_path=str(tmp_path / "report.html"),
                        categories=raw["meta"]["categories"],
                        site_url=raw["meta"]["site_url"], device="desktop",
                        accessibility=raw["accessibility"], security=raw["security"],
                        standards=raw["meta"]["standards"])
    assert "Security" in out and "WCAG Standards" in out
    assert "Missing HTTP Strict Transport Security" in out
    assert "Images must have alternate text" in out
    assert "axe-core" in out
