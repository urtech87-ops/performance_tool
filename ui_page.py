#!/usr/bin/env python3
"""
The dashboard's single page, as markup.

`dashboard.PAGE` is what this module's `render()` returns. Keeping it here
rather than inside the Flask app is what lets `ui_preview.py` render the same
page - the real one, not a mock-up - into a file for every state the UI can be
in, with no server and no scan.

Everything the page shows about itself comes from the brand:

    brand.app_name      the name in the top bar and the title
    brand.app_tagline   the sentence under the heading
    brand.agency_logo   the logo, inlined; a monogram stands in when absent
    ui_css.stylesheet   every colour, size and space, from design_tokens

Nothing here decides what a scan does. The form's field names, the request
body, the event names and the file-priority rules are exactly what they were.
"""

import html
import json

import scanconfig
import scanpresets
import ui_css
from report_brand import load_brand


def _options(choices, selected=""):
    """<option> markup for a scanconfig choice list.

    The two selects are built from `scanconfig` rather than typed out here, so
    a preset the server understands and one the form offers cannot drift apart.
    """
    out = []
    for value, label in choices:
        mark = " selected" if value == selected else ""
        out.append(f'<option value="{html.escape(value)}"{mark}>'
                   f'{html.escape(label)}</option>')
    return "".join(out)


def _brandmark(brand):
    """The logo lockup for the top bar: the real logo, or a monogram tile."""
    if brand.agency_logo:
        mark = f'<img src="{html.escape(brand.agency_logo)}" alt="{html.escape(brand.app_name)}">'
    else:
        mark = f'<span class="dot">{html.escape(brand.agency_monogram)}</span>'
    return (f'<div class="brandmark">{mark}<div class="names">'
            f'<div class="n">{html.escape(brand.app_name)}</div>'
            f'<div class="r">Site audits</div></div></div>')


def render(brand=None):
    """The whole page for `brand` (defaults to the resolved brand.json)."""
    brand = load_brand(brand)
    footer = brand.footer_note or f"{brand.app_name} · all processing happens on this machine."
    return (TEMPLATE
            .replace("<!--CSS-->", ui_css.stylesheet(brand))
            .replace("<!--FONT_LINK-->", ui_css.font_link(brand))
            .replace("<!--BRANDMARK-->", _brandmark(brand))
            .replace("<!--APP_NAME-->", html.escape(brand.app_name))
            .replace("<!--APP_TAGLINE-->", html.escape(brand.app_tagline))
            .replace("<!--FOOTER-->", html.escape(footer))
            .replace("<!--THROTTLE_OPTIONS-->",
                     _options(scanconfig.throttling_choices(), scanconfig.DEFAULT_THROTTLING))
            .replace("<!--DEVICE_OPTIONS-->",
                     '<option value="">Match the Device setting above</option>'
                     + _options(scanconfig.device_choices()))
            # The browser is handed the server's own list of what a preset may
            # hold, so the two cannot drift - and so no credential field can
            # appear on it.
            .replace("<!--PRESET_FIELDS-->", json.dumps(list(scanpresets.PRESET_FIELDS))))


TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title><!--APP_NAME--> &middot; scan dashboard</title>
<!--FONT_LINK-->
<style><!--CSS--></style></head>
<body>
<a class="sr-only" href="#setup">Skip to the scan form</a>

<header class="topnav">
  <!--BRANDMARK-->
  <span class="spacer"></span>
  <span class="env" id="envpill"><span class="led"></span><span id="envtext">Runs locally &middot; private</span></span>
</header>

<div class="shell">
  <div class="hero">
    <h1>Audit a whole site</h1>
    <p><!--APP_TAGLINE--></p>
  </div>

  <div class="layout">
    <main>
      <!-- ============================================ the scan form ==== -->
      <section class="card" id="setup">
        <div class="card-head">
          <div class="grow">
            <h2>New scan</h2>
            <p>Enter a URL, choose what to audit, and the report is built when the crawl finishes.</p>
          </div>
        </div>

        <div class="basics">
          <div>
            <label class="lbl" for="url">Website URL</label>
            <div class="urlfield">
              <input id="url" type="text" placeholder="https://example.com" autocomplete="off" spellcheck="false">
              <svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 0 1 0 18a15 15 0 0 1 0-18"/></svg>
            </div>
          </div>
          <div>
            <span class="lbl" id="devicelbl">Device</span>
            <div class="seg" id="device" role="group" aria-labelledby="devicelbl">
              <button data-v="desktop" class="on" type="button" aria-pressed="true"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" aria-hidden="true"><rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8M12 17v4"/></svg>Desktop</button>
              <button data-v="mobile" type="button" aria-pressed="false"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" aria-hidden="true"><rect x="7" y="3" width="10" height="18" rx="2"/><path d="M11 18h2"/></svg>Mobile</button>
            </div>
          </div>
        </div>

        <div class="block">
          <span class="lbl">Audit categories &mdash; tap to include</span>
          <div class="chips" id="cats" role="group" aria-label="Audit categories">
            <div class="chip on" data-cat="performance" role="checkbox" aria-checked="true" tabindex="0"><span class="dot"></span>Performance</div>
            <div class="chip on" data-cat="accessibility" role="checkbox" aria-checked="true" tabindex="0"><span class="dot"></span>Accessibility</div>
            <div class="chip on" data-cat="best-practices" role="checkbox" aria-checked="true" tabindex="0"><span class="dot"></span>Best Practices</div>
            <div class="chip on" data-cat="seo" role="checkbox" aria-checked="true" tabindex="0"><span class="dot"></span>SEO</div>
            <div class="chip" data-cat="security" role="checkbox" aria-checked="false" tabindex="0"><span class="dot"></span>Security</div>
            <div class="chip" data-cat="a11y-standards" role="checkbox" aria-checked="false" tabindex="0"><span class="dot"></span>Accessibility standards</div>
          </div>
        </div>

        <div class="block" id="stdwrap" hidden>
          <span class="lbl">Accessibility standards &mdash; frameworks to report</span>
          <div class="chips" id="stds" role="group" aria-label="Accessibility standards">
            <div class="chip std" data-std="wcag20a" role="checkbox" aria-checked="false" tabindex="0"><span class="dot"></span>WCAG 2.0 A</div>
            <div class="chip std" data-std="wcag20aa" role="checkbox" aria-checked="false" tabindex="0"><span class="dot"></span>WCAG 2.0 AA</div>
            <div class="chip std" data-std="wcag20aaa" role="checkbox" aria-checked="false" tabindex="0"><span class="dot"></span>WCAG 2.0 AAA</div>
            <div class="chip std" data-std="wcag21a" role="checkbox" aria-checked="false" tabindex="0"><span class="dot"></span>WCAG 2.1 A</div>
            <div class="chip std on" data-std="wcag21aa" role="checkbox" aria-checked="true" tabindex="0"><span class="dot"></span>WCAG 2.1 AA</div>
            <div class="chip std" data-std="wcag21aaa" role="checkbox" aria-checked="false" tabindex="0"><span class="dot"></span>WCAG 2.1 AAA</div>
            <div class="chip std" data-std="wcag22a" role="checkbox" aria-checked="false" tabindex="0"><span class="dot"></span>WCAG 2.2 A</div>
            <div class="chip std" data-std="wcag22aa" role="checkbox" aria-checked="false" tabindex="0"><span class="dot"></span>WCAG 2.2 AA</div>
            <div class="chip std" data-std="wcag22aaa" role="checkbox" aria-checked="false" tabindex="0"><span class="dot"></span>WCAG 2.2 AAA</div>
            <div class="chip std on" data-std="section508" role="checkbox" aria-checked="true" tabindex="0"><span class="dot"></span>Section 508 (US)</div>
            <div class="chip std on" data-std="en301549" role="checkbox" aria-checked="true" tabindex="0"><span class="dot"></span>EN 301 549 (EU)</div>
            <div class="chip std on" data-std="ada" role="checkbox" aria-checked="true" tabindex="0"><span class="dot"></span>ADA Title III (US)</div>
            <div class="chip std" data-std="unruh" role="checkbox" aria-checked="false" tabindex="0"><span class="dot"></span>California Unruh</div>
            <div class="chip std" data-std="aoda" role="checkbox" aria-checked="false" tabindex="0"><span class="dot"></span>Ontario AODA</div>
            <div class="chip std" data-std="uk_equality" role="checkbox" aria-checked="false" tabindex="0"><span class="dot"></span>UK Equality Act</div>
            <div class="chip std" data-std="au_dda" role="checkbox" aria-checked="false" tabindex="0"><span class="dot"></span>Australian DDA</div>
            <div class="chip std" data-std="il_5568" role="checkbox" aria-checked="false" tabindex="0"><span class="dot"></span>Israeli 5568</div>
            <div class="chip std" data-std="ca_aca" role="checkbox" aria-checked="false" tabindex="0"><span class="dot"></span>Canada ACA</div>
            <div class="chip std" data-std="atag20" role="checkbox" aria-checked="false" tabindex="0"><span class="dot"></span>ATAG 2.0</div>
          </div>
          <span class="hint">Automated axe-core checks only &mdash; the report states automated results per framework, never a legal compliance verdict. ATAG 2.0 is reported as not applicable (it governs authoring tools, not pages).</span>
        </div>

        <!-- ==================================== advanced options ======= -->
        <details class="adv">
          <summary><svg class="caret" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>Advanced options<span class="sub">presets, depth, connection, device, requests, sign-in</span></summary>
          <div class="adv-body">

            <div class="agroup boxed">
              <div class="atitle"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M5 4h14v16l-7-4-7 4z"/></svg>Presets</div>
              <div class="ahint">Save everything on this panel under a name and pick it again next time.
                A preset holds scan settings only &mdash; the sign-in fields below are never part of one.</div>
              <div class="fieldgrid">
                <label class="f"><span>Preset</span><select id="preset_select"></select></label>
                <label class="f wide"><span>Name</span><input id="preset_name" type="text" placeholder="e.g. Staging box, mobile 4G" autocomplete="off" spellcheck="false"></label>
                <div class="btnrow">
                  <button class="btn sm" id="preset_save" type="button">Save as new</button>
                  <button class="btn sm" id="preset_update" type="button">Update</button>
                  <button class="btn sm" id="preset_rename" type="button">Rename</button>
                  <button class="btn sm danger" id="preset_delete" type="button">Delete</button>
                  <label class="sw"><span class="switch"><input type="checkbox" id="preset_default"><span class="track"></span></span>Start new scans with this one</label>
                  <span class="pmsg" id="preset_msg" role="status"></span>
                </div>
              </div>
            </div>

            <div class="agroup">
              <div class="atitle">Scan depth</div>
              <div class="ahint">How much of the site is measured, and whether each page is taken
                apart into fix recommendations.</div>
              <div class="fieldgrid">
                <div class="f tiny"><span>Samples</span><input id="samples" type="number" min="1" max="5" value="1"></div>
                <div class="f tiny"><span>Max pages</span><input id="maxpages" type="number" min="1" max="200" value="30"></div>
                <div class="f tiny"><span>Parallel</span><input id="concurrency" type="number" min="1" max="6" value="3"></div>
                <label class="sw"><span class="switch"><input type="checkbox" id="deep" checked><span class="track"></span></span>Fix recommendations &amp; per-page reports</label>
              </div>
            </div>

            <div class="agroup">
              <div class="atitle">Connection</div>
              <div class="ahint">The network the pages are measured over. &ldquo;Lighthouse default&rdquo; is the
                simulated Slow 4G every score you have seen so far was measured on &mdash; change it and the
                numbers change with it, so compare like with like.</div>
              <div class="fieldgrid">
                <label class="f wide"><span>Throttling</span>
                  <select id="throttling"><!--THROTTLE_OPTIONS--></select>
                </label>
                <div class="f tiny" id="customdown" hidden><span>Download Kbps</span><input id="down_kbps" type="number" min="1" max="1000000" value="5000"></div>
                <div class="f tiny" id="customup" hidden><span>Upload Kbps</span><input id="up_kbps" type="number" min="1" max="1000000" value="1000"></div>
                <div class="f tiny" id="customlat" hidden><span>Latency ms</span><input id="latency_ms" type="number" min="0" max="10000" value="100"></div>
              </div>
            </div>

            <div class="agroup">
              <div class="atitle">Device &amp; viewport</div>
              <div class="ahint">A named handset sets the form factor, the screen and the User-Agent together.
                Leave the width, height and pixel ratio empty to use the device&rsquo;s own.</div>
              <div class="fieldgrid">
                <label class="f wide"><span>Device profile</span>
                  <select id="device_profile"><!--DEVICE_OPTIONS--></select>
                </label>
                <div class="f tiny"><span>Width</span><input id="viewport_width" type="number" min="200" max="3840" placeholder="auto"></div>
                <div class="f tiny"><span>Height</span><input id="viewport_height" type="number" min="200" max="3840" placeholder="auto"></div>
                <div class="f tiny"><span>Pixel ratio</span><input id="dpr" type="number" min="0.5" max="5" step="0.25" placeholder="auto"></div>
                <div class="f full"><span>User-Agent override</span><input id="user_agent" type="text" placeholder="leave empty for the device's own" autocomplete="off" spellcheck="false"></div>
              </div>
            </div>

            <div class="agroup">
              <div class="atitle">Requests &amp; DNS</div>
              <div class="ahint">Blocked URLs never load: the browser aborts them before they are sent, so
                ads, analytics and third-party widgets cost the page nothing. One pattern per line;
                <code>*</code> matches any run of characters, and a bare domain matches anywhere in the URL.
                The DNS override points one hostname at an address for this scan only &mdash; how a staging
                server is measured before its DNS is switched. Everything else about the request is
                unchanged, so the site still sees its own host name and certificate.</div>
              <div class="fieldgrid">
                <div class="f full"><span>Block URLs</span><textarea id="block_patterns" autocomplete="off" spellcheck="false" placeholder="*googletagmanager.com*&#10;*doubleclick.net*&#10;*/ads/*"></textarea></div>
              </div>
              <div class="fieldgrid" id="dnsrow">
                <div class="f"><span>Resolve host</span><input id="dns_host" type="text" placeholder="the scanned hostname" autocomplete="off" spellcheck="false"></div>
                <div class="f"><span>To this address</span><input id="dns_ip" type="text" placeholder="203.0.113.10" autocomplete="off" spellcheck="false"></div>
              </div>
            </div>

            <div class="agroup secret">
              <div class="atitle"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" aria-hidden="true"><rect x="4" y="10" width="16" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>Sign in to the site</div>
              <div class="ahint">For staging servers, member areas and anything behind a password. Fill in only
                what the site needs &mdash; all three methods can be combined.</div>
              <div class="ahint" id="authnote" hidden></div>
              <div class="privacy">
                <svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M12 3l7 4v5c0 4.4-3 7.6-7 9-4-1.4-7-4.6-7-9V7z"/><path d="M9 12l2 2 4-4"/></svg>
                <span><strong>These fields are never stored.</strong> They are sent once, over this
                request&rsquo;s body, used in memory for this scan and wiped when it ends. Nothing here is
                written to the run folder, the report, the scan log, the saved settings below, or any
                history &mdash; so a re-run asks for them again.</span>
              </div>
              <div class="fieldgrid">
                <div class="f"><span>HTTP Basic user</span><input id="auth_user" type="text" data-secret="1" autocomplete="off" spellcheck="false"></div>
                <div class="f"><span>HTTP Basic password</span><input id="auth_pass" type="password" data-secret="1" autocomplete="new-password"></div>
                <div class="f full"><span>Cookies</span><textarea id="cookies" data-secret="1" autocomplete="off" spellcheck="false" placeholder="session=abc123; consent=accepted"></textarea></div>
                <div class="f full"><span>Login form URL</span><input id="login_url" type="text" data-secret="1" autocomplete="off" spellcheck="false" placeholder="https://example.com/login"></div>
                <div class="f"><span>Username selector</span><input id="login_user_selector" type="text" data-secret="1" autocomplete="off" spellcheck="false" placeholder="#username"></div>
                <div class="f"><span>Password selector</span><input id="login_pass_selector" type="text" data-secret="1" autocomplete="off" spellcheck="false" placeholder="#password"></div>
                <div class="f"><span>Submit selector</span><input id="login_submit_selector" type="text" data-secret="1" autocomplete="off" spellcheck="false" placeholder="button[type=submit]"></div>
                <div class="f"><span>Login username</span><input id="login_user" type="text" data-secret="1" autocomplete="off" spellcheck="false"></div>
                <div class="f"><span>Login password</span><input id="login_pass" type="password" data-secret="1" autocomplete="new-password"></div>
                <div class="btnrow"><button class="btn sm" id="clearauth" type="button">Clear sign-in fields</button></div>
              </div>
            </div>

            <div class="agroup">
              <label class="sw"><span class="switch"><input type="checkbox" id="remember" checked><span class="track"></span></span>Remember these settings in this browser <span class="hint">(everything except the sign-in fields)</span></label>
            </div>
          </div>
        </details>

        <div class="cta">
          <button class="go" id="go" type="button"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg><span id="gotext">Generate report</span></button>
          <span class="hint">Deep audits run each page through Lighthouse &mdash; large sites take a few minutes.</span>
        </div>
      </section>

      <!-- ================================ refusals and adjustments ==== -->
      <div class="note err panel" id="notice" role="alert" hidden>
        <div class="ntitle" id="ntitle"></div>
        <div class="nmsg" id="nmsg"></div>
        <div class="verify" id="verify" hidden>
          <div class="vopt">
            <span class="lbl">Option 1 &mdash; meta tag in your homepage &lt;head&gt;</span>
            <code id="vmeta"></code>
          </div>
          <div class="vopt">
            <span class="lbl">Option 2 &mdash; DNS TXT record on your domain</span>
            <code id="vdns"></code>
          </div>
          <div class="vbtns">
            <button class="btn primary sm" id="vrecheck" type="button">I&rsquo;ve added it &mdash; re-check</button>
            <button class="btn sm" id="vsingle" type="button">Scan just this page instead</button>
          </div>
        </div>
      </div>

      <!-- ============================================== in progress ==== -->
      <section class="card panel" id="logwrap" hidden aria-live="polite">
        <div class="steps" id="steps">
          <div class="step" data-step="0"><span class="idx">1</span><div class="txt"><div class="t">Queued</div><div class="s">waiting for a worker</div></div></div>
          <div class="step" data-step="1"><span class="idx">2</span><div class="txt"><div class="t">Scanning</div><div class="s">crawl and audit</div></div></div>
          <div class="step" data-step="2"><span class="idx">3</span><div class="txt"><div class="t">Report</div><div class="s">built and ready</div></div></div>
        </div>
        <div class="statusline"><span class="spin" id="spin"></span><span id="status">Scanning&hellip;</span>
          <span class="qbadge" id="qbadge" hidden></span></div>
        <div class="note plain" id="statebox" hidden>
          <div class="ntitle" id="stitle"></div>
          <div class="nmsg" id="smsg"></div>
          <div class="coverwrap" id="scov" hidden>
            <div id="covring"></div>
            <div class="ctext" id="covtext"></div>
          </div>
        </div>
        <details class="logdetails" id="logdetails">
          <summary>Technical log &mdash; for advanced users</summary>
          <div id="log"></div>
        </details>
      </section>

      <!-- ================================================= results ===== -->
      <section class="panel" id="results" hidden>
        <div class="rbar">
          <a class="btn primary" id="openlink" href="#" target="_blank" rel="noopener"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" aria-hidden="true"><path d="M14 3h7v7M21 3l-9 9M10 5H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5"/></svg>Open full report</a>
          <a class="btn" id="pdflink" href="#" target="_blank" rel="noopener"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" aria-hidden="true"><path d="M12 3v12M7 10l5 5 5-5M5 21h14"/></svg>PDF</a>
          <a class="btn" id="seclink" href="#" target="_blank" rel="noopener" hidden><svg viewBox="0 0 24 24" fill="none" stroke-width="2" aria-hidden="true"><path d="M12 3l7 4v5c0 4.4-3 7.6-7 9-4-1.4-7-4.6-7-9V7z"/></svg>Security report</a>
          <a class="btn" id="a11ylink" href="#" target="_blank" rel="noopener" hidden><svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="4.5" r="1.6"/><path d="M4.5 8.2h15M12 8.2v5m0 0l-3.4 6.6M12 13.2l3.4 6.6"/></svg>Accessibility report</a>
        </div>
        <div class="frame">
          <div class="cap"><span class="dots"><i></i><i></i><i></i></span><span id="cap">Report preview</span></div>
          <iframe id="frame" src="about:blank" title="report"></iframe>
        </div>
      </section>

      <!-- ============================================ nothing yet ====== -->
      <section class="card panel empty" id="idle">
        <div class="icon"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 3v18h18"/><path d="M7 14l3-4 3 3 4-6"/></svg></div>
        <h3>No report yet</h3>
        <p>Nothing has been scanned in this browser session. The report appears here when the first run finishes.</p>
        <ul class="steps-list">
          <li><span class="n">1</span><span>Paste the site&rsquo;s address and pick a device.</span></li>
          <li><span class="n">2</span><span>Choose the categories you want measured.</span></li>
          <li><span class="n">3</span><span>Open the report, or download it as a PDF.</span></li>
        </ul>
      </section>
    </main>

    <!-- ==================================================== aside ====== -->
    <aside class="side">
      <div class="card">
        <h3>What a run produces</h3>
        <ul class="bullets">
          <li>One consolidated report: scores, core metrics and the fixes ranked by what they cost against what they buy.</li>
          <li>A print-ready PDF of the same document.</li>
          <li>Standalone security and accessibility pages when those categories are on.</li>
        </ul>
      </div>
      <div class="card" id="limitcard" hidden>
        <h3>This deployment</h3>
        <dl class="deflist" id="limits">
          <div><dt>Max pages</dt><dd id="lim_pages">&ndash;</dd></div>
          <div><dt>Samples per page</dt><dd id="lim_samples">&ndash;</dd></div>
          <div><dt>Parallel pages</dt><dd id="lim_parallel">&ndash;</dd></div>
          <div><dt>Scan timeout</dt><dd id="lim_timeout">&ndash;</dd></div>
        </dl>
        <p class="hint" style="margin-top:var(--sp-3)">A request above these is trimmed to them, and you are told when it is.</p>
      </div>
    </aside>
  </div>

  <div class="foot"><!--FOOTER--></div>
</div>

<script>
  // ------------------------------------------------------------------
  // What may be remembered, and what may never be.
  //
  // SECRET_FIELDS is the same list the server keeps in scanauth.SECRET_FIELDS.
  // Nothing on it is written to localStorage, put in a URL, or kept in the
  // re-run state - a re-run reads the live form, so if the fields have been
  // cleared it simply asks again. Everything else is a scan setting and is
  // remembered between visits.
  // ------------------------------------------------------------------
  var SECRET_FIELDS = ["auth_user","auth_pass","cookies","login_url",
                       "login_user_selector","login_pass_selector",
                       "login_submit_selector","login_user","login_pass"];
  var SETTING_FIELDS = ["samples","maxpages","concurrency","throttling","down_kbps",
                        "up_kbps","latency_ms","device_profile","viewport_width",
                        "viewport_height","dpr","user_agent","block_patterns",
                        "dns_host","dns_ip"];
  // The fields a preset may hold, straight from the server (scanpresets.
  // PRESET_FIELDS). Reading it from there is what keeps the two ends honest:
  // the browser cannot offer to save a field the server will not store, and
  // no name on SECRET_FIELDS is on it.
  var PRESET_FIELDS = <!--PRESET_FIELDS-->;
  // Where a preset field lives in the form, when the id is not the field name.
  var PRESET_INPUT = {max_pages:"maxpages"};
  var PREFS_KEY = "scan.settings.v1";

  function byId(id){ return document.getElementById(id); }
  function val(id){ var el = byId(id); return el ? el.value.trim() : ""; }
  function setVal(id, v){
    var el = byId(id);
    if(!el){ return; }
    if(Array.isArray(v)){ v = v.join("\\n"); }
    // 0 is how the server spells "not set" for the optional numbers.
    el.value = (v === 0 || v === null || v === undefined) ? "" : String(v);
  }
  /** Show or hide a panel. One helper, so nothing sets style.display by hand. */
  function show(el, on){
    if(!el){ return; }
    if(on){ el.removeAttribute("hidden"); } else { el.setAttribute("hidden", ""); }
  }

  // ---- the score ring ------------------------------------------------
  // The same shape report_charts.score_ring draws server-side, with the same
  // class names, so it is styled by the same rules as the report's rings.
  function ring(percent, label){
    var band = percent === null ? "na" : (percent >= 90 ? "good" : (percent >= 50 ? "avg" : "poor"));
    var size = 74, stroke = 7, r = (size - stroke) / 2, c = 2 * Math.PI * r;
    var dash = c * (percent === null ? 0 : Math.max(0, Math.min(1, percent / 100)));
    var mid = size / 2;
    var color = "var(--" + band + ")";
    return '<svg class="ring ring-' + band + '" viewBox="0 0 ' + size + ' ' + size + '" width="' + size +
      '" height="' + size + '" role="img" aria-label="' + label + ' ' +
      (percent === null ? "not measured" : percent + " percent") + '">' +
      '<circle cx="' + mid + '" cy="' + mid + '" r="' + r.toFixed(2) + '" fill="none" ' +
      'stroke="var(--line)" stroke-width="' + stroke + '"/>' +
      '<circle cx="' + mid + '" cy="' + mid + '" r="' + r.toFixed(2) + '" fill="none" stroke="' + color +
      '" stroke-width="' + stroke + '" stroke-linecap="round" stroke-dasharray="' +
      dash.toFixed(2) + ' ' + (c - dash).toFixed(2) + '" transform="rotate(-90 ' + mid + ' ' + mid + ')"/>' +
      '<text class="ring-value" x="' + mid + '" y="' + (mid + 7) + '" text-anchor="middle" ' +
      'font-size="21" font-weight="700" fill="' + color + '">' +
      (percent === null ? "-" : percent) + '</text></svg>';
  }

  // device segmented control
  let device = "desktop";
  function setDevice(v){
    device = (v === "mobile") ? "mobile" : "desktop";
    document.querySelectorAll("#device button").forEach(function(x){
      var on = x.dataset.v === device;
      x.classList.toggle("on", on);
      x.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }
  document.querySelectorAll("#device button").forEach(function(b){
    b.onclick = function(){ setDevice(b.dataset.v); };
  });
  // category chips toggle
  var stdwrap = byId("stdwrap");
  function syncStdWrap(){
    var on = document.querySelector('#cats .chip[data-cat="a11y-standards"]').classList.contains("on");
    show(stdwrap, on);
  }
  /** Chips are checkboxes: click or Space/Enter toggles, and the state is
      announced, not only coloured. */
  function wireChip(ch, after){
    function toggle(){
      ch.classList.toggle("on");
      ch.setAttribute("aria-checked", ch.classList.contains("on") ? "true" : "false");
      if(after){ after(); }
    }
    ch.onclick = toggle;
    ch.onkeydown = function(e){
      if(e.key === " " || e.key === "Enter"){ e.preventDefault(); toggle(); }
    };
  }
  document.querySelectorAll("#cats .chip").forEach(function(ch){ wireChip(ch, syncStdWrap); });
  document.querySelectorAll("#stds .chip").forEach(function(ch){ wireChip(ch, null); });
  syncStdWrap();

  // custom connection inputs appear only for the custom preset
  var throttleSel = byId("throttling");
  function syncThrottle(){
    var custom = throttleSel.value === "custom";
    ["customdown","customup","customlat"].forEach(function(id){ show(byId(id), custom); });
  }
  throttleSel.onchange = syncThrottle;

  // ---- saved settings ----------------------------------------------
  // Only SETTING_FIELDS are ever written. The sign-in fields are not on that
  // list, are not read here, and are not written here - by construction, not
  // by filtering something that already contains them.
  function saveSettings(){
    var el = byId("remember");
    try {
      if(!el || !el.checked){ localStorage.removeItem(PREFS_KEY); return; }
      var out = { device: device, deep: byId("deep").checked };
      SETTING_FIELDS.forEach(function(id){ out[id] = val(id); });
      localStorage.setItem(PREFS_KEY, JSON.stringify(out));
    } catch(err){ /* private mode, quota, disabled storage: settings just don't stick */ }
  }

  function loadSettings(){
    var saved = null;
    try { saved = JSON.parse(localStorage.getItem(PREFS_KEY) || "null"); } catch(err){ }
    if(!saved){ syncThrottle(); return false; }
    SETTING_FIELDS.forEach(function(id){
      var el = byId(id);
      if(el && typeof saved[id] === "string" && saved[id] !== ""){ el.value = saved[id]; }
    });
    if(typeof saved.deep === "boolean"){ byId("deep").checked = saved.deep; }
    if(saved.device === "mobile" || saved.device === "desktop"){ setDevice(saved.device); }
    syncThrottle();
    return true;
  }

  byId("clearauth").onclick = function(){
    SECRET_FIELDS.forEach(function(id){
      var el = byId(id);
      if(el){ el.value = ""; }
    });
  };

  var restored = loadSettings();

  // ---- presets ------------------------------------------------------
  // A named set of the settings on this panel, saved on the server against
  // this browser's session. The payload is assembled field by field from
  // PRESET_FIELDS, so what is sent is exactly what the server stores - the
  // sign-in inputs are not read here and have no way in.
  var presets = [], presetDefault = "";
  var presetSel = byId("preset_select");
  var presetName = byId("preset_name");
  var presetMsg = byId("preset_msg");
  var presetDefaultBox = byId("preset_default");

  function asList(v){
    if(Array.isArray(v)){ return v.slice(); }
    return String(v || "").split(",").map(function(x){ return x.trim(); })
      .filter(function(x){ return x !== ""; });
  }

  function pmsg(text, kind){
    presetMsg.className = "pmsg" + (kind ? " " + kind : "");
    presetMsg.textContent = text || "";
  }

  /** The form as a preset - only the fields the server stores. */
  function formSettings(){
    var cats = selectedCats();
    var live = {
      device: device,
      deep: byId("deep").checked,
      samples: val("samples"), max_pages: val("maxpages"),
      concurrency: val("concurrency"),
      categories: cats.filter(function(c){
        return c !== "security" && c !== "a11y-standards"; }).join(","),
      security: cats.indexOf("security") !== -1,
      a11y: cats.indexOf("a11y-standards") !== -1,
      standards: selectedStds().join(",")
    };
    var out = {};
    PRESET_FIELDS.forEach(function(field){
      out[field] = (field in live) ? live[field] : val(PRESET_INPUT[field] || field);
    });
    return out;
  }

  /** Fill the whole panel in from a preset's settings. */
  function applySettings(s){
    if(!s){ return; }
    setDevice(s.device);
    if(typeof s.deep === "boolean"){ byId("deep").checked = s.deep; }
    var cats = asList(s.categories);
    if(s.security){ cats.push("security"); }
    if(s.a11y){ cats.push("a11y-standards"); }
    document.querySelectorAll("#cats .chip").forEach(function(ch){
      setChip(ch, cats.indexOf(ch.dataset.cat) !== -1); });
    var stds = asList(s.standards);
    document.querySelectorAll("#stds .chip").forEach(function(ch){
      setChip(ch, stds.indexOf(ch.dataset.std) !== -1); });
    syncStdWrap();
    PRESET_FIELDS.forEach(function(field){
      if(["device","deep","categories","security","a11y","standards"].indexOf(field) !== -1){ return; }
      setVal(PRESET_INPUT[field] || field, s[field]);
    });
    syncThrottle();
  }

  function setChip(ch, on){
    ch.classList.toggle("on", on);
    ch.setAttribute("aria-checked", on ? "true" : "false");
  }

  function selectedPreset(){
    var id = presetSel.value;
    for(var i = 0; i < presets.length; i++){ if(presets[i].id === id){ return presets[i]; } }
    return null;
  }

  function renderPresets(select){
    presetSel.innerHTML = "";
    presets.forEach(function(p){
      var opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.name + (p.id === presetDefault ? " \\u00b7 default" : "");
      presetSel.appendChild(opt);
    });
    presetSel.value = select || presetDefault;
    var current = selectedPreset();
    presetDefaultBox.checked = !!current && current.id === presetDefault;
    byId("preset_update").disabled = !current || current.builtin;
    byId("preset_rename").disabled = !current || current.builtin;
    byId("preset_delete").disabled = !current || current.builtin;
  }

  function takePresets(payload, select){
    presets = payload.presets || [];
    presetDefault = payload.default || "";
    renderPresets(select);
  }

  function postPreset(body, done){
    fetch("/api/presets", {method:"POST", headers:{"Content-Type":"application/json"},
                           body: JSON.stringify(body)})
      .then(function(r){ return r.json().then(function(j){ return {ok:r.ok, body:j}; }); })
      .then(function(res){
        if(!res.ok){ pmsg(res.body.message || "That preset was not saved.", "err"); return; }
        takePresets(res.body, (res.body.preset || {}).id);
        if(done){ done(res.body); }
      })
      .catch(function(){ pmsg("Could not reach the server.", "err"); });
  }

  fetch("/api/presets").then(function(r){ return r.json(); }).then(function(j){
    takePresets(j);
    var start = selectedPreset();
    // A browser with settings of its own keeps them; a fresh one starts on
    // whichever preset is the default.
    if(!restored && start){ applySettings(start.settings); }
    if(start){ presetName.value = start.builtin ? "" : start.name; }
  }).catch(function(){ /* presets are a convenience - the form still works */ });

  presetSel.onchange = function(){
    var p = selectedPreset();
    if(!p){ return; }
    applySettings(p.settings);
    presetName.value = p.builtin ? "" : p.name;
    presetDefaultBox.checked = p.id === presetDefault;
    renderPresets(p.id);
    pmsg("Applied \\u201c" + p.name + "\\u201d.", "ok");
  };

  byId("preset_save").onclick = function(){
    var name = presetName.value.trim();
    if(!name){ pmsg("Give the preset a name first.", "err"); return; }
    postPreset({name:name, settings:formSettings(), default:presetDefaultBox.checked},
               function(){ pmsg("Saved \\u201c" + name + "\\u201d.", "ok"); });
  };

  byId("preset_update").onclick = function(){
    var p = selectedPreset();
    if(!p || p.builtin){ pmsg("Pick one of your own presets to update.", "err"); return; }
    var name = presetName.value.trim() || p.name;
    postPreset({id:p.id, name:name, settings:formSettings(),
                default:presetDefaultBox.checked},
               function(){ pmsg("Updated \\u201c" + name + "\\u201d.", "ok"); });
  };

  byId("preset_rename").onclick = function(){
    var p = selectedPreset();
    if(!p || p.builtin){ pmsg("Built-in presets cannot be renamed.", "err"); return; }
    var name = presetName.value.trim();
    if(!name){ pmsg("Type the new name first.", "err"); return; }
    postPreset({id:p.id, name:name, settings:p.settings},
               function(){ pmsg("Renamed to \\u201c" + name + "\\u201d.", "ok"); });
  };

  byId("preset_delete").onclick = function(){
    var p = selectedPreset();
    if(!p || p.builtin){ pmsg("Built-in presets cannot be deleted.", "err"); return; }
    fetch("/api/presets/" + encodeURIComponent(p.id), {method:"DELETE"})
      .then(function(r){ return r.json().then(function(j){ return {ok:r.ok, body:j}; }); })
      .then(function(res){
        if(!res.ok){ pmsg(res.body.message || "That preset was not deleted.", "err"); return; }
        takePresets(res.body);
        presetName.value = "";
        pmsg("Deleted \\u201c" + p.name + "\\u201d.", "ok");
      })
      .catch(function(){ pmsg("Could not reach the server.", "err"); });
  };

  presetDefaultBox.onchange = function(){
    var p = selectedPreset();
    if(!p){ return; }
    if(!presetDefaultBox.checked){
      // Turning it off means "back to the one this tool ships with".
      postPreset({id:"builtin_default", default:true},
                 function(){ renderPresets(p.id); pmsg("Default reset.", "ok"); });
      return;
    }
    postPreset({id:p.id, default:true},
               function(){ renderPresets(p.id);
                           pmsg("\\u201c" + p.name + "\\u201d is now the default.", "ok"); });
  };

  var logwrap = byId("logwrap");
  var logEl = byId("log");
  var go = byId("go");
  var goText = byId("gotext");
  var statusEl = byId("status");
  var spin = byId("spin");
  var qbadge = byId("qbadge");
  var notice = byId("notice");
  var verifyBox = byId("verify");
  var idle = byId("idle");
  var scanning = false;   // one scan at a time - the button stays disabled meanwhile
  var limits = null;      // what this server allows, fetched once

  fetch("/api/limits").then(function(r){ return r.json(); }).then(function(j){
    limits = j;
    var mp = byId("maxpages");
    mp.max = j.max_pages; if(+mp.value > j.max_pages){ mp.value = j.max_pages; }
    var sa = byId("samples");
    sa.max = j.samples;   if(+sa.value > j.samples){ sa.value = j.samples; }
    var pa = byId("concurrency");
    pa.max = j.parallel;  if(+pa.value > j.parallel){ pa.value = j.parallel; }
    // The caps, said up front instead of after a scan has been trimmed.
    byId("lim_pages").textContent = j.max_pages;
    byId("lim_samples").textContent = j.samples;
    byId("lim_parallel").textContent = j.parallel;
    byId("lim_timeout").textContent = Math.round(j.scan_timeout / 60) + " min";
    show(byId("limitcard"), true);
    if(j.mode === "public"){ byId("envtext").textContent = "Hosted \\u00b7 shared queue"; }
    if(j.dns_override_enabled === false){
      var dns = byId("dnsrow");
      if(dns){ show(dns, false); }
    }
    if(j.scan_auth_enabled === false){
      // This deployment does not accept third-party credentials at all, so
      // don't offer fields whose contents it would only refuse.
      var box = document.querySelector(".agroup.secret");
      if(box){ show(box, false); }
    } else if(j.auth_requires_verification){
      // Offered, but only for a domain you have proved you own. Say so where
      // the fields are, rather than letting the scan be refused after the
      // person has typed a password into them.
      var note = byId("authnote");
      if(note){
        note.textContent = "This deployment accepts sign-in details only for " +
                           "a domain you have verified ownership of.";
        show(note, true);
      }
    }
  }).catch(function(){ /* the caps still apply server-side */ });

  var statebox = byId("statebox");

  // ---- the progress rail ---------------------------------------------
  // Three steps in the order they happen. `state` is "" (not there yet),
  // "on" (here now) or "done".
  function setSteps(index, finished){
    document.querySelectorAll("#steps .step").forEach(function(el, i){
      el.classList.toggle("on", !finished && i === index);
      el.classList.toggle("done", finished ? i <= index : i < index);
    });
  }

  // The run's own state, in plain language. The raw log stays one click away
  // in the collapsed <details> below it - it is never the thing a person is
  // handed instead of an explanation.
  function showState(kind, title, message, coverage){
    statebox.className = "note " + (kind || "plain");
    byId("stitle").textContent = title || "";
    byId("smsg").textContent = message || "";
    var cov = byId("scov");
    if(coverage && coverage.attempted){
      var pct = coverage.percent || 0;
      byId("covring").innerHTML = ring(pct, "Coverage");
      byId("covtext").innerHTML = "<b>Coverage: " + coverage.ok + " of " + coverage.attempted +
        " page(s) (" + pct + "%).</b> Pages that could not be measured are unmeasured, not clean.";
      show(cov, true);
    } else { show(cov, false); }
    show(statebox, true);
  }
  function hideState(){ show(statebox, false); }

  function showNotice(title, message, isError){
    byId("ntitle").textContent = title;
    byId("nmsg").textContent = message;
    notice.className = "note panel " + (isError ? "err" : "warn");
    show(notice, true);
  }
  function hideNotice(){ show(notice, false); show(verifyBox, false); }

  function showVerification(v){
    byId("vmeta").textContent = v.meta_tag;
    byId("vdns").textContent = v.dns_record;
    show(verifyBox, true);
  }

  function setQueueBadge(text){
    qbadge.textContent = text || "";
    show(qbadge, !!text);
  }

  function setBusy(on){
    scanning = on;
    go.disabled = on;
    goText.textContent = on ? "Scanning\\u2026" : "Generate report";
  }

  function showLink(id, href){
    var el = byId(id);
    if(href){ el.href = href; show(el, true); }
    else { el.removeAttribute("href"); show(el, false); }
  }

  function selectedCats(){
    var out = [];
    document.querySelectorAll("#cats .chip.on").forEach(function(c){ out.push(c.dataset.cat); });
    return out;
  }

  function selectedStds(){
    var out = [];
    document.querySelectorAll("#stds .chip.on").forEach(function(c){ out.push(c.dataset.std); });
    return out;
  }

  var jobId = null, lastRequest = null;

  // "I've added the meta tag / TXT record" - ask the server to look again.
  byId("vrecheck").onclick = function(){
    if(!lastRequest){ return; }
    var btn = this; btn.textContent = "Checking\\u2026";
    fetch("/api/verify?check=1&url=" + encodeURIComponent(lastRequest.url))
      .then(function(r){ return r.json(); })
      .then(function(j){
        btn.textContent = "I\\u2019ve added it \\u2014 re-check";
        if(j.verified){ hideNotice(); go.click(); }
        else { showNotice("Still not visible",
                          "We could not find " + j.token + " on " + j.domain +
                          " yet. DNS changes can take a few minutes to propagate.", true);
               showVerification(j); }
      })
      .catch(function(){ btn.textContent = "I\\u2019ve added it \\u2014 re-check"; });
  };

  // The always-allowed alternative: one page, no crawl, no verification.
  byId("vsingle").onclick = function(){
    byId("maxpages").value = 1;
    hideNotice();
    go.click();
  };

  go.onclick = function(){
    if(scanning){ return; }              // a scan is already streaming
    var url = byId("url").value.trim();
    if(!url){ alert("Enter a URL first."); return; }
    var cats = selectedCats();
    if(cats.length === 0){ alert("Select at least one category."); return; }
    var security = cats.indexOf("security") !== -1 ? 1 : 0;
    var a11y = cats.indexOf("a11y-standards") !== -1 ? 1 : 0;
    var stds = selectedStds();
    if(a11y && stds.length === 0){ alert("Pick at least one accessibility standard."); return; }
    var lh = cats.filter(function(c){ return c !== "security" && c !== "a11y-standards"; });
    var anyLH = lh.length > 0;

    var deep = byId("deep").checked ? 1 : 0;
    var samples = byId("samples").value || 1;
    var maxpages = byId("maxpages").value || 30;
    var concurrency = byId("concurrency").value || 3;

    hideNotice();
    hideState();
    document.getElementById("logdetails").open = false;
    show(idle, false);
    show(logwrap, true);
    logEl.textContent = "";
    show(spin, true);
    setSteps(0, false);
    statusEl.textContent = "Submitting\\u2026";
    setQueueBadge(null);
    show(byId("results"), false);
    setBusy(true);
    // Only the URL is kept: the re-check and "scan this page instead" buttons
    // replay the form as it stands, they do not carry a saved copy of it - and
    // a saved copy is exactly what must not exist for the sign-in fields.
    lastRequest = { url: url };
    saveSettings();

    // The scan settings go in the POST body, not a query string: the sign-in
    // fields below would otherwise be written to the server's access log and
    // to this browser's own history.
    var body = { url:url, device:device, deep:deep, samples:samples,
      max_pages:maxpages, concurrency:concurrency, security:security,
      categories:lh.join(","), a11y:a11y, standards:stds.join(",") };
    SETTING_FIELDS.forEach(function(id){
      if(id === "samples" || id === "maxpages" || id === "concurrency"){ return; }
      var v = val(id); if(v !== ""){ body[id] = v; }
    });
    SECRET_FIELDS.forEach(function(id){
      var v = val(id); if(v !== ""){ body[id] = v; }
    });

    var es = null;

    fetch("/scan", {method:"POST", headers:{"Content-Type":"application/json"},
                    body: JSON.stringify(body)})
      .then(function(r){ return r.json().then(function(j){ return {ok:r.ok, body:j}; }); })
      .then(function(res){
        // Whatever happens next, this page is done holding the passwords.
        body = null;
        if(!res.ok){ onRejected(res.body || {}); return; }
        jobId = res.body.job;
        if(res.body.capped && res.body.capped.length){
          showNotice("Adjusted to this server's limits",
                     "Your request asked for more than this deployment allows, so it was " +
                     "trimmed to max pages " + res.body.params.max_pages + ", samples " +
                     res.body.params.samples + ", parallel " + res.body.params.concurrency + ".",
                     false);
        }
        statusEl.textContent = "Queued\\u2026";
        es = new EventSource(res.body.stream);
        wire(es);
      })
      .catch(function(){
        body = null;
        reset(); show(spin, false); setQueueBadge(null);
        statusEl.textContent = "Could not reach the server.";
        showNotice("Could not reach the server",
                   "The scan was not submitted. Check the connection and try again.", true);
        show(logwrap, false);
        show(idle, true);
      });

    // refused before anything was queued: rate limit, full queue, or the
    // domain-ownership gate for a multi-page audit
    function onRejected(d){
      if(es){ es.close(); }
      reset(); show(spin, false); setQueueBadge(null);
      var titles = { queue_full: "The queue is full",
                     hourly: "Scan limit reached",
                     concurrent: "A scan of yours is already running",
                     unverified: "Verify this domain first",
                     invalid: "That request can\\u2019t be scanned" };
      statusEl.textContent = titles[d.reason] || "Not accepted";
      showNotice(titles[d.reason] || "Not accepted",
                 d.message || "The scan was not accepted.", true);
      if(d.verification){ showVerification(d.verification); }
      show(logwrap, false);
      show(idle, true);
    }

    function wire(es){

    es.onmessage = function(e){ logEl.textContent += e.data + "\\n"; logEl.scrollTop = logEl.scrollHeight; };

    es.addEventListener("queued", function(e){
      var d = {};
      try { d = JSON.parse(e.data); } catch(err){ }
      statusEl.textContent = "Waiting for a free worker\\u2026";
      setSteps(0, false);
      setQueueBadge(d.position ? ("Queued \\u00b7 position " + d.position) : "Queued");
    });

    es.addEventListener("running", function(){
      statusEl.textContent = "Scanning\\u2026";
      setSteps(1, false);
      setQueueBadge("Running");
    });

    // the stream itself can still refuse: a job that expired before the
    // browser got back to it
    es.addEventListener("rejected", function(e){
      var d = {};
      try { d = JSON.parse(e.data); } catch(err){ }
      onRejected(d);
    });

    es.addEventListener("done", function(e){
      es.close(); reset();
      // payload: {"base":"/runs/<name>/","files":[...files the run wrote...]}
      var base = e.data, files = null, run = {};
      try {
        var payload = JSON.parse(e.data);
        if(payload && payload.base){ base = payload.base; files = payload.files || []; run = payload; }
      } catch(err){ /* older payload: the bare run folder */ }

      // How much of the site this report actually covers, said once, up front.
      var status = run.status || "", notice = run.notice || null;
      if(status === "failed"){
        showState("err", (notice && notice.title) || "The scan couldn\\u2019t be completed",
                  (notice && notice.message) || "No page could be measured.", run.coverage);
      } else if(status === "partial"){
        showState("warn", (notice && notice.title) || "Partial scan",
                  (notice && notice.message) ||
                  "Some pages could not be measured; the report covers the ones that were.",
                  run.coverage);
      } else if(status === "complete" && run.coverage && run.coverage.attempted){
        showState("ok", "Scan complete",
                  "Every page this run set out to measure was measured.", run.coverage);
      }

      function has(name){
        if(files !== null){ return files.indexOf(name) !== -1; }
        if(name === "report.html" || name === "report.pdf"){ return !!anyLH; }
        if(name === "accessibility.html"){ return !!a11y; }
        return !!security;
      }

      // preview the performance report when a Lighthouse category ran,
      // otherwise the accessibility one, otherwise security.
      var primary = null, cap = "Report preview";
      if(anyLH && has("report.html")){ primary = "report.html"; cap = "Performance report preview"; }
      else if(has("accessibility.html")){ primary = "accessibility.html"; cap = "Accessibility report preview"; }
      else if(has("security.html")){ primary = "security.html"; cap = "Security report preview"; }
      else if(has("report.html")){ primary = "report.html"; cap = "Performance report preview"; }

      show(spin, false);
      setQueueBadge(null);
      setSteps(2, true);
      if(!primary){
        ["openlink","pdflink","seclink","a11ylink"].forEach(function(id){ showLink(id, null); });
        statusEl.textContent = status === "failed" ? "Scan failed." : "No report was produced.";
        if(!notice){
          showState("err", "No report was produced",
                    "The scan finished without measuring any page. The technical log " +
                    "below has the details.", run.coverage);
        }
        show(byId("results"), false);
        show(idle, true);
        return;
      }
      byId("frame").src = base + primary;
      byId("cap").textContent = cap;
      showLink("openlink", base + primary);
      showLink("pdflink", has("report.pdf") ? base + "report.pdf" : null);
      showLink("seclink", has("security.html") ? base + "security.html" : null);
      showLink("a11ylink", has("accessibility.html") ? base + "accessibility.html" : null);
      show(byId("results"), true);
      show(idle, false);
      statusEl.textContent = (status === "partial") ? "Done - partial coverage." : "Done.";
    });
    es.addEventListener("fail", function(e){
      es.close(); reset(); show(spin, false); setQueueBadge(null);
      var why = "", note = null;
      try { var d = JSON.parse(e.data) || {}; why = d.message || ""; note = d.notice || null; }
      catch(err){ }
      statusEl.textContent = "Scan failed.";
      setSteps(1, false);
      document.querySelectorAll("#steps .step").forEach(function(el){ el.classList.remove("on"); });
      // The banner explains it; the raw reason stays in the log for whoever
      // wants it.
      showState("err", (note && note.title) || "The scan couldn\\u2019t be completed",
                (note && note.message) ||
                "Something went wrong before a report could be built.", null);
      logEl.textContent += "\\n--- scan failed" + (why ? ": " + why : "") + " ---\\n";
      show(idle, true);
    });
    es.onerror = function(){ es.close(); reset(); show(spin, false); setQueueBadge(null); };

    }   // wire()

    function reset(){ setBusy(false); }
  };

  // ---- preview harness ------------------------------------------------
  // Drives this page - the real one, not a mock-up - into any state it can be
  // in, so the design can be looked at without a scan, a browser or a site.
  // Nothing below runs unless a state is asked for by name: `?preview=partial`
  // on the live server, or the constant ui_preview.py writes into its files.
  (function preview(){
    var state = window.__PREVIEW_STATE__ ||
      (new URLSearchParams(window.location.search).get("preview") || "");
    if(!state){ return; }
    var demo = {
      ok:      {status:"complete", coverage:{ok:24, attempted:24, percent:100}},
      partial: {status:"partial",  coverage:{ok:19, attempted:24, percent:79}},
      failed:  {status:"failed",   coverage:{ok:0,  attempted:24, percent:0}}
    };
    byId("url").value = "https://harborlane.example";
    // The aside is fed by /api/limits, which a file:// preview cannot reach.
    // Stand-in numbers keep the layout honest; a live server overwrites them.
    if(!limits){
      byId("lim_pages").textContent = "50";
      byId("lim_samples").textContent = "3";
      byId("lim_parallel").textContent = "4";
      byId("lim_timeout").textContent = "15 min";
      show(byId("limitcard"), true);
    }
    show(idle, false);
    if(state === "advanced"){ document.querySelector("details.adv").open = true; show(idle, true); return; }
    if(state === "idle"){ show(idle, true); return; }

    if(state === "queued" || state === "running"){
      show(logwrap, true); setBusy(true);
      if(state === "queued"){
        setSteps(0, false); statusEl.textContent = "Waiting for a free worker\\u2026";
        setQueueBadge("Queued \\u00b7 position 2");
      } else {
        setSteps(1, false); statusEl.textContent = "Scanning\\u2026"; setQueueBadge("Running");
        logEl.textContent = "> crawling https://harborlane.example\\n" +
          "> 24 pages discovered\\n> auditing /collections/outerwear (12/24)\\n";
        byId("logdetails").open = true;
      }
      return;
    }
    if(state === "blocked"){
      showNotice("Verify this domain first",
        "A multi-page audit of harborlane.example needs proof that you control the domain. " +
        "Publish either token below, then re-check.", true);
      showVerification({meta_tag: '<meta name="site-audit-verification" content="sa_7f3c9e2b41d8">',
                        dns_record: 'site-audit-verification=sa_7f3c9e2b41d8'});
      show(idle, true);
      return;
    }
    if(state === "error"){
      showNotice("The queue is full",
        "This deployment is running as many scans as it will hold. Try again in a few minutes.", true);
      show(idle, true);
      return;
    }

    var run = demo[state === "failed" ? "failed" : (state === "partial" ? "partial" : "ok")];
    show(logwrap, true); show(spin, false); setSteps(2, true);
    if(run.status === "failed"){
      statusEl.textContent = "Scan failed.";
      showState("err", "The scan couldn\\u2019t be completed",
                "No page could be measured: every request to the site timed out.", run.coverage);
      show(idle, true);
      return;
    }
    if(run.status === "partial"){
      statusEl.textContent = "Done - partial coverage.";
      showState("warn", "Partial scan",
                "Some pages could not be measured; the report covers the ones that were.",
                run.coverage);
    } else {
      statusEl.textContent = "Done.";
      showState("ok", "Scan complete",
                "Every page this run set out to measure was measured.", run.coverage);
    }
    byId("cap").textContent = "Performance report preview";
    showLink("openlink", window.__PREVIEW_REPORT__ || "#");
    showLink("pdflink", "#");
    showLink("seclink", "#");
    showLink("a11ylink", "#");
    if(window.__PREVIEW_REPORT__){ byId("frame").src = window.__PREVIEW_REPORT__; }
    show(byId("results"), true);
  })();
</script>
</body></html>"""
