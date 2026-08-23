"""Authenticated scanning: Basic auth, pasted cookies and a scripted login.

What is checked here is that the credentials actually *reach* the scanners -
the right flags on `lighthouse`, the right flags on `unlighthouse-ci`, the
right browser context for the login. That they never reach anything else is
the subject of test_scan_auth_privacy.py.
"""

import json
import subprocess
from pathlib import Path

import pytest

import dashboard
import guardrails
import scanauth
import scanconfig
import scanlogin

from conftest import FakeRunner


ROUTES = [{"path": "/"}]

USER, PASSWORD = "site-admin", "hunter2-correct-horse"
COOKIES = "session=sess-abc-123; consent=accepted"


def creds(**over):
    fields = {"auth_user": USER, "auth_pass": PASSWORD, "cookies": COOKIES}
    fields.update(over)
    return scanauth.Credentials.from_request(fields.get)


# --------------------------------------------------------------------------
# parsing and validation
# --------------------------------------------------------------------------

def test_cookies_are_parsed_into_pairs():
    assert scanauth.parse_cookies("a=1; b=2") == [("a", "1"), ("b", "2")]


def test_a_cookie_value_may_contain_an_equals_sign():
    assert scanauth.parse_cookies("jwt=aaa=bb=") == [("jwt", "aaa=bb=")]


def test_newlines_are_accepted_as_separators():
    """Devtools hands you one cookie per line."""
    assert scanauth.parse_cookies("a=1\nb=2\n") == [("a", "1"), ("b", "2")]


def test_a_cookie_cannot_smuggle_a_header():
    """A CRLF ends the pair rather than starting a new header, and what follows
    it has to be a real cookie - "X-Admin: yes" is not."""
    with pytest.raises(scanauth.CredentialError):
        scanauth.parse_cookies("a=1\r\nX-Admin: yes=2")
    header = scanauth.Credentials(cookies=scanauth.parse_cookies("a=1;b=2")).cookie_header()
    assert "\r" not in header and "\n" not in header


def test_a_malformed_cookie_is_an_error_not_a_silent_drop():
    with pytest.raises(scanauth.CredentialError):
        scanauth.parse_cookies("not-a-pair")
    with pytest.raises(scanauth.CredentialError):
        scanauth.parse_cookies("bad name=1")


def test_a_password_without_a_username_is_refused():
    with pytest.raises(scanauth.CredentialError):
        scanauth.Credentials.from_request({"auth_pass": "x"}.get)


def test_a_login_needs_all_three_selectors():
    with pytest.raises(scanauth.CredentialError) as exc:
        scanauth.Credentials.from_request({"login_url": "https://x.test/login",
                                           "login_user_selector": "#u",
                                           "login_user": "a", "login_pass": "b"}.get)
    assert "password selector" in str(exc.value)


def test_a_login_url_must_be_http():
    with pytest.raises(scanauth.CredentialError):
        scanauth.Credentials.from_request({"login_url": "javascript:alert(1)",
                                           "login_user_selector": "#u",
                                           "login_pass_selector": "#p",
                                           "login_submit_selector": "b",
                                           "login_user": "a", "login_pass": "b"}.get)


def test_no_credentials_is_falsy_and_produces_no_flags():
    empty = scanauth.Credentials()
    assert not empty
    assert empty.lighthouse_flags() == [] and empty.crawl_flags() == []


def test_a_deployment_can_refuse_credentials_outright(monkeypatch):
    import config
    monkeypatch.setattr(config.settings, "ALLOW_SCAN_AUTH", False)
    got, error = guardrails.extract_credentials({"auth_user": "a", "auth_pass": "b"})
    assert got is None and "turned off" in error
    # and a scan without them is unaffected
    got, error = guardrails.extract_credentials({"url": "https://x.test"})
    assert error is None and not got


# --------------------------------------------------------------------------
# what the scanners are handed
# --------------------------------------------------------------------------

def test_basic_auth_becomes_an_authorization_header():
    import base64
    headers = creds(cookies="").extra_headers()
    raw = base64.b64decode(headers["Authorization"].split()[1]).decode()
    assert raw == f"{USER}:{PASSWORD}"


def test_cookies_become_one_cookie_header():
    assert creds(auth_user="", auth_pass="").extra_headers() == {
        "Cookie": "session=sess-abc-123; consent=accepted"}


def test_the_lighthouse_flag_is_a_single_json_object():
    flags = creds().lighthouse_flags()
    assert flags[0] == "--extra-headers"
    payload = json.loads(flags[1])
    assert set(payload) == {"Authorization", "Cookie"}


def test_the_crawler_gets_its_own_native_flags():
    flags = creds().crawl_flags()
    assert flags[flags.index("--auth") + 1] == f"{USER}:{PASSWORD}"
    assert flags[flags.index("--cookies") + 1] == "session=sess-abc-123; consent=accepted"


def test_the_browser_payload_carries_no_flags_at_all():
    payload = creds().browser_payload()
    assert payload["httpCredentials"] == {"username": USER, "password": PASSWORD}
    assert payload["cookies"] == [{"name": "session", "value": "sess-abc-123"},
                                  {"name": "consent", "value": "accepted"}]


# --------------------------------------------------------------------------
# through the pipeline
# --------------------------------------------------------------------------

@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    d = tmp_path / "runs"
    d.mkdir()
    monkeypatch.setattr(dashboard, "RUNS_DIR", d)
    return d


@pytest.fixture
def crawl(monkeypatch):
    monkeypatch.setattr(dashboard, "DRYRUN", False)
    calls = []

    def fake_run_stream(cmd, cwd, log, env=None, redact=None):
        calls.append({"cmd": list(cmd), "echoed": (redact or scanauth.NULL).cmd(cmd)})
        d = Path(cwd) / ".unlighthouse"
        d.mkdir(parents=True, exist_ok=True)
        (d / "ci-result.json").write_text(json.dumps({"routes": ROUTES}), encoding="utf-8")
        return 0

    monkeypatch.setattr(dashboard, "run_stream", fake_run_stream)
    return calls


class Subprocesses:
    def __init__(self):
        self.axe = FakeRunner()
        self.lighthouse = []
        self.node = []

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        if Path(str(cmd[0])).name in ("node", "node.exe"):
            self.node.append({"cmd": cmd, "input": kwargs.get("input")})
            if "login_playwright_runner.js" in " ".join(cmd):
                payload = json.dumps({"url": "https://x.test/members",
                                      "cookies": [{"name": "sid", "value": "from-login"}]})
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


def test_basic_auth_and_cookies_reach_both_scanners(runs_dir, crawl, procs, report):
    pipeline(credentials=creds())

    lh = procs.lighthouse[0]
    headers = json.loads(lh[lh.index("--extra-headers") + 1])
    assert headers["Cookie"] == "session=sess-abc-123; consent=accepted"
    assert headers["Authorization"].startswith("Basic ")

    crawl_cmd = crawl[0]["cmd"]
    assert crawl_cmd[crawl_cmd.index("--auth") + 1] == f"{USER}:{PASSWORD}"
    assert "session=sess-abc-123; consent=accepted" in crawl_cmd


def test_a_scan_without_credentials_passes_no_headers(runs_dir, crawl, procs, report):
    pipeline()
    assert "--extra-headers" not in procs.lighthouse[0]
    assert "--auth" not in crawl[0]["cmd"]


def test_the_accessibility_scan_is_signed_in_too(runs_dir, crawl, procs, report):
    pipeline(categories=[], deep=False, a11y=True, standards=["wcag21aa"],
             credentials=creds())
    page_runs = [c for c in procs.node if "--url" in c["cmd"]]
    assert page_runs, "the axe runner never ran"
    assert "--browser-stdin" in page_runs[0]["cmd"]
    payload = json.loads(page_runs[0]["input"])
    assert payload["httpCredentials"]["password"] == PASSWORD
    assert {"name": "session", "value": "sess-abc-123"} in payload["cookies"]


def test_an_ordinary_accessibility_scan_reads_no_stdin(runs_dir, crawl, procs, report):
    pipeline(categories=[], deep=False, a11y=True, standards=["wcag21aa"])
    page_runs = [c for c in procs.node if "--url" in c["cmd"]]
    assert "--browser-stdin" not in page_runs[0]["cmd"]
    assert page_runs[0]["input"] is None


# --------------------------------------------------------------------------
# the scripted login
# --------------------------------------------------------------------------

def login_creds(**over):
    fields = {"login_url": "https://x.test/login",
              "login_user_selector": "#username",
              "login_pass_selector": "#password",
              "login_submit_selector": "button[type=submit]",
              "login_user": "member@x.test", "login_pass": "let-me-in-99"}
    fields.update(over)
    return scanauth.Credentials.from_request(fields.get)


def test_the_login_runs_before_the_crawl_and_its_cookies_travel_on(runs_dir, crawl,
                                                                   procs, report):
    got = login_creds()
    pipeline(credentials=got)

    logins = [c for c in procs.node if "login_playwright_runner.js" in " ".join(c["cmd"])]
    assert len(logins) == 1, "the login ran once, before the scan"

    # the session cookie the login produced is on the scanners' command lines
    crawl_cmd = crawl[0]["cmd"]
    assert "sid=from-login" in crawl_cmd[crawl_cmd.index("--cookies") + 1]
    lh = procs.lighthouse[0]
    headers = json.loads(lh[lh.index("--extra-headers") + 1])
    assert "sid=from-login" in headers["Cookie"]


def test_the_login_credentials_go_on_stdin_never_on_the_command_line(runs_dir, crawl,
                                                                     procs, report):
    pipeline(credentials=login_creds())
    login = next(c for c in procs.node
                 if "login_playwright_runner.js" in " ".join(c["cmd"]))
    assert "let-me-in-99" not in " ".join(login["cmd"])
    assert "member@x.test" not in " ".join(login["cmd"])
    payload = json.loads(login["input"])
    assert payload["username"] == "member@x.test" and payload["password"] == "let-me-in-99"
    assert payload["userSelector"] == "#username"
    assert payload["submitSelector"] == "button[type=submit]"


def test_the_login_uses_the_scans_own_device(runs_dir, crawl, procs, report):
    pipeline(credentials=login_creds(),
             scan_config=scanconfig.ScanConfig(device_profile="iphone_se"))
    login = next(c for c in procs.node
                 if "login_playwright_runner.js" in " ".join(c["cmd"]))
    context = json.loads(login["input"])["context"]
    assert context["viewport"] == {"width": 375, "height": 667}
    assert "iPhone" in context["userAgent"]


def test_a_failed_login_degrades_the_run_instead_of_killing_it(runs_dir, crawl, procs,
                                                               report, monkeypatch):
    def boom(*a, **kw):
        raise scanlogin.LoginError("the submit button was never found")

    monkeypatch.setattr(scanlogin, "perform", boom)
    logged = []
    name = pipeline(credentials=login_creds(), log=logged.append)
    assert name                                   # the run still produced a folder
    assert any("Login failed" in line for line in logged)
    assert procs.lighthouse, "the scan went ahead unauthenticated"


def test_a_scan_with_no_login_never_starts_a_browser_for_one(runs_dir, crawl, procs, report):
    pipeline(credentials=creds())
    assert not [c for c in procs.node if "login_playwright_runner.js" in " ".join(c["cmd"])]
