# Changelog — 2026-07-02: Five critical fixes + data quality as a first-class value

Response to the five critical findings in [`docs/code-review-2026-07-02.md`](code-review-2026-07-02.md)
(C-1..C-5), implemented under one architectural principle applied app-wide: **data quality is a
first-class value**. A missing, stale, failed, or partial input is never allowed to present as a
benign one — it is represented distinctly (`None`/"unknown"/data-gap), degraded per-signal rather
than per-source-group, floored in confidence, and surfaced to the user next to the number it
affects. Engine purity (NFR-4), the provider boundary (FR-13), and thresholds-as-data (FR-20a)
are preserved throughout; no code path gained a verdict.

Suite: **343 passed** (baseline 285), hermetic, ruff clean. Golden SITREPs deliberately
regenerated (diff reviewed below). Threshold config `confidence` bumped **1.0.0 → 1.1.0** with
provenance.

---

## C-1 — Slot-canyon fallback was dead code live (fixed)

**`src/upstreamwx/ingest/openmeteo.py`**
- `convective_rate_in_per_hr` is now populated: the window max of Open-Meteo's hourly
  `precipitation` (inch over one hour = in/hr rate). The engine's conservative slot fallback
  (§16.1, force ≥ High at > 0.5 in/hr) is now live for the first time.
- `cape_jkg` is now populated (window max of the already-requested `cape` column) — the
  lightning CAPE-context note is live.
- `wind_mph` is now populated (window max) — the SITREP "Wind: n/a" line is gone.
- **`src/upstreamwx/engine/hazards/flash_flood.py`**: a slot mission whose rate feed is down now
  emits an explicit note — "DATA GAP: … the conservative slot-canyon fallback could not be
  evaluated" — instead of silently skipping the safeguard.

## C-2 — PDF endpoint injection into server-side Chromium (fixed)

**`frontend/pdf/briefing-pdf.html`**
- `fmtWindow()` HTML-escapes before the digit reformat, closing the three unescaped innerHTML
  sinks (hazard-table window, phase-card window, forecast header hours). `fmtWindowRange`,
  `fmtWhen`, lat/lon, watershed/RoC numbers, and all `risk_inputs` values are `esc()`-wrapped or
  `Number()`-coerced. Full-template audit: every interpolation now escapes, coerces, or maps
  through a fixed lookup. Output for legitimate briefings is byte-identical (the client-side
  `?print=1` path uses the same file and still works — pinned by a real-Chromium DOM test).

**`src/upstreamwx/api/models.py`**
- Typed sub-models (`MissionView`, `BlufEntry`, `PhaseCard`, `ForecastRow`, `ForecastTable`,
  `RiskInputsView`; all `extra="allow"`) replace bare dicts for every field the PDF template
  interpolates. `ClockToken`/`ShortToken` types reject `<`/`>` and cap length; numeric risk
  inputs reject string/HTML payloads. `frontend/data/sample-briefing.json` round-trips byte-equal
  (pinned by test).

**`src/upstreamwx/api/app.py`**
- `POST /v1/briefing/pdf`: 2 MiB payload cap (413; checked against Content-Length *and* actual
  bytes), a dedicated 2-slot render semaphore (503 + `Retry-After: 10` when saturated),
  and a `[A-Za-z0-9._-]` whitelist on the Content-Disposition filename.

**`src/upstreamwx/sitrep/pdf.py`**
- Playwright `page.route` gate: every request not resolving (after percent-decode +
  `Path.resolve`) to the template itself or its single logo asset is aborted — blocks `file://`
  local-file reads, http(s)/metadata-IP exfiltration, and `data:` navigation.

## C-3 — Scheduler blocked the event loop (fixed)

**`src/upstreamwx/api/scheduler.py`** — `warm_and_prune()` and `refresh_active()` now run via
`await asyncio.to_thread(...)`; the API stays responsive through cycle-boundary passes.
Exception/heartbeat semantics unchanged.

**`src/upstreamwx/api/app.py`** — lifespan shutdown awaits the cancelled scheduler task with a
10 s `asyncio.wait_for` (logged + abandoned on timeout) instead of hanging indefinitely; the
redundant `except (CancelledError, Exception)` split into explicit branches.

## C-4 — Missing/failed/NaN inputs read as "safe" (fixed, systemically)

### Aggregation layer
**`src/upstreamwx/grib/zonal.py`**
- `PolygonAggregate.max_value`/`mean_value` are now `float | None`: an **all-NaN reduction
  returns `None`** (previously a NaN that compared False against every cut point and read as
  "no hazard"). Partial NaN cells are ignored (nan-aware reductions, warning-free).
- The nearest-cell fallback now **refuses a polygon whose centroid is off the grid**
  (`ValueError`) — a mission outside a grid's domain degrades to "source unavailable" instead of
  being answered with an unrelated edge cell's value. The honest sub-cell-headwater fallback is
  preserved (`_off_grid` guard, one-cell slack, both longitude conventions).

### Ensemble providers
**`src/upstreamwx/ingest/gefs_provider.py`**
- **Per-member error containment**: network/timeout/OS errors (and the new off-grid
  `ValueError`) degrade a single member sample to `None` instead of discarding the entire
  ~250-task ensemble (the old all-or-nothing `fut.result()` propagate). Routine mid-publish
  404s no longer sink GEFS.
- **Member quorum** (`MIN_MEMBERS = 8`): a forecast hour publishes an exceedance probability
  only when ≥ 8 members answered — a 2-member "50%" is noise. Below quorum everywhere →
  explicit "ensemble unavailable" note. Partial ensembles surface a
  "as few as N/31 members contributed" provenance note.
- **Horizon guard**: `_select_fhours` is clamped to the 0.25° product's real f240 horizon
  (was 384 — guaranteed 404s), and a window *wholly beyond* the horizon now returns no hours →
  an explicit "beyond the product horizon" note, instead of silently sampling one clamped
  off-window hour and presenting it as the window's signal.
- NaN member values are dropped from **both numerator and denominator** (they arrive as `None`
  via the zonal layer's isfinite contract).
- `bundle.gefs_cycle` provenance stamp ("YYYYMMDD/HHZ") — the run actually used.
- LAoC decoupling: accepts `polygon=None` (watershed failed) and still serves the lightning
  proxy over the LAoC disk.

**`src/upstreamwx/ingest/refs_provider.py`** — same isfinite/off-grid handling via
`_domain_max`; accepts `polygon=None` (skips QPF, serves lightning); freshness gate (see C-5).

**`src/upstreamwx/ingest/refs_selection.py`** — **bucket-coverage selection**: a valid hour
between REFS's 3-hourly outputs is covered by the enclosing fhour's 3 h accumulation bucket
instead of being dropped. A 12:10–14:50Z slot window previously resolved to *zero* sources and
was mislabeled "outside the same-day range", silently trading the authoritative 3 km ensemble
for coarse GEFS — exactly the product's core use case.

### Surface feed
**`src/upstreamwx/ingest/openmeteo.py`**
- `forecast_days` 3 → 16 (covers the ensembles' 10-day horizon; a day-4 mission previously read
  as "dry"/no-thermal with no note, and the missing `measurable_precip` gated the GEFS Elevated
  flood band off).
- **Coverage-aware tri-state**: `measurable_precip` / `antecedent_precip_24_72h` are now
  `True` / `False` / `None`-unknown. `False` only when the window (resp. prior hours) was
  actually covered by the fetched series; partial or missing coverage yields `None` plus an
  explicit note. Observed precip is always a real `True` (asymmetric, conservative).

### Provider split-chain degradation
**`src/upstreamwx/ingest/nws.py`** — the alerts query and the AFD chain now degrade
**independently**: a failed AFD listing no longer discards successfully fetched alert flags
(the authoritative flood/thunderstorm anchor), and vice versa. `sources_ok["nws"]` = the alerts
check; new `sources_ok["nws_afd"]` = the discussion chain. Both are FR-5 mandatory
(`orchestrator.MANDATORY` updated).

### Orchestrator
**`src/upstreamwx/ingest/orchestrator.py`** — the **LAoC disk no longer requires the basin**:
a USGS/NLDI outage no longer silences the lightning ensemble (the disk needs only the activity
point). When the watershed fails but the LAoC stands, GEFS/REFS run lightning-only with an
explicit note.

### Bundle → engine boundary
**`src/upstreamwx/ingest/base.py`**
- `IngestBundle.measurable_precip`/`antecedent_precip_24_72h` are tri-state (`bool | None`,
  default `None` = unknown); new `gefs_cycle` provenance field.
- `to_hazard_inputs()` carries availability across the boundary: new
  `HazardInputs.nws_products_available` (False when the alerts check failed → the engine says
  "products unverified" instead of treating the False flags as "no active products").
- New **`bundle_data_gaps(bundle)`** — the single source of truth naming the gaps affecting a
  briefing (flood/lightning ensemble, thermal series, surface precip, NWS alerts/AFD,
  watershed), consumed by both the Markdown render and the structured contract.

### Engine (deterministic; corpus updated deliberately, see below)
**`src/upstreamwx/engine/models.py`** — `measurable_precip: bool | None` (default `False`
preserves every existing corpus vector), `antecedent_precip_24_72h: bool | None`,
`nws_products_available: bool = True`.

**`src/upstreamwx/engine/hazards/flash_flood.py`**
- Missing ensemble → driver now reads "DATA GAP: … flood tier is **unassessed, not low**"
  (was "No GEFS precip signal", readable as "dry").
- Elevated band with **unknown** `measurable_precip` applies **conservatively** (Elevated with
  an explanatory driver) instead of being gated off by a gap; a genuinely dry `False` still
  gates it off exactly as before (and its driver now says which case fired).
- Unchecked NWS products and an unevaluated slot fallback emit explicit DATA GAP notes.

**`src/upstreamwx/engine/hazards/lightning.py`**
- All-signals-missing → "DATA GAP: … lightning tier is unassessed, not low" (was a driver
  claiming probabilities were below threshold).
- Unchecked thunderstorm warnings emit a DATA GAP note.
- `refs_override_min` comparison fixed to `>=` matching the configured contract and the
  user-facing note (was strict `>`; REFS at exactly 60.0% now overrides the AFD ceiling).

**`src/upstreamwx/engine/hazards/heat.py` / `cold_wet.py`** — missing-data drivers now read
"DATA GAP: … unassessed, not absent/low".

**`src/upstreamwx/engine/confidence.py` + `data/thresholds/confidence.yaml` (1.0.0 → 1.1.0)**
- New **missing-primary floor**: when a hazard's primary basis (ensemble signal for
  flood/lightning, thermal series for heat/cold — or a *verified* active product) was
  unavailable, confidence is the configured `missing_primary_confidence: low` instead of the
  no-ensemble Moderate default. "Flash flood: Minimal, Moderate confidence" during a total
  ensemble outage — the review's worst degradation shape — is no longer possible; it now reads
  Minimal / **Low** confidence with a DATA GAP driver and a briefing-level gaps list.
- Stale SREF wording in the YAML refreshed; the floor is config, not code (FR-20a).

### Surfacing (render + structured contract + API)
**`src/upstreamwx/sitrep/render.py`** — new "DATA GAPS affecting this briefing:" section
(before Source availability) whenever gaps exist; GEFS section header carries the cycle used;
new tri-state "Measurable window precip" line; "Antecedent precip" renders
"unknown (data unavailable)" instead of a reassuring "no".

**`src/upstreamwx/sitrep/structured.py` + `src/upstreamwx/api/models.py` +
`frontend/data/sample-briefing.json`** — new top-level **`data_quality`** block:
`{gaps: [...], gefs_cycle, refs_cycle}` (additive contract change; PWA rendering of it is
follow-on work — the markdown Briefing tab already shows the gaps section).

## C-5 — Stale cycles served as current / cache token lied (fixed)

**`src/upstreamwx/config.py`** — new `ensemble_max_age_h` (default **24 h**): the freshness
contract, in config rather than ops behavior.

**`src/upstreamwx/ingest/gefs_provider.py::_resolve_cycle`** — a cached cycle older than the
bound is never served as current; falls through to the live NOMADS probe (the pre-existing
cache-through fetch handles the download).

**`src/upstreamwx/ingest/refs_provider.py`** — when the *newest* warmed REFS run exceeds the
bound, REFS degrades loudly to "unavailable" with an age note instead of serving a stale run as
the authoritative same-day signal (older runs remain usable as spin-up backfill only while a
fresh run leads).

**`src/upstreamwx/api/service.py`** — cache validity now tracks **the data, not the clock**:
`_cycle_token()` derives the token from the newest fresh-enough warmed cycle on disk (cheap,
hermetic), else a live `latest_available_cycle()` probe memoised 5 min, else the wall-clock
boundary as last resort (feed dark). At 12:10Z a briefing built from the 06Z run is now keyed
and advertised as `T06Z` — and regenerates within minutes of 12Z actually publishing, instead
of masquerading as 12Z-fresh for six hours. `refresh_active` uses the same token, eliminating
the warm-vs-refresh disagreement. Stale `SREF` docs in `api/cycles.py` (which claimed a
03/09/15/21Z cadence) corrected.

---

## Corpus & test changes (all deliberate, with rationale in-file)

- **`tests/corpus/confidence.yaml`** — member-support cases now set the primary driver they
  describe (support without a driver was an unreachable artifact vector); new cases pin the
  missing-primary floor for all four hazards, the product-anchors-confidence path, the
  unverified-products path, and partial-disagreement-leaves-Low (the `min(base, cap)` edge).
- **`tests/corpus/flash_flood.yaml`** — the REFS overlay cut points (40/39/10/9) and both REFS
  *raise* paths (over a GEFS band, over an active product) are now pinned — previously zero
  coverage of the "authoritative in-window" flood signal (review H-10); plus
  unknown-precip-conservative-band, ensemble-missing-unassessed, and
  antecedent-bump-saturates-at-Extreme.
- **`tests/corpus/lightning.yaml`** — ceiling override pinned at exactly 60 (and 59 stays
  capped), no-signal-unassessed case.
- **New `tests/test_data_quality.py`** (23 tests) — zonal all-NaN→None / partial-NaN /
  off-grid-raise / sub-cell-preserved; Open-Meteo C-1 fields, covered-dry=False,
  uncovered=unknown, partial-coverage=unknown; GEFS beyond-horizon returns no fhours; REFS
  staleness gate; NWS split-chain (alerts survive AFD failure; failed alerts mark products
  unchecked); `bundle_data_gaps` derivation; `_cycle_token` disk/probe/clock resolution order.
- **New `tests/test_api_models.py`, extended `tests/test_pdf_export.py`, new
  `tests/test_scheduler.py`** — C-2/C-3 coverage: hostile-payload rejection (8 parametrized
  vectors), 413/503/filename hardening, request-gate unit tests, a real-Chromium DOM
  no-execution test, loop-responsiveness sentinel, shutdown-timeout behavior.
- **`tests/test_refs_selection.py`** — updated to the bucket-coverage semantics + a new
  short-window regression test; **`tests/test_timezones.py`** adapted to the typed
  `MissionView`; **golden SITREPs regenerated** (`tests/gen_sitrep_goldens.py`), diff reviewed:
  slot DATA GAP note, GEFS cycle in the source header, tri-state precip lines, confidence
  version string.

## Client-visible behavior changes

1. Briefings during degradation now show **Low** confidence + DATA GAP drivers/notes instead of
   Minimal/Moderate with generic source notes; a new "DATA GAPS" section appears in the
   Markdown SITREP and a `data_quality` block in the JSON contract.
2. A mission in the GEFS Elevated band during a surface-feed outage now briefs **Elevated
   (conservative)** instead of Minimal.
3. Day-4/5 missions get real thermal/precip data (16-day fetch) instead of silent "no data".
4. Short same-day windows retain REFS coverage.
5. `cache_cycle` in responses is the newest *available* run, not the wall-clock boundary.
6. `/v1/briefing/pdf`: 413 over 2 MiB, 503+Retry-After under render load, 422 for markup in
   typed fields, stricter download filenames.
7. Slot missions with a live surface feed can now actually trigger the conservative slot
   fallback (C-1); with the feed down they say the safeguard was unevaluated.

## Known follow-ons deliberately out of scope here

High-severity review findings not covered by the five criticals: watershed trace truncation
(H-1) and watershed cache pinning/keys (H-9); alerts-over-basin (H-4); AFD window-blind ceiling
(H-5, partially mitigated by the ≥ fix); offline PWA persistence (H-7); API auth/rate limits
(H-8); heat-index proxy (H-11); dead heat modifier config (M-1); provider-level cut points into
YAML (arch #4). The dead `approach_strain_categories`/`approach_surface_min_category` keys and
the PWA rendering of `data_quality` remain open.
