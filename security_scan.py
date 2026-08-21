#!/usr/bin/env python3
"""
Passive web security scanner (standalone module).

Safe, non-intrusive checks only - it reads normal HTTP responses and the TLS
certificate; it does NOT probe for files, fuzz, or attack anything. For each
discovered page it inspects security headers, cookie flags, and mixed content,
plus a few site-level checks (HTTPS redirect, certificate chain/expiry,
security.txt). Findings are grouped by severity with a concrete fix for each.

Used by dashboard.py; also runnable directly:
    python security_scan.py https://example.com -o security.html
"""

import argparse
import html
import re
import socket
import ssl
import sys
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

SEVERITY_ORDER = ["Critical", "High", "Medium", "Low", "Info"]

# rule_id -> (title, severity, recommendation)
RULES = {
    "hsts": ("Missing HTTP Strict Transport Security (HSTS)", "High",
             "Add 'Strict-Transport-Security: max-age=31536000; includeSubDomains; preload' on HTTPS responses so browsers refuse to connect over plain HTTP."),
    "csp": ("Missing Content-Security-Policy", "High",
            "Define a Content-Security-Policy to control which scripts, styles and frames may load. Start in report-only mode, then enforce a restrictive policy (e.g. default-src 'self')."),
    "xfo": ("No clickjacking protection", "Medium",
            "Add 'X-Frame-Options: DENY' (or a CSP 'frame-ancestors' directive) to stop the page being embedded in a malicious iframe."),
    "nosniff": ("Missing X-Content-Type-Options", "Medium",
                "Add 'X-Content-Type-Options: nosniff' so browsers don't MIME-sniff responses into an unexpected content type."),
    "referrer": ("Missing Referrer-Policy", "Low",
                 "Add 'Referrer-Policy: strict-origin-when-cross-origin' to avoid leaking full URLs to third-party sites."),
    "permissions": ("Missing Permissions-Policy", "Low",
                    "Add a 'Permissions-Policy' header to disable powerful browser features (camera, geolocation, etc.) you don't use."),
    "server_banner": ("Server version disclosure", "Low",
                      "The 'Server' header reveals software and version, aiding targeted attacks. Suppress the version or the header at the web-server/proxy level."),
    "powered_by": ("Technology disclosure (X-Powered-By)", "Low",
                   "Remove the 'X-Powered-By' (and similar) headers so the backend framework/version isn't advertised."),
    "cors_wildcard": ("Permissive CORS (Access-Control-Allow-Origin: *)", "Medium",
                      "A wildcard CORS origin lets any site read responses. Restrict it to an explicit allow-list of trusted origins, and never combine '*' with credentials."),
    "cookie_secure": ("Cookie without Secure flag", "High",
                      "Set the 'Secure' attribute on cookies so they are only sent over HTTPS and cannot be intercepted over plain HTTP."),
    "cookie_httponly": ("Cookie without HttpOnly flag", "Medium",
                        "Set 'HttpOnly' on session/auth cookies so client-side JavaScript (and XSS) cannot read them."),
    "cookie_samesite": ("Cookie without SameSite attribute", "Low",
                        "Set 'SameSite=Lax' (or 'Strict') on cookies to reduce cross-site request forgery (CSRF) exposure."),
    "mixed_content": ("Mixed content on HTTPS page", "Medium",
                      "The HTTPS page references sub-resources over plain http://. Load every script, style, image and iframe over https:// (or protocol-relative) to avoid downgrade/injection."),
    "no_https_redirect": ("HTTP not redirected to HTTPS", "High",
                          "Configure the server to 301-redirect all http:// traffic to https://, and pair it with HSTS."),
    "cert_expired": ("TLS certificate expired", "Critical",
                     "The certificate is no longer valid - browsers will block the site. Renew and reinstall the certificate immediately."),
    "cert_expiring": ("TLS certificate expiring soon", "High",
                      "Renew the certificate before it lapses to avoid an outage; automate renewal (e.g. ACME/Let's Encrypt) where possible."),
    "cert_chain": ("Incomplete TLS certificate chain", "Medium",
                   "The server doesn't send the full intermediate chain, so stricter clients reject it (browsers often mask this). Install the intermediate certificate(s) alongside the leaf."),
    "cert_selfsigned": ("Self-signed / untrusted certificate", "High",
                        "Replace the self-signed certificate with one issued by a trusted CA."),
    "no_securitytxt": ("No security.txt", "Info",
                       "Consider publishing /.well-known/security.txt with a contact for vulnerability reports (RFC 9116)."),
}


def _fetch(url, timeout=15):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "PassiveSecurityScan/1.0"})
    resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
    raw = resp.read(400_000)
    try:
        body = raw.decode("utf-8", "replace")
    except Exception:
        body = ""
    return resp, body


def analyze_response(url, headers, set_cookies, body):
    """Pure analysis: returns a list of rule_ids that fired for this URL.
    `headers` is a case-insensitive-ish dict (use .get with lowercase);
    `set_cookies` is a list of Set-Cookie strings; `body` is response text.
    """
    def h(name):
        return headers.get(name.lower())

    is_https = url.lower().startswith("https")
    fired = []

    if is_https and not h("strict-transport-security"):
        fired.append("hsts")
    csp = h("content-security-policy")
    if not csp:
        fired.append("csp")
    if not h("x-frame-options") and not (csp and "frame-ancestors" in csp.lower()):
        fired.append("xfo")
    xcto = h("x-content-type-options")
    if not xcto or "nosniff" not in xcto.lower():
        fired.append("nosniff")
    if not h("referrer-policy"):
        fired.append("referrer")
    if not h("permissions-policy"):
        fired.append("permissions")

    server = h("server")
    if server and re.search(r"\d+\.\d+", server):
        fired.append("server_banner")
    if h("x-powered-by") or h("x-aspnet-version") or h("x-aspnetmvc-version"):
        fired.append("powered_by")

    acao = h("access-control-allow-origin")
    if acao and acao.strip() == "*":
        fired.append("cors_wildcard")

    for c in set_cookies or []:
        low = c.lower()
        if is_https and "secure" not in low:
            fired.append("cookie_secure")
        if "httponly" not in low:
            fired.append("cookie_httponly")
        if "samesite" not in low:
            fired.append("cookie_samesite")

    if is_https and body:
        if re.search(r"""(?:src|href|action)\s*=\s*["']http://""", body, re.I):
            fired.append("mixed_content")

    return fired


def _headers_dict(resp):
    d = {}
    for k, v in resp.headers.items():
        d[k.lower()] = v
    return d


def _cookies(resp):
    try:
        return resp.headers.get_all("Set-Cookie") or []
    except Exception:
        c = resp.headers.get("Set-Cookie")
        return [c] if c else []


def check_certificate(host, port=443, timeout=12):
    """Returns list of rule_ids for TLS cert issues, plus an info note string."""
    fired, note = [], ""
    # 1) verified handshake to detect expiry / chain / self-signed
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                cert = ss.getpeercert()
                exp = cert.get("notAfter")
                if exp:
                    expdt = datetime.strptime(exp, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
                    days = (expdt - datetime.now(timezone.utc)).days
                    note = f"Certificate valid, expires in {days} day(s)."
                    if days < 0:
                        fired.append("cert_expired")
                    elif days < 15:
                        fired.append("cert_expiring")
    except ssl.SSLCertVerificationError as e:
        msg = str(e).lower()
        if "expired" in msg:
            fired.append("cert_expired")
        elif "self signed" in msg or "self-signed" in msg:
            fired.append("cert_selfsigned")
        else:
            # unable to get local issuer / leaf signature -> incomplete chain
            fired.append("cert_chain")
        note = f"Certificate verification failed: {e}"
    except Exception as e:
        note = f"Could not check certificate: {e}"
    return fired, note


def check_https_redirect(host, timeout=12):
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(f"http://{host}/", headers={"User-Agent": "PassiveSecurityScan/1.0"})
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        return not resp.geturl().lower().startswith("https")  # True => NOT redirected
    except Exception:
        return False  # can't reach http => don't flag


def check_securitytxt(host, timeout=12):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for path in ("/.well-known/security.txt", "/security.txt"):
        try:
            req = urllib.request.Request(f"https://{host}{path}", headers={"User-Agent": "PassiveSecurityScan/1.0"})
            r = urllib.request.urlopen(req, timeout=timeout, context=ctx)
            if r.status == 200:
                return False  # exists
        except Exception:
            continue
    return True  # missing


def collect_site(site_url, urls, log=print):
    """Fetch each URL, run all checks, and return the findings as data.

    Identical work to what `scan_site` has always done; it stops one step
    earlier so the consolidated report can render the same findings. Nothing
    about the checks themselves changed."""
    host = urlparse(site_url).netloc
    # rule_id -> set of affected page urls (for per-page rules) or {"__site__"}
    hits = {}
    scanned = 0

    def add(rule_id, where):
        hits.setdefault(rule_id, set()).add(where)

    log(f"Security: analyzing {len(urls)} page(s)...")
    for u in urls:
        try:
            resp, body = _fetch(u)
        except Exception as e:
            log(f"  security: skip {u} ({e})")
            continue
        scanned += 1
        for rid in analyze_response(u, _headers_dict(resp), _cookies(resp), body):
            add(rid, u)

    # site-level checks (once)
    log("Security: checking TLS certificate and redirects...")
    cert_fired, cert_note = check_certificate(host)
    for rid in cert_fired:
        add(rid, "__site__")
    if check_https_redirect(host):
        add("no_https_redirect", "__site__")
    if check_securitytxt(host):
        add("no_securitytxt", "__site__")

    return {"site_url": site_url, "hits": hits, "scanned": scanned,
            "total": len(urls), "cert_note": cert_note}


def html_from(data):
    """The standalone security page for one `collect_site` result."""
    return build_security_html(data.get("site_url", ""), data.get("hits") or {},
                               data.get("scanned", 0), data.get("total", 0),
                               data.get("cert_note", ""))


def scan_site(site_url, urls, log=print):
    """Fetch each URL, run all checks, and return the security report HTML."""
    return html_from(collect_site(site_url, urls, log=log))


def _sev_class(sev):
    return {"Critical": "crit", "High": "high", "Medium": "med", "Low": "low", "Info": "info"}[sev]


def build_security_html(site_url, hits, scanned, total, cert_note=""):
    gen = datetime.now().strftime("%Y-%m-%d %H:%M")
    # bucket findings by severity
    by_sev = {s: [] for s in SEVERITY_ORDER}
    for rid, where in hits.items():
        if rid not in RULES:
            continue
        title, sev, rec = RULES[rid]
        site_wide = ("__site__" in where)
        pages = sorted(w for w in where if w != "__site__")
        by_sev[sev].append((title, rec, site_wide, pages))

    counts = {s: len(by_sev[s]) for s in SEVERITY_ORDER}

    def scope_text(site_wide, pages):
        if site_wide and not pages:
            return "Site-wide"
        n = len(pages)
        if n == 0:
            return "Site-wide"
        shown = "".join(f"<li>{html.escape(urlparse(p).path or '/')}</li>" for p in pages[:8])
        more = f"<li>+{n - 8} more</li>" if n > 8 else ""
        label = "all scanned pages" if n >= scanned and scanned else f"{n} of {scanned} pages"
        return f'Affects {label}<ul class="pages">{shown}{more}</ul>'

    cards = []
    for sev in SEVERITY_ORDER:
        for title, rec, site_wide, pages in by_sev[sev]:
            cards.append(
                f'<div class="finding {_sev_class(sev)}">'
                f'<div class="fh"><span class="sev {_sev_class(sev)}">{sev}</span>'
                f'<span class="ft">{html.escape(title)}</span></div>'
                f'<div class="scope">{scope_text(site_wide, pages)}</div>'
                f'<div class="rec"><b>Fix:</b> {html.escape(rec)}</div></div>')
    findings_html = "\n".join(cards) or '<p class="clean">No issues detected by the passive checks.</p>'

    stat_cards = "".join(
        f'<div class="stat {_sev_class(s)}"><b>{counts[s]}</b><span>{s}</span></div>'
        for s in SEVERITY_ORDER)

    note_html = f'<div class="meta">{html.escape(cert_note)}</div>' if cert_note else ""

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Security Report - {html.escape(site_url)}</title>
<style>
  :root {{ --ink:#1c2430; --line:#e3e6ea; --bg:#f6f7f9; --card:#fff;
           --crit:#b3122b; --high:#d6521f; --med:#b06f00; --low:#3b7dbf; --info:#5b6672; }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; margin:0; background:var(--bg); color:var(--ink); line-height:1.5; }}
  .wrap {{ max-width:1000px; margin:0 auto; padding:32px 20px 64px; }}
  h1 {{ font-size:23px; margin:0 0 4px; }}
  .meta {{ color:#5b6672; font-size:13px; margin-bottom:16px; }}
  .stats {{ display:flex; gap:12px; margin:18px 0 28px; flex-wrap:wrap; }}
  .stat {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 16px; min-width:96px; text-align:center; }}
  .stat b {{ display:block; font-size:24px; }}
  .stat span {{ font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:#5b6672; }}
  .stat.crit b {{ color:var(--crit); }} .stat.high b {{ color:var(--high); }}
  .stat.med b {{ color:var(--med); }} .stat.low b {{ color:var(--low); }} .stat.info b {{ color:var(--info); }}
  .finding {{ background:var(--card); border:1px solid var(--line); border-left:5px solid var(--line); border-radius:10px; padding:14px 18px; margin:12px 0; }}
  .finding.crit {{ border-left-color:var(--crit); }} .finding.high {{ border-left-color:var(--high); }}
  .finding.med {{ border-left-color:var(--med); }} .finding.low {{ border-left-color:var(--low); }} .finding.info {{ border-left-color:var(--info); }}
  .fh {{ display:flex; align-items:center; gap:10px; }}
  .sev {{ font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.04em; padding:2px 9px; border-radius:20px; color:#fff; }}
  .sev.crit {{ background:var(--crit); }} .sev.high {{ background:var(--high); }}
  .sev.med {{ background:var(--med); }} .sev.low {{ background:var(--low); }} .sev.info {{ background:var(--info); }}
  .ft {{ font-weight:600; font-size:15px; }}
  .scope {{ font-size:13px; color:#5b6672; margin:8px 0; }}
  .pages {{ margin:6px 0 0; padding-left:18px; font-size:12px; color:#5b6672; }}
  .rec {{ font-size:14px; margin-top:6px; }}
  .clean {{ color:#0a7d34; font-size:15px; }}
</style></head>
<body><div class="wrap">
  <h1>Security Report</h1>
  <div class="meta">{html.escape(site_url)} &middot; {scanned} of {total} page(s) analyzed &middot; Generated {gen}</div>
  {note_html}
  <div class="stats">{stat_cards}</div>
  {findings_html}
  <p class="meta" style="margin-top:24px">Passive checks only (headers, cookies, TLS, redirects, mixed content). This is a first-pass triage, not a substitute for a full penetration test.</p>
</div></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("site")
    ap.add_argument("-o", "--output", default="security.html")
    args = ap.parse_args()
    site = args.site if urlparse(args.site).scheme else "https://" + args.site
    html_out = scan_site(site, [site])
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html_out)
    print(f"Security report -> {args.output}")


if __name__ == "__main__":
    main()