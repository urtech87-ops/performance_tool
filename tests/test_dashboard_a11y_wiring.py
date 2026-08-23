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
    """Record what run_pipeline hands to accessibility_scan.collect_site.

    The pipeline collects the axe results as data (so the consolidated report
    can include them) and renders the standalone page from that same data, so
    both halves are stubbed here.
    """
    calls = []

    def fake_collect_site(site_url, urls, standards=None, concurrency=3, work_dir=None, log=print):
        calls.append({"site_url": site_url, "urls": list(urls), "standards": standards,
                      "concurrency": concurrency, "work_dir": work_dir})
        log("stub accessibility scan")
        return {"site_url": site_url, "panel": [], "violations": [], "incomplete": [],
                "pages_analyzed": len(urls), "pages_attempted": len(urls),
                "engine_version": "4.13.0", "tags": [], "failures": [],
                "checks_passed": 0, "standards": standards}

    monkeypatch.setattr(a11y, "collect_site", fake_collect_site)
    monkeypatch.setattr(a11y, "html_from",
                        lambda data: "<html><body>stub accessibility report</body></html>")
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

    def fake_run_stream(cmd, cwd, log, env=None, **kw):
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
    monkeypatch.setattr(a11y, "collect_site", boom)
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
    monkeypatch.setattr(security_scan, "collect_site",
                        lambda site, urls, log=print: sec_calls.append(list(urls)) or
                        {"site_url": site, "hits": {}, "scanned": len(urls),
                         "total": len(urls), "cert_note": ""})
    monkeypatch.setattr(security_scan, "html_from", lambda data: "<html>sec</html>")
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
