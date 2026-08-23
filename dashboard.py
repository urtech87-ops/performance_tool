#!/usr/bin/env python3
"""
Site Performance Dashboard
==========================
A tiny local web app: enter any URL, pick Desktop or Mobile, and get the same
consolidated multi-page performance report (scores + core metrics + itemized
fix recommendations) as one HTML page you can view, download, or export to PDF.

Engine:
  1. `unlighthouse-ci` crawls the whole site -> ci-result.json (all page paths,
     averaged category scores, core metrics).
  2. (optional "deep audit") Google `lighthouse` CLI runs per discovered page ->
     full reports containing the itemized failing audits + recommendations.
  3. consolidate_report.py rolls everything into one HTML (and optional PDF).

Optional, self-contained add-ons (each writes its own page into the run folder):
  - security_scan.py       -> security.html       (passive header/TLS checks)
  - accessibility_scan.py  -> accessibility.html  (axe-core WCAG / legal standards)

Requirements on the machine that RUNS this:
  - Python 3.9+  ->  pip install flask
  - Node.js (for `npx unlighthouse-ci`, `npx lighthouse` and, if the optional
    accessibility scan is used, `@axe-core/playwright`)
  - Chrome / Edge / Brave (Lighthouse drives it; also used for PDF)
  - the accessibility scan brings its own browser: on first run it installs
    @axe-core/playwright plus Playwright's bundled Chromium into a cache dir
    (override with A11Y_RUNNER_HOME), so it needs neither the machine's Chrome
    nor a ChromeDriver matching it

Run (local, single process - keeps the in-memory queue):
    pip install -r requirements.txt
    python dashboard.py
    # open http://127.0.0.1:5000

Run (public - Redis queue, worker pool, real web server): see README.md.

Set DASHBOARD_DRYRUN=1 to preview the UI/pipeline without a real scan.
"""

import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from flask import Flask, Response, jsonify, make_response, request, send_from_directory, abort

import consolidate_report as cr  # reuse the same report builder
import guardrails
import jobqueue
import runstate
import scanauth
import scanconfig
import verification
from config import settings

APP_DIR = Path(__file__).resolve().parent
RUNS_DIR = APP_DIR / "runs"
RUNS_DIR.mkdir(exist_ok=True)
DRYRUN = os.environ.get("DASHBOARD_DRYRUN") == "1"
NPX = "npx.cmd" if os.name == "nt" else "npx"
PER_PAGE_TIMEOUT = settings.PAGE_TIMEOUT   # seconds per page for the deep audit

def _lighthouse_cmd():
    """Prefer a globally installed `lighthouse`; fall back to `npx lighthouse`."""
    import shutil
    exe = shutil.which("lighthouse") or shutil.which("lighthouse.cmd")
    return [exe] if exe else [NPX, "lighthouse"]

LIGHTHOUSE = _lighthouse_cmd()


def scan_env():
    """Env for scan subprocesses: relax Node TLS so sites with an incomplete
    certificate chain (which browsers tolerate but Node rejects) still scan."""
    env = os.environ.copy()
    env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
    return env


app = Flask(__name__)

if settings.TRUST_PROXY:
    # Behind nginx/Caddy: take the client IP from X-Forwarded-For so per-IP
    # limits apply to the visitor, not to the proxy.
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


# --------------------------- serving layer ---------------------------
# Everything below is about *admitting* work: which queue it goes on, how much
# of it one client may ask for, and how progress gets back to the browser.
# The pipeline itself is untouched.

SESSION_COOKIE = "scan_sid"

_services_lock = threading.Lock()
_limiter = None


def queue_backend():
    return jobqueue.get_backend()


def _job_finished(job_id):
    return queue_backend().state(job_id)["state"] in jobqueue.TERMINAL


def rate_limiter():
    """One limiter per process, built to match whichever queue is in use.

    With Redis, the counters and the verified-domain cache live there too, so
    every web process enforces one shared budget instead of its own.
    """
    global _limiter
    with _services_lock:
        if _limiter is None:
            backend = queue_backend()
            shared = isinstance(backend, jobqueue.RedisQueueBackend)
            if shared:
                verification.set_cache(verification.RedisVerificationCache(backend.r))
            store = (guardrails.RedisLimitStore(backend.r) if shared
                     else guardrails.MemoryLimitStore())
            _limiter = guardrails.RateLimiter(store, is_finished=_job_finished)
        return _limiter


def reset_services():
    """Drop the cached limiter (used by tests and after a config reload)."""
    global _limiter
    with _services_lock:
        _limiter = None


def session_id():
    """A stable per-browser id, so limits survive an IP shared by many users
    and apply to a single tab-happy visitor too."""
    return request.cookies.get(SESSION_COOKIE) or ""


def new_session_id():
    return uuid.uuid4().hex


def _sse(event, payload):
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def _rejection(reason, message, extra=None):
    """A refusal the browser can render: SSE (so EventSource sees it) plus a
    header any other client or monitor can key off."""
    payload = {"reason": reason, "message": message}
    if extra:
        payload.update(extra)
    resp = Response(_sse("rejected", payload), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Scan-Rejected"] = reason
    return resp


# ----------------------------- pipeline -----------------------------

def slugify(url):
    return re.sub(r"[^a-z0-9]+", "-", urlparse(url).netloc.lower()).strip("-") or "site"


def origin_of(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _clean(line):
    return ANSI_RE.sub("", line).rstrip()


def run_stream(cmd, cwd, log, env=None, redact=None):
    """Run a subprocess, streaming its output into the log queue.

    `redact` is a `scanauth.Redactor`: the command is echoed through it, so a
    credential handed to a scanner never reaches the log. Its *output* is
    redacted too, by the log callable the pipeline wraps - belt and braces,
    because a tool that echoes back the header it was given would otherwise
    print it verbatim.
    """
    echoed = (redact or scanauth.NULL).cmd(cmd)
    log(f"$ {' '.join(echoed)}")
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding="utf-8", errors="replace", bufsize=1, env=env,
    )
    for raw in proc.stdout:
        line = _clean(raw)
        if line:
            log(line)
    proc.wait()
    return proc.returncode


def make_mock(run_dir, url, device, deep):
    """DRYRUN: fabricate a ci-result.json (+ optional LHR) so the UI is testable."""
    ul = run_dir / ".unlighthouse"
    ul.mkdir(parents=True, exist_ok=True)
    routes = []
    for path, perf in [("/", 0.62), ("/about/", 0.81), ("/contact/", 0.74)]:
        routes.append({
            "path": path, "score": perf,
            "categories": {"performance": {"score": perf}, "accessibility": {"score": 0.9},
                           "best-practices": {"score": 0.83}, "seo": {"score": 0.92}},
            "metrics": {
                "largest-contentful-paint": {"displayValue": "3.9 s", "numericValue": 3900, "score": 0.45},
                "cumulative-layout-shift": {"displayValue": "0.04", "numericValue": 0.04, "score": 0.96},
                "total-blocking-time": {"displayValue": "380 ms", "numericValue": 380, "score": 0.55},
            },
        })
    (ul / "ci-result.json").write_text(json.dumps({"routes": routes}), encoding="utf-8")
    if deep:
        lhr = run_dir / "lhr"
        lhr.mkdir(exist_ok=True)
        for i, r in enumerate(routes):
            full = urljoin(origin_of(url) + "/", r["path"].lstrip("/"))
            (lhr / f"page_{i}.json").write_text(json.dumps({
                "finalDisplayedUrl": full,
                "categories": {k: v for k, v in r["categories"].items()},
                "audits": {
                    "unused-javascript": {"title": "Reduce unused JavaScript",
                                          "description": "Remove dead code to cut bytes over the network.",
                                          "score": 0.3, "scoreDisplayMode": "numeric",
                                          "displayValue": "Potential savings of 240 KiB"},
                    "uses-responsive-images": {"title": "Properly size images",
                                               "description": "Serve images at the size they are displayed.",
                                               "score": 0.5, "scoreDisplayMode": "numeric",
                                               "displayValue": "Potential savings of 110 KiB"},
                    "largest-contentful-paint": {"title": "Largest Contentful Paint",
                                                 "score": 0.45, "scoreDisplayMode": "numeric",
                                                 "numericValue": 3900, "displayValue": "3.9 s"},
                },
            }), encoding="utf-8")


def _lh_outcome(output, stem):
    """What one Lighthouse attempt produced: (status, reason, detail).

    The report file on disk - not the exit code - is what says a page was
    really audited. When there is none, the CLI's own output is read for a
    condition we can name (a 403, a DNS failure, a hung page) so the page can
    say why it is missing instead of vanishing into a silent gap.
    """
    report = stem.with_name(stem.name + ".report.json")
    if report.exists() and report.stat().st_size > 0:
        return runstate.OK, "", ""
    detail = _clean_block(output)
    kind = runstate.classify(detail)
    if kind is None:
        return runstate.ERROR, "no report", detail
    return runstate.page_status(kind), kind, detail


def _clean_block(text):
    """A scanner's output with the ANSI noise stripped, trimmed to a few lines."""
    lines = [_clean(l) for l in str(text or "").splitlines()]
    lines = [l for l in lines if l.strip()]
    return "\n".join(lines[-6:])


def _preflight(url, state, log):
    """Ask the site once whether it will talk to us at all.

    A signal, not a verdict: a site that refuses this request may still let
    real Chrome through, so the scan goes ahead anyway. Only a name that does
    not resolve - or a connection refused twice - ends the run here, because
    there is nothing to scan and no reason to spend a worker finding that out
    the slow way.
    """
    signal = runstate.preflight(url, timeout=settings.PREFLIGHT_TIMEOUT)
    kind = signal.get("kind")
    if kind == "unreachable":                 # one bounded retry: it may be transient
        signal = runstate.preflight(url, timeout=settings.PREFLIGHT_TIMEOUT)
        kind = signal.get("kind")
    if not kind:
        return None
    state.site_signal(kind, signal.get("detail", ""))
    notice = runstate.friendly(kind)
    log(f"Pre-flight: {signal.get('detail') or kind}.")
    if kind in ("dns", "unreachable"):
        log(f"{notice['title']} - {notice['message']}")
        state.stage(STAGE_CRAWL, "skipped", signal.get("detail", ""))
        return kind
    # blocked / tls: the real browser may still get through, so keep going and
    # let the recorded outcome of the actual pages have the last word.
    log("Continuing anyway - the scanners drive a real browser, which the site "
        "may treat differently.")
    return None


STAGE_CRAWL = "crawl"
STAGE_LH = "lighthouse"
STAGE_A11Y = "accessibility"
STAGE_SEC = "security"

# The Chrome flags every Lighthouse audit has always run with.
BASE_CHROME_FLAGS = "--headless=new --no-sandbox --ignore-certificate-errors"


def _chrome_flags(cfg):
    """`--chrome-flags=...` for one Lighthouse audit.

    A custom viewport is emulated by Lighthouse (`--screenEmulation.*`), but
    Chrome's own window still has to be big enough to hold it, or a wide
    desktop viewport gets a scrollbar it would never have in real life.
    """
    flags = BASE_CHROME_FLAGS
    width, height, _dpr = cfg.screen("")
    if width and height:
        flags += f" --window-size={width},{height}"
    return f"--chrome-flags={flags}"


def _crawl_auth_flags(credentials):
    """`unlighthouse-ci`'s own --auth / --cookies, or nothing."""
    return credentials.crawl_flags() if credentials else []


def _browser_kwargs(cfg, credentials):
    """The `browser=` keyword for a Playwright-driven scanner, or {}.

    Returned as kwargs rather than a value so a run with neither credentials
    nor emulation calls the scanner with the exact signature it always used.
    """
    context = dict(cfg.browser_context())
    if credentials:
        context.update(credentials.browser_payload())
    return {"browser": context} if context else {}


def run_pipeline(url, device, samples, deep, max_pages, concurrency, security, categories, log,
                 a11y=False, standards=None, scan_config=None, credentials=None):
    """Generator-free worker: does the whole scan, returns the run folder name.
    `categories` = selected Lighthouse categories (subset of
    performance/accessibility/best-practices/seo). Empty means security-only.
    `a11y` turns on the optional axe-core accessibility-compliance scan and
    `standards` picks which legal frameworks it reports on.

    `scan_config` (a `scanconfig.ScanConfig`) and `credentials` (a
    `scanauth.Credentials`) are inputs to the scanners and nothing more: they
    add flags to the `unlighthouse-ci` and `lighthouse` command lines and, for
    a scripted login, one browser run before the crawl. Neither touches the
    scoring, the metrics or the report - with both left at their defaults the
    commands below are exactly the ones this pipeline has always run.

    Credentials never leave this function's memory. Every line logged from here
    down goes through a redactor first, and nothing written to the run folder -
    the ledger, the reports, the Lighthouse output - is ever given them.

    The scan is partial-safe: every page is recorded as ok / blocked / timeout /
    error in a `RunState` that is persisted after each stage, and the run
    finishes with whatever succeeded rather than with nothing. None of the
    scoring, metrics or issue extraction below is affected by that - the
    ledger only records what happened."""
    cfg = scan_config if scan_config is not None else scanconfig.DEFAULT
    creds = credentials
    # From here on `log` cannot emit a credential, whoever generated the line.
    redact = scanauth.redactor_for(creds)
    log = redact.wrap(log)

    device = cfg.family(device)      # a chosen handset decides the form factor
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_name = f"{slugify(url)}-{device}-{stamp}"
    run_dir = RUNS_DIR / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    categories = categories or []
    lh_on = len(categories) > 0

    state = runstate.RunState(run_dir, run_name, url=url, device=device,
                              budget=settings.RUN_BUDGET)
    state.primary_stage = STAGE_CRAWL
    state.save()

    if not cfg.is_default:
        log(f"Scan conditions: {cfg.summary(device)}.")
    if creds:
        # What was supplied, never what it was. The ledger and the reports are
        # not told even this much.
        log(f"Authenticated scan: {creds.describe()}.")

    # 0) is the site reachable at all? (skipped in dry-run: there is no site)
    if not DRYRUN and _preflight(url, state, log):
        state.finalize()
        return run_name

    # 0b) a scripted login, if one was configured: sign in once in a real
    #     browser and carry the resulting session cookies through the scan.
    if creds and creds.login and not DRYRUN:
        try:
            import scanlogin
            scanlogin.perform(creds, scan_config=cfg, log=log)
        except Exception as e:                                     # noqa: BLE001
            # A failed login is not a failed run: the site may still serve
            # public pages, and the per-page ledger will show what happened.
            log(f"(Login failed: {redact.text(e)} - continuing unauthenticated.)")
            state.note("the scripted login failed; pages were requested without "
                       "a signed-in session")

    # 1) crawl
    log(f"Crawling {url} ({device}, {samples} sample(s))...")
    if DRYRUN:
        log("[dry-run] fabricating crawl results")
        make_mock(run_dir, url, device, deep)
        time.sleep(0.3)
    else:
        rc = run_stream(
            [NPX, "unlighthouse-ci", "--site", url, *cfg.crawl_flags(device),
             "--samples", str(samples),
             "--reporter", "jsonExpanded", *_crawl_auth_flags(creds)],
            cwd=run_dir, log=log, env=scan_env(), redact=redact,
        )
        if rc != 0:
            log(f"Crawl exited with code {rc} (continuing with whatever was written).")

    # locate ci-result.json to get the page list
    ci_files = list(run_dir.rglob("ci-result.json"))
    routes = []
    if ci_files:
        try:
            data = json.loads(ci_files[0].read_text(encoding="utf-8"))
            routes = data.get("routes", []) or []
        except Exception as e:
            log(f"Could not read ci-result.json: {e}")
    log(f"Discovered {len(routes)} page(s).")

    base = origin_of(url)

    def page_url(route):
        path = (route.get("path") or "/").replace("&amp;", "&")
        return urljoin(base + "/", path.lstrip("/"))

    # Everything the crawler measured is a real result in its own right - the
    # deep audit below only adds the itemized findings on top of it.
    for route in routes:
        state.record(page_url(route), STAGE_CRAWL, runstate.OK)
    if routes:
        state.stage(STAGE_CRAWL, "ok")
    else:
        state.stage(STAGE_CRAWL, "failed",
                    state.signal_detail or "the crawl discovered no pages")
    state.save()

    # Safety net: if the crawl found nothing (blocked crawler, odd sitemap, etc.)
    # but deep audit is on, still audit at least the entered URL so the run
    # produces a usable single-page report instead of failing outright.
    if not routes and deep and not DRYRUN:
        log("Crawl found no pages - falling back to auditing the entered URL only.")
        entered_path = urlparse(url).path or "/"
        routes = [{"path": entered_path}]

    # 2) deep audit per page (full recommendations + standalone Lighthouse HTML)
    if deep and lh_on and routes and not DRYRUN:
        state.primary_stage = STAGE_LH
        lhr = run_dir / "lhr"
        lhr.mkdir(exist_ok=True)
        targets = routes[:max_pages]
        if len(routes) > max_pages:
            log(f"Deep audit limited to first {max_pages} of {len(routes)} pages "
                f"(raise 'Max pages' to cover all).")
        # Emulation + throttling from the scan configuration (with everything
        # at its default this is the same `--preset=desktop`/nothing as before),
        # then the credential headers, which are never echoed anywhere.
        preset = cfg.lighthouse_flags(device)
        auth_flags = creds.lighthouse_flags() if creds else []
        only_cats = "--only-categories=" + ",".join(categories)
        lh = LIGHTHOUSE
        tries = 1 + settings.PAGE_RETRIES

        def audit_one(idx, route):
            """One page, with a bounded retry for a transient failure.

            A page the site clearly refused is never retried - asking a second
            time cannot change a 403, and it costs the run's budget."""
            full = page_url(route)
            stem = lhr / f"page_{idx:03d}"          # lighthouse appends .report.json/.html
            attempt = 0
            status, reason, detail, secs = runstate.SKIPPED, "not attempted", "", 0
            while attempt < tries:
                left = state.time_left()
                if left is not None and left <= 1:
                    if attempt == 0:
                        status, reason = runstate.SKIPPED, "run time limit reached"
                    break
                limit = (PER_PAGE_TIMEOUT if left is None
                         else max(5, min(PER_PAGE_TIMEOUT, int(left))))
                attempt += 1
                t0 = time.time()
                try:
                    proc = subprocess.run(
                        lh + [full, "--quiet",
                              "--output=json", "--output=html", f"--output-path={stem}",
                              only_cats,
                              "--no-enable-error-reporting",
                              _chrome_flags(cfg)] + preset + auth_flags,
                        cwd=str(run_dir), check=False, timeout=limit,
                        env=scan_env(), stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
                    )
                    output = getattr(proc, "stdout", "") or ""
                    secs = int(time.time() - t0)
                    status, reason, detail = _lh_outcome(output, stem)
                except subprocess.TimeoutExpired as e:
                    secs = int(time.time() - t0)
                    status, reason = runstate.TIMEOUT, "timeout"
                    detail = _clean_block(getattr(e, "output", ""))
                except Exception as e:                              # noqa: BLE001
                    secs = int(time.time() - t0)
                    status, reason, detail = runstate.ERROR, f"{type(e).__name__}: {e}", ""
                if status in (runstate.OK, runstate.BLOCKED):
                    break                       # done, or refused: retrying is pointless
            return {"url": full, "status": status, "reason": reason,
                    "seconds": secs, "attempts": attempt, "detail": detail}

        log(f"Deep-auditing {len(targets)} page(s) with {concurrency} in parallel"
            + (f", up to {tries} attempt(s) each" if tries > 1 else "") + "...")
        done = 0
        seen_detail = set()
        from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(audit_one, i, r): r for i, r in enumerate(targets, 1)}
            for n, fut in enumerate(as_completed(futures), 1):
                try:
                    out = fut.result()
                except CancelledError:
                    continue        # never started: recorded as skipped below
                retried = " after a retry" if out["attempts"] > 1 else ""
                if out["status"] == runstate.OK:
                    done += 1
                    log(f"[{n}/{len(targets)}] OK ({out['seconds']}s{retried}) {out['url']}")
                else:
                    log(f"[{n}/{len(targets)}] skipped ({out['reason']}{retried}) "
                        f"{out['url']}")
                    # The tool's own words, once per distinct failure, so 30
                    # identically blocked pages do not bury the log.
                    if out["detail"] and out["detail"] not in seen_detail:
                        seen_detail.add(out["detail"])
                        for line in out["detail"].splitlines():
                            log(f"    lighthouse: {line}")
                state.record(out["url"], STAGE_LH, out["status"], out["reason"],
                             seconds=out["seconds"], attempts=out["attempts"])
                if state.expired() and not state.budget_hit:
                    # One bad site must not hold a worker forever: stop handing
                    # out new pages and finalize with what completed.
                    state.budget_hit = True
                    state.note(f"the run reached its {state.budget}s time limit "
                               f"before every page was audited")
                    log(f"Run time limit ({state.budget}s) reached - finalizing with "
                        f"the pages completed so far.")
                    for pending in futures:
                        pending.cancel()
        state.skip_remaining([page_url(r) for r in targets], STAGE_LH,
                             "run time limit reached")
        state.stage(STAGE_LH, "ok" if done else "failed")
        cov = state.coverage(STAGE_LH)
        log(f"Deep audit complete: {done}/{len(targets)} full reports captured "
            f"({cov['percent']}% coverage).")
        state.save()
    elif deep and lh_on and DRYRUN:
        log("[dry-run] deep-audit reports fabricated")

    # 3) the optional scans run BEFORE the report is built, so their findings
    #    can go into it. Each still writes its own standalone page, exactly as
    #    it always did - the consolidated report is additional, not a
    #    replacement.
    def scan_urls():
        return [page_url(r) for r in routes[:max_pages]] or [url]

    def out_of_time(what):
        """True when the run's ceiling leaves no room for another stage."""
        if not state.expired():
            return False
        state.budget_hit = True
        state.note(f"the {what} scan was skipped - the run reached its "
                   f"{state.budget}s time limit")
        log(f"Run time limit reached - skipping the {what} scan.")
        return True

    security_data = None
    if security and not DRYRUN:
        if out_of_time("security"):
            state.stage(STAGE_SEC, "skipped", "run time limit reached")
        else:
            try:
                import security_scan as sec
                security_data = sec.collect_site(url, scan_urls(), log=log)
                (run_dir / "security.html").write_text(sec.html_from(security_data), encoding="utf-8")
                log("Security report ready.")
                _record_security(state, security_data, scan_urls())
                state.stage(STAGE_SEC, "ok")
            except Exception as e:
                security_data = None
                log(f"(Security scan failed: {e})")
                state.stage(STAGE_SEC, "failed", f"{type(e).__name__}: {e}")
                state.note(f"the security scan failed: {e}")
        state.save()
    elif security and DRYRUN:
        (run_dir / "security.html").write_text("<html><body>dry-run security</body></html>", encoding="utf-8")
        log("[dry-run] security report fabricated")

    a11y_data = None
    if a11y and not DRYRUN:
        if out_of_time("accessibility"):
            state.stage(STAGE_A11Y, "skipped", "run time limit reached")
        else:
            try:
                import accessibility_scan as a11y_mod
                # A page behind a login is not an accessible page, it is an
                # unmeasured one - so the axe runner gets the same session and
                # the same device the rest of the scan is using. The extras are
                # only passed when there are any, so an ordinary run calls
                # collect_site exactly as it always did.
                extra = _browser_kwargs(cfg, creds)
                a11y_data = a11y_mod.collect_site(url, scan_urls(), standards=standards,
                                                  concurrency=concurrency,
                                                  work_dir=run_dir / "axe", log=log,
                                                  **extra)
                (run_dir / "accessibility.html").write_text(
                    a11y_mod.html_from(a11y_data), encoding="utf-8")
                log("Accessibility report ready.")
                _record_accessibility(state, a11y_data)
                state.stage(STAGE_A11Y, "ok" if a11y_data.get("pages_analyzed") else "failed")
            except Exception as e:
                a11y_data = None
                log(f"(Accessibility scan failed: {e})")
                state.stage(STAGE_A11Y, "failed", f"{type(e).__name__}: {e}")
                state.note(f"the accessibility scan failed: {e}")
        state.save()
    elif a11y and DRYRUN:
        (run_dir / "accessibility.html").write_text(
            "<html><body>dry-run accessibility</body></html>", encoding="utf-8")
        log("[dry-run] accessibility report fabricated")

    # Whichever scan the report leans on decides what "coverage" counts: with
    # no deep audit, that is the scan that actually measured the pages.
    if state.primary_stage == STAGE_CRAWL:
        if a11y_data:
            state.primary_stage = STAGE_A11Y
        elif security_data:
            state.primary_stage = STAGE_SEC

    # 4) one consolidated report covering every category that ran
    # Which modes produce a report is unchanged: a Lighthouse category still has
    # to have run. What changed is its contents - the accessibility and security
    # findings from this same run now go into it instead of living only on their
    # own pages, and a partial run says so on its face instead of reading as a
    # clean pass.
    report_html = run_dir / "report.html"
    wrote_report = False
    if lh_on:
        log("Building consolidated report...")
        ci_pages = cr.load_ci_results([run_dir])
        lhr_pages = cr.load_lhr_pages([run_dir])
        pages = cr.merge(ci_pages, lhr_pages)
        if pages:
            report_html.write_text(
                cr.build_html(pages, [run_dir], out_path=str(report_html),
                              categories=categories, site_url=url, device=device,
                              accessibility=a11y_data, security=security_data,
                              standards=standards,
                              coverage=state.report_coverage(),
                              scope={"pages_crawled": len(routes),
                                     "pages_deep_audited": min(len(routes), max_pages) if deep else 0,
                                     "samples": samples, "max_pages": max_pages}),
                encoding="utf-8")
            wrote_report = True
            log(f"Report ready: {len(pages)} page(s).")
        else:
            log("No performance data produced.")
    else:
        log("No Lighthouse category selected - skipping the deep audit "
            "and the performance report.")

    # 5) the print-perfect PDF of whatever the report ended up covering
    if not DRYRUN and wrote_report and report_html.exists():
        try:
            info = cr.html_to_pdf(str(report_html), str(run_dir / "report.pdf"), log=log)
            log(f"PDF exported (paginated by {info['engine']}).")
        except Exception as e:
            log(f"(PDF skipped: {e})")

    status = state.finalize()
    _log_outcome(state, status, log)
    return run_name


def _record_security(state, data, urls):
    """Per-page outcomes from the passive security scan.

    It reports the pages it could not fetch; everything it did fetch is an ok.
    """
    failures = {str(f.get("url")): f for f in (data.get("failures") or [])
                if isinstance(f, dict)}
    for url in urls:
        failure = failures.get(str(url))
        if failure is None:
            state.record(url, STAGE_SEC, runstate.OK)
            continue
        why = str(failure.get("why") or "")
        state.record(url, STAGE_SEC, runstate.page_status(runstate.classify(why)),
                     why or "could not be fetched")


def _record_accessibility(state, data):
    """Per-page outcomes from the axe scan, which already tracks its own."""
    for url in data.get("analyzed") or []:
        if url:
            state.record(url, STAGE_A11Y, runstate.OK)
    for failure in data.get("failures") or []:
        if not isinstance(failure, dict) or not failure.get("url"):
            continue
        why = str(failure.get("why") or "")
        state.record(failure["url"], STAGE_A11Y,
                     runstate.page_status(runstate.classify(why)),
                     why or "could not be analyzed")


def _log_outcome(state, status, log):
    """The last word in the log: what this run actually covers."""
    if status == runstate.COMPLETE:
        log(f"Run complete - {runstate.coverage_sentence(state.report_coverage())}.")
        return
    notice = state.notice() or {}
    if status == runstate.FAILED:
        log(f"Run FAILED - {notice.get('title', 'the scan could not be completed')}. "
            f"{notice.get('message', '')}")
        return
    log(f"Run PARTIAL - {runstate.coverage_sentence(state.report_coverage())}. "
        f"Pages that were not measured are unmeasured, not clean.")
    for failure in state.failures()[:10]:
        log(f"  - {failure['status']}: {failure['url']} ({failure['reason']})")
    if len(state.failures()) > 10:
        log(f"  - ... and {len(state.failures()) - 10} more")


def run_status(run_name):
    """The persisted status ledger of a run, or None when it has none."""
    return runstate.RunState.load(RUNS_DIR / run_name)


# Report files a run can produce, in the order the UI offers them.
REPORT_FILES = ["report.html", "report.pdf", "accessibility.html", "security.html"]


def run_artifacts(run_name):
    """The REPORT_FILES a finished run actually wrote (so the UI only offers
    buttons that lead somewhere)."""
    run_dir = RUNS_DIR / run_name
    return [f for f in REPORT_FILES if (run_dir / f).is_file()]


# ----------------------------- routes -----------------------------

@app.route("/")
def index():
    resp = make_response(PAGE)
    if not session_id():
        # Identifies the browser for rate limiting only - no personal data, and
        # it never leaves this server.
        resp.set_cookie(SESSION_COOKIE, new_session_id(), max_age=30 * 86400,
                        samesite="Lax", httponly=True)
    return resp


@app.route("/healthz")
def healthz():
    backend = queue_backend()
    return jsonify({"ok": True, "queue": backend.name, "depth": backend.depth(),
                    "max_depth": backend.max_depth})


@app.route("/api/limits")
def api_limits():
    """What this deployment allows - the UI shows the caps up front instead of
    silently shrinking a scan the user asked for."""
    return jsonify({
        "max_pages": settings.CAP_MAX_PAGES,
        "samples": settings.CAP_SAMPLES,
        "parallel": settings.CAP_PARALLEL,
        "scans_per_hour": settings.MAX_SCANS_PER_HOUR,
        "concurrent_scans": settings.MAX_CONCURRENT_SCANS,
        "queue_depth": settings.MAX_QUEUE_DEPTH,
        "scan_timeout": settings.SCAN_TIMEOUT,
        "verification_required": settings.REQUIRE_DOMAIN_VERIFICATION,
        "rate_limit_enabled": settings.RATE_LIMIT_ENABLED,
        "scan_auth_enabled": settings.ALLOW_SCAN_AUTH,
    })


@app.route("/api/verify")
def api_verify():
    """The token to publish for a domain, and whether it is already there."""
    url, error = guardrails.normalize_url(request.args.get("url"))
    if error:
        return jsonify({"error": error}), 400
    domain = guardrails.registrable_domain(url)
    payload = verification.instructions(domain)
    recheck = request.args.get("check") == "1"
    payload["verified"] = (verification.is_verified(domain, use_cache=not recheck)
                           if settings.REQUIRE_DOMAIN_VERIFICATION or recheck else True)
    payload["required"] = settings.REQUIRE_DOMAIN_VERIFICATION
    return jsonify(payload)


@app.route("/api/job/<job_id>")
def api_job(job_id):
    """Job state on its own, for a client that lost its stream."""
    return jsonify(queue_backend().state(job_id))


class Rejected(Exception):
    """A request that will not be queued, with the reason the client is told."""

    def __init__(self, reason, message, extra=None):
        super().__init__(message)
        self.reason, self.message, self.extra = reason, message, extra or {}


def _admit(source, credentials, sid):
    """Cap, rate-limit, gate and enqueue one scan.

    Returns `(job_id, keys, capped, params)`, or raises `Rejected`. This is the
    whole admission path, shared by the GET stream and the POST submission, so
    the two cannot drift apart on what a client is allowed to ask for.

    `credentials` is handed to the queue *beside* the job rather than inside
    it: `params` is what gets stored and echoed, and a credential is never
    part of that.
    """
    params, error, capped = guardrails.sanitize_params(source)
    if error:
        raise Rejected("invalid", error)

    backend = queue_backend()
    limiter = rate_limiter()
    keys = guardrails.client_keys(request.remote_addr, sid)

    try:
        limiter.check(keys)
    except guardrails.RateLimitError as exc:
        raise Rejected(exc.reason, exc.message)

    allowed, detail = verification.gate(guardrails.registrable_domain(params["url"]),
                                        guardrails.is_multipage_scan(params))
    if not allowed:
        raise Rejected("unverified", detail["message"], {"verification": detail})

    job_id = jobqueue.new_job_id()
    limiter.reserve(keys, job_id)       # hold the slot before the job can start
    try:
        backend.enqueue(params, client=keys[0][1], job_id=job_id,
                        credentials=credentials or None)
    except jobqueue.QueueFull:
        limiter.release(keys, job_id)   # a refusal costs the client nothing
        raise Rejected(
            "queue_full",
            f"All {backend.max_depth} queue slots are taken right now. "
            "Please try again shortly - nothing was lost.")
    limiter.charge(keys, job_id)
    return job_id, keys, capped, params


def _stream_token(job_id, sid):
    """Proof that the browser following a job is the one that started it.

    The stream carries a scan's whole log, so it is not something a job id
    guessed off the wire should open.

    It reuses the deployment's one cross-process secret, with the purpose
    written into the message so a stream token and a domain-verification token
    can never be mistaken for one another.
    """
    key = hashlib.sha256((sid or "").encode()).hexdigest()
    mac = hmac.new(settings.VERIFY_TOKEN_SECRET.encode(),
                   f"scan-stream:{job_id}|{key}".encode(), hashlib.sha256)
    return mac.hexdigest()[:32]


def _stream_response(job_id, keys, capped, params):
    return Response(_stream(job_id, keys, capped, params),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "X-Scan-Job": job_id})


@app.route("/scan", methods=["GET"])
def scan():
    """Admit a scan and stream it, in one request.

    The original entry point, unchanged and still the whole API for an
    unauthenticated scan. It reads the query string, which is precisely why it
    never accepts credentials: a query string is written to access logs, sent
    in `Referer` headers, and kept in the user's own history. Anything secret
    comes in through POST below.
    """
    try:
        job_id, keys, capped, params = _admit(request.args, None, session_id())
    except Rejected as exc:
        return _rejection(exc.reason, exc.message, exc.extra)

    resp = _stream_response(job_id, keys, capped, params)
    if not session_id():
        resp.set_cookie(SESSION_COOKIE, new_session_id(), max_age=30 * 86400,
                        samesite="Lax", httponly=True)
    return resp


@app.route("/scan", methods=["POST"])
def scan_submit():
    """Submit a scan whose settings include credentials.

    The two halves of a scan are split here on purpose: this POST carries the
    request body (where a password may live and where nothing logs it), and
    the browser then follows the job with an EventSource on `/scan/stream/...`,
    which carries nothing but the job id and a token. A credential therefore
    never appears in a URL, an access log, or a browser history entry.

    Answers with the job to follow, or 4xx and the reason it was refused.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = request.form
    credentials, error = guardrails.extract_credentials(body)
    if error:
        return jsonify({"reason": "invalid", "message": error}), 400

    sid = session_id() or new_session_id()
    try:
        job_id, keys, capped, params = _admit(body, credentials, sid)
    except Rejected as exc:
        payload = {"reason": exc.reason, "message": exc.message}
        payload.update(exc.extra)
        status = 429 if exc.reason in ("hourly", "concurrent", "queue_full") else 400
        return jsonify(payload), status
    finally:
        # Whatever happened, this process is done with them: either the queue
        # has taken a copy, or the request was refused and there is nothing to
        # take. Either way they do not stay in the web process's memory.
        if credentials is not None:
            credentials.scrub()

    resp = jsonify({
        "job": job_id,
        "capped": capped,
        # Only ever the public half of the request - the client already knows
        # what it sent, and this is what a "capped" notice is built from.
        "params": {k: params[k] for k in ("max_pages", "samples", "concurrency")},
        "stream": f"/scan/stream/{job_id}?t={_stream_token(job_id, sid)}",
    })
    resp.headers["X-Scan-Job"] = job_id
    if not session_id():
        resp.set_cookie(SESSION_COOKIE, sid, max_age=30 * 86400,
                        samesite="Lax", httponly=True)
    return resp


@app.route("/scan/stream/<job_id>")
def scan_stream(job_id):
    """Follow a job submitted by POST. Carries no scan settings at all."""
    sid = session_id()
    token = request.args.get("t") or ""
    if not hmac.compare_digest(token, _stream_token(job_id, sid)):
        return _rejection("invalid", "That scan belongs to a different session.")
    keys = guardrails.client_keys(request.remote_addr, sid)
    state = queue_backend().state(job_id)
    if state["state"] == jobqueue.UNKNOWN:
        return _rejection("invalid", "That scan is no longer available.")
    return _stream_response(job_id, keys, [], None)


KEEPALIVE_EVERY = 15.0   # seconds of silence before a comment frame


def _stream(job_id, keys, capped, params):
    """Follow one job: queue position, then its log, then the outcome."""
    backend = queue_backend()

    def gen():
        cursor = 0
        position = object()          # a sentinel that no position equals
        running_announced = False
        last_output = time.time()
        # `params` is None for a stream the client is only *following*: it was
        # told what was accepted in the POST's own answer, and repeating a
        # fabricated copy of it here would be worse than saying nothing.
        yield _sse("accepted", {"job": job_id, "capped": capped,
                                "params": {k: params[k] for k in
                                           ("max_pages", "samples", "concurrency")}
                                if params else {}})
        if capped:
            yield f"data: {_capped_note(capped, params)}\n\n"
        while True:
            # State first, so "position 3" and "running" arrive before the log
            # lines they explain.
            state = backend.state(job_id)
            if (state["state"] == jobqueue.QUEUED and state["position"] is not None
                    and state["position"] != position):
                position = state["position"]
                last_output = time.time()
                yield _sse("queued", {"position": position, "depth": backend.depth()})
            elif state["state"] == jobqueue.RUNNING and not running_announced:
                running_announced = True
                last_output = time.time()
                yield _sse("running", {"job": job_id})

            lines, cursor = backend.logs(job_id, cursor)
            for line in lines:
                last_output = time.time()
                yield f"data: {line}\n\n"

            if state["state"] in jobqueue.TERMINAL:
                if not running_announced:
                    # A short scan can go queued -> done between two polls; the
                    # browser still needs to see that it left the queue.
                    running_announced = True
                    yield _sse("running", {"job": job_id})
                lines, cursor = backend.logs(job_id, cursor)     # drain the tail
                for line in lines:
                    yield f"data: {line}\n\n"
                if state["state"] == jobqueue.DONE and state["result"]:
                    yield _sse("done", state["result"])
                else:
                    # A job that died without leaving a report still owes the
                    # person a sentence they can act on, not the raw error.
                    error = state["error"] or "scan failed"
                    yield _sse("fail", {"message": error,
                                        "notice": runstate.friendly_from_error(error)})
                rate_limiter().release(keys, job_id)
                break

            if time.time() - last_output > KEEPALIVE_EVERY:
                last_output = time.time()
                yield ": keepalive\n\n"       # keeps idle proxies from hanging up
            backend.wait(job_id, 0.25)

    return gen()


def _capped_note(capped, params):
    labels = {"max_pages": f"max pages -> {params['max_pages']}",
              "samples": f"samples -> {params['samples']}",
              "parallel": f"parallel -> {params['concurrency']}"}
    return ("Server limits applied: "
            + ", ".join(labels[name] for name in capped if name in labels) + ".")


@app.route("/runs/<path:relpath>")
def serve_run(relpath):
    # only serve files inside RUNS_DIR
    target = (RUNS_DIR / relpath).resolve()
    if RUNS_DIR.resolve() not in target.parents:
        abort(403)
    return send_from_directory(target.parent, target.name)


# ----------------------------- UI -----------------------------

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Site Performance & Security Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#0b1020; --bg2:#0e1530; --panel:#ffffff; --ink:#0f172a; --muted:#64748b;
    --line:#e6e9ef; --brand:#4f46e5; --brand2:#7c3aed; --ring:rgba(79,70,229,.35);
    --perf:#f59e0b; --a11y:#0ea5e9; --bp:#10b981; --seo:#8b5cf6; --sec:#ef4444; --wcag:#6b4fc4;
    --shadow:0 10px 40px rgba(15,23,42,.10), 0 2px 8px rgba(15,23,42,.05);
  }
  *{box-sizing:border-box}
  html,body{margin:0}
  body{
    font-family:'Inter',-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
    color:var(--ink); min-height:100vh;
    background:
      radial-gradient(1100px 500px at 12% -8%, rgba(124,58,237,.25), transparent 60%),
      radial-gradient(900px 500px at 100% 0%, rgba(79,70,229,.28), transparent 55%),
      linear-gradient(180deg,var(--bg),var(--bg2));
    background-attachment:fixed;
  }
  .shell{max-width:960px;margin:0 auto;padding:40px 22px 72px}
  /* header */
  .brand{display:flex;align-items:center;gap:14px;color:#fff;margin-bottom:6px}
  .logo{width:44px;height:44px;border-radius:13px;display:grid;place-items:center;
    background:linear-gradient(140deg,var(--brand),var(--brand2));box-shadow:0 8px 24px rgba(79,70,229,.45)}
  .logo svg{width:24px;height:24px;stroke:#fff}
  .brand h1{font-size:22px;font-weight:800;letter-spacing:-.02em;margin:0;line-height:1.1}
  .brand p{margin:2px 0 0;color:#c7d2fe;font-size:13px;font-weight:500}
  .pill{margin-left:auto;font-size:11px;font-weight:600;color:#c7d2fe;border:1px solid rgba(199,210,254,.35);
    padding:5px 11px;border-radius:999px;background:rgba(255,255,255,.05)}
  /* card */
  .card{background:var(--panel);border-radius:20px;box-shadow:var(--shadow);padding:26px;margin-top:22px;
    border:1px solid rgba(255,255,255,.6)}
  .lbl{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:9px;display:block}
  .field{position:relative}
  .field svg{position:absolute;left:14px;top:50%;transform:translateY(-50%);width:18px;height:18px;stroke:var(--muted)}
  input[type=text]{width:100%;padding:14px 14px 14px 42px;border:1.5px solid var(--line);border-radius:12px;
    font-size:15.5px;font-family:inherit;color:var(--ink);transition:.15s;background:#fbfcfe}
  input[type=text]:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 4px var(--ring);background:#fff}
  .grid{display:grid;grid-template-columns:1fr auto;gap:18px;align-items:end}
  @media(max-width:640px){.grid{grid-template-columns:1fr}}
  /* segmented device control */
  .seg{display:inline-flex;background:#f1f3f9;border-radius:12px;padding:4px;gap:4px}
  .seg button{border:0;background:transparent;padding:10px 16px;border-radius:9px;font:600 14px inherit;color:var(--muted);
    cursor:pointer;display:flex;align-items:center;gap:7px;transition:.15s}
  .seg button svg{width:16px;height:16px;stroke:currentColor}
  .seg button.on{background:#fff;color:var(--brand);box-shadow:0 2px 8px rgba(15,23,42,.12)}
  /* category chips */
  .cats{display:flex;flex-wrap:wrap;gap:10px;margin-top:4px}
  .chip{--c:var(--brand);cursor:pointer;user-select:none;display:flex;align-items:center;gap:8px;
    padding:10px 15px;border-radius:12px;border:1.5px solid var(--line);background:#fff;font:600 13.5px inherit;
    color:var(--muted);transition:.15s;position:relative}
  .chip .dot{width:9px;height:9px;border-radius:50%;background:var(--c);opacity:.4;transition:.15s}
  .chip:hover{border-color:var(--c);color:var(--ink)}
  .chip.on{color:#fff;background:var(--c);border-color:var(--c);box-shadow:0 6px 16px -4px var(--c)}
  .chip.on .dot{background:#fff;opacity:1}
  .chip[data-cat=performance]{--c:var(--perf)} .chip[data-cat=accessibility]{--c:var(--a11y)}
  .chip[data-cat=best-practices]{--c:var(--bp)} .chip[data-cat=seo]{--c:var(--seo)}
  .chip[data-cat=security]{--c:var(--sec)}
  .chip[data-cat=a11y-standards]{--c:var(--wcag)}
  .chip.std{--c:var(--wcag);padding:8px 12px;font-size:12.5px}
  /* advanced */
  details.adv{margin-top:20px;border-top:1px solid var(--line);padding-top:16px}
  details.adv summary{cursor:pointer;font:600 13px inherit;color:var(--brand);list-style:none;display:flex;align-items:center;gap:6px}
  details.adv summary::-webkit-details-marker{display:none}
  details.adv summary svg{width:14px;height:14px;stroke:currentColor;transition:.2s}
  details.adv[open] summary svg{transform:rotate(90deg)}
  .adv-grid{display:flex;flex-wrap:wrap;gap:20px;margin-top:14px;align-items:center}
  .adv-grid label.sw{display:flex;align-items:center;gap:9px;font:500 14px inherit;color:#334155;cursor:pointer}
  .num{display:flex;flex-direction:column;gap:5px}
  .num span{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
  .num input{width:84px;padding:9px 11px;border:1.5px solid var(--line);border-radius:9px;font:600 14px inherit;color:var(--ink)}
  .num input:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 3px var(--ring)}
  .num.wide input{width:200px}
  .num.grow{flex:1 1 240px}
  .num.grow input{width:100%}
  /* advanced groups */
  .agroup{margin-top:22px;padding-top:16px;border-top:1px dashed var(--line)}
  .agroup:first-of-type{border-top:0;padding-top:0}
  .agroup > .atitle{font:700 12px inherit;letter-spacing:.06em;text-transform:uppercase;color:#334155;
    display:flex;align-items:center;gap:8px;margin-bottom:4px}
  .agroup > .ahint{font:500 12.5px/1.5 inherit;color:var(--muted);margin-bottom:12px}
  .sel{display:flex;flex-direction:column;gap:5px}
  .sel span{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
  .sel select{padding:9px 11px;border:1.5px solid var(--line);border-radius:9px;font:600 14px inherit;
    color:var(--ink);background:#fbfcfe;min-width:230px}
  .sel select:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 3px var(--ring)}
  .num input[type=password],.num input[type=text].tin{width:200px}
  .num textarea{width:100%;min-height:70px;padding:9px 11px;border:1.5px solid var(--line);border-radius:9px;
    font:12.5px/1.5 ui-monospace,Menlo,Consolas,monospace;color:var(--ink);resize:vertical}
  .num textarea:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 3px var(--ring)}
  /* the credentials block says, on its face, what happens to what you type */
  .agroup.secret{border:1.5px solid #ddd0f7;background:#faf8ff;border-radius:14px;padding:16px 18px;margin-top:22px}
  .agroup.secret > .atitle{color:var(--wcag)}
  .privacy{display:flex;gap:9px;align-items:flex-start;font:500 12.5px/1.55 inherit;color:#475569;
    background:#fff;border:1px solid #ddd0f7;border-radius:10px;padding:10px 12px;margin-bottom:14px}
  .privacy svg{width:15px;height:15px;stroke:var(--wcag);flex:0 0 auto;margin-top:2px}
  /* toggle switch */
  .switch{position:relative;width:40px;height:23px;flex:0 0 auto}
  .switch input{opacity:0;width:0;height:0}
  .track{position:absolute;inset:0;background:#cbd5e1;border-radius:999px;transition:.2s}
  .track:before{content:"";position:absolute;width:17px;height:17px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.2s;box-shadow:0 1px 3px rgba(0,0,0,.2)}
  .switch input:checked + .track{background:var(--brand)}
  .switch input:checked + .track:before{transform:translateX(17px)}
  /* CTA */
  .cta{margin-top:22px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
  .go{border:0;cursor:pointer;color:#fff;font:700 15.5px inherit;padding:14px 26px;border-radius:13px;
    background:linear-gradient(135deg,var(--brand),var(--brand2));box-shadow:0 10px 24px -6px var(--ring);
    display:flex;align-items:center;gap:9px;transition:.15s}
  .go:hover{transform:translateY(-1px);box-shadow:0 14px 30px -6px var(--ring)}
  .go:disabled{opacity:.6;cursor:default;transform:none}
  .go svg{width:18px;height:18px;stroke:#fff}
  .hint{color:var(--muted);font-size:12.5px}
  /* log */
  /* queue + refusal notices */
  .notice{display:none;margin-top:18px;border:1.5px solid #fde68a;background:#fffbeb;border-radius:14px;padding:16px 18px}
  .notice.err{border-color:#fecaca;background:#fef2f2}
  .notice .ntitle{font:700 13.5px inherit;color:#92400e;margin-bottom:5px}
  .notice.err .ntitle{color:#991b1b}
  .notice .nmsg{font:500 13.5px/1.55 inherit;color:#475569}
  .verify{margin-top:14px;display:grid;gap:6px}
  .verify code{display:block;background:#0b1020;color:#c7d2e6;border-radius:9px;padding:10px 12px;
    font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;word-break:break-all;user-select:all}
  .verify .vbtns{display:flex;gap:10px;flex-wrap:wrap;margin-top:8px}
  .qbadge{margin-left:auto;font:700 11px inherit;letter-spacing:.06em;text-transform:uppercase;
    color:var(--brand);background:#eef2ff;border-radius:999px;padding:4px 10px}
  /* the state of a run, in plain language - the log is for the curious */
  .statebox{margin:0 0 12px;border-radius:12px;padding:13px 15px;border:1.5px solid var(--line);background:#fff}
  .statebox.ok{border-color:#bbf7d0;background:#f0fdf4}
  .statebox.warn{border-color:#fde68a;background:#fffbeb}
  .statebox.err{border-color:#fecaca;background:#fef2f2}
  .statebox .stitle{font:700 13.5px inherit;color:var(--ink);margin-bottom:4px}
  .statebox .smsg{font:500 13.5px/1.55 inherit;color:#475569}
  .statebox .scov{font:600 12.5px inherit;color:var(--muted);margin-top:6px}
  .logdetails{margin-top:4px}
  .logdetails summary{cursor:pointer;font:600 12.5px inherit;color:var(--muted);padding:2px 0}
  .logdetails summary:hover{color:var(--brand)}
  .logwrap{margin-top:20px;display:none}
  .logbar{display:flex;align-items:center;gap:10px;font:600 13px inherit;color:#334155;margin-bottom:8px}
  .spin{width:15px;height:15px;border:2.5px solid #dbe1ea;border-top-color:var(--brand);border-radius:50%;animation:sp .7s linear infinite}
  @keyframes sp{to{transform:rotate(360deg)}}
  #log{background:#0b1020;color:#c7d2e6;font:12px/1.6 ui-monospace,Menlo,Consolas,monospace;
    padding:14px 16px;border-radius:12px;max-height:280px;overflow:auto;white-space:pre-wrap;border:1px solid #1e293b}
  /* results */
  .results{display:none;margin-top:24px}
  .rbar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}
  .btn{display:inline-flex;align-items:center;gap:8px;text-decoration:none;font:600 13.5px inherit;
    padding:10px 16px;border-radius:11px;border:1.5px solid var(--line);color:var(--ink);background:#fff;transition:.15s}
  .btn:hover{border-color:var(--brand);color:var(--brand)}
  .btn.primary{background:linear-gradient(135deg,var(--brand),var(--brand2));color:#fff;border:0}
  .btn.sec{border-color:#fecaca;color:var(--sec)} .btn.sec:hover{background:#fef2f2}
  .btn.wcag{border-color:#ddd0f7;color:var(--wcag)} .btn.wcag:hover{background:#f6f2ff}
  .btn svg{width:15px;height:15px;stroke:currentColor}
  .frame{background:#fff;border:1px solid var(--line);border-radius:14px;overflow:hidden;box-shadow:var(--shadow)}
  .frame .cap{padding:9px 14px;font:600 12px inherit;color:var(--muted);border-bottom:1px solid var(--line);
    display:flex;align-items:center;gap:8px;background:#fbfcfe}
  iframe{width:100%;height:66vh;border:0;background:#fff;display:block}
  .foot{color:#93a2c2;font-size:12px;text-align:center;margin-top:26px}
</style></head>
<body>
<div class="shell">
  <div class="brand">
    <div class="logo"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M7 14l3-4 3 3 4-6"/></svg></div>
    <div>
      <h1>Site Performance &amp; Security</h1>
      <p>Whole-site audits &mdash; performance, accessibility, SEO, best practices, security &amp; WCAG standards.</p>
    </div>
    <span class="pill">&#9679; Runs locally &middot; private</span>
  </div>

  <div class="card">
    <div class="grid">
      <div>
        <label class="lbl">Website URL</label>
        <div class="field">
          <svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 0 1 0 18a15 15 0 0 1 0-18"/></svg>
          <input id="url" type="text" placeholder="https://example.com" autocomplete="off" spellcheck="false">
        </div>
      </div>
      <div>
        <label class="lbl">Device</label>
        <div class="seg" id="device">
          <button data-v="desktop" class="on" type="button"><svg viewBox="0 0 24 24" fill="none" stroke-width="2"><rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8M12 17v4"/></svg>Desktop</button>
          <button data-v="mobile" type="button"><svg viewBox="0 0 24 24" fill="none" stroke-width="2"><rect x="7" y="3" width="10" height="18" rx="2"/><path d="M11 18h2"/></svg>Mobile</button>
        </div>
      </div>
    </div>

    <div style="margin-top:22px">
      <label class="lbl">Audit categories &mdash; tap to include</label>
      <div class="cats" id="cats">
        <div class="chip on" data-cat="performance"><span class="dot"></span>Performance</div>
        <div class="chip on" data-cat="accessibility"><span class="dot"></span>Accessibility</div>
        <div class="chip on" data-cat="best-practices"><span class="dot"></span>Best Practices</div>
        <div class="chip on" data-cat="seo"><span class="dot"></span>SEO</div>
        <div class="chip" data-cat="security"><span class="dot"></span>Security</div>
        <div class="chip" data-cat="a11y-standards"><span class="dot"></span>Accessibility standards</div>
      </div>
    </div>

    <div id="stdwrap" style="margin-top:22px;display:none">
      <label class="lbl">Accessibility standards &mdash; frameworks to report</label>
      <div class="cats" id="stds">
        <div class="chip std" data-std="wcag20a"><span class="dot"></span>WCAG 2.0 A</div>
        <div class="chip std" data-std="wcag20aa"><span class="dot"></span>WCAG 2.0 AA</div>
        <div class="chip std" data-std="wcag20aaa"><span class="dot"></span>WCAG 2.0 AAA</div>
        <div class="chip std" data-std="wcag21a"><span class="dot"></span>WCAG 2.1 A</div>
        <div class="chip std on" data-std="wcag21aa"><span class="dot"></span>WCAG 2.1 AA</div>
        <div class="chip std" data-std="wcag21aaa"><span class="dot"></span>WCAG 2.1 AAA</div>
        <div class="chip std" data-std="wcag22a"><span class="dot"></span>WCAG 2.2 A</div>
        <div class="chip std" data-std="wcag22aa"><span class="dot"></span>WCAG 2.2 AA</div>
        <div class="chip std" data-std="wcag22aaa"><span class="dot"></span>WCAG 2.2 AAA</div>
        <div class="chip std on" data-std="section508"><span class="dot"></span>Section 508 (US)</div>
        <div class="chip std on" data-std="en301549"><span class="dot"></span>EN 301 549 (EU)</div>
        <div class="chip std on" data-std="ada"><span class="dot"></span>ADA Title III (US)</div>
        <div class="chip std" data-std="unruh"><span class="dot"></span>California Unruh</div>
        <div class="chip std" data-std="aoda"><span class="dot"></span>Ontario AODA</div>
        <div class="chip std" data-std="uk_equality"><span class="dot"></span>UK Equality Act</div>
        <div class="chip std" data-std="au_dda"><span class="dot"></span>Australian DDA</div>
        <div class="chip std" data-std="il_5568"><span class="dot"></span>Israeli 5568</div>
        <div class="chip std" data-std="ca_aca"><span class="dot"></span>Canada ACA</div>
        <div class="chip std" data-std="atag20"><span class="dot"></span>ATAG 2.0</div>
      </div>
      <span class="hint" style="display:block;margin-top:10px">Automated axe-core checks only &mdash; the report states automated results per framework, never a legal compliance verdict. ATAG 2.0 is reported as not applicable (it governs authoring tools, not pages).</span>
    </div>

    <details class="adv">
      <summary><svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round"><path d="M9 6l6 6-6 6"/></svg>Advanced options</summary>

      <div class="agroup">
        <div class="atitle">Scan depth</div>
        <div class="adv-grid">
          <label class="sw"><span class="switch"><input type="checkbox" id="deep" checked><span class="track"></span></span>Fix recommendations &amp; per-page reports</label>
          <div class="num"><span>Samples</span><input id="samples" type="number" min="1" max="5" value="1"></div>
          <div class="num"><span>Max pages</span><input id="maxpages" type="number" min="1" max="200" value="30"></div>
          <div class="num"><span>Parallel</span><input id="concurrency" type="number" min="1" max="6" value="3"></div>
        </div>
      </div>

      <div class="agroup">
        <div class="atitle">Connection</div>
        <div class="ahint">The network the pages are measured over. &ldquo;Lighthouse default&rdquo; is the
          simulated Slow 4G every score you have seen so far was measured on &mdash; change it and the
          numbers change with it, so compare like with like.</div>
        <div class="adv-grid">
          <label class="sel"><span>Throttling</span>
            <select id="throttling"><!--THROTTLE_OPTIONS--></select>
          </label>
          <div class="num" id="customdown" style="display:none"><span>Download Kbps</span><input id="down_kbps" type="number" min="1" max="1000000" value="5000"></div>
          <div class="num" id="customup" style="display:none"><span>Upload Kbps</span><input id="up_kbps" type="number" min="1" max="1000000" value="1000"></div>
          <div class="num" id="customlat" style="display:none"><span>Latency ms</span><input id="latency_ms" type="number" min="0" max="10000" value="100"></div>
        </div>
      </div>

      <div class="agroup">
        <div class="atitle">Device &amp; viewport</div>
        <div class="ahint">A named handset sets the form factor, the screen and the User-Agent together.
          Leave the width, height and pixel ratio empty to use the device&rsquo;s own.</div>
        <div class="adv-grid">
          <label class="sel"><span>Device profile</span>
            <select id="device_profile"><!--DEVICE_OPTIONS--></select>
          </label>
          <div class="num"><span>Width</span><input id="viewport_width" type="number" min="200" max="3840" placeholder="auto"></div>
          <div class="num"><span>Height</span><input id="viewport_height" type="number" min="200" max="3840" placeholder="auto"></div>
          <div class="num"><span>Pixel ratio</span><input id="dpr" type="number" min="0.5" max="5" step="0.25" placeholder="auto"></div>
        </div>
        <div class="adv-grid">
          <div class="num grow"><span>User-Agent override</span><input id="user_agent" type="text" class="tin" placeholder="leave empty for the device's own" autocomplete="off" spellcheck="false"></div>
        </div>
      </div>

      <div class="agroup secret">
        <div class="atitle"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="10" width="16" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>Sign in to the site</div>
        <div class="ahint">For staging servers, member areas and anything behind a password. Fill in only
          what the site needs &mdash; all three methods can be combined.</div>
        <div class="privacy">
          <svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round"><path d="M12 3l7 4v5c0 4.4-3 7.6-7 9-4-1.4-7-4.6-7-9V7z"/><path d="M9 12l2 2 4-4"/></svg>
          <span><strong>These fields are never stored.</strong> They are sent once, over this
          request&rsquo;s body, used in memory for this scan and wiped when it ends. Nothing here is
          written to the run folder, the report, the scan log, the saved settings below, or any
          history &mdash; so a re-run asks for them again.</span>
        </div>
        <div class="adv-grid">
          <div class="num"><span>HTTP Basic user</span><input id="auth_user" type="text" class="tin" data-secret="1" autocomplete="off" spellcheck="false"></div>
          <div class="num"><span>HTTP Basic password</span><input id="auth_pass" type="password" class="tin" data-secret="1" autocomplete="new-password"></div>
        </div>
        <div class="adv-grid">
          <div class="num grow"><span>Cookies</span><textarea id="cookies" data-secret="1" autocomplete="off" spellcheck="false" placeholder="session=abc123; consent=accepted"></textarea></div>
        </div>
        <div class="adv-grid">
          <div class="num grow"><span>Login form URL</span><input id="login_url" type="text" data-secret="1" autocomplete="off" spellcheck="false" placeholder="https://example.com/login"></div>
        </div>
        <div class="adv-grid">
          <div class="num"><span>Username selector</span><input id="login_user_selector" type="text" class="tin" data-secret="1" autocomplete="off" spellcheck="false" placeholder="#username"></div>
          <div class="num"><span>Password selector</span><input id="login_pass_selector" type="text" class="tin" data-secret="1" autocomplete="off" spellcheck="false" placeholder="#password"></div>
          <div class="num"><span>Submit selector</span><input id="login_submit_selector" type="text" class="tin" data-secret="1" autocomplete="off" spellcheck="false" placeholder="button[type=submit]"></div>
        </div>
        <div class="adv-grid">
          <div class="num"><span>Login username</span><input id="login_user" type="text" class="tin" data-secret="1" autocomplete="off" spellcheck="false"></div>
          <div class="num"><span>Login password</span><input id="login_pass" type="password" class="tin" data-secret="1" autocomplete="new-password"></div>
          <button class="btn" id="clearauth" type="button">Clear sign-in fields</button>
        </div>
      </div>

      <div class="agroup">
        <label class="sw"><span class="switch"><input type="checkbox" id="remember" checked><span class="track"></span></span>Remember these settings in this browser <span class="hint">(everything except the sign-in fields)</span></label>
      </div>
    </details>

    <div class="cta">
      <button class="go" id="go" type="button"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>Generate report</button>
      <span class="hint">Deep audits run each page through Lighthouse &mdash; large sites take a few minutes.</span>
    </div>

    <div class="notice" id="notice">
      <div class="ntitle" id="ntitle"></div>
      <div class="nmsg" id="nmsg"></div>
      <div class="verify" id="verify" style="display:none">
        <label class="lbl" style="margin-top:6px">Option 1 &mdash; meta tag in your homepage &lt;head&gt;</label>
        <code id="vmeta"></code>
        <label class="lbl" style="margin-top:6px">Option 2 &mdash; DNS TXT record on your domain</label>
        <code id="vdns"></code>
        <div class="vbtns">
          <button class="btn" id="vrecheck" type="button">I&rsquo;ve added it &mdash; re-check</button>
          <button class="btn" id="vsingle" type="button">Scan just this page instead</button>
        </div>
      </div>
    </div>

    <div class="logwrap" id="logwrap">
      <div class="logbar"><span class="spin" id="spin"></span><span id="status">Scanning&hellip;</span>
        <span class="qbadge" id="qbadge" style="display:none"></span></div>
      <div class="statebox" id="statebox" style="display:none">
        <div class="stitle" id="stitle"></div>
        <div class="smsg" id="smsg"></div>
        <div class="scov" id="scov" style="display:none"></div>
      </div>
      <details class="logdetails" id="logdetails">
        <summary>Technical log &mdash; for advanced users</summary>
        <div id="log"></div>
      </details>
    </div>
  </div>

  <div class="results" id="results">
    <div class="rbar">
      <a class="btn primary" id="openlink" href="#" target="_blank"><svg viewBox="0 0 24 24" fill="none" stroke-width="2"><path d="M14 3h7v7M21 3l-9 9M10 5H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5"/></svg>Open full report</a>
      <a class="btn" id="pdflink" href="#" target="_blank"><svg viewBox="0 0 24 24" fill="none" stroke-width="2"><path d="M12 3v12M7 10l5 5 5-5M5 21h14"/></svg>PDF</a>
      <a class="btn sec" id="seclink" href="#" target="_blank" style="display:none"><svg viewBox="0 0 24 24" fill="none" stroke-width="2"><path d="M12 3l7 4v5c0 4.4-3 7.6-7 9-4-1.4-7-4.6-7-9V7z"/></svg>Security report</a>
      <a class="btn wcag" id="a11ylink" href="#" target="_blank" style="display:none"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="4.5" r="1.6"/><path d="M4.5 8.2h15M12 8.2v5m0 0l-3.4 6.6M12 13.2l3.4 6.6"/></svg>Accessibility report</a>
    </div>
    <div class="frame">
      <div class="cap"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/></svg><span id="cap">Report preview</span></div>
      <iframe id="frame" src="about:blank" title="report"></iframe>
    </div>
  </div>

  <div class="foot">Built for internal QA &middot; all processing happens on this machine.</div>
</div>

<script>
  // ------------------------------------------------------------------
  // What may be remembered, and what may never be.
  //
  // SECRET_FIELDS is the same list the server keeps in scanauth.SECRET_FIELDS.
  // Nothing on it is written to localStorage, put in a URL, or kept in the
  // re-run state - a re-run reads the live form, so if the fields have been
  // cleared it simply asks again. Everything else is a scan setting and is
  // remembered between visits.
  // ------------------------------------------------------------------
  var SECRET_FIELDS = ["auth_user","auth_pass","cookies","login_url",
                       "login_user_selector","login_pass_selector",
                       "login_submit_selector","login_user","login_pass"];
  var SETTING_FIELDS = ["samples","maxpages","concurrency","throttling","down_kbps",
                        "up_kbps","latency_ms","device_profile","viewport_width",
                        "viewport_height","dpr","user_agent"];
  var PREFS_KEY = "scan.settings.v1";

  function val(id){ var el = document.getElementById(id); return el ? el.value.trim() : ""; }

  // device segmented control
  let device = "desktop";
  document.querySelectorAll("#device button").forEach(function(b){
    b.onclick = function(){ device = b.dataset.v;
      document.querySelectorAll("#device button").forEach(function(x){ x.classList.toggle("on", x===b); }); };
  });
  // category chips toggle
  var stdwrap = document.getElementById("stdwrap");
  function syncStdWrap(){
    var on = document.querySelector('#cats .chip[data-cat="a11y-standards"]').classList.contains("on");
    stdwrap.style.display = on ? "block" : "none";
  }
  document.querySelectorAll("#cats .chip").forEach(function(ch){
    ch.onclick = function(){ ch.classList.toggle("on"); syncStdWrap(); };
  });
  // standards chips toggle
  document.querySelectorAll("#stds .chip").forEach(function(ch){
    ch.onclick = function(){ ch.classList.toggle("on"); };
  });
  syncStdWrap();

  // custom connection inputs appear only for the custom preset
  var throttleSel = document.getElementById("throttling");
  function syncThrottle(){
    var custom = throttleSel.value === "custom" ? "flex" : "none";
    ["customdown","customup","customlat"].forEach(function(id){
      document.getElementById(id).style.display = custom;
    });
  }
  throttleSel.onchange = syncThrottle;

  // ---- saved settings ----------------------------------------------
  // Only SETTING_FIELDS are ever written. The sign-in fields are not on that
  // list, are not read here, and are not written here - by construction, not
  // by filtering something that already contains them.
  function saveSettings(){
    var el = document.getElementById("remember");
    try {
      if(!el || !el.checked){ localStorage.removeItem(PREFS_KEY); return; }
      var out = { device: device, deep: document.getElementById("deep").checked };
      SETTING_FIELDS.forEach(function(id){ out[id] = val(id); });
      localStorage.setItem(PREFS_KEY, JSON.stringify(out));
    } catch(err){ /* private mode, quota, disabled storage: settings just don't stick */ }
  }

  function loadSettings(){
    var saved = null;
    try { saved = JSON.parse(localStorage.getItem(PREFS_KEY) || "null"); } catch(err){ }
    if(!saved){ syncThrottle(); return; }
    SETTING_FIELDS.forEach(function(id){
      var el = document.getElementById(id);
      if(el && typeof saved[id] === "string" && saved[id] !== ""){ el.value = saved[id]; }
    });
    if(typeof saved.deep === "boolean"){ document.getElementById("deep").checked = saved.deep; }
    if(saved.device === "mobile" || saved.device === "desktop"){
      device = saved.device;
      document.querySelectorAll("#device button").forEach(function(x){
        x.classList.toggle("on", x.dataset.v === device); });
    }
    syncThrottle();
  }

  document.getElementById("clearauth").onclick = function(){
    SECRET_FIELDS.forEach(function(id){
      var el = document.getElementById(id);
      if(el){ el.value = ""; }
    });
  };

  loadSettings();

  var logwrap = document.getElementById("logwrap");
  var logEl = document.getElementById("log");
  var go = document.getElementById("go");
  var statusEl = document.getElementById("status");
  var spin = document.getElementById("spin");
  var qbadge = document.getElementById("qbadge");
  var notice = document.getElementById("notice");
  var verifyBox = document.getElementById("verify");
  var scanning = false;   // one scan at a time - the button stays disabled meanwhile
  var limits = null;      // what this server allows, fetched once

  fetch("/api/limits").then(function(r){ return r.json(); }).then(function(j){
    limits = j;
    var mp = document.getElementById("maxpages");
    mp.max = j.max_pages; if(+mp.value > j.max_pages){ mp.value = j.max_pages; }
    var sa = document.getElementById("samples");
    sa.max = j.samples;   if(+sa.value > j.samples){ sa.value = j.samples; }
    var pa = document.getElementById("concurrency");
    pa.max = j.parallel;  if(+pa.value > j.parallel){ pa.value = j.parallel; }
    if(j.scan_auth_enabled === false){
      // This deployment does not accept third-party credentials at all, so
      // don't offer fields whose contents it would only refuse.
      var box = document.querySelector(".agroup.secret");
      if(box){ box.style.display = "none"; }
    }
  }).catch(function(){ /* the caps still apply server-side */ });

  var statebox = document.getElementById("statebox");

  // The run's own state, in plain language. The raw log stays one click away
  // in the collapsed <details> below it - it is never the thing a person is
  // handed instead of an explanation.
  function showState(kind, title, message, coverage){
    statebox.className = "statebox " + (kind || "");
    document.getElementById("stitle").textContent = title || "";
    document.getElementById("smsg").textContent = message || "";
    var cov = document.getElementById("scov");
    if(coverage && coverage.attempted){
      cov.textContent = "Coverage: " + coverage.ok + " of " + coverage.attempted +
                        " page(s) (" + (coverage.percent || 0) + "%). Pages that " +
                        "could not be measured are unmeasured, not clean.";
      cov.style.display = "block";
    } else { cov.style.display = "none"; }
    statebox.style.display = "block";
  }
  function hideState(){ statebox.style.display = "none"; }

  function showNotice(title, message, isError){
    document.getElementById("ntitle").textContent = title;
    document.getElementById("nmsg").textContent = message;
    notice.classList.toggle("err", !!isError);
    notice.style.display = "block";
  }
  function hideNotice(){ notice.style.display = "none"; verifyBox.style.display = "none"; }

  function showVerification(v){
    document.getElementById("vmeta").textContent = v.meta_tag;
    document.getElementById("vdns").textContent = v.dns_record;
    verifyBox.style.display = "grid";
  }

  function setQueueBadge(text){
    if(text){ qbadge.textContent = text; qbadge.style.display = "inline-block"; }
    else { qbadge.style.display = "none"; }
  }

  function setBusy(on){
    scanning = on;
    go.disabled = on;
    go.textContent = on ? "Scanning\u2026" : "Generate report";
  }

  function showLink(id, href){
    var el = document.getElementById(id);
    if(href){ el.href = href; el.style.display = "inline-flex"; }
    else { el.removeAttribute("href"); el.style.display = "none"; }
  }

  function selectedCats(){
    var out = [];
    document.querySelectorAll("#cats .chip.on").forEach(function(c){ out.push(c.dataset.cat); });
    return out;
  }

  function selectedStds(){
    var out = [];
    document.querySelectorAll("#stds .chip.on").forEach(function(c){ out.push(c.dataset.std); });
    return out;
  }

  var jobId = null, lastRequest = null;

  // "I've added the meta tag / TXT record" - ask the server to look again.
  document.getElementById("vrecheck").onclick = function(){
    if(!lastRequest){ return; }
    var btn = this; btn.textContent = "Checking\u2026";
    fetch("/api/verify?check=1&url=" + encodeURIComponent(lastRequest.url))
      .then(function(r){ return r.json(); })
      .then(function(j){
        btn.textContent = "I\u2019ve added it \u2014 re-check";
        if(j.verified){ hideNotice(); go.click(); }
        else { showNotice("Still not visible",
                          "We could not find " + j.token + " on " + j.domain +
                          " yet. DNS changes can take a few minutes to propagate.", true);
               showVerification(j); }
      })
      .catch(function(){ btn.textContent = "I\u2019ve added it \u2014 re-check"; });
  };

  // The always-allowed alternative: one page, no crawl, no verification.
  document.getElementById("vsingle").onclick = function(){
    document.getElementById("maxpages").value = 1;
    hideNotice();
    go.click();
  };

  go.onclick = function(){
    if(scanning){ return; }              // a scan is already streaming
    var url = document.getElementById("url").value.trim();
    if(!url){ alert("Enter a URL first."); return; }
    var cats = selectedCats();
    if(cats.length === 0){ alert("Select at least one category."); return; }
    var security = cats.indexOf("security") !== -1 ? 1 : 0;
    var a11y = cats.indexOf("a11y-standards") !== -1 ? 1 : 0;
    var stds = selectedStds();
    if(a11y && stds.length === 0){ alert("Pick at least one accessibility standard."); return; }
    var lh = cats.filter(function(c){ return c !== "security" && c !== "a11y-standards"; });
    var anyLH = lh.length > 0;

    var deep = document.getElementById("deep").checked ? 1 : 0;
    var samples = document.getElementById("samples").value || 1;
    var maxpages = document.getElementById("maxpages").value || 30;
    var concurrency = document.getElementById("concurrency").value || 3;

    hideNotice();
    hideState();
    document.getElementById("logdetails").open = false;
    logwrap.style.display = "block";
    logEl.textContent = "";
    spin.style.display = "inline-block";
    statusEl.textContent = "Submitting\u2026";
    setQueueBadge(null);
    document.getElementById("results").style.display = "none";
    setBusy(true);
    // Only the URL is kept: the re-check and "scan this page instead" buttons
    // replay the form as it stands, they do not carry a saved copy of it - and
    // a saved copy is exactly what must not exist for the sign-in fields.
    lastRequest = { url: url };
    saveSettings();

    // The scan settings go in the POST body, not a query string: the sign-in
    // fields below would otherwise be written to the server's access log and
    // to this browser's own history.
    var body = { url:url, device:device, deep:deep, samples:samples,
      max_pages:maxpages, concurrency:concurrency, security:security,
      categories:lh.join(","), a11y:a11y, standards:stds.join(",") };
    SETTING_FIELDS.forEach(function(id){
      if(id === "samples" || id === "maxpages" || id === "concurrency"){ return; }
      var v = val(id); if(v !== ""){ body[id] = v; }
    });
    SECRET_FIELDS.forEach(function(id){
      var v = val(id); if(v !== ""){ body[id] = v; }
    });

    var es = null;

    fetch("/scan", {method:"POST", headers:{"Content-Type":"application/json"},
                    body: JSON.stringify(body)})
      .then(function(r){ return r.json().then(function(j){ return {ok:r.ok, body:j}; }); })
      .then(function(res){
        // Whatever happens next, this page is done holding the passwords.
        body = null;
        if(!res.ok){ onRejected(res.body || {}); return; }
        jobId = res.body.job;
        if(res.body.capped && res.body.capped.length){
          showNotice("Adjusted to this server's limits",
                     "Your request asked for more than this deployment allows, so it was " +
                     "trimmed to max pages " + res.body.params.max_pages + ", samples " +
                     res.body.params.samples + ", parallel " + res.body.params.concurrency + ".",
                     false);
        }
        statusEl.textContent = "Queued\u2026";
        es = new EventSource(res.body.stream);
        wire(es);
      })
      .catch(function(){
        body = null;
        reset(); spin.style.display = "none"; setQueueBadge(null);
        statusEl.textContent = "Could not reach the server.";
        showNotice("Could not reach the server",
                   "The scan was not submitted. Check the connection and try again.", true);
        logwrap.style.display = "none";
      });

    // refused before anything was queued: rate limit, full queue, or the
    // domain-ownership gate for a multi-page audit
    function onRejected(d){
      if(es){ es.close(); }
      reset(); spin.style.display = "none"; setQueueBadge(null);
      var titles = { queue_full: "The queue is full",
                     hourly: "Scan limit reached",
                     concurrent: "A scan of yours is already running",
                     unverified: "Verify this domain first",
                     invalid: "That request can\u2019t be scanned" };
      statusEl.textContent = titles[d.reason] || "Not accepted";
      showNotice(titles[d.reason] || "Not accepted",
                 d.message || "The scan was not accepted.", true);
      if(d.verification){ showVerification(d.verification); }
      logwrap.style.display = "none";
    }

    function wire(es){

    es.onmessage = function(e){ logEl.textContent += e.data + "\\n"; logEl.scrollTop = logEl.scrollHeight; };

    es.addEventListener("queued", function(e){
      var d = {};
      try { d = JSON.parse(e.data); } catch(err){ }
      statusEl.textContent = "Waiting for a free worker\u2026";
      setQueueBadge(d.position ? ("Queued \u00b7 position " + d.position) : "Queued");
    });

    es.addEventListener("running", function(){
      statusEl.textContent = "Scanning\u2026";
      setQueueBadge("Running");
    });

    // the stream itself can still refuse: a job that expired before the
    // browser got back to it
    es.addEventListener("rejected", function(e){
      var d = {};
      try { d = JSON.parse(e.data); } catch(err){ }
      onRejected(d);
    });

    es.addEventListener("done", function(e){
      es.close(); reset();
      // payload: {"base":"/runs/<name>/","files":[...files the run wrote...]}
      var base = e.data, files = null, run = {};
      try {
        var payload = JSON.parse(e.data);
        if(payload && payload.base){ base = payload.base; files = payload.files || []; run = payload; }
      } catch(err){ /* older payload: the bare run folder */ }

      // How much of the site this report actually covers, said once, up front.
      var status = run.status || "", notice = run.notice || null;
      if(status === "failed"){
        showState("err", (notice && notice.title) || "The scan couldn\u2019t be completed",
                  (notice && notice.message) || "No page could be measured.", run.coverage);
      } else if(status === "partial"){
        showState("warn", (notice && notice.title) || "Partial scan",
                  (notice && notice.message) ||
                  "Some pages could not be measured; the report covers the ones that were.",
                  run.coverage);
      } else if(status === "complete" && run.coverage && run.coverage.attempted){
        showState("ok", "Scan complete",
                  "Every page this run set out to measure was measured.", run.coverage);
      }

      function has(name){
        if(files !== null){ return files.indexOf(name) !== -1; }
        if(name === "report.html" || name === "report.pdf"){ return !!anyLH; }
        if(name === "accessibility.html"){ return !!a11y; }
        return !!security;
      }

      // preview the performance report when a Lighthouse category ran,
      // otherwise the accessibility one, otherwise security.
      var primary = null, cap = "Report preview";
      if(anyLH && has("report.html")){ primary = "report.html"; cap = "Performance report preview"; }
      else if(has("accessibility.html")){ primary = "accessibility.html"; cap = "Accessibility report preview"; }
      else if(has("security.html")){ primary = "security.html"; cap = "Security report preview"; }
      else if(has("report.html")){ primary = "report.html"; cap = "Performance report preview"; }

      spin.style.display = "none";
      setQueueBadge(null);
      if(!primary){
        ["openlink","pdflink","seclink","a11ylink"].forEach(function(id){ showLink(id, null); });
        statusEl.textContent = status === "failed" ? "Scan failed." : "No report was produced.";
        if(!notice){
          showState("err", "No report was produced",
                    "The scan finished without measuring any page. The technical log " +
                    "below has the details.", run.coverage);
        }
        document.getElementById("results").style.display = "none";
        return;
      }
      document.getElementById("frame").src = base + primary;
      document.getElementById("cap").textContent = cap;
      showLink("openlink", base + primary);
      showLink("pdflink", has("report.pdf") ? base + "report.pdf" : null);
      showLink("seclink", has("security.html") ? base + "security.html" : null);
      showLink("a11ylink", has("accessibility.html") ? base + "accessibility.html" : null);
      document.getElementById("results").style.display = "block";
      statusEl.textContent = (status === "partial") ? "Done - partial coverage." : "Done.";
    });
    es.addEventListener("fail", function(e){
      es.close(); reset(); spin.style.display = "none"; setQueueBadge(null);
      var why = "", note = null;
      try { var d = JSON.parse(e.data) || {}; why = d.message || ""; note = d.notice || null; }
      catch(err){ }
      statusEl.textContent = "Scan failed.";
      // The banner explains it; the raw reason stays in the log for whoever
      // wants it.
      showState("err", (note && note.title) || "The scan couldn\u2019t be completed",
                (note && note.message) ||
                "Something went wrong before a report could be built.", null);
      logEl.textContent += "\\n--- scan failed" + (why ? ": " + why : "") + " ---\\n";
    });
    es.onerror = function(){ es.close(); reset(); spin.style.display="none"; setQueueBadge(null); };

    }   // wire()

    function reset(){ setBusy(false); }
  };
</script>
</body></html>"""


def _options(choices, selected=""):
    """<option> markup for a scanconfig choice list.

    The two selects are built from `scanconfig` rather than typed out here, so
    a preset the server understands and one the form offers cannot drift apart.
    """
    import html as _html

    out = []
    for value, label in choices:
        mark = " selected" if value == selected else ""
        out.append(f'<option value="{_html.escape(value)}"{mark}>'
                   f'{_html.escape(label)}</option>')
    return "".join(out)


PAGE = PAGE.replace(
    "<!--THROTTLE_OPTIONS-->",
    _options(scanconfig.throttling_choices(), scanconfig.DEFAULT_THROTTLING))
PAGE = PAGE.replace(
    "<!--DEVICE_OPTIONS-->",
    '<option value="">Match the Device setting above</option>'
    + _options(scanconfig.device_choices()))


if __name__ == "__main__":
    # Local, single-process convenience only. It binds to loopback and runs the
    # in-memory queue, so nothing here is shared between processes. Anything
    # public runs under gunicorn with Redis + separate workers - see README.md.
    backend = queue_backend()
    if backend.name == "inline":
        print(f"Queue: in-memory, {backend.concurrency} worker thread(s), "
              f"depth {backend.max_depth}. Set REDIS_URL for the real queue.")
    else:
        print(f"Queue: {backend.name} ({settings.QUEUE_NAME}) - workers must be running.")
    print(f"Caps: max_pages<={settings.CAP_MAX_PAGES} samples<={settings.CAP_SAMPLES} "
          f"parallel<={settings.CAP_PARALLEL} timeout={settings.SCAN_TIMEOUT}s")
    print(f"Dashboard on http://127.0.0.1:5000  (dry-run: {DRYRUN})")
    app.run(host="127.0.0.1", port=5000, threaded=True, debug=False)