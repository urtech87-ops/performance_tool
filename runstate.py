#!/usr/bin/env python3
"""
Run state: per-page outcomes, run status, and plain-language failures
=====================================================================
A scan is a lot of independent page fetches, and any of them can fail on its
own. Before this module a failure was only ever a line in the log: the run
either produced a report or it did not, and nothing recorded *which* pages went
missing or why. This is the ledger that fixes that.

Three jobs, and nothing else:

  1. Per-page status.  Every page a stage touches is recorded as ok / blocked /
     timeout / error / skipped, with the reason in the tool's own words. The
     ledger is persisted to `run-status.json` inside the run folder after every
     stage, so a report can be built from partial data at any point - including
     after the worker that was running the scan died.

  2. Run status.  `complete` (every attempted page came back), `partial` (some
     did) or `failed` (none did). Coverage is counted, never estimated.

  3. Plain language.  `classify()` turns a scanner's raw output into one of a
     handful of known conditions (blocked, dns, tls, timeout, unreachable), and
     `friendly()` turns that into the sentence a person is shown. The raw text
     is kept alongside it for the log, never in place of it.

It scores nothing, aggregates no findings and changes no report math. It only
records what happened.
"""

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---- per-page outcomes ---------------------------------------------------
OK = "ok"
BLOCKED = "blocked"
TIMEOUT = "timeout"
ERROR = "error"
SKIPPED = "skipped"

PAGE_STATUSES = (OK, BLOCKED, TIMEOUT, ERROR, SKIPPED)

# Which outcome wins when one page fails differently in two stages: a page the
# site refused outright is a harder fact than one that merely errored.
_RANK = {OK: 0, SKIPPED: 1, ERROR: 2, TIMEOUT: 3, BLOCKED: 4}

# ---- run-level outcomes --------------------------------------------------
COMPLETE = "complete"
PARTIAL = "partial"
FAILED = "failed"

STATUS_FILE = "run-status.json"

# ---- known conditions, in plain language ---------------------------------
# kind -> (title, message). Nothing here mentions a tool, an exit code or a
# stack trace: this is what the person who asked for the scan is shown.
FRIENDLY = {
    BLOCKED: (
        "This site is blocking automated scans",
        "The site answered our requests with a refusal (bot protection, a "
        "firewall rule or a CAPTCHA), so its pages could not be measured. Try a "
        "site you control, or verify domain ownership to scan this one."),
    "dns": (
        "We couldn't reach this site",
        "The address didn't resolve to a server. Check the domain for a typo, "
        "and that it is publicly reachable from the internet."),
    "unreachable": (
        "We couldn't reach this site",
        "The server refused the connection or never answered. It may be down, "
        "firewalled, or only reachable from inside a private network."),
    "tls": (
        "The site's HTTPS certificate couldn't be verified",
        "The certificate is expired, self-signed, or missing part of its chain. "
        "Browsers may warn visitors before the page even loads; fixing the "
        "certificate is the first step before the rest of this report matters."),
    TIMEOUT: (
        "The scan timed out",
        "The site took longer to respond than this scan allows. That is often a "
        "very slow page, a redirect loop, or a server under load - a report was "
        "built from whatever finished in time."),
    ERROR: (
        "The scan couldn't be completed",
        "Something went wrong while measuring this site. The technical log below "
        "has the details."),
}

FAILURE_KINDS = tuple(FRIENDLY)

# The page-level outcome each condition implies.
_KIND_TO_PAGE_STATUS = {
    BLOCKED: BLOCKED,
    TIMEOUT: TIMEOUT,
    "dns": ERROR,
    "tls": ERROR,
    "unreachable": ERROR,
    ERROR: ERROR,
}

# Ordered: the first pattern that matches decides. DNS and TLS come first
# because their messages often also carry the word "connect" or "timeout".
_PATTERNS = [
    ("dns", re.compile(
        r"ERR_NAME_NOT_RESOLVED|ERR_NAME_RESOLUTION_FAILED|DNS_PROBE|NXDOMAIN"
        r"|ENOTFOUND|getaddrinfo|name or service not known"
        r"|nodename nor servname|no address associated with hostname", re.I)),
    ("tls", re.compile(
        r"ERR_CERT[_A-Z]*|ERR_SSL[_A-Z]*|CERTIFICATE_VERIFY_FAILED"
        r"|certificate verif\w+ failed|self.signed certificate|unable to get "
        r"local issuer|SSLError|SSLCertVerificationError|ssl handshake"
        r"|UNABLE_TO_VERIFY_LEAF|hostname mismatch", re.I)),
    (BLOCKED, re.compile(
        r"ERR_BLOCKED_BY[_A-Z]*|\b(?:40[13]|429)\b|forbidden|access denied"
        r"|permission denied by the (?:site|server)|bot ?(?:protection|detection)"
        r"|captcha|are you a human|just a moment|attention required"
        r"|cloudflare|please enable (?:js|javascript) and cookies"
        r"|rate.?limited|too many requests", re.I)),
    (TIMEOUT, re.compile(
        r"\btimed? ?out\b|\btimeout\b|ETIMEDOUT|ERR_TIMED_OUT|PAGE_HUNG"
        r"|NO_FCP|ERR_CONNECTION_TIMED_OUT|deadline exceeded"
        # the queue's own words when it kills a job that overran
        r"|exceeded the \d+s limit|\(limit \d+s\)|time limit reached", re.I)),
    ("unreachable", re.compile(
        r"ECONNREFUSED|ECONNRESET|EHOSTUNREACH|ENETUNREACH|ERR_CONNECTION_REFUSED"
        r"|ERR_CONNECTION_RESET|ERR_CONNECTION_CLOSED|ERR_ADDRESS_UNREACHABLE"
        r"|ERR_EMPTY_RESPONSE|ERR_INTERNET_DISCONNECTED|connection refused"
        r"|network is unreachable|failed to establish a new connection", re.I)),
]


def classify(text):
    """The known condition `text` describes, or None if it is just an error.

    `text` is whatever a scanner said - stdout, stderr, an exception. Nothing
    here interprets a score or a finding; it reads failure output only.
    """
    blob = str(text or "")
    if not blob.strip():
        return None
    for kind, pattern in _PATTERNS:
        if pattern.search(blob):
            return kind
    return None


def page_status(kind):
    """The per-page outcome a condition maps to (unknown conditions are errors)."""
    return _KIND_TO_PAGE_STATUS.get(kind, ERROR)


def friendly(kind, status=None):
    """The banner for a condition: {kind, title, message} - never raw output."""
    title, message = FRIENDLY.get(kind or ERROR, FRIENDLY[ERROR])
    notice = {"kind": kind or ERROR, "title": title, "message": message}
    if status:
        notice["status"] = status
    return notice


def friendly_from_error(text, status=FAILED):
    """A banner for an error string that never reached a run folder - a worker
    that died, or a job the queue timed out."""
    kind = classify(text)
    if kind is None and text:
        kind = ERROR
    return friendly(kind, status)


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------

PREFLIGHT_UA = ("Mozilla/5.0 (compatible; SiteAuditBot/1.0; "
                "+automated performance, accessibility and security scan)")


def preflight(url, timeout=10, opener=None):
    """One cheap request to see whether the site will talk to us at all.

    Returns {"kind": <condition or None>, "code": <http status or None>,
    "detail": <one line, the server's or the library's own words>}. It never
    raises: an unknown failure is reported as an unknown failure.

    This is a signal, not a verdict. A site that refuses this request may still
    let real Chrome through, so the scan runs regardless - the signal is what
    lets a run that ends up with zero pages say *why* instead of shrugging.
    """
    request = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": PREFLIGHT_UA,
                                              "Accept": "text/html,*/*"})
    try:
        opener = opener or urllib.request.urlopen
        resp = opener(request, timeout=timeout)
        code = getattr(resp, "status", None) or getattr(resp, "code", None)
        try:
            resp.close()
        except Exception:                                          # noqa: BLE001
            pass
        if code and int(code) in (401, 403, 429):
            return {"kind": BLOCKED, "code": int(code),
                    "detail": f"the site answered HTTP {int(code)}"}
        return {"kind": None, "code": int(code) if code else None, "detail": ""}
    except urllib.error.HTTPError as exc:
        code = int(getattr(exc, "code", 0) or 0)
        kind = BLOCKED if code in (401, 403, 429) else None
        return {"kind": kind, "code": code or None,
                "detail": f"the site answered HTTP {code}" if code else str(exc)}
    except Exception as exc:                                       # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"
        return {"kind": classify(detail) or classify(str(getattr(exc, "reason", ""))),
                "code": None, "detail": detail}


# --------------------------------------------------------------------------
# the ledger
# --------------------------------------------------------------------------

def _now():
    """The clock the ledger runs on - one indirection so a test can drive it."""
    return time.time()


class RunState:
    """Everything a run knows about its own completeness.

    Built at the top of the pipeline, handed a result for every page every
    stage touches, and saved after each one. `load()` reads it back, so the
    report (and the UI) can be built from a run that never finished.
    """

    def __init__(self, run_dir=None, run_name="", url="", device="",
                 budget=None, clock=None):
        self.run_dir = Path(run_dir) if run_dir else None
        self.run_name = run_name or (self.run_dir.name if self.run_dir else "")
        self.url = url
        self.device = device
        self.budget = budget                    # seconds, or None for no ceiling
        self._clock = clock or _now
        self.started_at = self._clock()
        self.finished_at = None
        self.pages = {}                         # url -> record
        self.stages = {}                        # stage -> {"state","detail"}
        self.notes = []                         # degradations, in order
        self.signal = None                      # preflight condition, if any
        self.signal_detail = ""
        self.primary_stage = ""
        self.budget_hit = False

    # -- clock ----------------------------------------------------------
    def elapsed(self):
        return max(0.0, self._clock() - self.started_at)

    def time_left(self):
        """Seconds of run budget left, or None when the run has no ceiling."""
        if not self.budget:
            return None
        return max(0.0, self.budget - self.elapsed())

    def expired(self):
        """True once the run-level ceiling is reached - one bad site must not
        tie up a worker past it."""
        left = self.time_left()
        return left is not None and left <= 0

    # -- recording ------------------------------------------------------
    def site_signal(self, kind, detail=""):
        if kind:
            self.signal = kind
            self.signal_detail = str(detail or "")
        return self

    def note(self, text):
        """One line about something the run had to give up on."""
        text = str(text or "").strip()
        if text and text not in self.notes:
            self.notes.append(text)
        return self

    def stage(self, name, state, detail=""):
        """How a whole stage went: ok / skipped / failed, plus why."""
        self.stages[name] = {"state": state, "detail": str(detail or "")}
        return self

    def record(self, url, stage, status, reason="", seconds=None, attempts=1):
        """One page, one stage, one outcome."""
        if status not in PAGE_STATUSES:
            status = ERROR
        page = self.pages.setdefault(str(url), {"url": str(url), "status": OK,
                                                "reason": "", "stages": {}})
        page["stages"][stage] = {"status": status, "reason": str(reason or ""),
                                 "seconds": seconds, "attempts": attempts}
        worst = max(page["stages"].values(), key=lambda s: _RANK[s["status"]])
        page["status"] = worst["status"]
        page["reason"] = worst["reason"]
        return page

    def skip_remaining(self, urls, stage, reason):
        """Pages a stage never got to - recorded, not forgotten."""
        for url in urls:
            page = self.pages.get(str(url))
            if page and stage in page["stages"]:
                continue
            self.record(url, stage, SKIPPED, reason)
        return self

    # -- reading --------------------------------------------------------
    def stage_pages(self, stage):
        return [p for p in self.pages.values() if stage in p["stages"]]

    def coverage(self, stage=None):
        """{ok, attempted, percent, failed, by_status} for one stage.

        With no stage given it uses the run's primary one (the stage the report
        is built from), falling back to every page the run touched.
        """
        stage = stage or self.primary_stage
        records = self.stage_pages(stage) if stage else list(self.pages.values())
        if stage:
            statuses = [p["stages"][stage]["status"] for p in records]
        else:
            statuses = [p["status"] for p in records]
        attempted = len(statuses)
        ok = sum(1 for s in statuses if s == OK)
        by_status = {s: statuses.count(s) for s in PAGE_STATUSES if statuses.count(s)}
        return {"stage": stage or "", "ok": ok, "attempted": attempted,
                "failed": attempted - ok,
                "percent": int(round(100.0 * ok / attempted)) if attempted else 0,
                "by_status": by_status}

    def failures(self, stage=None):
        """The pages that did not come back, with the reason each gave."""
        stage = stage or self.primary_stage
        out = []
        for page in self.pages.values():
            entry = page["stages"].get(stage) if stage else None
            status = entry["status"] if entry else page["status"]
            reason = entry["reason"] if entry else page["reason"]
            if stage and entry is None:
                continue
            if status != OK:
                out.append({"url": page["url"], "status": status, "reason": reason})
        out.sort(key=lambda f: (-_RANK[f["status"]], f["url"]))
        return out

    @property
    def status(self):
        """complete / partial / failed, from what was actually recorded.

        The primary stage - the one the report is built from - decides. A run
        whose primary stage came back empty is only `failed` when nothing else
        produced anything either: a crawl that scored pages is a real result,
        even when every deep audit on top of it was refused.
        """
        cov = self.coverage()
        # A page any stage could not measure degrades the run, even when the
        # stage the report is built from came back clean: the missing result is
        # missing either way.
        degraded = (self.budget_hit or bool(self.notes) or self._stage_failed()
                    or self._any_failed())
        if cov["ok"]:
            return PARTIAL if (cov["failed"] or degraded) else COMPLETE
        if self._any_ok(besides=self.primary_stage):
            return PARTIAL
        if cov["attempted"] or self.signal or self._stage_failed():
            return FAILED
        return COMPLETE

    def _any_failed(self):
        """True when any stage failed to measure any page."""
        return any(entry["status"] != OK
                   for page in self.pages.values()
                   for entry in page["stages"].values())

    def _any_ok(self, besides=""):
        """True when some other stage measured at least one page."""
        for page in self.pages.values():
            for name, entry in page["stages"].items():
                if name != besides and entry["status"] == OK:
                    return True
        return False

    def _stage_failed(self):
        return any(s["state"] == "failed" for s in self.stages.values())

    @property
    def failure_kind(self):
        """The condition to explain a degraded run with, or None when clean."""
        status = self.status
        if status == COMPLETE:
            return None
        if self.budget_hit:
            return TIMEOUT
        failures = self.failures()
        if failures:
            worst = failures[0]["status"]
            if worst == BLOCKED:
                return BLOCKED
            if worst == TIMEOUT:
                return TIMEOUT
            # A page-level error is best explained by whatever the site itself
            # told us on the way in.
            return self.signal or ERROR
        return self.signal or ERROR

    def notice(self):
        """The banner for this run, or None when it completed cleanly."""
        status = self.status
        if status == COMPLETE:
            return None
        kind = self.failure_kind
        if status == FAILED and self.signal in ("dns", "unreachable", "tls"):
            kind = self.signal
        note = friendly(kind, status)
        cov = self.coverage()
        if status == PARTIAL and cov["attempted"]:
            note["message"] = (
                f"{note['message']} This report covers the {cov['ok']} of "
                f"{cov['attempted']} page(s) that were measured.")
        return note

    # -- persistence ----------------------------------------------------
    def as_dict(self):
        cov = self.coverage()
        return {
            "run": self.run_name,
            "url": self.url,
            "device": self.device,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "budget": self.budget,
            "budget_hit": self.budget_hit,
            "primary_stage": self.primary_stage,
            "coverage": cov,
            "coverage_by_stage": {name: self.coverage(name)
                                  for name in sorted(self._stage_names())},
            "notice": self.notice(),
            "signal": self.signal,
            "signal_detail": self.signal_detail,
            "stages": self.stages,
            "notes": list(self.notes),
            "failures": self.failures(),
            "pages": sorted(self.pages.values(), key=lambda p: p["url"]),
        }

    def _stage_names(self):
        names = set(self.stages)
        for page in self.pages.values():
            names.update(page["stages"])
        return names

    def save(self):
        """Persist the ledger next to the run's other output.

        Called after every stage, so the file on disk always describes a run
        that can be reported on - even one that is still going, or one whose
        worker was killed mid-scan.
        """
        if self.run_dir is None:
            return None
        path = Path(self.run_dir) / STATUS_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(self.as_dict(), indent=2, default=str),
                           encoding="utf-8")
            tmp.replace(path)
        except OSError:
            return None
        return path

    def finalize(self):
        """Close the run and write the ledger one last time."""
        if self.finished_at is None:
            self.finished_at = self._clock()
        self.save()
        return self.status

    # -- what the report is told ----------------------------------------
    def report_coverage(self, stage=None):
        """The coverage block handed to the report builder.

        Counts and reasons only - the report renders them, it does not
        recompute anything from them, and no score is derived from them.
        """
        cov = self.coverage(stage)
        return {
            "status": self.status,
            "pages_ok": cov["ok"],
            "pages_attempted": cov["attempted"],
            "percent": cov["percent"],
            "budget_hit": self.budget_hit,
            "notes": list(self.notes),
            "failures": self.failures(stage),
            "notice": self.notice(),
        }

    # -- reading a run back ---------------------------------------------
    @classmethod
    def load(cls, run_dir):
        """The persisted ledger of a run, or None when it has none."""
        path = Path(run_dir) / STATUS_FILE
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


def report_coverage_from(saved):
    """The report-shaped coverage block for a ledger read back off disk.

    This is what lets a report be built from a run that never finished - the
    file is written after every stage, so whatever is on disk describes a run
    that can be reported on honestly.
    """
    if not isinstance(saved, dict):
        return None
    cov = saved.get("coverage") or {}
    return {
        "status": saved.get("status") or PARTIAL,
        "pages_ok": cov.get("ok", 0),
        "pages_attempted": cov.get("attempted", 0),
        "percent": cov.get("percent", 0),
        "budget_hit": bool(saved.get("budget_hit")),
        "notes": list(saved.get("notes") or []),
        "failures": list(saved.get("failures") or []),
        "notice": saved.get("notice"),
    }


def coverage_sentence(coverage):
    """"Coverage: 39 of 55 pages (71%)" - the same words everywhere it appears."""
    if not coverage:
        return ""
    ok = coverage.get("pages_ok", 0)
    attempted = coverage.get("pages_attempted", 0)
    if not attempted:
        return "Coverage: no pages were measured"
    return (f"Coverage: {ok} of {attempted} page(s) "
            f"({coverage.get('percent', 0)}%)")
