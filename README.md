# Site Performance & Security Dashboard

Enter a URL, pick Desktop or Mobile, get one consolidated report: Lighthouse
scores and core metrics, itemized fix recommendations, plus optional passive
security checks and an axe-core accessibility-standards audit.

Under **Advanced options** the scan can also be shaped to match the real world:
a connection preset (or a custom one), a named handset, a viewport and a
User-Agent - and, for a site that is not public, HTTP Basic credentials, a
pasted cookie jar, or a scripted form login.

The scanning engine (`run_pipeline`, the scoring, and every report builder) is
unchanged by anything described below. What this README covers is the serving
layer: how scans are queued, how many run at once, and what a single anonymous
visitor is allowed to ask for.

---

## Local run (one process, no Redis)

```bash
pip install -r requirements.txt
python dashboard.py            # http://127.0.0.1:5000
```

This keeps an in-memory queue and a small pool of worker threads inside the
web process. It is for one person on one machine. Add `DASHBOARD_DRYRUN=1` to
click through the UI without a real scan.

## Public run (Redis queue + worker pool + gunicorn)

Three processes, started in this order:

```bash
# 1. Redis - the queue, the rate-limit counters, and the live log fan-out
redis-server

# 2. The workers: this is the ONLY place a scan actually runs.
#    WORKER_CONCURRENCY is how many scans can be in flight at once.
REDIS_URL=redis://127.0.0.1:6379/0 \
WORKER_CONCURRENCY=2 \
SCAN_TIMEOUT=900 \
python worker.py

# 3. The web tier - accepts, queues and streams; never scans.
REDIS_URL=redis://127.0.0.1:6379/0 \
BIND=0.0.0.0:8000 \
gunicorn -c gunicorn.conf.py wsgi:app
```

Both the web tier and the workers need the same `REDIS_URL` and the same
`VERIFY_TOKEN_SECRET`; give every process the same environment.

`gunicorn.conf.py` uses `gthread` workers with no request timeout, because a
scan stream is a long-lived SSE connection that mostly waits. The scan's own
limit is `SCAN_TIMEOUT`, enforced by RQ in the worker.

Scaling up means raising `WORKER_CONCURRENCY` or running `worker.py` on more
machines - never raising `WEB_WORKERS`, which only adds connection capacity.

### Health

* `GET /healthz`    - queue backend and current depth
* `GET /api/limits` - the caps and budgets this deployment enforces
* `GET /api/job/<id>` - state of one job, for a client that lost its stream

---

## Configuration

Everything is environment variables; the defaults are the small, safe ones.

### Queue and workers

| Variable | Default | Meaning |
|---|---|---|
| `REDIS_URL` | *(empty)* | Empty = in-process queue. Set it for anything public or multi-process. |
| `QUEUE_NAME` | `scans` | RQ queue name. |
| `WORKER_CONCURRENCY` | `2` | Scans running at once, across the whole deployment. |
| `MAX_QUEUE_DEPTH` | `20` | Jobs allowed to wait. Past this, submissions are refused politely. |
| `SCAN_TIMEOUT` | `900` | Seconds one scan may take before the worker kills it. |
| `JOB_TTL` | `86400` | How long job state and logs are kept in Redis. |

### Resilience: what one page, and one run, may cost

A scan is a lot of independent page fetches, and any of them can fail. These
bound the damage one bad page - or one bad site - can do.

| Variable | Default | Meaning |
|---|---|---|
| `PAGE_TIMEOUT` | `150` | Seconds one page's deep audit may take. |
| `PAGE_RETRIES` | `1` | Extra attempts a **transient** page failure gets. A clear 403/block is never retried. |
| `RUN_BUDGET` | `85% of SCAN_TIMEOUT` | The pipeline's own ceiling. On hitting it the run finalizes as `partial` with whatever completed. |
| `PREFLIGHT_TIMEOUT` | `10` | Seconds for the one request that checks the site answers at all. |

`RUN_BUDGET` sits below `SCAN_TIMEOUT` on purpose: `SCAN_TIMEOUT` is the
queue's hard kill, which leaves no report at all, while `RUN_BUDGET` is the
pipeline stopping itself in time to deliver one.

### Per-client guardrails

| Variable | Default | Meaning |
|---|---|---|
| `RATE_LIMIT_ENABLED` | `1` | Master switch. |
| `MAX_SCANS_PER_HOUR` | `5` | Per IP **and** per browser session. |
| `MAX_CONCURRENT_SCANS` | `1` | Scans one client may have in flight. |
| `RATE_LIMIT_WINDOW` | `3600` | Length of the "per hour" window, in seconds. |
| `TRUST_PROXY` | `0` | Set to `1` behind nginx/Caddy so limits use `X-Forwarded-For`. |

### Hard caps on a scan's size

Applied server-side to every request. A client can send whatever it likes; the
pipeline never receives more than these.

| Variable | Default | Caps |
|---|---|---|
| `CAP_MAX_PAGES` | `20` | Pages crawled and deep-audited. |
| `CAP_SAMPLES` | `2` | Lighthouse samples per page. |
| `CAP_PARALLEL` | `3` | Pages audited in parallel inside one scan. |

When a request is trimmed, the UI says so instead of silently shrinking the
scan.

---

## Scan conditions

Everything here is an *input* to the scanners: it becomes flags on the
`lighthouse` and `unlighthouse-ci` command lines. No score, metric or report is
computed differently - Lighthouse simply measures a different situation, and
the report says what that situation was. Leave it all alone and a scan runs the
exact commands it always did.

**Connection.** `Lighthouse default` (its own simulated Slow 4G, the setting
every score you have seen so far was measured on), `Unthrottled`, `Broadband
Fast`, `Broadband`, `LTE`, `4G`, `3G`, or a custom down/up/latency. Presets
throttle the network with Chrome's own shaping (`--throttling-method=devtools`)
and leave the CPU alone: they emulate a connection, not a slower phone.

**Device.** Desktop and Mobile as before, plus named handsets - iPhone SE,
iPhone 12/13/14, iPhone 14 Pro/15/16, iPhone 15 Pro Max, Pixel 5, Pixel 7,
Pixel 8 Pro. A handset sets the form factor, the screen and the User-Agent
together, and the crawl follows it, so picking a Pixel really does audit the
site as a phone (the run folder is named `-mobile-` accordingly).

**Viewport and User-Agent.** An explicit width, height and device pixel ratio
override whatever the profile would have used, and Chrome's own window is sized
to match. A User-Agent override replaces the device's. Every value is clamped
server-side, and a User-Agent cannot carry a newline into a header.

Comparing two runs is only meaningful when these match - a 3G Pixel and an
unthrottled desktop are not two measurements of the same thing.

## Authenticated scanning

For staging servers, member areas and consent-walled sites. Three methods,
combinable:

* **HTTP Basic** - a username and password, sent as an `Authorization` header
  to Lighthouse and as `--auth` to the crawler.
* **Cookies** - pasted as `name=value; other=value`, injected before the first
  page loads.
* **Scripted form login** - a login URL and CSS selectors for the username,
  password and submit controls. Before the crawl, `login_playwright_runner.js`
  opens that form in a real browser, signs in, and the session cookies it
  produces travel with every page of the scan.

### What happens to the credentials

They are used in memory, for one scan, and are not stored anywhere:

* They are **never accepted from a query string.** The form submits them in a
  `POST /scan` body and the browser then follows the job with an EventSource on
  `/scan/stream/<job>?t=<token>`, so nothing secret reaches an access log, a
  `Referer` header, or the browser's own history. `GET /scan` - the original
  entry point - has no door for them at all.
* They are **not part of a scan's parameters.** `guardrails.sanitize_params`
  never returns them, so they cannot reach the job record, the run folder,
  `run-status.json`, a report, or the payload the browser is sent back.
* They are **never logged.** Every line the pipeline emits passes through a
  redactor first, so a command, a stack trace or a tool's own output cannot
  echo one back. The crawl command still appears in the log - with `[redacted]`
  where the values were.
* They are **not saved as a preset.** The "remember these settings" box writes
  only the scan-condition fields to `localStorage`; the sign-in fields are not
  on that list, so a re-run asks for them again.
* They are **wiped when the scan ends** - in a `finally`, whether it succeeded,
  failed or raised. Secrets are held in `bytearray`s so the wipe is real rather
  than a rebinding.

Two things are worth knowing rather than discovering:

* The `lighthouse` and `unlighthouse-ci` processes are given the credentials as
  **command-line arguments**, because neither tool accepts a header any other
  way and the alternative is a JSON file on disk. Those processes are ours and
  short-lived, but anyone who can read `/proc` on the scanning host as the same
  user can see them. The scripted login has no such exposure: it takes its
  configuration on stdin.
* With the **Redis queue**, credentials sit in their own short-TTL key for the
  length of the queue wait, apart from the job record and from the RQ job's
  arguments; the worker takes that key with `GETDEL` - one read, then it is
  gone. Run that Redis with persistence off (`--save "" --appendonly no`) if
  you accept credentials.

| Variable | Default | Meaning |
|---|---|---|
| `ALLOW_SCAN_AUTH` | `1` | `0` refuses credentials outright and hides the sign-in fields. |

### Domain-ownership gate

Off by default (internal use). Turn it on for a public deployment:

| Variable | Default | Meaning |
|---|---|---|
| `REQUIRE_DOMAIN_VERIFICATION` | `0` | `1` gates multi-page scans. |
| `VERIFY_TOKEN_SECRET` | *(generated)* | HMAC secret. Set it explicitly so every process agrees. |
| `VERIFY_META_NAME` | `site-audit-verification` | Meta-tag / TXT-record name. |
| `VERIFY_TTL` | `86400` | How long a verified domain stays verified. |

With the gate on, a **single-page** scan (`Max pages = 1`) of any URL runs for
anyone. A **multi-page** audit of a domain first needs the domain's token
published one of two ways:

```html
<meta name="site-audit-verification" content="sav-…">
```

```
site-audit-verification=sav-…      (DNS TXT record on the domain)
```

The token is an HMAC of the domain, so it is stable and needs no storage. The
UI shows both options and offers a "re-check" button and a "scan just this page
instead" fallback. `GET /api/verify?url=…&check=1` does the same from the
command line. DNS lookups use `dnspython` when installed and fall back to
`dig`; meta-tag checks fetch the homepage over HTTP.

---

## Partial runs, and saying so

A run always produces a report from whatever succeeded. If page 40 of 55
fails, the other 39 are still delivered - labelled with the coverage they
actually have.

**The ledger.** `runstate.RunState` records every page a stage touches as
`ok` / `blocked` / `timeout` / `error` / `skipped`, with the reason in the
tool's own words, and persists it to `run-status.json` inside the run folder
**after every stage**. A run whose worker died mid-scan is still reportable:

```bash
python consolidate_report.py runs/<run-folder> -o report.html
```

picks the ledger up on its own and states the coverage on the report's cover.

**The run status.** `complete` (every attempted page was measured), `partial`
(some were) or `failed` (none were). It appears on the report cover, in the
methodology section - with the list of pages that were not measured and why -
and as a banner in the UI.

**Friendly failures.** The common conditions are recognised from what the
scanners actually printed and turned into one plain sentence:

| Condition | What the person is told |
|---|---|
| whole-site 403 / bot protection | "This site is blocking automated scans. Try a site you control, or verify domain ownership." |
| DNS / unreachable | "We couldn't reach this site." |
| TLS / certificate | "The site's HTTPS certificate couldn't be verified." + why it matters |
| global timeout / stuck site | "The scan timed out." |

The raw log is never shown in place of that sentence - it stays one click
away, in a collapsed *Technical log* section for whoever wants it.

**Honesty carries through.** A partial run is labelled partial everywhere it
appears: coverage percentage, the pages that were skipped and why, and the
plain statement that an unmeasured page is unmeasured, not clean. The pinned
tool versions and the automated-only caveat are unchanged.

None of this touches the scoring, the metrics, the issue extraction or the
report's layout. The ledger records what happened; nothing in the report is
derived from it.

## How a scan flows

1. `POST /scan` - the browser submits the settings in the request body and
   gets back `{job, capped, params, stream}`. It then opens an SSE stream on
   the `stream` URL, which carries a job id and a session-bound token and
   nothing else. (`GET /scan?…` still does both in one request, for a scan
   with no credentials - it is the original entry point and is unchanged.)
2. The request is sanitized and capped (`guardrails.sanitize_params`), rate
   limited, and put through the ownership gate. Credentials come in through
   `guardrails.extract_credentials`, a separate door that reads the body only.
3. It is enqueued (`jobqueue`) - the credentials beside the job, never inside
   it - and the response starts streaming:
   `accepted` → `queued` (with position) → `running` → log lines → `done` /
   `fail`. A refusal comes back as a single `rejected` event carrying a
   reason (`queue_full`, `hourly`, `concurrent`, `unverified`, `invalid`) and,
   for `unverified`, the verification instructions. Refusals also set an
   `X-Scan-Rejected` header; a refused POST answers 4xx with the same payload.
4. A worker picks the job up and calls `run_pipeline` with exactly the
   arguments it has always taken, plus the scan conditions and (if any) the
   credentials (`tasks.run_scan_job`), which it wipes when the scan ends. The `done` payload
   carries the run's `status`, `coverage` and (when it fell short) the
   plain-language `notice`, so the browser can state what the report covers.
   A job that died without leaving a run folder gets the same treatment: its
   `fail` event carries a `notice` alongside the raw message.
5. Log lines travel back through the queue, so any web process can stream a
   scan any other one accepted.

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests -q
```

The suite never needs Node, a browser, or a Redis server: the pipeline is
stubbed at `run_pipeline`, and the RQ path runs against `fakeredis`.

* `tests/test_job_queue.py` - enqueue/dequeue, worker-pool bounds, queue
  positions, full-queue refusal, per-scan timeout.
* `tests/test_guardrails.py` - caps enforced even when the client oversends,
  rate limits, clean refusals, the ownership gate, and the SSE contract.
* `tests/test_redis_queue.py` - the same job through real RQ + Redis.
* `tests/test_scan_resilience.py` - a run where some pages fail (a partial
  report with the right coverage), a fully blocked site (the friendly state,
  not a crash), and the run budget (finalize as partial).
* `tests/test_runstate.py` - the ledger itself: what counts as blocked rather
  than broken, when a run is partial rather than failed, and the words each
  condition is given.
* `tests/test_scan_options.py` - every throttling preset, handset, viewport and
  User-Agent as it reaches the `lighthouse` and `unlighthouse-ci` command
  lines, and the guarantee that an unconfigured scan still runs the command it
  always ran.
* `tests/test_scan_auth.py` - Basic auth and cookies reaching both scanners,
  the scripted login taking its password on stdin rather than argv, and its
  session cookies travelling on into the scan.
* `tests/test_scan_auth_privacy.py` - the rule credentials live by, checked as
  searches rather than assertions about particular fields: no file in the run
  folder, no log line, no ledger entry, no job record, no streamed event, no
  saved setting and no URL may contain one - and, as the counterweight, that
  the scanners really were given them.
