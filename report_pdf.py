#!/usr/bin/env python3
"""
PDF export via paged.js, replacing the plain Chrome print-to-PDF.

Why paged.js and not WeasyPrint
-------------------------------
Both implement the CSS Paged Media features this report needs - @page margin
boxes, running headers via string-set, page counters, break control. The
deciding factors were:

* One rendering engine. Chrome is already a hard requirement here (Lighthouse
  drives it, and the tool has always shelled out to it for PDFs), so paged.js
  paginates the exact HTML the client sees in the browser, with the same
  engine. WeasyPrint would introduce a second engine whose CSS support differs
  from Chrome's - grid, modern colour syntax and SVG text metrics all render
  differently - so the HTML and the PDF would drift apart. "Renders identically
  in HTML and PDF" is a requirement of this report, and one engine is the only
  way to actually get it.
* No new native dependencies. WeasyPrint needs Pango, HarfBuzz and cairo; on
  Windows, where this dashboard is mostly run, that means installing a GTK
  runtime. paged.js is a single JavaScript file.
* The polyfill is optional at runtime. When it is missing we still produce a
  PDF through Chrome's own pagination - a graceful degradation WeasyPrint
  could not offer, since a missing WeasyPrint means no PDF at all.

The cost is honest: paged.js is slower than WeasyPrint (it lays out every page
in a real browser) and it is a JS dependency to keep around. Both are
acceptable for a report that is generated once at the end of a scan that
already takes minutes.

Resolution order for the polyfill:
    1. $PAGEDJS_POLYFILL              (an explicit file path)
    2. vendor/pagedjs/paged.polyfill.min.js next to this file
    3. an earlier install in the cache dir ($PAGEDJS_HOME or ~/.pagedjs-runner)
    4. npm install pagedjs into that cache dir, once per machine
Set PAGEDJS_AUTO_INSTALL=0 to stop at step 3.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

POLYFILL_ENV = "PAGEDJS_POLYFILL"
HOME_ENV = "PAGEDJS_HOME"
AUTO_INSTALL_ENV = "PAGEDJS_AUTO_INSTALL"
PAGEDJS_VERSION = "0.4.3"

VENDOR_POLYFILL = APP_DIR / "vendor" / "pagedjs" / "paged.polyfill.min.js"
POLYFILL_RELPATH = Path("node_modules") / "pagedjs" / "dist" / "paged.polyfill.min.js"

NPM = "npm.cmd" if os.name == "nt" else "npm"
INSTALL_TIMEOUT = 600
RENDER_TIMEOUT = 300

# paged.js lays out page by page; Chrome must not snapshot before it finishes.
# The polyfill sets `pagedjs-ready` on <html>, and the virtual time budget
# gives it room to get there on a long report.
VIRTUAL_TIME_BUDGET_MS = 90_000

READY_HOOK = """
<script>
  window.PagedConfig = {
    auto: true,
    before: function () {
      // filters and collapsibles must never hide content on paper
      if (window.__reportOpenAll) { window.__reportOpenAll(); }
    },
    after: function () { document.documentElement.classList.add('pagedjs-ready'); }
  };
</script>
"""


def _log(log, message):
    if log:
        log(message)


def pagedjs_home():
    env = os.environ.get(HOME_ENV, "").strip()
    if env:
        return Path(env)
    return Path.home() / ".pagedjs-runner"


def _installed_polyfill(home):
    return Path(home) / POLYFILL_RELPATH


def install_pagedjs(home, log=None):
    """npm install pagedjs into a cache dir - once per machine."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    if not (home / "package.json").exists():
        (home / "package.json").write_text(
            '{"name":"pagedjs-runner","private":true,"version":"1.0.0"}\n', encoding="utf-8")
    if not shutil.which(NPM) and not shutil.which("npm"):
        raise RuntimeError("npm is not on PATH, so paged.js cannot be installed")
    _log(log, f"Installing pagedjs@{PAGEDJS_VERSION} into {home} (first run only)...")
    subprocess.run(
        [shutil.which(NPM) or "npm", "install", "--no-audit", "--no-fund",
         "--loglevel=error", f"pagedjs@{PAGEDJS_VERSION}"],
        cwd=str(home), check=True, timeout=INSTALL_TIMEOUT,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    return _installed_polyfill(home)


def find_pagedjs(log=None, auto_install=None):
    """The polyfill's path, or None when it cannot be had."""
    explicit = os.environ.get(POLYFILL_ENV, "").strip()
    if explicit and Path(explicit).is_file():
        return Path(explicit)
    if VENDOR_POLYFILL.is_file():
        return VENDOR_POLYFILL
    home = pagedjs_home()
    cached = _installed_polyfill(home)
    if cached.is_file():
        return cached
    if auto_install is None:
        auto_install = os.environ.get(AUTO_INSTALL_ENV, "1") != "0"
    if not auto_install:
        return None
    try:
        installed = install_pagedjs(home, log=log)
    except Exception as exc:
        _log(log, f"(paged.js unavailable: {exc})")
        return None
    return installed if installed.is_file() else None


def build_print_document(html_text, polyfill_text):
    """The same report, with paged.js inlined so the print pass needs no
    network and no sibling files."""
    injection = READY_HOOK + f"<script>{polyfill_text}</script>"
    lowered = html_text.lower()
    idx = lowered.rfind("</body>")
    if idx == -1:
        return html_text + injection
    return html_text[:idx] + injection + html_text[idx:]


def find_chrome():
    """A Chromium-based browser to print with."""
    la = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("PROGRAMFILES", r"C:\\Program Files")
    pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\\Program Files (x86)")
    env = os.environ.get("CHROME_PATH", "").strip()
    candidates = [
        env,
        rf"{pf}\Google\Chrome\Application\chrome.exe",
        rf"{pf86}\Google\Chrome\Application\chrome.exe",
        rf"{la}\Google\Chrome\Application\chrome.exe",
        rf"{pf}\Microsoft\Edge\Application\msedge.exe",
        rf"{pf86}\Microsoft\Edge\Application\msedge.exe",
        rf"{pf}\BraveSoftware\Brave-Browser\Application\brave.exe",
        rf"{pf86}\BraveSoftware\Brave-Browser\Application\brave.exe",
        "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for cand in candidates:
        if cand and os.path.exists(cand):
            return cand
    for name in ("chrome", "google-chrome", "chromium", "chromium-browser", "msedge", "brave"):
        found = shutil.which(name)
        if found:
            return found
    # the Chromium Playwright brings along for the accessibility scan
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip(),
             str(Path.home() / ".cache" / "ms-playwright"),
             str(Path.home() / "AppData" / "Local" / "ms-playwright")]
    patterns = ["chromium-*/chrome-linux/chrome",
                "chromium-*/chrome-win/chrome.exe",
                "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium"]
    for root in [r for r in roots if r and Path(r).is_dir()]:
        for pattern in patterns:
            for found in sorted(Path(root).glob(pattern), reverse=True):
                if found.exists():
                    return str(found)
    return None


def _print_with_chrome(chrome, source_path, pdf_path, paged):
    args = [
        chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--no-pdf-header-footer", "--run-all-compositor-stages-before-draw",
        f"--print-to-pdf={os.path.abspath(pdf_path)}",
    ]
    if paged:
        # let the polyfill finish laying out every page before the snapshot
        args.append(f"--virtual-time-budget={VIRTUAL_TIME_BUDGET_MS}")
    args.append("file:///" + os.path.abspath(source_path).replace("\\", "/"))
    subprocess.run(args, check=True, timeout=RENDER_TIMEOUT,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def html_to_pdf(html_path, pdf_path, log=None, keep_print_html=False):
    """Render `html_path` to `pdf_path`.

    The paginated pass writes a sibling ".print.html" first: paged.js is
    inlined into it, but every other relative link (the per-page Lighthouse
    reports) still resolves, because it sits in the same directory.
    """
    html_path = Path(html_path)
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError("No Chrome/Edge/Brave found for PDF export. "
                           "Install one, or skip the PDF.")

    polyfill = find_pagedjs(log=log)
    if not polyfill:
        _log(log, "paged.js not available - falling back to Chrome's own pagination "
                  "(no running headers or page numbers).")
        _print_with_chrome(chrome, html_path, pdf_path, paged=False)
        return {"engine": "chrome", "polyfill": None}

    print_html = html_path.with_suffix(".print.html")
    try:
        document = build_print_document(
            html_path.read_text(encoding="utf-8"),
            Path(polyfill).read_text(encoding="utf-8"))
        print_html.write_text(document, encoding="utf-8")
        _log(log, f"Paginating with paged.js ({polyfill.name})...")
        _print_with_chrome(chrome, print_html, pdf_path, paged=True)
    finally:
        if print_html.exists() and not keep_print_html:
            try:
                print_html.unlink()
            except OSError:                                      # pragma: no cover
                pass
    return {"engine": "pagedjs", "polyfill": str(polyfill)}


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Render a report HTML file to PDF via paged.js")
    ap.add_argument("html")
    ap.add_argument("-o", "--output", default="")
    ap.add_argument("--keep", action="store_true", help="keep the intermediate .print.html")
    args = ap.parse_args()
    out = args.output or str(Path(args.html).with_suffix(".pdf"))
    info = html_to_pdf(args.html, out, log=lambda m: print(m, file=sys.stderr),
                       keep_print_html=args.keep)
    print(f"{out} ({info['engine']})")


if __name__ == "__main__":
    main()
