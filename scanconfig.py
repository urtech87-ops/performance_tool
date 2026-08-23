#!/usr/bin/env python3
"""
Scan configuration - what the browser looks and feels like
==========================================================
Connection throttling, device emulation, viewport and User-Agent: the knobs
that decide *under what conditions* a page is measured. Nothing here is
secret; credentials live in `scanauth.py` and never pass through this module.

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
could inject a second HTTP header, and an unknown preset id falls back to the
default rather than raising.
"""

import re
from collections import OrderedDict, namedtuple

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
                 "device_profile", "width", "height", "dpr", "user_agent")

    def __init__(self, throttling=DEFAULT_THROTTLING, down_kbps=0, up_kbps=0,
                 latency_ms=0, device_profile="", width=0, height=0, dpr=0,
                 user_agent=""):
        self.throttling = throttling if throttling in THROTTLING else DEFAULT_THROTTLING
        self.down_kbps = _clamp_int(down_kbps, MIN_KBPS, MAX_KBPS)
        self.up_kbps = _clamp_int(up_kbps, MIN_KBPS, MAX_KBPS)
        self.latency_ms = int(max(0, min(MAX_LATENCY_MS, _num(latency_ms, 0))))
        self.device_profile = device_profile if device_profile in DEVICES else ""
        self.width = _clamp_int(width, MIN_VIEWPORT, MAX_VIEWPORT)
        self.height = _clamp_int(height, MIN_VIEWPORT, MAX_VIEWPORT)
        self.dpr = _clamp_float(dpr, MIN_DPR, MAX_DPR)
        self.user_agent = clean_user_agent(user_agent)
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
        )

    @classmethod
    def from_dict(cls, data):
        return cls(**{k: v for k, v in (data or {}).items() if k in cls.__slots__})

    def as_dict(self):
        """Plain values, safe to persist, log or send back to the browser -
        there is nothing secret in a ScanConfig."""
        return {name: getattr(self, name) for name in self.__slots__}

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
    def is_default(self):
        """Nothing was configured - the pipeline runs its original commands."""
        return self.throttling == DEFAULT_THROTTLING and not self.emulates

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
        return ", ".join(bits)

    # -- command-line flags ---------------------------------------------
    def lighthouse_flags(self, device):
        """Flags for one `lighthouse` invocation.

        With nothing configured this is exactly the `--preset=desktop`/nothing
        the pipeline has always passed.
        """
        return self._emulation_flags(device) + self._throttling_flags()

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
        """Playwright context options, for the scanners that drive a browser
        directly (the login step, the axe runner). Empty when nothing is set."""
        options = {}
        width, height, dpr = self.screen("")
        if width and height:
            options["viewport"] = {"width": width, "height": height}
        if dpr:
            options["deviceScaleFactor"] = dpr
        agent = self.agent()
        if agent:
            options["userAgent"] = agent
        return options


DEFAULT = ScanConfig()


def throttling_choices():
    """(id, label) pairs in the order the UI should offer them."""
    return [(t.id, t.label) for t in THROTTLING.values()]


def device_choices():
    return [(d.id, d.label) for d in DEVICES.values()]
