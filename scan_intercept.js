'use strict';

/**
 * Request blocking and browser-level networking, shared by the two Playwright
 * runners (axe_playwright_runner.js and login_playwright_runner.js).
 *
 * A block list is a list of URL patterns - `*doubleclick.net*`, `*\/ads\/*`,
 * `*.gif` - that must not load while a page is measured. They are enforced by
 * real request interception: a matching request is aborted before it leaves
 * the browser, so a blocked third party costs the page no connection, no
 * bytes and no main-thread time. It is not a cosmetic filter over the results.
 *
 * The pattern language is Lighthouse's own, because the Lighthouse audits in
 * the same scan are given the identical list through
 * `--blocked-url-patterns`: a plain substring, with `*` standing for "any run
 * of characters". `ads` and `*ads*` therefore mean the same thing.
 *
 * scanconfig.py implements the same rule in Python (`pattern_matcher`), and
 * the test suite pins the two to each other - a URL either browser blocks is a
 * URL the Python side agrees is blocked.
 */

/** Escape everything a regular expression treats specially - except `*`. */
function escapeLiteral(text) {
  return String(text).replace(/[.+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * A `(url) => boolean` for these patterns, or null when there are none.
 * Matching is a search, not a full match, and is case-insensitive.
 */
function blockMatcher(patterns) {
  const list = (Array.isArray(patterns) ? patterns : [])
    .map((p) => String(p || '').trim())
    .filter(Boolean);
  if (!list.length) return null;
  const source = list
    .map((p) => escapeLiteral(p).split('*').join('.*'))
    .join('|');
  const re = new RegExp(source, 'i');
  return (url) => re.test(String(url || ''));
}

/**
 * Abort every request matching `patterns` in this browsing context.
 *
 * Returns the matcher that was installed (null when there was nothing to
 * block, in which case no route is registered at all and the context behaves
 * exactly as it did before any of this existed).
 */
async function installBlocking(context, patterns) {
  const blocked = blockMatcher(patterns);
  if (!blocked) return null;
  await context.route(
    (url) => blocked(String(url)),
    (route) => route.abort('blockedbyclient').catch(() => {})
  );
  return blocked;
}

/**
 * Launch arguments for the browser process, from the runner's stdin payload.
 *
 * These are flags for Chromium itself rather than context options - the DNS
 * override (`--host-resolver-rules`) is one, because name resolution happens
 * below the browsing context and cannot be set on it.
 */
function launchArgs(extra) {
  const base = ['--no-sandbox', '--disable-gpu', '--ignore-certificate-errors'];
  const given = (Array.isArray(extra) ? extra : [])
    .map((a) => String(a || '').trim())
    .filter(Boolean);
  return base.concat(given);
}

module.exports = { blockMatcher, installBlocking, launchArgs };
