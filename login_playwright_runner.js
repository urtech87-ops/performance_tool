#!/usr/bin/env node
'use strict';

/**
 * Sign in to a site once, and print the cookies the session left behind.
 *
 * Driven by scanlogin.py:
 *
 *     echo '<json>' | node login_playwright_runner.js
 *
 * Why the configuration arrives on **stdin** and never as arguments: this
 * script is handed somebody else's password. A command line is visible to every
 * process on the machine (`ps`, /proc/<pid>/cmdline); a pipe is not. Nothing
 * secret is ever written to disk here either - the browser profile is
 * Playwright's own throwaway temp context, and the only thing this script
 * emits is the cookie jar the login produced, on stdout, for the Python side
 * to hold in memory.
 *
 * Input (one JSON object):
 *   {
 *     "url": "https://site/login",       // where the form is
 *     "userSelector": "#username",       // CSS selectors for the three fields
 *     "passSelector": "#password",
 *     "submitSelector": "button[type=submit]",
 *     "username": "...", "password": "...",
 *     "timeout": 60,                     // seconds for the whole login
 *     "context": { ... },                // Playwright newContext() options,
 *                                        // plus blockPatterns / launchArgs
 *     "httpCredentials": {"username": "...", "password": "..."}
 *   }
 *
 * The sign-in runs under the same conditions as the scan that follows it: the
 * same blocked URLs and the same DNS override, so the session it produces
 * belongs to the server the scan is about to measure.
 *
 * Output contract, identical in spirit to axe_playwright_runner.js:
 *   - stdout carries exactly one line: RESULT_MARKER followed by
 *     {"cookies": [{name, value, domain, path}, ...], "url": "<landed on>"}.
 *   - every diagnostic goes to stderr. A diagnostic never contains the
 *     password: only selectors, URLs and the browser's own error text.
 *   - exit 0 means "the login ran"; non-zero means it did not.
 */

const { installBlocking, launchArgs } = require('./scan_intercept.js');

const RESULT_MARKER = '__LOGIN_RESULT__';
const DEFAULT_TIMEOUT_S = 60;
const SETTLE_TIMEOUT_MS = 15000;

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

function load(name) {
  try {
    return require(name);
  } catch (err) {
    die(
      `cannot load ${name}: ${err && err.message ? err.message : err}\n` +
      'Install it with: npm install playwright'
    );
  }
}

async function main() {
  const raw = await readStdin();
  let config;
  try {
    config = JSON.parse(raw);
  } catch (err) {
    die('login config on stdin is not valid JSON');
  }
  if (!config || !config.url) die('no login url given');
  for (const key of ['userSelector', 'passSelector', 'submitSelector']) {
    if (!config[key]) die(`no ${key} given`);
  }

  const seconds = Number(config.timeout);
  const timeout = Math.max(1, Number.isFinite(seconds) ? seconds : DEFAULT_TIMEOUT_S) * 1000;

  const {
    blockPatterns, launchArgs: extraArgs, ...contextOptions
  } = config.context || {};

  const { chromium } = load('playwright');
  let browser;
  try {
    browser = await chromium.launch({
      headless: true,
      args: launchArgs(extraArgs),
    });
  } catch (err) {
    die(
      `could not launch Playwright's Chromium: ${err && err.message ? err.message : err}\n` +
      'Install the browser with: npx playwright install chromium'
    );
  }

  try {
    const options = Object.assign({ ignoreHTTPSErrors: true }, contextOptions);
    // Basic auth is applied to the login request too: plenty of sites put a
    // form behind an .htpasswd wall on a staging box.
    if (config.httpCredentials) options.httpCredentials = config.httpCredentials;
    const context = await browser.newContext(options);
    await installBlocking(context, blockPatterns);
    if (Array.isArray(config.cookies) && config.cookies.length) {
      // Cookies the user pasted (a consent banner, usually) are in place
      // before the login form is ever loaded.
      const origin = new URL(config.url).origin;
      await context.addCookies(
        config.cookies.map((c) => Object.assign({ url: origin }, c))
      ).catch(() => {});
    }
    const page = await context.newPage();
    page.setDefaultTimeout(timeout);
    page.setDefaultNavigationTimeout(timeout);

    await page.goto(config.url, { waitUntil: 'load', timeout });
    await page.fill(config.userSelector, String(config.username || ''));
    await page.fill(config.passSelector, String(config.password || ''));

    // Most login forms navigate; some answer with fetch() and rewrite the page.
    // Wait for whichever happens, then let the result settle - but never hang.
    await Promise.all([
      page.waitForNavigation({ waitUntil: 'load', timeout }).catch(() => {}),
      page.click(config.submitSelector, { timeout }),
    ]);
    await page
      .waitForLoadState('networkidle', { timeout: Math.min(timeout, SETTLE_TIMEOUT_MS) })
      .catch(() => {});

    const cookies = await context.cookies();
    process.stdout.write(`${RESULT_MARKER}${JSON.stringify({
      url: page.url(),
      cookies: cookies.map((c) => ({
        name: c.name, value: c.value, domain: c.domain, path: c.path,
      })),
    })}\n`);
  } finally {
    await browser.close().catch(() => {});
  }
}

main().catch((err) => {
  die(`login failed: ${err && err.message ? err.message : err}`);
});
