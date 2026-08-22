#!/usr/bin/env python3
"""
Serving-layer configuration
===========================
Every knob the public-facing deployment needs, read once from the environment.

Nothing here touches the scan itself: `run_pipeline`, the scoring and the
report builders are unchanged. These settings only govern *how many* scans may
run, *for whom*, and *how big* a scan a client is allowed to ask for.

Tests monkeypatch attributes on the module-level `settings` object; a process
can also call `reload()` after changing the environment.
"""

import os
import secrets
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

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

        # ---- queue / workers -------------------------------------------
        # Empty REDIS_URL keeps the single-process in-memory queue (local dev
        # and the test suite). Public deployments set it and run real workers.
        self.REDIS_URL = (env.get("REDIS_URL") or "").strip()
        self.QUEUE_NAME = (env.get("QUEUE_NAME") or "scans").strip() or "scans"
        self.WORKER_CONCURRENCY = _int(env.get("WORKER_CONCURRENCY"), 2, 1, 64)
        self.MAX_QUEUE_DEPTH = _int(env.get("MAX_QUEUE_DEPTH"), 20, 1, 10_000)
        self.SCAN_TIMEOUT = _int(env.get("SCAN_TIMEOUT"), 900, 30, 86_400)
        self.JOB_TTL = _int(env.get("JOB_TTL"), 86_400, 60, 30 * 86_400)

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
