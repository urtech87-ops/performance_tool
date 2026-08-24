# The design system

One system, two surfaces: the dashboard app you scan from, and the report a
scan produces. They share a token set, a component layer and a stylesheet
shape, so a card in the app *is* the card in the report — and re-skinning the
product is editing tokens, not chasing hex codes through two stylesheets.

```
brand.json ─► report_brand.Brand ─► design_tokens ─┬─► ui_css     ─► ui_page    (the app)
              names, logos,         the :root       │  + design_css.PRIMITIVES
              3 colours, 2 fonts    custom props    └─► report_css ─► report_render (the report)
```

| module | owns |
| --- | --- |
| `design_tokens.py` | every value: palette, type scale, spacing, radii, shadows, motion, focus |
| `design_css.py` | `PRIMITIVES` — the components both surfaces share, spliced into both |
| `ui_css.py` | the app's own layer: scan form, progress rail, results frame |
| `report_css.py` | the report's own layer: cover, actions, deep dives, paged media |
| `report_brand.py` | who the product belongs to: names, logos, the three brandable colours |

`tests/test_design_system.py` holds the line: it fails if either stylesheet
hard-codes a colour, a type size or a spacing step, if a shared component gets
redefined, or if a re-skin leaves any of the default palette behind.

---

## How to rebrand

Everything below is one file. Drop a `brand.json` next to the code (or next to
the report being written, or point `$PERF_REPORT_BRAND` at one) — see
`brand.example.json`. Every field is optional.

```json
{
  "primary":     "#b4361f",
  "secondary":   "#e08900",
  "ink":         "#1d2321",
  "font_stack":  "'Söhne', -apple-system, Segoe UI, sans-serif",
  "mono_stack":  "'Berkeley Mono', ui-monospace, Menlo, monospace",

  "agency_name": "Ridgeway Partners",
  "agency_logo": "assets/ridgeway.svg",
  "app_name":    "Ridgeway Site Audit",
  "app_tagline": "Every page, every release.",

  "client_name":  "Harbor Lane",
  "client_logo":  "assets/harbor-lane.png",
  "report_title": "Website Health Report",
  "footer_note":  "Confidential - prepared for Harbor Lane"
}
```

### Colour — change `primary`

`primary` is the whole accent. From it, `design_tokens.color_tokens()` derives
the hover and active states, the soft and softer tints, the hairline, the
gradient, the report cover's deep ground and the focus ring. Nothing else needs
touching, and nothing anywhere hard-codes an accent.

* `secondary` is the gradient's far end — the logo tile, the cover wash. Used
  sparingly on purpose.
* `ink` is the neutral ramp's darkest step. Every grey in the product —
  body text, captions, borders, the two paper tones, all four shadows — is
  mixed from it, so a warm ink warms the whole UI and a cool one cools it.
* `--brand-ink` (text *on* the accent) is chosen for contrast, not assumed: a
  pale brand colour automatically gets dark text.

**What a brand cannot change, deliberately:** the status palette (good / needs
work / critical / not measured) and the severity palette. A failing score has
to look like a failing score whatever colour the agency picked, so those are
fixed, contrast-checked values in `design_tokens.STATUS_COLORS` and
`SEVERITY_COLORS`, and no brand input can reach them. The same goes for the six
category accents. Changing those is editing `design_tokens.py` — a decision
about the product, not about a client.

### Font — change `font_stack`

`font_stack` and `mono_stack` become `--font` and `--mono`, which is what every
rule in both stylesheets names. Two things follow automatically:

* The app links Google Fonts **only while the stack still names Inter**
  (`ui_css.font_link`). Point it at anything else and the link disappears
  rather than leaving a font being fetched that nothing uses.
* The report never links a font at all — it is a single portable file, so it
  ships with a stack, not a download.

Self-hosting a face: put the `@font-face` rules in `ui_css._APP` (app) and set
`font_stack` to name it. For the report, a self-hosted face has to be embedded
as a data URI to keep `report.html` one file.

### Logo — set `agency_logo`

Any `.svg`, `.png`, `.jpg`, `.webp` or `.avif` under 2 MB, by path relative to
the brand file. It is inlined as a data URI, so it reaches:

* the app's top bar (`ui_page._brandmark`),
* the report's cover and its screen nav,
* and the PDF, without depending on the filesystem.

With no logo, a monogram tile stands in — initials from `agency_name` on the
brand gradient. `client_logo` is the same, for the "prepared for" side of the
report cover.

### Names

| field | where it shows |
| --- | --- |
| `app_name` | the app's top bar and page title (falls back to `agency_name`) |
| `app_tagline` | the sentence under the app's heading |
| `agency_name` | the report cover's "prepared by", the monogram source |
| `client_name` | the report cover's "prepared for", the running header |
| `report_title` | the report's cover heading and its PDF header |
| `footer_note` | the report's cover footer and the app's page footer |

### Deeper than the brand file

Editing `design_tokens.py` changes the product rather than a client's copy of
it: the type scale, the spacing scale, the radii (`--r-*` — drop them all a
step for a squarer product), the shadow ramp, the motion durations, and the
semantic palettes. Both surfaces pick it up on the next render.

---

## Seeing the change

Neither surface needs a scan to look at.

```
python ui_preview.py                 # preview/index.html - the app in every state
python ui_preview.py -b brand.json   # ... in your colours
python report_sample.py              # sample-report.html from fixtures/sample_scan.json
python report_sample.py --pdf        # ... and the paginated PDF
```

`ui_preview.py` writes the **real page** — the same string the server serves —
once per state it can be in (empty, advanced options open, queued, scanning,
complete, partial, failed, blocked, refused), plus the fixture-rendered report
so the two can be compared side by side. On a running local server the same
harness is live at `/preview`, or `/?preview=partial` for any single state.

The preview only fabricates what the page *displays*. It never submits,
queues, or reaches a scanner, and the route is refused outside a local
deployment.

---

## Rules for anything added

1. **Tokens only.** No hex, no `rgba()`, no font size, no spacing step, no
   radius, no shadow written into a rule. If a value is missing, add a token.
2. **Shared things go in `design_css.PRIMITIVES`.** If both surfaces would want
   it, it belongs there — and nothing media-specific does.
3. **Every interactive component states four states:** rest, hover,
   `:focus-visible` (one ring, `--focus-ring`, for the whole product), active,
   and disabled.
4. **Semantic colour is not decoration.** Green/amber/red mean good/needs
   work/critical and nothing else. Never use the brand accent to signal a
   verdict, and never use a verdict colour for emphasis.
5. **Contrast is checked, not eyeballed.** `design_tokens.contrast_ratio()` is
   right there, and the test suite asserts AA on every semantic colour against
   both the paper it sits on and its own tint.
