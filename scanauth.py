#!/usr/bin/env python3
"""
Scan credentials - third-party secrets, held for one scan and no longer
=======================================================================
Everything needed to reach a page that is not public: HTTP Basic auth, a
pasted cookie jar, and a scripted form login. These are somebody else's
passwords, so this module is written around a single rule:

    **A credential exists in memory, for the length of one scan, and nowhere
    else. Ever.**

Concretely, and enforced by tests:

  * Credentials are never part of `sanitize_params`' output, so they cannot
    reach the run folder, `run-status.json`, a report, the job record, the
    `accepted` SSE payload or anything the browser stores as a preset.
  * They are never accepted from a query string - only from a request body -
    so they cannot land in an access log, a `Referer` header, or the user's
    own browser history. `dashboard.scan()` reads them from POST only.
  * Every line the pipeline logs goes through `Redactor` first, so a
    credential cannot be echoed back by a command, a stack trace or a tool's
    own error output.
  * `scrub()` zeroes the buffers when the scan ends.

The one place a secret is unavoidably visible is the argument list of the
`lighthouse` and `unlighthouse-ci` processes we start: neither tool can be
handed a header any other way, and the alternative - a JSON file on disk - is
worse. Those processes are ours, short-lived, and run as the same user as the
scanner; the command is redacted before it reaches the log. The scripted login
is not affected: it takes its configuration on stdin.

On why the buffers are `bytearray`: CPython strings are immutable and cannot be
wiped, so a secret is held as a mutable buffer that `scrub()` really can zero.
Copies made while building a header still exist until the garbage collector
takes them - that is a property of the runtime, not something this module can
fix - but the long-lived copy, the one that would otherwise sit in a worker
between scans, is gone.
"""

import base64
import json
import re

# The form fields that carry a secret. Nothing on this list may be persisted,
# logged, echoed, or saved as a preset - client-side or server-side. It is
# exported so the UI, the request layer and the tests all name the same set.
SECRET_FIELDS = (
    "auth_user", "auth_pass",
    "cookies",
    "login_url", "login_user_selector", "login_pass_selector",
    "login_submit_selector", "login_user", "login_pass",
)

MAX_FIELD = 4096          # any single credential field
MAX_COOKIES = 60
MAX_COOKIE_HEADER = 8192

# A cookie name is an HTTP token; a value may not contain anything that ends
# the header. Both are enforced rather than trusted.
_COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_STRIP = re.compile(r"[\r\n\x00]")
_CTL = re.compile(r"[\x00-\x1f\x7f]")


class CredentialError(ValueError):
    """A credential the client sent cannot be used - reported as a plain
    request error, never with the offending value quoted back."""


def _text(raw):
    """One request field: control characters removed, length bounded."""
    return _CTL.sub("", str(raw or "")).strip()[:MAX_FIELD]


def parse_cookies(raw):
    """`name=value; other=value` -> [(name, value)].

    Accepts the format a browser's devtools hands you, including newlines
    instead of semicolons. Anything that is not a valid cookie is an error the
    user can fix, not something silently dropped: a scan that quietly lost the
    session cookie would report a login page as the site.
    """
    text = _STRIP.sub(";", str(raw or "")).strip()
    if not text:
        return []
    pairs = []
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise CredentialError(
                "Cookies must be pasted as name=value pairs separated by ';'.")
        name, value = chunk.split("=", 1)
        name, value = name.strip(), value.strip()
        if not _COOKIE_NAME.match(name):
            raise CredentialError(f"{name[:32]!r} is not a valid cookie name.")
        pairs.append((name, _CTL.sub("", value)))
        if len(pairs) > MAX_COOKIES:
            raise CredentialError(f"At most {MAX_COOKIES} cookies can be sent.")
    header = "; ".join(f"{n}={v}" for n, v in pairs)
    if len(header) > MAX_COOKIE_HEADER:
        raise CredentialError("That cookie header is too large to send.")
    return pairs


class _Secret:
    """A string held in a buffer that can actually be wiped."""

    __slots__ = ("_buf",)

    def __init__(self, value=""):
        self._buf = bytearray(str(value or "").encode("utf-8"))

    def __bool__(self):
        return bool(self._buf)

    def __len__(self):
        return len(self._buf)

    def get(self):
        return self._buf.decode("utf-8", "replace")

    def wipe(self):
        for i in range(len(self._buf)):
            self._buf[i] = 0
        del self._buf[:]

    # A secret must never render itself by accident - not in a traceback, not
    # in a debug print, not in an f-string somebody adds two years from now.
    def __repr__(self):
        return "<redacted>"

    __str__ = __repr__


class FormLogin:
    """A scripted browser login: where the form is, and what to type into it."""

    __slots__ = ("url", "user_selector", "pass_selector", "submit_selector",
                 "username", "password")

    def __init__(self, url="", user_selector="", pass_selector="",
                 submit_selector="", username="", password=""):
        self.url = _text(url)
        self.user_selector = _text(user_selector)
        self.pass_selector = _text(pass_selector)
        self.submit_selector = _text(submit_selector)
        self.username = _Secret(username)
        self.password = _Secret(password)

    def __bool__(self):
        return bool(self.url)

    def validate(self):
        if not self.url:
            return
        from urllib.parse import urlparse
        if urlparse(self.url).scheme not in ("http", "https"):
            raise CredentialError("The login URL must be an http:// or https:// URL.")
        missing = [label for label, value in
                   (("username selector", self.user_selector),
                    ("password selector", self.pass_selector),
                    ("submit selector", self.submit_selector))
                   if not value]
        if missing:
            raise CredentialError(
                "A scripted login needs all three selectors - missing: "
                + ", ".join(missing) + ".")
        if not self.username and not self.password:
            raise CredentialError(
                "A scripted login needs the username and password to type in.")

    def payload(self):
        """What the Node login runner reads on stdin."""
        return {"url": self.url,
                "userSelector": self.user_selector,
                "passSelector": self.pass_selector,
                "submitSelector": self.submit_selector,
                "username": self.username.get(),
                "password": self.password.get()}

    def secrets(self):
        return [self.username.get(), self.password.get()]

    def scrub(self):
        self.username.wipe()
        self.password.wipe()

    def __repr__(self):
        return f"FormLogin(url={self.url!r}, secrets=<redacted>)"


class Credentials:
    """Everything one scan needs to get past a login, and nothing else.

    An empty instance is falsy, and every method on it returns "no flags" -
    which is what keeps an ordinary public scan running the exact commands it
    always ran.
    """

    __slots__ = ("basic_user", "basic_pass", "cookies", "login", "_scrubbed")

    def __init__(self, basic_user="", basic_pass="", cookies=(), login=None):
        self.basic_user = _Secret(basic_user)
        self.basic_pass = _Secret(basic_pass)
        self.cookies = [(n, v) for n, v in cookies]
        self.login = login if login is not None else FormLogin()
        self._scrubbed = False

    # -- construction ---------------------------------------------------
    @classmethod
    def from_request(cls, get):
        """Read credentials out of a request *body* mapping's `.get`.

        Raises CredentialError with a message the user can act on. Callers must
        only ever hand this a POST body - see the module docstring.
        """
        creds = cls(
            basic_user=_text(get("auth_user")),
            basic_pass=_text(get("auth_pass")),
            cookies=parse_cookies(get("cookies")),
            login=FormLogin(url=get("login_url"),
                            user_selector=get("login_user_selector"),
                            pass_selector=get("login_pass_selector"),
                            submit_selector=get("login_submit_selector"),
                            username=get("login_user"),
                            password=get("login_pass")),
        )
        if creds.basic_pass and not creds.basic_user:
            raise CredentialError("Basic authentication needs a username too.")
        creds.login.validate()
        return creds

    @classmethod
    def from_payload(cls, data):
        """Rebuild from the transport dict `payload()` produced."""
        if not data:
            return cls()
        login = data.get("login") or {}
        return cls(basic_user=data.get("basic_user", ""),
                   basic_pass=data.get("basic_pass", ""),
                   cookies=[tuple(pair) for pair in data.get("cookies") or []],
                   login=FormLogin(url=login.get("url", ""),
                                   user_selector=login.get("user_selector", ""),
                                   pass_selector=login.get("pass_selector", ""),
                                   submit_selector=login.get("submit_selector", ""),
                                   username=login.get("username", ""),
                                   password=login.get("password", "")))

    def payload(self):
        """The only serialization of a credential that exists.

        Used for exactly one thing: handing a job to a worker in another
        process. It is never written to the run folder, the job record, a
        report or a log - see `jobqueue`, which keeps it in a separate
        short-lived slot that the worker consumes and deletes.
        """
        return {"basic_user": self.basic_user.get(),
                "basic_pass": self.basic_pass.get(),
                "cookies": [list(pair) for pair in self.cookies],
                "login": {"url": self.login.url,
                          "user_selector": self.login.user_selector,
                          "pass_selector": self.login.pass_selector,
                          "submit_selector": self.login.submit_selector,
                          "username": self.login.username.get(),
                          "password": self.login.password.get()}}

    # -- state ----------------------------------------------------------
    def __bool__(self):
        return bool(self.basic_user or self.basic_pass or self.cookies or self.login)

    @property
    def has_basic(self):
        return bool(self.basic_user)

    def describe(self):
        """What was supplied, in words - never the values themselves."""
        bits = []
        if self.has_basic:
            bits.append("HTTP Basic authentication")
        if self.cookies:
            bits.append(f"{len(self.cookies)} cookie(s)")
        if self.login:
            bits.append("a scripted form login")
        return ", ".join(bits) or "no credentials"

    # -- what the scanners need -----------------------------------------
    def basic_header(self):
        if not self.has_basic:
            return ""
        raw = f"{self.basic_user.get()}:{self.basic_pass.get()}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def cookie_header(self):
        return "; ".join(f"{name}={value}" for name, value in self.cookies)

    def extra_headers(self):
        headers = {}
        if self.has_basic:
            headers["Authorization"] = self.basic_header()
        if self.cookies:
            headers["Cookie"] = self.cookie_header()
        return headers

    def lighthouse_flags(self):
        """`lighthouse --extra-headers '{...}'`, or nothing at all."""
        headers = self.extra_headers()
        if not headers:
            return []
        return ["--extra-headers", json.dumps(headers, separators=(",", ":"))]

    def crawl_flags(self):
        """`unlighthouse-ci` has first-class flags for both of these."""
        flags = []
        if self.has_basic:
            flags += ["--auth", f"{self.basic_user.get()}:{self.basic_pass.get()}"]
        if self.cookies:
            flags += ["--cookies", self.cookie_header()]
        return flags

    def browser_payload(self):
        """What a Playwright-driven scanner reads on stdin: no flags, no argv,
        so the scripted login and the axe runner never expose a secret at all."""
        payload = {}
        if self.has_basic:
            payload["httpCredentials"] = {"username": self.basic_user.get(),
                                          "password": self.basic_pass.get()}
        if self.cookies:
            payload["cookies"] = [{"name": n, "value": v} for n, v in self.cookies]
        return payload

    def add_cookies(self, pairs):
        """Fold the cookies a scripted login produced into the jar, replacing
        any of the same name that were pasted by hand."""
        by_name = dict(self.cookies)
        for name, value in pairs:
            name = str(name or "").strip()
            if _COOKIE_NAME.match(name):
                by_name[name] = _CTL.sub("", str(value or ""))
        self.cookies = list(by_name.items())[:MAX_COOKIES]
        return self.cookies

    # -- redaction + disposal -------------------------------------------
    def secrets(self):
        """Every string that must never appear in output, longest first so a
        value that contains another is replaced whole."""
        values = [self.basic_user.get(), self.basic_pass.get(),
                  self.basic_header(), self.cookie_header()]
        values += [value for _name, value in self.cookies]
        values += self.login.secrets()
        seen, out = set(), []
        for value in values:
            # One- and two-character "secrets" are not distinctive enough to
            # redact: blanking every "a" in the log helps nobody.
            if len(value) > 2 and value not in seen:
                seen.add(value)
                out.append(value)
        out.sort(key=len, reverse=True)
        return out

    def redactor(self):
        return Redactor(self.secrets())

    def scrub(self):
        """Wipe every buffer. The object stays usable-but-empty afterwards, so
        a `finally` block can call it without guarding anything."""
        self.basic_user.wipe()
        self.basic_pass.wipe()
        self.login.scrub()
        self.cookies = []
        self._scrubbed = True

    @property
    def scrubbed(self):
        return self._scrubbed

    def __repr__(self):
        return f"Credentials({self.describe()})"

    __str__ = __repr__


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------

MASK = "[redacted]"


class Redactor:
    """Replaces known secret values wherever they turn up in text.

    Deliberately dumb and total: it does not try to understand the line, it
    just removes the values. Anything the pipeline logs - a command, a stack
    trace, a tool's own stderr - goes through it, so a credential cannot leak
    through a code path nobody thought about.
    """

    def __init__(self, secrets=()):
        self.secrets = [s for s in secrets if s]

    def __bool__(self):
        return bool(self.secrets)

    def text(self, value):
        out = str(value)
        for secret in self.secrets:
            if secret in out:
                out = out.replace(secret, MASK)
        return out

    def cmd(self, cmd):
        """A command line with every secret argument masked, ready to log."""
        return [self.text(arg) for arg in cmd]

    def wrap(self, log):
        """A `log` callable that redacts everything written through it."""
        if not self.secrets:
            return log

        def redacting_log(line):
            return log(self.text(line))

        return redacting_log


NULL = Redactor()


def redactor_for(credentials):
    """A Redactor for `credentials`, which may be None."""
    return credentials.redactor() if credentials else NULL
