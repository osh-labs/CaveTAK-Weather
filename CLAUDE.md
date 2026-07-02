# CLAUDE.md

Guidance for AI coding agents (and humans) working in this repository. Read this
top-to-bottom once before your first change; it encodes the non-negotiable product
constraints and the conventions every existing file already follows.

## What this is

**UpstreamWX** (repo dir: `CaveTAK-Weather`) is a mission-specific, multi-hazard
weather briefing system for **caving and canyoneering** across the contiguous US.
It synthesizes NWS products, Open-Meteo derived fields, and in-house **GEFS + REFS
ensemble** GRIB2 processing into a BLUF/SITREP covering four life-safety hazards
(GEFS + REFS replace the retired SREF + HREF — NWS SCN 26-47, EOL 2026-08-31):

- **flash flooding** (with **upstream-watershed aggregation** — the technical centerpiece)
- **lightning**
- **heat stress** (uses NWS Heat Index categories, *not* the 4-tier ladder)
- **cold/wet hypothermia**

It is a free, donation-supported PWA. The two governing documents are
[`UpstreamWX-PRD-v0.8.md`](UpstreamWX-PRD-v0.8.md) (the PRD — behavior, content,
requirements, the `FR-XX`/`NFR-X` numbers used everywhere in the code) and
[`roadmap.md`](roadmap.md) (the build sequence, milestones `M0.0`–`M0.5`).

## Non-negotiable product constraints

These are load-bearing. Violating one is a correctness bug, not a style nit.

1. **Reference-only, never a verdict.** The product surfaces hazard assessment and
   links to authoritative sources; it **never** issues a go/no-go, "all clear," or
   "all systems go." No code path may emit a recommendation. The reference-only
   disclaimer (PRD Appendix C) ships in **every** user-facing artifact and is
   persistent/non-dismissible in the PWA (FR-39, FR-40).
2. **The deterministic engine owns every posture; the LLM only frames.** The rule
   engine (`upstreamwx.engine`) decides all hazard tiers/categories/confidence.
   The Claude Haiku layer (`sitrep/frame.py`) may *narrate* the structured result
   but must **never** change a posture (FR-13, FR-20, FR-21). Identical inputs →
   identical engine output (NFR-4); keep `engine.assess` pure and deterministic.
3. **Thresholds are data, not code.** Every hazard cut point lives in versioned
   YAML under `src/upstreamwx/data/thresholds/` with a `provenance` block. The
   engine references config by key and **never hard-codes a number** (FR-20a). To
   tune a threshold, edit the YAML — not the evaluator.
4. **Providers are swappable behind an interface.** The engine never imports a data
   provider. Every source fills an `IngestBundle`; `to_hazard_inputs()` maps it to
   the engine's `HazardInputs` (FR-13, §12).
5. **Graceful degradation.** A non-mandatory source being down must mark that input
   unavailable and continue, never crash the briefing (NFR-6).

## Repository layout

```
src/upstreamwx/        backend package (importable as `upstreamwx`)
  config.py              pydantic-settings Settings (env prefix UPSTREAMWX_, reads .env)
  timezones.py           lat/lon -> IANA zone (timezonefinder, offline); localizes the mission window to local wall-clock (FR-9)
  engine/                deterministic decision engine (the product's spine)
    models.py              Mission, HazardInputs, HazardPosture, BriefingResult; Tier/HeatCategory/Confidence enums
    assess.py              orchestrator: assess(mission, inputs, config) -> BriefingResult
    phases.py              phase inference (FR-9a), applicability matrix (FR-14a), thermal weighting (FR-14b), gating (FR-14c)
    confidence.py          per-hazard confidence from ensemble member support / source agreement (FR-17)
    thresholds.py          loads the YAML threshold config (FR-20a)
    hazards/               one pure evaluate() per hazard: flash_flood, lightning, heat, cold_wet
  data/thresholds/*.yaml   externalized Appendix B threshold matrices + provenance
  ingest/                provider abstraction + live adapters (nws, openmeteo, spc, gefs_provider, refs_provider)
    base.py                IngestBundle + Provider Protocol + to_hazard_inputs()
    orchestrator.py        mission -> trace -> bundle -> HazardInputs
  grib/                  shared GRIB2 .idx byte-range subsetting + polygon zonal aggregation (used by gefs + refs)
  gefs/                  GEFS global ensemble processor — per-member grids, in-house member-exceedance (SREF replacement)
  refs/                  REFS ~3 km same-day supplement (~6-36 h), reuses grib/ — HREF replacement; feed is configurable (refs_source: aws prototype / nomads_para / nomads_prod), prod = com/refs/prod ensprod NEP (SCN 26-48)
  sref/, href/           retired SREF/HREF packages — no longer wired; kept for the M0.0 spikes (post-cutover cleanup deletes them)
  watershed/             HUC-12 resolution + upstream trace + pour-point delineation + on-disk cache — Spike B/D
    cache.py               disk cache for trace/pour-point basins + single-flight registry (warm & briefing coalesce on one trace)
    roc.py                 Radius-of-Concern clip: bound the basin to a user-set disk before aggregation (FR-3)
  sitrep/                M0.2 SITREP layer + `upstreamwx` CLI
    generate.py            generate_briefing(...) — the ONE generation core the CLI and API both call
    render.py              deterministic Markdown render (golden-file tested)
    structured.py          BriefingResult+bundle -> the PWA's structured JSON contract (M0.4)
    hazard_copy.py         static per-hazard threshold-logic copy for the Hazards view (FR-20)
    frame.py               optional Claude Haiku framing (no-posture-change)
    pdf.py                 server-side PDF export: render_pdf(briefing) -> bytes via headless Chromium/Playwright (FR-27)
    sources.py             verify-against-NWS source links
    cli.py                 `upstreamwx` console entry
  api/                   M0.3 FastAPI service (`upstreamwx-api`)
    app.py                 POST /v1/briefing, POST /v1/briefing/pdf, POST /v1/watershed/warm, GET /v1/health; mounts the PWA (StaticFiles, M0.4); refresh scheduler + warm pool
    service.py             BriefingService (cache-aware generation + active-mission refresh + background watershed warming)
    cache.py               BriefingCache (keyed by location/window/activity, valid one ensemble cycle)
    cycles.py              pure ensemble-cycle arithmetic (00/06/12/18Z)
    scheduler.py           asyncio refresh loop
    models.py              MissionSpec request / BriefingResponse (pydantic)
spikes/                  runnable de-risk CLIs (spike_a..f) — historical, still runnable
                           (spike_e REFS / spike_f GEFS de-risk the SREF+HREF EOL transition; see docs/m0.0)
tests/                   hermetic suite + committed fixtures + validation corpus
  corpus/*.yaml            the validation oracle: boundary cases per hazard + historical_replay
  fixtures/                committed sample data (GRIB2 subsets, GeoJSON, golden SITREP .md)
  gen_sitrep_goldens.py    regenerate golden files after an intentional render change
frontend/                static PWA (M0.4); fetches POST /v1/briefing; STYLE_GUIDE.md is the visual source of truth
  data/sample-briefing.json  the frozen structured contract; rendered ONLY in demo mode (GitHub Pages host or ?demo) — production never falls back to it
  pdf/briefing-pdf.html      print-optimized PDF export template (FR-27); rendered server-side by sitrep/pdf.py via headless Chromium; client falls back to localStorage + ?print=1 when offline
docs/m0.0../m0.4/        per-milestone findings + spike reports — read these for "why"
.claude/hooks/           SessionStart hook that installs deps in the web environment
```

## Setup, test, lint, run

The project uses **uv** and Python **3.11**. In Claude Code on the web, the
`SessionStart` hook (`.claude/hooks/session-start.sh`) already creates `.venv` and
runs `uv pip install -e '.[dev]'`, and puts `.venv/bin` on `PATH`. If `pytest` /
`ruff` aren't found, run setup manually:

```sh
uv venv --python 3.11
uv pip install -e ".[dev]"
```

Commands (use the `.venv/bin/` prefix if the venv isn't on PATH):

```sh
pytest                 # hermetic, offline — committed fixtures; network tests deselected by default
pytest -m network      # OPT-IN live-service tests (NOMADS / AWS / USGS). Do NOT run in CI/offline.
ruff check .           # lint (line length 100; rules E,F,I,UP,B)
```

Run the product:

```sh
# CLI — offline, reproducible from a saved HazardInputs (the path to use in dev):
upstreamwx --lat 37.0192 --lon -111.9889 --activity canyon \
    --start 2026-06-20T08:00 --end 2026-06-20T18:00 --name "Buckskin Gulch" --slot \
    --inputs tests/fixtures/sitrep/sample_inputs.yaml --no-frame

# CLI — live end-to-end (hits NWS/Open-Meteo/GEFS/REFS/USGS); framed if ANTHROPIC_API_KEY set
upstreamwx --lat 37.0192 --lon -111.9889 --activity canyon \
    --start 2026-06-20T08:00 --end 2026-06-20T18:00

# API:
upstreamwx-api         # or: uvicorn upstreamwx.api.app:app --reload
```

`--inputs FILE` (CLI) / `"inputs": {...}` (API) **skips live ingest** and renders
from a pinned `HazardInputs` feature vector — use this for any deterministic,
offline, network-free work. No secrets are required for offline work.

## Configuration & secrets

Config is `pydantic-settings` in `config.py`, env prefix `UPSTREAMWX_`, optional
`.env` (git-ignored; see `.env.example`). Key vars:

- `UPSTREAMWX_DATA_DIR` (default `./data`) — runtime cache root, git-ignored.
- `UPSTREAMWX_NWS_USER_AGENT` — NWS API requires a self-identifying UA (FR-5).
- `ANTHROPIC_API_KEY` — enables Haiku framing (read *without* the `UPSTREAMWX_`
  prefix, via a `validation_alias`). Absent → CLI emits the structured render only.
- `UPSTREAMWX_API_ENABLE_SCHEDULER` — set `0` to run the API without the background loop.
- `UPSTREAMWX_API_ENABLE_WARM` — set `0` to disable the background watershed-warming pool.
- `UPSTREAMWX_HEALTHCHECK_URL` — optional Healthchecks.io-style ping URL; the refresh
  scheduler pings it each cycle (dead-man's-switch for a stalled scheduler, FR-12). Unset → no pings.

Never commit secrets, `.env`, or anything under `data/`/`cache/`/`*.grib2` (the
`.gitignore` already excludes these; `tests/fixtures/*.grib2` are the deliberate exception).

## Conventions to match

The codebase is consistent — new code should be indistinguishable from existing code.

- **`from __future__ import annotations`** at the top of every module.
- **Type hints everywhere**, modern syntax (`X | None`, `list[str]`, `tuple[...]`).
- **Docstrings** open every module/public function and **cite the PRD requirement**
  they implement, e.g. `(FR-19)`, `(NFR-4)`, `§16.1`, `Appendix B`. Preserve and add
  these references — they are how the code maps to the spec and how reviewers verify it.
- **Engine domain types are `@dataclass`** (`models.py`); **API request/response are
  pydantic** (`api/models.py`). Threshold/config types are frozen dataclasses.
- **Enums are ordered `IntEnum`** so `Tier`/`HeatCategory` comparisons and the FR-19
  `max(...)` work; each has a `.label` and `.from_name()`. Don't compare by string.
- **Hazard evaluators** are pure functions:
  `evaluate(inputs, cfg, *, ...) -> tuple[Tier, list[str], list[str]]` (tier, drivers,
  notes). They read `cfg[...]` (the YAML) and never hard-code cut points. A signal may
  only **raise** a posture relative to other signals, never silently lower it.
- **ruff** is the linter (line length 100). Run it before finishing.
- Keep comments at the density of the surrounding file; explain *why*, not *what*.

## Data flow (end to end)

```
Mission (point, window, cave/canyon)
  └─ watershed: resolve HUC-12 / pour-point trace -> upstream polygon
       (optional Radius of Concern: clip the basin to a user-set disk — roc.py, FR-3)
  └─ ingest.orchestrator: NWS + Open-Meteo + SPC + GEFS (+ REFS if in same-day range)
       aggregate ensemble probs over the upstream polygon (grib/ zonal) -> IngestBundle
  └─ to_hazard_inputs(bundle) -> HazardInputs   (normalized feature vector)
  └─ engine.assess(mission, inputs, config) -> BriefingResult   (deterministic)
  └─ sitrep.render.render_md(result, ...) -> Markdown   (golden-file deterministic)
  └─ sitrep.frame.frame_briefing(...) -> prepends a plain-language summary (optional, Haiku)
```

The CLI (`cli.py`) and API (`api/service.py`) both route through the **single**
`sitrep.generate.generate_briefing(...)` core, so the API cannot drift from the CLI.
When you change generation behavior, change it there — not in two places.

Ensemble lead-time rule: REFS inside the same-day window (~6–36 h), GEFS beyond; where
both are in range the engine takes the **higher** tier (FR-19), and REFS (3 km) is
**authoritative in-window** for confidence: ingest folds REFS member support into
`member_support[hazard]` (overriding coarse GEFS), and `engine/confidence.py` reads that
`member_support` — the engine never special-cases REFS, keeping the provider boundary clean.
GEFS has no native thunderstorm field, so lightning beyond REFS range uses a GEFS
CAPE×precip member-exceedance **proxy** (`gefs_p_tstm`); REFS `LTNG` drives it in-window.

## Testing conventions

- **Hermetic by default.** `pytest` runs offline against committed fixtures; tests
  that hit live services are marked `@pytest.mark.network` and **deselected by
  default** (`addopts = -m 'not network'`). Any new live test must carry that marker.
- **The validation corpus is the engine's oracle.** `tests/corpus/*.yaml` holds
  hand-built boundary cases per hazard (`test_engine_corpus.py`) plus documented
  historical events (`historical_replay.yaml`, `test_engine_replay.py`). If you
  change engine logic or thresholds, the expected postures in the corpus must still
  hold (or be deliberately, provably updated with rationale).
- **Render is golden-file tested.** `tests/test_sitrep_render.py` compares against
  `tests/fixtures/sitrep/*.md`. After an *intentional* render format change,
  regenerate: `python tests/gen_sitrep_goldens.py`, and review the diff.
- **Framing tests** assert the structured block stays byte-identical (no posture change).
- Add tests with each change; keep the suite green and `ruff` clean before finishing.

## Milestone status (as of this writing)

**Ensemble EOL transition (SREF+HREF → GEFS+REFS).** NWS SCN 26-47 retires SREF **and**
HREF on 2026-08-31. The ensemble spine was migrated to the durable replacements: **GEFS**
(global, per-member; the provider computes member-exceedance in-house since GEFS ships no
probability product, with member fetches fanned across a thread pool) replaces SREF, and
**REFS** (3 km RRFS Ensemble, AWS `rrfs_a` enspost NEP) replaces HREF. The orchestrator runs
`gefs_provider` + `refs_provider`; REFS is authoritative in-window (tier *and* confidence),
GEFS the coarse backstop beyond range; lightning uses REFS `LTNG` in-window and a GEFS
CAPE×precip proxy beyond. Bundle/engine/threshold fields are `gefs_*`/`refs_*`; cadence is
00/06/12/18Z. The new feeds were de-risked live first (spikes E/F, docs/m0.0). The `sref/` and
`href/` packages remain only for those spikes (post-cutover cleanup deletes them). Cut points
are carried over as a seeded baseline pending field calibration to the new ensembles.

Built and validated: **M0.0** (de-risk spikes A/B/C/D resolved YES), **M0.1**
(engine + thresholds + corpus + watershed + ingest), **M0.2** (CLI → `.md` SITREP +
Haiku framing), **M0.3** (FastAPI service, cache, cycle math, shared generation core),
**M0.4** (PWA wired to the live API — see below).

**M0.4** (PWA: map point in → SITREP out). The API now emits the full structured briefing
the PWA renders its five views from (`sitrep/structured.py`, the API analogue of
`render.py`; the contract is `frontend/data/sample-briefing.json`) and serves the PWA
single-origin (`app.py` `StaticFiles` mount, `UPSTREAMWX_FRONTEND_DIR` to override). The
frontend POSTs `/v1/briefing`; dropping/moving the point or editing the mission re-fetches
live, the upstream watershed re-traces and renders (FR-1, FR-33, FR-38). Missions are
planned/edited in a map-based **mission planner** modal (`openMissionPlanner` in
`frontend/js/app.js`; shown at first run and from the mission-card edit pencil): geocode
an address or paste decimal/DMS coordinates, GPS "use current location", a switchable
topo/aerial/street basemap, and a long-press to drop/move a marker whose tooltip edits the
mission name (FR-1, FR-9), and a **Radius of Concern** slider (discrete stops 10/20/50/100/200
mi; stored as `radius_km`) that caps the upstream watershed: the orchestrator clips the basin to
that disk before GEFS/REFS aggregation (`watershed/roc.py`, FR-3). The main-map watershed renders
the kept (clipped) basin as before, the excluded remainder hatched, and the RoC as a fine dashed
orange ring (`watershed.excluded_geometry` + top-level `roc` in the structured contract). Saving
persists the spec to `localStorage` (FR-10) and re-fetches. The Open-Meteo
adapter now also persists a per-hour display series (`IngestBundle.forecast_hourly`,
display-only — never an engine input). Verified live end-to-end in-container.

**Lightning Area of Concern (LAoC).** Lightning is a point/corridor estimate, not a
basin-routed one (PRD §16.1, §13 principle 4), so its ensemble fields (`gefs_p_tstm`,
`refs_p_lightning`) aggregate over a disk around the activity rather than the upstream
watershed. The disk reuses `watershed/roc.py`'s `roc_disk` (the raw circle, *not* intersected
with the basin); the orchestrator hands GEFS/REFS a separate `lightning_polygon` while flash
flood keeps the watershed/RoC domain. The radius is an **app-wide user preference** (not
per-mission): a modular prefs store (`uwx.prefs.v1` in `localStorage`, `loadPrefs`/`savePrefs`
in `frontend/js/app.js`) configured from a **Settings** sheet opened by a persistent gear icon
in the status bar. `postBriefing` folds `lightning_radius_km` into every request; the PWA draws
the LAoC as a solid yellow ring (top-level `laoc` in the structured contract) and a legend item,
without touching map zoom/pan. `mission_cache_key` folds in both `radius_km` and
`lightning_radius_km`. A future version adds a trailhead point + linear route corridor (deferred).

**Latency follow-on (watershed warming).** Cold pour-point delineation (~3–15 s) was the
dominant remaining briefing latency, and pre-caching whole basins is futile (every set of
coordinates yields a slightly different watershed). Instead the planner warms it *the moment
coordinates change*: dropping/moving/geocoding a point (or GPS/manual entry) fires a
debounced `POST /v1/watershed/warm` (`frontend/js/app.js`), which `BriefingService.warm_watershed`
runs on a small background `ThreadPoolExecutor` (`api_enable_warm`, default on), delineating the
basin while the user finishes entering the mission. By the time they generate, the briefing's
`delineate_cached(mission.lat, mission.lon)` hits the warm disk file. The quick user who
generates mid-warm is handled by a **single-flight registry** in `watershed/cache.py`: the
briefing *joins* the in-flight delineation instead of racing it (cache writes are atomic).
Warming only fills a cache the briefing already used — engine output is unchanged.

**Latency follow-on (GEFS ingest speed).** GEFS replaced SREF's pre-baked probability product with
per-member grids, so a cold briefing fetches/decodes ~500 subsets (members × fhours × fields) — and
every cfgrib decode was serialized on one global lock (eccodes is not thread-safe), so the 16-worker
member fan-out decoded one-at-a-time. Three fixes: (1) **GEFS warming is on by default** — the
scheduler *and* `deploy/deploy.sh` pre-warm the `gefs_warm_fhours` band (default f24–f120 / 6 h, the
horizon GEFS owns beyond REFS), via a parallel **download-only** `gefs.warm_cycle` (no wasted decode
at warm time); (2) each per-member GEFS **decode crops to the union of the watershed + LAoC bboxes
at decode time** (`gefs.cache._decode_cropped`, returning a detached ~KB array, *not* the 16.5 MB
global grid) — this happens **in-process by default** so the 16-way member fan-out and the LRU never
retain full grids (retaining full grids in-process is what OOM-killed uvicorn on the ≤2 GB prod host
→ nginx 502). The crop can *optionally* run in a spawn `ProcessPoolExecutor` owned by the API
lifespan (`api_enable_decode_pool`, **opt-in / default OFF**; the pool path skips the compute lock —
cross-process decode is eccodes-safe — and falls back in-process on a broken pool, NFR-6). **The
pool is off by default because each spawn worker re-imports the scientific stack (xarray + cfgrib +
regionmask/rasterio + timezonefinder) at ~300–500 MB RSS each, which OOMs a small host; only enable
it where there is real RAM headroom.** Cropping at decode time is bit-identical to the old
decode-full-then-crop-per-domain (NFR-4). (3) the decoded-grid LRU
(`grib/cache.py`) is now **memory-budget-aware** (`decode_cache_max_bytes`, default ~128 MiB; with
GEFS cropped at decode time this mainly bounds the larger REFS native grids) with a count backstop
instead of a flat 48-entry cap. Engine output is unchanged — the union-crop-then-mask is
bit-identical to decode-full-then-crop-per-domain (NFR-4).

**Data-quality hardening (2026-07-02).** Following the pre-launch review
(`docs/code-review-2026-07-02.md`; changelog `docs/changelog-2026-07-02-data-quality.md`),
**data quality is a first-class value** end to end: a missing/stale/NaN/partial input is never
allowed to read as benign. Concretely: zonal aggregates return `None` (never NaN) and refuse
off-grid nearest-cell fallbacks; GEFS tolerates per-member failures behind a member quorum and
is clamped to the real f240 horizon; REFS selection covers between-output hours by accumulation
bucket and both ensembles enforce a freshness bound (`ensemble_max_age_h`, default 24 h) — the
API cache token tracks the newest *available* cycle, not the wall clock; Open-Meteo fetches 16
days, populates `convective_rate_in_per_hr`/`cape_jkg`/`wind_mph` (the slot fallback is live),
and the precip booleans are tri-state (`None` = unknown ≠ dry — an unknown applies the GEFS
Elevated band conservatively); NWS alerts/AFD degrade independently (`sources_ok["nws_afd"]`);
the LAoC no longer needs the basin (a watershed failure doesn't silence lightning); the engine
emits explicit "DATA GAP … unassessed, not low" drivers and `confidence.yaml` v1.1 floors
confidence at Low when a hazard's primary driver was unavailable (`missing_primary_confidence`).
`bundle_data_gaps()` (ingest/base.py) is the single gap-derivation source rendered as the
SITREP "DATA GAPS" section and the structured contract's `data_quality` block. The PDF endpoint
is hardened (typed sub-models in `api/models.py`, template escaping, a Playwright request gate,
size/concurrency caps) and the refresh scheduler runs off the event loop (`asyncio.to_thread`).
A second round (changelog `docs/changelog-2026-07-02-high-fixes.md`) closed the remaining
review highs: the upstream trace probes external inflow at **every** node and carries
first-class completeness (`UpstreamTrace.complete` → DATA GAP + flash-flood confidence capped
at Moderate, `confidence.yaml` v1.2); the watershed cache is identical-point-only (6-decimal
keys, TTL'd fallback entries, self-healing reads, resolve-before-write single-flight); NWS
flood products are checked over the **basin** (sampled points, OR-merged with the point check);
the lightning AFD ceiling applies only in REFS range (`lightning.yaml` v1.5); `heat_index_f`
is the real NWS Rothfusz index from temp+RH (apparent temperature remains the cold/wet basis);
the PWA persists the last briefing for offline review (`uwx.briefing.v1`, age-labeled) and the
offline PDF handoff works; the API validates MissionSpec (CONUS bounds, window/radius caps,
currency), bounds `_active`/warm queues, and rate-limits frame/pdf/warm per IP.

**GEFS corrupt-subset resilience (2026-07-02).** A byte-range subset fetched while a `.grib2`
was still publishing can be truncated (decodes to `EOFError`); a one-member hiccup previously
sank the whole GEFS source and stuck there (changelog `docs/changelog-2026-07-02-gefs-resilience.md`).
Now: the shared download path (`grib/idx.py`) validates GRIB2 framing (`GRIB`…`7777`, declared
lengths, message count) via `validate_grib2_bytes` before a subset is accepted — a truncated
download raises `TruncatedGribError` (a `ValueError`) so it never reaches the cache and the member
degrades behind the quorum; `gefs/cache.py` self-heals any bad file already on disk (discard +
re-fetch once); and `gefs_provider._member_sample` catches `EOFError`. Applies to REFS too (same
download path). Engine output unchanged (NFR-4).

**Briefing tab.** The PWA now has six primary tabs in this order: Overview, Map, Hazards,
**Briefing**, Forecast, Resources. The Briefing tab renders the full Markdown SITREP
(`BriefingResponse.markdown`) as formatted HTML using a zero-dependency in-browser converter
(`renderMarkdown` / `_inlineFormat` in `frontend/js/app.js`) that handles headings, pipe
tables, bullet lists, bold, and URLs. When Haiku framing is active (`b.framed === true`) a
non-dismissible attribution banner appears above the text. The `markdown` field is now
included in `to_structured()` (and therefore in the structured JSON contract and
`sample-briefing.json`) rather than being spliced in separately by the service layer.

Deferred to **M0.1.1** (requires the always-on EC2 host; cannot be validated in an
ephemeral container): the recurring GEFS/REFS scheduler **cadence** and the
**cross-restart persistent cache**. The host-independent cores (on-demand GEFS/REFS
processing, cache semantics, cycle arithmetic, a single refresh pass) are built and
tested. **M0.5** (flesh out the PWA — offline cache timestamp UX FR-26/41, remaining
timeline polish) is in progress; `STYLE_GUIDE.md` is the visual source of truth. **PDF
export (FR-27)** is built server-side: `POST /v1/briefing/pdf` accepts the structured
`BriefingResponse` the PWA already holds in memory, renders `frontend/pdf/briefing-pdf.html`
via headless Chromium (Playwright, `sitrep/pdf.py`) with `window.__BRIEFING__` injected as
an init script, and returns a clean `application/pdf` download — no browser URL chrome, no
iOS print-preview trap. The client falls back to the localStorage → `?print=1` path when
offline or the server endpoint is unavailable. The print template (light-theme, US Letter,
running §17.3 reference-only footer in every page's `<tfoot>`) is precached by `sw.js`
so the fallback path still works offline.

**Domain split (app subdomain + static landing).** The app (PWA + `/v1/*`, still
single-origin) now lives at **`app.upstreamwx.com`**; the apex **`upstreamwx.com`** (+ `www`)
serves a standalone **static landing page** from `landing/` — a vendored-token mirror of the
PWA's About view (who-we-are, donate, condensed methodology) with the reference-only
disclaimer and a prominent "Open the app" / install CTA. nginx grew a second server block
(`deploy/nginx/landing.conf`, installed only when `DEPLOY_LANDING_SERVER_NAME` is set; the app
block is `deploy/nginx/upstreamwx.conf`), config gained `DEPLOY_APP_SERVER_NAME` /
`DEPLOY_LANDING_SERVER_NAME` / `DEPLOY_LANDING_ROOT`, and one multi-SAN cert covers both names.
The frontend is origin-portable (relative `/v1/*` paths, `./` manifest scope), so the move
needed no API rewiring or CORS; `manifest.webmanifest` `id` is pinned to the app origin and an
in-app "Add to Home Screen" pill (status bar) captures `beforeinstallprompt`.

For the "why" behind any milestone, read `docs/m0.X/README.md` and the spike reports
in `docs/m0.0/`.

## Working agreements

- **Branch:** develop on `claude/codebase-review-v0.5.0-xu0o7t` (create locally if
  needed). Never push to a different branch without explicit permission. Commit with
  clear messages; push with `git push -u origin <branch>` (retry with backoff on
  network errors). **Do not open a PR unless explicitly asked.**
- Prefer the **offline `--inputs` path** for development and testing — it is
  network-free, deterministic, and needs no secrets.
- When in doubt about *what* to show or *how it behaves*, the **PRD wins**; for PWA
  *visual* details, `frontend/STYLE_GUIDE.md` is authoritative.
- Don't weaken the five non-negotiables above to make a test pass — that's the bug.
- **Releases & deploys:** the workflow is GitHub Flow → CI gate (`.github/workflows/ci.yml`)
  → staging mirror → tag-promoted production. The discipline (env ladder, branch
  protection, tag promotion, rollback, PWA cache busting) lives in
  `docs/deployment-workflow.md`; the host scripts live in `deploy/` (`DEPLOY_CONFIG`
  selects the environment). `deploy.sh` stamps the release into git-ignored
  `frontend/version.json`, which `/v1/health` echoes and the PWA polls to nudge stale
  clients to reload.
</content>
</invoke>
