"""A report may never claim compliance for something it did not measure.

Two shapes of run are pinned here, both with the Node runner stubbed:

  (a) the runner returns a real violations set  -> the standards in scope read
      "Not compliant", name the failing rules, and carry impact / WCAG SC /
      fix / affected pages;
  (b) the runner fails on every page            -> nothing is analyzed, so
      every standard reads "Not determined" and nothing anywhere reads green.

Plus the plumbing that makes (b) diagnosable instead of silent: the runner's
own stdout/stderr reaches the scan log, failures are counted, and the report
states pages_analyzed against pages_attempted.
"""

import re
from pathlib import Path

import pytest

import accessibility_scan as a11y
from conftest import FakeRunner, axe_payload, violation


PAGES = ["https://x.test/", "https://x.test/about/"]

# What a real axe run hands back on a page with problems.
REAL_VIOLATIONS = [
    violation("image-alt", "critical",
              ["cat.text-alternatives", "wcag2a", "wcag111", "section508", "EN-301-549"],
              "Images must have alternate text", nodes=2,
              node_html='<img src="hero.png">'),
    violation("color-contrast", "serious",
              ["cat.color", "wcag2aa", "wcag143"],
              "Elements must meet minimum color contrast ratio thresholds", nodes=3,
              node_html='<p style="color:#bbb">hard to read</p>'),
    violation("target-size", "serious",
              ["cat.sensory-and-visual-cues", "wcag22aa", "wcag258"],
              "All touch targets must be 24px large, or leave sufficient space"),
]
REAL_INCOMPLETE = [
    violation("color-contrast", "serious", ["cat.color", "wcag2aa", "wcag143"],
              "Elements must meet minimum color contrast ratio thresholds", nodes=2),
    violation("aria-hidden-focus", "serious", ["cat.aria", "wcag2a", "wcag412"],
              "aria-hidden elements must not be focusable"),
]

# What a real broken run prints: the browser never came up, so the page was
# never audited. (The ChromeDriver version mismatch this used to be is gone
# with the CLI - Playwright's Chromium cannot drift out of step - but a page
# that fails for any other reason must still be reported, never assumed clean.)
RUNNER_ERROR = (
    "could not launch Playwright's Chromium: browserType.launch: Executable "
    "doesn't exist at /root/.cache/ms-playwright/chromium-1187/chrome-linux/chrome\n"
    "Install the browser with: npx playwright install chromium"
)

GREEN_PHRASES = ("Compliant (automated checks)", "No violations detected by the automated checks",
                 "Nothing flagged for manual review")


def scan(monkeypatch, stub, standards=None, log=None):
    monkeypatch.setattr(a11y.subprocess, "run", stub)
    return a11y.scan_site("https://x.test", PAGES, concurrency=2,
                          standards=standards or ["wcag21aa", "wcag22aa", "section508", "atag20"],
                          log=log if log is not None else (lambda *_: None))


# --------------------------------------------------------------------------
# (a) a real violations set -> "Not compliant", with the failing rules
# --------------------------------------------------------------------------

@pytest.fixture
def failing_report(monkeypatch):
    stub = FakeRunner(payload_for=lambda url: axe_payload(
        url, violations=REAL_VIOLATIONS, incomplete=REAL_INCOMPLETE, passes=41))
    return scan(monkeypatch, stub)


def test_a_standard_with_violations_in_scope_is_not_compliant(failing_report):
    assert "Not compliant (automated)" in failing_report
    assert "Not determined" not in failing_report


def test_the_failing_standard_counts_its_rules_elements_and_pages(failing_report):
    # WCAG 2.1 AA sees image-alt (2 nodes) + color-contrast (3 nodes) on both pages
    assert re.search(r"2 failing rule\(s\).{0,40}10 element\(s\).{0,40}2 page\(s\)",
                     failing_report)


def test_the_standard_panel_lists_the_failing_rules_in_scope(failing_report):
    panel = failing_report.split("Findings by impact")[0]
    assert "Failing rules in scope:" in panel
    for rule_id in ("image-alt", "color-contrast"):
        assert rule_id in panel
    # target-size is WCAG 2.2 only: it fails 2.2 AA but must not be listed as a
    # 2.1 AA / Section 508 failure.
    assert panel.count("target-size") == 1


def test_each_failing_rule_carries_impact_sc_fix_and_where_it_fires(failing_report):
    panel = failing_report.split("Findings by impact")[0]
    assert "critical" in panel and "serious" in panel          # impact
    assert "1.1.1 Non-text Content" in panel                   # WCAG success criterion
    assert "1.4.3 Contrast (Minimum)" in panel
    assert "Give every meaningful image an alt attribute" in panel      # fix
    assert "contrast ratio is at least 4.5:1" in panel
    assert re.search(r"\d+ element\(s\) on \d+ page\(s\): /, /about/", panel)  # where


def test_a_standard_outside_the_violation_scope_is_still_compliant(monkeypatch):
    """target-size is WCAG 2.2 only, so 2.2 AA fails while Section 508 (2.0 AA)
    is a genuine pass - a pass is only ever stated for pages that were read."""
    stub = FakeRunner(payload_for=lambda url: axe_payload(
        url, incomplete=[], violations=[REAL_VIOLATIONS[2]]))
    out = scan(monkeypatch, stub, standards=["wcag22aa", "section508"])
    assert "Not compliant (automated)" in out          # WCAG 2.2 AA
    assert "Compliant (automated checks)" in out       # Section 508
    assert "Zero violations within this standard" in out


def test_incomplete_items_are_surfaced_with_real_counts(failing_report):
    manual = failing_report.split("Needs manual review")[1]
    assert "<b>2 rule(s)</b>" in manual
    assert "<b>6 element(s)</b>" in manual              # (2 + 1) nodes on each of 2 pages
    assert "<b>2 page(s)</b>" in manual
    assert "aria-hidden-focus" in manual


def test_the_run_reports_analyzed_attempted_and_passes(failing_report):
    assert "2 of 2 page(s) analyzed" in failing_report
    assert "page(s) failed" not in failing_report
    assert ">82<" in failing_report                    # 41 axe passes x 2 pages


# --------------------------------------------------------------------------
# (b) zero analyzed pages -> "Not determined", never green
# --------------------------------------------------------------------------

@pytest.fixture
def broken_run(monkeypatch):
    logged = []
    stub = FakeRunner(emit_result=False, fail_output=RUNNER_ERROR, fail_code=2)
    out = scan(monkeypatch, stub, log=logged.append)
    return out, logged


def test_no_standard_is_reported_as_compliant_when_nothing_was_analyzed(broken_run):
    report, _ = broken_run
    assert "Not determined" in report
    assert "scan did not complete" in report
    for phrase in GREEN_PHRASES:
        assert phrase not in report, phrase


def test_every_applicable_standard_reads_not_determined(broken_run):
    report, _ = broken_run
    panel = report.split("Standards &mdash; automated check results")[1] \
                  .split("Findings by impact")[0]
    # three applicable standards were selected; ATAG 2.0 stays not applicable
    assert panel.count("Not determined") == 3
    assert "Not applicable" in panel
    assert "Not compliant (automated)" not in report


def test_the_summary_says_the_scan_failed(broken_run):
    report, _ = broken_run
    assert "0 of 2 page(s) analyzed" in report
    assert "Scan failed" in report
    assert "nothing was measured" in report


def test_an_empty_findings_list_is_not_presented_as_clean(broken_run):
    report, _ = broken_run
    assert "No pages were analyzed, so no findings could be collected" in report
    assert "This is not a clean result" in report
    assert "nothing could be flagged for manual review" in report


def test_the_report_carries_the_runner_error_not_just_a_blank(broken_run):
    report, _ = broken_run
    assert "Why pages failed" in report
    assert "could not launch Playwright&#x27;s Chromium" in report
    assert "npx playwright install chromium" in report


def test_the_scan_log_carries_the_runner_stderr_verbatim(broken_run):
    _, logged = broken_run
    assert any("a11y FAILED" in line for line in logged)
    assert any("could not launch Playwright's Chromium" in line for line in logged)
    assert any("runner exited 2" in line for line in logged)
    assert any("SCAN FAILED - 0 of 2 page(s) analyzed" in line for line in logged)


def test_the_same_error_is_not_repeated_for_every_page(broken_run):
    _, logged = broken_run
    assert sum("Executable doesn't exist" in line for line in logged) == 1
    assert any("same error as an earlier page" in line for line in logged)


def test_evaluate_standards_refuses_to_pass_zero_analyzed_pages():
    rows = a11y.evaluate_standards([], ["wcag21aa", "ada", "atag20"], pages_analyzed=0)
    verdicts = {r["id"]: r["verdict"] for r in rows}
    assert verdicts == {"wcag21aa": "unknown", "ada": "unknown", "atag20": "n/a"}
    assert "pass" not in verdicts.values()


# --------------------------------------------------------------------------
# partial runs: measured pages report, unmeasured ones are called out
# --------------------------------------------------------------------------

def test_a_partial_scan_says_so_and_scopes_its_results(monkeypatch):
    logged = []
    stub = FakeRunner(fail_urls={"https://x.test/about/"}, fail_output=RUNNER_ERROR)
    out = scan(monkeypatch, stub, log=logged.append)
    assert "1 of 2 page(s) analyzed" in out
    assert "1 page(s) failed" in out
    assert "Partial scan" in out
    assert "unmeasured, not clean" in out
    assert any("could not be analyzed" in line for line in logged)


# --------------------------------------------------------------------------
# the runner itself: real output kept, driver pinned, result validated
# --------------------------------------------------------------------------

def test_run_axe_hands_back_the_runner_output_on_failure(monkeypatch, tmp_path):
    stub = FakeRunner(emit_result=False, fail_output=RUNNER_ERROR, fail_code=2)
    monkeypatch.setattr(a11y.subprocess, "run", stub)
    result, _secs, why, output = a11y.run_axe("https://x.test/", tmp_path, index=1)
    assert result is None
    assert "runner exited 2" in why
    assert "could not launch Playwright's Chromium" in output


def test_run_axe_captures_output_instead_of_discarding_it(fake_runner, tmp_path):
    """stdout carries the result, stderr the diagnostics - both are read, so a
    page that fails can say why."""
    a11y.run_axe("https://x.test/", tmp_path, index=1)
    kwargs = fake_runner.calls[0]["kwargs"]
    assert kwargs["stdout"] is a11y.subprocess.PIPE
    assert kwargs["stderr"] is a11y.subprocess.PIPE


def test_run_axe_drives_the_playwright_runner_and_not_a_chromedriver(fake_runner, tmp_path):
    """No ChromeDriver anywhere: Node runs our script, Playwright supplies the
    browser, so the two can never be a version apart."""
    a11y.run_axe("https://x.test/", tmp_path, index=1)
    cmd = fake_runner.calls[0]["cmd"]
    assert cmd[0] == a11y.NODE
    assert Path(cmd[1]).name == "axe_playwright_runner.js"
    assert not any("chromedriver" in str(a).lower() for a in cmd)
    assert fake_runner.calls[0]["kwargs"]["env"]["NODE_TLS_REJECT_UNAUTHORIZED"] == "0"


def test_the_scanner_no_longer_knows_how_to_find_a_chromedriver():
    """The mismatch was only reachable through the CLI's driver; the CLI and the
    driver discovery that fed it are gone, not left lying around to be reused."""
    for gone in ("find_chromedriver", "find_chrome", "AXE", "AXE_CLI_PACKAGE"):
        assert not hasattr(a11y, gone), gone


def test_output_that_is_not_an_axe_result_is_a_failure(monkeypatch, tmp_path):
    """JSON on stdout is not proof of an audit - it has to carry axe's buckets."""
    def prints_junk(cmd, **kwargs):
        return a11y.subprocess.CompletedProcess(list(cmd), 0,
                                                stdout='{"hello": "world"}', stderr="")

    monkeypatch.setattr(a11y.subprocess, "run", prints_junk)
    result, _secs, why, _out = a11y.run_axe("https://x.test/", tmp_path, index=1)
    assert result is None and why == "runner output is not an axe result"


def test_a_real_axe_result_reports_violations_incomplete_and_passes(fake_runner, tmp_path):
    result, _secs, why, _out = a11y.run_axe("https://x.test/", tmp_path, index=1)
    assert why == ""
    assert result["violations"] and result["incomplete"]
    assert result["passes"] == 3
