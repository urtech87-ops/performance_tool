#!/usr/bin/env python3
"""
Scan configuration - what the browser looks and feels like
==========================================================
Connection throttling, device emulation, viewport, User-Agent, a URL block
list and a DNS override: the knobs that decide *under what conditions* a page
is measured. Nothing here is secret; credentials live in `scanauth.py` and
never pass through this module.

Two rules govern everything below:

  * **Nothing is scored here.** These values only build command-line flags for
    `lighthouse` and `unlighthouse-ci`. The scoring, the metrics and the report
    builders are untouched - they read whatever those tools produce.
  * **The default is "exactly what the tool did before".** `ScanConfig()` with
    no arguments emits the same flags the pipeline has always emitted
    (`--preset=desktop` for desktop, nothing for mobile, `--throttle` on the
    crawl), so an unconfigured scan is byte-identical to yesterday's.

Every value a client sends is clamped here, the same way `guardrails` clamps
page counts: a viewport is bounded, a User-Agent is stripped of anything that
could inject a second HTTP header, a block pattern cannot carry whitespace, a
DNS override must parse as a real hostname and a real IP address, and an
unknown preset id falls back to the default rather than raising.
"""

import ipaddress
import re
from collections import OrderedDict, namedtuple
from urllib.parse import urlparse

# --------------------------------------------------------------------------
# connection throttling
# --------------------------------------------------------------------------

# Lighthouse offers three throttling methods:
#   simulate  - the default; Lighthouse's own lantern model, applied after a
#               fast load. This is what the tool has always used.
#   devtools  - real packet-level shaping by Chrome, which is what a
#               "connection preset" means to anyone who has used GTmetrix.
#   provided  - no throttling at all; whatever the machine's link really is.
#
# The presets below therefore use `devtools`, with the CPU left alone: they
# emulate a *connection*, not a slower phone. Device emulation is a separate
# control.
Throttle = namedtuple("Throttle", "id label method down up latency cpu")

THROTTLING = OrderedDict((t.id, t) for t in [
    # The default keeps Lighthouse's own behaviour, flags and all.
    Throttle("default", "Lighthouse default (simulated Slow 4G)", "", 0, 0, 0, 0),
    Throttle("none", "Unthrottled (this machine's connection)", "provided", 0, 0, 0, 1),
    Throttle("broadband_fast", "Broadband Fast - 100/50 Mbps, 5 ms",
             "devtools", 100_000, 50_000, 5, 1),
    Throttle("broadband", "Broadband - 20/10 Mbps, 20 ms",
             "devtools", 20_000, 10_000, 20, 1),
    Throttle("lte", "LTE - 12/12 Mbps, 70 ms", "devtools", 12_000, 12_000, 70, 1),
    Throttle("4g", "4G - 9/9 Mbps, 85 ms", "devtools", 9_000, 9_000, 85, 1),
    Throttle("3g", "3G - 1.6/0.768 Mbps, 300 ms", "devtools", 1_600, 768, 300, 1),
    # Filled in from the request; the numbers below are only its placeholders.
    Throttle("custom", "Custom", "devtools", 5_000, 1_000, 100, 1),
])

DEFAULT_THROTTLING = "default"

# Bounds on a custom connection. Wide enough to describe any real link, narrow
# enough that a client cannot ask Chrome for something absurd.
MIN_KBPS, MAX_KBPS = 1, 1_000_000
MAX_LATENCY_MS = 10_000

# --------------------------------------------------------------------------
# device emulation
# --------------------------------------------------------------------------

Device = namedtuple("Device", "id label family width height dpr user_agent")

_IOS_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
           "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 "
           "Safari/604.1")


def _android_ua(model):
    return (f"Mozilla/5.0 (Linux; Android 14; {model}) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36")


# `desktop` and `mobile` reproduce Lighthouse's own two form factors, so
# picking them changes nothing about how the tool has always run. The named
# handsets are CSS viewport sizes as the device reports them.
DEVICES = OrderedDict((d.id, d) for d in [
    Device("desktop", "Desktop", "desktop", 0, 0, 0, ""),
    Device("mobile", "Mobile (Lighthouse default)", "mobile", 0, 0, 0, ""),
    Device("iphone_se", "iPhone SE", "mobile", 375, 667, 2, _IOS_UA),
    Device("iphone_12", "iPhone 12 / 13 / 14", "mobile", 390, 844, 3, _IOS_UA),
    Device("iphone_14_pro", "iPhone 14 Pro / 15 / 16", "mobile", 393, 852, 3, _IOS_UA),
    Device("iphone_15_pro_max", "iPhone 15 Pro Max", "mobile", 430, 932, 3, _IOS_UA),
    Device("pixel_5", "Pixel 5", "mobile", 393, 851, 2.75, _android_ua("Pixel 5")),
    Device("pixel_7", "Pixel 7", "mobile", 412, 915, 2.625, _android_ua("Pixel 7")),
    Device("pixel_8_pro", "Pixel 8 Pro", "mobile", 448, 998, 3, _android_ua("Pixel 8 Pro")),
])

MIN_VIEWPORT, MAX_VIEWPORT = 200, 3840
MIN_DPR, MAX_DPR = 0.5, 5.0
MAX_UA_LENGTH = 512

# A User-Agent is written into an HTTP header. Anything that could end that
# header early - or start a second one - is removed rather than rejected, so a
# stray newline pasted from a terminal does not fail the scan.
_UA_STRIP = re.compile(r"[\r\n\x00-\x1f\x7f]")


def clean_user_agent(raw):
    """A User-Agent safe to put on a command line and in a header."""
    ua = _UA_STRIP.sub(" ", str(raw or "")).strip()
    return ua[:MAX_UA_LENGTH]


# --------------------------------------------------------------------------
# blocked requests
# --------------------------------------------------------------------------
# A block list is a list of URL patterns - `*doubleclick.net*`, `*/ads/*`,
# `*.gif` - that must not load while the page is measured. Chrome enforces it
# for the Lighthouse audits (`--blocked-url-patterns`, which is
# Network.setBlockedURLs underneath) and the Playwright runners enforce it with
# request interception; both abort the request rather than let it complete, so
# a blocked third party costs the page nothing at all.
#
# The pattern language is deliberately the one Lighthouse already speaks: a
# plain substring, with `*` standing for "any run of characters". `ads` and
# `*ads*` therefore mean the same thing, which is what a person typing a domain
# into the box expects.

MAX_BLOCK_PATTERNS = 40
MAX_PATTERN_LENGTH = 200

# A pattern is matched against a URL, which has no whitespace in it, and is put
# on a command line - so whitespace and control characters are removed rather
# than quoted.
_PATTERN_STRIP = re.compile(r"[\s\x00-\x1f\x7f]")
_PATTERN_SPLIT = re.compile(r"[\n\r,]")


def clean_patterns(raw):
    """A block list from a textarea, a comma-separated field or a JSON list.

    Order is kept (it is the order the user typed), duplicates are dropped, and
    the list is bounded: a client cannot hand Chrome ten thousand patterns.
    """
    items = raw if isinstance(raw, (list, tuple)) else _PATTERN_SPLIT.split(str(raw or ""))
    out = []
    for item in items:
        pattern = _PATTERN_STRIP.sub("", str(item or ""))[:MAX_PATTERN_LENGTH]
        if pattern and pattern not in out:
            out.append(pattern)
        if len(out) >= MAX_BLOCK_PATTERNS:
            break
    return tuple(out)


def pattern_matcher(patterns):
    """A compiled `re` for `patterns`, or None when there are none.

    `*` is the only metacharacter; everything else is literal, so a pattern
    containing `?`, `.` or `+` matches those characters and nothing else.
    Matching is a search, not a full match - `ads` blocks any URL containing it.

    The same rule is implemented in `scan_intercept.js` for the Playwright
    runners; the two are pinned to each other by the tests.
    """
    if not patterns:
        return None
    alternatives = "|".join(re.escape(p).replace(r"\*", ".*") for p in patterns)
    return re.compile(alternatives, re.IGNORECASE)


# --------------------------------------------------------------------------
# DNS override
# --------------------------------------------------------------------------
# "Test the staging box before the DNS is switched": the scan resolves one
# hostname to an IP address of the user's choosing, for the length of the scan
# and nowhere else. It is Chrome's own --host-resolver-rules, so the request
# still carries the real Host header, the real SNI and the real cookies - only
# the address the connection goes to changes.

_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")


def clean_host(raw):
    """A hostname safe to put in a resolver rule, or ""."""
    host = _UA_STRIP.sub("", str(raw or "")).strip().lower().strip(".")
    return host if _HOSTNAME.match(host) else ""


def clean_ip(raw):
    """An IPv4/IPv6 address in its canonical form, or "" if it is not one."""
    text = _UA_STRIP.sub("", str(raw or "")).strip().strip("[]")
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return ""


# The ranges a scan target may not be pointed at. An override that resolves
# into one of these is not "measure my staging box": it is the scanner being
# used to reach something only the scanning host can reach - its own loopback,
# the private network it sits on, or the cloud metadata service at
# 169.254.169.254, which hands out credentials to anything that asks.
#
# Listed explicitly rather than leaning on `ipaddress.is_private`, which also
# covers the documentation ranges (192.0.2/24, 198.51.100/24, 203.0.113/24) -
# those are not reachable and not a way in, and refusing them would only make
# the examples in the README a lie.
_BLOCKED_V4 = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8",          # "this network" - and 0.0.0.0, which means localhost
    "10.0.0.0/8",         # RFC1918
    "100.64.0.0/10",      # carrier-grade NAT
    "127.0.0.0/8",        # loopback
    "169.254.0.0/16",     # link-local, incl. the 169.254.169.254 metadata IP
    "172.16.0.0/12",      # RFC1918
    "192.0.0.0/24",       # IETF protocol assignments
    "192.168.0.0/16",     # RFC1918
    "198.18.0.0/15",      # benchmarking
    "224.0.0.0/4",        # multicast
    "240.0.0.0/4",        # reserved, incl. 255.255.255.255
))

_BLOCKED_V6 = tuple(ipaddress.ip_network(n) for n in (
    "::/128",             # unspecified
    "::1/128",            # loopback
    "fc00::/7",           # unique local
    "fe80::/10",          # link-local
    "ff00::/8",           # multicast
))


def _unwrap_v6(ip):
    """The IPv4 address an IPv6 address really points at, or None.

    `::ffff:10.0.0.1`, a 6to4 address and a Teredo address all reach an IPv4
    host, so classifying them by their IPv6 form alone would let 10.0.0.1
    through in a different spelling.
    """
    for attr in ("ipv4_mapped", "sixtofour", "teredo"):
        value = getattr(ip, attr, None)
        if attr == "teredo" and value:
            value = value[0]        # (server, client) - the server is the peer
        if value:
            return value
    return None


def is_private_target(raw):
    """True when `raw` is an address a scan must not be pointed at.

    Takes anything `clean_ip` accepts. Text that is not an address at all is
    not a private target - it is not a target, and `clean_ip` has already
    dropped it.
    """
    text = clean_ip(raw)
    if not text:
        return False
    ip = ipaddress.ip_address(text)
    if ip.version == 6:
        mapped = _unwrap_v6(ip)
        if mapped is not None and is_private_target(str(mapped)):
            return True
        return any(ip in net for net in _BLOCKED_V6)
    return any(ip in net for net in _BLOCKED_V4)


def _num(raw, default=0.0):
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError, AttributeError):
        return default


def _clamp_int(raw, lo, hi, default=0):
    value = _num(raw, default)
    if value <= 0:
        return 0                       # "not set" - the caller leaves it out
    return int(max(lo, min(hi, value)))


def _clamp_float(raw, lo, hi):
    value = _num(raw, 0.0)
    if value <= 0:
        return 0.0
    return round(max(lo, min(hi, value)), 3)


# --------------------------------------------------------------------------
# the configuration itself
# --------------------------------------------------------------------------

class ScanConfig:
    """One scan's browser conditions, already clamped.

    Build it with `from_request` (a client's raw values) or directly with
    keyword arguments (tests, scripts). `is_default` is true when nothing was
    asked for, which is what keeps an ordinary scan on the old code path.
    """

    __slots__ = ("throttling", "down_kbps", "up_kbps", "latency_ms",
                 "device_profile", "width", "height", "dpr", "user_agent",
                 "block_patterns", "dns_host", "dns_ip")

    def __init__(self, throttling=DEFAULT_THROTTLING, down_kbps=0, up_kbps=0,
                 latency_ms=0, device_profile="", width=0, height=0, dpr=0,
                 user_agent="", block_patterns=(), dns_host="", dns_ip=""):
        self.throttling = throttling if throttling in THROTTLING else DEFAULT_THROTTLING
        self.down_kbps = _clamp_int(down_kbps, MIN_KBPS, MAX_KBPS)
        self.up_kbps = _clamp_int(up_kbps, MIN_KBPS, MAX_KBPS)
        self.latency_ms = int(max(0, min(MAX_LATENCY_MS, _num(latency_ms, 0))))
        self.device_profile = device_profile if device_profile in DEVICES else ""
        self.width = _clamp_int(width, MIN_VIEWPORT, MAX_VIEWPORT)
        self.height = _clamp_int(height, MIN_VIEWPORT, MAX_VIEWPORT)
        self.dpr = _clamp_float(dpr, MIN_DPR, MAX_DPR)
        self.user_agent = clean_user_agent(user_agent)
        self.block_patterns = clean_patterns(block_patterns)
        self.dns_host = clean_host(dns_host)
        self.dns_ip = clean_ip(dns_ip)
        if self.throttling == "custom" and not (self.down_kbps or self.up_kbps
                                                or self.latency_ms):
            # "Custom" with nothing filled in is not a configuration.
            self.throttling = DEFAULT_THROTTLING

    # -- construction ---------------------------------------------------
    @classmethod
    def from_request(cls, get):
        """Read the knobs out of a request mapping's `.get`."""
        return cls(
            throttling=(get("throttling") or DEFAULT_THROTTLING).strip().lower(),
            down_kbps=get("down_kbps"),
            up_kbps=get("up_kbps"),
            latency_ms=get("latency_ms"),
            device_profile=(get("device_profile") or "").strip().lower(),
            width=get("viewport_width"),
            height=get("viewport_height"),
            dpr=get("dpr"),
            user_agent=get("user_agent"),
            block_patterns=get("block_patterns"),
            dns_host=get("dns_host"),
            dns_ip=get("dns_ip"),
        )

    @classmethod
    def from_dict(cls, data):
        return cls(**{k: v for k, v in (data or {}).items() if k in cls.__slots__})

    def as_dict(self):
        """Plain values, safe to persist, log or send back to the browser -
        there is nothing secret in a ScanConfig."""
        data = {name: getattr(self, name) for name in self.__slots__}
        data["block_patterns"] = list(self.block_patterns)   # JSON has no tuple
        return data

    def for_url(self, url):
        """This configuration as it applies to `url`.

        A DNS override entered as an IP alone means "the site I am scanning",
        so the hostname is filled in from the URL here - once, where the URL is
        known - and every flag built below is then URL-independent.
        """
        if not self.dns_ip or self.dns_host:
            return self
        data = self.as_dict()
        data["dns_host"] = urlparse(url).hostname or ""
        return ScanConfig(**data)

    def __eq__(self, other):
        return isinstance(other, ScanConfig) and self.as_dict() == other.as_dict()

    def __repr__(self):
        return f"ScanConfig({self.as_dict()})"

    # -- derived facts --------------------------------------------------
    @property
    def preset(self):
        """The throttling preset, with a custom one's numbers filled in."""
        base = THROTTLING[self.throttling]
        if self.throttling != "custom":
            return base
        return base._replace(down=self.down_kbps or base.down,
                             up=self.up_kbps or base.up,
                             latency=self.latency_ms)

    @property
    def device(self):
        return DEVICES.get(self.device_profile)

    @property
    def emulates(self):
        """True when the browser's screen or identity is being overridden."""
        return bool(self.device_profile or self.width or self.height
                    or self.dpr or self.user_agent)

    @property
    def blocks(self):
        """True when some requests are to be blocked during the scan."""
        return bool(self.block_patterns)

    @property
    def overrides_dns(self):
        """True when a hostname is pinned to an address for this scan.

        Both halves are needed: an address with no host to attach it to is not
        an override, and `for_url` is what supplies the host when the user gave
        only the address.
        """
        return bool(self.dns_ip and self.dns_host)

    @property
    def targets_private_network(self):
        """True when the DNS override points at an address that only means
        something on the scanning host's own network - see
        `is_private_target`. The decision of what to do about it belongs to
        the deployment, so it is made in `guardrails`, not here."""
        return is_private_target(self.dns_ip)

    @property
    def intercepts(self):
        """True when the browser's own networking is being altered."""
        return self.blocks or bool(self.dns_ip)

    @property
    def is_default(self):
        """Nothing was configured - the pipeline runs its original commands."""
        return (self.throttling == DEFAULT_THROTTLING and not self.emulates
                and not self.intercepts)

    def blocked(self, url):
        """True when `url` is one this scan refuses to load.

        The pattern language is Lighthouse's, and the browsers do the real
        blocking - this is the same decision, available to Python for the log
        and for the tests.
        """
        matcher = pattern_matcher(self.block_patterns)
        return bool(matcher and matcher.search(str(url or "")))

    def host_rules(self):
        """Chrome's `--host-resolver-rules` value for this scan, or "".

        `MAP <host> <address>` changes only where the connection goes: the
        request still carries the real host name, so the site sees the same
        Host header, certificate name and cookies it always would.
        """
        return f"MAP {self.dns_host} {self.dns_ip}" if self.overrides_dns else ""

    def family(self, device):
        """`desktop` or `mobile` for this scan: a chosen profile wins over the
        Desktop/Mobile toggle, so picking a Pixel really does run as a phone."""
        chosen = self.device
        if chosen is not None:
            return chosen.family
        return "desktop" if device == "desktop" else "mobile"

    def screen(self, device):
        """(width, height, dpr) for this scan - zeros where nothing is set."""
        chosen = self.device
        width = self.width or (chosen.width if chosen else 0)
        height = self.height or (chosen.height if chosen else 0)
        dpr = self.dpr or (chosen.dpr if chosen else 0)
        return width, height, dpr

    def agent(self):
        """The User-Agent override, or "" for the browser's own."""
        chosen = self.device
        return self.user_agent or (chosen.user_agent if chosen else "")

    def summary(self, device):
        """One line for the scan log. No secrets pass through here."""
        bits = [f"device {self.family(device)}"]
        chosen = self.device
        if chosen is not None:
            bits.append(chosen.label)
        width, height, dpr = self.screen(device)
        if width and height:
            bits.append(f"viewport {width}x{height}" + (f"@{dpr:g}x" if dpr else ""))
        elif dpr:
            bits.append(f"dpr {dpr:g}")
        bits.append(f"connection {THROTTLING[self.throttling].label}")
        if self.agent():
            bits.append("custom User-Agent")
        if self.blocks:
            bits.append(f"blocking {len(self.block_patterns)} URL pattern(s)")
        if self.overrides_dns:
            bits.append(f"{self.dns_host} resolved to {self.dns_ip}")
        return ", ".join(bits)

    # -- command-line flags ---------------------------------------------
    def lighthouse_flags(self, device):
        """Flags for one `lighthouse` invocation.

        With nothing configured this is exactly the `--preset=desktop`/nothing
        the pipeline has always passed.
        """
        return (self._emulation_flags(device) + self._throttling_flags()
                + self._blocking_flags())

    def _blocking_flags(self):
        """Lighthouse blocks these itself, through Chrome's own network layer -
        one flag per pattern, so a pattern needs no quoting."""
        return [f"--blocked-url-patterns={p}" for p in self.block_patterns]

    def _emulation_flags(self, device):
        family = self.family(device)
        width, height, dpr = self.screen(device)
        agent = self.agent()

        if not self.emulates:
            # Untouched: Lighthouse's desktop preset, or its mobile default.
            return ["--preset=desktop"] if family == "desktop" else []

        # An explicit emulation replaces the preset rather than fighting it:
        # --preset=desktop sets form factor, screen and throttling in one go,
        # and half-overriding it is how you end up with a desktop-sized phone.
        flags = [f"--form-factor={family}",
                 f"--screenEmulation.mobile={'true' if family == 'mobile' else 'false'}"]
        if width:
            flags.append(f"--screenEmulation.width={width}")
        if height:
            flags.append(f"--screenEmulation.height={height}")
        if dpr:
            flags.append(f"--screenEmulation.deviceScaleFactor={dpr:g}")
        if not width and not height and family == "desktop":
            # Desktop with no size of its own still needs a desktop-shaped
            # screen; these are the numbers --preset=desktop uses.
            flags += ["--screenEmulation.width=1350", "--screenEmulation.height=940",
                      "--screenEmulation.deviceScaleFactor=1"]
        if agent:
            flags.append(f"--emulatedUserAgent={agent}")
        return flags

    def _throttling_flags(self):
        preset = self.preset
        if not preset.method:                       # "default": say nothing
            return []
        flags = [f"--throttling-method={preset.method}"]
        if preset.method == "provided":
            return flags + ["--throttling.cpuSlowdownMultiplier=1"]
        if preset.latency:
            flags.append(f"--throttling.requestLatencyMs={preset.latency}")
        if preset.down:
            flags.append(f"--throttling.downloadThroughputKbps={preset.down}")
        if preset.up:
            flags.append(f"--throttling.uploadThroughputKbps={preset.up}")
        flags.append(f"--throttling.cpuSlowdownMultiplier={preset.cpu}")
        return flags

    def chrome_flags(self):
        """Extra flags for the Chrome that Lighthouse launches, or [].

        The value of a resolver rule contains spaces, and Lighthouse splits its
        `--chrome-flags` string into arguments the way a shell would - so the
        value is quoted here, where the rule is built, rather than left for
        every caller to remember.
        """
        rules = self.host_rules()
        return [f'--host-resolver-rules="{rules}"'] if rules else []

    def crawl_config(self):
        """Configuration for `unlighthouse-ci`, or {}.

        The crawler's CLI knows nothing about blocked URLs or resolver rules,
        but it does read an `unlighthouse.config.js` from the directory it runs
        in, and it passes both of these straight through to the Lighthouse and
        the browser it drives. Written only when there is something to say, so
        an ordinary scan runs in a directory with no config file at all - the
        crawl it has always been.
        """
        config = {}
        if self.block_patterns:
            config["lighthouseOptions"] = {"blockedUrlPatterns": list(self.block_patterns)}
        rules = self.host_rules()
        if rules:
            config["puppeteerOptions"] = {"args": [f"--host-resolver-rules={rules}"]}
        return config

    def crawl_flags(self, device):
        """Flags for `unlighthouse-ci`, whose CLI is much smaller: it knows
        desktop/mobile, one User-Agent, and whether to throttle at all."""
        flags = ["--desktop"] if self.family(device) == "desktop" else []
        # The crawl has always thrown --throttle; only an explicit
        # "Unthrottled" takes it away.
        if self.throttling != "none":
            flags.append("--throttle")
        agent = self.agent()
        if agent:
            flags += ["--user-agent", agent]
        return flags

    def browser_context(self):
        """Options for the scanners that drive a browser directly (the login
        step, the axe runner). Empty when nothing is set.

        Mostly Playwright `newContext()` options, plus two keys the runners
        lift out because they are not context options at all:

          `blockPatterns` - installed as a request-interception route, so a
                            blocked request is aborted before it is issued;
          `launchArgs`    - flags for the browser process itself, which is why
                            the DNS override has to be here rather than in the
                            context: resolution happens below the context.
        """
        options = {}
        width, height, dpr = self.screen("")
        if width and height:
            options["viewport"] = {"width": width, "height": height}
        if dpr:
            options["deviceScaleFactor"] = dpr
        agent = self.agent()
        if agent:
            options["userAgent"] = agent
        if self.block_patterns:
            options["blockPatterns"] = list(self.block_patterns)
        rules = self.host_rules()
        if rules:
            options["launchArgs"] = [f"--host-resolver-rules={rules}"]
        return options


DEFAULT = ScanConfig()


def throttling_choices():
    """(id, label) pairs in the order the UI should offer them."""
    return [(t.id, t.label) for t in THROTTLING.values()]


def device_choices():
    return [(d.id, d.label) for d in DEVICES.values()]
