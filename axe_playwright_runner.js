#!/usr/bin/env node
'use strict';

/**
 * Run axe-core against ONE page and print the result as JSON on stdout.
 *
 * Driven by accessibility_scan.py, one process per page:
 *
 *     node axe_playwright_runner.js --url https://example.com/ \
 *          --tags wcag2a,wcag2aa --timeout 150
 *
 * With --browser-stdin, a JSON object of Playwright newContext() options is
 * read from stdin and used to build the browsing context: cookies, HTTP
 * credentials, a device viewport, a User-Agent. It arrives on stdin rather
 * than as an argument because it can carry somebody's password, and a command
 * line is readable by every process on the machine.
 *
 * Two keys in that object are not context options and are lifted out:
 * `blockPatterns`, installed as a request-interception route so those requests
 * never load, and `launchArgs`, flags for the browser process itself (the DNS
 * override lives there - resolution happens below the browsing context).
 *
 * Why Playwright instead of @axe-core/cli: the CLI drives Chrome through
 * ChromeDriver, and the npm `chromedriver` package is versioned independently
 * of the Chrome installed on the machine. Chrome auto-updates, the driver does
 * not, and every page then dies with
 *
 *     session not created: This version of ChromeDriver only supports
 *     Chrome version 152 / Current browser version is 151
 *
 * Playwright ships its own Chromium build and the matching protocol client in
 * the same package, so the pair cannot drift apart and a Chrome update on the
 * host cannot break the scan.
 *
 * Contract with the Python side:
 *   - stdout carries exactly one line: RESULT_MARKER followed by the JSON
 *     result. Nothing else is ever written to stdout, so a stray log line from
 *     a dependency cannot corrupt the payload.
 *   - every diagnostic - including the reason a page failed - goes to stderr,
 *     which the scanner logs verbatim.
 *   - exit 0 means "this page was audited"; any non-zero exit means it was not.
 */

const { installBlocking, launchArgs } = require('./scan_intercept.js');

const RESULT_MARKER = '__AXE_RESULT__';
const DEFAULT_TIMEOUT_S = 150;
const SETTLE_TIMEOUT_MS = 15000;   // cap on waiting for a page to go quiet

function arg(name, fallback) {
  const flag = `--${name}`;
  const i = process.argv.indexOf(flag);
  if (i !== -1 && i + 1 < process.argv.length) return process.argv[i + 1];
  const inline = process.argv.find((a) => a.startsWith(`${flag}=`));
  return inline ? inline.slice(flag.length + 1) : fallback;
}

function has(name) {
  const flag = `--${name}`;
  return process.argv.some((a) => a === flag || a.startsWith(`${flag}=`));
}

function die(message, code) {
  process.stderr.write(`${message}\n`);
  process.exit(code === undefined ? 1 : code);
}

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (chunk) => { data += chunk; });
    process.stdin.on('end', () => resolve(data));
    process.stdin.on('error', reject);
  });
}

/**
 * The browsing context this page is audited in. `cookies`, `blockPatterns`
 * and `launchArgs` are lifted out: the first needs context.addCookies() after
 * the context exists, the second becomes a route on it, and the third belongs
 * to the browser process. `httpCredentials` is a newContext() option like the
 * rest.
 */
async function browserOptions() {
  if (!has('browser-stdin')) return { options: {}, cookies: [], block: [], args: [] };
  let config;
  try {
    config = JSON.parse(await readStdin());
  } catch (err) {
    die('browser context on stdin is not valid JSON');
  }
  const { cookies, blockPatterns, launchArgs: args, ...options } = config || {};
  return {
    options,
    cookies: Array.isArray(cookies) ? cookies : [],
    block: Array.isArray(blockPatterns) ? blockPatterns : [],
    args: Array.isArray(args) ? args : [],
  };
}

function load(name) {
  try {
    return require(name);
  } catch (err) {
    die(
      `cannot load ${name}: ${err && err.message ? err.message : err}\n` +
      'Install it with: npm install @axe-core/playwright playwright'
    );
  }
}

/**
 * axe's own result, slimmed for the pipe: `violations` and `incomplete` are
 * what the report renders and are kept whole; `passes` only ever gets counted
 * (it is the evidence that rules really ran), so its element payloads - often
 * megabytes on a large page - are dropped. `inapplicable` is unused.
 */
function slim(results, url) {
  return {
    url: results.url || url,
    timestamp: results.timestamp || new Date().toISOString(),
    testEngine: results.testEngine || null,
    violations: results.violations || [],
    incomplete: results.incomplete || [],
    passes: (results.passes || []).map((p) => ({ id: p.id })),
    inapplicable: [],
  };
}

async function main() {
  const url = arg('url');
  if (!url) die('no --url given');

  const tags = String(arg('tags', ''))
    .split(',')
    .map((t) => t.trim())
    .filter(Boolean);
  const seconds = Number(arg('timeout', DEFAULT_TIMEOUT_S));
  const timeout = Math.max(1, Number.isFinite(seconds) ? seconds : DEFAULT_TIMEOUT_S) * 1000;

  const { chromium } = load('playwright');
  const axeModule = load('@axe-core/playwright');
  const AxeBuilder = axeModule.AxeBuilder || axeModule.default;
  if (typeof AxeBuilder !== 'function') die('@axe-core/playwright exports no AxeBuilder');

  // Read the browser payload before launching: it decides the launch flags.
  const { options, cookies, block, args } = await browserOptions();

  let browser;
  try {
    browser = await chromium.launch({
      headless: true,
      // Same relaxations the ChromeDriver runner used: containers have no
      // sandbox, CI boxes have no GPU, and a site with a broken certificate
      // chain should still be audited rather than skipped - plus whatever the
      // scan configuration asked the browser process itself for.
      args: launchArgs(args),
    });
  } catch (err) {
    die(
      `could not launch Playwright's Chromium: ${err && err.message ? err.message : err}\n` +
      'Install the browser with: npx playwright install chromium'
    );
  }

  try {
    const context = await browser.newContext(
      Object.assign({ ignoreHTTPSErrors: true }, options)
    );
    // Blocked URLs are aborted at the network layer, before the page can spend
    // anything on them.
    await installBlocking(context, block);
    if (cookies.length) {
      const origin = new URL(url).origin;
      await context
        .addCookies(cookies.map((c) => Object.assign({ url: origin }, c)))
        .catch(() => {});
    }
    const page = await context.newPage();
    page.setDefaultTimeout(timeout);
    page.setDefaultNavigationTimeout(timeout);

    await page.goto(url, { waitUntil: 'load', timeout });
    // Let a client-rendered page settle, but never hang on one that keeps a
    // socket open forever (analytics, chat widgets, polling).
    await page
      .waitForLoadState('networkidle', { timeout: Math.min(timeout, SETTLE_TIMEOUT_MS) })
      .catch(() => {});

    let builder = new AxeBuilder({ page });
    if (tags.length) builder = builder.withTags(tags);
    const results = await builder.analyze();

    process.stdout.write(`${RESULT_MARKER}${JSON.stringify(slim(results, url))}\n`);
  } finally {
    await browser.close().catch(() => {});
  }
}

main().catch((err) => {
  die(`axe run failed: ${err && err.stack ? err.stack : err}`);
});
