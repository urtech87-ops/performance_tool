#!/usr/bin/env python3
"""
The report's half of the design system.

Three things to know before editing:

1. Colour, type, spacing, radii and shadows come from `design_tokens` - the
   same tokens the app reads. Nothing below hard-codes a value.

2. The components every surface shares - cards, buttons, pills, tags, chips,
   tables, rings, notes - come from `design_css.PRIMITIVES`, spliced in whole.
   A card here *is* the card in the app. What is left in this file is only
   what the report alone has: the cover, the action list, the deep dives, the
   appendix and the paged-media rules.

3. Media queries decide which of the two renderings a rule belongs to.
   paged.js drops every `@media screen` block and keeps every `@media print`
   one, so the same file drives both outputs with no duplication:

       (no media query)  shared: type, colour, cards, tables, charts
       @media screen     the app chrome: sticky nav, hover, collapsibles
       @media print      paged media: @page boxes, running header, breaks
"""

import design_tokens
from design_css import PRIMITIVES


def stylesheet(brand):
    """The whole stylesheet, with `brand`'s tokens spliced into :root."""
    return (f":root {{\n{design_tokens.css_variables(brand, '  ')}\n}}\n"
            f"{PRIMITIVES}\n"
            + _TEMPLATE.replace("__FONT__", brand.font_stack)
                       .replace("__INK3__", brand.tokens()["--ink-3"])
                       .replace("__BRAND__", brand.primary))


_TEMPLATE = """
/* ============================================== report: base + layout ====
   The shared component layer (design_css.PRIMITIVES) has already been
   emitted above this point. Everything below is the report's own.
   ======================================================================== */
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  margin: 0; background: var(--paper-2); color: var(--ink);
  font-family: var(--font); font-size: var(--t-base); line-height: var(--lh);
  -webkit-font-smoothing: antialiased;
}
ul { margin: 0; padding: 0; list-style: none; }

/* ---------------------------------------------------------------- layout */
.doc { max-width: var(--page-w); margin: 0 auto; padding: 0 var(--sp-5) var(--sp-8); }
.section { margin: 0 0 var(--sp-7); }
.section-head { display: flex; align-items: flex-end; gap: var(--sp-4);
  margin: 0 0 var(--sp-4); padding-bottom: var(--sp-3);
  border-bottom: 2px solid var(--brand-line); }
.section-head .num { font-size: var(--t-sm); font-weight: 800; color: var(--brand);
  letter-spacing: .12em; text-transform: uppercase; }
.section-head p { margin: 2px 0 0; color: var(--ink-3); font-size: var(--t-sm); max-width: 62ch; }
.section-head .grow { flex: 1; min-width: 0; }
.section-head .aside { flex: 0 0 auto; align-self: center; }
/* Sources for the PDF's running header. Never visible in either rendering;
   paged.js reads their text through string-set (see @media print). */
.rh { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0;
  overflow: hidden; clip: rect(0 0 0 0); clip-path: inset(50%); white-space: nowrap;
  border: 0; }
@media screen and (max-width: 900px) {
  .g-2, .g-3, .g-4 { grid-template-columns: 1fr; }
}

/* ----------------------------------------------------------------- cover */
.cover { position: relative; color: var(--on-dark); background: var(--cover-bg);
  border-radius: var(--r-xl); overflow: hidden; padding: var(--sp-7) var(--sp-7) var(--sp-6); }
.cover::after { content: ""; position: absolute; inset: 0;
  background: radial-gradient(700px 340px at 88% -12%, var(--on-dark-sheen), transparent 62%);
  pointer-events: none; }
.cover > * { position: relative; z-index: 1; }
.cover-top { display: flex; align-items: center; justify-content: space-between;
  gap: var(--sp-5); margin-bottom: var(--sp-7); }
.logo-slot { display: flex; align-items: center; gap: var(--sp-3); }
.logo-slot img { max-height: 46px; max-width: 190px; object-fit: contain; }
.logo-mark { width: 46px; height: 46px; border-radius: var(--r-md); display: grid;
  place-items: center; background: var(--on-dark-fill); border: 1px solid var(--on-dark-line);
  font-weight: 800; font-size: var(--t-md); letter-spacing: .02em; }
.logo-name { font-weight: 700; font-size: var(--t-base); }
.logo-role { font-size: var(--t-xs); color: var(--on-dark-2); letter-spacing: var(--track-caps);
  text-transform: uppercase; }
.cover h1 { font-size: var(--t-4xl); max-width: 18ch; margin-bottom: var(--sp-3); }
.cover .site { font-size: var(--t-lg); font-weight: 600; opacity: .95;
  word-break: break-all; margin-bottom: var(--sp-5); }
.cover-body { display: grid; grid-template-columns: minmax(0,1fr) auto; gap: var(--sp-6);
  align-items: center; }
.cover-facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--sp-3) var(--sp-5); margin-top: var(--sp-5);
  border-top: 1px solid var(--on-dark-line); padding-top: var(--sp-4); }
.fact .k { font-size: var(--t-xs); letter-spacing: var(--track-caps);
  text-transform: uppercase; color: var(--on-dark-2); }
.fact .v { font-size: var(--t-base); font-weight: 600; }
.cover-score { background: var(--paper); color: var(--ink);
  border-radius: var(--r-lg); padding: var(--sp-4) var(--sp-5); text-align: center;
  box-shadow: var(--shadow-lg); }
.cover-score .band { font-weight: 800; font-size: var(--t-md); margin-top: -6px; }
.cover-score .cap { font-size: var(--t-xs); letter-spacing: .1em; text-transform: uppercase;
  color: var(--ink-3); }
.cover-foot { margin-top: var(--sp-6); display: flex; justify-content: space-between;
  gap: var(--sp-4); font-size: var(--t-sm); color: var(--on-dark-2);
  border-top: 1px solid var(--on-dark-line); padding-top: var(--sp-3); }

/* ------------------------------------------------------------ scorecards */
.scorecard { display: grid; gap: var(--sp-4);
  grid-template-columns: repeat(auto-fit, minmax(158px, 1fr)); }
.score-tile { background: var(--paper); border: 1px solid var(--line); border-radius: var(--r-lg);
  padding: var(--sp-4); text-align: center; box-shadow: var(--shadow);
  border-top: 4px solid var(--na); }
.score-tile.good { border-top-color: var(--good); }
.score-tile.avg  { border-top-color: var(--avg); }
.score-tile.poor { border-top-color: var(--poor); }
.score-tile .name { font-weight: 700; font-size: var(--t-base); margin-top: var(--sp-2); }
.score-tile .verdict { font-size: var(--t-xs); font-weight: 700; letter-spacing: .06em;
  text-transform: uppercase; margin-top: 2px; }
.score-tile.good .verdict { color: var(--good); }
.score-tile.avg .verdict  { color: var(--avg); }
.score-tile.poor .verdict { color: var(--poor); }
.score-tile.na .verdict   { color: var(--na); }
.score-tile .blurb { font-size: var(--t-sm); color: var(--ink-3); margin-top: var(--sp-2);
  line-height: 1.45; }

/* ------------------------------------------------------- exec + verdicts */
.verdict-row { display: grid; grid-template-columns: 116px 78px 1fr; gap: var(--sp-3);
  align-items: center; padding: var(--sp-3) 0; border-bottom: 1px solid var(--line-2); }
.verdict-row:last-child { border-bottom: 0; }
.verdict-row .cat { font-weight: 700; }

/* ---------------------------------------------------------- action cards */
.action { display: grid; grid-template-columns: 46px 1fr; gap: var(--sp-4);
  background: var(--paper); border: 1px solid var(--line); border-radius: var(--r-lg);
  padding: var(--sp-4) var(--sp-5); margin-bottom: var(--sp-3); box-shadow: var(--shadow); }
.action .rank { width: 38px; height: 38px; border-radius: var(--r-md); display: grid;
  place-items: center; font-weight: 800; font-size: var(--t-md); color: var(--paper);
  background: var(--sev-minor); }
.action.critical .rank { background: var(--sev-critical); }
.action.serious .rank  { background: var(--sev-serious); }
.action.moderate .rank { background: var(--sev-moderate); }
.action-head { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2);
  margin-bottom: var(--sp-2); }
.action-head h3 { flex: 1 1 320px; }
.tag.effort { display: inline-flex; align-items: center; gap: 6px; color: var(--ink-2); }
.tag.effort .pips { color: var(--brand); }
.action .meta { font-size: var(--t-sm); color: var(--ink-3); margin-bottom: var(--sp-2); }
.action .meta b { color: var(--ink-2); }
.fix { background: var(--paper-2); border-radius: var(--r-md); padding: var(--sp-3) var(--sp-4);
  font-size: var(--t-base); }
.fix b { color: var(--brand-dark); }
.evidence { margin-top: var(--sp-3); font-size: var(--t-sm); color: var(--ink-3); }
.evidence li { padding: 3px 0; }
.evidence code { background: var(--line-2); padding: 1px 6px; border-radius: 4px;
  color: var(--ink-2); word-break: break-all; }
.evidence pre { margin: 4px 0 0; background: var(--brand-deep); color: var(--brand-soft);
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-sm); overflow-x: auto; white-space: pre-wrap; word-break: break-all; }
.pagelist { display: flex; flex-wrap: wrap; gap: 6px; margin-top: var(--sp-2); }
.pagelist code { font-size: var(--t-xs); background: var(--line-2); border-radius: 4px;
  padding: 2px 7px; color: var(--ink-2); }

/* ---------------------------------------------------------------- tables */
td.path { word-break: normal; overflow-wrap: anywhere; }
.linkout { font-weight: 700; font-size: var(--t-xs); text-decoration: none;
  color: var(--brand); white-space: nowrap; }
.linkout:hover { text-decoration: underline; }

/* ------------------------------------------------------------- deep dive */
.dive-head { display: grid; grid-template-columns: auto 1fr auto; gap: var(--sp-5);
  align-items: center; background: var(--paper); border: 1px solid var(--line);
  border-radius: var(--r-lg); padding: var(--sp-4) var(--sp-5); margin-bottom: var(--sp-4);
  box-shadow: var(--shadow); }
.dive-head .sevcols { max-width: 300px; }
.metrics { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
.metric { border: 1px solid var(--line); border-radius: var(--r-md); padding: 6px 12px;
  min-width: 92px; background: var(--paper); }
.metric .k { display: block; font-size: 10px; font-weight: 800; letter-spacing: .07em;
  text-transform: uppercase; color: var(--ink-3); }
.metric .v { font-size: var(--t-base); font-weight: 700; }
.metric.good .v { color: var(--good); } .metric.avg .v { color: var(--avg); }
.metric.poor .v { color: var(--poor); }
.std-row { display: grid; grid-template-columns: 1fr auto; gap: var(--sp-3); align-items: center;
  padding: var(--sp-3) 0; border-bottom: 1px solid var(--line-2); }
.std-row:last-child { border-bottom: 0; }
.std-row .region { font-size: var(--t-xs); color: var(--ink-3); }
.pill.pass { background: var(--good-soft); color: var(--good); }
.pill.fail { background: var(--poor-soft); color: var(--poor); }
.pill.unk  { background: var(--avg-soft);  color: var(--avg); }

/* -------------------------------------------------------------- appendix */
.page-card { background: var(--paper); border: 1px solid var(--line);
  border-radius: var(--r-lg); padding: var(--sp-4) var(--sp-5); margin-bottom: var(--sp-3); }
.page-card h3 { word-break: break-all; margin-bottom: var(--sp-2); }
.page-card .row { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2);
  margin-bottom: var(--sp-3); }
.mini { display: inline-flex; align-items: baseline; gap: 6px; font-size: var(--t-xs);
  font-weight: 700; border: 1px solid var(--line); border-radius: 999px; padding: 3px 10px; }
.mini b { font-size: var(--t-base); }
.mini.good b { color: var(--good); } .mini.avg b { color: var(--avg); }
.mini.poor b { color: var(--poor); } .mini.na b { color: var(--na); }

/* -------------------------------------------------------- run coverage */
/* A run that measured only part of a site says so on its face - on the cover
   and again in the methodology - so partial coverage can never be read as a
   clean pass. */
.cover-flag { margin-top: var(--sp-3); border-radius: var(--r-md);
  padding: var(--sp-2) var(--sp-3); font-size: var(--t-sm); font-weight: 600;
  background: var(--on-dark-fill); border: 1px solid var(--on-dark-line);
  color: var(--on-dark); }
.cover-flag b { font-weight: 800; letter-spacing: .04em; text-transform: uppercase;
  font-size: var(--t-xs); }
.coverage ul { margin: var(--sp-2) 0 0; padding-left: var(--sp-4); }
.covertable td.why { color: var(--ink-3); font-size: var(--t-xs); }
.covertable td.state { font-weight: 700; text-transform: uppercase;
  font-size: var(--t-xs); letter-spacing: .04em; white-space: nowrap; }

/* ----------------------------------------------------------- methodology */
.method dt { font-weight: 700; margin-top: var(--sp-3); }
.method dd { margin: 2px 0 0; color: var(--ink-2); }
.toolgrid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: var(--sp-3); }
.tool { border: 1px solid var(--line); border-radius: var(--r-md); padding: var(--sp-3); }
.tool .n { font-weight: 700; }
.tool .v { font-family: var(--mono); font-size: var(--t-xs); color: var(--brand-dark); }
.tool .r { font-size: var(--t-xs); color: var(--ink-3); }

/* ================================================================ SCREEN */
@media screen {
  body { padding-top: 58px; }
  .topnav { position: fixed; top: 0; left: 0; right: 0; z-index: 40; height: 58px;
    background: var(--paper); box-shadow: var(--shadow-xs);
    border-bottom: 1px solid var(--line); display: flex; align-items: center;
    gap: var(--sp-4); padding: 0 var(--sp-5); }
  .topnav .brandmark { display: flex; align-items: center; gap: var(--sp-2);
    font-weight: 800; font-size: var(--t-base); white-space: nowrap; }
  .topnav .brandmark img { max-height: 26px; max-width: 120px; }
  .topnav .dot { width: 30px; height: 30px; border-radius: var(--r-sm); background: var(--grad);
    color: var(--brand-ink); display: grid; place-items: center; font-size: var(--t-sm);
    font-weight: 800; box-shadow: var(--shadow-sm); }
  .topnav nav { display: flex; gap: 2px; overflow-x: auto; flex: 1; }
  .topnav nav a { font-size: var(--t-sm); font-weight: 600; color: var(--ink-3);
    text-decoration: none; padding: 7px var(--sp-3); border-radius: var(--r-sm);
    white-space: nowrap; transition: background var(--dur-fast) var(--ease),
    color var(--dur-fast) var(--ease); }
  .topnav nav a:hover { background: var(--brand-softer); color: var(--brand-dark); }
  .topnav nav a.on { background: var(--brand-soft); color: var(--brand-dark); }
  .doc { padding-top: var(--sp-5); }
  .print-only { display: none !important; }

  .toolbar { display: flex; flex-wrap: wrap; gap: var(--sp-2); align-items: center;
    margin-bottom: var(--sp-4); }
  .toolbar .lbl { font-size: var(--t-xs); font-weight: 800; letter-spacing: .08em;
    text-transform: uppercase; color: var(--ink-3); margin-right: var(--sp-1); }
  .filter { border: 1px solid var(--line); background: var(--paper); color: var(--ink-2);
    font: 700 var(--t-sm)/1 var(--font); padding: 7px var(--sp-3);
    border-radius: var(--r-pill); cursor: pointer;
    transition: background var(--dur-fast) var(--ease),
                border-color var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease); }
  .filter:hover { border-color: var(--brand-line); background: var(--brand-softer);
    color: var(--brand-dark); }
  .filter:focus-visible { outline: none; box-shadow: var(--focus-ring); border-color: var(--brand); }
  .filter.on { background: var(--brand); border-color: var(--brand); color: var(--brand-ink); }
  .action[hidden] { display: none; }

  details.expand { border: 1px solid var(--line); border-radius: var(--r-md);
    background: var(--paper); margin-bottom: var(--sp-2); }
  details.expand > summary { cursor: pointer; list-style: none; padding: var(--sp-3) var(--sp-4);
    font-weight: 700; font-size: var(--t-base); display: flex; align-items: center; gap: var(--sp-2); }
  details.expand > summary::-webkit-details-marker { display: none; }
  details.expand > summary::before { content: "+"; font-family: var(--mono); font-weight: 700;
    color: var(--brand); width: 14px; }
  details.expand[open] > summary::before { content: "-"; }
  details.expand > .body { padding: 0 var(--sp-4) var(--sp-4); }
  .anchor-target { scroll-margin-top: calc(var(--nav-h) + var(--sp-3)); }
  .score-tile, .action, .page-card { transition: box-shadow var(--dur) var(--ease),
    border-color var(--dur) var(--ease); }
  .score-tile:hover, .action:hover, .page-card:hover { box-shadow: var(--shadow-lg);
    border-color: var(--brand-line); }
  details.expand > summary:focus-visible { outline: none; box-shadow: var(--focus-ring); }
  a.tolist { font-size: var(--t-sm); font-weight: 700; text-decoration: none; }
}

/* ================================================================= PRINT */
@media print {
  /* Named pages: the cover gets its own bleed-free sheet, everything else
     carries the running header and the page number. */
  @page {
    size: A4;
    margin: 17mm 15mm 16mm;
    @top-left {
      content: string(doc-title);
      font-family: __FONT__; font-size: 8pt; color: __INK3__;
      letter-spacing: .06em; text-transform: uppercase; vertical-align: bottom;
      padding-bottom: 3mm;
    }
    @top-right {
      content: string(doc-section);
      font-family: __FONT__; font-size: 8pt; color: __BRAND__; font-weight: 700;
      letter-spacing: .06em; text-transform: uppercase; vertical-align: bottom;
      padding-bottom: 3mm;
    }
    @bottom-left {
      content: string(doc-client);
      font-family: __FONT__; font-size: 7.5pt; color: __INK3__;
      vertical-align: top; padding-top: 3mm;
    }
    @bottom-right {
      content: counter(page) " / " counter(pages);
      font-family: __FONT__; font-size: 8pt; font-weight: 700; color: __INK3__;
      vertical-align: top; padding-top: 3mm;
    }
  }
  @page cover { margin: 0; @top-left { content: none; } @top-right { content: none; }
                @bottom-left { content: none; } @bottom-right { content: none; } }

  html, body { background: var(--paper); font-size: 9.6pt; }
  body { padding: 0; }
  .doc { max-width: none; padding: 0; }
  .screen-only, .topnav, .toolbar { display: none !important; }
  .print-only { display: block; }

  h1 { font-size: 22pt; } h2 { font-size: 15pt; } h3 { font-size: 11pt; }
  .card, .action, .page-card, .dive-head, .callout, .kpi, .score-tile, .tool,
  .verdict-row, .std-row, .disclaimer, tr, .metric {
    break-inside: avoid; page-break-inside: avoid;
  }
  .card, .action, .page-card, .dive-head, .score-tile { box-shadow: none; }
  /* No trailing margin: the page break already separates sections, and a
     bottom margin that overflows the last page of a section makes paged.js
     emit an empty page for it. */
  .section { break-before: page; page-break-before: always; margin-bottom: 0; }
  .section > *:last-child { margin-bottom: 0; }
  .dive:last-child > *:last-child, .page-card:last-child { margin-bottom: 0; }
  .section:first-of-type { break-before: avoid; page-break-before: avoid; }
  .section-head { break-after: avoid; page-break-after: avoid; }
  h2, h3 { break-after: avoid; page-break-after: avoid; }
  .keep-with-next { break-after: avoid; page-break-after: avoid; }
  .allow-break { break-inside: auto; page-break-inside: auto; }

  /* What the running header reads. Each source element is invisible in both
     renderings; paged.js copies its text into the page margin boxes and keeps
     it up to date as sections start. */
  .rh-title   { string-set: doc-title content(); }
  .rh-client  { string-set: doc-client content(); }
  .section-head h2 { string-set: doc-section content(); }


  .cover { page: cover; break-after: page; page-break-after: always;
    border-radius: 0; min-height: 297mm; padding: 22mm 20mm;
    display: flex; flex-direction: column; justify-content: space-between; }
  .cover h1 { font-size: 30pt; }
  .cover-foot { margin-top: 0; }

  /* collapsibles are always open on paper */
  details.expand { border: 0; }
  details.expand > summary { padding: 0 0 var(--sp-2); font-size: 11pt; }
  details.expand > .body { padding: 0; }

  /* The executive summary is meant to be one page a director can be handed.
     It is the only section tuned this tightly; everything else flows. */
  #summary .card { padding: var(--sp-3) var(--sp-4); }
  #summary .callout { padding: var(--sp-3); }
  #summary .lede { font-size: 10.5pt; }
  #summary .kpis { grid-template-columns: repeat(5, minmax(0, 1fr)); gap: var(--sp-2); }
  #summary .kpi { padding: var(--sp-2); }
  #summary .kpi b { font-size: 16pt; }
  #summary .verdict-row { padding: 4.5px 0; grid-template-columns: 108px 66px 1fr; }
  #summary .evidence li { padding: 1.5px 0; }
  #summary .legend { margin-top: var(--sp-2); gap: var(--sp-3); }

  a { color: var(--ink); text-decoration: none; }
  a.linkout::after { content: " (" attr(href) ")"; font-size: 7pt; color: var(--ink-3);
    word-break: break-all; font-weight: 400; }
  .evidence pre { background: var(--paper-2); color: var(--ink); border: 1px solid var(--line); }
  table { break-inside: auto; }
  /* `overflow: hidden` (which rounds the table's corners on screen) makes the
     table an unbreakable box: it would jump whole to the next page, dragging
     its heading with it and leaving a blank page behind. On paper the table
     breaks between rows instead, and the header row repeats. */
  table { overflow: visible; border-radius: 0; }
  /* The printed href appended to each raw-report link would otherwise win the
     column-width auction and squeeze the page paths into a vertical ribbon. */
  #pages table { table-layout: fixed; }
  #pages th:first-child, #pages td:first-child { width: 30%; }
  #pages th:last-child, #pages td:last-child { width: 22%; }
  #pages td:last-child { text-align: left; }
  thead { display: table-header-group; }
  tfoot { display: table-footer-group; }
}
"""
