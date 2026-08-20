"""The accessibility runner is Node + @axe-core/playwright, not @axe-core/cli.

The CLI drove Chrome through ChromeDriver, and the two are versioned apart: the
moment Chrome auto-updated, every page died with

    session not created: This version of ChromeDriver only supports Chrome
    version 152 / Current browser version is 151

which made the scan analyze 0 pages. Playwright ships the browser and the client
that drives it in one release, so that mismatch is not reachable any more.

What is pinned here, with the Node runner stubbed so no browser is needed:

  - a run whose runner returns real violations flips the standards in scope to
    "Not compliant";
  - a run whose runner analyzes nothing reports "Not determined" - never a
    false green - which is the failure mode the mismatch produced;
  - first run installs the npm packages and Playwright's own Chromium, and says
    so in the scan log; a later run installs nothing;
  - the per-page contract: one Node process per URL, the result read off its
    stdout.
"""

import json
import subprocess
from pathlib import Path

import pytest

import accessibility_scan as a11y
from conftest import FakeRunner, axe_payload, violation


PAGES = ["https://x.test/", "https://x.test/about/"]

VIOLATIONS = [
    violation("image-alt", "critical",
              ["cat.text-alternatives", "wcag2a", "wcag111", "section508", "EN-301-549"],
              "Images must have alternate text", nodes=2,
              node_html='<img src="hero.png">'),
    violation("color-contrast", "serious", ["cat.color", "wcag2aa", "wcag143"],
              "Elements must meet minimum color contrast ratio thresholds", nodes=3),
]

STANDARDS = ["wcag21aa", "section508", "en301549", "atag20"]

GREEN_PHRASES = ("Compliant (automated checks)",
                 "No violations detected by the automated checks",
                 "Nothing flagged for manual review")


def scan(monkeypatch, stub, log=None):
    monkeypatch.setattr(a11y.subprocess, "run", stub)
    return a11y.scan_site("https://x.test", PAGES, standards=STANDARDS, concurrency=2,
                          log=log if log is not None else (lambda *_: None))


# --------------------------------------------------------------------------
# the runner returns real violations -> "Not compliant"
# --------------------------------------------------------------------------

def test_violations_from_the_node_runner_flip_the_standards_to_not_compliant(monkeypatch):
    logged = []
    stub = FakeRunner(payload_for=lambda url: axe_payload(
        url, violations=VIOLATIONS, incomplete=[], passes=37))
    report = scan(monkeypatch, stub, log=logged.append)

    panel = report.split("Standards &mdash; automated check results")[1] \
                  .split("Findings by impact")[0]
    # every applicable standard in scope sees image-alt (2.0 A) or color-contrast
    assert panel.count("Not compliant (automated)") == 3
    assert "Not determined" not in report
    assert "Compliant (automated checks)" not in report
    assert "image-alt" in panel and "color-contrast" in panel

    assert "2 of 2 page(s) analyzed" in report
    assert sorted(stub.urls) == PAGES
    assert any("2/2 page(s) analyzed" in line for line in logged)


def test_the_verdicts_are_computed_from_what_the_runner_actually_returned(monkeypatch,
                                                                          tmp_path):
    stub = FakeRunner(payload_for=lambda url: axe_payload(
        url, violations=VIOLATIONS, incomplete=[]))
    monkeypatch.setattr(a11y.subprocess, "run", stub)
    results = [a11y.run_axe(u, tmp_path, index=i)[0] for i, u in enumerate(PAGES, 1)]
    findings, _incomplete, engine = a11y.aggregate(results)
    verdicts = {r["id"]: r["verdict"]
                for r in a11y.evaluate_standards(findings, STANDARDS, pages_analyzed=2)}
    assert verdicts == {"wcag21aa": "fail", "section508": "fail",
                        "en301549": "fail", "atag20": "n/a"}
    assert engine == "4.10.2"          # the engine version the runner reported


# --------------------------------------------------------------------------
# the runner analyzes nothing -> "Not determined"
# --------------------------------------------------------------------------

BROWSER_MISSING = (
    "could not launch Playwright's Chromium: browserType.launch: "
    "Executable doesn't exist\nInstall the browser with: npx playwright install chromium"
)


def test_zero_analyzed_pages_report_not_determined_and_nothing_green(monkeypatch):
    logged = []
    stub = FakeRunner(emit_result=False, fail_output=BROWSER_MISSING, fail_code=1)
    report = scan(monkeypatch, stub, log=logged.append)

    panel = report.split("Standards &mdash; automated check results")[1] \
                  .split("Findings by impact")[0]
    assert panel.count("Not determined") == 3       # the three applicable standards
    assert "Not applicable" in panel                # ATAG 2.0, as always
    assert "Not compliant (automated)" not in report
    for phrase in GREEN_PHRASES:
        assert phrase not in report, phrase

    assert "0 of 2 page(s) analyzed" in report
    assert "npx playwright install chromium" in report       # the runner's own words
    assert any("SCAN FAILED - 0 of 2 page(s) analyzed" in line for line in logged)


def test_a_runner_that_prints_nothing_at_all_is_still_not_determined(monkeypatch):
    """The silent failure that started this: no output, no result, no pages -
    and still no standard allowed to read compliant."""
    stub = FakeRunner(emit_result=False, fail_output="", fail_code=1)
    report = scan(monkeypatch, stub)
    assert "Not determined" in report
    assert "the runner produced no output" in report
    for phrase in GREEN_PHRASES:
        assert phrase not in report, phrase


# --------------------------------------------------------------------------
# per-page contract + parallelism
# --------------------------------------------------------------------------

def test_one_node_process_per_page_carrying_the_selected_tag_set(monkeypatch):
    stub = FakeRunner()
    monkeypatch.setattr(a11y.subprocess, "run", stub)
    a11y.scan_site("https://x.test", [f"https://x.test/p{i}" for i in range(5)],
                   standards=["wcag20aaa"], concurrency=3, log=lambda *_: None)

    assert len(stub.calls) == 5
    assert sorted(stub.urls) == sorted(f"https://x.test/p{i}" for i in range(5))
    for call in stub.calls:
        cmd = call["cmd"]
        assert cmd[0] == a11y.NODE
        assert Path(cmd[1]).name == "axe_playwright_runner.js"
        tags = cmd[cmd.index("--tags") + 1].split(",")
        assert set(a11y.RUN_TAGS) <= set(tags)
        assert "wcag2aaa" in tags          # the selected standard's extra tag


def test_the_browser_is_prepared_once_per_scan_not_once_per_page(monkeypatch):
    """ensure_runner runs before the pool starts; the pages then only run axe."""
    stub = FakeRunner()
    monkeypatch.setattr(a11y.subprocess, "run", stub)
    a11y.scan_site("https://x.test", [f"https://x.test/p{i}" for i in range(5)],
                   concurrency=3, log=lambda *_: None)
    assert len(stub.probes) == 1


def test_the_runner_script_injects_axe_and_asks_for_the_tag_set():
    js = a11y.RUNNER_JS.read_text(encoding="utf-8")
    assert "@axe-core/playwright" in js
    assert "AxeBuilder" in js and "withTags(tags)" in js
    assert "analyze()" in js
    assert "chromium.launch" in js


# --------------------------------------------------------------------------
# reading the result off stdout
# --------------------------------------------------------------------------

def test_the_marked_payload_survives_noise_on_stdout():
    payload = axe_payload("https://x.test/")
    stdout = ("Some dependency said something\n"
              f"{a11y.RESULT_MARKER}{json.dumps(payload)}\n")
    data, leftover = a11y.parse_runner_output(stdout)
    assert data["url"] == "https://x.test/"
    assert leftover == "Some dependency said something"


def test_a_bare_json_document_is_accepted_too():
    data, leftover = a11y.parse_runner_output(json.dumps(axe_payload("https://x.test/")))
    assert data["violations"] and leftover == ""


def test_stdout_without_a_result_is_kept_as_diagnostics():
    data, leftover = a11y.parse_runner_output("Error: something went wrong")
    assert data is None and leftover == "Error: something went wrong"
    assert a11y.parse_runner_output("") == (None, "")


# --------------------------------------------------------------------------
# first-run setup: npm packages + Playwright's own Chromium
# --------------------------------------------------------------------------

class FakeSetup:
    """Answers ensure_runner's setup commands the way a machine with nothing
    installed would: `npm install` unpacks the packages, and the browser probe
    finds nothing until `npx playwright install chromium` has run."""

    def __init__(self, home, chromium="/pw/chromium", npm_code=0, browser_code=0):
        self.home = Path(home)
        self.cmds = []
        self.chromium = chromium
        self.npm_code = npm_code
        self.browser_code = browser_code
        self.browser_installed = False

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        self.cmds.append(cmd)
        if cmd[0] == a11y.NPM:
            if self.npm_code == 0:
                for package in a11y.RUNNER_DEPS:
                    (self.home / "node_modules" / package).mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(cmd, self.npm_code,
                                               stdout="npm said so", stderr="")
        if cmd[0] == a11y.NODE and "-e" in cmd:                  # the browser probe
            found = self.chromium if self.browser_installed else ""
            return subprocess.CompletedProcess(cmd, 0, stdout=found, stderr="")
        if cmd[0] == a11y.NPX:                                   # playwright install
            self.browser_installed = self.browser_code == 0
            return subprocess.CompletedProcess(cmd, self.browser_code,
                                               stdout="playwright said so", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def ran(self, exe):
        return [c for c in self.cmds if c[0] == exe]


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """A machine that has never run the scan: no packages, no browser."""
    home = tmp_path / "fresh"
    monkeypatch.setenv("A11Y_RUNNER_HOME", str(home))
    return home


def test_first_run_installs_the_packages_and_playwrights_chromium(fresh_home, monkeypatch):
    setup = FakeSetup(fresh_home)
    monkeypatch.setattr(a11y.subprocess, "run", setup)
    logged = []
    runner = a11y.ensure_runner(log=logged.append)

    assert setup.ran(a11y.NPM), "npm install never ran"
    assert "install" in setup.ran(a11y.NPM)[0]
    deps = json.loads((fresh_home / "package.json").read_text(encoding="utf-8"))["dependencies"]
    assert deps == dict(a11y.RUNNER_DEPS)

    assert [c[-3:] for c in setup.ran(a11y.NPX)] == [["playwright", "install", "chromium"]]
    assert a11y.deps_installed(fresh_home)
    assert runner.env["NODE_PATH"].startswith(str(fresh_home / "node_modules"))

    # and the scan log says what was installed, so a slow first run is explained
    assert any("installing the runner's npm packages" in line for line in logged)
    assert any("npx playwright install chromium" in line for line in logged)
    assert any("Chromium installed (/pw/chromium)" in line for line in logged)


def test_a_later_run_installs_nothing(runner_home, monkeypatch):
    """The autouse home already has its packages; with the browser in place too,
    a scan starts straight away."""
    setup = FakeSetup(runner_home)
    setup.browser_installed = True
    monkeypatch.setattr(a11y.subprocess, "run", setup)
    logged = []
    a11y.ensure_runner(log=logged.append)

    assert setup.ran(a11y.NPM) == [] and setup.ran(a11y.NPX) == []
    assert any("chromium /pw/chromium" in line for line in logged)


def test_a_failed_install_warns_and_lets_the_scan_report_its_real_error(fresh_home,
                                                                       monkeypatch):
    """An offline machine must not crash the scan - it must produce a run that
    analyzed nothing, which the report then states as "Not determined"."""
    setup = FakeSetup(fresh_home, npm_code=1, browser_code=1)
    monkeypatch.setattr(a11y.subprocess, "run", setup)
    logged = []
    runner = a11y.ensure_runner(log=logged.append)

    assert isinstance(runner, a11y.Runner)          # no exception, scan continues
    assert any("WARNING" in line and "npm packages failed" in line for line in logged)
    assert any("WARNING" in line and "could not install Playwright's Chromium" in line
               for line in logged)
    assert any("npm: npm said so" in line for line in logged)


def test_the_runner_home_is_overridable_and_kept_out_of_the_repo(fresh_home):
    assert a11y.runner_home() == fresh_home
    assert a11y.RUNNER_JS.parent not in a11y.runner_home().parents
