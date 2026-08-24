#!/usr/bin/env python3
"""
The dashboard app's stylesheet.

Same shape as `report_css`: a `:root` of tokens from `design_tokens`, then the
shared component layer from `design_css.PRIMITIVES` verbatim, then the parts
only the app has - the scan form, the progress rail, the results frame.

    ui_css.stylesheet(brand)   -> the whole <style> body
    ui_css.font_link(brand)    -> the <link> tags, or "" for a system stack

Nothing below hard-codes a colour, a type size, a spacing step, a radius or a
shadow. Every one of them is a token, which is what makes the whole app
re-skinnable from `brand.json` alone - see DESIGN.md.
"""

import design_tokens
from design_css import PRIMITIVES

# The one webfont the default skin asks for. A brand that sets its own
# `font_stack` gets no font link at all rather than a stale download - see
# `font_link` below.
WEBFONT_FAMILY = "Inter"
WEBFONT_HREF = ("https://fonts.googleapis.com/css2?"
                "family=Inter:wght@400;500;600;700;800&display=swap")


def font_link(brand):
    """`<link>` markup for the brand's font, or "" when it needs none.

    Rebranding onto a system stack (or a self-hosted face) should not leave the
    page fetching a font it no longer uses, so the link is emitted only while
    the brand's own stack still names the family we know how to fetch.
    """
    if WEBFONT_FAMILY.lower() not in (brand.font_stack or "").lower():
        return ""
    return ('<link rel="preconnect" href="https://fonts.googleapis.com">\n'
            '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
            f'<link href="{WEBFONT_HREF}" rel="stylesheet">')


def stylesheet(brand):
    """The app's whole stylesheet for `brand`."""
    return (f":root {{\n{design_tokens.css_variables(brand, '  ')}\n}}\n"
            f"{PRIMITIVES}\n{_APP}")


_APP = """
/* ========================================================== app: base ==== */
* { box-sizing: border-box; }
html, body { margin: 0; }
html { scroll-behavior: smooth; }
body {
  font-family: var(--font); font-size: var(--t-base); line-height: var(--lh);
  color: var(--ink); background: var(--paper-2);
  min-height: 100vh; padding-top: var(--nav-h);
  -webkit-font-smoothing: antialiased;
}
ul { margin: 0; padding: 0; list-style: none; }
/* `hidden` is how this page hides anything, so it has to beat every display
   rule below it. Nothing sets style.display by hand - see show() in ui_page. */
[hidden] { display: none !important; }
/* One focus ring, for everything the keyboard can reach. */
:focus-visible { outline: none; box-shadow: var(--focus-ring); border-radius: var(--r-sm); }
.sr-only { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0;
  overflow: hidden; clip: rect(0 0 0 0); clip-path: inset(50%); white-space: nowrap; border: 0; }

/* ========================================================== app: chrome == */
/* The same top bar the report wears, so the two obviously belong together. */
.topnav { position: fixed; top: 0; left: 0; right: 0; z-index: 40;
  height: var(--nav-h); display: flex; align-items: center; gap: var(--sp-4);
  padding: 0 var(--sp-5); background: var(--paper);
  border-bottom: 1px solid var(--line); box-shadow: var(--shadow-xs); }
.topnav .brandmark { display: flex; align-items: center; gap: var(--sp-3);
  min-width: 0; text-decoration: none; color: var(--ink); }
.topnav .brandmark img { max-height: 28px; max-width: 132px; object-fit: contain; display: block; }
.topnav .dot { width: 30px; height: 30px; border-radius: var(--r-sm); flex: 0 0 auto;
  display: grid; place-items: center; background: var(--grad);
  color: var(--brand-ink); font-size: var(--t-sm); font-weight: 800;
  letter-spacing: .02em; box-shadow: var(--shadow-sm); }
.topnav .names { min-width: 0; }
.topnav .names .n { font-size: var(--t-base); font-weight: 800;
  letter-spacing: var(--track-tight); line-height: var(--lh-tight);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.topnav .names .r { font-size: var(--t-xs); font-weight: 700; color: var(--ink-3);
  letter-spacing: var(--track-caps); text-transform: uppercase; }
.topnav .spacer { flex: 1 1 auto; }
.topnav .env { display: inline-flex; align-items: center; gap: 6px;
  font-size: var(--t-xs); font-weight: 700; color: var(--ink-3);
  border: 1px solid var(--line); border-radius: var(--r-pill);
  padding: 5px var(--sp-3); background: var(--paper-2); white-space: nowrap; }
.topnav .env .led { width: 7px; height: 7px; border-radius: 50%; background: var(--good); }
@media (max-width: 720px) {
  .topnav { padding: 0 var(--sp-4); gap: var(--sp-3); }
  /* Both are context, not controls: on a phone the name is the whole bar. */
  .topnav .names .r, .topnav .env { display: none; }
}

.shell { max-width: var(--app-w); margin: 0 auto; padding: var(--sp-6) var(--sp-5) var(--sp-8); }

/* ------------------------------------------------------------------ hero */
.hero { margin-bottom: var(--sp-5); }
.hero h1 { font-size: var(--t-2xl); margin-bottom: var(--sp-2); }
.hero p { color: var(--ink-2); font-size: var(--t-md); max-width: 72ch; margin: 0; }

/* --------------------------------------------------------------- layout */
/* Two columns where there is room for them: the work on the left, the things
   that explain the work on the right. One column below 980px, aside last. */
.layout { display: grid; gap: var(--sp-5); align-items: start;
  grid-template-columns: minmax(0, 1fr) 340px; }
.layout > .full { grid-column: 1 / -1; }
@media (max-width: 980px) {
  .layout { grid-template-columns: minmax(0, 1fr); }
  .layout > .full { grid-column: auto; }
}
@media (max-width: 640px) {
  .shell { padding: var(--sp-5) var(--sp-4) var(--sp-7); }
}

/* Section header inside a card: title, hint, and an optional right-hand slot. */
.card-head { display: flex; align-items: flex-start; gap: var(--sp-4);
  margin-bottom: var(--sp-5); }
.card-head h2 { font-size: var(--t-lg); }
.card-head p { margin: var(--sp-1) 0 0; color: var(--ink-3); font-size: var(--t-sm);
  max-width: 60ch; }
.card-head .aside { margin-left: auto; flex: 0 0 auto; }

/* ========================================================== app: forms === */
/* One field component. `.f` stacks a label over a control; `.f.wide` and
   `.f.full` are the only size variants, so nothing needs a bespoke width. */
.f { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.f > span { font-size: var(--t-xs); font-weight: 700;
  letter-spacing: .06em; text-transform: uppercase; color: var(--ink-3); }
.f.wide { grid-column: span 2; }
.f.full { grid-column: 1 / -1; }
/* A two-digit value does not need a 260px box. The cell still holds its
   column, so the grid stays aligned; only the control is sized to its
   content. */
.f.tiny input { max-width: 130px; }
@media (max-width: 560px) { .f.wide { grid-column: 1 / -1; } }

/* The controls themselves - one border, one radius, one focus ring. */
input[type=text], input[type=password], input[type=number], input[type=url],
select, textarea {
  width: 100%; font: 500 var(--t-base)/1.4 var(--font); color: var(--ink);
  background: var(--paper); border: 1px solid var(--line);
  border-radius: var(--r-md); padding: 10px var(--sp-3);
  transition: border-color var(--dur-fast) var(--ease),
              box-shadow var(--dur-fast) var(--ease),
              background var(--dur-fast) var(--ease); }
input::placeholder, textarea::placeholder { color: var(--ink-3); opacity: 1; }
input[type=text]:hover, input[type=password]:hover, input[type=number]:hover,
select:hover, textarea:hover { border-color: var(--ink-4); }
input:focus, select:focus, textarea:focus {
  outline: none; border-color: var(--brand); box-shadow: var(--focus-ring); }
input:disabled, select:disabled, textarea:disabled {
  background: var(--paper-3); color: var(--ink-3); cursor: not-allowed; }
select { appearance: none; -webkit-appearance: none; cursor: pointer;
  padding-right: var(--sp-6); font-weight: 600;
  /* The chevron is drawn from the ink token, so it re-skins with the text. */
  background-image: linear-gradient(45deg, transparent 50%, currentColor 50%),
                    linear-gradient(135deg, currentColor 50%, transparent 50%);
  background-position: right var(--sp-4) center, right var(--sp-3) center;
  background-size: 5px 5px, 5px 5px; background-repeat: no-repeat;
  color: var(--ink-2); }
textarea { min-height: 78px; resize: vertical;
  font: 500 var(--t-sm)/var(--lh) var(--mono); }
input[type=number] { font-variant-numeric: tabular-nums; font-weight: 600; }

/* The URL field: the one input this whole page exists for, sized like it. */
.urlfield { position: relative; }
.urlfield svg { position: absolute; left: var(--sp-4); top: 50%; transform: translateY(-50%);
  width: 18px; height: 18px; stroke: var(--ink-3); pointer-events: none; }
.urlfield input { padding: var(--sp-3) var(--sp-4) var(--sp-3) var(--sp-7);
  font-size: var(--t-md); font-weight: 500; }
.urlfield input:focus + svg, .urlfield input:hover + svg { stroke: var(--brand); }

/* Segmented control - device, and anything else with two or three choices. */
.seg { display: inline-flex; align-items: stretch; background: var(--paper-3);
  border: 1px solid var(--line); border-radius: var(--r-md); padding: 3px; gap: 3px;
  /* matches the height of the input it sits beside in `.basics` */
  min-height: 50px; }
.seg button { border: 0; background: transparent; cursor: pointer;
  font: 600 var(--t-sm)/1 var(--font); color: var(--ink-3);
  padding: 9px var(--sp-4); border-radius: var(--r-sm);
  display: inline-flex; align-items: center; gap: 7px;
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease); }
.seg button svg { width: 15px; height: 15px; stroke: currentColor; }
.seg button:hover { color: var(--ink); }
.seg button:focus-visible { outline: none; box-shadow: var(--focus-ring); }
.seg button.on { background: var(--paper); color: var(--brand-dark);
  box-shadow: var(--shadow-sm); }

/* Toggle switch. The label is the hit target; the switch just shows state. */
.sw { display: inline-flex; align-items: center; gap: var(--sp-3);
  font: 500 var(--t-base)/var(--lh-snug) var(--font); color: var(--ink-2);
  cursor: pointer; }
.sw:hover { color: var(--ink); }
.switch { position: relative; width: 40px; height: 23px; flex: 0 0 auto; }
.switch input { position: absolute; opacity: 0; width: 100%; height: 100%;
  margin: 0; cursor: pointer; }
.track { position: absolute; inset: 0; background: var(--ink-4);
  border-radius: var(--r-pill); pointer-events: none;
  transition: background var(--dur) var(--ease); }
.track:before { content: ""; position: absolute; width: 17px; height: 17px;
  left: 3px; top: 3px; background: var(--paper); border-radius: 50%;
  box-shadow: var(--shadow-sm); transition: transform var(--dur) var(--ease); }
.switch input:checked + .track { background: var(--brand); }
.switch input:checked + .track:before { transform: translateX(17px); }
.switch input:focus-visible + .track { box-shadow: var(--focus-ring); }
.switch input:disabled + .track { opacity: .5; }

/* Chip rows. */
.chips { display: flex; flex-wrap: wrap; gap: var(--sp-2); }

/* The basics: URL and device side by side, stacking on a narrow screen. */
.basics { display: grid; grid-template-columns: minmax(0, 1fr) auto;
  gap: var(--sp-4); align-items: end; }
@media (max-width: 640px) { .basics { grid-template-columns: minmax(0, 1fr); } }

.block { margin-top: var(--sp-5); }
.block > .hint { display: block; margin-top: var(--sp-3); }
.hint { color: var(--ink-3); font-size: var(--t-sm); line-height: var(--lh); }
.hint code { background: var(--line-2); border-radius: var(--r-xs); padding: 1px 5px;
  color: var(--ink-2); }

/* ------------------------------------------------- advanced options ---- */
/* Everything a routine scan does not need, behind one disclosure, grouped so
   the panel reads as five short forms instead of thirty controls. */
details.adv { margin-top: var(--sp-5); border: 1px solid var(--line);
  border-radius: var(--r-md); background: var(--paper-2); }
details.adv > summary { cursor: pointer; list-style: none;
  display: flex; align-items: center; gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-4); border-radius: var(--r-md);
  font: 700 var(--t-base)/1.3 var(--font); color: var(--ink);
  transition: background var(--dur-fast) var(--ease); }
details.adv > summary::-webkit-details-marker { display: none; }
details.adv > summary:hover { background: var(--paper-3); }
details.adv > summary:focus-visible { outline: none; box-shadow: var(--focus-ring); }
details.adv > summary .caret { width: 16px; height: 16px; stroke: var(--brand);
  flex: 0 0 auto; transition: transform var(--dur) var(--ease); }
details.adv[open] > summary .caret { transform: rotate(90deg); }
details.adv > summary .sub { margin-left: auto; font-size: var(--t-sm);
  font-weight: 600; color: var(--ink-3); }
@media (max-width: 760px) { details.adv > summary .sub { display: none; } }
details.adv[open] > summary { border-bottom: 1px solid var(--line);
  border-radius: var(--r-md) var(--r-md) 0 0; }
.adv-body { padding: var(--sp-5) var(--sp-4) var(--sp-4); background: var(--paper);
  border-radius: 0 0 var(--r-md) var(--r-md); }

.agroup { padding-top: var(--sp-5); margin-top: var(--sp-5);
  border-top: 1px solid var(--line-2); }
.agroup:first-child { padding-top: 0; margin-top: 0; border-top: 0; }
.agroup > .atitle { display: flex; align-items: center; gap: var(--sp-2);
  font: 800 var(--t-sm)/1.3 var(--font); letter-spacing: .06em;
  text-transform: uppercase; color: var(--ink); margin-bottom: var(--sp-2); }
.agroup > .atitle svg { width: 14px; height: 14px; stroke: currentColor; }
.agroup > .ahint { font-size: var(--t-sm); line-height: var(--lh);
  color: var(--ink-3); margin-bottom: var(--sp-4); max-width: 78ch; }
/* Panels that need to look different because they behave differently. */
.agroup.boxed { border: 1px solid var(--brand-line); background: var(--brand-softer);
  border-radius: var(--r-md); padding: var(--sp-4); margin-top: var(--sp-5); }
.agroup.boxed > .atitle { color: var(--brand-dark); }
.agroup.secret { border: 1px solid var(--cat-a11y-standards-line);
  background: var(--paper); border-radius: var(--r-md);
  padding: var(--sp-4); margin-top: var(--sp-5); }
.agroup.secret > .atitle { color: var(--cat-a11y-standards); }

/* The field grid: as many columns as fit, nothing narrower than 200px. */
.fieldgrid { display: grid; gap: var(--sp-4);
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); align-items: end; }
.fieldgrid + .fieldgrid { margin-top: var(--sp-4); }
.fieldgrid .sw { grid-column: 1 / -1; }
.fieldgrid .btnrow { grid-column: 1 / -1; display: flex; flex-wrap: wrap;
  align-items: center; gap: var(--sp-2); }

/* The credentials panel says on its face what happens to what you type. */
.privacy { display: flex; gap: var(--sp-3); align-items: flex-start;
  font-size: var(--t-sm); line-height: var(--lh); color: var(--ink-2);
  background: var(--paper-2); border: 1px solid var(--line);
  border-radius: var(--r-md); padding: var(--sp-3) var(--sp-4); margin-bottom: var(--sp-4); }
.privacy svg { width: 16px; height: 16px; stroke: var(--cat-a11y-standards);
  flex: 0 0 auto; margin-top: 2px; }
.privacy strong { color: var(--ink); }

.pmsg { font-size: var(--t-sm); font-weight: 600; color: var(--ink-3); }
.pmsg.err { color: var(--poor); }
.pmsg.ok { color: var(--good); }

/* ----------------------------------------------------------------- CTA -- */
.cta { display: flex; align-items: center; gap: var(--sp-4); flex-wrap: wrap;
  margin-top: var(--sp-5); padding-top: var(--sp-5); border-top: 1px solid var(--line-2); }
.go { border: 0; cursor: pointer; background: var(--brand); color: var(--brand-ink);
  font: 700 var(--t-md)/1 var(--font); padding: var(--sp-4) var(--sp-6);
  border-radius: var(--r-md); display: inline-flex; align-items: center; gap: var(--sp-2);
  box-shadow: var(--shadow-sm);
  transition: background var(--dur-fast) var(--ease),
              box-shadow var(--dur-fast) var(--ease),
              transform var(--dur-fast) var(--ease); }
.go:hover { background: var(--brand-hover); box-shadow: var(--shadow); transform: translateY(-1px); }
.go:active { background: var(--brand-active); transform: translateY(0); box-shadow: none; }
.go:focus-visible { outline: none; box-shadow: var(--focus-ring); }
/* Busy, not broken: the accent at tint strength still reads as this
   product's button, and the label stays legible while it works. */
.go:disabled { background: var(--brand-soft); color: var(--brand-dark);
  cursor: not-allowed; transform: none; box-shadow: none; }
.go svg { width: 18px; height: 18px; stroke: currentColor; }
.cta .hint { flex: 1 1 240px; }

/* ======================================================== app: progress == */
.panel { margin-top: var(--sp-5); }
.panel[hidden] { display: none; }

/* Three steps, in the order they happen. `.on` is where the run is now. */
.steps { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--sp-2); margin-bottom: var(--sp-5); }
.step { display: flex; align-items: center; gap: var(--sp-3);
  padding: var(--sp-3); border: 1px solid var(--line); border-radius: var(--r-md);
  background: var(--paper-2); min-width: 0;
  transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease); }
.step .idx { width: 24px; height: 24px; border-radius: 50%; flex: 0 0 auto;
  display: grid; place-items: center; font-size: var(--t-xs); font-weight: 800;
  background: var(--line); color: var(--ink-3); }
.step .txt { min-width: 0; }
.step .t { font-size: var(--t-sm); font-weight: 700; color: var(--ink-3);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.step .s { font-size: var(--t-xs); color: var(--ink-3); }
.step.on { border-color: var(--brand); background: var(--brand-softer); }
.step.on .idx { background: var(--brand); color: var(--brand-ink); }
.step.on .t { color: var(--brand-dark); }
.step.done .idx { background: var(--good); color: var(--paper); }
.step.done .t { color: var(--ink-2); }
@media (max-width: 640px) { .steps { grid-template-columns: minmax(0, 1fr); } }

.statusline { display: flex; align-items: center; gap: var(--sp-3);
  font-size: var(--t-base); font-weight: 600; color: var(--ink-2);
  margin-bottom: var(--sp-4); }
.spin { width: 15px; height: 15px; flex: 0 0 auto; border-radius: 50%;
  border: 2.5px solid var(--line); border-top-color: var(--brand);
  animation: spin .7s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.qbadge { margin-left: auto; font-size: var(--t-xs); font-weight: 800;
  letter-spacing: .06em; text-transform: uppercase; color: var(--brand-dark);
  background: var(--brand-soft); border-radius: var(--r-pill); padding: 4px var(--sp-3); }

/* Coverage: the one number that says how much of the site the run actually
   measured, drawn with the report's own ring so they read as one thing. */
.coverwrap { display: flex; align-items: center; gap: var(--sp-4);
  margin-top: var(--sp-3); padding-top: var(--sp-3); border-top: 1px solid var(--line-2); }
.coverwrap .ring { margin: 0; flex: 0 0 auto; }
.coverwrap .ctext { font-size: var(--t-sm); color: var(--ink-2); line-height: var(--lh); }
.coverwrap .ctext b { color: var(--ink); }

/* The log is for the curious, so it starts closed and never speaks first. */
.logdetails { margin-top: var(--sp-4); }
.logdetails > summary { cursor: pointer; list-style: none; display: inline-flex;
  align-items: center; gap: var(--sp-2); font-size: var(--t-sm); font-weight: 700;
  color: var(--ink-3); padding: var(--sp-1) 0; }
.logdetails > summary::-webkit-details-marker { display: none; }
.logdetails > summary:hover { color: var(--brand-dark); }
.logdetails > summary:focus-visible { outline: none; box-shadow: var(--focus-ring); }
.logdetails > summary::before { content: "+"; font-family: var(--mono);
  font-weight: 700; color: var(--brand); }
.logdetails[open] > summary::before { content: "-"; }
#log { margin-top: var(--sp-2); background: var(--brand-deep); color: var(--brand-soft);
  font: 500 var(--t-sm)/var(--lh-loose) var(--mono); padding: var(--sp-3) var(--sp-4);
  border-radius: var(--r-md); max-height: 300px; overflow: auto;
  white-space: pre-wrap; word-break: break-word; }

/* ========================================================= app: states === */
/* Empty, error, partial and blocked all get a real design, because a run that
   produced nothing is the moment a person most needs to be told why. */
.empty { text-align: center; padding: var(--sp-7) var(--sp-5); }
.empty .icon { width: 46px; height: 46px; margin: 0 auto var(--sp-4);
  border-radius: var(--r-md); display: grid; place-items: center;
  background: var(--brand-soft); color: var(--brand-dark); }
.empty .icon svg { width: 22px; height: 22px; stroke: currentColor; }
.empty h3 { margin-bottom: var(--sp-2); }
.empty p { color: var(--ink-3); font-size: var(--t-sm); max-width: 46ch;
  margin: 0 auto var(--sp-4); }
.empty .steps-list { text-align: left; max-width: 34ch; margin: 0 auto;
  display: grid; gap: var(--sp-2); }
.empty .steps-list li { display: flex; gap: var(--sp-3); align-items: flex-start;
  font-size: var(--t-sm); color: var(--ink-2); }
.empty .steps-list .n { width: 20px; height: 20px; border-radius: 50%; flex: 0 0 auto;
  display: grid; place-items: center; font-size: var(--t-xs); font-weight: 800;
  background: var(--brand-soft); color: var(--brand-dark); }

/* Verification: the site said no, here is exactly how to get a yes. */
.verify { margin-top: var(--sp-4); display: grid; gap: var(--sp-2); }
.verify .vopt { display: grid; gap: 6px; }
.verify code { display: block; background: var(--paper); color: var(--ink);
  border: 1px solid var(--line); border-radius: var(--r-sm); padding: var(--sp-3);
  user-select: all; font: 500 var(--t-sm)/var(--lh) var(--mono); word-break: break-all; }
.verify .vbtns { display: flex; gap: var(--sp-2); flex-wrap: wrap; margin-top: var(--sp-2); }

/* ======================================================== app: results === */
.rbar { display: flex; gap: var(--sp-2); flex-wrap: wrap; margin-bottom: var(--sp-4); }
/* The buttons are all one neutral shape; the icon carries which report it is. */
#seclink svg { stroke: var(--cat-security); }
#a11ylink svg { stroke: var(--cat-a11y-standards); }
#pdflink svg { stroke: var(--ink-3); }
.frame { background: var(--paper); border: 1px solid var(--line);
  border-radius: var(--r-lg); overflow: hidden; box-shadow: var(--shadow); }
.frame .cap { display: flex; align-items: center; gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-4); font-size: var(--t-sm); font-weight: 700;
  color: var(--ink-3); background: var(--paper-2); border-bottom: 1px solid var(--line); }
.frame .cap svg { width: 14px; height: 14px; stroke: currentColor; }
.frame .cap .dots { display: flex; gap: 5px; margin-right: var(--sp-2); }
.frame .cap .dots i { width: 8px; height: 8px; border-radius: 50%; background: var(--line); }
.frame iframe { width: 100%; height: 68vh; min-height: 420px; border: 0;
  background: var(--paper); display: block; }

/* ---------------------------------------------------------------- aside - */
.side { display: grid; gap: var(--sp-4); }
.side .card { padding: var(--sp-4); }
.side h3 { font-size: var(--t-base); margin-bottom: var(--sp-3); }
.bullets { display: grid; gap: var(--sp-3); }
.bullets li { position: relative; padding-left: var(--sp-4);
  font-size: var(--t-sm); line-height: var(--lh); color: var(--ink-2); }
.bullets li::before { content: ""; position: absolute; left: 0; top: 8px;
  width: 6px; height: 6px; border-radius: 50%; background: var(--brand); }
.deflist { display: grid; gap: var(--sp-2); }
.deflist div { display: flex; align-items: baseline; justify-content: space-between;
  gap: var(--sp-3); font-size: var(--t-sm); }
.deflist dt { color: var(--ink-3); }
.deflist dd { margin: 0; font-weight: 700; color: var(--ink);
  font-variant-numeric: tabular-nums; }

.foot { margin-top: var(--sp-6); padding-top: var(--sp-4);
  border-top: 1px solid var(--line); text-align: center;
  font-size: var(--t-sm); color: var(--ink-3); }
.foot a { color: var(--ink-2); }
"""
