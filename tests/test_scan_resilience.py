"""A scan that goes wrong still has to produce something honest.

Three things are guarded here, and nothing else:

  * a run where some pages fail delivers a report for the pages that did not,
    labelled with the coverage it actually has;
  * a site that refuses automated scans outright produces a plain-language
    state, not a stack trace and not a silent empty report;
  * a run that hits its time ceiling finalizes as `partial` with whatever
    completed, instead of holding a worker until the queue kills it.

Every subprocess is stubbed, so none of this needs Node, Chrome or a network.
"""

import json
import subprocess
from pathlib import Path

import pytest

import consolidate_report as cr
import dashboard
import runstate

from conftest import FakeRunner


ROUTES = [{"path": f"/p{i}/"} for i in range(5)]
URLS = [f"https://x.test/p{i}/" for i in range(5)]


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    d = tmp_path / "runs"
    d.mkdir()
    monkeypatch.setattr(dashboard, "RUNS_DIR", d)
    return d


@pytest.fixture(autouse=True)
def reachable_site(monkeypatch):
    """By default the site answers - the tests that care say otherwise."""
    monkeypatch.setattr(runstate, "preflight",
                        lambda url, timeout=10, opener=None:
                        {"kind": None, "code": 200, "detail": ""})


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


@pytest.fixture
def empty_crawl(monkeypatch):
    """A crawl that discovers nothing at all - a blocked crawler."""
    monkeypatch.setattr(dashboard, "DRYRUN", False)
    monkeypatch.setattr(dashboard, "run_stream", lambda cmd, cwd, log, env=None: 1)


class Lighthouse:
    """A stubbed `lighthouse` CLI whose behaviour is set per URL.

    `outcomes` maps a URL to a list of what each successive attempt does:
    "ok" writes the report file, "timeout" raises TimeoutExpired, and anything
    else is treated as the CLI's own output on a run that wrote no report.
    Anything not named succeeds.
    """

    def __init__(self, outcomes=None, on_call=None):
        self.outcomes = {url: list(v) for url, v in (outcomes or {}).items()}
        self.calls = []
        self.on_call = on_call

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        if Path(str(cmd[0])).name in ("node", "node.exe"):      # the axe runner
            return FakeRunner()(cmd, **kwargs)
        url = next(a for a in cmd if str(a).startswith("http"))
        self.calls.append(url)
        if self.on_call:
            self.on_call(url)
        step = "ok"
        queue = self.outcomes.get(url)
        if queue:
            step = queue.pop(0)
        if step == "timeout":
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))
        if step == "ok":
            out = next(a for a in cmd if a.startswith("--output-path="))
            stem = Path(out.split("=", 1)[1])
            stem.parent.mkdir(parents=True, exist_ok=True)
            stem.with_name(stem.name + ".report.json").write_text("{}", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout=step, stderr="")

    def attempts(self, url):
        return self.calls.count(url)


@pytest.fixture
def lighthouse(monkeypatch):
    def install(outcomes=None, on_call=None):
        stub = Lighthouse(outcomes, on_call)
        monkeypatch.setattr(subprocess, "run", stub)
        return stub
    return install


@pytest.fixture
def perf_report(monkeypatch):
    """Keep the report builder off the real loaders and Chrome, and record the
    coverage it is handed."""
    seen = {}
    monkeypatch.setattr(cr, "load_ci_results",
                        lambda roots: [{"path": u, "device": "desktop", "issues": None,
                                        "scores": {}, "metrics": {}} for u in URLS])
    monkeypatch.setattr(cr, "load_lhr_pages", lambda roots: [])

    def fake_build(pages, roots, **kw):
        seen.update(kw)
        seen["pages"] = list(pages)
        return "<html><body>perf report</body></html>"

    monkeypatch.setattr(cr, "build_html", fake_build)
    monkeypatch.setattr(cr, "html_to_pdf",
                        lambda html_path, pdf_path, **kw:
                        (Path(pdf_path).write_text("%PDF-1.4", encoding="utf-8"),
                         {"engine": "stub", "polyfill": None})[1])
    return seen


def pipeline(**over):
    """run_pipeline with the dashboard's own defaults; override per test."""
    kw = dict(url="https://x.test", device="desktop", samples=1, deep=True, max_pages=30,
              concurrency=3, security=False, categories=["performance"],
              log=lambda *_: None, a11y=False, standards=None)
    kw.update(over)
    return dashboard.run_pipeline(kw.pop("url"), kw.pop("device"), kw.pop("samples"),
                                  kw.pop("deep"), kw.pop("max_pages"), kw.pop("concurrency"),
                                  kw.pop("security"), kw.pop("categories"), kw.pop("log"),
                                  **kw)


def ledger(runs_dir, name):
    saved = runstate.RunState.load(runs_dir / name)
    assert saved is not None, "the run wrote no status ledger"
    return saved


def status_of(saved, url):
    page = next(p for p in saved["pages"] if p["url"] == url)
    return page["status"]


# --------------------------------------------------------------------------
# 1. some pages fail -> a partial report, with the coverage it really has
# --------------------------------------------------------------------------

def test_pages_that_fail_do_not_take_the_report_with_them(runs_dir, crawl, lighthouse,
                                                          perf_report):
    """Two of five pages fail: the report still covers the other three and says
    so, instead of the run producing nothing."""
    lighthouse({URLS[3]: ["Runtime error: Status code: 403", "x"],
                URLS[4]: ["timeout", "timeout"]})
    logged = []
    name = pipeline(log=logged.append)

    assert (runs_dir / name / "report.html").exists()          # a report, not a failure

    saved = ledger(runs_dir, name)
    assert saved["status"] == "partial"
    assert saved["coverage"]["ok"] == 3
    assert saved["coverage"]["attempted"] == 5
    assert saved["coverage"]["percent"] == 60
    assert status_of(saved, URLS[3]) == "blocked"
    assert status_of(saved, URLS[4]) == "timeout"
    assert [status_of(saved, u) for u in URLS[:3]] == ["ok", "ok", "ok"]

    # and the report is told, so the coverage is on its cover, not only in a log
    coverage = perf_report["coverage"]
    assert coverage["status"] == "partial"
    assert (coverage["pages_ok"], coverage["pages_attempted"]) == (3, 5)
    assert {f["url"] for f in coverage["failures"]} == {URLS[3], URLS[4]}

    assert any("PARTIAL" in line for line in logged)
    assert any("unmeasured, not clean" in line for line in logged)


def test_the_report_can_be_rebuilt_from_the_persisted_ledger(runs_dir, crawl, lighthouse,
                                                             perf_report):
    """The ledger is on disk, so a report can be built from partial data later -
    including from a run whose worker died before it finished."""
    lighthouse({URLS[2]: ["Status code: 403", "x"]})
    name = pipeline()

    coverage = cr.run_coverage([runs_dir / name])
    assert coverage["status"] == "partial"
    assert coverage["pages_ok"] == 4 and coverage["pages_attempted"] == 5
    assert coverage["failures"][0]["url"] == URLS[2]
    # also reachable from the folder the scanners write into
    assert cr.run_coverage([runs_dir / name / ".unlighthouse"])["pages_ok"] == 4


def test_a_clean_run_is_labelled_complete(runs_dir, crawl, lighthouse, perf_report):
    lighthouse()
    name = pipeline()
    saved = ledger(runs_dir, name)
    assert saved["status"] == "complete"
    assert saved["coverage"] == {"stage": "lighthouse", "ok": 5, "attempted": 5,
                                 "failed": 0, "percent": 100, "by_status": {"ok": 5}}
    assert saved["notice"] is None
    assert perf_report["coverage"]["status"] == "complete"


def test_the_ledger_survives_every_stage(runs_dir, crawl, lighthouse, perf_report):
    lighthouse()
    name = pipeline(security=False)
    saved = ledger(runs_dir, name)
    assert saved["stages"]["crawl"]["state"] == "ok"
    assert saved["stages"]["lighthouse"]["state"] == "ok"
    # every crawled page is recorded against both stages it went through
    assert {p["url"] for p in saved["pages"]} == set(URLS)
    assert all(set(p["stages"]) == {"crawl", "lighthouse"} for p in saved["pages"])


# --------------------------------------------------------------------------
# 2. retries: bounded, and never for a refusal
# --------------------------------------------------------------------------

def test_a_transient_failure_gets_one_retry(runs_dir, crawl, lighthouse, perf_report):
    stub = lighthouse({URLS[1]: ["timeout", "ok"]})
    name = pipeline()
    assert stub.attempts(URLS[1]) == 2
    saved = ledger(runs_dir, name)
    assert saved["status"] == "complete"
    assert status_of(saved, URLS[1]) == "ok"


def test_a_transient_failure_is_retried_only_once(runs_dir, crawl, lighthouse, perf_report):
    stub = lighthouse({URLS[1]: ["timeout", "timeout", "ok"]})
    name = pipeline()
    assert stub.attempts(URLS[1]) == 2                  # not three, not forever
    assert status_of(ledger(runs_dir, name), URLS[1]) == "timeout"


def test_a_blocked_page_is_never_retried(runs_dir, crawl, lighthouse, perf_report):
    """Asking a second time cannot change a 403 - and it costs the run budget."""
    stub = lighthouse({URLS[2]: ["Runtime error encountered: Status code: 403", "ok"]})
    name = pipeline()
    assert stub.attempts(URLS[2]) == 1
    assert status_of(ledger(runs_dir, name), URLS[2]) == "blocked"


def test_retries_can_be_switched_off(runs_dir, crawl, lighthouse, perf_report, monkeypatch):
    monkeypatch.setattr(dashboard.settings, "PAGE_RETRIES", 0)
    stub = lighthouse({URLS[1]: ["timeout", "ok"]})
    pipeline()
    assert stub.attempts(URLS[1]) == 1


# --------------------------------------------------------------------------
# 3. a site that blocks everything
# --------------------------------------------------------------------------

def test_a_fully_blocked_site_says_so_in_plain_language(runs_dir, empty_crawl, lighthouse,
                                                        perf_report, monkeypatch):
    """Nothing crawls, every audit is refused: the run finishes, says why in a
    sentence a person can act on, and never raises."""
    monkeypatch.setattr(runstate, "preflight",
                        lambda url, timeout=10, opener=None:
                        {"kind": "blocked", "code": 403,
                         "detail": "the site answered HTTP 403"})
    monkeypatch.setattr(cr, "load_ci_results", lambda roots: [])
    lighthouse({"https://x.test/": ["Runtime error: Status code: 403"] * 3})
    logged = []

    name = pipeline(log=logged.append)                    # no exception escapes

    saved = ledger(runs_dir, name)
    assert saved["status"] == "failed"
    assert saved["notice"]["kind"] == "blocked"
    assert saved["notice"]["title"] == "This site is blocking automated scans"
    assert "verify domain ownership" in saved["notice"]["message"]
    assert saved["coverage"]["ok"] == 0
    assert not (runs_dir / name / "report.html").exists()
    # the raw tool output is kept for the log, never shown in place of a reason
    assert any("lighthouse: Runtime error: Status code: 403" in line for line in logged)
    assert any("Run FAILED" in line for line in logged)


def test_a_blocked_site_still_reaches_the_browser_through_the_scan(runs_dir, crawl,
                                                                   lighthouse, perf_report,
                                                                   monkeypatch):
    """A pre-flight refusal is a signal, not a verdict: real Chrome may still be
    let through, so the scan runs and the pages have the last word."""
    monkeypatch.setattr(runstate, "preflight",
                        lambda url, timeout=10, opener=None:
                        {"kind": "blocked", "code": 403, "detail": "HTTP 403"})
    stub = lighthouse()
    name = pipeline()
    assert len(stub.calls) == len(ROUTES)
    assert ledger(runs_dir, name)["status"] == "complete"


def test_a_name_that_does_not_resolve_ends_the_run_early(runs_dir, empty_crawl,
                                                         lighthouse, monkeypatch):
    """There is nothing to scan, and no reason to spend a worker finding out."""
    monkeypatch.setattr(runstate, "preflight",
                        lambda url, timeout=10, opener=None:
                        {"kind": "dns", "code": None,
                         "detail": "URLError: [Errno -2] Name or service not known"})
    stub = lighthouse()
    logged = []
    name = pipeline(log=logged.append)

    assert stub.calls == []                               # nothing was scanned
    saved = ledger(runs_dir, name)
    assert saved["status"] == "failed"
    assert saved["notice"]["title"] == "We couldn't reach this site"
    assert any("We couldn't reach this site" in line for line in logged)


def test_the_done_payload_carries_the_run_status(runs_dir, crawl, lighthouse, perf_report):
    """What the browser is handed is what the banner is built from."""
    import tasks
    lighthouse({URLS[4]: ["Status code: 403"] * 2})
    params = {"url": "https://x.test", "device": "desktop", "samples": 1, "deep": True,
              "max_pages": 30, "concurrency": 3, "security": False,
              "categories": ["performance"], "a11y": False, "standards": None}
    payload = tasks.run_scan_job(params, lambda *_: None)

    assert payload["base"].startswith("/runs/")
    assert payload["status"] == "partial"
    assert payload["coverage"]["ok"] == 4
    assert payload["notice"]["title"]


# --------------------------------------------------------------------------
# 4. the run-level ceiling
# --------------------------------------------------------------------------

class Clock:
    """A clock the test moves by hand, so no test has to actually wait."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(runstate, "_now", c)
    return c


def test_the_run_budget_finalizes_as_partial(runs_dir, crawl, lighthouse, perf_report,
                                             clock, monkeypatch):
    """One slow site must not tie up a worker: the run stops handing out pages
    and finalizes with whatever completed."""
    monkeypatch.setattr(dashboard.settings, "RUN_BUDGET", 100)
    logged = []
    # every page takes 60s of the run's 100s ceiling
    lighthouse(on_call=lambda url: clock.advance(60))
    name = pipeline(concurrency=1, log=logged.append)

    saved = ledger(runs_dir, name)
    assert saved["status"] == "partial"
    assert saved["budget_hit"] is True
    assert saved["coverage"]["ok"] == 2                    # 2 pages fit, 3 did not
    assert saved["coverage"]["by_status"]["skipped"] == 3
    assert all(f["reason"] == "run time limit reached"
               for f in saved["failures"])
    assert saved["notice"]["kind"] == "timeout"
    assert saved["notice"]["title"] == "The scan timed out"
    assert (runs_dir / name / "report.html").exists()       # partial, but delivered
    assert any("Run time limit (100s) reached" in line for line in logged)
    assert perf_report["coverage"]["budget_hit"] is True


def test_the_budget_also_stops_the_optional_scans(runs_dir, crawl, lighthouse, perf_report,
                                                  clock, monkeypatch):
    monkeypatch.setattr(dashboard.settings, "RUN_BUDGET", 100)
    calls = []
    monkeypatch.setattr("security_scan.collect_site",
                        lambda *a, **kw: calls.append(a) or {})
    lighthouse(on_call=lambda url: clock.advance(60))
    logged = []
    name = pipeline(concurrency=1, security=True, log=logged.append)

    assert calls == []                                     # never started
    saved = ledger(runs_dir, name)
    assert saved["stages"]["security"]["state"] == "skipped"
    assert saved["status"] == "partial"
    assert any("skipping the security scan" in line for line in logged)


def test_a_page_timeout_is_bounded_by_what_is_left_of_the_budget(runs_dir, crawl,
                                                                 lighthouse, perf_report,
                                                                 clock, monkeypatch):
    """A page can never be given more time than the run itself has left."""
    monkeypatch.setattr(dashboard.settings, "RUN_BUDGET", 100)
    monkeypatch.setattr(dashboard, "PER_PAGE_TIMEOUT", 150)
    limits = []

    class Recorder(Lighthouse):
        def __call__(self, cmd, **kwargs):
            if Path(str(cmd[0])).name not in ("node", "node.exe"):
                limits.append(kwargs.get("timeout"))
                clock.advance(30)
            return super().__call__(cmd, **kwargs)

    stub = Recorder()
    monkeypatch.setattr(subprocess, "run", stub)
    pipeline(concurrency=1)

    assert limits[0] == 100                                # not the 150s page limit
    assert limits[1] == 70 and limits[2] == 40
    assert all(l <= 100 for l in limits)


# --------------------------------------------------------------------------
# 5. the optional scans record their pages too
# --------------------------------------------------------------------------

def test_the_security_scan_records_the_pages_it_could_not_fetch(runs_dir, crawl,
                                                                lighthouse, monkeypatch):
    monkeypatch.setattr("security_scan.collect_site",
                        lambda site, urls, log=print: {
                            "site_url": site, "hits": {}, "scanned": len(urls) - 1,
                            "total": len(urls), "cert_note": "",
                            "failures": [{"url": URLS[2],
                                          "why": "URLError: ECONNREFUSED"}]})
    monkeypatch.setattr("security_scan.html_from", lambda data: "<html>sec</html>")
    lighthouse()
    name = pipeline(categories=[], security=True, deep=False)

    saved = ledger(runs_dir, name)
    page = next(p for p in saved["pages"] if p["url"] == URLS[2])
    assert page["stages"]["security"]["status"] == "error"
    assert saved["status"] == "partial"


def test_the_accessibility_scan_records_the_pages_it_could_not_analyze(runs_dir, crawl,
                                                                       lighthouse,
                                                                       monkeypatch):
    monkeypatch.setattr("accessibility_scan.collect_site",
                        lambda site, urls, **kw: {
                            "site_url": site, "panel": [], "violations": [],
                            "incomplete": [], "pages_analyzed": len(urls) - 1,
                            "pages_attempted": len(urls), "engine_version": "4.13.0",
                            "tags": [], "checks_passed": 0, "standards": [],
                            "analyzed": list(urls[:-1]),
                            "failures": [{"url": urls[-1], "why": "timeout",
                                          "output": ""}]})
    monkeypatch.setattr("accessibility_scan.html_from", lambda data: "<html>a11y</html>")
    lighthouse()
    name = pipeline(categories=[], a11y=True, standards=["wcag21aa"], deep=False)

    saved = ledger(runs_dir, name)
    page = next(p for p in saved["pages"] if p["url"] == URLS[-1])
    assert page["stages"]["accessibility"]["status"] == "timeout"
    assert saved["status"] == "partial"


def test_a_scanner_that_falls_over_leaves_the_run_partial_not_silent(runs_dir, crawl,
                                                                     lighthouse,
                                                                     perf_report,
                                                                     monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("axe exploded")

    monkeypatch.setattr("accessibility_scan.collect_site", boom)
    lighthouse()
    logged = []
    name = pipeline(a11y=True, standards=["wcag21aa"], log=logged.append)

    saved = ledger(runs_dir, name)
    assert saved["status"] == "partial"
    assert saved["stages"]["accessibility"]["state"] == "failed"
    assert any("the accessibility scan failed" in n for n in saved["notes"])
    assert (runs_dir / name / "report.html").exists()      # the rest still ships


# --------------------------------------------------------------------------
# 6. what the person actually sees
# --------------------------------------------------------------------------

def test_the_ui_shows_a_state_banner_rather_than_the_log():
    page = dashboard.PAGE
    assert 'id="statebox"' in page
    assert "function showState(" in page
    for status in ("failed", "partial", "complete"):
        assert f'status === "{status}"' in page
    # the coverage is spelled out for the person, not left implied
    assert "Coverage: " in page
    assert "unmeasured, not clean" in page


def test_the_ui_keeps_the_log_but_collapses_it():
    page = dashboard.PAGE
    assert '<details class="logdetails" id="logdetails">' in page
    assert "Technical log &mdash; for advanced users" in page
    assert '<div id="log"></div>' in page
    assert 'document.getElementById("logdetails").open = false;' in page


def test_a_failed_job_is_explained_in_the_ui_not_dumped():
    page = dashboard.PAGE
    fail = page[page.index('addEventListener("fail"'):]
    assert "d.notice" in fail                     # the friendly banner
    assert 'showState("err"' in fail
    assert "logEl.textContent +=" in fail          # the raw reason is still kept


def test_a_dead_job_still_gets_a_sentence(runs_dir, monkeypatch):
    """A worker killed by the queue leaves no ledger - the stream still has to
    say something a person can act on."""
    dashboard.app.config["TESTING"] = True
    client = dashboard.app.test_client()

    def boom(*a, **kw):
        raise RuntimeError("scan exceeded the 900s limit")

    monkeypatch.setattr(dashboard, "run_pipeline", boom)
    body = client.get("/scan?url=https://x.test&categories=performance").get_data(as_text=True)
    payload = json.loads(body.split("event: fail\ndata: ")[1].split("\n")[0])
    assert payload["notice"]["title"] == "The scan timed out"
    assert payload["message"]                      # the raw reason is still there


# --------------------------------------------------------------------------
# 7. the report itself, rendered for real
# --------------------------------------------------------------------------

@pytest.fixture
def partial_report_html(tmp_path):
    """A real consolidated report for a run that only measured part of a site."""
    pages = [{"path": f"https://x.test/p{i}/", "device": "Desktop", "metrics": {},
              "scores": {"performance": 0.9}, "issues": [], "report_html": None}
             for i in range(39)]
    coverage = {
        "status": "partial", "pages_ok": 39, "pages_attempted": 55, "percent": 71,
        "budget_hit": True,
        "notes": ["the run reached its 900s time limit before every page was audited"],
        "failures": [{"url": "https://x.test/p40/", "status": "blocked",
                      "reason": "blocked"},
                     {"url": "https://x.test/p41/", "status": "timeout",
                      "reason": "timeout"}],
        "notice": runstate.friendly("timeout", "partial"),
    }
    return cr.build_html(pages, [tmp_path], out_path=str(tmp_path / "report.html"),
                         categories=["performance"], site_url="https://x.test",
                         device="desktop", coverage=coverage)


def test_a_partial_report_says_so_on_its_cover(partial_report_html):
    cover = partial_report_html[partial_report_html.index('class="cover"'):
                                partial_report_html.index('id="summary"')]
    assert "Partial coverage" in cover
    assert "39 of 55 page(s) measured (71%)" in cover
    assert "unmeasured, not clean" in cover


def test_a_partial_report_names_the_pages_it_missed(partial_report_html):
    method = partial_report_html[partial_report_html.index('id="method"'):]
    assert "Coverage: 39 of 55 page(s) measured (71%)" in method
    assert "https://x.test/p40/" in method and "Blocked" in method
    assert "https://x.test/p41/" in method and "Timed out" in method
    assert "900s time limit" in method
    # the discipline already in place is untouched
    assert "Automated testing only" in method
    assert "Nothing in this report is a statement of legal compliance" in method


def test_a_partial_report_states_its_coverage_in_the_scope_table(partial_report_html):
    method = partial_report_html[partial_report_html.index('id="method"'):]
    scope = method[method.index("Scope of this run"):]
    assert "Pages Measured" in scope and "Pages Attempted" in scope
    assert "Coverage" in scope and "71%" in scope
    assert "Run Status" in scope and "partial" in scope


def test_a_complete_run_is_not_shouted_about(tmp_path):
    """A clean run states its coverage without a warning banner it hasn't earned."""
    pages = [{"path": "https://x.test/", "device": "Desktop", "metrics": {},
              "scores": {"performance": 0.9}, "issues": [], "report_html": None}]
    html = cr.build_html(pages, [tmp_path], out_path=str(tmp_path / "report.html"),
                         categories=["performance"], site_url="https://x.test",
                         coverage={"status": "complete", "pages_ok": 1,
                                   "pages_attempted": 1, "percent": 100,
                                   "failures": [], "notes": []})
    assert '<div class="cover-flag">' not in html      # no warning it hasn't earned
    assert '<div class="coverage' not in html
    assert "Complete - 1 of 1 page(s) measured (100%)" in html


def test_a_report_built_without_a_ledger_is_unchanged(tmp_path):
    """Every existing caller passes no coverage at all - and gets what it always
    got."""
    pages = [{"path": "https://x.test/", "device": "Desktop", "metrics": {},
              "scores": {"performance": 0.9}, "issues": [], "report_html": None}]
    html = cr.build_html(pages, [tmp_path], out_path=str(tmp_path / "report.html"),
                         categories=["performance"])
    assert '<div class="cover-flag">' not in html
    assert "Run status" not in html and "Pages Measured" not in html
    assert "<!DOCTYPE html>" in html
