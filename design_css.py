#!/usr/bin/env python3
"""
The component layer, shared verbatim by both surfaces.

`ui_css.stylesheet()` (the dashboard app) and `report_css.stylesheet()` (the
generated report) both splice `PRIMITIVES` in, unchanged, right after their
`:root` block. A card, a button, a pill, a chip, a table, a score ring and a
note look the same in the app as they do in the report because they are the
same rules, not two copies that agree today.

Rules of the road for anything added here:

  * media-agnostic. Nothing that only makes sense on screen (sticky, hover-only
    affordances) or only on paper (`@page`, break control) belongs here - those
    live in the surface's own `@media` block.
  * tokens only. No hex, no px outside the spacing/type/radius scales.
  * every interactive component states all four states: rest, hover, focus
    (`:focus-visible`, one ring for the whole product), active, disabled.

`tests/test_design_system.py` checks both of the last two.
"""

PRIMITIVES = """
/* ================================================== shared components ====
   One definition, two surfaces. See design_css.py.
   ======================================================================== */

/* ------------------------------------------------------------ typography */
h1, h2, h3, h4 { margin: 0; line-height: var(--lh-tight);
  letter-spacing: var(--track-tight); font-weight: 700; }
h1 { font-size: var(--t-3xl); }
h2 { font-size: var(--t-xl); }
h3 { font-size: var(--t-md); }
h4 { font-size: var(--t-base); }
p { margin: 0 0 var(--sp-3); }
a { color: var(--brand); }
code, pre { font-family: var(--mono); font-size: var(--t-xs); }

.eyebrow { font-size: var(--t-xs); font-weight: 800; letter-spacing: .14em;
  text-transform: uppercase; color: var(--brand); margin: 0 0 var(--sp-1); }
.lede { font-size: var(--t-md); line-height: var(--lh-loose); color: var(--ink-2);
  max-width: 70ch; }
.muted { color: var(--ink-3); }
/* The one small-caps label used for every field, column and section tag. */
.lbl { display: block; font-size: var(--t-xs); font-weight: 800;
  letter-spacing: var(--track-wide); text-transform: uppercase;
  color: var(--ink-3); margin-bottom: var(--sp-2); }

/* ----------------------------------------------------------------- cards */
.card { background: var(--paper); border: 1px solid var(--line);
  border-radius: var(--r-lg); padding: var(--sp-5); box-shadow: var(--shadow); }
.card.flat { box-shadow: none; }
.card.quiet { background: var(--paper-2); box-shadow: none; }
.card-title { font-size: var(--t-md); font-weight: 700; margin: 0 0 var(--sp-2); }

/* --------------------------------------------------------------- buttons */
/* `.btn` is the whole button system: neutral by default, `.primary` for the
   one affirmative action in a view, `.ghost` for tertiary, `.danger` for
   destructive. Size comes from `.sm` / `.lg`, never from a one-off padding. */
.btn { display: inline-flex; align-items: center; justify-content: center;
  gap: var(--sp-2); text-decoration: none; white-space: nowrap; cursor: pointer;
  font: 700 var(--t-sm)/1.2 var(--font); padding: 9px var(--sp-4);
  border: 1px solid var(--line); border-radius: var(--r-md);
  background: var(--paper); color: var(--ink);
  box-shadow: var(--shadow-xs);
  transition: background var(--dur-fast) var(--ease),
              border-color var(--dur-fast) var(--ease),
              color var(--dur-fast) var(--ease),
              box-shadow var(--dur-fast) var(--ease),
              transform var(--dur-fast) var(--ease); }
.btn:hover { border-color: var(--brand-line); color: var(--brand-dark);
  background: var(--brand-softer); }
.btn:active { transform: translateY(1px); box-shadow: none; }
.btn:focus-visible { outline: none; box-shadow: var(--focus-ring); border-color: var(--brand); }
.btn[disabled], .btn:disabled, .btn[aria-disabled="true"] {
  opacity: .55; cursor: not-allowed; transform: none;
  background: var(--paper-3); color: var(--ink-3); border-color: var(--line);
  box-shadow: none; }
.btn.primary { background: var(--brand); border-color: var(--brand);
  color: var(--brand-ink); box-shadow: var(--shadow-sm); }
.btn.primary:hover { background: var(--brand-hover); border-color: var(--brand-hover);
  color: var(--brand-ink); }
.btn.primary:active { background: var(--brand-active); border-color: var(--brand-active); }
.btn.ghost { background: transparent; border-color: transparent; color: var(--ink-2);
  box-shadow: none; }
.btn.ghost:hover { background: var(--brand-softer); color: var(--brand-dark); }
.btn.danger { color: var(--poor); border-color: var(--poor-line); }
.btn.danger:hover { background: var(--poor-soft); border-color: var(--poor); color: var(--poor); }
.btn.sm { font-size: var(--t-xs); padding: 6px var(--sp-3); }
.btn.lg { font-size: var(--t-md); padding: var(--sp-3) var(--sp-5); border-radius: var(--r-md); }
.btn svg { width: 15px; height: 15px; stroke: currentColor; flex: 0 0 auto; }

/* ----------------------------------------------------------------- pills */
/* A verdict, in one word. Colour is the semantic palette, never the brand. */
.pill { display: inline-block; font-size: var(--t-xs); font-weight: 800;
  letter-spacing: .05em; text-transform: uppercase; padding: 3px 9px;
  border-radius: var(--r-pill); white-space: nowrap;
  background: var(--line-2); color: var(--ink-2); }
.pill.good, .pill.pass { background: var(--good-soft); color: var(--good); }
.pill.avg,  .pill.unk  { background: var(--avg-soft);  color: var(--avg); }
.pill.poor, .pill.fail { background: var(--poor-soft); color: var(--poor); }
.pill.na { background: var(--line-2); color: var(--ink-3); }
.pill.brand { background: var(--brand-soft); color: var(--brand-dark); }

/* ------------------------------------------------------------------ tags */
.tag { font-size: var(--t-xs); font-weight: 700; letter-spacing: .04em;
  padding: 3px 9px; border-radius: var(--r-pill); background: var(--line-2);
  color: var(--ink-2); white-space: nowrap; }
.tag.sev { color: var(--paper); }
.tag.sev.critical { background: var(--sev-critical); }
.tag.sev.serious  { background: var(--sev-serious); }
.tag.sev.moderate { background: var(--sev-moderate); }
.tag.sev.minor    { background: var(--sev-minor); }
.tag.cat { background: var(--brand-soft); color: var(--brand-dark); }

/* ----------------------------------------------------------------- chips */
/* Base: a small labelled token (the report's page lists, "+4 more").
   Variant: the app's selectable chip, which is the same token made pressable -
   `[data-cat]` / `[data-std]` mark the ones that toggle. */
.chip { display: inline-block; font-size: var(--t-xs); padding: 2px 8px;
  border-radius: var(--r-pill); background: var(--line-2); color: var(--ink-3);
  font-weight: 700; white-space: nowrap; }
.chip[data-cat], .chip[data-std] {
  --c: var(--brand); --c-soft: var(--brand-soft);
  display: inline-flex; align-items: center; gap: var(--sp-2); cursor: pointer;
  user-select: none; white-space: normal;
  padding: 9px var(--sp-4); font-size: var(--t-sm); font-weight: 600;
  border: 1px solid var(--line); border-radius: var(--r-md);
  background: var(--paper); color: var(--ink-2);
  transition: border-color var(--dur-fast) var(--ease),
              background var(--dur-fast) var(--ease),
              color var(--dur-fast) var(--ease),
              box-shadow var(--dur-fast) var(--ease); }
.chip[data-cat] .dot, .chip[data-std] .dot {
  width: 8px; height: 8px; border-radius: 50%; background: var(--c);
  opacity: .45; flex: 0 0 auto; transition: opacity var(--dur-fast) var(--ease); }
.chip[data-cat]:hover, .chip[data-std]:hover {
  border-color: var(--c); color: var(--ink); background: var(--paper); }
.chip[data-cat]:focus-visible, .chip[data-std]:focus-visible {
  outline: none; box-shadow: var(--focus-ring); border-color: var(--brand); }
/* Selected: the category's own colour, at tint strength. Six chips on at once
   have to sit together on one card, so the colour identifies the category
   rather than competing for the eye - the accent stays the brand's. */
.chip.on[data-cat], .chip.on[data-std] {
  background: var(--c-soft); border-color: var(--c); color: var(--c);
  font-weight: 700; box-shadow: inset 0 0 0 1px var(--c); }
.chip.on[data-cat]:hover, .chip.on[data-std]:hover {
  background: var(--c-soft); color: var(--c); }
.chip.on[data-cat] .dot, .chip.on[data-std] .dot { opacity: 1;
  box-shadow: 0 0 0 3px var(--c-soft); }
.chip[data-cat="performance"] { --c: var(--cat-performance); --c-soft: var(--cat-performance-soft); }
.chip[data-cat="accessibility"] { --c: var(--cat-accessibility); --c-soft: var(--cat-accessibility-soft); }
.chip[data-cat="best-practices"] { --c: var(--cat-best-practices); --c-soft: var(--cat-best-practices-soft); }
.chip[data-cat="seo"] { --c: var(--cat-seo); --c-soft: var(--cat-seo-soft); }
.chip[data-cat="security"] { --c: var(--cat-security); --c-soft: var(--cat-security-soft); }
.chip[data-cat="a11y-standards"] { --c: var(--cat-a11y-standards); --c-soft: var(--cat-a11y-standards-soft); }
.chip[data-std] { --c: var(--cat-a11y-standards); --c-soft: var(--cat-a11y-standards-soft);
  padding: 7px var(--sp-3); font-size: var(--t-sm); }

/* ---------------------------------------------------------------- tables */
table { width: 100%; border-collapse: collapse; background: var(--paper);
  border: 1px solid var(--line); border-radius: var(--r-md); overflow: hidden; }
th, td { padding: 9px var(--sp-3); text-align: left; font-size: var(--t-sm);
  border-bottom: 1px solid var(--line-2); }
th { background: var(--paper-2); font-size: var(--t-xs); text-transform: uppercase;
  letter-spacing: .06em; color: var(--ink-3); font-weight: 800; }
tbody tr:last-child td { border-bottom: 0; }
td.num, th.num { text-align: center; }
td.score { text-align: center; font-weight: 800; }
td.score.good { color: var(--good); } td.score.avg { color: var(--avg); }
td.score.poor { color: var(--poor); } td.score.na { color: var(--na); }

/* ------------------------------------------------------ rings and gauges */
/* The SVG comes from report_charts (server) or the app's own ring() (browser);
   both emit this markup, so both are styled from here. */
.ring { display: block; margin: 0 auto; }
.ring-label { fill: var(--ink-3); font-weight: 600; letter-spacing: .04em;
  text-transform: uppercase; }
.legend { display: flex; flex-wrap: wrap; gap: var(--sp-4); margin-top: var(--sp-4);
  font-size: var(--t-sm); color: var(--ink-3); }
.legend .k { display: inline-flex; align-items: center; gap: 6px; }
.legend .k b { color: var(--ink); font-weight: 800; }
.swatch { width: 11px; height: 11px; border-radius: var(--r-xs); display: inline-block; }

/* ------------------------------------------------------------------ KPIs */
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(124px, 1fr));
  gap: var(--sp-3); }
.kpi { background: var(--paper); border: 1px solid var(--line);
  border-radius: var(--r-md); padding: var(--sp-3) var(--sp-4); }
.kpi b { display: block; font-size: var(--t-2xl); line-height: var(--lh-tight);
  letter-spacing: -.02em; }
.kpi span { font-size: var(--t-xs); text-transform: uppercase;
  letter-spacing: var(--track-wide); color: var(--ink-3); font-weight: 700; }
.kpi.critical b { color: var(--sev-critical); }
.kpi.serious b  { color: var(--sev-serious); }
.kpi.quick b    { color: var(--good); }

/* ----------------------------------------------------------------- notes */
/* One banner, four tones. Anything the product has to *say* - a caveat, a
   refusal, a partial result, a clean pass - is one of these. */
.note { border: 1px solid var(--brand-line); background: var(--brand-softer);
  border-left: 3px solid var(--brand); border-radius: var(--r-md);
  padding: var(--sp-4); color: var(--ink-2); }
.note > .ntitle { font: 700 var(--t-base)/var(--lh-snug) var(--font);
  color: var(--brand-dark); margin-bottom: var(--sp-1); }
.note > .nmsg { font-size: var(--t-base); line-height: var(--lh); color: var(--ink-2); }
.note.ok   { border-color: var(--good-line); background: var(--good-soft);
             border-left-color: var(--good); }
.note.ok > .ntitle { color: var(--good); }
.note.warn { border-color: var(--avg-line); background: var(--avg-soft);
             border-left-color: var(--avg); }
.note.warn > .ntitle { color: var(--avg); }
.note.err  { border-color: var(--poor-line); background: var(--poor-soft);
             border-left-color: var(--poor); }
.note.err > .ntitle { color: var(--poor); }
.note.plain { border-color: var(--line); background: var(--paper-2);
              border-left-color: var(--line); }
.note.plain > .ntitle { color: var(--ink); }
/* The report's two long-form callouts are notes with a name of their own. */
.callout { border: 1px solid var(--brand-line); background: var(--brand-softer);
  border-left: 3px solid var(--brand); border-radius: var(--r-md); padding: var(--sp-4); }
.callout h4 { color: var(--brand-dark); margin-bottom: var(--sp-1); }
.disclaimer, .coverage { border: 1px solid var(--avg-line); background: var(--avg-soft);
  border-left: 3px solid var(--avg); border-radius: var(--r-md);
  padding: var(--sp-4); color: var(--ink-2); }
.disclaimer h4, .coverage h4 { color: var(--avg); margin-bottom: var(--sp-1); }
.coverage.failed { border-color: var(--poor-line); background: var(--poor-soft);
  border-left-color: var(--poor); }
.coverage.failed h4 { color: var(--poor); }

/* ------------------------------------------------------------- utilities */
.grid { display: grid; gap: var(--sp-4); }
.g-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.g-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.g-4 { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.row { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2); }
.grow { flex: 1 1 auto; min-width: 0; }
.stack > * + * { margin-top: var(--sp-3); }

/* Motion is decoration. Anyone who has asked for less of it gets none. */
@media (prefers-reduced-motion: reduce) {
  * { animation-duration: .001ms !important; animation-iteration-count: 1 !important;
      transition-duration: .001ms !important; scroll-behavior: auto !important; }
}
"""
