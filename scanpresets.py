#!/usr/bin/env python3
"""
Named scan presets - a combination of settings, saved and re-selectable
======================================================================
A preset is one thing: **a name with a set of scan settings attached**. Pick
"Mobile 4G, no ads" from the list and the form fills in the device, the
throttling, the viewport, the block list and the rest in one click, instead of
being set up by hand for the fortieth time.

A preset is *not* a second way to configure a scan. Every value in it is a
value the form already posts and `guardrails.sanitize_params` already reads and
clamps; `to_request()` produces exactly that request shape, and the scan that
follows takes the same path as any other. Nothing here touches the pipeline,
the scoring or the reports.

    **A preset never contains a credential.**

That is the rule this module is built around, and it is the reason a preset is
assembled from a fixed field list (`PRESET_FIELDS`) rather than by filtering
whatever a client sent:

  * `settings_from` reads only the names on that list, so a body carrying
    `auth_pass` produces a preset dict that has no `auth_pass` in it - not
    because the key was removed, but because it was never read;
  * `check_no_secrets` is a second, independent tripwire on the way into the
    store, so a preset that somehow acquired one cannot be persisted;
  * `PresetError` is raised - and the request refused - when a client posts a
    preset body containing one of `scanauth.SECRET_FIELDS`, rather than
    silently dropping it and leaving the user thinking it was saved.

Credentials stay where `scanauth` puts them: in memory, for one scan, and
nowhere else. A re-run with a preset asks for them again, by design.

Storage is per browser session (the same `scan_sid` cookie the rate limiter
uses), through a document store: a JSON file per owner locally, Redis where
the deployment has it. The logic lives in `PresetStore` and the backends only
read and write one dict.
"""

import json
import re
import threading
import time
import uuid
from pathlib import Path

import scanauth
import scanconfig

# --------------------------------------------------------------------------
# what a preset may hold
# --------------------------------------------------------------------------
# Request field names, exactly as the form posts them and as
# `guardrails.sanitize_params` reads them. This tuple is the whole contract:
# the server stores these and only these, and the browser is handed the same
# list (dashboard renders it into the page) so the two cannot drift apart.
PRESET_FIELDS = (
    # scope
    "device", "deep", "samples", "max_pages", "concurrency",
    "categories", "security", "a11y", "standards",
    # connection
    "throttling", "down_kbps", "up_kbps", "latency_ms",
    # device + viewport
    "device_profile", "viewport_width", "viewport_height", "dpr", "user_agent",
    # requests
    "block_patterns", "dns_host", "dns_ip",
)

# Ceilings for the counts. Deliberately the form's own maxima rather than the
# deployment's caps: a preset outlives a config change, and a scan started from
# one is capped at scan time like any other request (and says so).
MAX_SAMPLES = 5
MAX_PAGES = 200
MAX_PARALLEL = 6

MAX_NAME = 60
MAX_PRESETS = 20          # per owner; a saved-settings list, not a database

VALID_CATEGORIES = ("performance", "accessibility", "best-practices", "seo")

_NAME_STRIP = re.compile(r"[\x00-\x1f\x7f]")
_TRUE = ("1", "true", "yes", "on")


class PresetError(ValueError):
    """A preset a client sent cannot be stored, with a message for the user."""


def _flag(raw, default=False):
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in _TRUE


def _count(raw, default, ceiling):
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError, AttributeError):
        return default
    return max(1, min(ceiling, value))


def _list(raw, allowed=None, limit=40):
    """A repeated field, which may arrive as a JSON list or a CSV string."""
    if isinstance(raw, (list, tuple)):
        items = [str(item).strip().lower() for item in raw]
    else:
        items = [item.strip().lower() for item in str(raw or "").split(",")]
    items = [item for item in items if item]
    if allowed is not None:
        return [value for value in allowed if value in items]     # canonical order
    seen, out = set(), []
    for item in items:
        if re.fullmatch(r"[a-z0-9_]+", item) and item not in seen:
            seen.add(item)
            out.append(item)
    return out[:limit]


def clean_name(raw):
    name = _NAME_STRIP.sub("", str(raw or "")).strip()
    return name[:MAX_NAME]


def contains_secrets(data):
    """True when `data` carries any field from `scanauth.SECRET_FIELDS`.

    The tripwire, used on the way in (refuse the request) and on the way to the
    store (refuse to persist). Deliberately checks for the *key*, set or empty:
    a preset has no business carrying the name of a credential field at all.
    """
    if not isinstance(data, dict):
        return False
    return any(name in data for name in scanauth.SECRET_FIELDS)


def check_no_secrets(data, where="a preset"):
    if contains_secrets(data):
        raise PresetError(
            f"Sign-in details are never saved, so {where} cannot contain them. "
            "Fill them in on the scan itself - they are used once and wiped.")
    return data


def settings_from(raw):
    """The settings half of a preset, read out of a request-shaped mapping.

    Every value is clamped exactly as the scan itself would clamp it, so what
    a preset restores is what a scan would have run.
    """
    raw = raw or {}
    get = raw.get
    cfg = scanconfig.ScanConfig.from_request(get)          # clamps the browser knobs
    return {
        "device": "desktop" if str(get("device") or "").strip() == "desktop" else "mobile",
        "deep": _flag(get("deep"), True),
        "samples": _count(get("samples"), 1, MAX_SAMPLES),
        "max_pages": _count(get("max_pages"), 30, MAX_PAGES),
        "concurrency": _count(get("concurrency"), 3, MAX_PARALLEL),
        "categories": _list(get("categories"), VALID_CATEGORIES),
        "security": _flag(get("security")),
        "a11y": _flag(get("a11y")),
        "standards": _list(get("standards")),
        "throttling": cfg.throttling,
        "down_kbps": cfg.down_kbps,
        "up_kbps": cfg.up_kbps,
        "latency_ms": cfg.latency_ms,
        "device_profile": cfg.device_profile,
        "viewport_width": cfg.width,
        "viewport_height": cfg.height,
        "dpr": cfg.dpr,
        "user_agent": cfg.user_agent,
        "block_patterns": list(cfg.block_patterns),
        "dns_host": cfg.dns_host,
        "dns_ip": cfg.dns_ip,
    }


# --------------------------------------------------------------------------
# a preset
# --------------------------------------------------------------------------

class Preset:
    """A name, an id that survives a rename, and the settings themselves."""

    __slots__ = ("id", "name", "settings", "builtin")

    def __init__(self, name, settings=None, preset_id="", builtin=False):
        self.name = clean_name(name)
        if not self.name:
            raise PresetError("A preset needs a name.")
        check_no_secrets(settings, "a preset")
        self.settings = settings_from(settings)
        self.id = clean_id(preset_id) or uuid.uuid4().hex[:12]
        self.builtin = bool(builtin)

    @classmethod
    def from_request(cls, body):
        """Build one from a POSTed body: `{name, settings, id?}`.

        The settings may also be sent flat (the form's own field names at the
        top level), which is what the browser does when it saves the form as
        it stands.
        """
        body = body or {}
        check_no_secrets(body, "a preset")
        settings = body.get("settings")
        if not isinstance(settings, dict):
            settings = body
        check_no_secrets(settings, "a preset")
        return cls(name=body.get("name"), settings=settings,
                   preset_id=body.get("id") or "")

    @classmethod
    def from_dict(cls, data):
        data = data or {}
        return cls(name=data.get("name"), settings=data.get("settings") or {},
                   preset_id=data.get("id") or "", builtin=data.get("builtin"))

    def as_dict(self):
        """What is stored and what the browser is sent. Nothing else exists."""
        return {"id": self.id, "name": self.name, "builtin": self.builtin,
                "settings": dict(self.settings)}

    def renamed(self, name):
        clone = Preset.from_dict(self.as_dict())
        clone.name = clean_name(name)
        if not clone.name:
            raise PresetError("A preset needs a name.")
        return clone

    def to_request(self):
        """The settings as the scan request that carries them.

        A list-valued field becomes the comma-separated form the request layer
        already accepts, so the result can be posted to `/scan` as it stands.
        """
        out = {}
        for field in PRESET_FIELDS:
            value = self.settings.get(field)
            if isinstance(value, (list, tuple)):
                value = ",".join(str(item) for item in value)
            out[field] = value
        return out

    def scan_config(self):
        """The `ScanConfig` this preset describes."""
        return scanconfig.ScanConfig.from_request(self.to_request().get)

    def __eq__(self, other):
        return isinstance(other, Preset) and self.as_dict() == other.as_dict()

    def __repr__(self):
        return f"Preset({self.name!r}, id={self.id!r}, builtin={self.builtin})"


_ID_STRIP = re.compile(r"[^a-zA-Z0-9_-]")


def clean_id(raw):
    return _ID_STRIP.sub("", str(raw or ""))[:40]


# --------------------------------------------------------------------------
# what the tool ships with
# --------------------------------------------------------------------------
# Read-only, always present, and the same on every machine - so a new browser
# has something to pick, and "reset it to how it was" is one click rather than
# a hunt through the advanced options. `DEFAULT_PRESET_ID` is what a browser
# that has never saved anything starts on: the settings the form has always
# shipped with, so the default scan is the scan this tool has always run.

DEFAULT_PRESET_ID = "builtin_default"

# Third-party requests almost every site carries, and almost nobody wants in a
# performance measurement of their own pages.
THIRD_PARTY_PATTERNS = [
    "*googletagmanager.com*", "*google-analytics.com*", "*analytics.google.com*",
    "*doubleclick.net*", "*googlesyndication.com*", "*facebook.net*",
    "*connect.facebook.com*", "*hotjar.com*", "*intercom.io*",
]

BUILTIN = (
    Preset(preset_id=DEFAULT_PRESET_ID, builtin=True,
           name="Default (as shipped)",
           settings={"device": "desktop", "deep": True, "samples": 1,
                     "max_pages": 30, "concurrency": 3,
                     "categories": list(VALID_CATEGORIES)}),
    Preset(preset_id="builtin_mobile_4g", builtin=True,
           name="Mobile 4G handset",
           settings={"device": "mobile", "deep": True, "samples": 1,
                     "max_pages": 30, "concurrency": 3,
                     "categories": list(VALID_CATEGORIES),
                     "throttling": "4g", "device_profile": "pixel_7"}),
    Preset(preset_id="builtin_first_party", builtin=True,
           name="First-party only (no ads or analytics)",
           settings={"device": "desktop", "deep": True, "samples": 1,
                     "max_pages": 30, "concurrency": 3,
                     "categories": list(VALID_CATEGORIES),
                     "block_patterns": list(THIRD_PARTY_PATTERNS)}),
    Preset(preset_id="builtin_staging", builtin=True,
           name="Staging server (unthrottled)",
           settings={"device": "desktop", "deep": True, "samples": 1,
                     "max_pages": 10, "concurrency": 3,
                     "categories": list(VALID_CATEGORIES),
                     "throttling": "none"}),
)

BUILTIN_BY_ID = {preset.id: preset for preset in BUILTIN}


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------
# One document per owner: {"presets": [...], "default": "<id>"}. The backends
# below only move that dict around; every rule about what may be in it lives in
# PresetStore.

class MemoryPresetDocs:
    """Per-process storage - the test suite, and a single dev process."""

    name = "memory"

    def __init__(self):
        self._docs = {}
        self._lock = threading.Lock()

    def read(self, owner):
        with self._lock:
            return json.loads(json.dumps(self._docs.get(owner) or {}))

    def write(self, owner, doc):
        with self._lock:
            self._docs[owner] = json.loads(json.dumps(doc))


class FilePresetDocs:
    """A JSON file per owner, next to the app.

    Presets are worth keeping across a restart - that is the whole point of
    naming one - and a file is enough for the local tool and for a handful of
    gunicorn workers on one host. A deployment with Redis uses that instead.
    """

    name = "file"

    def __init__(self, directory):
        self.dir = Path(directory)
        self._lock = threading.Lock()

    def _path(self, owner):
        return self.dir / f"{clean_id(owner) or 'anonymous'}.json"

    def read(self, owner):
        try:
            return json.loads(self._path(owner).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def write(self, owner, doc):
        with self._lock:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                path = self._path(owner)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(doc), encoding="utf-8")
                tmp.replace(path)             # never a half-written preset list
            except OSError:
                pass                          # read-only disk: presets just don't stick


class RedisPresetDocs:
    """Shared storage, so every web process sees the same saved settings."""

    name = "redis"
    TTL = 180 * 86_400

    def __init__(self, redis_conn, ttl=None):
        self.r = redis_conn
        self.ttl = int(ttl or self.TTL)

    def read(self, owner):
        raw = self.r.get(f"presets:{owner}")
        if not raw:
            return {}
        try:
            return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except ValueError:
            return {}

    def write(self, owner, doc):
        self.r.setex(f"presets:{owner}", self.ttl, json.dumps(doc))


class PresetStore:
    """Create, rename, update, delete and choose a default - for one owner.

    Every write goes through `check_no_secrets` first: whatever a caller thinks
    it is storing, a credential cannot reach the backend.
    """

    def __init__(self, docs=None):
        self.docs = docs if docs is not None else MemoryPresetDocs()

    # -- reading --------------------------------------------------------
    def _doc(self, owner):
        doc = self.docs.read(owner) or {}
        saved = []
        for entry in doc.get("presets") or []:
            try:
                saved.append(Preset.from_dict(entry))
            except PresetError:
                continue                    # a corrupt entry is skipped, not fatal
        return saved, clean_id(doc.get("default"))

    def saved(self, owner):
        """Only this owner's own presets, newest last."""
        return self._doc(owner)[0]

    def list(self, owner):
        """Everything the browser offers: the built-ins, then the saved ones."""
        return list(BUILTIN) + self.saved(owner)

    def get(self, owner, preset_id):
        preset_id = clean_id(preset_id)
        for preset in self.list(owner):
            if preset.id == preset_id:
                return preset
        return None

    def default_id(self, owner):
        """The preset a fresh page starts on - always one that exists."""
        _saved, chosen = self._doc(owner)
        if chosen and self.get(owner, chosen) is not None:
            return chosen
        return DEFAULT_PRESET_ID

    def default(self, owner):
        return self.get(owner, self.default_id(owner))

    def payload(self, owner):
        """What `/api/presets` answers with."""
        return {"presets": [p.as_dict() for p in self.list(owner)],
                "default": self.default_id(owner),
                "fields": list(PRESET_FIELDS),
                "max": MAX_PRESETS}

    # -- writing --------------------------------------------------------
    def _write(self, owner, presets, default_id):
        self.docs.write(owner, {"presets": [p.as_dict() for p in presets],
                                "default": clean_id(default_id),
                                "updated": int(time.time())})

    def save(self, owner, preset, make_default=False):
        """Create a preset, or update the one with the same id.

        A rename is the same call with the same id and a different name, which
        is why the id is not derived from the name.
        """
        check_no_secrets(preset.settings, "a preset")
        if preset.id in BUILTIN_BY_ID:
            raise PresetError(f"{preset.name!r} is a built-in preset and cannot be "
                              "changed. Save it under a new name instead.")
        saved, chosen = self._doc(owner)
        by_id = {p.id: i for i, p in enumerate(saved)}
        if preset.id in by_id:
            saved[by_id[preset.id]] = preset
        else:
            if len(saved) >= MAX_PRESETS:
                raise PresetError(
                    f"{MAX_PRESETS} presets is the limit - delete one first.")
            if any(p.name.lower() == preset.name.lower() for p in saved):
                raise PresetError(f"A preset called {preset.name!r} already exists.")
            saved.append(preset)
        self._write(owner, saved, preset.id if make_default else chosen)
        return preset

    def rename(self, owner, preset_id, name):
        preset = self.get(owner, preset_id)
        if preset is None:
            raise PresetError("That preset no longer exists.")
        return self.save(owner, preset.renamed(name))

    def delete(self, owner, preset_id):
        preset_id = clean_id(preset_id)
        if preset_id in BUILTIN_BY_ID:
            raise PresetError("A built-in preset cannot be deleted.")
        saved, chosen = self._doc(owner)
        kept = [p for p in saved if p.id != preset_id]
        if len(kept) == len(saved):
            return False
        self._write(owner, kept, "" if chosen == preset_id else chosen)
        return True

    def set_default(self, owner, preset_id):
        """Choose which preset a fresh page starts on. A built-in is allowed."""
        preset = self.get(owner, preset_id)
        if preset is None:
            raise PresetError("That preset no longer exists.")
        saved, _chosen = self._doc(owner)
        self._write(owner, saved, preset.id)
        return preset


_store = None
_store_lock = threading.Lock()


def set_store(store):
    """Install the process's store (dashboard picks one; tests use their own)."""
    global _store
    with _store_lock:
        _store = store
    return store


def get_store():
    global _store
    with _store_lock:
        if _store is None:
            _store = PresetStore(MemoryPresetDocs())
        return _store
