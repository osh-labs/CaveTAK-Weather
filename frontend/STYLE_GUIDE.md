---
project: UpstreamWX
type: ui-style-guide
status: draft
version: 0.1
date: 2026-06-18
owner: Chris Lee
related: UpstreamWX PRD v0.8 (§6.8, Appendix D), roadmap.md (M0.4–M0.5)
---

# UpstreamWX — UI Style Guide (Source of Truth)

This is the **visual source of truth** for the UpstreamWX PWA. The PRD (§6.8,
Appendix D §18) governs *behavior, content, and information architecture*; this
document governs *how it looks* — the dark briefing chrome, the design tokens,
and the component patterns derived from the reference mockups and adapted to the
PRD.

> **Precedence.** Where this guide and the PRD disagree on *what* is shown or
> *how it behaves*, the PRD wins. Where the PRD is silent on a *visual* detail
> (a color value, a radius, a spacing step), this guide is authoritative. The
> tokens here are mirrored exactly in [`styles/tokens.css`](styles/tokens.css) —
> change them in both places, or treat the CSS as generated from this table.

---

## 1. Design principles

1. **Dark, glanceable, field-oriented.** The chrome is a low-light "weather
   briefing" surface meant to be read on a phone at a trailhead in bright sun or
   at night. High contrast for posture/severity; quiet chrome everywhere else.
2. **BLUF first.** The most consequential information — overall posture, the
   four hazards, confidence — is the largest and highest-contrast thing on the
   Overview. Drill-down detail is quieter and one tap away (FR-22).
3. **Severity is the only loud color.** Color carries meaning (the severity
   ladder, FR-35). Chrome is neutral so that a red Extreme bar is never
   competing with decorative color.
4. **Reference-only, never a verdict.** No go/no-go, no "all clear," no green
   "all systems go" affordance (FR-39). Status chrome describes **data
   currency** ("Briefing current as of …"), not a recommendation. The
   reference-only disclaimer is persistent, not dismissible (FR-40).
5. **Confidence is always visible.** Every posture carries its confidence —
   as timeline hatching *and* an explicit label — and is never collapsed to a
   single mission number (FR-36).
6. **Honest about assumptions.** Inferred phases, wet-egress, cave-isolation,
   and slot-shelter assumptions are surfaced in-line, not hidden (FR-9a, §10).

---

## 2. Adaptations from the reference mockups

The mockups (Summit SAR) contribute **chrome and layout only**. The following
table is the authoritative diff applied in this guide and the PWA (PRD §6.8,
Appendix D, §13 "Mockup elements explicitly dropped").

| Mockup element | UpstreamWX treatment | Why |
|---|---|---|
| "Summit SAR" brand + summit hex mark | **UpstreamWX** brand + upstream/water hex mark | Rebrand (FR-33 header) |
| Tabs: Overview · Forecast · Map Layers · Hazards · Resources | **Overview · Forecast · Map · Hazards · Resources** | FR-32 five-view IA |
| Severity legend Low / Moderate / Elevated / Extreme | **Minimal / Elevated / High / Extreme** ladder | FR-35 (mockup legend dropped) |
| Hazards: High Winds, Icing, Blowing Snow, Rough Terrain, … | **Flash flood · Lightning · Heat · Cold/wet** only | FR-14 four-hazard set |
| Timeline keyed to wall-clock only | **Phase-primary** (approach → technical → egress), wall-clock secondary | FR-34 |
| "OVERALL RISK — MODERATE" verdict chip | **"OVERALL POSTURE"** shown as information, never a recommendation | FR-39 |
| "All Systems Go" footer status | **"Briefing current as of <time>"** (data currency) | FR-39 |
| "Add as Alert" / push notifications | **Removed** | Out of scope (§13) |
| Radar / nowcast map layer | **Removed**; map shows **upstream watershed overlay** + alert polygons + point conditions | FR-38 |
| Multi-waypoint route on map | **Single free-form mission point** | FR-1 |
| Heat as a generic colored bar | Heat uses its **NWS categories** (Caution → Extreme Danger) | FR-15 |
| (absent) confidence rendering | **Solid vs. hatched** bars + explicit High/Mod/Low label | FR-36 |
| (absent) reference-only disclaimer | **Persistent** footer on Overview and every briefing surface | FR-40 |
| (absent) offline/cached state | **Cached badge + generation timestamp** when offline | FR-41 |

---

## 3. Color tokens

All values are dark-theme. CSS variable names match
[`styles/tokens.css`](styles/tokens.css) exactly.

### 3.1 Surfaces & chrome

| Token | Value | Use |
|---|---|---|
| `--color-bg` | `#0a0e14` | App background (deepest layer) |
| `--color-surface` | `#11161f` | Cards, tab bar, header |
| `--color-surface-2` | `#1a212d` | Nested/raised panels, metric cards |
| `--color-surface-3` | `#232d3b` | Hover, pressed, active rows |
| `--color-border` | `rgba(255,255,255,0.08)` | Hairline card/divider borders |
| `--color-border-strong` | `rgba(255,255,255,0.16)` | Emphasized borders, focus rings base |
| `--color-overlay` | `rgba(8,11,16,0.72)` | Map card scrims, modals |

### 3.2 Text

| Token | Value | Use |
|---|---|---|
| `--color-text` | `#e6edf3` | Primary text, values |
| `--color-text-secondary` | `#9aa7b8` | Labels, secondary copy |
| `--color-text-muted` | `#5f6b7c` | Captions, timestamps, axis ticks |
| `--color-text-inverse` | `#0a0e14` | Text on solid severity / brand fills |

### 3.3 Brand

UpstreamWX reads "water / upstream"; the accent is a river cyan, deliberately
distinct from the mockups' summit amber.

| Token | Value | Use |
|---|---|---|
| `--color-brand` | `#38bdf8` | Logo mark, active tab indicator, links |
| `--color-brand-strong` | `#0ea5e9` | Pressed brand, focus ring |
| `--color-brand-dim` | `rgba(56,189,248,0.14)` | Active tab wash, link hover bg |

### 3.4 Severity ladder (FR-35) — the only semantic color set

Minimal = green, Elevated = amber, High = orange, Extreme = red. Each tier has a
**solid** fill (bars, chips), a **wash** (chip/row backgrounds), and a **text**
tone (on dark surfaces). Persistence (FR-37) is encoded by bar *length*, not
color; confidence (FR-36) by *hatching*, not color.

| Tier | `--sev-*` (solid) | `--sev-*-wash` | `--sev-*-text` |
|---|---|---|---|
| **Minimal** | `#3fb950` | `rgba(63,185,80,0.16)` | `#56d364` |
| **Elevated** | `#d9a514` | `rgba(217,165,20,0.16)` | `#e3b341` |
| **High** | `#f0883e` | `rgba(240,136,62,0.16)` | `#f0a763` |
| **Extreme** | `#f85149` | `rgba(248,81,73,0.18)` | `#ff7b72` |

### 3.5 Heat — NWS Heat Index categories (FR-15)

Heat does **not** use the four-tier ladder; it uses the NWS categories directly.
Distinct ramp so heat is not confused with the severity tiers.

| NWS category | Token | Value |
|---|---|---|
| Caution | `--heat-caution` | `#e3c01a` |
| Extreme Caution | `--heat-ext-caution` | `#f0a020` |
| Danger | `--heat-danger` | `#ec6a2c` |
| Extreme Danger | `--heat-ext-danger` | `#da3633` |

### 3.6 Confidence (FR-36)

Confidence is rendered by **fill style**, not hue, so it never competes with
severity color:

- **High** — solid fill, full opacity.
- **Moderate** — solid fill at `--confidence-mod-opacity` (`0.82`).
- **Low** — **hatched** fill (`--confidence-hatch`), the mockups' "possible"
  treatment, repurposed as the ensemble-spread cue.

```
--confidence-hatch: repeating-linear-gradient(
  -45deg, transparent 0 5px, rgba(255,255,255,0.22) 5px 7px );
```

The explicit label (High / Moderate / Low) always accompanies the visual cue in
hazard detail.

### 3.7 Utility / feedback

| Token | Value | Use |
|---|---|---|
| `--color-info` | `#58a6ff` | Informational chips, links in body |
| `--color-warn` | `#d9a514` | Degraded-source / assumption notices (NFR-6) |
| `--color-offline` | `#bc8cff` | Cached/offline badge (FR-41) — not a severity hue |
| `--color-success` | `#3fb950` | Form validation only — **never** a posture verdict |

---

## 4. Typography

System font stack (no webfont dependency — installs and renders offline).
Tabular/monospace numerals for all weather values so columns align.

| Token | Stack |
|---|---|
| `--font-sans` | `system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif` |
| `--font-mono` | `ui-monospace, "SF Mono", "Roboto Mono", Menlo, Consolas, monospace` |

### Type scale

| Token | Size / line | Weight | Use |
|---|---|---|---|
| `--text-display` | 28 / 34 px | 700 | Big metric values (temp) |
| `--text-h1` | 22 / 28 px | 650 | Mission title |
| `--text-h2` | 17 / 24 px | 620 | Section / card headers |
| `--text-body` | 15 / 22 px | 400 | Body copy, summary |
| `--text-label` | 13 / 18 px | 500 | Field labels, tab labels |
| `--text-caption` | 11 / 16 px | 600 | Eyebrow labels (MISSION), timestamps; uppercase, `--tracking-wide` |

`--tracking-wide: 0.06em` for eyebrow/caption uppercase labels.

---

## 5. Spacing, radius, elevation, motion

**Spacing** — 4 px base scale: `--space-1: 4px` … `--space-2: 8`, `--space-3: 12`,
`--space-4: 16`, `--space-5: 24`, `--space-6: 32`, `--space-8: 48`.

**Radius** — `--radius-sm: 6px` (chips, inputs), `--radius-md: 10px` (bars,
buttons), `--radius-lg: 14px` (cards), `--radius-pill: 999px`.

**Elevation** — flat by default; depth via surface step + hairline border, not
heavy shadow. `--shadow-card: 0 1px 0 rgba(255,255,255,0.03) inset, 0 2px 8px
rgba(0,0,0,0.4)`. `--shadow-pop: 0 8px 28px rgba(0,0,0,0.55)` for map cards/menus.

**Motion** — `--ease: cubic-bezier(0.2,0.6,0.2,1)`; `--dur-fast: 120ms`,
`--dur: 200ms`. Respect `prefers-reduced-motion`.

**Focus** — visible 2 px ring: `outline: 2px solid var(--color-brand-strong);
outline-offset: 2px`. Never remove focus outlines.

---

## 6. Layout & breakpoints

Mobile-first; the mockups are phone portrait. The shell is a single column that
caps width on larger screens.

| Token | Value | Meaning |
|---|---|---|
| `--app-max-width` | `480px` | Content column cap (phone-faithful) |
| `--bp-md` | `768px` | Tablet: roomier padding, 2-up metric grid stays |
| `--bp-lg` | `1024px` | Desktop: center the column, dim gutters |

Structure (all views share it):

```
┌ Header (sticky)  brand · mission title · cave/canyon · overall posture
├ Tab bar (sticky) Overview · Forecast · Map · Hazards · Resources
│ ─────────────────────────────────────────────────────────────
│ View body (scrolls)
│ ─────────────────────────────────────────────────────────────
└ Disclaimer footer (persistent) + data-currency / offline status
```

---

## 7. Components

Each component below is implemented in [`styles/app.css`](styles/app.css) under
the noted class. Visual contract first; PRD reference in parentheses.

### 7.1 App header (`.app-header`) — FR-33, Appendix D §18.1
- Left: hex mark + `UpstreamWX` wordmark.
- Center/left: mission title (editable affordance — pencil) + window line.
- Right: **cave/canyon** indicator and the **overall posture** chip (info only).
- Sticky; `--color-surface` with bottom hairline.

### 7.2 Tab bar (`.tab-bar`) — FR-32
- Five equal tabs. Active tab: `--color-brand` label + 2 px brand underline +
  `--color-brand-dim` wash. Inactive: `--color-text-secondary`.
- Sticky beneath header. Icons optional; labels mandatory (glanceable).

### 7.3 Mission card (`.mission-card`) — Appendix D §18.1
- Eyebrow `MISSION` (caption), title, date + window, location/HUC line.
- Right column: `OVERALL POSTURE` eyebrow + severity chip + confidence label.
- **No verdict language** (FR-39).

### 7.4 Posture chip (`.posture-chip`) — FR-35
- Pill, severity `--sev-*-wash` background, `--sev-*-text` text, optional solid
  dot. Sizes: `.is-lg` (header/overview), default (inline).
- Heat variant uses the heat ramp + the NWS category label.

### 7.5 Confidence bar (`.confidence`) — FR-36
- A three-stop orange (`--sev-high`) track the same width as the posture pill it
  sits under, with the small gray label `High|Moderate|Low confidence` centered
  below.
- Three beads — low (left) · moderate (centre) · high (right). The stop for the
  engine's level is **filled**, the other two **hollow** (orange outline); the
  level is read from **position, not hue** (FR-36 — style, not hue).
- The connecting segments are fixed decoration: a thin segment left of centre,
  a thicker one to the right. Non-interactive — set by the engine.

### 7.6 Metric card (`.metric-card`) — Appendix D §18.2
- Grid of glanceable cards: label (caption), big value (`--text-display`, mono),
  unit + secondary (feels-like / gust / chance). 2-up on phones.

### 7.7 Hazard line (`.hazard-line`) — FR-22 item 1 / Overview
- One row per hazard: icon, name, posture chip, confidence tag, window-of-concern
  (shown for Elevated+). Tappable → Hazards detail.

### 7.8 Phase strip (`.phase-strip`) — FR-14b, Appendix D §18.2
- Three segments: Approach → Technical span → Egress, with the
  **phase-weighted thermal hazard leading** each (heat↑ approach, cold↑ egress).
- Cave technical span renders **flash flood only** with an "isolated from surface
  weather" note (FR-14c).

### 7.9 Hazard timeline / Gantt (`.timeline`) — FR-34, FR-35, FR-36, FR-37
The defining drill-down visual. **Phase-primary**:
- **Rows** = hazards; **column groups** = phases (approach/technical/egress);
  a secondary wall-clock axis runs beneath.
- **Bar color** = severity (`--sev-*`).
- **Bar length** = persistence: full-period (persistent) vs. windowed (FR-37) —
  *length only, never a different color*.
- **Bar fill** = confidence: solid (High/Mod) vs. hatched (Low) (FR-36).
- A hazard appears **only in phases where it applies** (FR-14a): no lightning bar
  across a canyon technical span; a cave technical span shows flash flood only.
- Legend shows the four tiers + the solid/hatched confidence key. **No**
  Low/Moderate/Elevated/Extreme mockup legend.

### 7.10 Hazard detail row (`.hazard-detail`) — Appendix D §18.5
- Expandable. Header: hazard + posture chip + confidence tag.
- Body: key drivers, explicit confidence label, the relevant threshold logic
  (Appendix B), and stated assumptions (wet-egress / cave-isolation / slot).

### 7.11 Map card (`.map-card`) — Appendix D §18.4
- Map fills the view; the **upstream watershed overlay** is the hero layer
  (`--sev-elevated`/brand tinted polygon). A floating **point-conditions callout**
  card (`--shadow-pop`, `--color-overlay` scrim) shows temp/wind/precip at the
  pin. Alert polygons use severity color at low opacity. **No radar, no routes.**

### 7.12 Source / resource link (`.resource-link`) — Appendix D §18.6
- Row with label, source provenance, external-link affordance. Verify-against-NWS
  links (AFD, alerts, model source), PDF export action, "how this is calculated."

### 7.13 Disclaimer footer (`.disclaimer`) — FR-40
- Persistent, non-dismissible. `--color-text-secondary`, top hairline, slightly
  inset. Carries Appendix C §17.2 copy verbatim.

### 7.14 Status / currency line (`.status-line`) — FR-39, FR-41
- "Briefing current as of <time>" by default. When serving a cached briefing
  offline: prepend the **cached badge** (`--color-offline`) and show the
  generation timestamp (FR-41). Degraded sources (NFR-6) show a `--color-warn`
  note. Never a recommendation.

### 7.15 First-run acknowledgment (`.ack-modal`) — FR-31, Appendix C §17.1
- Full-screen modal shown once before first use; single "I understand — continue"
  action. Copy is Appendix C §17.1 verbatim. Blocks the app until acknowledged.

---

## 8. Iconography

- Line icons, 1.75 px stroke, 24 px grid, `currentColor` so they inherit text
  tone. Hazard glyphs: flash flood = wave/water, lightning = bolt, heat = sun,
  cold/wet = thermometer/snow. Implemented as inline SVG (`js/icons.js`) — no
  icon-font dependency (offline-safe).
- The brand mark is an **upstream hex** (hexagon with a converging-channels
  motif) in `--color-brand`. Provided at `icons/icon.svg` (maskable) and used for
  the PWA install icon.

---

## 9. Accessibility

- Contrast: body text ≥ 4.5:1 on its surface; severity chip text uses the
  `--sev-*-text` tone (verified ≥ 4.5:1 on `--sev-*-wash`). **Never** rely on
  color alone — severity always carries a text label, confidence always carries
  a label, persistence is a shape.
- Hit targets ≥ 44 px. Focus rings always visible (§5).
- `prefers-reduced-motion`: disable bar grow / tab slide transitions.
- Timeline is also exposed as a semantic list for screen readers (each hazard ×
  phase as text: hazard, phase, tier, confidence, window).

---

## 10. Tokens ↔ code mapping

- **Tokens** (§3–§6) → [`styles/tokens.css`](styles/tokens.css) `:root`.
- **Components** (§7) → [`styles/app.css`](styles/app.css).
- **Data contract** the views render → mirrors `BriefingResponse` /
  `BriefingResult` (`src/upstreamwx/api/models.py`, `engine/models.py`); the
  offline sample is [`data/sample-briefing.json`](data/sample-briefing.json).

When the backend lands (M0.4), the only change is swapping the sample fetch for
the live `POST /v1/briefing` call — the render layer and these tokens are
unchanged.
