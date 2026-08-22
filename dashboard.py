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
import verification
from config import settings

APP_DIR = Path(__file__).resolve().parent
RUNS_DIR = APP_DIR / "runs"
RUNS_DIR.mkdir(exist_ok=True)
DRYRUN = os.environ.get("DASHBOARD_DRYRUN") == "1"
NPX = "npx.cmd" if os.name == "nt" else "npx"
PER_PAGE_TIMEOUT = 150  # seconds per page for the deep audit

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


def run_stream(cmd, cwd, log, env=None):
    """Run a subprocess, streaming its output into the log queue."""
    log(f"$ {' '.join(cmd)}")
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


def run_pipeline(url, device, samples, deep, max_pages, concurrency, security, categories, log,
                 a11y=False, standards=None):
    """Generator-free worker: does the whole scan, returns the run folder name.
    `categories` = selected Lighthouse categories (subset of
    performance/accessibility/best-practices/seo). Empty means security-only.
    `a11y` turns on the optional axe-core accessibility-compliance scan and
    `standards` picks which legal frameworks it reports on."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_name = f"{slugify(url)}-{device}-{stamp}"
    run_dir = RUNS_DIR / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    categories = categories or []
    lh_on = len(categories) > 0

    device_args = ["--desktop"] if device == "desktop" else []  # mobile is the default

    # 1) crawl
    log(f"Crawling {url} ({device}, {samples} sample(s))...")
    if DRYRUN:
        log("[dry-run] fabricating crawl results")
        make_mock(run_dir, url, device, deep)
        time.sleep(0.3)
    else:
        rc = run_stream(
            [NPX, "unlighthouse-ci", "--site", url, *device_args,
             "--samples", str(samples), "--throttle",
             "--reporter", "jsonExpanded"],
            cwd=run_dir, log=log, env=scan_env(),
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

    # Safety net: if the crawl found nothing (blocked crawler, odd sitemap, etc.)
    # but deep audit is on, still audit at least the entered URL so the run
    # produces a usable single-page report instead of failing outright.
    if not routes and deep and not DRYRUN:
        log("Crawl found no pages - falling back to auditing the entered URL only.")
        entered_path = urlparse(url).path or "/"
        routes = [{"path": entered_path}]

    # 2) deep audit per page (full recommendations + standalone Lighthouse HTML)
    if deep and lh_on and routes and not DRYRUN:
        lhr = run_dir / "lhr"
        lhr.mkdir(exist_ok=True)
        base = origin_of(url)
        targets = routes[:max_pages]
        if len(routes) > max_pages:
            log(f"Deep audit limited to first {max_pages} of {len(routes)} pages "
                f"(raise 'Max pages' to cover all).")
        preset = ["--preset=desktop"] if device == "desktop" else []
        only_cats = "--only-categories=" + ",".join(categories)
        lh = LIGHTHOUSE

        def audit_one(idx, route):
            path = (route.get("path") or "/").replace("&amp;", "&")
            full = urljoin(base + "/", path.lstrip("/"))
            stem = lhr / f"page_{idx:03d}"          # lighthouse appends .report.json/.html
            t0 = time.time()
            try:
                subprocess.run(
                    lh + [full, "--quiet",
                          "--output=json", "--output=html", f"--output-path={stem}",
                          only_cats,
                          "--no-enable-error-reporting",
                          "--chrome-flags=--headless=new --no-sandbox --ignore-certificate-errors"] + preset,
                    cwd=str(run_dir), check=False, timeout=PER_PAGE_TIMEOUT,
                    env=scan_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired:
                return (full, False, PER_PAGE_TIMEOUT, "timeout")
            j = stem.with_name(stem.name + ".report.json")
            ok = j.exists() and j.stat().st_size > 0
            return (full, ok, int(time.time() - t0), "" if ok else "no report")

        log(f"Deep-auditing {len(targets)} page(s) with {concurrency} in parallel...")
        done = 0
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(audit_one, i, r): i for i, r in enumerate(targets, 1)}
            for n, fut in enumerate(as_completed(futures), 1):
                full, ok, secs, why = fut.result()
                if ok:
                    done += 1
                    log(f"[{n}/{len(targets)}] OK ({secs}s) {full}")
                else:
                    log(f"[{n}/{len(targets)}] skipped ({why}) {full}")
        log(f"Deep audit complete: {done}/{len(targets)} full reports captured.")
    elif deep and lh_on and DRYRUN:
        log("[dry-run] deep-audit reports fabricated")

    # 3) the optional scans run BEFORE the report is built, so their findings
    #    can go into it. Each still writes its own standalone page, exactly as
    #    it always did - the consolidated report is additional, not a
    #    replacement.
    def scan_urls():
        base = origin_of(url)
        return [urljoin(base + "/", ((r.get("path") or "/").replace("&amp;", "&")).lstrip("/"))
                for r in routes[:max_pages]] or [url]

    security_data = None
    if security and not DRYRUN:
        try:
            import security_scan as sec
            security_data = sec.collect_site(url, scan_urls(), log=log)
            (run_dir / "security.html").write_text(sec.html_from(security_data), encoding="utf-8")
            log("Security report ready.")
        except Exception as e:
            security_data = None
            log(f"(Security scan failed: {e})")
    elif security and DRYRUN:
        (run_dir / "security.html").write_text("<html><body>dry-run security</body></html>", encoding="utf-8")
        log("[dry-run] security report fabricated")

    a11y_data = None
    if a11y and not DRYRUN:
        try:
            import accessibility_scan as a11y_mod
            a11y_data = a11y_mod.collect_site(url, scan_urls(), standards=standards,
                                              concurrency=concurrency,
                                              work_dir=run_dir / "axe", log=log)
            (run_dir / "accessibility.html").write_text(
                a11y_mod.html_from(a11y_data), encoding="utf-8")
            log("Accessibility report ready.")
        except Exception as e:
            a11y_data = None
            log(f"(Accessibility scan failed: {e})")
    elif a11y and DRYRUN:
        (run_dir / "accessibility.html").write_text(
            "<html><body>dry-run accessibility</body></html>", encoding="utf-8")
        log("[dry-run] accessibility report fabricated")

    # 4) one consolidated report covering every category that ran
    # Which modes produce a report is unchanged: a Lighthouse category still has
    # to have run. What changed is its contents - the accessibility and security
    # findings from this same run now go into it instead of living only on their
    # own pages.
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

    return run_name


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


@app.route("/scan")
def scan():
    """Admit a scan and stream it.

    The request no longer runs the pipeline: it is capped, rate limited,
    gated, put on the queue, and then followed. A worker does the scanning.
    """
    params, error, capped = guardrails.sanitize_params(request.args)
    if error:
        return _rejection("invalid", error)

    backend = queue_backend()
    limiter = rate_limiter()
    keys = guardrails.client_keys(request.remote_addr, session_id())

    try:
        limiter.check(keys)
    except guardrails.RateLimitError as exc:
        return _rejection(exc.reason, exc.message)

    allowed, detail = verification.gate(guardrails.registrable_domain(params["url"]),
                                        guardrails.is_multipage_scan(params))
    if not allowed:
        return _rejection("unverified", detail["message"], {"verification": detail})

    job_id = jobqueue.new_job_id()
    limiter.reserve(keys, job_id)       # hold the slot before the job can start
    try:
        backend.enqueue(params, client=keys[0][1], job_id=job_id)
    except jobqueue.QueueFull:
        limiter.release(keys, job_id)   # a refusal costs the client nothing
        return _rejection(
            "queue_full",
            f"All {backend.max_depth} queue slots are taken right now. "
            "Please try again shortly - nothing was lost.")
    limiter.charge(keys, job_id)

    resp = Response(_stream(job_id, keys, capped, params),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "X-Scan-Job": job_id})
    if not session_id():
        resp.set_cookie(SESSION_COOKIE, new_session_id(), max_age=30 * 86400,
                        samesite="Lax", httponly=True)
    return resp


KEEPALIVE_EVERY = 15.0   # seconds of silence before a comment frame


def _stream(job_id, keys, capped, params):
    """Follow one job: queue position, then its log, then the outcome."""
    backend = queue_backend()

    def gen():
        cursor = 0
        position = object()          # a sentinel that no position equals
        running_announced = False
        last_output = time.time()
        yield _sse("accepted", {"job": job_id, "capped": capped,
                                "params": {k: params[k] for k in
                                           ("max_pages", "samples", "concurrency")}})
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
                    yield _sse("fail", {"message": state["error"] or "scan failed"})
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
      <div class="adv-grid">
        <label class="sw"><span class="switch"><input type="checkbox" id="deep" checked><span class="track"></span></span>Fix recommendations &amp; per-page reports</label>
        <div class="num"><span>Samples</span><input id="samples" type="number" min="1" max="5" value="1"></div>
        <div class="num"><span>Max pages</span><input id="maxpages" type="number" min="1" max="200" value="30"></div>
        <div class="num"><span>Parallel</span><input id="concurrency" type="number" min="1" max="6" value="3"></div>
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
      <div id="log"></div>
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
  }).catch(function(){ /* the caps still apply server-side */ });

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
    logwrap.style.display = "block";
    logEl.textContent = "";
    spin.style.display = "inline-block";
    statusEl.textContent = "Submitting\u2026";
    setQueueBadge(null);
    document.getElementById("results").style.display = "none";
    setBusy(true);
    lastRequest = { url: url };

    var qs = new URLSearchParams({ url:url, device:device, deep:deep, samples:samples,
      max_pages:maxpages, concurrency:concurrency, security:security, categories:lh.join(","),
      a11y:a11y, standards:stds.join(",") });
    var es = new EventSource("/scan?" + qs.toString());

    es.onmessage = function(e){ logEl.textContent += e.data + "\\n"; logEl.scrollTop = logEl.scrollHeight; };

    // the scan is on the queue - the browser now follows it, it is not running yet
    es.addEventListener("accepted", function(e){
      try {
        var d = JSON.parse(e.data);
        jobId = d.job;
        if(d.capped && d.capped.length){
          showNotice("Adjusted to this server's limits",
                     "Your request asked for more than this deployment allows, so it was " +
                     "trimmed to max pages " + d.params.max_pages + ", samples " +
                     d.params.samples + ", parallel " + d.params.concurrency + ".", false);
        }
      } catch(err){ /* keep streaming regardless */ }
      statusEl.textContent = "Queued\u2026";
    });

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

    // refused before anything was queued: rate limit, full queue, or the
    // domain-ownership gate for a multi-page audit
    es.addEventListener("rejected", function(e){
      es.close(); reset(); spin.style.display = "none"; setQueueBadge(null);
      var d = {};
      try { d = JSON.parse(e.data); } catch(err){ }
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
    });

    es.addEventListener("done", function(e){
      es.close(); reset();
      // payload: {"base":"/runs/<name>/","files":[...files the run wrote...]}
      var base = e.data, files = null;
      try {
        var payload = JSON.parse(e.data);
        if(payload && payload.base){ base = payload.base; files = payload.files || []; }
      } catch(err){ /* older payload: the bare run folder */ }

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
        statusEl.textContent = "Done - no report was produced (see the log).";
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
      statusEl.textContent = "Done.";
    });
    es.addEventListener("fail", function(e){
      es.close(); reset(); spin.style.display = "none"; setQueueBadge(null);
      var why = "";
      try { why = (JSON.parse(e.data) || {}).message || ""; } catch(err){ }
      statusEl.textContent = why ? ("Scan failed: " + why) : "Scan failed.";
      logEl.textContent += "\\n--- scan failed" + (why ? ": " + why : "") + " ---\\n";
    });
    es.onerror = function(){ es.close(); reset(); spin.style.display="none"; setQueueBadge(null); };

    function reset(){ setBusy(false); }
  };
</script>
</body></html>"""


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