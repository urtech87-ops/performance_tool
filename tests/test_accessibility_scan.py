"""Standards mapping + report rendering, with the Node runner stubbed out."""

import json
import os
import re
from pathlib import Path

import pytest

import accessibility_scan as a11y
from conftest import FakeRunner, axe_payload, violation


# --------------------------------------------------------------------------
# subprocess invocation
# --------------------------------------------------------------------------

def test_a_page_is_audited_by_node_running_the_playwright_runner(fake_runner, tmp_path):
    a11y.run_axe("https://example.com/", tmp_path, index=1)
    cmd = fake_runner.calls[0]["cmd"]
    assert cmd[0] == a11y.NODE
    assert Path(cmd[1]).name == "axe_playwright_runner.js"
    assert cmd[cmd.index("--url") + 1] == "https://example.com/"
    assert cmd[cmd.index("--timeout") + 1] == str(a11y.PER_PAGE_TIMEOUT)


def test_the_runner_packages_are_pinned():
    """axe-core's version is what the report prints, so @axe-core/playwright is
    pinned to it; playwright itself is a range because its browser and its
    client ship together and cannot drift apart."""
    assert a11y.RUNNER_DEPS["@axe-core/playwright"] == a11y.AXE_CORE_VERSION
    assert a11y.RUNNER_DEPS["playwright"].startswith("^1.")


def test_run_axe_requests_every_required_tag(fake_runner, tmp_path):
    a11y.run_axe("https://example.com/", tmp_path, index=1)
    tags = fake_runner.tags
    for required in ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa",
                     "best-practice", "section508", "EN-301-549"]:
        assert required in tags


def test_run_axe_parses_the_result_the_runner_printed(fake_runner, tmp_path):
    result, secs, why, _out = a11y.run_axe("https://example.com/", tmp_path, index=3)
    assert why == ""
    assert result["url"] == "https://example.com/"
    assert result["engine"] == "4.10.2"
    assert {v["id"] for v in result["violations"]} == {
        "image-alt", "color-contrast", "target-size",
        "scrollable-region-focusable", "region"}
    assert (tmp_path / "axe_003.json").exists()


def test_run_axe_trusts_the_payload_over_the_exit_code(monkeypatch, tmp_path):
    """The result on stdout is the evidence a page was audited; a runner that
    also grumbles on the way out has still audited it."""
    stub = FakeRunner()

    def noisy_exit(cmd, **kwargs):
        proc = stub(cmd, **kwargs)
        return a11y.subprocess.CompletedProcess(cmd, 1, stdout=proc.stdout,
                                                stderr="a warning on the way out")

    monkeypatch.setattr(a11y.subprocess, "run", noisy_exit)
    result, _secs, why, _out = a11y.run_axe("https://example.com/", tmp_path, index=1)
    assert why == "" and result["violations"]


def test_run_axe_reports_a_page_that_printed_no_result(monkeypatch, tmp_path):
    stub = FakeRunner(emit_result=False)
    monkeypatch.setattr(a11y.subprocess, "run", stub)
    result, _secs, why, _out = a11y.run_axe("https://example.com/", tmp_path, index=1)
    assert result is None and why.startswith("no result from the runner")


def test_run_axe_survives_a_timeout(monkeypatch, tmp_path):
    def boom(cmd, **kw):
        raise a11y.subprocess.TimeoutExpired(cmd, 1)
    monkeypatch.setattr(a11y.subprocess, "run", boom)
    result, _secs, why, _out = a11y.run_axe("https://example.com/", tmp_path, index=1)
    assert result is None and why == "timeout"


def test_run_axe_keeps_the_raw_result_in_the_work_dir(fake_runner, tmp_path):
    a11y.run_axe("https://example.com/", tmp_path, index=2)
    saved = json.loads((tmp_path / "axe_002.json").read_text(encoding="utf-8"))
    assert {v["id"] for v in saved["violations"]} >= {"image-alt", "color-contrast"}


def test_the_runner_script_is_headless_and_relaxes_certs_and_the_sandbox():
    """Everything the ChromeDriver command line used to carry now lives in the
    runner script, since Playwright launches the browser itself."""
    js = a11y.RUNNER_JS.read_text(encoding="utf-8")
    assert "headless: true" in js
    assert "ignoreHTTPSErrors: true" in js
    assert "--no-sandbox" in js and "--ignore-certificate-errors" in js


def test_run_axe_relaxes_node_tls_and_points_node_at_the_runner_packages(fake_runner, tmp_path):
    a11y.run_axe("https://example.com/", tmp_path, index=1)
    env = fake_runner.calls[0]["kwargs"]["env"]
    assert env["NODE_TLS_REJECT_UNAUTHORIZED"] == "0"
    assert env["NODE_PATH"].split(os.pathsep)[0].endswith("node_modules")


def test_normalize_axe_accepts_a_bare_result_object():
    payload = axe_payload("https://example.com/")
    norm = a11y.normalize_axe(payload)
    assert norm["url"] == "https://example.com/"
    assert len(norm["violations"]) == 5
    assert norm["engine"] == "4.10.2"
    assert norm["passes"] == 3


def test_normalize_axe_tolerates_junk():
    norm = a11y.normalize_axe(None, "https://example.com/")
    assert norm == {"url": "https://example.com/", "violations": [],
                    "incomplete": [], "passes": 0, "engine": ""}


# --------------------------------------------------------------------------
# WCAG success criteria
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tags,expected", [
    (["wcag2a", "wcag111"], ["1.1.1"]),
    (["cat.color", "wcag2aa", "wcag143"], ["1.4.3"]),
    (["wcag21aa", "wcag1412"], ["1.4.12"]),
    (["wcag22aa", "wcag258"], ["2.5.8"]),
    (["best-practice", "cat.keyboard"], []),
    (["wcag412", "wcag131"], ["1.3.1", "4.1.2"]),
])
def test_sc_from_tags(tags, expected):
    assert a11y.sc_from_tags(tags) == expected


def test_sc_label_adds_the_criterion_name():
    assert a11y.sc_label("1.4.3") == "1.4.3 Contrast (Minimum)"
    assert a11y.sc_label("9.9.9") == "9.9.9"


def test_fix_is_plain_language_and_falls_back_to_axe_help():
    curated = a11y.fix_for({"id": "color-contrast", "help": "x", "description": "y"})
    assert "contrast ratio" in curated and "4.5:1" in curated
    fallback = a11y.fix_for({"id": "some-new-rule", "help": "Do the thing",
                             "description": "Because reasons"})
    assert fallback == "Do the thing. Because reasons"


# --------------------------------------------------------------------------
# aggregation
# --------------------------------------------------------------------------

def _aggregate_of(*results):
    return a11y.aggregate(list(results))


def test_aggregate_dedupes_rules_across_pages_and_counts_elements():
    pages = [a11y.normalize_axe(axe_payload(u))
             for u in ("https://x.test/", "https://x.test/about/")]
    violations, incomplete, engine = _aggregate_of(*pages)
    assert engine == "4.10.2"
    by_id = {v["id"]: v for v in violations}
    assert set(by_id) == {"image-alt", "color-contrast", "target-size",
                          "scrollable-region-focusable", "region"}
    # color-contrast has 4 nodes on each of the 2 pages
    assert by_id["color-contrast"]["nodes"] == 8
    assert set(by_id["color-contrast"]["pages"]) == {"https://x.test/", "https://x.test/about/"}
    assert by_id["image-alt"]["sc"] == ["1.1.1"]
    assert [v["id"] for v in incomplete] == ["color-contrast"]


def test_aggregate_sorts_most_severe_first():
    pages = [a11y.normalize_axe(axe_payload("https://x.test/"))]
    violations, _, _ = a11y.aggregate(pages)
    impacts = [v["impact"] for v in violations]
    assert impacts == sorted(impacts, key=lambda i: a11y.IMPACT_ORDER.index(i))
    assert impacts[0] == "critical"


def test_aggregate_keeps_the_most_severe_impact_seen_for_a_rule():
    a = a11y.normalize_axe(axe_payload(
        "https://x.test/", violations=[violation("dup", "minor", ["wcag2a"])], incomplete=[]))
    b = a11y.normalize_axe(axe_payload(
        "https://x.test/b", violations=[violation("dup", "critical", ["wcag2a"])], incomplete=[]))
    violations, _, _ = a11y.aggregate([a, b])
    assert violations[0]["impact"] == "critical"


def test_unknown_impact_is_bucketed_as_minor():
    page = a11y.normalize_axe(axe_payload(
        "https://x.test/", violations=[violation("odd", None, ["wcag2a"])], incomplete=[]))
    violations, _, _ = a11y.aggregate([page])
    assert violations[0]["impact"] == "minor"


# --------------------------------------------------------------------------
# standards table + per-standard verdicts
# --------------------------------------------------------------------------

REQUIRED_STANDARDS = {
    "wcag20a", "wcag20aa", "wcag20aaa", "wcag21a", "wcag21aa", "wcag21aaa",
    "wcag22a", "wcag22aa", "wcag22aaa", "section508", "en301549", "ada",
    "unruh", "aoda", "uk_equality", "au_dda", "il_5568", "ca_aca", "atag20",
}


def test_every_required_framework_is_in_the_table():
    assert REQUIRED_STANDARDS <= set(a11y.STANDARDS_BY_ID)


@pytest.mark.parametrize("sid,expected", [
    ("section508", set(a11y.W20AA) | {"section508"}),
    ("en301549", set(a11y.W21AA) | {"EN-301-549"}),
    ("ada", set(a11y.W21AA)),
    ("unruh", set(a11y.W21AA)),
    ("aoda", set(a11y.W20AA)),
    ("uk_equality", set(a11y.W21AA)),
    ("au_dda", set(a11y.W21AA)),
    ("il_5568", set(a11y.W20AA)),
    ("ca_aca", set(a11y.W21AA)),
])
def test_legal_frameworks_map_to_the_right_wcag_scope(sid, expected):
    assert set(a11y.STANDARDS_BY_ID[sid]["tags"]) == expected


def test_atag_is_marked_not_applicable():
    atag = a11y.STANDARDS_BY_ID["atag20"]
    assert atag["applicable"] is False and atag["tags"] == ()
    row = a11y.evaluate_standards([], ["atag20"])[0]
    assert row["verdict"] == "n/a"


def test_atag_stays_not_applicable_even_with_violations():
    violations, _, _ = a11y.aggregate([a11y.normalize_axe(axe_payload("https://x.test/"))])
    row = a11y.evaluate_standards(violations, ["atag20"])[0]
    assert row["verdict"] == "n/a" and row["rules"] == 0


def _verdicts(violations, ids):
    return {r["id"]: r["verdict"] for r in a11y.evaluate_standards(violations, ids)}


ALL_IDS = [s["id"] for s in a11y.STANDARDS]


def test_a_wcag21aa_only_violation_splits_the_2_0_and_2_1_standards():
    page = a11y.normalize_axe(axe_payload("https://x.test/", incomplete=[], violations=[
        violation("reflow", "serious", ["wcag21aa", "wcag1410"])]))
    violations, _, _ = a11y.aggregate([page])
    v = _verdicts(violations, ALL_IDS)
    # 2.1 AA (and everything scoped to it) fails
    for sid in ("wcag21aa", "wcag21aaa", "wcag22aa", "en301549", "ada",
                "unruh", "uk_equality", "au_dda", "ca_aca"):
        assert v[sid] == "fail", sid
    # anything scoped to WCAG 2.0 only is unaffected
    for sid in ("wcag20a", "wcag20aa", "wcag20aaa", "wcag21a", "section508",
                "aoda", "il_5568"):
        assert v[sid] == "pass", sid


def test_a_section508_only_violation_fails_only_section508():
    page = a11y.normalize_axe(axe_payload("https://x.test/", incomplete=[], violations=[
        violation("legacy-508", "serious", ["section508", "section508.22.a"])]))
    violations, _, _ = a11y.aggregate([page])
    v = _verdicts(violations, ALL_IDS)
    assert v["section508"] == "fail"
    assert v["wcag20aa"] == "pass" and v["wcag21aa"] == "pass" and v["en301549"] == "pass"


def test_a_best_practice_only_violation_fails_no_legal_standard():
    page = a11y.normalize_axe(axe_payload("https://x.test/", incomplete=[], violations=[
        violation("region", "moderate", ["best-practice", "cat.keyboard"])]))
    violations, _, _ = a11y.aggregate([page])
    v = _verdicts(violations, ALL_IDS)
    assert all(verdict in ("pass", "n/a") for verdict in v.values())


def test_a_wcag2a_violation_fails_every_page_level_standard():
    page = a11y.normalize_axe(axe_payload("https://x.test/", incomplete=[], violations=[
        violation("image-alt", "critical", ["wcag2a", "wcag111"])]))
    violations, _, _ = a11y.aggregate([page])
    v = _verdicts(violations, ALL_IDS)
    assert v["atag20"] == "n/a"
    assert all(verdict == "fail" for sid, verdict in v.items() if sid != "atag20")


def test_a_wcag22aa_violation_only_fails_the_2_2_standards():
    page = a11y.normalize_axe(axe_payload("https://x.test/", incomplete=[], violations=[
        violation("target-size", "serious", ["wcag22aa", "wcag258"])]))
    violations, _, _ = a11y.aggregate([page])
    v = _verdicts(violations, ALL_IDS)
    assert v["wcag22aa"] == "fail" and v["wcag22aaa"] == "fail"
    assert v["wcag21aa"] == "pass" and v["en301549"] == "pass" and v["ada"] == "pass"


def test_verdict_carries_rule_element_and_page_counts():
    pages = [a11y.normalize_axe(axe_payload(u))
             for u in ("https://x.test/", "https://x.test/about/")]
    violations, _, _ = a11y.aggregate(pages)
    row = {r["id"]: r for r in a11y.evaluate_standards(violations, ["wcag21aa"])}["wcag21aa"]
    assert row["verdict"] == "fail"
    # image-alt (wcag2a), color-contrast (wcag2aa), scrollable-region (wcag21a)
    assert sorted(row["rule_ids"]) == ["color-contrast", "image-alt", "scrollable-region-focusable"]
    assert row["pages"] == 2
    assert row["elements"] == (1 + 4 + 1) * 2


def test_incomplete_items_never_drive_a_verdict():
    page = a11y.normalize_axe(axe_payload("https://x.test/", violations=[], incomplete=[
        violation("color-contrast", "serious", ["wcag2aa", "wcag143"])]))
    violations, incomplete, _ = a11y.aggregate([page])
    assert incomplete and not violations
    assert all(r["verdict"] in ("pass", "n/a")
               for r in a11y.evaluate_standards(violations, ALL_IDS))


# --------------------------------------------------------------------------
# selection / tag plumbing
# --------------------------------------------------------------------------

def test_normalize_standards_drops_junk_and_keeps_canonical_order():
    assert a11y.normalize_standards(["ada", "nope", "wcag21aa"]) == ["wcag21aa", "ada"]
    assert a11y.normalize_standards("section508,ada") == ["section508", "ada"]


def test_normalize_standards_falls_back_to_defaults():
    assert a11y.normalize_standards(None) == a11y.DEFAULT_STANDARDS
    assert a11y.normalize_standards([]) == a11y.DEFAULT_STANDARDS
    assert a11y.normalize_standards(["bogus"]) == a11y.DEFAULT_STANDARDS


def test_run_tags_adds_tags_a_selected_standard_needs():
    assert "wcag2aaa" not in a11y.run_tags(["wcag21aa"])
    tags = a11y.run_tags(["wcag20aaa"])
    assert "wcag2aaa" in tags
    assert tags[:len(a11y.RUN_TAGS)] == a11y.RUN_TAGS  # base list stays intact


# --------------------------------------------------------------------------
# report rendering
# --------------------------------------------------------------------------

@pytest.fixture
def report(fake_runner):
    return a11y.scan_site("https://x.test",
                          ["https://x.test/", "https://x.test/about/"],
                          standards=["wcag21aa", "section508", "en301549", "aoda", "atag20"],
                          concurrency=2, log=lambda *_: None)


def test_report_labels_everything_as_automated_checks(report):
    assert "Automated checks" in report
    assert "automated" in report.lower()
    # never a legal-compliance claim
    assert not re.search(r"\b(is|fully)\s+compliant\b", report, re.I)
    assert "certif" not in report.lower() or "not a legal compliance" in report.lower()


def test_report_prints_the_pinned_axe_version(report):
    assert a11y.AXE_CORE_VERSION in report
    assert "axe-core" in report


def test_report_has_a_panel_row_per_selected_standard(report):
    for name in ("WCAG 2.1 Level AA", "Section 508 (Revised)", "EN 301 549",
                 "Ontario AODA", "ATAG 2.0"):
        assert name in report
    assert "ADA Title III" not in report  # unselected standards stay out
    assert "Not applicable" in report     # ATAG 2.0


def test_report_shows_the_standard_scope_and_a_failing_verdict(report):
    assert "Not compliant (automated)" in report      # WCAG 2.1 AA fails
    assert "Scope: WCAG 2.0 Level AA (+ Section 508 specific rules)" in report


def test_report_shows_pass_and_fail_side_by_side(monkeypatch):
    """A 2.1-AA-only violation: 2.1 AA standards fail, 2.0 AA standards pass."""
    stub = FakeRunner(payload_for=lambda url: axe_payload(url, incomplete=[], violations=[
        violation("reflow", "serious", ["wcag21aa", "wcag1410"])]))
    monkeypatch.setattr(a11y.subprocess, "run", stub)
    out = a11y.scan_site("https://x.test", ["https://x.test/"],
                         standards=["wcag21aa", "aoda"], log=lambda *_: None)
    assert "Not compliant (automated)" in out          # WCAG 2.1 AA
    assert "Compliant (automated checks)" in out       # Ontario AODA (WCAG 2.0 AA)


def test_report_groups_findings_by_impact_with_sc_fix_and_elements(report):
    assert report.index("Critical") < report.index("Serious") < report.index("Moderate")
    assert "1.1.1 Non-text Content" in report
    assert "1.4.3 Contrast (Minimum)" in report
    assert "contrast ratio is at least 4.5:1" in report      # plain-language fix
    assert "dequeuniversity.com/rules/axe/4.13/image-alt" in report  # help URL
    assert "#color-contrast-0" in report                     # element selector
    assert "/about/" in report                               # affected page


def test_report_surfaces_incomplete_items_for_manual_review(report):
    assert "Needs manual review" in report
    assert "a person has to confirm them" in report


def test_report_escapes_element_html(fake_runner, monkeypatch):
    stub = FakeRunner(payload_for=lambda url: axe_payload(url, incomplete=[], violations=[
        violation("image-alt", "critical", ["wcag2a", "wcag111"],
                  node_html='<img src=x onerror="alert(1)"><script>bad()</script>')]))
    monkeypatch.setattr(a11y.subprocess, "run", stub)
    out = a11y.scan_site("https://x.test", ["https://x.test/"], log=lambda *_: None)
    assert "<script>bad()</script>" not in out
    assert "&lt;script&gt;bad()&lt;/script&gt;" in out


def test_report_is_clean_when_nothing_fires(monkeypatch):
    stub = FakeRunner(payload_for=lambda url: axe_payload(url, violations=[], incomplete=[]))
    monkeypatch.setattr(a11y.subprocess, "run", stub)
    out = a11y.scan_site("https://x.test", ["https://x.test/"],
                         standards=["wcag21aa"], log=lambda *_: None)
    assert "No violations detected by the automated checks" in out
    assert "Nothing flagged for manual review" in out
    assert "Compliant (automated checks)" in out


# --------------------------------------------------------------------------
# crawl plumbing: max pages + parallel are honoured by the caller's URL list
# --------------------------------------------------------------------------

def test_scan_site_audits_every_url_it_is_given(fake_runner):
    urls = [f"https://x.test/p{i}" for i in range(7)]
    a11y.scan_site("https://x.test", urls, concurrency=3, log=lambda *_: None)
    assert sorted(fake_runner.urls) == sorted(urls)


def test_scan_site_writes_axe_json_into_the_work_dir(fake_runner, tmp_path):
    work = tmp_path / "axe"
    a11y.scan_site("https://x.test", ["https://x.test/", "https://x.test/b"],
                   work_dir=work, log=lambda *_: None)
    assert sorted(p.name for p in work.glob("*.json")) == ["axe_001.json", "axe_002.json"]


def test_scan_site_keeps_going_when_one_page_fails(monkeypatch):
    stub = FakeRunner(fail_urls={"https://x.test/broken"})
    monkeypatch.setattr(a11y.subprocess, "run", stub)
    logged = []
    out = a11y.scan_site("https://x.test",
                         ["https://x.test/", "https://x.test/broken"],
                         log=logged.append)
    assert "1 of 2 page(s) analyzed" in out
    assert any("a11y FAILED (no result from the runner" in line for line in logged)


def test_scan_site_falls_back_to_the_site_url_when_given_no_pages(fake_runner):
    a11y.scan_site("https://x.test/", [], log=lambda *_: None)
    assert fake_runner.urls == ["https://x.test/"]
