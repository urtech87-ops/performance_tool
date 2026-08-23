"""The rule credentials live by: memory, for one scan, and nowhere else.

Every test in this file is a place a third party's password must never turn
up. They are written as searches rather than as assertions about particular
fields on purpose: a new file in the run folder, a new line in the log or a
new key in the job record should fail these tests without anyone remembering
to come back and add a case.
"""

import json
import subprocess
from pathlib import Path

import pytest

import dashboard
import guardrails
import jobqueue
import runstate
import scanauth
import scanlogin
import tasks

from conftest import FakeRunner


# Distinctive enough that a substring search cannot match by accident.
BASIC_USER = "auth-user-Qx71"
BASIC_PASS = "basic-pass-Zk93vv"
COOKIE_VALUE = "cookie-val-Mn44rr"
LOGIN_USER = "login-user-Wp28"
LOGIN_PASS = "login-pass-Ht55dd"
LOGIN_COOKIE = "login-cookie-Bq62"

SECRETS = [BASIC_USER, BASIC_PASS, COOKIE_VALUE, LOGIN_USER, LOGIN_PASS]

FIELDS = {
    "auth_user": BASIC_USER,
    "auth_pass": BASIC_PASS,
    "cookies": f"session={COOKIE_VALUE}",
    "login_url": "https://x.test/login",
    "login_user_selector": "#username",
    "login_pass_selector": "#password",
    "login_submit_selector": "button",
    "login_user": LOGIN_USER,
    "login_pass": LOGIN_PASS,
}

ROUTES = [{"path": "/"}]


def credentials():
    return scanauth.Credentials.from_request(FIELDS.get)


def leaks(text):
    """Which secrets appear in `text`. Empty is the only passing answer."""
    blob = text if isinstance(text, str) else json.dumps(text, default=str)
    return [s for s in SECRETS if s in blob]


# --------------------------------------------------------------------------
# harness: a whole run, with every scanner stubbed
# --------------------------------------------------------------------------

@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    d = tmp_path / "runs"
    d.mkdir()
    monkeypatch.setattr(dashboard, "RUNS_DIR", d)
    return d


class Subprocesses:
    """Every scanner, stubbed - and each one writes its command line into the
    run folder, which is exactly what a careless tool would do."""

    def __init__(self, run_dir_holder):
        self.axe = FakeRunner()
        self.calls = []
        self.holder = run_dir_holder

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        self.calls.append({"cmd": cmd, "input": kwargs.get("input")})
        if Path(str(cmd[0])).name in ("node", "node.exe"):
            if "login_playwright_runner.js" in " ".join(cmd):
                payload = json.dumps({"url": "https://x.test/in",
                                      "cookies": [{"name": "sid", "value": LOGIN_COOKIE}]})
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=f"{scanlogin.RESULT_MARKER}{payload}\n", stderr="")
            return self.axe(cmd, **kwargs)
        out = next((a for a in cmd if a.startswith("--output-path=")), None)
        if out:
            stem = Path(out.split("=", 1)[1])
            stem.parent.mkdir(parents=True, exist_ok=True)
            stem.with_name(stem.name + ".report.json").write_text("{}", encoding="utf-8")
            stem.with_name(stem.name + ".report.html").write_text("<html></html>",
                                                                  encoding="utf-8")
        # A tool that echoes its own arguments back: the pipeline must not put
        # that in the log unredacted.
        return subprocess.CompletedProcess(cmd, 0, stdout="lighthouse ran: " + " ".join(cmd))


@pytest.fixture
def scanned(runs_dir, monkeypatch):
    """One complete authenticated run. Returns (run_dir, log_lines, procs)."""
    monkeypatch.setattr(dashboard, "DRYRUN", False)
    procs = Subprocesses(runs_dir)
    monkeypatch.setattr(subprocess, "run", procs)
    import accessibility_scan
    monkeypatch.setattr(accessibility_scan.subprocess, "run", procs)

    def fake_run_stream(cmd, cwd, log, env=None, redact=None):
        log("$ " + " ".join((redact or scanauth.NULL).cmd(cmd)))
        # unlighthouse printing its own configuration back at us
        log("unlighthouse config: " + " ".join(cmd))
        d = Path(cwd) / ".unlighthouse"
        d.mkdir(parents=True, exist_ok=True)
        (d / "ci-result.json").write_text(json.dumps({"routes": ROUTES}), encoding="utf-8")
        return 0

    monkeypatch.setattr(dashboard, "run_stream", fake_run_stream)
    monkeypatch.setattr(dashboard.cr, "load_ci_results",
                        lambda roots: [{"path": "/", "device": "desktop", "issues": None}])
    monkeypatch.setattr(dashboard.cr, "load_lhr_pages", lambda roots: [])
    monkeypatch.setattr(dashboard.cr, "build_html",
                        lambda pages, roots, **kw: "<html>report</html>")
    monkeypatch.setattr(dashboard.cr, "html_to_pdf",
                        lambda html_path, pdf_path, **kw:
                        (Path(pdf_path).write_text("%PDF-1.4", encoding="utf-8"),
                         {"engine": "stub", "polyfill": None})[1])

    logged = []
    name = dashboard.run_pipeline("https://x.test", "desktop", 1, True, 5, 1,
                                  True, ["performance"], logged.append,
                                  a11y=True, standards=["wcag21aa"],
                                  credentials=credentials())
    return runs_dir / name, logged, procs


@pytest.fixture(autouse=True)
def stub_security(monkeypatch):
    import security_scan
    monkeypatch.setattr(security_scan, "collect_site",
                        lambda site, urls, log=print: {"site_url": site, "hits": {},
                                                       "scanned": len(urls),
                                                       "total": len(urls), "cert_note": ""})
    monkeypatch.setattr(security_scan, "html_from", lambda data: "<html>sec</html>")


# --------------------------------------------------------------------------
# 1. nothing in the run folder
# --------------------------------------------------------------------------

def test_no_file_in_the_run_folder_contains_a_credential(scanned):
    run_dir, _log, _procs = scanned
    files = [p for p in run_dir.rglob("*") if p.is_file()]
    assert files, "the run wrote nothing - this test would pass vacuously"
    for path in files:
        found = leaks(path.read_text(encoding="utf-8", errors="replace"))
        assert not found, f"{path.relative_to(run_dir)} leaks {found}"


def test_the_status_ledger_records_the_run_not_the_login(scanned):
    run_dir, _log, _procs = scanned
    saved = runstate.RunState.load(run_dir)
    assert saved is not None and saved["pages"], "no ledger to check"
    assert not leaks(saved)


def test_the_reports_are_written_and_clean(scanned):
    """The point of the run folder sweep above is that a report really existed."""
    run_dir, _log, _procs = scanned
    for name in ("report.html", "accessibility.html", "security.html"):
        assert (run_dir / name).is_file(), name
        assert not leaks((run_dir / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 2. nothing in the log
# --------------------------------------------------------------------------

def test_no_log_line_contains_a_credential(scanned):
    _run_dir, logged, _procs = scanned
    assert logged, "nothing was logged - this test would pass vacuously"
    for line in logged:
        found = leaks(line)
        assert not found, f"log line leaks {found}: {line}"


def test_the_echoed_command_is_redacted_but_still_readable(scanned):
    _run_dir, logged, _procs = scanned
    echoed = next(l for l in logged if l.startswith("$ ") and "unlighthouse-ci" in l)
    assert "--auth" in echoed and "--cookies" in echoed     # still shows what ran
    assert scanauth.MASK in echoed                          # but not with what


def test_a_tool_that_echoes_its_own_arguments_is_redacted_too(scanned):
    """Redaction is on the log, not on one call site, so output the pipeline
    never anticipated is covered as well."""
    _run_dir, logged, _procs = scanned
    echoed = [l for l in logged if "unlighthouse config:" in l]
    assert echoed and not leaks(echoed[0])


def test_the_log_still_says_that_the_scan_was_authenticated(scanned):
    _run_dir, logged, _procs = scanned
    assert any("Authenticated scan" in l for l in logged)
    assert any("HTTP Basic authentication" in l for l in logged)


def test_the_scanners_really_were_given_the_credentials(scanned):
    """The counterweight to every test above: redaction must not be achieved by
    quietly not sending them."""
    _run_dir, _log, procs = scanned
    lh = next(c["cmd"] for c in procs.calls
              if any(a.startswith("--extra-headers") for a in c["cmd"]))
    headers = json.loads(lh[lh.index("--extra-headers") + 1])
    assert COOKIE_VALUE in headers["Cookie"]
    assert LOGIN_COOKIE in headers["Cookie"]        # from the scripted login
    assert headers["Authorization"].startswith("Basic ")


# --------------------------------------------------------------------------
# 3. nothing in the request layer: params, job record, streamed events
# --------------------------------------------------------------------------

def test_sanitized_params_never_carry_a_credential():
    params, error, _ = guardrails.sanitize_params(dict(FIELDS, url="https://x.test"))
    assert error is None
    assert not guardrails.has_secrets(params)
    assert not leaks(params)


@pytest.fixture
def parked_queue(monkeypatch):
    """An inline queue that starts no worker threads, so the job stays put and
    these tests can look at it instead of racing a scan."""
    backend = jobqueue.InlineBackend()
    monkeypatch.setattr(backend, "_ensure_workers", lambda: None)
    return backend


def test_the_queue_keeps_credentials_out_of_the_job_record(parked_queue):
    params, _, _ = guardrails.sanitize_params({"url": "https://x.test"})
    job_id = parked_queue.enqueue(params, client="c", credentials=credentials())
    assert not leaks(parked_queue.state(job_id))
    assert not leaks(parked_queue._jobs[job_id]["params"])


def test_a_worker_takes_the_credentials_off_the_queue_exactly_once(parked_queue):
    params, _, _ = guardrails.sanitize_params({"url": "https://x.test"})
    job_id = parked_queue.enqueue(params, client="c", credentials=credentials())
    first = parked_queue.take_credentials(job_id)
    assert first and first.has_basic
    assert parked_queue.take_credentials(job_id) is None   # gone from the queue


def test_the_redis_queue_stores_credentials_apart_and_deletes_them_on_pickup():
    fakeredis = pytest.importorskip("fakeredis")
    conn = fakeredis.FakeStrictRedis()
    backend = jobqueue.RedisQueueBackend(redis_conn=conn, queue_name="t")
    params, _, _ = guardrails.sanitize_params({"url": "https://x.test"})
    job_id = backend.enqueue(params, client="c", credentials=credentials())

    record = {k.decode(): v.decode() for k, v in conn.hgetall(f"scanjob:{job_id}").items()}
    assert not leaks(record), "the job record must never hold a credential"

    got = backend.take_credentials(job_id)
    assert got.has_basic and got.cookie_header().endswith(COOKIE_VALUE)
    assert conn.get(f"scanjob:{job_id}:cred") is None    # deleted as it was read
    assert not backend.take_credentials(job_id)


def test_credentials_that_expired_in_the_queue_are_reported_not_ignored():
    """A scan of the logged-out site is a different scan. It must not be handed
    over as though it were the one that was asked for."""
    fakeredis = pytest.importorskip("fakeredis")
    conn = fakeredis.FakeStrictRedis()
    backend = jobqueue.RedisQueueBackend(redis_conn=conn, queue_name="t")
    params, _, _ = guardrails.sanitize_params({"url": "https://x.test"})
    job_id = backend.enqueue(params, client="c", credentials=credentials())

    assert backend.credentials_expected(job_id)
    conn.delete(f"scanjob:{job_id}:cred")          # the TTL ran out in the queue
    assert not backend.take_credentials(job_id)
    assert backend.credentials_expected(job_id)    # and the job still says so

    plain = backend.enqueue(params, client="c")
    assert not backend.credentials_expected(plain)


def test_credentials_are_never_accepted_from_a_query_string(monkeypatch):
    """The GET route is the one that lands in access logs and history, so it
    has no door for a credential at all."""
    calls = []
    monkeypatch.setattr(dashboard, "run_pipeline",
                        lambda *a, **kw: calls.append(kw) or "run-folder")
    monkeypatch.setattr(dashboard, "run_artifacts", lambda name: [])
    dashboard.app.config["TESTING"] = True
    client = dashboard.app.test_client()
    query = "&".join(f"{k}={v}" for k, v in FIELDS.items() if k != "login_url")
    client.get(f"/scan?url=https://x.test&categories=performance&{query}")
    assert calls[0]["credentials"] is None


def test_the_post_route_takes_them_and_the_stream_url_carries_none(monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard, "run_pipeline",
                        lambda *a, **kw: calls.append(kw) or "run-folder")
    monkeypatch.setattr(dashboard, "run_artifacts", lambda name: [])
    dashboard.app.config["TESTING"] = True
    client = dashboard.app.test_client()

    resp = client.post("/scan", json=dict(FIELDS, url="https://x.test",
                                          categories="performance"))
    assert resp.status_code == 200
    payload = resp.get_json()
    assert not leaks(payload)
    assert not leaks(payload["stream"])              # the URL the browser opens

    body = client.get(payload["stream"]).get_data(as_text=True)
    assert not leaks(body), "the event stream must carry no credential"
    assert "event: done" in body
    got = calls[0]["credentials"]
    assert got is not None and got.scrubbed          # wiped once the scan ended


def test_the_queue_holds_its_own_copy_of_the_credentials(parked_queue):
    """The web request wipes its credentials the moment the job is queued. The
    queue therefore has to have taken a copy - inline the two would otherwise
    be one object, and wiping it would disarm the scan that is about to use it.
    """
    got = credentials()
    params, _, _ = guardrails.sanitize_params({"url": "https://x.test"})
    job_id = parked_queue.enqueue(params, client="c", credentials=got)
    got.scrub()                                   # what the request does next
    queued = parked_queue.take_credentials(job_id)
    assert queued is not got
    assert queued.has_basic and COOKIE_VALUE in queued.cookie_header()


def test_the_posted_scan_really_is_authenticated(monkeypatch):
    """The same thing through the whole round trip."""
    seen = {}

    def fake_pipeline(*a, **kw):
        got = kw.get("credentials")
        seen["cookie"] = got.cookie_header() if got else ""
        seen["basic"] = bool(got and got.has_basic)
        return "run-folder"

    monkeypatch.setattr(dashboard, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(dashboard, "run_artifacts", lambda name: [])
    dashboard.app.config["TESTING"] = True
    client = dashboard.app.test_client()
    resp = client.post("/scan", json=dict(FIELDS, url="https://x.test",
                                          categories="performance"))
    client.get(resp.get_json()["stream"])
    assert seen["basic"] and COOKIE_VALUE in seen["cookie"]


def test_a_stream_cannot_be_followed_from_another_session(monkeypatch):
    monkeypatch.setattr(dashboard, "run_pipeline", lambda *a, **kw: "run-folder")
    monkeypatch.setattr(dashboard, "run_artifacts", lambda name: [])
    dashboard.app.config["TESTING"] = True
    client = dashboard.app.test_client()
    stream = client.post("/scan", json={"url": "https://x.test",
                                        "categories": "performance"}).get_json()["stream"]
    other = dashboard.app.test_client()
    body = other.get(stream).get_data(as_text=True)
    assert "rejected" in body


# --------------------------------------------------------------------------
# 4. nothing survives the scan in memory
# --------------------------------------------------------------------------

def test_the_job_wrapper_wipes_the_credentials_when_the_scan_ends(monkeypatch):
    monkeypatch.setattr(dashboard, "run_pipeline", lambda *a, **kw: "run-folder")
    monkeypatch.setattr(dashboard, "run_artifacts", lambda name: [])
    got = credentials()
    params, _, _ = guardrails.sanitize_params({"url": "https://x.test"})
    tasks.run_scan_job(params, lambda *_: None, credentials=got)
    assert got.scrubbed
    assert got.extra_headers() == {} and got.crawl_flags() == []


def test_they_are_wiped_even_when_the_scan_blows_up(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("scanner died")

    monkeypatch.setattr(dashboard, "run_pipeline", boom)
    got = credentials()
    params, _, _ = guardrails.sanitize_params({"url": "https://x.test"})
    with pytest.raises(RuntimeError):
        tasks.run_scan_job(params, lambda *_: None, credentials=got)
    assert got.scrubbed and not got.secrets()


def test_a_credential_never_renders_itself():
    got = credentials()
    assert not leaks(repr(got))
    assert not leaks(str(got))
    assert not leaks(repr(got.login))
    assert not leaks(f"{got.basic_pass}")


# --------------------------------------------------------------------------
# 5. nothing in what the browser saves
# --------------------------------------------------------------------------

def test_the_form_marks_every_credential_field_as_secret():
    page = dashboard.PAGE
    for field in scanauth.SECRET_FIELDS:
        assert f'id="{field}"' in page, field
    # each one carries the marker, and the passwords are password inputs
    for field in scanauth.SECRET_FIELDS:
        chunk = page.split(f'id="{field}"')[1][:200]
        assert 'data-secret="1"' in chunk, field
    assert page.count('type="password"') >= 2


def test_the_saved_settings_list_and_the_secret_list_never_overlap():
    """The browser writes SETTING_FIELDS to localStorage. If a credential field
    ever appears on that list, this fails."""
    page = dashboard.PAGE
    settings = _js_array(page, "SETTING_FIELDS")
    secrets = _js_array(page, "SECRET_FIELDS")
    assert set(secrets) == set(scanauth.SECRET_FIELDS), "the two lists have drifted"
    assert not set(settings) & set(secrets)
    assert "user_agent" in settings and "throttling" in settings   # not vacuous


def test_the_browser_is_told_what_happens_to_what_it_typed():
    page = dashboard.PAGE
    assert "These fields are never stored." in page
    assert "wiped when it ends" in page


def test_the_settings_are_saved_but_the_credentials_are_not():
    """saveSettings() may only ever read SETTING_FIELDS."""
    page = dashboard.PAGE
    body = page.split("function saveSettings()")[1].split("function loadSettings()")[0]
    assert "SETTING_FIELDS" in body
    assert "SECRET_FIELDS" not in body
    for field in scanauth.SECRET_FIELDS:
        assert field not in body, field


def _js_array(page, name):
    """The string literals of a `var NAME = [...]` declaration in the page."""
    import re
    chunk = page.split(f"var {name} = ")[1].split("];")[0]
    return re.findall(r'"([a-z_]+)"', chunk)
