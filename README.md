# Site Performance & Security Dashboard

Enter a URL, pick Desktop or Mobile, get one consolidated report: Lighthouse
scores and core metrics, itemized fix recommendations, plus optional passive
security checks and an axe-core accessibility-standards audit.

One setting decides the security posture: `DEPLOYMENT_MODE` is `local` (one
person, one machine - everything convenient stays on) or `public` (the
deployment takes scans from strangers, and the settings that would turn this
tool into someone else's network probe are forced off). See
[Deployment mode](#deployment-mode).

Under **Advanced options** the scan can also be shaped to match the real world:
a connection preset (or a custom one), a named handset, a viewport, a
User-Agent, a list of URLs to block, and a DNS override that points one
hostname at an address of your choosing - and, for a site that is not public,
HTTP Basic credentials, a pasted cookie jar, or a scripted form login. Any
combination of those (except the sign-in fields) can be **saved as a named
preset** and picked again next time.

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
web process. It is for one person on one machine, which is what
`DEPLOYMENT_MODE=local` (the default) assumes. Add `DASHBOARD_DRYRUN=1` to
click through the UI without a real scan.

To look at the interface itself - every state it can be in, and the report
beside it - without a server or a scan at all:

```bash
python ui_preview.py           # -> preview/index.html
python report_sample.py        # -> sample-report.html, from a fixture
```

A running local server serves the same thing at `/preview`. The app and the
report share one design system, re-skinnable from a single `brand.json`; see
**[DESIGN.md](DESIGN.md)**.

## Public run (Redis queue + worker pool + gunicorn)

Three processes, started in this order:

```bash
# 1. Redis - the queue, the rate-limit counters, and the live log fan-out
redis-server

# 2. The workers: this is the ONLY place a scan actually runs.
#    WORKER_CONCURRENCY is how many scans can be in flight at once.
DEPLOYMENT_MODE=public \
REDIS_URL=redis://127.0.0.1:6379/0 \
WORKER_CONCURRENCY=2 \
SCAN_TIMEOUT=900 \
python worker.py

# 3. The web tier - accepts, queues and streams; never scans.
DEPLOYMENT_MODE=public \
REDIS_URL=redis://127.0.0.1:6379/0 \
BIND=0.0.0.0:8000 \
gunicorn -c gunicorn.conf.py wsgi:app
```

`DEPLOYMENT_MODE=public` is the one setting that matters for a deployment that
takes scans from strangers - see [Deployment mode](#deployment-mode). If you
accept credentials in that mode, start Redis with persistence off
(`redis-server --save "" --appendonly no`), because the queue checks.

Both the web tier and the workers need the same `REDIS_URL` and the same
`VERIFY_TOKEN_SECRET`; give every process the same environment.

`gunicorn.conf.py` uses `gthread` workers with no request timeout, because a
scan stream is a long-lived SSE connection that mostly waits. The scan's own
limit is `SCAN_TIMEOUT`, enforced by RQ in the worker.

Scaling up means raising `WORKER_CONCURRENCY` or running `worker.py` on more
machines - never raising `WEB_WORKERS`, which only adds connection capacity.

### Health

* `GET /healthz`    - queue backend and current depth
* `GET /api/limits` - the mode, the caps and budgets this deployment enforces,
  and which optional controls it accepts at all
* `GET /api/job/<id>` - state of one job, for a client that lost its stream

---

## Configuration

Everything is environment variables; the defaults are the small, safe ones.

### Deployment mode

One setting decides the security posture. Set it first, and the rest of this
section is detail rather than a checklist.

| Variable | Default | Meaning |
|---|---|---|
| `DEPLOYMENT_MODE` | `local` | `local` = one person, one machine, trusted network. `public` = the deployment takes scans from strangers. |

**`local`** is what `python dashboard.py` has always done: DNS overrides
offered, credentials handed straight to the queue, no ownership proof asked of
anyone.

**`public`** forces four things, whatever else the environment says:

| Forced in `public` | Effect |
|---|---|
| `ALLOW_DNS_OVERRIDE=0` | The DNS fields are dropped from every request, and the UI stops offering them. |
| `ALLOW_PRIVATE_DNS_TARGETS=0` | The escape hatch below is not available at all. |
| `PERSIST_SCAN_AUTH=0` | Credentials are refused unless the queue's Redis *says* it has persistence off - see [What happens to the credentials](#what-happens-to-the-credentials). |
| `REQUIRE_VERIFIED_DOMAIN_FOR_AUTH=1` | HTTP auth, a cookie jar or a form login are only accepted for a domain whose ownership has been proved. |

The forcing is one-directional: `public` closes things and never opens them,
so `ALLOW_SCAN_AUTH=0` still works on top of it, and a `local` deployment can
set any of the four by hand. What it will not do is let a public deployment be
half-hardened - `DEPLOYMENT_MODE=public ALLOW_DNS_OVERRIDE=1` gets no DNS
override, because getting one of five booleans wrong should not be possible.

Anything set but unrecognised - `prod`, `Public`, a typo - reads as `public`.
`local` is spelled exactly one way (case and surrounding space aside). A
mistake in the variable that decides the posture buys the strict one.

`GET /api/limits` reports the mode and every flag derived from it, which is
how the UI knows to hide a field the deployment would only refuse.

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

**Blocked URLs.** A list of patterns - one per line, `*` matching any run of
characters, a bare domain matching anywhere in the URL - that must not load
while the pages are measured. This is real request interception, not a filter
over the results: Chrome is given the list for the Lighthouse audits
(`--blocked-url-patterns`, which is `Network.setBlockedURLs` underneath), the
crawl gets it through its own config file, and the Playwright runners abort
matching requests in the browsing context. A blocked third party therefore
costs the page no connection, no bytes and no main-thread time, which is what
makes "how fast is *my* site" answerable on a page full of tag managers.

**DNS override.** Map one hostname to one IP address for the length of the
scan - how a staging server is measured before its DNS is switched. It is
Chrome's own `--host-resolver-rules`, applied to every browser the scan
starts (the Lighthouse audits, the crawl, the axe runner and the scripted
login), so only the address the connection goes to changes: the request still
carries the real host name, the real SNI and the real cookies. Leave the host
empty and the site being scanned is the one that gets mapped.

| Variable | Default | Meaning |
|---|---|---|
| `ALLOW_DNS_OVERRIDE` | `1` (forced `0` in `public`) | `0` refuses DNS overrides and hides the fields. |
| `ALLOW_PRIVATE_DNS_TARGETS` | `0` (forced `0` in `public`) | `1` allows an override to point inside a private network. See below. |

**Where an override may point.** Even where the feature is offered, the
address has to be one the public internet can reach. These are refused, with a
message naming the address:

```
0.0.0.0/8  10.0.0.0/8  100.64.0.0/10  127.0.0.0/8  169.254.0.0/16
172.16.0.0/12  192.0.0.0/24  192.168.0.0/16  198.18.0.0/15
224.0.0.0/4  240.0.0.0/4
::/128  ::1/128  fc00::/7  fe80::/10  ff00::/8
```

...including the same addresses written as IPv4-mapped (`::ffff:10.0.0.9`) or
6to4 (`2002:a00:9::`) IPv6. Otherwise the override is not "measure my staging
box", it is the scanner being used to reach something only the scanning host
can reach - its own loopback, the private network it sits on, or the cloud
metadata service at `169.254.169.254`, which hands out credentials to anything
that asks it.

It is **refused**, not quietly dropped: a scan that silently measured the
public site instead of the staging box would be a wrong answer delivered as a
right one.

If your staging box really is on `10.x` - the ordinary case for a laptop and a
LAN - set `ALLOW_PRIVATE_DNS_TARGETS=1`. That is a deliberate statement that
this deployment scans its own network, so `public` mode does not offer it.
The documentation ranges (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`)
are *not* on the list, so the examples above and in the UI work as written.

Comparing two runs is only meaningful when these match - a 3G Pixel and an
unthrottled desktop are not two measurements of the same thing.

## Presets

A preset is a name with a set of scan settings attached: device, depth,
categories, throttling, viewport, User-Agent, block list, DNS override, max
pages, parallelism, standards. Pick one from the box at the top of **Advanced
options** and the whole panel fills in; save, rename, update or delete them
there too, and mark one to start new scans on.

Four are built in and always offered: *Default (as shipped)* - the settings
this tool has always run with; *Mobile 4G handset*; *First-party only*, which
blocks the usual analytics and ad hosts; and *Staging server (unthrottled)*.
Built-ins cannot be renamed or deleted.

**A preset never holds a credential.** Not HTTP Basic details, not cookies,
not the form-login fields - those are used in memory for one scan and wiped
(see below), so a preset re-selected tomorrow asks for them again. That is
enforced rather than intended: `scanpresets.PRESET_FIELDS` is the fixed list a
preset is assembled from, so a credential field is never read in the first
place; a body carrying one is refused with a 400 rather than quietly trimmed;
and the store refuses to persist a preset that contains one however it got
there. The browser is handed that same field list, so the two ends cannot
drift apart.

Presets are per browser session (the `scan_sid` cookie, hashed - the store
never holds the session id itself). They live in JSON files under `presets/`
next to the app, or in Redis where the deployment has it.

```
GET    /api/presets            -> {presets: [...], default: "<id>", fields: [...]}
POST   /api/presets            -> create, update, rename, or set the default
DELETE /api/presets/<id>       -> delete one
```

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

  In `public` mode that stops being advice. `PERSIST_SCAN_AUTH` is forced off,
  and the queue then asks the server (`CONFIG GET save` / `appendonly`) before
  it will write a credential key at all. A Redis with RDB or AOF on is refused,
  with the reason; so is one that will not answer the question - a managed
  Redis with `CONFIG` locked down cannot prove it, and not answering is not the
  same as answering "no" when somebody's password is what is being bet. The
  answer is asked for once per process and remembered. The refusal reaches the
  client as a 400 before the job is queued, rather than the scan quietly
  running as an anonymous visitor and reporting the login page as the site.

| Variable | Default | Meaning |
|---|---|---|
| `ALLOW_SCAN_AUTH` | `1` | `0` refuses credentials outright and hides the sign-in fields. |
| `PERSIST_SCAN_AUTH` | `1` (forced `0` in `public`) | `0` requires proof that the queue's Redis does not write to disk before a credential is accepted. |
| `REQUIRE_VERIFIED_DOMAIN_FOR_AUTH` | `0` (forced `1` in `public`) | `1` accepts credentials only for a domain whose ownership has been proved. |

**Credentials and domain ownership.** With
`REQUIRE_VERIFIED_DOMAIN_FOR_AUTH=1`, a scan carrying HTTP auth, a cookie jar
or a form login is refused for a domain nobody has proved they control - the
same token, published the same two ways, as the gate below. It is a *separate*
gate from `REQUIRE_DOMAIN_VERIFICATION`, and a stricter one: that gate is about
what a crawl costs this deployment, so it only applies to multi-page scans,
while this one is about whose password is being handed over, so a single-page
scan with a cookie jar is gated exactly like a twenty-page one. A scan with no
sign-in details is untouched by it.

### Domain-ownership gate

Off by default (internal use). Turn it on for a public deployment:

| Variable | Default | Meaning |
|---|---|---|
| `REQUIRE_DOMAIN_VERIFICATION` | `0` | `1` gates multi-page scans. Not forced by `public` - it is about what a crawl costs you, not about safety. |
| `REQUIRE_VERIFIED_DOMAIN_FOR_AUTH` | `0` (forced `1` in `public`) | `1` gates any scan carrying credentials, whatever its size. |
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

The two gates use the same token and the same two publishing methods, and
differ only in what they gate: `REQUIRE_DOMAIN_VERIFICATION` gates multi-page
crawls (a cost question, so `Max pages = 1` escapes it), and
`REQUIRE_VERIFIED_DOMAIN_FOR_AUTH` gates any scan carrying sign-in details (a
whose-password question, so nothing escapes it but leaving the fields empty).

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
   nothing else. (`GET /scan?…` still does both in one request - see
   **Two doors, one set of guards** below.)
2. The request is sanitized and capped (`guardrails.sanitize_params`), rate
   limited, and put through both ownership gates. Credentials come in through
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

### Two doors, one set of guards

`GET /scan` was **kept**, not removed. It is the whole API for an
unauthenticated scan from a script or a `curl`, and dropping it would break
every caller that has one for the sake of a door that is not actually open.

It is not a second admission path. Both routes hand their input to the same
`dashboard._admit`, so `sanitize_params`, the hard caps, the rate limiter,
both verification gates and the private-address check on a DNS override are
one implementation reached two ways. `tests/test_scan_endpoint_parity.py`
runs each guard through both doors and compares the answers, so the two cannot
drift apart; it also asserts that no route admits a scan without going through
`_admit`. `/scan/stream/<job>` is not a third door - it carries a job id and a
token and no scan settings at all.

The one deliberate difference is credentials. The POST body has a door for
them; a query string never will, because it reaches access logs, `Referer`
headers and the user's own browser history. A `GET /scan` carrying one of the
sign-in fields is **refused** rather than scanned without them - the refusal
happens before admission, so it costs the client nothing, and it beats handing
back a report of the login page.

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
* `tests/test_scan_request_controls.py` - blocked URLs and the DNS override:
  the flags and config each scanner is given, and the interception code itself
  driven under Node to check that a matching request really is aborted and
  everything else is left alone.
* `tests/test_scan_presets.py` - presets round-tripping (save, reload, and
  apply as a real scan request), create/rename/update/delete, the default
  preset, and - from every direction a credential could get in - that no
  preset ever holds one.
* `tests/test_scan_auth.py` - Basic auth and cookies reaching both scanners,
  the scripted login taking its password on stdin rather than argv, and its
  session cookies travelling on into the scan.
* `tests/test_scan_auth_privacy.py` - the rule credentials live by, checked as
  searches rather than assertions about particular fields: no file in the run
  folder, no log line, no ledger entry, no job record, no streamed event, no
  saved setting and no URL may contain one - and, as the counterweight, that
  the scanners really were given them.
* `tests/test_design_system.py` - the look, held to its promise: both surfaces
  emit one token set and one shared component layer, no rule hard-codes a
  colour, type size or spacing step, a re-skin leaves nothing of the default
  behind, and no brand input can recolour a verdict.
* `tests/test_deployment_mode.py` - the defaults of both modes, that `public`
  cannot be loosened one variable at a time, that an unrecognised value reads
  as `public`, the credential ownership gate, the Redis persistence proof, and
  what `/api/limits` tells the UI.
* `tests/test_dns_target_ranges.py` - every private, loopback, link-local,
  metadata, multicast and reserved range an override may not point at, in each
  spelling (plain, IPv4-mapped, 6to4), refused through both routes; the public
  addresses that still work; and the one escape hatch that opens it.
* `tests/test_scan_endpoint_parity.py` - each guard run through `GET /scan`
  and `POST /scan` and the two answers compared, so the two entry points
  cannot drift apart on caps, sanitisation, gates or rate limits.

### One thing the suite cannot check

**Block-URL and DNS override on the `unlighthouse-ci` crawl.** The tests pin
the *configuration* end to end - that `unlighthouse.config.js` is written into
the crawl's working directory with the right `lighthouseOptions.blockedUrlPatterns`
and `puppeteerOptions.args`, and that Chrome and the Playwright runners are
handed the same. What no test can check is that unlighthouse actually honours
that config file, because that is a promise made by a third-party tool and a
real browser. Confirm it by hand after upgrading `unlighthouse`, or when a
scan's numbers look wrong:

1. Pick a site whose homepage loads a third party you can see in the report -
   `www.theguardian.com` and `edition.cnn.com` both pull in ad and analytics
   hosts. Run a **deep** scan of it (Max pages > 1, so the crawl runs) with
   **Blocked URLs** empty, and keep the run folder.
2. Run the same scan again with `*doubleclick.net*` and
   `*google-analytics.com*` in **Blocked URLs**.
3. Open both reports. In the second, the blocked hosts must be **absent** from
   the network requests of *every crawled page*, not just the entry URL - that
   is the half only the crawl can prove. Request count and total bytes should
   drop with it. If the third party is still there on pages 2..n, the crawl
   ignored the config file: check that `unlighthouse.config.js` exists in the
   run folder (the scan log names the directory) and that its
   `lighthouseOptions.blockedUrlPatterns` is what you typed.
4. For the **DNS override**, point a hostname you control at a public address
   that serves visibly different content - two hosts of your own, or a
   staging box on a routable IP. Scan `https://<hostname>/` with **Resolve
   host** = that hostname and **To this address** = the other host's IP. Every
   page in the report must show the *other* host's content while the report,
   the run folder name and the request's `Host` header still say the original
   hostname; that combination is what proves `--host-resolver-rules` reached
   the crawl's browser rather than only the entry-page audit. The scan log's
   conditions line says `<host> resolved to <ip>` when the override was built
   at all - if that line is missing, the request never carried one, and the
   deployment's `ALLOW_DNS_OVERRIDE` / `ALLOW_PRIVATE_DNS_TARGETS` settings
   are the first thing to check.
