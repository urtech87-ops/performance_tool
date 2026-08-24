#!/usr/bin/env python3
"""
Serving-layer configuration
===========================
Every knob the public-facing deployment needs, read once from the environment.

Nothing here touches the scan itself: `run_pipeline`, the scoring and the
report builders are unchanged. These settings only govern *how many* scans may
run, *for whom*, and *how big* a scan a client is allowed to ask for.

`DEPLOYMENT_MODE` is the one setting that decides the security posture: every
other flag that could open the deployment up is derived from it rather than
trusted on its own. See the block below the imports.

Tests monkeypatch attributes on the module-level `settings` object; a process
can also call `reload()` after changing the environment.
"""

import os
import secrets
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

# --------------------------------------------------------------------------
# deployment mode
# --------------------------------------------------------------------------
# One knob an operator has to get right, instead of five.
#
#   local   (default) - one person, one machine, trusted network. Everything
#                       convenient stays on: DNS overrides, credentials
#                       straight onto the queue, no ownership proof.
#   public            - the deployment takes scans from strangers. The
#                       settings that turn this tool into someone else's
#                       network probe are forced off here, whatever the
#                       environment says.
#
# Anything set but unrecognised reads as `public`: a typo in the one variable
# that decides the posture must not silently buy the loose one.
MODE_LOCAL = "local"
MODE_PUBLIC = "public"


def _mode(raw):
    text = (raw or "").strip().lower()
    if not text:
        return MODE_LOCAL
    return MODE_LOCAL if text == MODE_LOCAL else MODE_PUBLIC


# Where a generated verification secret is persisted, so the token a user is
# asked to publish survives a restart (and is identical in web + worker).
SECRET_FILE = APP_DIR / ".verify_secret"


def _int(raw, default, lo=1, hi=10 ** 9):
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def _bool(raw, default=False):
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _verify_secret(env):
    """A stable HMAC secret for verification tokens.

    Explicit env wins. Otherwise reuse (or create once) a file next to the app
    so web processes and workers agree on the token for a domain.
    """
    given = (env.get("VERIFY_TOKEN_SECRET") or "").strip()
    if given:
        return given
    try:
        if SECRET_FILE.exists():
            stored = SECRET_FILE.read_text(encoding="utf-8").strip()
            if stored:
                return stored
        generated = secrets.token_urlsafe(32)
        SECRET_FILE.write_text(generated, encoding="utf-8")
        try:
            SECRET_FILE.chmod(0o600)
        except OSError:
            pass
        return generated
    except OSError:
        # Read-only filesystem: fall back to a per-process secret. Tokens then
        # change on restart, so set VERIFY_TOKEN_SECRET in that deployment.
        return secrets.token_urlsafe(32)


class Settings:
    """A snapshot of the environment. Attributes are plain values so a test can
    override one without re-reading anything."""

    def __init__(self, env=None):
        env = os.environ if env is None else env

        # ---- deployment mode -------------------------------------------
        # The single source of truth for the security posture. Every setting
        # below that reads `public` is *forced*, not defaulted: an environment
        # that also sets the individual variable does not get its way, so an
        # operator cannot half-harden a public deployment by getting one of
        # five booleans wrong.
        self.DEPLOYMENT_MODE = _mode(env.get("DEPLOYMENT_MODE"))
        public = self.DEPLOYMENT_MODE == MODE_PUBLIC

        # ---- queue / workers -------------------------------------------
        # Empty REDIS_URL keeps the single-process in-memory queue (local dev
        # and the test suite). Public deployments set it and run real workers.
        self.REDIS_URL = (env.get("REDIS_URL") or "").strip()
        self.QUEUE_NAME = (env.get("QUEUE_NAME") or "scans").strip() or "scans"
        self.WORKER_CONCURRENCY = _int(env.get("WORKER_CONCURRENCY"), 2, 1, 64)
        self.MAX_QUEUE_DEPTH = _int(env.get("MAX_QUEUE_DEPTH"), 20, 1, 10_000)
        self.SCAN_TIMEOUT = _int(env.get("SCAN_TIMEOUT"), 900, 30, 86_400)
        self.JOB_TTL = _int(env.get("JOB_TTL"), 86_400, 60, 30 * 86_400)

        # ---- resilience: what one page, and one run, may cost -----------
        # PAGE_TIMEOUT bounds a single page; PAGE_RETRIES is how many extra
        # attempts a *transient* page failure gets (a clear block is never
        # retried). RUN_BUDGET is the pipeline's own ceiling: it finalizes the
        # run as 'partial' with whatever completed, which is why it sits below
        # SCAN_TIMEOUT - the queue's hard kill, which leaves no report at all.
        self.PAGE_TIMEOUT = _int(env.get("PAGE_TIMEOUT"), 150, 10, 3_600)
        self.PAGE_RETRIES = _int(env.get("PAGE_RETRIES"), 1, 0, 3)
        self.RUN_BUDGET = _int(env.get("RUN_BUDGET"),
                               max(30, int(self.SCAN_TIMEOUT * 0.85)),
                               30, 86_400)
        self.PREFLIGHT_TIMEOUT = _int(env.get("PREFLIGHT_TIMEOUT"), 10, 1, 120)

        # ---- per-client guardrails -------------------------------------
        self.RATE_LIMIT_ENABLED = _bool(env.get("RATE_LIMIT_ENABLED"), True)
        self.MAX_SCANS_PER_HOUR = _int(env.get("MAX_SCANS_PER_HOUR"), 5, 1, 10_000)
        self.MAX_CONCURRENT_SCANS = _int(env.get("MAX_CONCURRENT_SCANS"), 1, 1, 100)
        self.RATE_LIMIT_WINDOW = _int(env.get("RATE_LIMIT_WINDOW"), 3600, 60, 86_400)
        self.TRUST_PROXY = _bool(env.get("TRUST_PROXY"), False)

        # ---- hard caps on what a scan may ask for ----------------------
        # Enforced server-side on every request, whatever the client sends.
        self.CAP_MAX_PAGES = _int(env.get("CAP_MAX_PAGES"), 20, 1, 200)
        self.CAP_SAMPLES = _int(env.get("CAP_SAMPLES"), 2, 1, 5)
        self.CAP_PARALLEL = _int(env.get("CAP_PARALLEL"), 3, 1, 6)

        # ---- authenticated scanning ------------------------------------
        # Credentials are held in memory for one scan and never written
        # anywhere (see scanauth.py). A deployment that would rather not be
        # handed third-party passwords at all can refuse them outright.
        self.ALLOW_SCAN_AUTH = _bool(env.get("ALLOW_SCAN_AUTH"), True)

        # Whether a credential may be handed to a Redis that might write it to
        # disk. With the Redis queue a credential spends the queue wait in its
        # own short-TTL key; a Redis with RDB/AOF on can flush that key to disk
        # before the worker takes it. Locally that is the operator's own Redis
        # and their own password, so this stays on - today's behaviour. In
        # public mode it is off, and the queue then refuses credentials unless
        # it can *prove* the server has persistence disabled.
        self.PERSIST_SCAN_AUTH = (not public
                                  and _bool(env.get("PERSIST_SCAN_AUTH"), True))

        # Whether a scan may be handed credentials for a domain nobody has
        # proved they own. Somebody else's password aimed at somebody else's
        # site is the one request a deployment open to strangers should never
        # take on trust, so public mode requires ownership first - for any
        # scan carrying HTTP auth, a cookie jar or a form login, single-page
        # or not.
        self.REQUIRE_VERIFIED_DOMAIN_FOR_AUTH = public or _bool(
            env.get("REQUIRE_VERIFIED_DOMAIN_FOR_AUTH"), False)

        # ---- DNS override ----------------------------------------------
        # Pinning a hostname to an address is what lets a staging server be
        # measured before its DNS is switched. It also lets a request be
        # pointed at an address the requester chose, which on a public
        # deployment is a way to reach hosts the internet cannot - so public
        # mode refuses it outright, exactly as it refuses to persist a
        # credential.
        self.ALLOW_DNS_OVERRIDE = (not public
                                   and _bool(env.get("ALLOW_DNS_OVERRIDE"), True))

        # Even where the override is offered, an address inside the ranges
        # that only mean "this machine" or "this network" - RFC1918, loopback,
        # link-local and the cloud metadata endpoint that lives in it - is
        # refused: that is the SSRF the feature would otherwise be. Pointing a
        # scan at a staging box on 10.x is a real thing to want, so there is
        # one escape hatch, and it is never available in public mode.
        self.ALLOW_PRIVATE_DNS_TARGETS = (
            not public and _bool(env.get("ALLOW_PRIVATE_DNS_TARGETS"), False))

        # ---- domain-ownership gate for deep multi-page scans -----------
        self.REQUIRE_DOMAIN_VERIFICATION = _bool(
            env.get("REQUIRE_DOMAIN_VERIFICATION"), False)
        self.VERIFY_TOKEN_SECRET = _verify_secret(env)
        self.VERIFY_META_NAME = (env.get("VERIFY_META_NAME")
                                 or "site-audit-verification").strip()
        self.VERIFY_TTL = _int(env.get("VERIFY_TTL"), 86_400, 60, 30 * 86_400)
        self.VERIFY_HTTP_TIMEOUT = _int(env.get("VERIFY_HTTP_TIMEOUT"), 10, 1, 120)


settings = Settings()


def reload(env=None):
    """Rebuild `settings` in place from the environment."""
    fresh = Settings(env)
    settings.__dict__.update(fresh.__dict__)
    return settings
