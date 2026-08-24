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
import scanpresets
import ui_page
import ui_preview
import verification
import config
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

# Saved scan settings, per browser session. Never credentials - see
# scanpresets, which is built so one cannot get in.
PRESETS_DIR = APP_DIR / "presets"

_services_lock = threading.Lock()
_limiter = None
_presets = None


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


def preset_store():
    """One preset store per process, matching whichever queue is in use.

    With Redis, saved settings live there and every web process sees the same
    ones; otherwise they are JSON files next to the app, so the local tool
    keeps them across a restart.
    """
    global _presets
    with _services_lock:
        if _presets is None:
            backend = queue_backend()
            docs = (scanpresets.RedisPresetDocs(backend.r)
                    if isinstance(backend, jobqueue.RedisQueueBackend)
                    else scanpresets.FilePresetDocs(PRESETS_DIR))
            _presets = scanpresets.set_store(scanpresets.PresetStore(docs))
        return _presets


def reset_services():
    """Drop the cached services (used by tests and after a config reload)."""
    global _limiter, _presets
    with _services_lock:
        _limiter = None
        _presets = None


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

    A DNS override joins it here as Chrome's own `--host-resolver-rules`, so
    the audit connects to the address the scan was told to use while the
    request itself is unchanged - same host name, same certificate, same
    cookies. `scanconfig` quotes the rule, whose value contains spaces.
    """
    flags = BASE_CHROME_FLAGS
    width, height, _dpr = cfg.screen("")
    if width and height:
        flags += f" --window-size={width},{height}"
    for extra in cfg.chrome_flags():
        flags += f" {extra}"
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


CRAWL_CONFIG = "unlighthouse.config.js"


def _write_crawl_config(run_dir, cfg, log):
    """Hand the crawler the two settings its CLI has no flag for.

    `unlighthouse-ci` reads a config file from the directory it runs in - which
    is the run folder - and passes both of these straight down: the block list
    to the Lighthouse it drives, the resolver rule to the browser it launches.
    Written only when there is something to say, so an ordinary scan still runs
    in a folder with no config file in it at all.
    """
    config = cfg.crawl_config()
    if not config:
        return None
    path = Path(run_dir) / CRAWL_CONFIG
    path.write_text("module.exports = " + json.dumps(config, indent=2) + ";\n",
                    encoding="utf-8")
    if config.get("lighthouseOptions"):
        log(f"Crawl will block {len(cfg.block_patterns)} URL pattern(s).")
    if config.get("puppeteerOptions"):
        log(f"Crawl will resolve {cfg.dns_host} to {cfg.dns_ip}.")
    return path


def run_pipeline(url, device, samples, deep, max_pages, concurrency, security, categories, log,
                 a11y=False, standards=None, scan_config=None, credentials=None):
    """Generator-free worker: does the whole scan, returns the run folder name.
    `categories` = selected Lighthouse categories (subset of
    performance/accessibility/best-practices/seo). Empty means security-only.
    `a11y` turns on the optional axe-core accessibility-compliance scan and
    `standards` picks which legal frameworks it reports on.

    `scan_config` (a `scanconfig.ScanConfig`) and `credentials` (a
    `scanauth.Credentials`) are inputs to the scanners and nothing more: they
    add flags to the `unlighthouse-ci` and `lighthouse` command lines, tell the
    browsers which requests to block and which host to resolve where, and, for
    a scripted login, run one browser before the crawl. Neither touches the
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
    # A DNS override given as an address alone means "the site being scanned",
    # so the host it maps is resolved once, here, where the URL is known.
    cfg = cfg.for_url(url)
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
    _write_crawl_config(run_dir, cfg, log)
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


@app.route("/preview")
def preview_index():
    """A contact sheet of every UI state, for looking at the design without
    running a scan.

    The page it links to is the real one - `?preview=<state>` hands the same
    document its own preview harness, which fabricates a run's *display* state
    and nothing else. It never queues, submits or reaches a scanner. Offered
    on a local deployment (or a dry run) only; a public one has no business
    serving fabricated results next to real ones.
    """
    if not (DRYRUN or settings.DEPLOYMENT_MODE == config.MODE_LOCAL):
        abort(404)
    return Response(
        ui_preview.index_page(href=lambda slug: f"/?preview={slug}", report_href=""),
        mimetype="text/html")


@app.route("/healthz")
def healthz():
    backend = queue_backend()
    return jsonify({"ok": True, "queue": backend.name, "depth": backend.depth(),
                    "max_depth": backend.max_depth})


@app.route("/api/limits")
def api_limits():
    """What this deployment allows - the UI shows the caps up front instead of
    silently shrinking a scan the user asked for."""
    storage_ok, storage_reason = queue_backend().credential_storage_ok()
    auth_enabled = settings.ALLOW_SCAN_AUTH and storage_ok
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
        # A field the deployment will only refuse is not offered. `mode` is
        # the reason for every `false` below it, so the UI can say *why* a
        # control is missing instead of just leaving a hole in the form.
        "mode": settings.DEPLOYMENT_MODE,
        "scan_auth_enabled": auth_enabled,
        "scan_auth_disabled_reason": "" if auth_enabled else (
            storage_reason or "Authenticated scanning is turned off on this "
                              "deployment (ALLOW_SCAN_AUTH)."),
        "auth_requires_verification": settings.REQUIRE_VERIFIED_DOMAIN_FOR_AUTH,
        "dns_override_enabled": settings.ALLOW_DNS_OVERRIDE,
        "private_dns_targets_enabled": settings.ALLOW_PRIVATE_DNS_TARGETS,
        "max_presets": scanpresets.MAX_PRESETS,
        "max_block_patterns": scanconfig.MAX_BLOCK_PATTERNS,
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


# --------------------------- saved presets ---------------------------
# A preset is a named set of scan settings, kept per browser session. The three
# routes below are the whole feature: list, save (create / rename / update),
# delete. Choosing which one a fresh page starts on is a save away.
#
# What they will not do is store a credential. `scanpresets` reads only the
# fields on its own list, and refuses outright - 400, with a message - when a
# body carries one of `scanauth.SECRET_FIELDS`, so a client that posts the
# whole form by mistake is told, rather than quietly having its password
# written to disk.

def _owner_key(sid):
    """The browser a set of presets belongs to, as an opaque key.

    Hashed like the rate limiter's buckets: the store never holds the session
    id itself, and one browser cannot read another's saved settings.
    """
    return hashlib.sha256((sid or "").encode()).hexdigest()[:24] if sid else ""


def _preset_response(payload, sid=None, status=200):
    resp = jsonify(payload)
    if sid and not session_id():
        resp.set_cookie(SESSION_COOKIE, sid, max_age=30 * 86400,
                        samesite="Lax", httponly=True)
    return resp, status


@app.route("/api/presets")
def api_presets():
    """Every preset this browser can pick, and which one it starts on."""
    return jsonify(preset_store().payload(_owner_key(session_id())))


@app.route("/api/presets", methods=["POST"])
def api_presets_save():
    """Create, update or rename a preset - and optionally make it the default.

    One route because they are one operation: a preset is identified by its id,
    so a body with an id updates (or renames) that preset and a body without
    one creates it. `{"id": ..., "default": true}` on its own only changes
    which preset is the default, which is how a built-in becomes one.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = request.form.to_dict() if request.form else {}
    sid = session_id() or new_session_id()
    owner = _owner_key(sid)
    store = preset_store()
    try:
        if body.get("name"):
            preset = store.save(owner, scanpresets.Preset.from_request(body),
                                make_default=bool(body.get("default")))
        elif body.get("id") and body.get("default"):
            preset = store.set_default(owner, body.get("id"))
        else:
            raise scanpresets.PresetError("A preset needs a name.")
    except scanpresets.PresetError as exc:
        return jsonify({"reason": "invalid", "message": str(exc)}), 400
    payload = store.payload(owner)
    payload["preset"] = preset.as_dict()
    return _preset_response(payload, sid)


@app.route("/api/presets/<preset_id>", methods=["DELETE"])
def api_presets_delete(preset_id):
    owner = _owner_key(session_id())
    store = preset_store()
    try:
        removed = store.delete(owner, preset_id)
    except scanpresets.PresetError as exc:
        return jsonify({"reason": "invalid", "message": str(exc)}), 400
    if not removed:
        return jsonify({"reason": "invalid",
                        "message": "That preset no longer exists."}), 404
    payload = store.payload(owner)
    payload["deleted"] = preset_id
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

    domain = guardrails.registrable_domain(params["url"])
    allowed, detail = verification.gate(domain,
                                        guardrails.is_multipage_scan(params))
    if not allowed:
        raise Rejected("unverified", detail["message"], {"verification": detail})

    # Somebody's password aimed at somebody's site: in public mode that needs
    # proof of ownership whatever the size of the scan, which is why this gate
    # is separate from the one above rather than folded into it.
    allowed, detail = verification.credential_gate(domain, bool(credentials))
    if not allowed:
        raise Rejected("unverified", detail["message"], {"verification": detail})

    # And whether this queue may carry a credential at all - asked before a
    # slot is taken, so a refusal costs the client nothing. The backend raises
    # the same refusal from `enqueue` as a backstop, for any caller that does
    # not come through here.
    if credentials:
        storage_ok, storage_reason = backend.credential_storage_ok()
        if not storage_ok:
            raise Rejected("invalid", storage_reason)

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
    except jobqueue.CredentialsRefused as exc:
        limiter.release(keys, job_id)
        raise Rejected("invalid", str(exc))
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

    The original entry point, kept: it is the whole API for an unauthenticated
    scan from a script or a `curl`, and dropping it would break every caller
    that has one for the sake of a door that is not actually open. It is not a
    second admission path - it hands the query string to the same `_admit` the
    POST uses, so `sanitize_params`, the caps, the rate limiter, both
    verification gates and the SSRF check are one implementation, reached two
    ways. `tests/test_scan_endpoint_parity.py` pins that.

    The one thing it will not do is take a credential. A query string is
    written to access logs, sent in `Referer` headers and kept in the user's
    own history, so there is no door for one here - and a request that tries
    is *told*, rather than quietly scanned as an anonymous visitor and handed
    back a report of a login page.
    """
    if any(str(request.args.get(name) or "").strip()
           for name in scanauth.SECRET_FIELDS):
        return _rejection(
            "invalid",
            "Sign-in details cannot be sent in a URL - a query string reaches "
            "access logs, Referer headers and your own browser history. Submit "
            "them with POST /scan instead.")
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

# The page itself lives in `ui_page`, and its look in `ui_css` / `design_tokens`.
# Keeping the markup out of the Flask app is what lets `ui_preview.py` render
# the real page - not a mock-up - into a file for every state it can be in.
PAGE = ui_page.render()


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