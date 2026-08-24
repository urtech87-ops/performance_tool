#!/usr/bin/env python3
"""
Domain-ownership gate
=====================
A single-page look at one URL is cheap and stays open to everyone. Crawling a
whole domain is not, so when the gate is on (REQUIRE_DOMAIN_VERIFICATION=1) a
multi-page scan first has to prove the requester controls the domain, by
publishing a token either way:

  meta tag  <meta name="site-audit-verification" content="TOKEN">  on the homepage
  DNS TXT   site-audit-verification=TOKEN                          on the domain

The token is an HMAC of the domain, so it is stable, unguessable, and needs no
storage to hand out. Successful checks are cached (in Redis when available) for
VERIFY_TTL so a verified domain is not re-fetched on every scan.

`credential_gate` is the second, stricter gate built on the same token: where
`REQUIRE_VERIFIED_DOMAIN_FOR_AUTH` is on - which `DEPLOYMENT_MODE=public`
forces - a scan carrying HTTP auth, a cookie jar or a form login needs the
same proof, whatever its size. The first gate is about what a crawl costs this
deployment; this one is about whose password is being handed over, which is
why one page is gated exactly like twenty.

Off by default: an internal/local deployment leaves both gates closed and
nothing in here ever runs.
"""

import hashlib
import hmac
import re
import shutil
import subprocess
import time
import urllib.request

from config import settings

META_RE_TEMPLATE = (r"<meta[^>]+name=[\"']{name}[\"'][^>]*content=[\"']([^\"']+)[\"']"
                    r"|<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*name=[\"']{name}[\"']")

MAX_HTML_BYTES = 512 * 1024        # a homepage's <head> is far inside this


def token_for(domain):
    """The token this deployment expects on `domain`."""
    domain = (domain or "").lower().strip().strip(".")
    digest = hmac.new(settings.VERIFY_TOKEN_SECRET.encode(),
                      domain.encode(), hashlib.sha256).hexdigest()
    return f"sav-{digest[:40]}"


def instructions(domain):
    """Everything the UI needs to tell a user how to verify."""
    token = token_for(domain)
    return {
        "domain": domain,
        "token": token,
        "meta_tag": f'<meta name="{settings.VERIFY_META_NAME}" content="{token}">',
        "dns_record": f'{settings.VERIFY_META_NAME}={token}',
        "how": ("Add either the meta tag to your homepage's <head>, or the TXT "
                "record to your domain's DNS, then run the scan again."),
    }


# --------------------------------------------------------------------------
# the two checks
# --------------------------------------------------------------------------

def _fetch(url, timeout):
    req = urllib.request.Request(url, headers={
        "User-Agent": "site-audit-verifier/1.0",
        "Accept": "text/html,application/xhtml+xml",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310
        return resp.read(MAX_HTML_BYTES).decode("utf-8", "replace")


def check_meta_tag(domain, token, timeout=None):
    """True when the homepage carries the token in its verification meta tag."""
    timeout = timeout or settings.VERIFY_HTTP_TIMEOUT
    pattern = re.compile(
        META_RE_TEMPLATE.format(name=re.escape(settings.VERIFY_META_NAME)),
        re.IGNORECASE)
    for url in (f"https://{domain}/", f"https://www.{domain}/", f"http://{domain}/"):
        try:
            html = _fetch(url, timeout)
        except Exception:                                          # noqa: BLE001
            continue
        for match in pattern.finditer(html):
            found = match.group(1) or match.group(2) or ""
            if hmac.compare_digest(found.strip(), token):
                return True
    return False


def _txt_records(domain, timeout):
    """TXT records for a domain, via dnspython when installed, else `dig`."""
    try:
        import dns.resolver                                        # noqa: PLC0415

        resolver = dns.resolver.Resolver()
        resolver.lifetime = timeout
        answers = resolver.resolve(domain, "TXT")
        return ["".join(part.decode() if isinstance(part, bytes) else str(part)
                        for part in record.strings) for record in answers]
    except ImportError:
        pass
    except Exception:                                              # noqa: BLE001
        return []
    dig = shutil.which("dig")
    if not dig:
        return []
    try:
        out = subprocess.run([dig, "+short", "TXT", domain], capture_output=True,
                             text=True, timeout=timeout, check=False).stdout
    except Exception:                                              # noqa: BLE001
        return []
    return [line.strip().strip('"') for line in out.splitlines() if line.strip()]


def check_dns_txt(domain, token, timeout=None):
    """True when a TXT record on the domain carries the token."""
    timeout = timeout or settings.VERIFY_HTTP_TIMEOUT
    expected = f"{settings.VERIFY_META_NAME}={token}"
    for record in _txt_records(domain, timeout):
        value = record.strip().strip('"')
        if hmac.compare_digest(value, expected) or hmac.compare_digest(value, token):
            return True
    return False


# --------------------------------------------------------------------------
# cache + public entry point
# --------------------------------------------------------------------------

class MemoryVerificationCache:
    def __init__(self):
        self._seen = {}

    def get(self, domain):
        expires = self._seen.get(domain)
        if expires and expires > time.time():
            return True
        self._seen.pop(domain, None)
        return False

    def set(self, domain, ttl):
        self._seen[domain] = time.time() + ttl


class RedisVerificationCache:
    def __init__(self, redis_conn):
        self.r = redis_conn

    def get(self, domain):
        return bool(self.r.exists(f"verified:{domain}"))

    def set(self, domain, ttl):
        self.r.setex(f"verified:{domain}", int(ttl), "1")


_cache = MemoryVerificationCache()


def set_cache(cache):
    global _cache
    _cache = cache
    return cache


def is_verified(domain, use_cache=True):
    """Has this domain proved ownership, by either method?"""
    domain = (domain or "").lower().strip().strip(".")
    if not domain:
        return False
    if use_cache and _cache.get(domain):
        return True
    token = token_for(domain)
    if check_dns_txt(domain, token) or check_meta_tag(domain, token):
        _cache.set(domain, settings.VERIFY_TTL)
        return True
    return False


def gate(domain, multipage):
    """Decide whether a scan may proceed.

    Returns (allowed, detail). `detail` is the instructions payload when the
    scan is blocked, so the caller can show the user exactly what to publish.
    """
    if not settings.REQUIRE_DOMAIN_VERIFICATION or not multipage:
        return True, None
    if is_verified(domain):
        return True, None
    detail = instructions(domain)
    detail["message"] = (
        f"A multi-page audit of {domain} needs proof you control the domain. "
        f"{detail['how']} A single-page scan (Max pages = 1) runs without it.")
    return False, detail


def credential_gate(domain, credentialed):
    """Decide whether a scan carrying *credentials* may proceed.

    A separate gate from `gate` above, and a stricter one: that one is about
    what a crawl costs this deployment, this one is about who is being handed
    somebody's password. Size does not enter into it, so a single-page scan
    with a cookie jar is gated exactly like a 20-page one.

    On in public mode, off by default locally, where the person scanning and
    the person who owns the site are the same person.

    Returns (allowed, detail), the same shape as `gate`.
    """
    if not settings.REQUIRE_VERIFIED_DOMAIN_FOR_AUTH or not credentialed:
        return True, None
    if is_verified(domain):
        return True, None
    detail = instructions(domain)
    detail["message"] = (
        f"Signing in to {domain} needs proof you control the domain: this "
        f"deployment does not take credentials for a site on a stranger's "
        f"say-so. {detail['how']} A scan without sign-in details runs without "
        f"it.")
    return False, detail
