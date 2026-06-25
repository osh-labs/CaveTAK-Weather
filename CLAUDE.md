# CLAUDE.md

Guidance for AI coding agents (and humans) working in this repository. Read this
top-to-bottom once before your first change; it encodes the non-negotiable product
constraints and the conventions every existing file already follows.

## What this is

**UpstreamWX** (repo dir: `CaveTAK-Weather`) is a mission-specific, multi-hazard
weather briefing system for **caving and canyoneering** across the contiguous US.
It synthesizes NWS products, Open-Meteo derived fields, and in-house **SREF + HREF
ensemble** GRIB2 processing into a BLUF/SITREP covering four life-safety hazards:

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
    confidence.py          per-hazard confidence from SREF spread / source agreement (FR-17)
    thresholds.py          loads the YAML threshold config (FR-20a)
    hazards/               one pure evaluate() per hazard: flash_flood, lightning, heat, cold_wet
  data/thresholds/*.yaml   externalized Appendix B threshold matrices + provenance
  ingest/                provider abstraction + live adapters (nws, openmeteo, spc, sref_provider, href_provider)
    base.py                IngestBundle + Provider Protocol + to_hazard_inputs()
    orchestrator.py        mission -> trace -> bundle -> HazardInputs
  grib/                  shared GRIB2 .idx byte-range subsetting + polygon zonal aggregation (used by sref + href)
  sref/                  SREF ensemble processor (fetch/extract/aggregate over a polygon) — Spike A
  href/                  HREF ~3 km same-day supplement (~6-36 h), reuses grib/ — Spike C
  watershed/             HUC-12 resolution + upstream trace + pour-point delineation + on-disk cache — Spike B/D
  sitrep/                M0.2 SITREP layer + `upstreamwx` CLI
    generate.py            generate_briefing(...) — the ONE generation core the CLI and API both call
    render.py              deterministic Markdown render (golden-file tested)
    structured.py          BriefingResult+bundle -> the PWA's structured JSON contract (M0.4)
    hazard_copy.py         static per-hazard threshold-logic copy for the Hazards view (FR-20)
    frame.py               optional Claude Haiku framing (no-posture-change)
    sources.py             verify-against-NWS source links
    cli.py                 `upstreamwx` console entry
  api/                   M0.3 FastAPI service (`upstreamwx-api`)
    app.py                 POST /v1/briefing, GET /v1/health; mounts the PWA (StaticFiles, M0.4); refresh scheduler
    service.py             BriefingService (cache-aware generation + active-mission refresh)
    cache.py               BriefingCache (keyed by location/window/activity, valid one SREF cycle)
    cycles.py              pure SREF-cycle arithmetic (03/09/15/21Z)
    scheduler.py           asyncio refresh loop
    models.py              MissionSpec request / BriefingResponse (pydantic)
spikes/                  runnable de-risk CLIs (spike_a..d) — historical, still runnable
tests/                   hermetic suite + committed fixtures + validation corpus
  corpus/*.yaml            the validation oracle: boundary cases per hazard + historical_replay
  fixtures/                committed sample data (GRIB2 subsets, GeoJSON, golden SITREP .md)
  gen_sitrep_goldens.py    regenerate golden files after an intentional render change
frontend/                static PWA (M0.4); fetches POST /v1/briefing; STYLE_GUIDE.md is the visual source of truth
  data/sample-briefing.json  the frozen structured contract + SW offline fallback
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

# CLI — live end-to-end (hits NWS/Open-Meteo/SREF/HREF/USGS); framed if ANTHROPIC_API_KEY set
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
  └─ ingest.orchestrator: NWS + Open-Meteo + SPC + SREF (+ HREF if in same-day range)
       aggregate ensemble probs over the upstream polygon (grib/ zonal) -> IngestBundle
  └─ to_hazard_inputs(bundle) -> HazardInputs   (normalized feature vector)
  └─ engine.assess(mission, inputs, config) -> BriefingResult   (deterministic)
  └─ sitrep.render.render_md(result, ...) -> Markdown   (golden-file deterministic)
  └─ sitrep.frame.frame_briefing(...) -> prepends a plain-language summary (optional, Haiku)
```

The CLI (`cli.py`) and API (`api/service.py`) both route through the **single**
`sitrep.generate.generate_briefing(...)` core, so the API cannot drift from the CLI.
When you change generation behavior, change it there — not in two places.

Ensemble lead-time rule: HREF inside the same-day window (~6–36 h), SREF beyond;
where both are in range the engine takes the **higher** tier (FR-19).

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
mission name (FR-1, FR-9). Saving persists the spec to `localStorage` (FR-10) and
re-fetches. The Open-Meteo
adapter now also persists a per-hour display series (`IngestBundle.forecast_hourly`,
display-only — never an engine input). Verified live end-to-end in-container.

Deferred to **M0.1.1** (requires the always-on EC2 host; cannot be validated in an
ephemeral container): the recurring SREF scheduler **cadence** and the
**cross-restart persistent cache**. The host-independent cores (on-demand SREF
processing, cache semantics, cycle arithmetic, a single refresh pass) are built and
tested. **M0.5** (flesh out the PWA — offline cache timestamp UX FR-26/41, PDF export
FR-27, remaining timeline polish) is upcoming; `STYLE_GUIDE.md` is the visual source of
truth.

For the "why" behind any milestone, read `docs/m0.X/README.md` and the spike reports
in `docs/m0.0/`.

## Working agreements

- **Branch:** develop on `claude/claude-md-agent-guide-51fwbu` (create locally if
  needed). Never push to a different branch without explicit permission. Commit with
  clear messages; push with `git push -u origin <branch>` (retry with backoff on
  network errors). **Do not open a PR unless explicitly asked.**
- Prefer the **offline `--inputs` path** for development and testing — it is
  network-free, deterministic, and needs no secrets.
- When in doubt about *what* to show or *how it behaves*, the **PRD wins**; for PWA
  *visual* details, `frontend/STYLE_GUIDE.md` is authoritative.
- Don't weaken the five non-negotiables above to make a test pass — that's the bug.
</content>
</invoke>
