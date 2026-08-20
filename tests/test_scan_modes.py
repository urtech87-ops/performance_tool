"""What a run actually produces for each combination of selected categories.

The two "no Lighthouse category" modes - security-only and
accessibility-standards-only - must finish without ever starting the
Lighthouse deep audit and without a performance report, while still writing
their own page into the run folder. Both subprocesses (lighthouse and the axe
CLI) are stubbed, so nothing here needs Node or a browser.
"""

import json
import subprocess
from pathlib import Path

import pytest

import accessibility_scan as a11y
import dashboard

from conftest import FakeRunner


ROUTES = [{"path": "/"}, {"path": "/about/"}]


@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    d = tmp_path / "runs"
    d.mkdir()
    monkeypatch.setattr(dashboard, "RUNS_DIR", d)
    return d


@pytest.fixture
def crawl(monkeypatch):
    """A crawl that discovers ROUTES without running unlighthouse."""
    monkeypatch.setattr(dashboard, "DRYRUN", False)

    def fake_run_stream(cmd, cwd, log, env=None):
        d = Path(cwd) / ".unlighthouse"
        d.mkdir(parents=True, exist_ok=True)
        (d / "ci-result.json").write_text(json.dumps({"routes": ROUTES}), encoding="utf-8")
        return 0

    monkeypatch.setattr(dashboard, "run_stream", fake_run_stream)


class Subprocesses:
    """One stub for both runners: everything Node starts is the accessibility
    runner and is served by FakeRunner, lighthouse calls are recorded (and
    write the report file the real CLI would)."""

    def __init__(self):
        self.axe = FakeRunner()
        self.lighthouse = []

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        if Path(str(cmd[0])).name in ("node", "node.exe"):   # the axe runner
            return self.axe(cmd, **kwargs)
        self.lighthouse.append(cmd)               # lighthouse
        out = next((a for a in cmd if a.startswith("--output-path=")), None)
        if out:
            stem = Path(out.split("=", 1)[1])
            stem.parent.mkdir(parents=True, exist_ok=True)
            stem.with_name(stem.name + ".report.json").write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)

    @property
    def only_categories(self):
        return [a for c in self.lighthouse for a in c if a.startswith("--only-categories=")]


@pytest.fixture
def procs(monkeypatch):
    stub = Subprocesses()
    monkeypatch.setattr(subprocess, "run", stub)
    return stub


@pytest.fixture
def fake_security(monkeypatch):
    """The security scan does live HTTP - stub it, as the existing tests do."""
    import security_scan
    calls = []

    def fake_scan_site(site_url, urls, log=print):
        calls.append(list(urls))
        return "<html><body>stub security report</body></html>"

    monkeypatch.setattr(security_scan, "scan_site", fake_scan_site)
    return calls


@pytest.fixture
def perf_report(monkeypatch):
    """Keep the consolidated-report builder off the real loaders/Chrome."""
    monkeypatch.setattr(dashboard.cr, "load_ci_results",
                        lambda roots: [{"path": "/", "device": "desktop", "issues": None}])
    monkeypatch.setattr(dashboard.cr, "load_lhr_pages", lambda roots: [])
    monkeypatch.setattr(dashboard.cr, "build_html",
                        lambda pages, roots, out_path=None, categories=None:
                        "<html><body>perf report</body></html>")
    monkeypatch.setattr(dashboard.cr, "html_to_pdf",
                        lambda html_path, pdf_path: open(pdf_path, "w").write("%PDF-1.4"))


def pipeline(**over):
    """run_pipeline with the dashboard's own defaults; override per test."""
    kw = dict(url="https://x.test", device="desktop", samples=1, deep=True, max_pages=30,
              concurrency=3, security=False, categories=[], log=lambda *_: None,
              a11y=False, standards=None)
    kw.update(over)
    return dashboard.run_pipeline(kw.pop("url"), kw.pop("device"), kw.pop("samples"),
                                  kw.pop("deep"), kw.pop("max_pages"), kw.pop("concurrency"),
                                  kw.pop("security"), kw.pop("categories"), kw.pop("log"),
                                  **kw)


# --------------------------------------------------------------------------
# accessibility-standards only
# --------------------------------------------------------------------------

def test_accessibility_only_writes_its_report_without_a_deep_audit(runs_dir, crawl, procs):
    logged = []
    name = pipeline(categories=[], a11y=True, standards=["wcag21aa", "section508"],
                    log=logged.append)
    run = runs_dir / name

    assert procs.lighthouse == []                     # no Lighthouse deep audit
    assert not (run / "lhr").exists()
    assert not (run / "report.html").exists()         # and no performance report
    assert not (run / "report.pdf").exists()

    page = (run / "accessibility.html").read_text(encoding="utf-8")
    assert "axe-core" in page                         # a real report, not a stub
    # the pages are audited in parallel, so compare the set, not the order
    assert sorted(procs.axe.urls) == ["https://x.test/", "https://x.test/about/"]
    assert any("skipping the deep audit" in line for line in logged)


def test_accessibility_only_run_reports_done_with_its_file(runs_dir, crawl, procs, client):
    """The whole /scan round trip: an accessibility-only run ends in a `done`
    event naming the run folder and the accessibility report inside it."""
    body = client.get("/scan?url=https://x.test&categories=&a11y=1"
                      "&standards=wcag21aa").get_data(as_text=True)
    payload = json.loads(body.split("event: done\ndata: ")[1].split("\n")[0])
    assert payload["files"] == ["accessibility.html"]
    assert payload["base"].startswith("/runs/") and payload["base"].endswith("/")
    assert procs.lighthouse == []


# --------------------------------------------------------------------------
# security only (unchanged behaviour - guarded here too)
# --------------------------------------------------------------------------

def test_security_only_writes_its_report_without_a_deep_audit(runs_dir, crawl, procs,
                                                              fake_security):
    name = pipeline(categories=[], security=True)
    run = runs_dir / name

    assert procs.lighthouse == []
    assert not (run / "report.html").exists()
    assert not (run / "report.pdf").exists()
    assert (run / "security.html").read_text() == "<html><body>stub security report</body></html>"
    assert fake_security == [["https://x.test/", "https://x.test/about/"]]
    assert not (run / "accessibility.html").exists()


def test_security_and_accessibility_together_skip_lighthouse(runs_dir, crawl, procs,
                                                             fake_security):
    name = pipeline(categories=[], security=True, a11y=True, standards=["ada"])
    run = runs_dir / name
    assert procs.lighthouse == []
    assert (run / "security.html").exists()
    assert (run / "accessibility.html").exists()
    assert not (run / "report.html").exists()


# --------------------------------------------------------------------------
# a Lighthouse category still behaves exactly as before
# --------------------------------------------------------------------------

def test_a_lighthouse_category_still_deep_audits_and_builds_the_report(runs_dir, crawl, procs,
                                                                      perf_report):
    name = pipeline(categories=["performance", "seo"])
    run = runs_dir / name
    assert len(procs.lighthouse) == len(ROUTES)
    assert procs.only_categories == ["--only-categories=performance,seo"] * len(ROUTES)
    assert (run / "report.html").read_text() == "<html><body>perf report</body></html>"
    assert (run / "report.pdf").exists()


def test_a_lighthouse_category_with_accessibility_produces_both(runs_dir, crawl, procs,
                                                                perf_report):
    name = pipeline(categories=["accessibility"], a11y=True, standards=["wcag21aa"])
    run = runs_dir / name
    assert len(procs.lighthouse) == len(ROUTES)       # the deep audit still runs
    assert (run / "report.html").exists()
    assert (run / "accessibility.html").exists()
    assert procs.axe.urls                              # axe ran as well


def test_deep_audit_is_off_when_no_lighthouse_category_is_selected_even_with_deep(
        runs_dir, crawl, procs, perf_report):
    """`deep` alone must not start Lighthouse - the categories decide."""
    name = pipeline(categories=[], deep=True, a11y=True, standards=["ada"])
    assert procs.lighthouse == []
    assert not (runs_dir / name / "report.html").exists()


# --------------------------------------------------------------------------
# the done payload
# --------------------------------------------------------------------------

@pytest.fixture
def client():
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


def test_run_artifacts_lists_only_files_that_exist(runs_dir):
    run = runs_dir / "demo"
    run.mkdir()
    assert dashboard.run_artifacts("demo") == []
    (run / "accessibility.html").write_text("x")
    (run / "report.html").write_text("x")
    assert dashboard.run_artifacts("demo") == ["report.html", "accessibility.html"]


def test_done_event_carries_the_base_and_the_files(runs_dir, client, monkeypatch):
    def fake_pipeline(*a, **kw):
        run = runs_dir / "run-folder"
        run.mkdir()
        (run / "security.html").write_text("x")
        return "run-folder"

    monkeypatch.setattr(dashboard, "run_pipeline", fake_pipeline)
    body = client.get("/scan?url=https://x.test&security=1&categories=").get_data(as_text=True)
    payload = json.loads(body.split("event: done\ndata: ")[1].split("\n")[0])
    assert payload == {"base": "/runs/run-folder/", "files": ["security.html"]}


def test_a_failed_run_still_reports_fail(client, monkeypatch):
    monkeypatch.setattr(dashboard, "run_pipeline", lambda *a, **kw: None)
    body = client.get("/scan?url=https://x.test&categories=performance").get_data(as_text=True)
    assert "event: fail" in body


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

def test_ui_picks_the_primary_report_by_priority():
    page = dashboard.PAGE
    assert 'if(run.anyLH && has("report.html"))' in page
    assert 'else if(has("accessibility.html"))' in page
    assert 'else if(has("security.html"))' in page


def test_ui_only_shows_buttons_for_files_that_exist():
    page = dashboard.PAGE
    for link, name in [("pdflink", "report.pdf"), ("seclink", "security.html"),
                       ("a11ylink", "accessibility.html")]:
        assert f'showLink("{link}", has("{name}")' in page


def test_ui_blocks_a_second_scan_while_one_runs():
    page = dashboard.PAGE
    assert "var scanning = false;" in page
    assert "if(scanning){ return; }" in page
    assert "setBusy(true);" in page
    # re-enabled on every terminal event
    for evt in ('addEventListener("done"', 'addEventListener("fail"', "es.onerror"):
        assert evt in page
    assert page.count("setBusy(false)") >= 3
