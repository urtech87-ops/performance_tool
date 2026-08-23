#!/usr/bin/env python3
"""
Scripted form login
===================
Drives `login_playwright_runner.js` once, before the scan starts: it opens the
site's login form in a real browser, types the credentials in, submits, and
hands back the cookies the session left behind. Those cookies then travel with
the crawl and the Lighthouse audits, so every page after this is fetched as a
signed-in user.

The credentials go to the browser **on stdin**, never as arguments, so they are
not visible in a process listing. Nothing is written to disk: the cookies come
back on stdout and stay in the `Credentials` object in memory.

The Node runtime, its packages and Playwright's bundled Chromium are the same
ones the accessibility scan installs - reusing them means an authenticated
scan needs no extra setup on the machine.
"""

import json
import subprocess
import time
from pathlib import Path

RUNNER_JS = Path(__file__).resolve().parent / "login_playwright_runner.js"
RESULT_MARKER = "__LOGIN_RESULT__"
LOGIN_TIMEOUT = 90        # seconds for the whole sign-in


class LoginError(RuntimeError):
    """The login could not be performed. Its message is safe to log: it
    carries the runner's diagnostics, which never include the password."""


def _runner():
    """Node, the script, and the environment that lets it find Playwright."""
    import accessibility_scan as a11y

    a11y_runner = a11y.runner_for()
    return a11y.NODE, a11y_runner.env, a11y_runner.home


def _parse(stdout):
    """The one marker line the runner prints, or None."""
    for line in str(stdout or "").splitlines():
        line = line.strip()
        if line.startswith(RESULT_MARKER):
            try:
                return json.loads(line[len(RESULT_MARKER):].strip())
            except ValueError:
                return None
    return None


def perform(credentials, scan_config=None, timeout=LOGIN_TIMEOUT, log=print):
    """Log in and fold the resulting cookies into `credentials`.

    Returns the number of cookies the session produced. Raises LoginError when
    the login could not be run at all - the caller decides whether that ends
    the scan, and never logs anything but the message.
    """
    login = credentials.login
    if not login:
        return 0
    if not RUNNER_JS.is_file():
        raise LoginError(f"the login runner is missing at {RUNNER_JS}")

    node, env, home = _runner()
    payload = login.payload()
    payload["timeout"] = int(timeout)
    payload.update(credentials.browser_payload())
    if scan_config is not None:
        context = scan_config.browser_context()
        if context:
            payload["context"] = context

    # The one and only place this payload exists outside memory is the pipe
    # into the child process.
    blob = json.dumps(payload)
    log(f"Signing in at {login.url} ...")
    t0 = time.time()
    try:
        proc = subprocess.run(
            [node, str(RUNNER_JS)], cwd=str(home), check=False, timeout=timeout + 30,
            input=blob, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise LoginError(f"the login timed out after {timeout}s")
    except FileNotFoundError:
        raise LoginError(f"the login runner needs Node.js on PATH ({node} not found)")
    except Exception as exc:                                       # noqa: BLE001
        raise LoginError(f"{type(exc).__name__}: {exc}")
    finally:
        blob = None                 # drop the credential copy immediately

    result = _parse(getattr(proc, "stdout", ""))
    if result is None:
        detail = _tail(getattr(proc, "stderr", "")) or \
            f"the login runner exited {getattr(proc, 'returncode', 1)} without a result"
        raise LoginError(detail)

    cookies = [(c.get("name"), c.get("value")) for c in result.get("cookies") or []
               if c.get("name")]
    credentials.add_cookies(cookies)
    secs = int(time.time() - t0)
    landed = result.get("url") or login.url
    log(f"Signed in ({secs}s) - {len(cookies)} session cookie(s) captured, "
        f"landed on {landed}.")
    return len(cookies)


def _tail(text, lines=6):
    kept = [l.rstrip() for l in str(text or "").splitlines() if l.strip()]
    return "\n".join(kept[-lines:])
