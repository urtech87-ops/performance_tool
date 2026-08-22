#!/usr/bin/env python3
"""
Request guardrails
==================
Two things stand between an anonymous request and a worker:

  * `sanitize_params` - the only place scan parameters are read from a client.
    Every numeric knob is clamped to a server-side cap, so what the client
    sends can never make a scan bigger than the deployment allows.
  * `RateLimiter`     - per-IP and per-session budgets: scans per hour and
    concurrent scans per client.

Neither knows anything about how a scan works; they shape the *request*, never
the pipeline.
"""

import hashlib
import re
import time
from urllib.parse import urlparse

from config import settings

VALID_CATEGORIES = ["performance", "accessibility", "best-practices", "seo"]

# The values the UI ships with, used when a request omits them.
DEFAULT_SAMPLES = 1
DEFAULT_MAX_PAGES = 30
DEFAULT_PARALLEL = 3


def _int(raw, default):
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError, AttributeError):
        return default


def normalize_url(raw):
    """Trim, add a scheme if missing, and reject anything that is not a plain
    http(s) URL with a hostname (no file://, javascript:, credentials, ...)."""
    url = (raw or "").strip()
    if not url:
        return None, "No URL provided."
    if not urlparse(url).scheme:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None, "Only http:// and https:// URLs can be scanned."
    if not parsed.hostname:
        return None, "That URL has no hostname."
    if parsed.username or parsed.password:
        return None, "URLs with embedded credentials are not accepted."
    return url, None


def registrable_domain(url):
    """The host a scan belongs to, lowercased and without a leading www."""
    host = (urlparse(url).hostname or "").lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def sanitize_params(args):
    """Turn raw request args into the exact arguments `run_pipeline` takes.

    Returns `(params, error, capped)`:
      params - dict ready to hand to the pipeline (None when `error` is set)
      error  - a message for the client, or None
      capped - names of the knobs that were clamped, so the UI can say so
    """
    get = args.get                              # dict or werkzeug MultiDict

    url, error = normalize_url(get("url"))
    if error:
        return None, error, []

    capped = []

    def cap(name, raw, default, ceiling):
        wanted = max(1, _int(raw, default))
        if wanted > ceiling:
            capped.append(name)
            return ceiling
        return wanted

    samples = cap("samples", get("samples"), DEFAULT_SAMPLES, settings.CAP_SAMPLES)
    max_pages = cap("max_pages", get("max_pages"), DEFAULT_MAX_PAGES, settings.CAP_MAX_PAGES)
    concurrency = cap("parallel", get("concurrency"), DEFAULT_PARALLEL, settings.CAP_PARALLEL)

    raw_cats = (get("categories") or "").split(",")
    categories = [c for c in VALID_CATEGORIES if c in raw_cats]  # canonical order, junk dropped
    standards = re.findall(r"[a-z0-9_]+", (get("standards") or "").lower())[:40]

    params = {
        "url": url,
        "device": "desktop" if get("device") == "desktop" else "mobile",
        "samples": samples,
        "deep": get("deep", "1") == "1",
        "max_pages": max_pages,
        "concurrency": concurrency,
        "security": get("security", "0") == "1",
        "categories": categories,
        "a11y": get("a11y", "0") == "1",
        "standards": standards,
    }
    return params, None, capped


def is_multipage_scan(params):
    """A scan that walks a whole domain, as opposed to a single-page look at
    one URL. Only these need proof the requester owns the domain."""
    return params.get("max_pages", 1) > 1


# --------------------------------------------------------------------------
# rate limiting
# --------------------------------------------------------------------------

def client_keys(remote_addr, session_id):
    """The buckets a request is charged against: its IP and its session."""
    keys = []
    if remote_addr:
        keys.append(("ip", hashlib.sha256(remote_addr.encode()).hexdigest()[:24]))
    if session_id:
        keys.append(("session", hashlib.sha256(session_id.encode()).hexdigest()[:24]))
    return keys or [("ip", "unknown")]


class MemoryLimitStore:
    """Per-process counters - correct for the single-process dev server."""

    def __init__(self):
        self._hits = {}     # key -> [timestamps]
        self._active = {}   # key -> {job_id}

    def record_hit(self, key, now, window):
        hits = [t for t in self._hits.get(key, []) if t > now - window]
        hits.append(now)
        self._hits[key] = hits
        return len(hits)

    def count_hits(self, key, now, window):
        hits = [t for t in self._hits.get(key, []) if t > now - window]
        self._hits[key] = hits
        return len(hits)

    def add_active(self, key, job_id, ttl):
        self._active.setdefault(key, set()).add(job_id)

    def active(self, key):
        return set(self._active.get(key, ()))

    def drop_active(self, key, job_ids):
        current = self._active.get(key)
        if current:
            current.difference_update(job_ids)


class RedisLimitStore:
    """Shared counters, so every gunicorn worker enforces the same budget."""

    def __init__(self, redis_conn):
        self.r = redis_conn

    def record_hit(self, key, now, window):
        pipe = self.r.pipeline()
        rkey = f"rl:hits:{key}"
        pipe.zremrangebyscore(rkey, "-inf", now - window)
        pipe.zadd(rkey, {f"{now}:{id(self)}:{time.monotonic_ns()}": now})
        pipe.zcard(rkey)
        pipe.expire(rkey, int(window) + 60)
        return pipe.execute()[2]

    def count_hits(self, key, now, window):
        pipe = self.r.pipeline()
        rkey = f"rl:hits:{key}"
        pipe.zremrangebyscore(rkey, "-inf", now - window)
        pipe.zcard(rkey)
        return pipe.execute()[1]

    def add_active(self, key, job_id, ttl):
        rkey = f"rl:active:{key}"
        self.r.sadd(rkey, job_id)
        self.r.expire(rkey, int(ttl))

    def active(self, key):
        return {v.decode() if isinstance(v, bytes) else v
                for v in self.r.smembers(f"rl:active:{key}")}

    def drop_active(self, key, job_ids):
        if job_ids:
            self.r.srem(f"rl:active:{key}", *job_ids)


class RateLimitError(Exception):
    """Raised when a client is over one of its budgets."""

    def __init__(self, message, reason):
        super().__init__(message)
        self.message = message
        self.reason = reason


class RateLimiter:
    """Per-IP and per-session budgets.

    `is_finished(job_id) -> bool` lets the limiter prune concurrency slots that
    belong to jobs which already ended, so a crashed worker cannot leak a slot.
    """

    def __init__(self, store, is_finished=None):
        self.store = store
        self.is_finished = is_finished or (lambda job_id: True)

    def _live(self, key):
        held = self.store.active(key)
        if not held:
            return set()
        finished = {j for j in held if self.is_finished(j)}
        if finished:
            self.store.drop_active(key, finished)
        return held - finished

    def check(self, keys, now=None):
        """Raise RateLimitError if any bucket is already at its limit."""
        if not settings.RATE_LIMIT_ENABLED:
            return
        now = time.time() if now is None else now
        window = settings.RATE_LIMIT_WINDOW
        for kind, key in keys:
            live = self._live(key)
            if len(live) >= settings.MAX_CONCURRENT_SCANS:
                raise RateLimitError(
                    f"You already have {len(live)} scan(s) running. "
                    "Wait for them to finish, then start another.",
                    "concurrent")
            if self.store.count_hits(key, now, window) >= settings.MAX_SCANS_PER_HOUR:
                minutes = max(1, window // 60)
                raise RateLimitError(
                    f"Limit of {settings.MAX_SCANS_PER_HOUR} scans per "
                    f"{minutes} minutes reached for this {kind}. Try again shortly.",
                    "hourly")

    def reserve(self, keys, job_id):
        """Take the concurrency slot *before* the job exists.

        Between enqueue and charge a job is already running but not yet booked,
        which is exactly long enough for a second request to slip past the
        concurrency limit. Reserving first closes that window; an id that never
        makes it onto the queue reads as finished and is pruned.
        """
        if not settings.RATE_LIMIT_ENABLED:
            return
        for _kind, key in keys:
            self.store.add_active(key, job_id, settings.SCAN_TIMEOUT * 2)

    def charge(self, keys, job_id, now=None):
        """Book an accepted scan against every bucket's hourly budget."""
        if not settings.RATE_LIMIT_ENABLED:
            return
        now = time.time() if now is None else now
        self.reserve(keys, job_id)
        for _kind, key in keys:
            self.store.record_hit(key, now, settings.RATE_LIMIT_WINDOW)

    def release(self, keys, job_id):
        for _kind, key in keys:
            self.store.drop_active(key, {job_id})
