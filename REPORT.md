# The report layer

The scan pipeline is unchanged. What changed is everything after it: the flat,
Lighthouse-style table has been replaced by a structured, white-labelable
report with a print-perfect PDF.

```
consolidate_report.py   reads the run folder -> the page list       (reading)
report_model.py         page list + axe + security -> one Report    (what it says)
report_charts.py        inline SVG: rings, gauge, bars, heatmap     (how it shows)
report_css.py           design tokens + screen CSS + paged CSS      (how it looks)
report_render.py        Report -> one HTML document                 (assembly)
report_pdf.py           that document -> PDF, paginated by paged.js (paper)
report_brand.py         brand.json -> names, logos, colour tokens   (whose it is)
report_sample.py        the same report from a fixture, no scan     (iteration)
```

Nothing in this layer re-scores anything. Lighthouse category scores, axe
impacts and the security severities are carried through exactly as the scanners
produced them. What the report adds — a shared severity scale, an effort
estimate, a priority order, an overall health roll-up, and 0-100 figures for the
two scanners that emit findings rather than scores — is presentation, and every
addition is stated on the report's own methodology page.

## What the report is

1. **Cover** — agency logo, optional client logo, site, date, device, scope and
   the overall health score.
2. **Executive summary** — one page, plain language: what it means, a verdict
   per category, and the handful of findings that matter most.
3. **Scorecard** — a score ring per category plus a pages × categories heatmap.
4. **What to fix first** — findings ranked by severity, then by how much of the
   site they touch against how much work they are. Filterable on screen.
5. **Category deep dives** — every finding, grouped by category, with the
   standards panel under WCAG and the core metrics under Performance.
6. **Page appendix** — every page, its scores, and the existing links to the
   full Lighthouse report for that page.
7. **Methodology & disclaimer** — pinned tool versions, how each derived number
   was produced, and the automated-only caveat.

## One document, two renderings

`report.html` is both the interactive report and the print master. paged.js
drops `@media screen` rules and keeps `@media print` ones, so a single
stylesheet drives both with no duplication:

| block | carries |
| --- | --- |
| unmediated | type, colour, cards, tables, charts |
| `@media screen` | sticky section nav, severity filters, collapsibles, hover |
| `@media print` | `@page` margin boxes, running header, page numbers, breaks |

On screen: section navigation, filter the action list by severity, effort or
category, expand/collapse detail. On paper: a full-bleed cover with no
furniture, a running header naming the current section, `page / total` in the
footer, and cards that never split across a page boundary.

## Why paged.js and not WeasyPrint

Both implement the paged-media features this report needs — `@page` margin
boxes, running headers via `string-set`, page counters, break control. paged.js
won on three points:

- **One rendering engine.** Chrome is already a hard requirement (Lighthouse
  drives it, and the tool has always shelled out to it for PDFs), so paged.js
  paginates the exact HTML the client sees, in the same engine. WeasyPrint would
  add a second engine whose CSS support differs from Chrome's — grid, modern
  colour syntax and SVG text metrics all render differently — so the HTML and
  the PDF would drift apart. Rendering identically in both is a requirement
  here, and one engine is the only way to actually get it.
- **No new native dependencies.** WeasyPrint needs Pango, HarfBuzz and cairo;
  on Windows, where this dashboard is mostly run, that means a GTK runtime.
  paged.js is a single JavaScript file.
- **It degrades.** When the polyfill cannot be resolved the PDF is still
  produced through Chrome's own pagination, losing the running header and page
  numbers but nothing else. A missing WeasyPrint means no PDF at all.

The honest cost: paged.js is slower (it lays out every page in a real browser)
and it is a JS dependency to keep around. Both are fine for something generated
once at the end of a scan that already takes minutes.

The polyfill is resolved in this order, and only the last step needs a network:

1. `$PAGEDJS_POLYFILL` — an explicit file path
2. `vendor/pagedjs/paged.polyfill.min.js` next to the code
3. an earlier install under `$PAGEDJS_HOME` (default `~/.pagedjs-runner`)
4. `npm install pagedjs` into that cache dir, once per machine
   (`PAGEDJS_AUTO_INSTALL=0` stops at step 3)

## White-labelling

Drop a `brand.json` next to the report being written, or next to the code, or
point `$PERF_REPORT_BRAND` at one. See `brand.example.json`; every field is
optional.

```json
{
  "agency_name": "Northbeam Studio",
  "agency_logo": "assets/logo.svg",
  "client_name": "Harbor Lane",
  "primary": "#1f4fd8",
  "secondary": "#7a3ff2",
  "report_title": "Website Health Report",
  "footer_note": "Confidential - prepared for Harbor Lane"
}
```

Logos are inlined as data URIs, so `report.html` stays a single file you can
email. The primary and secondary colours derive the whole brand token set. The
status palette (green/amber/red) and the severity palette are deliberately not
brandable — a critical finding has to look critical whatever the agency's
colours are.

## Iterating on the layout without a scan

`fixtures/sample_scan.json` is one fabricated but structurally faithful run —
the same shapes the three scanners emit, including a page that was crawled but
never deep-audited.

```
python report_sample.py                        # -> sample-report.html
python report_sample.py --pdf                  # -> ... and sample-report.pdf
python report_sample.py --brand brand.json     # white-labelled
python report_sample.py --pdf --keep-print-html   # keep the paginated document
```

`tests/test_premium_report.py` renders from the same fixture, so the layout,
the ordering rules, the escaping, the brand plumbing and the paged-media rules
are all checked in under a second with no Node, no browser and no live site.

## What changed in the existing modules

The scanners' logic was not touched. Three seams were opened so the report can
reach their data:

- `accessibility_scan.scan_site` and `security_scan.scan_site` were each split
  into `collect_site` (the same work, returning data) and `html_from` (the
  standalone page). `scan_site` still exists and still returns exactly what it
  always did.
- `consolidate_report.load_lhr_pages` now also records each failing audit's
  `id` and the category the LHR's own `auditRefs` file it under. That is what
  lets findings be grouped by category instead of dumped in one list.
- `dashboard.run_pipeline` runs the optional scans before consolidating, so
  their findings go into the one report. Which modes produce a report is
  unchanged, and both standalone pages are still written.
