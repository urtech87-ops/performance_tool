"""Blocked URLs and the DNS override: two more inputs to the scanners.

What is pinned here:

  1. a blocked pattern really stops the request - Chrome is told to block it
     for the Lighthouse audits and the crawl, and the Playwright runners abort
     it in the browsing context, which is checked by driving the interception
     code itself;
  2. a DNS override routes the scan - and only the scan - to the address it
     was given, through Chrome's own resolver rules, everywhere a browser is
     started;
  3. neither is on by default: with nothing configured the commands and the
     run folder are exactly what they were before any of this existed.

Nothing about scoring is touched. These are conditions the pages are measured
under; what the tools then report is their business.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import dashboard
import guardrails
import scanconfig
from config import settings

from conftest import FakeRunner

ROOT = Path(__file__).resolve().parent.parent
INTERCEPT_JS = ROOT / "scan_intercept.js"
ROUTES = [{"path": "/"}]

ADS = ["*doubleclick.net*", "*google-analytics.com*", "*/ads/*"]


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    d = tmp_path / "runs"
    d.mkdir()
    monkeypatch.setattr(dashboard, "RUNS_DIR", d)
    return d


@pytest.fixture
def crawl(monkeypatch):
    """Record the crawl's argv and the directory it ran in."""
    monkeypatch.setattr(dashboard, "DRYRUN", False)
    calls = []

    def fake_run_stream(cmd, cwd, log, env=None, **kw):
        calls.append({"cmd": list(cmd), "cwd": Path(cwd)})
        d = Path(cwd) / ".unlighthouse"
        d.mkdir(parents=True, exist_ok=True)
        (d / "ci-result.json").write_text(json.dumps({"routes": ROUTES}), encoding="utf-8")
        return 0

    monkeypatch.setattr(dashboard, "run_stream", fake_run_stream)
    return calls


class Subprocesses:
    """Node calls are the Playwright runners; everything else is lighthouse."""

    def __init__(self):
        self.axe = FakeRunner()
        self.lighthouse = []
        self.node = []

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        if Path(str(cmd[0])).name in ("node", "node.exe"):
            self.node.append({"cmd": cmd, "input": kwargs.get("input")})
            if "login_playwright_runner.js" in " ".join(cmd):
                import scanlogin
                payload = json.dumps({"url": "https://x.test/", "cookies": []})
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=f"{scanlogin.RESULT_MARKER}{payload}\n", stderr="")
            return self.axe(cmd, **kwargs)
        self.lighthouse.append(cmd)
        out = next((a for a in cmd if a.startswith("--output-path=")), None)
        if out:
            stem = Path(out.split("=", 1)[1])
            stem.parent.mkdir(parents=True, exist_ok=True)
            stem.with_name(stem.name + ".report.json").write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)

    @property
    def page_runs(self):
        return [c for c in self.node if "--url" in c["cmd"]]

    def chrome_flags(self):
        return next(a for a in self.lighthouse[0] if a.startswith("--chrome-flags="))


@pytest.fixture
def procs(monkeypatch):
    stub = Subprocesses()
    monkeypatch.setattr(subprocess, "run", stub)
    import accessibility_scan
    monkeypatch.setattr(accessibility_scan.subprocess, "run", stub)
    return stub


@pytest.fixture
def report(monkeypatch):
    monkeypatch.setattr(dashboard.cr, "load_ci_results", lambda roots: [])
    monkeypatch.setattr(dashboard.cr, "load_lhr_pages", lambda roots: [])
    monkeypatch.setattr(dashboard.cr, "build_html", lambda pages, roots, **kw: "<html></html>")


def pipeline(**over):
    kw = dict(url="https://x.test", device="desktop", samples=1, deep=True, max_pages=30,
              concurrency=1, security=False, categories=["performance"],
              log=lambda *_: None)
    kw.update(over)
    return dashboard.run_pipeline(kw.pop("url"), kw.pop("device"), kw.pop("samples"),
                                  kw.pop("deep"), kw.pop("max_pages"), kw.pop("concurrency"),
                                  kw.pop("security"), kw.pop("categories"), kw.pop("log"),
                                  **kw)


def crawl_config(runs_dir):
    """The config file the crawl was handed, parsed - or None."""
    files = list(Path(runs_dir).rglob(dashboard.CRAWL_CONFIG))
    if not files:
        return None
    text = files[0].read_text(encoding="utf-8")
    return json.loads(text.split("=", 1)[1].strip().rstrip(";"))


# --------------------------------------------------------------------------
# nothing configured changes nothing
# --------------------------------------------------------------------------

def test_an_unblocked_scan_says_nothing_about_blocking_or_dns(runs_dir, crawl, procs, report):
    pipeline()
    lh = procs.lighthouse[0]
    assert not [a for a in lh if a.startswith("--blocked-url-patterns")]
    assert "host-resolver-rules" not in procs.chrome_flags()
    assert crawl_config(runs_dir) is None, "an ordinary crawl gets no config file"


def test_a_config_with_neither_is_still_the_default():
    assert scanconfig.ScanConfig().is_default
    assert not scanconfig.ScanConfig().browser_context()
    assert scanconfig.ScanConfig(block_patterns=ADS).is_default is False
    assert scanconfig.ScanConfig(dns_ip="10.0.0.9").is_default is False


# --------------------------------------------------------------------------
# blocked URLs
# --------------------------------------------------------------------------

def test_every_block_pattern_reaches_lighthouse(runs_dir, crawl, procs, report):
    pipeline(scan_config=scanconfig.ScanConfig(block_patterns=ADS))
    lh = procs.lighthouse[0]
    for pattern in ADS:
        assert f"--blocked-url-patterns={pattern}" in lh


def test_the_crawl_is_handed_the_block_list_in_its_own_directory(runs_dir, crawl, procs, report):
    """`unlighthouse-ci` has no flag for this, but it reads a config file from
    the directory it runs in - which is the run folder."""
    pipeline(scan_config=scanconfig.ScanConfig(block_patterns=ADS))
    config = crawl_config(runs_dir)
    assert config["lighthouseOptions"]["blockedUrlPatterns"] == ADS
    assert (crawl[0]["cwd"] / dashboard.CRAWL_CONFIG).is_file()


def test_the_axe_runner_is_told_to_block_them_too(runs_dir, crawl, procs, report):
    pipeline(categories=[], deep=False, a11y=True, standards=["wcag21aa"],
             scan_config=scanconfig.ScanConfig(block_patterns=ADS))
    page = procs.page_runs[0]
    assert "--browser-stdin" in page["cmd"]
    assert json.loads(page["input"])["blockPatterns"] == ADS


def test_the_scripted_login_runs_under_the_same_block_list(runs_dir, crawl, procs, report):
    import scanauth

    creds = scanauth.Credentials(login=scanauth.FormLogin(
        url="https://x.test/login", user_selector="#u", pass_selector="#p",
        submit_selector="#go", username="someone", password="secret"))
    pipeline(credentials=creds, scan_config=scanconfig.ScanConfig(block_patterns=ADS))
    login = next(c for c in procs.node if "login_playwright_runner.js" in " ".join(c["cmd"]))
    assert json.loads(login["input"])["context"]["blockPatterns"] == ADS


def test_a_pattern_matches_the_way_lighthouse_matches():
    cfg = scanconfig.ScanConfig(block_patterns=["*doubleclick.net*", "/ads/", "*.gif"])
    assert cfg.blocked("https://ad.doubleclick.net/x.js")
    assert cfg.blocked("https://x.test/ads/banner.png")     # bare substring
    assert cfg.blocked("https://x.test/img/spacer.gif")
    assert cfg.blocked("https://X.test/ADS/banner.png")     # case-insensitive
    assert not cfg.blocked("https://x.test/js/app.js")
    # A pattern is a substring, not an anchored rule: `*.gif` is `.gif`
    # anywhere in the URL, which is what Lighthouse does with the same list.
    assert cfg.blocked("https://x.test/download.gifted")
    assert not scanconfig.ScanConfig().blocked("https://anything/at/all")


def test_a_pattern_is_only_ever_a_wildcard_never_a_regular_expression():
    cfg = scanconfig.ScanConfig(block_patterns=["a.c", "x+y"])
    assert cfg.blocked("https://s/a.c") and cfg.blocked("https://s/x+y")
    assert not cfg.blocked("https://s/abc")


def test_a_block_list_is_read_from_a_textarea_a_csv_or_a_list():
    lines = scanconfig.clean_patterns("*ads*\n*beacon*\n\n*ads*")
    assert lines == ("*ads*", "*beacon*")                 # blanks and repeats dropped
    assert scanconfig.clean_patterns("*ads*, *beacon*") == ("*ads*", "*beacon*")
    assert scanconfig.clean_patterns(["*ads*", " *beacon* "]) == ("*ads*", "*beacon*")


def test_a_block_list_is_bounded_and_carries_no_whitespace():
    cfg = scanconfig.ScanConfig(block_patterns=["*x*"] * 5 + [f"*p{i}*" for i in range(200)])
    assert len(cfg.block_patterns) == scanconfig.MAX_BLOCK_PATTERNS
    smuggled = scanconfig.ScanConfig(block_patterns=["*ads* --no-sandbox\n--foo"])
    assert smuggled.block_patterns == ("*ads*--no-sandbox--foo",)
    assert len(scanconfig.ScanConfig(block_patterns=["a" * 5000]).block_patterns[0]) \
        == scanconfig.MAX_PATTERN_LENGTH


# --------------------------------------------------------------------------
# blocking is really interception - the runners' own code says so
# --------------------------------------------------------------------------

HARNESS = """
const {installBlocking, blockMatcher, launchArgs} = require(process.argv[2]);
const patterns = JSON.parse(process.argv[3]);
const urls = JSON.parse(process.argv[4]);

(async () => {
  let registered = null;
  const context = {
    route: async (predicate, handler) => { registered = {predicate, handler}; },
  };
  await installBlocking(context, patterns);
  const results = [];
  for (const url of urls) {
    if (!registered) { results.push({url, intercepted: false, aborted: false}); continue; }
    const intercepted = registered.predicate(new URL(url));
    let aborted = false, loaded = false;
    if (intercepted) {
      await registered.handler({
        abort: async () => { aborted = true; },
        continue: async () => { loaded = true; },
      });
    }
    results.push({url, intercepted, aborted, loaded: !intercepted || loaded});
  }
  process.stdout.write(JSON.stringify({
    routed: !!registered, results, args: launchArgs(['--host-resolver-rules=MAP a 1.2.3.4']),
  }));
})();
"""

BLOCKED = ["https://ad.doubleclick.net/pixel.gif",
           "https://www.google-analytics.com/collect?v=1",
           "https://x.test/ads/leaderboard.js"]
ALLOWED = ["https://x.test/", "https://x.test/css/app.css",
           "https://cdn.x.test/hero.jpg"]


def run_intercept(tmp_path, patterns, urls):
    """Drive the runners' own interception code under Node."""
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness), str(INTERCEPT_JS), json.dumps(patterns), json.dumps(urls)],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


needs_node = pytest.mark.skipif(shutil.which("node") is None,
                                reason="the interception code is JavaScript")


@needs_node
def test_a_blocked_request_is_aborted_and_never_loads(tmp_path):
    out = run_intercept(tmp_path, ADS, BLOCKED + ALLOWED)
    by_url = {r["url"]: r for r in out["results"]}
    for url in BLOCKED:
        assert by_url[url]["intercepted"], url
        assert by_url[url]["aborted"], url
        assert not by_url[url]["loaded"], url


@needs_node
def test_everything_else_is_left_alone(tmp_path):
    out = run_intercept(tmp_path, ADS, BLOCKED + ALLOWED)
    by_url = {r["url"]: r for r in out["results"]}
    for url in ALLOWED:
        assert not by_url[url]["intercepted"], url
        assert not by_url[url]["aborted"], url
        assert by_url[url]["loaded"], url


@needs_node
def test_no_block_list_installs_no_route_at_all(tmp_path):
    out = run_intercept(tmp_path, [], BLOCKED)
    assert out["routed"] is False
    assert all(not r["aborted"] for r in out["results"])


@needs_node
def test_the_browser_and_python_agree_on_what_is_blocked(tmp_path):
    """The same list decides in both places - Chrome blocks these for the
    Lighthouse audits, Playwright aborts them for the axe run."""
    patterns = ADS + ["*.gif", "beacon"]
    urls = BLOCKED + ALLOWED + ["https://x.test/beacon", "https://x.test/a.gif"]
    out = run_intercept(tmp_path, patterns, urls)
    cfg = scanconfig.ScanConfig(block_patterns=patterns)
    for row in out["results"]:
        assert row["intercepted"] == cfg.blocked(row["url"]), row["url"]


@needs_node
def test_the_launch_flags_keep_the_relaxations_and_add_the_scans_own(tmp_path):
    out = run_intercept(tmp_path, [], [])
    assert "--no-sandbox" in out["args"]
    assert "--ignore-certificate-errors" in out["args"]
    assert "--host-resolver-rules=MAP a 1.2.3.4" in out["args"]


# --------------------------------------------------------------------------
# DNS override
# --------------------------------------------------------------------------

def test_the_override_reaches_chrome_as_a_resolver_rule(runs_dir, crawl, procs, report):
    pipeline(scan_config=scanconfig.ScanConfig(dns_host="staging.x.test", dns_ip="10.0.0.9"))
    flags = procs.chrome_flags()
    assert '--host-resolver-rules="MAP staging.x.test 10.0.0.9"' in flags
    # the flags Lighthouse has always been given are still there
    assert "--headless=new" in flags and "--ignore-certificate-errors" in flags


def test_an_address_on_its_own_maps_the_site_being_scanned(runs_dir, crawl, procs, report):
    """The common case: "test this site, but against that box"."""
    pipeline(url="https://Staging.X.test/path", scan_config=scanconfig.ScanConfig(dns_ip="10.0.0.9"))
    assert '--host-resolver-rules="MAP staging.x.test 10.0.0.9"' in procs.chrome_flags()


def test_the_crawl_and_the_runners_resolve_it_the_same_way(runs_dir, crawl, procs, report):
    cfg = scanconfig.ScanConfig(dns_host="staging.x.test", dns_ip="10.0.0.9")
    pipeline(categories=[], deep=False, a11y=True, standards=["wcag21aa"], scan_config=cfg)
    assert crawl_config(runs_dir)["puppeteerOptions"]["args"] == [
        "--host-resolver-rules=MAP staging.x.test 10.0.0.9"]
    payload = json.loads(procs.page_runs[0]["input"])
    assert payload["launchArgs"] == ["--host-resolver-rules=MAP staging.x.test 10.0.0.9"]


def test_the_rule_is_quoted_because_lighthouse_splits_that_string_on_spaces():
    cfg = scanconfig.ScanConfig(dns_host="x.test", dns_ip="203.0.113.10")
    flag = cfg.chrome_flags()[0]
    assert flag == '--host-resolver-rules="MAP x.test 203.0.113.10"'
    # the browsers are handed an argv element, where quoting would be wrong
    assert cfg.browser_context()["launchArgs"] == [
        "--host-resolver-rules=MAP x.test 203.0.113.10"]


def test_an_address_that_is_not_an_address_is_dropped():
    for junk in ("1.2.3", "localhost", "10.0.0.1 --disable-web-security", "", None):
        assert scanconfig.ScanConfig(dns_host="x.test", dns_ip=junk).dns_ip == ""
    assert scanconfig.ScanConfig(dns_ip="::1").dns_ip == "::1"
    assert scanconfig.ScanConfig(dns_ip=" 10.0.0.9 ").dns_ip == "10.0.0.9"


def test_a_host_that_is_not_a_host_is_dropped():
    for junk in ("x.test/../y", "MAP evil 1.2.3.4", "a b", "-bad-.test"):
        assert scanconfig.ScanConfig(dns_host=junk, dns_ip="10.0.0.9").dns_host == ""
    assert scanconfig.ScanConfig(dns_host="Staging.X.Test.", dns_ip="10.0.0.9").dns_host \
        == "staging.x.test"


def test_an_address_with_no_host_is_not_an_override_yet():
    cfg = scanconfig.ScanConfig(dns_ip="10.0.0.9")
    assert cfg.host_rules() == ""                 # nothing to map until the URL is known
    assert cfg.for_url("https://x.test/").host_rules() == "MAP x.test 10.0.0.9"


def test_the_override_is_named_in_the_scan_log(runs_dir, crawl, procs, report):
    lines = []
    pipeline(log=lines.append,
             scan_config=scanconfig.ScanConfig(block_patterns=ADS, dns_ip="10.0.0.9"))
    text = "\n".join(lines)
    assert "x.test resolved to 10.0.0.9" in text
    assert "blocking 3 URL pattern(s)" in text


# --------------------------------------------------------------------------
# the request layer
# --------------------------------------------------------------------------

def test_the_request_layer_reads_both_controls():
    params, error, _ = guardrails.sanitize_params({
        "url": "https://x.test", "block_patterns": "*ads*\n*beacon*",
        "dns_host": "staging.x.test", "dns_ip": "10.0.0.9"})
    assert error is None
    cfg = scanconfig.ScanConfig.from_dict(params["scan_config"])
    assert cfg.block_patterns == ("*ads*", "*beacon*")
    assert cfg.host_rules() == "MAP staging.x.test 10.0.0.9"


def test_the_block_list_survives_the_queue_as_json(runs_dir, monkeypatch):
    """A worker in another process rebuilds the same configuration."""
    import tasks

    seen = {}
    monkeypatch.setattr(dashboard, "run_pipeline",
                        lambda *a, **kw: seen.update(kw) or "run-folder")
    monkeypatch.setattr(dashboard, "run_artifacts", lambda name: [])
    params, _, _ = guardrails.sanitize_params({"url": "https://x.test",
                                               "block_patterns": ["*ads*"],
                                               "dns_ip": "10.0.0.9"})
    tasks.run_scan_job(json.loads(json.dumps(params)), lambda *_: None)
    assert seen["scan_config"] == scanconfig.ScanConfig(block_patterns=["*ads*"],
                                                        dns_ip="10.0.0.9")


def test_the_scan_route_passes_both_through(monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard, "run_pipeline",
                        lambda *a, **kw: calls.append(kw) or "run-folder")
    monkeypatch.setattr(dashboard, "run_artifacts", lambda name: [])
    dashboard.app.config["TESTING"] = True
    client = dashboard.app.test_client()
    resp = client.post("/scan", json={"url": "https://x.test",
                                      "categories": "performance",
                                      "block_patterns": "*ads*\n*beacon*",
                                      "dns_ip": "10.0.0.9"})
    assert resp.status_code == 200
    client.get(resp.get_json()["stream"])
    cfg = calls[0]["scan_config"]
    assert cfg.block_patterns == ("*ads*", "*beacon*")
    assert cfg.dns_ip == "10.0.0.9"


def test_a_deployment_can_refuse_dns_overrides(monkeypatch):
    """Pointing a hostname at an address of the client's choosing is how a
    staging box is measured - and, on a server that scans for strangers, a way
    to reach hosts the internet cannot. So it can be turned off, like
    authenticated scanning."""
    monkeypatch.setattr(settings, "ALLOW_DNS_OVERRIDE", False)
    params, _, _ = guardrails.sanitize_params({"url": "https://x.test",
                                               "dns_host": "x.test",
                                               "dns_ip": "10.0.0.9",
                                               "block_patterns": "*ads*"})
    cfg = scanconfig.ScanConfig.from_dict(params["scan_config"])
    assert cfg.dns_ip == "" and cfg.dns_host == ""
    assert cfg.block_patterns == ("*ads*",)       # blocking is unaffected


def test_the_limits_endpoint_says_whether_it_is_offered(monkeypatch):
    dashboard.app.config["TESTING"] = True
    client = dashboard.app.test_client()
    assert client.get("/api/limits").get_json()["dns_override_enabled"] is True
    monkeypatch.setattr(settings, "ALLOW_DNS_OVERRIDE", False)
    assert client.get("/api/limits").get_json()["dns_override_enabled"] is False


# --------------------------------------------------------------------------
# the form
# --------------------------------------------------------------------------

def test_the_form_offers_both_controls_inside_advanced_options():
    page = dashboard.PAGE
    for field in ("block_patterns", "dns_host", "dns_ip"):
        assert f'id="{field}"' in page, field
    assert '<details class="adv">' in page
    advanced = page.split('<details class="adv">', 1)[1]
    for field in ("block_patterns", "dns_host", "dns_ip"):
        assert f'id="{field}"' in advanced, field
