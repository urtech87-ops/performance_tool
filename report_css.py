#!/usr/bin/env python3
"""
The report's design system, as one stylesheet.

Two things to know before editing:

1. Media queries decide which of the two renderings a rule belongs to.
   paged.js drops every `@media screen` block and keeps every `@media print`
   one, so the same file drives both outputs with no duplication:

       (no media query)  shared: type, colour, cards, tables, charts
       @media screen     the app chrome: sticky nav, hover, collapsibles
       @media print      paged media: @page boxes, running header, breaks

2. Colour, type and spacing only ever come from the tokens at the top.
   report_brand.Brand owns the values; nothing below hard-codes a hex.
"""

TYPE_AND_SPACE = """
  /* type scale (1.25 minor third, 16px base) and 4px spacing scale */
  --t-xs: 11px;   --t-sm: 12.5px; --t-base: 14.5px; --t-md: 16px;
  --t-lg: 20px;   --t-xl: 25px;   --t-2xl: 31px;    --t-3xl: 39px;  --t-4xl: 49px;
  --sp-1: 4px;  --sp-2: 8px;  --sp-3: 12px; --sp-4: 16px; --sp-5: 24px;
  --sp-6: 32px; --sp-7: 48px; --sp-8: 64px;
  --r-sm: 6px; --r-md: 10px; --r-lg: 16px; --r-xl: 22px;
  --page-w: 1120px;
"""


def stylesheet(brand):
    """The whole stylesheet, with `brand`'s tokens spliced into :root."""
    return _TEMPLATE.replace("/*__TOKENS__*/", brand.css_variables("  ")) \
                    .replace("/*__SCALE__*/", TYPE_AND_SPACE) \
                    .replace("__FONT__", brand.font_stack) \
                    .replace("__INK3__", brand.tokens()["--ink-3"]) \
                    .replace("__BRAND__", brand.primary)


_TEMPLATE = """
:root {
/*__TOKENS__*/
/*__SCALE__*/
}

* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  margin: 0; background: var(--paper-2); color: var(--ink);
  font-family: var(--font); font-size: var(--t-base); line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}
h1, h2, h3, h4 { margin: 0; line-height: 1.2; letter-spacing: -0.015em; font-weight: 700; }
h1 { font-size: var(--t-3xl); }
h2 { font-size: var(--t-xl); }
h3 { font-size: var(--t-md); }
h4 { font-size: var(--t-base); }
p { margin: 0 0 var(--sp-3); }
a { color: var(--brand); }
code, pre { font-family: var(--mono); font-size: var(--t-xs); }
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
.eyebrow { font-size: var(--t-xs); font-weight: 800; letter-spacing: .14em;
  text-transform: uppercase; color: var(--brand); margin: 0 0 var(--sp-1); }
.lede { font-size: var(--t-md); line-height: 1.6; color: var(--ink-2); max-width: 70ch; }
.muted { color: var(--ink-3); }
/* Sources for the PDF's running header. Never visible in either rendering;
   paged.js reads their text through string-set (see @media print). */
.rh { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0;
  overflow: hidden; clip: rect(0 0 0 0); clip-path: inset(50%); white-space: nowrap;
  border: 0; }
.grid { display: grid; gap: var(--sp-4); }
.g-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.g-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.g-4 { grid-template-columns: repeat(4, minmax(0, 1fr)); }
@media screen and (max-width: 900px) {
  .g-2, .g-3, .g-4 { grid-template-columns: 1fr; }
}

/* ----------------------------------------------------------------- cards */
.card { background: var(--paper); border: 1px solid var(--line);
  border-radius: var(--r-lg); padding: var(--sp-5); box-shadow: var(--shadow); }
.card.flat { box-shadow: none; }
.card-title { font-size: var(--t-md); font-weight: 700; margin: 0 0 var(--sp-2); }

/* ----------------------------------------------------------------- cover */
.cover { position: relative; color: #fff; background: var(--cover-bg);
  border-radius: var(--r-xl); overflow: hidden; padding: var(--sp-7) var(--sp-7) var(--sp-6); }
.cover::after { content: ""; position: absolute; inset: 0;
  background: radial-gradient(700px 340px at 88% -12%, rgba(255,255,255,.20), transparent 62%);
  pointer-events: none; }
.cover > * { position: relative; z-index: 1; }
.cover-top { display: flex; align-items: center; justify-content: space-between;
  gap: var(--sp-5); margin-bottom: var(--sp-7); }
.logo-slot { display: flex; align-items: center; gap: var(--sp-3); }
.logo-slot img { max-height: 46px; max-width: 190px; object-fit: contain; }
.logo-mark { width: 46px; height: 46px; border-radius: 13px; display: grid; place-items: center;
  background: rgba(255,255,255,.16); border: 1px solid rgba(255,255,255,.3);
  font-weight: 800; font-size: var(--t-md); letter-spacing: .02em; }
.logo-name { font-weight: 700; font-size: var(--t-base); }
.logo-role { font-size: var(--t-xs); opacity: .72; letter-spacing: .1em; text-transform: uppercase; }
.cover h1 { font-size: var(--t-4xl); max-width: 18ch; margin-bottom: var(--sp-3); }
.cover .site { font-size: var(--t-lg); font-weight: 600; opacity: .95;
  word-break: break-all; margin-bottom: var(--sp-5); }
.cover-body { display: grid; grid-template-columns: minmax(0,1fr) auto; gap: var(--sp-6);
  align-items: center; }
.cover-facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--sp-3) var(--sp-5); margin-top: var(--sp-5);
  border-top: 1px solid rgba(255,255,255,.22); padding-top: var(--sp-4); }
.fact .k { font-size: var(--t-xs); letter-spacing: .1em; text-transform: uppercase; opacity: .68; }
.fact .v { font-size: var(--t-base); font-weight: 600; }
.cover-score { background: rgba(255,255,255,.97); color: var(--ink);
  border-radius: var(--r-lg); padding: var(--sp-4) var(--sp-5); text-align: center;
  box-shadow: var(--shadow-lg); }
.cover-score .band { font-weight: 800; font-size: var(--t-md); margin-top: -6px; }
.cover-score .cap { font-size: var(--t-xs); letter-spacing: .1em; text-transform: uppercase;
  color: var(--ink-3); }
.cover-foot { margin-top: var(--sp-6); display: flex; justify-content: space-between;
  gap: var(--sp-4); font-size: var(--t-sm); opacity: .8; border-top: 1px solid rgba(255,255,255,.22);
  padding-top: var(--sp-3); }

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
.ring { display: block; margin: 0 auto; }
.ring-label { fill: var(--ink-3); font-weight: 600; letter-spacing: .04em; text-transform: uppercase; }
.legend { display: flex; flex-wrap: wrap; gap: var(--sp-4); margin-top: var(--sp-4);
  font-size: var(--t-sm); color: var(--ink-3); }
.legend .k { display: inline-flex; align-items: center; gap: 6px; }
.legend .k b { color: var(--ink); font-weight: 800; }
.swatch { width: 11px; height: 11px; border-radius: 3px; display: inline-block; }

/* ------------------------------------------------------- exec + verdicts */
.verdict-row { display: grid; grid-template-columns: 116px 78px 1fr; gap: var(--sp-3);
  align-items: center; padding: var(--sp-3) 0; border-bottom: 1px solid var(--line-2); }
.verdict-row:last-child { border-bottom: 0; }
.verdict-row .cat { font-weight: 700; }
.pill { display: inline-block; font-size: var(--t-xs); font-weight: 800; letter-spacing: .05em;
  text-transform: uppercase; padding: 3px 9px; border-radius: 999px; white-space: nowrap; }
.pill.good { background: var(--good-soft); color: var(--good); }
.pill.avg  { background: var(--avg-soft);  color: var(--avg); }
.pill.poor { background: var(--poor-soft); color: var(--poor); }
.pill.na   { background: var(--line-2);    color: var(--ink-3); }
.callout { background: var(--brand-softer); border: 1px solid var(--brand-line);
  border-left: 4px solid var(--brand); border-radius: var(--r-md);
  padding: var(--sp-4); }
.callout h4 { color: var(--brand-dark); margin-bottom: var(--sp-1); }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(124px, 1fr));
  gap: var(--sp-3); }
.kpi { background: var(--paper); border: 1px solid var(--line); border-radius: var(--r-md);
  padding: var(--sp-3) var(--sp-4); }
.kpi b { display: block; font-size: var(--t-2xl); line-height: 1.1; letter-spacing: -.02em; }
.kpi span { font-size: var(--t-xs); text-transform: uppercase; letter-spacing: .08em;
  color: var(--ink-3); font-weight: 700; }
.kpi.critical b { color: var(--sev-critical); }
.kpi.serious b  { color: var(--sev-serious); }
.kpi.quick b    { color: var(--good); }

/* ---------------------------------------------------------- action cards */
.action { display: grid; grid-template-columns: 46px 1fr; gap: var(--sp-4);
  background: var(--paper); border: 1px solid var(--line); border-radius: var(--r-lg);
  padding: var(--sp-4) var(--sp-5); margin-bottom: var(--sp-3); box-shadow: var(--shadow); }
.action .rank { width: 38px; height: 38px; border-radius: 11px; display: grid; place-items: center;
  font-weight: 800; font-size: var(--t-md); color: #fff; background: var(--sev-minor); }
.action.critical .rank { background: var(--sev-critical); }
.action.serious .rank  { background: var(--sev-serious); }
.action.moderate .rank { background: var(--sev-moderate); }
.action-head { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2);
  margin-bottom: var(--sp-2); }
.action-head h3 { flex: 1 1 320px; }
.tag { font-size: var(--t-xs); font-weight: 700; letter-spacing: .04em; padding: 3px 9px;
  border-radius: 999px; background: var(--line-2); color: var(--ink-2); white-space: nowrap; }
.tag.sev { color: #fff; }
.tag.sev.critical { background: var(--sev-critical); }
.tag.sev.serious  { background: var(--sev-serious); }
.tag.sev.moderate { background: var(--sev-moderate); }
.tag.sev.minor    { background: var(--sev-minor); }
.tag.cat { background: var(--brand-soft); color: var(--brand-dark); }
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
.evidence pre { margin: 4px 0 0; background: var(--ink); color: #e8edf6; padding: 8px 10px;
  border-radius: var(--r-sm); overflow-x: auto; white-space: pre-wrap; word-break: break-all; }
.pagelist { display: flex; flex-wrap: wrap; gap: 6px; margin-top: var(--sp-2); }
.pagelist code { font-size: var(--t-xs); background: var(--line-2); border-radius: 4px;
  padding: 2px 7px; color: var(--ink-2); }

/* ---------------------------------------------------------------- tables */
table { width: 100%; border-collapse: collapse; background: var(--paper);
  border: 1px solid var(--line); border-radius: var(--r-md); overflow: hidden; }
th, td { padding: 9px 12px; text-align: left; font-size: var(--t-sm);
  border-bottom: 1px solid var(--line-2); }
th { background: var(--paper-2); font-size: var(--t-xs); text-transform: uppercase;
  letter-spacing: .06em; color: var(--ink-3); font-weight: 800; }
tbody tr:last-child td { border-bottom: 0; }
td.num, th.num { text-align: center; }
td.score { text-align: center; font-weight: 800; }
td.score.good { color: var(--good); } td.score.avg { color: var(--avg); }
td.score.poor { color: var(--poor); } td.score.na { color: var(--na); }
td.path { word-break: normal; overflow-wrap: anywhere; }
.chip { display: inline-block; font-size: var(--t-xs); padding: 2px 8px; border-radius: 999px;
  background: var(--line-2); color: var(--ink-3); font-weight: 700; white-space: nowrap; }
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

/* ----------------------------------------------------------- methodology */
.method dt { font-weight: 700; margin-top: var(--sp-3); }
.method dd { margin: 2px 0 0; color: var(--ink-2); }
.disclaimer { border: 1px solid var(--avg); background: var(--avg-soft);
  border-radius: var(--r-md); padding: var(--sp-4); color: var(--ink-2); }
.disclaimer h4 { color: var(--avg); margin-bottom: var(--sp-1); }
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
    background: rgba(255,255,255,.92); backdrop-filter: blur(10px);
    border-bottom: 1px solid var(--line); display: flex; align-items: center;
    gap: var(--sp-4); padding: 0 var(--sp-5); }
  .topnav .brandmark { display: flex; align-items: center; gap: var(--sp-2);
    font-weight: 800; font-size: var(--t-base); white-space: nowrap; }
  .topnav .brandmark img { max-height: 26px; max-width: 120px; }
  .topnav .dot { width: 26px; height: 26px; border-radius: 8px; background: var(--grad);
    color: var(--brand-ink); display: grid; place-items: center; font-size: var(--t-xs); }
  .topnav nav { display: flex; gap: 2px; overflow-x: auto; flex: 1; }
  .topnav nav a { font-size: var(--t-sm); font-weight: 600; color: var(--ink-3);
    text-decoration: none; padding: 7px 11px; border-radius: 8px; white-space: nowrap; }
  .topnav nav a:hover { background: var(--brand-softer); color: var(--brand-dark); }
  .topnav nav a.on { background: var(--brand-soft); color: var(--brand-dark); }
  .btn { border: 1px solid var(--line); background: var(--paper); color: var(--ink);
    font: 700 var(--t-sm)/1 var(--font); padding: 9px 14px; border-radius: 9px;
    cursor: pointer; white-space: nowrap; }
  .btn:hover { border-color: var(--brand); color: var(--brand); }
  .btn.primary { background: var(--grad); color: var(--brand-ink); border: 0; }
  .doc { padding-top: var(--sp-5); }
  .print-only { display: none !important; }

  .toolbar { display: flex; flex-wrap: wrap; gap: var(--sp-2); align-items: center;
    margin-bottom: var(--sp-4); }
  .toolbar .lbl { font-size: var(--t-xs); font-weight: 800; letter-spacing: .08em;
    text-transform: uppercase; color: var(--ink-3); margin-right: var(--sp-1); }
  .filter { border: 1px solid var(--line); background: var(--paper); color: var(--ink-2);
    font: 700 var(--t-sm)/1 var(--font); padding: 7px 12px; border-radius: 999px; cursor: pointer; }
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
  .anchor-target { scroll-margin-top: 72px; }
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

  html, body { background: #fff; font-size: 9.6pt; }
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
