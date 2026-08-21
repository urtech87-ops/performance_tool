#!/usr/bin/env python3
"""
Automated accessibility / WCAG compliance scanner (standalone module).

Runs axe-core (via `@axe-core/playwright`, driving Playwright's own bundled
Chromium) against every discovered page, then rolls the raw rule violations up
into:

  1. a per-standard panel - each legal framework is mapped to the WCAG scope
     it points at, and its verdict is computed by filtering the violations to
     that framework's axe tag set;
  2. findings grouped by impact (critical / serious / moderate / minor), each
     with its WCAG success criteria, a plain-language fix, and the affected
     pages + element selectors;
  3. an axe "incomplete" list surfaced as "Needs manual review".

IMPORTANT - this reports AUTOMATED CHECKS ONLY. Automated tooling can detect
roughly a third of WCAG failures; a clean run is not a statement of legal
compliance, and nothing in the report should be presented as one.

Just as important: a page that was never analyzed is not a page that passed.
Failures are counted, the runner's own stdout/stderr is logged verbatim, and a
run that analyzed zero pages reports every standard as "Not determined" rather
than as clean.

Requires Node.js and nothing else: the browser is Playwright's own Chromium,
fetched on first use. Playwright ships that browser and the client that drives
it in one release, so the two cannot drift apart - which is what killed the
previous ChromeDriver-based runner every time Chrome auto-updated underneath it
("session not created: This version of ChromeDriver only supports Chrome
version N / Current browser version is N-1").

Used by dashboard.py; also runnable directly:
    python accessibility_scan.py https://example.com -o accessibility.html
"""

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import namedtuple
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------

# Pinned so a run is reproducible and the report can state exactly what ran.
AXE_CORE_VERSION = "4.13.0"

# The two npm packages the Node runner needs. @axe-core/playwright is versioned
# in lockstep with axe-core itself, so it is pinned to the same number the
# report prints. playwright is a range on purpose: it ships the browser build
# and the client that speaks to it in one release, so any 1.x is internally
# consistent - and the newest one is the release whose browser is still
# downloadable.
RUNNER_DEPS = {
    "@axe-core/playwright": AXE_CORE_VERSION,
    "playwright": "^1.55.0",
}

NODE = "node.exe" if os.name == "nt" else "node"
NPM = "npm.cmd" if os.name == "nt" else "npm"
NPX = "npx.cmd" if os.name == "nt" else "npx"

# The per-page runner (see its own header for the stdout contract).
RUNNER_JS = Path(__file__).resolve().parent / "axe_playwright_runner.js"
RESULT_MARKER = "__AXE_RESULT__"

PER_PAGE_TIMEOUT = 150   # seconds per page
INSTALL_TIMEOUT = 900    # npm install / browser download - once per machine

# Tags axe is always asked to evaluate (a selected standard may add to these).
RUN_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa",
            "best-practice", "section508", "EN-301-549"]

IMPACT_ORDER = ["critical", "serious", "moderate", "minor"]


def scan_env(node_path=None):
    """Env for scan subprocesses: relax Node TLS so sites with an incomplete
    certificate chain (which browsers tolerate but Node rejects) still scan,
    and put the runner's own node_modules on Node's resolution path so the
    script can live in this repo while its packages live in a cache dir."""
    env = os.environ.copy()
    env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
    if node_path:
        existing = env.get("NODE_PATH")
        env["NODE_PATH"] = os.pathsep.join(
            [str(node_path)] + ([existing] if existing else []))
    return env


# --------------------------------------------------------------------------
# runner output + the Node/Playwright runner itself
# --------------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
OUTPUT_MAX_LINES = 24     # keep a failure readable in the scan log
OUTPUT_MAX_COLS = 400

# Noise every Node process emits once NODE_TLS_REJECT_UNAUTHORIZED is set, plus
# npm's own upgrade nag - dropping it keeps the real error visible.
_NOISE = ("Warning: Setting the NODE_TLS_REJECT_UNAUTHORIZED",
          "(Use `node --trace-warnings", "npm notice", "npm warn")


def clean_output(text):
    """ANSI-stripped, de-noised runner output, capped so one broken page cannot
    flood the scan log."""
    if not text:
        return ""
    lines = []
    for raw in str(text).splitlines():
        line = ANSI_RE.sub("", raw).rstrip()
        if not line.strip() or any(n in line for n in _NOISE):
            continue
        lines.append(line[:OUTPUT_MAX_COLS])
    if len(lines) > OUTPUT_MAX_LINES:
        dropped = len(lines) - OUTPUT_MAX_LINES
        lines = (lines[:OUTPUT_MAX_LINES - 6]
                 + [f"... ({dropped} more line(s) omitted)"] + lines[-6:])
    return "\n".join(lines)


# What run_axe needs to start a page: the script, and the environment that lets
# Node find its packages. Built once per scan by ensure_runner().
Runner = namedtuple("Runner", "script env home")


def runner_home():
    """Directory holding the runner's npm packages.

    Kept out of the repo - which may sit on a read-only checkout, and is not
    where npm state belongs - and reused between runs, so the install happens
    once per machine rather than once per scan.
    """
    override = os.environ.get("A11Y_RUNNER_HOME")
    if override:
        return Path(override)
    base = (os.environ.get("LOCALAPPDATA") if os.name == "nt"
            else os.environ.get("XDG_CACHE_HOME")) or str(Path.home() / ".cache")
    try:
        home = Path(base) / "accessibility-scan-runner"
        home.mkdir(parents=True, exist_ok=True)
        return home
    except Exception:
        return Path(tempfile.gettempdir()) / "accessibility-scan-runner"


def node_modules(home):
    return Path(home) / "node_modules"


def deps_installed(home):
    """True when both npm packages are already unpacked in `home`."""
    root = node_modules(home)
    return all((root / name.replace("/", os.sep)).is_dir() for name in RUNNER_DEPS)


def runner_for(home=None):
    """A Runner pointing at `home` - paths and env only, nothing installed."""
    home = Path(home) if home is not None else runner_home()
    return Runner(script=str(RUNNER_JS), env=scan_env(node_modules(home)), home=home)


def _run_tool(cmd, cwd, env, timeout=INSTALL_TIMEOUT):
    """Run a setup command. Returns (returncode, cleaned combined output);
    a command that cannot start is a non-zero code, never an exception."""
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), check=False, timeout=timeout, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as e:
        return 1, clean_output(getattr(e, "output", "")) or f"timed out after {timeout}s"
    except FileNotFoundError:
        return 1, f"{cmd[0]} not found on PATH"
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"
    return getattr(proc, "returncode", 0), clean_output(getattr(proc, "stdout", ""))


def install_deps(home, env, log=print):
    """npm install @axe-core/playwright + playwright into `home`.

    Returns True on success. A failure is logged and never raised: the scan
    still runs, every page then fails with the runner's real error, and a run
    that analyzed nothing reports "Not determined" rather than a false green.
    """
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    (home / "package.json").write_text(json.dumps({
        "name": "accessibility-scan-runner",
        "version": "1.0.0",
        "private": True,
        "description": "npm dependencies for accessibility_scan.py's axe runner",
        "dependencies": dict(RUNNER_DEPS),
    }, indent=2) + "\n", encoding="utf-8")

    wanted = ", ".join(f"{name}@{spec}" for name, spec in RUNNER_DEPS.items())
    log(f"Accessibility: installing the runner's npm packages (first run only): {wanted}")
    rc, output = _run_tool([NPM, "install", "--no-audit", "--no-fund", "--loglevel=error"],
                           cwd=home, env=env)
    if rc == 0 and deps_installed(home):
        log(f"Accessibility: runner packages installed in {home}")
        return True
    log(f"Accessibility: WARNING - installing the runner's npm packages failed "
        f"(npm exited {rc}). Every page will fail until `npm install` can reach the "
        f"registry; install them by hand in {home} if this machine is offline.")
    for line in (output or "").splitlines():
        log(f"    npm: {line}")
    return False


# Asks Playwright to actually start its browser and print where it lives. A
# launch, not a file check: a headless run uses Playwright's headless shell
# rather than the full Chromium binary, so "the file exists" is the wrong
# question - "does it come up" is the one that decides whether pages can be
# audited, and it catches a missing shared library too.
BROWSER_PROBE = (
    "const {chromium} = require('playwright');"
    "chromium.launch({headless: true, args: ['--no-sandbox', '--disable-gpu']})"
    "  .then(async (b) => { const p = chromium.executablePath();"
    "                       await b.close();"
    "                       process.stdout.write(p || 'ready'); })"
    "  .catch((e) => { process.stderr.write(String(e)); process.exit(1); });"
)


def chromium_path(home, env):
    """Path to a Chromium that really starts, or "" when Playwright cannot get
    one up yet (not downloaded, or Playwright itself cannot be loaded)."""
    try:
        proc = subprocess.run([NODE, "-e", BROWSER_PROBE], cwd=str(home), check=False,
                              timeout=120, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, encoding="utf-8", errors="replace")
    except Exception:
        return ""
    if getattr(proc, "returncode", 1) != 0:
        return ""
    return (getattr(proc, "stdout", "") or "").strip()


def install_chromium(home, env, log=print):
    """Fetch Playwright's own Chromium build. Returns its path, or ""."""
    log("Accessibility: Playwright's Chromium is not present yet - fetching it "
        "(first run only): npx playwright install chromium")
    rc, output = _run_tool([NPX, "--yes", "playwright", "install", "chromium"],
                           cwd=home, env=env)
    path = chromium_path(home, env)
    if path:
        log(f"Accessibility: Chromium installed ({path})")
        return path
    log(f"Accessibility: WARNING - could not install Playwright's Chromium "
        f"(exited {rc}). Every page will fail until it is downloadable; run "
        f"`npx playwright install chromium` in {home} once the network allows it.")
    for line in (output or "").splitlines():
        log(f"    playwright: {line}")
    return ""


def ensure_runner(log=print):
    """Get the Node runner ready before the first page is scanned.

    Installs the npm packages and Playwright's Chromium if they are missing,
    logging each step. Everything here is a warning, never a hard stop - a run
    should still be attempted and report its real error. Returns the Runner
    scan_site hands to every page.
    """
    home = runner_home()
    runner = runner_for(home)
    log(f"Accessibility: runner {NODE} {RUNNER_JS.name} "
        f"(@axe-core/playwright {AXE_CORE_VERSION} on Playwright's bundled Chromium)")

    node = shutil.which(NODE) or shutil.which("node")
    if node:
        log(f"Accessibility: node {node}")
    else:
        log(f"Accessibility: WARNING - {NODE} not found on PATH; the runner cannot start "
            f"without Node.js. Install Node 18+ and re-run.")
    if not RUNNER_JS.is_file():
        log(f"Accessibility: WARNING - runner script missing at {RUNNER_JS}")

    if deps_installed(home):
        log(f"Accessibility: runner packages ready in {home}")
    else:
        install_deps(home, runner.env, log=log)

    path = chromium_path(home, runner.env)
    if path:
        log(f"Accessibility: chromium {path}")
    else:
        install_chromium(home, runner.env, log=log)
    return runner


# --------------------------------------------------------------------------
# standards mapping
# --------------------------------------------------------------------------
# Each legal framework points at a WCAG version/level; we express that scope as
# the set of axe tags that belong to it, then filter violations by those tags.

W20A = ("wcag2a",)
W20AA = W20A + ("wcag2aa",)
W20AAA = W20AA + ("wcag2aaa",)
W21A = W20A + ("wcag21a",)
W21AA = W20AA + ("wcag21a", "wcag21aa")
W21AAA = W20AAA + ("wcag21a", "wcag21aa", "wcag21aaa")
W22A = W21A + ("wcag22a",)
W22AA = W21AA + ("wcag22a", "wcag22aa")
W22AAA = W21AAA + ("wcag22a", "wcag22aa", "wcag22aaa")

# id -> name, region, the WCAG scope in words, the axe tags that define it.
STANDARDS = [
    {"id": "wcag20a", "name": "WCAG 2.0 Level A", "region": "International",
     "basis": "WCAG 2.0 Level A", "tags": W20A, "applicable": True},
    {"id": "wcag20aa", "name": "WCAG 2.0 Level AA", "region": "International",
     "basis": "WCAG 2.0 Levels A + AA", "tags": W20AA, "applicable": True},
    {"id": "wcag20aaa", "name": "WCAG 2.0 Level AAA", "region": "International",
     "basis": "WCAG 2.0 Levels A + AA + AAA", "tags": W20AAA, "applicable": True},
    {"id": "wcag21a", "name": "WCAG 2.1 Level A", "region": "International",
     "basis": "WCAG 2.1 Level A", "tags": W21A, "applicable": True},
    {"id": "wcag21aa", "name": "WCAG 2.1 Level AA", "region": "International",
     "basis": "WCAG 2.1 Levels A + AA", "tags": W21AA, "applicable": True},
    {"id": "wcag21aaa", "name": "WCAG 2.1 Level AAA", "region": "International",
     "basis": "WCAG 2.1 Levels A + AA + AAA", "tags": W21AAA, "applicable": True},
    {"id": "wcag22a", "name": "WCAG 2.2 Level A", "region": "International",
     "basis": "WCAG 2.2 Level A", "tags": W22A, "applicable": True},
    {"id": "wcag22aa", "name": "WCAG 2.2 Level AA", "region": "International",
     "basis": "WCAG 2.2 Levels A + AA", "tags": W22AA, "applicable": True},
    {"id": "wcag22aaa", "name": "WCAG 2.2 Level AAA", "region": "International",
     "basis": "WCAG 2.2 Levels A + AA + AAA", "tags": W22AAA, "applicable": True},

    {"id": "section508", "name": "Section 508 (Revised)", "region": "United States",
     "basis": "WCAG 2.0 Level AA (+ Section 508 specific rules)",
     "tags": W20AA + ("section508",), "applicable": True},
    {"id": "en301549", "name": "EN 301 549", "region": "European Union",
     "basis": "WCAG 2.1 Level AA (+ EN 301 549 specific rules)",
     "tags": W21AA + ("EN-301-549",), "applicable": True},
    {"id": "ada", "name": "ADA Title III", "region": "United States",
     "basis": "WCAG 2.1 Level AA (DOJ-referenced benchmark)",
     "tags": W21AA, "applicable": True},
    {"id": "unruh", "name": "California Unruh Civil Rights Act", "region": "United States (CA)",
     "basis": "WCAG 2.1 Level AA", "tags": W21AA, "applicable": True},
    {"id": "aoda", "name": "Ontario AODA", "region": "Canada (ON)",
     "basis": "WCAG 2.0 Level AA", "tags": W20AA, "applicable": True},
    {"id": "uk_equality", "name": "UK Equality Act 2010", "region": "United Kingdom",
     "basis": "WCAG 2.1 Level AA", "tags": W21AA, "applicable": True},
    {"id": "au_dda", "name": "Australian DDA", "region": "Australia",
     "basis": "WCAG 2.1 Level AA", "tags": W21AA, "applicable": True},
    {"id": "il_5568", "name": "Israeli Standard 5568", "region": "Israel",
     "basis": "WCAG 2.0 Level AA", "tags": W20AA, "applicable": True},
    {"id": "ca_aca", "name": "Accessible Canada Act (ACA)", "region": "Canada",
     "basis": "WCAG 2.1 Level AA", "tags": W21AA, "applicable": True},

    {"id": "atag20", "name": "ATAG 2.0", "region": "International",
     "basis": "Authoring-tool guideline - not testable against a rendered page",
     "tags": (), "applicable": False,
     "note": "ATAG 2.0 governs authoring tools (how content is produced), not the "
             "delivered page, so no page-level automated check applies."},
]

STANDARDS_BY_ID = {s["id"]: s for s in STANDARDS}

DEFAULT_STANDARDS = ["wcag21aa", "section508", "en301549", "ada"]


def normalize_standards(selected):
    """Keep known ids in canonical order, drop junk; empty selection -> defaults."""
    if not selected:
        return list(DEFAULT_STANDARDS)
    if isinstance(selected, str):
        selected = [s.strip() for s in selected.split(",")]
    wanted = {str(s).strip() for s in selected if str(s).strip()}
    out = [s["id"] for s in STANDARDS if s["id"] in wanted]
    return out or list(DEFAULT_STANDARDS)


def run_tags(selected=None):
    """axe tag list for a run: always RUN_TAGS, plus anything a selected
    standard needs that the base list doesn't already cover (e.g. AAA)."""
    tags = list(RUN_TAGS)
    for sid in normalize_standards(selected):
        std = STANDARDS_BY_ID[sid]
        for t in std["tags"]:
            if t not in tags:
                tags.append(t)
    return tags


# --------------------------------------------------------------------------
# WCAG success criteria + plain-language fixes
# --------------------------------------------------------------------------

SC_TAG_RE = re.compile(r"^wcag(\d)(\d)(\d{1,2})$")

SC_NAMES = {
    "1.1.1": "Non-text Content", "1.2.1": "Audio-only and Video-only",
    "1.2.2": "Captions (Prerecorded)", "1.3.1": "Info and Relationships",
    "1.3.2": "Meaningful Sequence", "1.3.4": "Orientation",
    "1.3.5": "Identify Input Purpose", "1.4.1": "Use of Color",
    "1.4.2": "Audio Control", "1.4.3": "Contrast (Minimum)",
    "1.4.4": "Resize Text", "1.4.6": "Contrast (Enhanced)",
    "1.4.10": "Reflow", "1.4.11": "Non-text Contrast",
    "1.4.12": "Text Spacing", "1.4.13": "Content on Hover or Focus",
    "2.1.1": "Keyboard", "2.1.2": "No Keyboard Trap",
    "2.2.1": "Timing Adjustable", "2.2.2": "Pause, Stop, Hide",
    "2.4.1": "Bypass Blocks", "2.4.2": "Page Titled",
    "2.4.3": "Focus Order", "2.4.4": "Link Purpose (In Context)",
    "2.4.6": "Headings and Labels", "2.4.7": "Focus Visible",
    "2.5.3": "Label in Name", "2.5.8": "Target Size (Minimum)",
    "3.1.1": "Language of Page", "3.1.2": "Language of Parts",
    "3.2.1": "On Focus", "3.2.2": "On Input", "3.2.6": "Consistent Help",
    "3.3.1": "Error Identification", "3.3.2": "Labels or Instructions",
    "4.1.1": "Parsing", "4.1.2": "Name, Role, Value", "4.1.3": "Status Messages",
}

# Plain-language remediation for the rules that fire most often. Anything not
# listed falls back to axe's own help text.
FIX_HINTS = {
    "color-contrast":
        "Darken the text or lighten the background until the contrast ratio is at "
        "least 4.5:1 for normal text (3:1 for text 18pt/14pt-bold and larger).",
    "color-contrast-enhanced":
        "For AAA, push the contrast ratio to at least 7:1 for normal text (4.5:1 for large text).",
    "image-alt":
        "Give every meaningful image an alt attribute that describes what it conveys; "
        "use alt=\"\" for purely decorative images so screen readers skip them.",
    "input-image-alt":
        "Add an alt attribute to image buttons describing the action they perform.",
    "area-alt":
        "Give each image-map area an alt attribute describing its destination.",
    "link-name":
        "Make sure every link has text (or an aria-label / visually-hidden span) that "
        "says where it goes - icon-only links need an accessible name.",
    "button-name":
        "Give every button visible text or an aria-label; icon-only buttons are "
        "announced as just \"button\" without one.",
    "label":
        "Attach a <label for=\"...\"> to every form control (or use aria-label / "
        "aria-labelledby) so its purpose is announced.",
    "form-field-multiple-labels":
        "Leave exactly one label per form field - multiple labels are announced inconsistently.",
    "select-name":
        "Give each <select> an associated label so its purpose is announced.",
    "aria-required-attr":
        "Add the ARIA attributes the element's role requires, or drop the role and use "
        "the native HTML element instead.",
    "aria-required-children":
        "Nest the child roles the parent role requires (e.g. a role=\"list\" needs role=\"listitem\" children).",
    "aria-required-parent":
        "Place the element inside the parent role its ARIA role requires.",
    "aria-valid-attr-value":
        "Fix the ARIA attribute value - aria-labelledby/aria-describedby must point at "
        "ids that actually exist on the page.",
    "aria-hidden-focus":
        "Never put aria-hidden=\"true\" on something focusable; remove the attribute or "
        "take the element out of the tab order.",
    "aria-allowed-attr":
        "Remove ARIA attributes that aren't allowed on this element's role.",
    "aria-roles":
        "Use a valid ARIA role, or remove the role and rely on the native element semantics.",
    "html-has-lang":
        "Add a lang attribute to the <html> element (e.g. lang=\"en\") so screen readers "
        "use the right pronunciation.",
    "html-lang-valid":
        "Correct the <html> lang value to a valid BCP-47 language tag.",
    "valid-lang":
        "Correct the lang attribute on the element to a valid BCP-47 language tag.",
    "document-title":
        "Give the page a unique, descriptive <title> - it is the first thing announced.",
    "heading-order":
        "Use heading levels in order (h1 then h2 then h3) without skipping levels.",
    "empty-heading":
        "Remove empty headings or give them text - a heading with no content is announced as noise.",
    "page-has-heading-one":
        "Add a single <h1> that names what the page is about.",
    "landmark-one-main":
        "Wrap the primary content in one <main> element so keyboard users can jump to it.",
    "region":
        "Put all page content inside landmarks (header/nav/main/footer) so it can be navigated by region.",
    "bypass":
        "Add a \"skip to main content\" link (or proper landmarks) so keyboard users can "
        "bypass the repeated navigation.",
    "duplicate-id":
        "Make every id on the page unique - duplicates break label and ARIA references.",
    "duplicate-id-active":
        "Make ids on interactive elements unique so labels and ARIA references resolve.",
    "duplicate-id-aria":
        "Make ids referenced by ARIA attributes unique so they resolve to one element.",
    "list":
        "Only <li> (and script/template) may be direct children of <ul>/<ol> - move other markup inside an <li>.",
    "listitem":
        "Put every <li> inside a <ul> or <ol> so it is announced as part of a list.",
    "definition-list":
        "Structure <dl> as <dt>/<dd> pairs only.",
    "td-headers-attr":
        "Point each headers attribute at ids of cells in the same table.",
    "th-has-data-cells":
        "Make sure each header cell describes data cells, and mark headers with <th>/scope.",
    "table-fake-caption":
        "Use a real <caption> element instead of a styled row for the table caption.",
    "frame-title":
        "Give every <iframe> a title attribute describing the embedded content.",
    "meta-viewport":
        "Remove user-scalable=no and allow a maximum-scale of at least 5 so the page can be zoomed.",
    "meta-refresh":
        "Remove the auto-refresh/redirect, or make the delay adjustable by the user.",
    "tabindex":
        "Avoid tabindex greater than 0 - it makes the tab order unpredictable; use DOM order instead.",
    "target-size":
        "Make interactive targets at least 24x24 CSS pixels, or space them so they don't overlap.",
    "scrollable-region-focusable":
        "Make scrollable regions keyboard-reachable (tabindex=\"0\") so they can be scrolled without a mouse.",
    "nested-interactive":
        "Don't nest interactive controls (e.g. a button inside a link) - screen readers announce only one.",
    "video-caption":
        "Add captions to video so the audio content is available to deaf and hard-of-hearing users.",
    "object-alt":
        "Give <object> elements alternative text describing the embedded content.",
    "server-side-image-map":
        "Replace server-side image maps with client-side maps that expose alt text per area.",
    "blink": "Remove <blink> - blinking content is a distraction and a seizure risk.",
    "marquee": "Replace <marquee> with static content the user controls.",
}


def sc_from_tags(tags):
    """axe tags like 'wcag143' -> WCAG success criterion '1.4.3'."""
    out = set()
    for t in tags or []:
        m = SC_TAG_RE.match(str(t).strip())
        if m:
            out.add(".".join(m.groups()))
    return sorted(out, key=lambda s: [int(x) for x in s.split(".")])


def sc_label(sc):
    name = SC_NAMES.get(sc)
    return f"{sc} {name}" if name else sc


def fix_for(finding):
    """Plain-language remediation: curated where we have one, axe's help otherwise."""
    hint = FIX_HINTS.get(finding.get("id"))
    if hint:
        return hint
    helptext = (finding.get("help") or "").strip().rstrip(".")
    desc = (finding.get("description") or "").strip()
    if helptext and desc:
        return f"{helptext}. {desc}"
    return helptext or desc or "Review this element against the referenced success criteria."


# --------------------------------------------------------------------------
# axe invocation + aggregation
# --------------------------------------------------------------------------

AXE_RESULT_KEYS = ("violations", "incomplete", "passes", "inapplicable")


def is_axe_result(data):
    """True when `data` really is an axe run result, not some other JSON that
    happened to land in the output directory. A genuine result always carries
    at least one of axe's outcome buckets."""
    items = data if isinstance(data, list) else [data]
    return any(isinstance(d, dict) and any(k in d for k in AXE_RESULT_KEYS)
               for d in items)


def normalize_axe(data, url=""):
    """The Playwright runner prints one result object per page; a saved
    @axe-core/cli file was an ARRAY of them. Accept either shape.

    `passes` is kept as a count: it is the evidence that rules actually ran on
    the page, which is what separates "clean" from "never audited"."""
    if isinstance(data, list):
        data = next((d for d in data if isinstance(d, dict)), {})
    if not isinstance(data, dict):
        data = {}

    def bucket(key):
        return [v for v in (data.get(key) or []) if isinstance(v, dict)]

    engine = data.get("testEngine")
    version = engine.get("version") if isinstance(engine, dict) else ""
    return {
        "url": data.get("url") or url,
        "violations": bucket("violations"),
        "incomplete": bucket("incomplete"),
        "passes": len(bucket("passes")),
        "engine": version or "",
    }


def _streams(*parts):
    """Join whatever the runner wrote on either stream, skipping empties."""
    return "\n".join(p for p in (str(x or "").strip() for x in parts) if p)


def parse_runner_output(stdout):
    """Pull the runner's JSON payload out of its stdout.

    Returns (payload|None, leftover_text). The runner tags its payload line, so
    anything a dependency happens to print alongside it stays diagnostics
    instead of corrupting the parse; a bare JSON document is accepted too.
    """
    text = (stdout or "").strip()
    if not text:
        return None, ""
    payload, other = None, []
    for line in text.splitlines():
        candidate = line.strip()
        if candidate.startswith(RESULT_MARKER):
            candidate = candidate[len(RESULT_MARKER):].strip()
        elif not candidate.startswith(("{", "[")):
            other.append(line)
            continue
        try:
            payload = json.loads(candidate)
        except ValueError:
            other.append(line)
    if payload is None:
        try:                       # a payload pretty-printed across lines
            return json.loads(text), ""
        except ValueError:
            return None, "\n".join(other) or text
    return payload, "\n".join(other)


def run_axe(url, out_dir, tags=None, index=0, timeout=PER_PAGE_TIMEOUT, runner=None):
    """Run axe-core against one URL through the Playwright runner.

    Returns (normalized_result|None, seconds, why, output). `output` is the
    runner's own diagnostics - never discarded, so a page that fails can say
    why instead of vanishing into a silent "0 pages analyzed".
    """
    tags = list(tags or RUN_TAGS)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"axe_{index:03d}.json"
    runner = runner or runner_for()
    cmd = [NODE, runner.script,
           "--url", url,
           "--tags", ",".join(tags),
           "--timeout", str(timeout)]

    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(out_dir), check=False, timeout=timeout + 30,
                              env=runner.env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as e:
        return (None, int(time.time() - t0), "timeout",
                clean_output(_streams(getattr(e, "output", ""), getattr(e, "stderr", ""))))
    except FileNotFoundError:
        return (None, int(time.time() - t0),
                f"runner not found: {NODE} (Node.js 18+ must be installed and on PATH)", "")
    except Exception as e:
        return None, int(time.time() - t0), f"{type(e).__name__}: {e}", ""

    secs = int(time.time() - t0)
    rc = getattr(proc, "returncode", 0)
    # The payload on stdout - not the exit code - is what says the page was
    # really audited; stderr (plus anything else on stdout) is the diagnostics.
    data, leftover = parse_runner_output(getattr(proc, "stdout", ""))
    output = clean_output(_streams(leftover, getattr(proc, "stderr", "")))

    if data is None:
        why = "no result from the runner"
        if rc:
            why += f"; runner exited {rc}"
        if not output:
            why += ("; the runner produced no output - usually Node.js missing, or its "
                    "npm packages never installed")
        return None, secs, why, output
    if not is_axe_result(data):
        return None, secs, "runner output is not an axe result", output
    try:                            # keep the raw result next to the run, as before
        out_file.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass
    return normalize_axe(data, url), secs, "", output


def _norm_impact(impact):
    """axe impact -> one of IMPACT_ORDER (anything unknown/absent is 'minor')."""
    imp = (impact or "").strip().lower()
    return imp if imp in IMPACT_ORDER else "minor"


def _impact_rank(impact):
    return IMPACT_ORDER.index(_norm_impact(impact))


def _collect(bucket, url, items):
    """Fold one page's axe items into the cross-page rule bucket."""
    for it in items:
        rid = it.get("id") or "unknown"
        entry = bucket.get(rid)
        if entry is None:
            entry = bucket[rid] = {
                "id": rid,
                "impact": _norm_impact(it.get("impact")),
                "help": it.get("help") or rid,
                "description": it.get("description") or "",
                "helpUrl": it.get("helpUrl") or "",
                "tags": list(it.get("tags") or []),
                "pages": {},
                "nodes": 0,
            }
        elif _impact_rank(it.get("impact")) < _impact_rank(entry["impact"]):
            entry["impact"] = _norm_impact(it.get("impact"))  # keep the most severe seen
        for t in it.get("tags") or []:
            if t not in entry["tags"]:
                entry["tags"].append(t)
        elements = entry["pages"].setdefault(url, [])
        for n in it.get("nodes") or []:
            if not isinstance(n, dict):
                continue
            target = n.get("target") or []
            elements.append({
                "target": ", ".join(str(t) for t in target) if isinstance(target, list) else str(target),
                "html": n.get("html") or "",
            })
            entry["nodes"] += 1


def aggregate(results):
    """results: list of normalized axe results.
    Returns (violations, incomplete, engine_version) with rules deduplicated
    across pages and sorted most-severe first."""
    viol, incomp, engine = {}, {}, ""
    for r in results:
        url = r.get("url") or ""
        engine = engine or (r.get("engine") or "")
        _collect(viol, url, r.get("violations") or [])
        _collect(incomp, url, r.get("incomplete") or [])

    def finish(bucket):
        out = list(bucket.values())
        for f in out:
            f["sc"] = sc_from_tags(f["tags"])
        out.sort(key=lambda f: (_impact_rank(f["impact"]), f["id"]))
        return out

    return finish(viol), finish(incomp), engine


def evaluate_standards(findings, selected=None, pages_analyzed=None):
    """Per-standard verdict: filter the violations to the standard's tag set.

    A verdict is only ever "pass" when pages were actually analyzed. An empty
    violation list means one of two very different things - "axe checked the
    pages and found nothing in scope" or "axe never ran" - and only the first
    is a pass. Callers that know the page count pass `pages_analyzed`; 0 turns
    every applicable standard into "unknown" instead of a false green.

    `pages_analyzed=None` means "not tracked" and keeps the plain
    violations-only behaviour, for callers evaluating a known set of findings.
    """
    scanned_nothing = pages_analyzed is not None and pages_analyzed <= 0
    out = []
    for sid in normalize_standards(selected):
        std = STANDARDS_BY_ID[sid]
        row = {"id": sid, "name": std["name"], "region": std["region"],
               "basis": std["basis"], "note": std.get("note", ""),
               "rules": 0, "elements": 0, "pages": 0, "rule_ids": [], "findings": []}
        if not std["applicable"]:
            row["verdict"] = "n/a"
            out.append(row)
            continue
        if scanned_nothing:
            row["verdict"] = "unknown"
            out.append(row)
            continue
        scope = set(std["tags"])
        matched = [f for f in findings if scope & set(f.get("tags") or [])]
        pages = set()
        for f in matched:
            pages |= set(f.get("pages") or {})
            row["elements"] += f.get("nodes", 0)
        row["rules"] = len(matched)
        row["pages"] = len(pages)
        row["rule_ids"] = [f["id"] for f in matched]
        row["findings"] = matched
        row["verdict"] = "fail" if matched else "pass"
        out.append(row)
    return out


def collect_site(site_url, urls, standards=None, concurrency=3, work_dir=None, log=print):
    """Run axe over every URL and return the results as data.

    Same work `scan_site` has always done - this is only the point where the
    run's results are handed back as a dict instead of straight into the HTML
    builder, so the consolidated report can render them too. `scan_site` still
    returns the standalone accessibility page and is unchanged from a caller's
    point of view."""
    standards = normalize_standards(standards)
    tags = run_tags(standards)
    urls = list(urls) or [site_url]
    tmp_holder = None
    if work_dir is None:
        tmp_holder = tempfile.mkdtemp(prefix="axe-")
        work_dir = tmp_holder
    work_dir = Path(work_dir)

    pages_attempted = len(urls)
    log(f"Accessibility: axe-core {AXE_CORE_VERSION} on {pages_attempted} page(s), "
        f"{concurrency} in parallel...")
    runner = ensure_runner(log)

    results, failures = [], []
    seen_output = set()
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=max(1, int(concurrency or 1))) as pool:
        futures = {pool.submit(run_axe, u, work_dir, tags, i, runner=runner): u
                   for i, u in enumerate(urls, 1)}
        for n, fut in enumerate(as_completed(futures), 1):
            u = futures[fut]
            res, secs, why, output = fut.result()
            if res:
                results.append(res)
                log(f"[{n}/{pages_attempted}] a11y OK ({secs}s, {len(res['violations'])} "
                    f"violation rule(s), {res['passes']} check(s) passed) {u}")
                continue
            failures.append({"url": u, "why": why, "output": output})
            log(f"[{n}/{pages_attempted}] a11y FAILED ({why}) {u}")
            # The runner's own words, once per distinct error - the same broken
            # runner on 30 pages should not bury the log 30 times over.
            if output and output not in seen_output:
                seen_output.add(output)
                for line in output.splitlines():
                    log(f"    axe: {line}")
            elif output:
                log("    axe: (same error as an earlier page)")

    pages_analyzed = len(results)
    pages_failed = pages_attempted - pages_analyzed
    checks_passed = sum(r.get("passes", 0) for r in results)
    violations, incomplete, engine = aggregate(results)

    if pages_analyzed == 0:
        log(f"Accessibility: SCAN FAILED - 0 of {pages_attempted} page(s) analyzed. "
            f"No standard can be reported as compliant; the report will show every "
            f"standard as 'Not determined'.")
    else:
        if pages_failed:
            log(f"Accessibility: {pages_failed} of {pages_attempted} page(s) could not be "
                f"analyzed - results below cover the {pages_analyzed} that were.")
        log(f"Accessibility: {pages_analyzed}/{pages_attempted} page(s) analyzed, "
            f"{checks_passed} check(s) passed, {len(violations)} failing rule(s), "
            f"{len(incomplete)} needing manual review.")

    panel = evaluate_standards(violations, standards, pages_analyzed=pages_analyzed)
    return {
        "site_url": site_url,
        "panel": panel,
        "violations": violations,
        "incomplete": incomplete,
        "pages_analyzed": pages_analyzed,
        "pages_attempted": pages_attempted,
        "engine_version": engine or AXE_CORE_VERSION,
        "tags": tags,
        "failures": failures,
        "checks_passed": checks_passed,
        "standards": standards,
    }


def html_from(data):
    """The standalone accessibility page for one `collect_site` result."""
    return build_accessibility_html(
        data.get("site_url", ""), data.get("panel") or [],
        data.get("violations") or [], data.get("incomplete") or [],
        pages_analyzed=data.get("pages_analyzed", 0),
        pages_attempted=data.get("pages_attempted", 0),
        engine_version=data.get("engine_version") or AXE_CORE_VERSION,
        tags=data.get("tags"), failures=data.get("failures"),
        checks_passed=data.get("checks_passed", 0))


def scan_site(site_url, urls, standards=None, concurrency=3, work_dir=None, log=print):
    """Run axe over every URL and return the accessibility report HTML."""
    return html_from(collect_site(site_url, urls, standards=standards,
                                  concurrency=concurrency, work_dir=work_dir, log=log))


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

VERDICT_LABEL = {
    "pass": "Compliant (automated checks)",
    "fail": "Not compliant (automated)",
    "unknown": "Not determined &mdash; scan did not complete",
    "n/a": "Not applicable",
}
VERDICT_CLASS = {"pass": "ok", "fail": "bad", "unknown": "unk", "n/a": "na"}

IMPACT_BLURB = {
    "critical": "Blocks access outright for some users.",
    "serious": "Makes content very hard to use with assistive technology.",
    "moderate": "Causes friction or confusion for some users.",
    "minor": "Minor annoyance or inconsistency.",
}


def _pages_html(pages, scanned):
    """Affected pages + a few offending element selectors/snippets each."""
    if not pages:
        return '<div class="scope">No element detail captured.</div>'
    items = []
    for url in sorted(pages)[:8]:
        elements = pages[url]
        path = urlparse(url).path or "/"
        snips = []
        for el in elements[:3]:
            tgt = html.escape(el.get("target") or "")
            snip = html.escape((el.get("html") or "")[:220])
            block = f'<code class="sel">{tgt}</code>' if tgt else ""
            if snip:
                block += f'<pre class="snip">{snip}</pre>'
            if block:
                snips.append(f"<li>{block}</li>")
        more = (f'<li class="more">+{len(elements) - 3} more element(s) on this page</li>'
                if len(elements) > 3 else "")
        items.append(f'<li class="pg"><b>{html.escape(path)}</b>'
                     f'<span class="cnt">{len(elements)} element(s)</span>'
                     f'<ul class="els">{"".join(snips)}{more}</ul></li>')
    extra = (f'<li class="more">+{len(pages) - 8} more page(s)</li>'
             if len(pages) > 8 else "")
    label = ("all scanned pages" if scanned and len(pages) >= scanned
             else f"{len(pages)} of {scanned} pages" if scanned else f"{len(pages)} page(s)")
    return (f'<div class="scope">Affects {label}</div>'
            f'<ul class="pages">{"".join(items)}{extra}</ul>')


def _std_rules_html(row):
    """The failing rules inside one standard's WCAG scope, with everything a
    developer needs to act: rule id, impact, success criteria, the fix, and
    which pages/elements it fires on."""
    findings = row.get("findings") or []
    if not findings:
        return ""
    items = []
    for f in findings[:12]:
        impact = _norm_impact(f.get("impact"))
        sc = ", ".join(sc_label(s) for s in f.get("sc") or []) or "-"
        pages = f.get("pages") or {}
        paths = sorted({urlparse(u).path or "/" for u in pages})
        where = f'{f.get("nodes", 0)} element(s) on {len(pages)} page(s)'
        if paths:
            where += ": " + ", ".join(paths[:4]) + (", ..." if len(paths) > 4 else "")
        items.append(
            f'<li><code class="rid">{html.escape(f.get("id"))}</code>'
            f'<span class="imp {impact}">{impact}</span>'
            f'<span class="scmini">{html.escape(sc)}</span>'
            f'<div class="sfix"><b>Fix:</b> {html.escape(fix_for(f))}</div>'
            f'<div class="swhere">{html.escape(where)}</div></li>')
    more = (f'<li class="more">+{len(findings) - 12} more failing rule(s) &mdash; see '
            f'&ldquo;Findings by impact&rdquo; below</li>' if len(findings) > 12 else "")
    return f'<div class="srulesh">Failing rules in scope:</div><ul class="srules">{"".join(items)}{more}</ul>'


def _failures_html(failures):
    """Why the pages that could not be analyzed failed, in the runner's own
    words - grouped so one broken runner reads as one problem."""
    if not failures:
        return ""
    groups = {}
    for f in failures:
        key = (f.get("why") or "unknown error", f.get("output") or "")
        groups.setdefault(key, []).append(f.get("url") or "")
    items = []
    for (why, output), urls in list(groups.items())[:6]:
        sample = ", ".join(html.escape(u) for u in urls[:3])
        if len(urls) > 3:
            sample += f" (+{len(urls) - 3} more)"
        detail = f'<pre class="snip">{html.escape(output)}</pre>' if output else ""
        items.append(f'<li><b>{html.escape(why)}</b> &middot; {len(urls)} page(s)'
                     f'<div class="scope">{sample}</div>{detail}</li>')
    extra = (f'<li class="more">+{len(groups) - 6} more distinct error(s)</li>'
             if len(groups) > 6 else "")
    return (f'<div class="failh">Why pages failed</div>'
            f'<ul class="fails">{"".join(items)}{extra}</ul>')


def _finding_card(f, scanned, manual=False):
    sc = ", ".join(sc_label(s) for s in f.get("sc") or []) or "-"
    tags = ", ".join(t for t in (f.get("tags") or []) if not t.startswith("cat."))
    impact = _norm_impact(f.get("impact"))
    helpurl = f.get("helpUrl") or ""
    link = (f'<a class="hu" href="{html.escape(helpurl)}" target="_blank" rel="noopener">axe rule reference &#8599;</a>'
            if helpurl else "")
    fix_label = "What to check" if manual else "Fix"
    return (
        f'<div class="finding {impact}">'
        f'<div class="fh"><span class="imp {impact}">{impact}</span>'
        f'<span class="ft">{html.escape(f.get("help") or f.get("id"))}</span>'
        f'<code class="rid">{html.escape(f.get("id"))}</code></div>'
        f'<div class="sc"><b>WCAG success criteria:</b> {html.escape(sc)}</div>'
        f'<div class="tags">{html.escape(tags)}</div>'
        f'<div class="rec"><b>{fix_label}:</b> {html.escape(fix_for(f))} {link}</div>'
        f'{_pages_html(f.get("pages") or {}, scanned)}'
        f'</div>')


def build_accessibility_html(site_url, panel, violations, incomplete,
                             pages_analyzed=0, pages_attempted=0,
                             engine_version=AXE_CORE_VERSION, tags=None,
                             failures=None, checks_passed=0):
    gen = datetime.now().strftime("%Y-%m-%d %H:%M")
    tags = list(tags or RUN_TAGS)
    failures = list(failures or [])
    pages_failed = max(0, pages_attempted - pages_analyzed)
    analyzed = pages_analyzed > 0

    # --- per-standard compliance panel ---
    cards = []
    for row in panel:
        cls = VERDICT_CLASS[row["verdict"]]
        if row["verdict"] == "n/a":
            detail = html.escape(row.get("note") or "Not testable by automated page checks.")
        elif row["verdict"] == "unknown":
            detail = ("axe-core did not analyze a single page, so this standard was never "
                      "evaluated. Treat this as no result &mdash; not as a pass.")
        elif row["verdict"] == "pass":
            detail = (f'Zero violations within this standard&rsquo;s WCAG scope across the '
                      f'{pages_analyzed} page(s) analyzed.')
        else:
            detail = (f'{row["rules"]} failing rule(s) &middot; {row["elements"]} element(s) '
                      f'&middot; {row["pages"]} page(s) within this standard&rsquo;s WCAG scope')
        cards.append(
            f'<div class="std {cls}">'
            f'<div class="sh"><span class="verdict {cls}">{VERDICT_LABEL[row["verdict"]]}</span>'
            f'<b>{html.escape(row["name"])}</b>'
            f'<span class="reg">{html.escape(row["region"])}</span></div>'
            f'<div class="basis">Scope: {html.escape(row["basis"])}</div>'
            f'<div class="sdetail">{detail}</div>{_std_rules_html(row)}</div>')
    panel_html = "\n".join(cards) or '<p class="note">No standards selected.</p>'

    # --- run summary: a failed scan says so before anything else ---
    if not analyzed:
        summary = (f'<div class="alert"><b>Scan failed &mdash; 0 of {pages_attempted} page(s) '
                   f'analyzed.</b> axe-core produced no result for any queued page, so this '
                   f'report contains no accessibility data at all. Every standard below reads '
                   f'<b>Not determined</b>: none of them passed, and the empty findings list '
                   f'means &ldquo;nothing was measured&rdquo;, not &ldquo;nothing is '
                   f'wrong&rdquo;. Fix the errors below and re-run before drawing any '
                   f'conclusion.</div>{_failures_html(failures)}')
    elif failures:
        summary = (f'<div class="warn"><b>Partial scan &mdash; {pages_failed} of '
                   f'{pages_attempted} page(s) could not be analyzed.</b> Everything below '
                   f'describes only the {pages_analyzed} page(s) that were; the pages that '
                   f'failed are unmeasured, not clean.</div>{_failures_html(failures)}')
    else:
        summary = ""

    # --- findings grouped by impact ---
    counts = {i: 0 for i in IMPACT_ORDER}
    for f in violations:
        counts[_norm_impact(f.get("impact"))] += 1

    if not analyzed:
        findings_html = ('<p class="nodata">No pages were analyzed, so no findings could be '
                         'collected. This is not a clean result &mdash; the scan failed.</p>')
        manual_html = ('<p class="nodata">No pages were analyzed, so nothing could be flagged '
                       'for manual review.</p>')
        manual_count = ""
    else:
        groups = []
        for impact in IMPACT_ORDER:
            group = [f for f in violations if _norm_impact(f.get("impact")) == impact]
            if not group:
                continue
            body = "\n".join(_finding_card(f, pages_analyzed) for f in group)
            groups.append(
                f'<h3 class="grp {impact}">{impact.title()} '
                f'<span class="gc">{len(group)} rule(s)</span>'
                f'<span class="gb">{IMPACT_BLURB[impact]}</span></h3>{body}')
        findings_html = "\n".join(groups) or \
            (f'<p class="clean">No violations detected by the automated checks on the '
             f'{pages_analyzed} page(s) analyzed ({checks_passed} check(s) passed).</p>')

        manual_html = "\n".join(_finding_card(f, pages_analyzed, manual=True) for f in incomplete) or \
            '<p class="clean">Nothing flagged for manual review on the scanned pages.</p>'
        inc_elements = sum(f.get("nodes", 0) for f in incomplete)
        inc_pages = len({u for f in incomplete for u in (f.get("pages") or {})})
        manual_count = (f'<p class="note"><b>{len(incomplete)} rule(s)</b> across '
                        f'<b>{inc_elements} element(s)</b> on <b>{inc_pages} page(s)</b> '
                        f'need a human decision.</p>' if incomplete else "")

    # --- headline numbers ---
    dash = "&mdash;"

    def stat(value, label, cls):
        return f'<div class="stat {cls}"><b>{value}</b><span>{label}</span></div>'

    stat_cards = stat(f"{pages_analyzed}/{pages_attempted}", "pages analyzed", "pages")
    if pages_failed:
        stat_cards += stat(pages_failed, "pages failed", "failed")
    for i in IMPACT_ORDER:
        stat_cards += stat(counts[i] if analyzed else dash, i, i)
    stat_cards += stat(len(incomplete) if analyzed else dash, "manual review", "manual")
    stat_cards += stat(checks_passed if analyzed else dash, "checks passed", "passed")

    failed_meta = f' &middot; {pages_failed} page(s) failed' if pages_failed else ""

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Accessibility Report (Automated checks) - {html.escape(site_url)}</title>
<style>
  :root {{ --ink:#1c2430; --line:#e3e6ea; --bg:#f6f7f9; --card:#fff; --muted:#5b6672;
           --critical:#b3122b; --serious:#d6521f; --moderate:#b06f00; --minor:#3b7dbf;
           --ok:#0a7d34; --bad:#b3122b; --na:#5b6672; --unk:#9b2c2c; --manual:#6b4fc4; }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; margin:0;
          background:var(--bg); color:var(--ink); line-height:1.5; }}
  .wrap {{ max-width:1040px; margin:0 auto; padding:32px 20px 64px; }}
  h1 {{ font-size:23px; margin:0 0 4px; }}
  h2 {{ margin:36px 0 10px; font-size:18px; }}
  .meta {{ color:var(--muted); font-size:13px; margin-bottom:14px; }}
  .banner {{ background:#fbf6e6; border:1px solid #efe1b0; color:#7a5c00; border-radius:10px;
             padding:12px 16px; font-size:13px; margin:14px 0 4px; }}
  .alert {{ background:#fdf0f1; border:1px solid #f0c2c7; border-left:5px solid var(--bad);
            color:#7d1223; border-radius:10px; padding:13px 16px; font-size:13.5px; margin:14px 0 4px; }}
  .warn {{ background:#fff7ec; border:1px solid #f2d9b0; border-left:5px solid var(--moderate);
           color:#7a4d00; border-radius:10px; padding:13px 16px; font-size:13.5px; margin:14px 0 4px; }}
  .failh {{ font-size:13px; font-weight:700; margin:12px 0 2px; color:var(--bad); }}
  ul.fails {{ margin:4px 0 0; padding-left:18px; font-size:12.5px; }}
  ul.fails li {{ margin-bottom:8px; }}
  .stats {{ display:flex; gap:12px; margin:18px 0 8px; flex-wrap:wrap; }}
  .stat {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
           padding:12px 16px; min-width:104px; text-align:center; }}
  .stat b {{ display:block; font-size:24px; }}
  .stat span {{ font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }}
  .stat.critical b {{ color:var(--critical); }} .stat.serious b {{ color:var(--serious); }}
  .stat.moderate b {{ color:var(--moderate); }} .stat.minor b {{ color:var(--minor); }}
  .stat.manual b {{ color:var(--manual); }} .stat.passed b {{ color:var(--ok); }}
  .stat.pages b {{ color:var(--ink); }} .stat.failed b {{ color:var(--bad); }}
  .stds {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:12px; }}
  .std {{ background:var(--card); border:1px solid var(--line); border-left:5px solid var(--line);
          border-radius:10px; padding:13px 16px; }}
  .std.ok {{ border-left-color:var(--ok); }} .std.bad {{ border-left-color:var(--bad); }}
  .std.na {{ border-left-color:var(--na); }}
  .std.unk {{ border-left-color:var(--unk); background:#fdf7f7; }}
  .sh {{ display:flex; align-items:center; gap:9px; flex-wrap:wrap; }}
  .sh b {{ font-size:15px; }}
  .verdict {{ font-size:10.5px; font-weight:700; text-transform:uppercase; letter-spacing:.04em;
              padding:2px 9px; border-radius:20px; color:#fff; }}
  .verdict.ok {{ background:var(--ok); }} .verdict.bad {{ background:var(--bad); }}
  .verdict.na {{ background:var(--na); }} .verdict.unk {{ background:var(--unk); }}
  .reg {{ font-size:11px; background:#eef1f4; color:var(--muted); padding:1px 8px; border-radius:20px; }}
  .basis {{ font-size:12.5px; color:var(--muted); margin-top:6px; }}
  .sdetail {{ font-size:13px; margin-top:4px; }}
  .srulesh {{ font-size:12px; font-weight:700; color:var(--muted); margin-top:9px;
              text-transform:uppercase; letter-spacing:.03em; }}
  ul.srules {{ margin:4px 0 0; padding-left:16px; font-size:12.5px; }}
  ul.srules li {{ margin-bottom:8px; word-break:break-word; }}
  .scmini {{ font-size:11.5px; color:var(--muted); margin-left:6px; }}
  .sfix {{ margin-top:3px; }}
  .swhere {{ color:var(--muted); margin-top:2px; }}
  .grp {{ margin:26px 0 8px; font-size:15px; text-transform:capitalize; }}
  .grp.critical {{ color:var(--critical); }} .grp.serious {{ color:var(--serious); }}
  .grp.moderate {{ color:var(--moderate); }} .grp.minor {{ color:var(--minor); }}
  .gc {{ font-size:12px; font-weight:600; color:var(--muted); margin-left:8px; }}
  .gb {{ display:block; font-size:12px; font-weight:400; color:var(--muted); }}
  .finding {{ background:var(--card); border:1px solid var(--line); border-left:5px solid var(--line);
              border-radius:10px; padding:14px 18px; margin:12px 0; }}
  .finding.critical {{ border-left-color:var(--critical); }}
  .finding.serious {{ border-left-color:var(--serious); }}
  .finding.moderate {{ border-left-color:var(--moderate); }}
  .finding.minor {{ border-left-color:var(--minor); }}
  .fh {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
  .imp {{ font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.04em;
          padding:2px 9px; border-radius:20px; color:#fff; }}
  .imp.critical {{ background:var(--critical); }} .imp.serious {{ background:var(--serious); }}
  .imp.moderate {{ background:var(--moderate); }} .imp.minor {{ background:var(--minor); }}
  .ft {{ font-weight:600; font-size:15px; }}
  .rid {{ font-size:11.5px; background:#eef1f4; color:var(--muted); padding:1px 7px; border-radius:5px; }}
  .sc {{ font-size:13px; margin-top:8px; }}
  .tags {{ font-size:11.5px; color:var(--muted); margin-top:2px; word-break:break-word; }}
  .rec {{ font-size:14px; margin-top:8px; }}
  .hu {{ font-size:12px; color:#0a58ca; text-decoration:none; white-space:nowrap; }}
  .scope {{ font-size:13px; color:var(--muted); margin-top:10px; }}
  ul.pages {{ margin:6px 0 0; padding-left:18px; font-size:12.5px; }}
  ul.pages .pg {{ margin-bottom:6px; }}
  ul.pages .cnt {{ color:var(--muted); margin-left:8px; font-size:11.5px; }}
  ul.els {{ list-style:none; margin:4px 0 0; padding:0; }}
  ul.els li {{ margin:3px 0; }}
  .sel {{ font-size:11.5px; background:#eef1f4; padding:1px 6px; border-radius:5px; word-break:break-all; }}
  .snip {{ margin:3px 0 0; padding:6px 8px; background:#0f172a; color:#c7d2e6; border-radius:6px;
           font-size:11px; white-space:pre-wrap; word-break:break-all; overflow:auto; }}
  .more {{ color:var(--muted); }}
  .clean {{ color:var(--ok); font-size:15px; }}
  .nodata {{ color:var(--bad); font-size:15px; font-weight:600; }}
  .note {{ color:var(--muted); font-size:13px; }}
  @media print {{ body {{ background:#fff; }} .finding, .std {{ break-inside:avoid; }} }}
</style></head>
<body><div class="wrap">
  <h1>Accessibility Report &mdash; Automated checks</h1>
  <div class="meta">{html.escape(site_url)} &middot; {pages_analyzed} of {pages_attempted} page(s) analyzed{failed_meta}
    &middot; axe-core {html.escape(str(engine_version))} &middot; Generated {gen}</div>
  {summary}
  <div class="banner"><b>Automated checks only.</b> These results come from axe-core rules run
    against the rendered pages. Automated testing can surface only a portion of WCAG failures
    (many criteria require human judgement), so this report describes automated check results
    &mdash; it is not a legal compliance determination or a certification for any standard listed below.</div>
  <div class="meta">axe tags evaluated: {html.escape(", ".join(tags))}</div>
  <div class="stats">{stat_cards}</div>

  <h2>Standards &mdash; automated check results</h2>
  <div class="stds">{panel_html}</div>

  <h2>Findings by impact</h2>
  {findings_html}

  <h2>Needs manual review</h2>
  <p class="note">axe could not decide these automatically &mdash; a person has to confirm them.</p>
  {manual_count}
  {manual_html}

  <p class="meta" style="margin-top:26px">Generated with axe-core
    {html.escape(str(engine_version))} (pinned {AXE_CORE_VERSION}). Automated checks only &mdash;
    pair them with manual keyboard, screen-reader and cognitive-load testing before making any
    accessibility conformance claim.</p>
</div></body></html>"""


def main():
    ap = argparse.ArgumentParser(description="Automated accessibility (axe-core) scan.")
    ap.add_argument("site", nargs="?")
    ap.add_argument("-o", "--output", default="accessibility.html")
    ap.add_argument("--standards", default=",".join(DEFAULT_STANDARDS),
                    help="comma-separated standard ids (see --list-standards)")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--list-standards", action="store_true")
    args = ap.parse_args()

    if args.list_standards:
        for s in STANDARDS:
            flag = "" if s["applicable"] else "  (not applicable)"
            print(f'{s["id"]:<12} {s["name"]} - {s["basis"]}{flag}')
        return
    if not args.site:
        ap.error("a site URL is required")

    site = args.site if urlparse(args.site).scheme else "https://" + args.site
    out = scan_site(site, [site], standards=args.standards, concurrency=args.concurrency)
    Path(args.output).write_text(out, encoding="utf-8")
    print(f"Accessibility report -> {args.output}")


if __name__ == "__main__":
    main()
