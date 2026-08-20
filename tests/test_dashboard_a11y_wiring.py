"""The accessibility module is wired into the dashboard the same way the
security scan is: optional, additive, and never disturbing the existing run."""

import re

import pytest

import accessibility_scan as a11y
import dashboard


@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    """Keep test runs out of the real runs/ folder."""
    d = tmp_path / "runs"
    d.mkdir()
    monkeypatch.setattr(dashboard, "RUNS_DIR", d)
    return d


@pytest.fixture
def no_crawl(monkeypatch):
    """Stub the unlighthouse crawl - these tests are about the a11y block."""
    monkeypatch.setattr(dashboard, "DRYRUN", False)
    monkeypatch.setattr(dashboard, "run_stream", lambda *a, **kw: 0)


@pytest.fixture
def captured_scan(monkeypatch):
    """Record what run_pipeline hands to accessibility_scan.scan_site."""
    calls = []

    def fake_scan_site(site_url, urls, standards=None, concurrency=3, work_dir=None, log=print):
        calls.append({"site_url": site_url, "urls": list(urls), "standards": standards,
                      "concurrency": concurrency, "work_dir": work_dir})
        log("stub accessibility scan")
        return "<html><body>stub accessibility report</body></html>"

    monkeypatch.setattr(a11y, "scan_site", fake_scan_site)
    return calls


# --------------------------------------------------------------------------
# run_pipeline
# --------------------------------------------------------------------------

def test_pipeline_writes_accessibility_html_when_enabled(runs_dir, no_crawl, captured_scan):
    name = dashboard.run_pipeline("https://x.test", "desktop", 1, False, 30, 3,
                                  False, [], lambda *_: None,
                                  a11y=True, standards=["wcag21aa", "section508"])
    out = runs_dir / name / "accessibility.html"
    assert out.read_text(encoding="utf-8") == "<html><body>stub accessibility report</body></html>"
    assert captured_scan[0]["standards"] == ["wcag21aa", "section508"]
    assert captured_scan[0]["work_dir"] == runs_dir / name / "axe"


def test_pipeline_skips_accessibility_by_default(runs_dir, no_crawl, captured_scan):
    name = dashboard.run_pipeline("https://x.test", "desktop", 1, False, 30, 3,
                                  False, [], lambda *_: None)
    assert not (runs_dir / name / "accessibility.html").exists()
    assert captured_scan == []


def test_pipeline_honours_max_pages_and_parallel(runs_dir, no_crawl, captured_scan, monkeypatch):
    routes = [{"path": f"/p{i}/"} for i in range(9)]

    def fake_run_stream(cmd, cwd, log, env=None):
        import json
        from pathlib import Path
        d = Path(cwd) / ".unlighthouse"
        d.mkdir(parents=True, exist_ok=True)
        (d / "ci-result.json").write_text(json.dumps({"routes": routes}), encoding="utf-8")
        return 0

    monkeypatch.setattr(dashboard, "run_stream", fake_run_stream)
    dashboard.run_pipeline("https://x.test", "desktop", 1, False, 4, 5,
                           False, [], lambda *_: None, a11y=True, standards=["ada"])
    call = captured_scan[0]
    assert call["urls"] == [f"https://x.test/p{i}/" for i in range(4)]  # max_pages
    assert call["concurrency"] == 5                                     # parallel


def test_pipeline_falls_back_to_the_entered_url_with_no_routes(runs_dir, no_crawl, captured_scan):
    dashboard.run_pipeline("https://x.test/landing", "desktop", 1, False, 30, 3,
                           False, [], lambda *_: None, a11y=True)
    assert captured_scan[0]["urls"] == ["https://x.test/landing"]


def test_a_failing_accessibility_scan_never_breaks_the_run(runs_dir, no_crawl, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("axe exploded")
    monkeypatch.setattr(a11y, "scan_site", boom)
    logged = []
    name = dashboard.run_pipeline("https://x.test", "desktop", 1, False, 30, 3,
                                  False, [], logged.append, a11y=True)
    assert name  # the run still completes
    assert any("Accessibility scan failed: axe exploded" in line for line in logged)
    assert not (runs_dir / name / "accessibility.html").exists()


def test_dryrun_fabricates_an_accessibility_report(runs_dir, monkeypatch):
    monkeypatch.setattr(dashboard, "DRYRUN", True)
    name = dashboard.run_pipeline("https://x.test", "desktop", 1, False, 30, 3,
                                  False, [], lambda *_: None, a11y=True)
    assert "dry-run accessibility" in (runs_dir / name / "accessibility.html").read_text()


def test_existing_reports_are_untouched_by_the_new_block(runs_dir, no_crawl, captured_scan,
                                                         monkeypatch):
    """Performance + security still behave exactly as before when a11y is on."""
    sec_calls = []
    import security_scan
    monkeypatch.setattr(security_scan, "scan_site",
                        lambda site, urls, log=print: sec_calls.append(urls) or "<html>sec</html>")
    monkeypatch.setattr(dashboard.cr, "load_ci_results", lambda roots: [])
    monkeypatch.setattr(dashboard.cr, "load_lhr_pages", lambda roots: [])
    name = dashboard.run_pipeline("https://x.test", "desktop", 1, False, 30, 3,
                                  True, ["performance"], lambda *_: None,
                                  a11y=True, standards=["ada"])
    run = runs_dir / name
    assert (run / "security.html").read_text() == "<html>sec</html>"
    assert (run / "accessibility.html").exists()
    assert sec_calls == [["https://x.test"]]


# --------------------------------------------------------------------------
# /scan request plumbing
# --------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


@pytest.fixture
def captured_pipeline(monkeypatch):
    calls = []

    def fake_pipeline(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return "run-folder"

    monkeypatch.setattr(dashboard, "run_pipeline", fake_pipeline)
    return calls


def test_scan_route_passes_a11y_and_standards_through(client, captured_pipeline):
    resp = client.get("/scan?url=https://x.test&a11y=1&standards=wcag21aa,section508"
                      "&categories=performance&concurrency=4")
    body = resp.get_data(as_text=True)
    assert "event: done" in body
    kwargs = captured_pipeline[0]["kwargs"]
    assert kwargs["a11y"] is True
    assert kwargs["standards"] == ["wcag21aa", "section508"]


def test_scan_route_defaults_a11y_off(client, captured_pipeline):
    client.get("/scan?url=https://x.test&categories=seo")
    assert captured_pipeline[0]["kwargs"]["a11y"] is False


def test_scan_route_sanitizes_standard_ids(client, captured_pipeline):
    client.get("/scan?url=https://x.test&a11y=1&standards=" +
               "WCAG21AA,../../etc/passwd,<script>,ada")
    stds = captured_pipeline[0]["kwargs"]["standards"]
    assert all(re.fullmatch(r"[a-z0-9_]+", s) for s in stds)
    assert "wcag21aa" in stds and "ada" in stds
    # only known ids survive normalization inside the module
    assert a11y.normalize_standards(stds) == ["wcag21aa", "ada"]


def test_scan_route_still_works_without_a11y_params(client, captured_pipeline):
    client.get("/scan?url=https://x.test&categories=performance,seo&security=1")
    args = captured_pipeline[0]["args"]
    assert args[6] is True                    # security
    assert args[7] == ["performance", "seo"]  # categories, canonical order


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

def test_ui_has_the_accessibility_standards_selector():
    page = dashboard.PAGE
    assert 'data-cat="a11y-standards"' in page
    assert 'id="stds"' in page
    for sid in [s["id"] for s in a11y.STANDARDS]:
        assert f'data-std="{sid}"' in page, sid


def test_ui_defaults_match_the_module_defaults():
    page = dashboard.PAGE
    on = set(re.findall(r'chip std on" data-std="([a-z0-9_]+)"', page))
    assert on == set(a11y.DEFAULT_STANDARDS)


def test_ui_keeps_the_existing_controls():
    page = dashboard.PAGE
    for cat in ("performance", "accessibility", "best-practices", "seo", "security"):
        assert f'data-cat="{cat}"' in page
    assert 'id="maxpages"' in page and 'id="concurrency"' in page and 'id="deep"' in page
    assert 'id="a11ylink"' in page and 'id="seclink"' in page


def test_ui_never_promises_legal_compliance():
    page = dashboard.PAGE
    assert "never a legal compliance verdict" in page


# --------------------------------------------------------------------------
# a finished report is opened, not re-scanned
# --------------------------------------------------------------------------
# Clicking "Generate report" after a scan finished used to start the whole scan
# again: the button had exactly one behaviour, and the finished run was not
# remembered. Now a completed run is kept with the settings that produced it,
# and the button opens it while those settings still match.

def test_ui_remembers_the_finished_run_with_the_settings_that_produced_it():
    page = dashboard.PAGE
    assert "var lastRun = null;" in page
    # the run is tagged with the query string that produced it ...
    assert "var run = { qs: f.qs, base: base, files: files," in page
    # ... and only becomes the remembered run once it has something to show
    assert "if(showResults(run)){" in page
    assert "lastRun = run;" in page


def test_ui_opens_the_existing_report_instead_of_scanning_again():
    page = dashboard.PAGE
    click = page.split("go.onclick = function(){")[1].split("};")[0]
    # the short-circuit comes before any scan is started
    assert "if(lastRun && lastRun.qs === f.qs){" in click
    assert click.index("showResults(lastRun)") < click.index("startScan(f)")
    assert "scrollIntoView" in click
    # and only startScan ever opens the stream that runs a scan
    assert "EventSource" not in click
    assert page.count("new EventSource(") == 1


def test_ui_offers_an_explicit_way_to_scan_again():
    page = dashboard.PAGE
    assert 'id="rescan"' in page
    assert "rescan.onclick = function(){" in page
    rescan = page.split("rescan.onclick = function(){")[1].split("};")[0]
    assert "startScan(f)" in rescan          # always a fresh scan, never the cache


def test_ui_button_says_what_it_will_do():
    page = dashboard.PAGE
    assert 'id="golabel"' in page
    assert 'goLabel.textContent = ready ? "View report" : "Generate report";' in page
    # changing anything in the form re-decides, so the label cannot go stale
    assert 'document.addEventListener(evt, refreshButton);' in page
    assert "lastRun = null;                      // the report on screen is being replaced" in page


def test_ui_keeps_the_icon_when_the_button_label_changes():
    """setBusy used to overwrite the button's textContent, which threw the SVG
    away on the first click; the label now lives in its own span."""
    page = dashboard.PAGE
    assert "go.textContent" not in page
    assert 'goLabel.textContent = "Scanning' in page


def test_the_scan_stream_tells_the_browser_not_to_reconnect(client, captured_pipeline):
    """EventSource retries a dropped stream by itself, and a connection to
    /scan starts a scan - so an unsuppressed retry re-runs the whole audit."""
    body = client.get("/scan?url=https://x.test&categories=performance").get_data(as_text=True)
    assert body.startswith("retry: ")
    assert int(body.split("retry: ")[1].split("\n")[0]) >= 3600000
    assert len(captured_pipeline) == 1
