"""Shared fixtures: a fake Node runner so the suite never needs a browser."""

import json
import subprocess
import sys
import time
import weakref
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import accessibility_scan as a11y  # noqa: E402
import config  # noqa: E402
import dashboard  # noqa: E402
import jobqueue  # noqa: E402
import verification  # noqa: E402


# Every inline queue built during a test, so none can outlive it. Several
# tests build their own backend rather than using the fixture's, and its worker
# threads are daemons that keep pulling from it long after the test returns.
_LIVE_QUEUES = weakref.WeakSet()
_inline_init = jobqueue.InlineBackend.__init__


def _tracked_init(self, *args, **kwargs):
    _inline_init(self, *args, **kwargs)
    _LIVE_QUEUES.add(self)


jobqueue.InlineBackend.__init__ = _tracked_init


def drain(backend, timeout=10.0):
    """End a test's queue: drop what has not started, wait for what has.

    The inline queue runs jobs on daemon threads. A test that enqueues a scan
    and does not follow it to the end leaves that job waiting - and this
    teardown runs *after* pytest has undone the test's monkeypatches, so a
    worker picking it up then would call the real `run_pipeline` against the
    test's fake URL: a live `npx` against a domain that does not exist, writing
    into the repository's own runs/ folder and racing whatever test is running
    by then.

    So jobs still queued are cancelled under the backend's own lock (where no
    worker can have taken one yet), and jobs already running - which started
    while the test's stubs were in place - are waited out.

    Called from `pytest_runtest_call` below rather than from a fixture, because
    it has to happen while the test's own stubs are still installed: fixture
    finalizers run after `monkeypatch` has already put the real `run_pipeline`
    back.
    """
    jobs = getattr(backend, "_jobs", None)
    if jobs is None:
        return
    with backend._cond:
        for job_id in list(backend._pending):
            record = jobs.get(job_id)
            if record is not None:
                record["state"] = jobqueue.FAILED
                record["error"] = "cancelled when the test ended"
                record.pop("credentials", None)
        backend._pending.clear()
        backend._cond.notify_all()
    deadline = time.time() + timeout
    for job_id in list(jobs):
        while time.time() < deadline:
            if backend.state(job_id)["state"] in jobqueue.TERMINAL:
                break
            backend.wait(job_id, 0.05)


@pytest.fixture(autouse=True)
def fresh_serving_layer():
    """A clean queue, rate-limit ledger and verification cache per test.

    The serving layer keeps process-wide state (that is the point of it), so
    without this one test's scans would count against the next one's budget.
    """
    config.reload()
    backend = jobqueue.InlineBackend()
    jobqueue.set_backend(backend)
    verification.set_cache(verification.MemoryVerificationCache())
    dashboard.reset_services()
    yield
    jobqueue.set_backend(None)
    dashboard.reset_services()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """Empty the queue the moment the test body returns.

    This is the last point at which the test's monkeypatches are still in
    place, which is exactly what `drain` needs - see its docstring.
    """
    yield
    for backend in list(_LIVE_QUEUES):
        try:
            drain(backend)
        except Exception:                                         # noqa: BLE001
            pass
    _LIVE_QUEUES.clear()


def violation(rule_id, impact, tags, help_text="", nodes=1, node_html="<img src=x>"):
    return {
        "id": rule_id,
        "impact": impact,
        "tags": list(tags),
        "help": help_text or f"{rule_id} help",
        "description": f"{rule_id} description",
        "helpUrl": f"https://dequeuniversity.com/rules/axe/4.13/{rule_id}",
        "nodes": [{"target": [f"#{rule_id}-{i}"], "html": node_html} for i in range(nodes)],
    }


# A realistic axe payload: one rule per WCAG level/standard slice we map.
DEFAULT_VIOLATIONS = [
    violation("image-alt", "critical",
              ["cat.text-alternatives", "wcag2a", "wcag111", "section508",
               "section508.22.a", "EN-301-549", "EN-9.1.1.1"],
              "Images must have alternate text"),
    violation("color-contrast", "serious",
              ["cat.color", "wcag2aa", "wcag143", "EN-301-549", "EN-9.1.4.3"],
              "Elements must meet minimum color contrast ratio thresholds", nodes=4,
              node_html='<p style="color:#bbb">hard to read</p>'),
    violation("target-size", "serious",
              ["cat.sensory-and-visual-cues", "wcag22aa", "wcag258"],
              "All touch targets must be 24px large, or leave sufficient space"),
    violation("scrollable-region-focusable", "serious",
              ["cat.keyboard", "wcag21a", "wcag211"],
              "Scrollable region must have keyboard access"),
    violation("region", "moderate", ["cat.keyboard", "best-practice"],
              "All page content should be contained by landmarks"),
]

DEFAULT_INCOMPLETE = [
    violation("color-contrast", "serious",
              ["cat.color", "wcag2aa", "wcag143"],
              "Elements must meet minimum color contrast ratio thresholds"),
]


def axe_payload(url, violations=None, incomplete=None, version="4.10.2", passes=3):
    """The shape the Node runner prints: one axe result object per page."""
    return {
        "url": url,
        "timestamp": "2026-08-19T00:00:00.000Z",
        "testEngine": {"name": "axe-core", "version": version},
        "violations": DEFAULT_VIOLATIONS if violations is None else violations,
        "incomplete": DEFAULT_INCOMPLETE if incomplete is None else incomplete,
        "passes": [{"id": f"passing-rule-{i}"} for i in range(passes)],
        "inapplicable": [],
    }


FAKE_CHROMIUM = "/fake/playwright/chromium"


class FakeRunner:
    """Stands in for `node axe_playwright_runner.js`: records the argv it was
    handed and prints, on stdout, the JSON result the real runner would print.

    Anything without a `--url` is a setup probe (the "where is Chromium"
    question ensure_runner asks before the first page), not a page audit - the
    stub answers those with a browser path, without polluting `calls`.
    """

    def __init__(self, payload_for=None, fail_urls=(), emit_result=True,
                 output="", fail_output=None, fail_code=1, chromium=FAKE_CHROMIUM):
        self.calls = []
        self.probes = []
        self.payload_for = payload_for or (lambda url: axe_payload(url))
        self.fail_urls = set(fail_urls)
        self.emit_result = emit_result
        self.output = output
        self.fail_output = output if fail_output is None else fail_output
        self.fail_code = fail_code
        self.chromium = chromium

    @staticmethod
    def _url(cmd):
        return cmd[cmd.index("--url") + 1] if "--url" in cmd else None

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        url = self._url(cmd)
        if url is None:                       # a setup probe, not a page audit
            self.probes.append({"cmd": cmd, "kwargs": kwargs})
            return subprocess.CompletedProcess(cmd, 0, stdout=self.chromium, stderr="")
        self.calls.append({"cmd": cmd, "kwargs": kwargs})
        if not self.emit_result or url in self.fail_urls:
            # A page the runner could not audit: nothing on stdout, the reason
            # on stderr, and a non-zero exit.
            return subprocess.CompletedProcess(cmd, self.fail_code, stdout="",
                                               stderr=self.fail_output)
        payload = json.dumps(self.payload_for(url))
        return subprocess.CompletedProcess(
            cmd, 0, stdout=f"{a11y.RESULT_MARKER}{payload}\n", stderr=self.output)

    @property
    def urls(self):
        return [self._url(c["cmd"]) for c in self.calls]

    @property
    def tags(self):
        cmd = self.calls[0]["cmd"]
        return cmd[cmd.index("--tags") + 1].split(",")


@pytest.fixture
def fake_runner(monkeypatch):
    """Replace the Node runner subprocess; hand the stub back for assertions."""
    stub = FakeRunner()
    monkeypatch.setattr(a11y.subprocess, "run", stub)
    return stub


@pytest.fixture(autouse=True)
def runner_home(tmp_path, monkeypatch):
    """Point the runner at a throwaway home with its npm packages already in
    place, so no test can install anything, touch the machine's real cache dir,
    or depend on what happens to be installed on it."""
    home = tmp_path / "runner-home"
    for package in a11y.RUNNER_DEPS:
        (home / "node_modules" / package).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("A11Y_RUNNER_HOME", str(home))
    return home
