---
project: UpstreamWX
type: roadmap
status: draft
version: 0.5
date: 2026-06-29
host: upstreamwx.com
owner: Chris Lee
related: UpstreamWX PRD v0.8
---

# UpstreamWX — Development Roadmap (Draft)

## Purpose

Sequence the build from core logic to a fleshed-out PWA. Milestones below follow the ordering proposed by Chris, with additive de-risking recommendations folded in (M0.0, threshold config, validation corpus, structured-vs-framing split, early disclaimer). The underlying engineering sequence is unchanged: deterministic engine first, then CLI, then API, then minimal PWA, then full PWA.

## Note on the ensemble spine (SREF+HREF → GEFS+REFS)

The original de-risk spikes and the first build of the ingest layer used **SREF** (global
ensemble probabilities) and **HREF** (same-day convection-allowing supplement). NWS **SCN
26-47** retires *both* SREF and HREF on **2026-08-31**, so the ensemble spine was migrated to
the durable replacements before the public beta:

- **GEFS** (global, per-member) replaces **SREF**. GEFS ships no probability product, so the
  provider computes member-exceedance in-house (member fetches fanned across a thread/process
  pool). It is the coarse backstop beyond the REFS window.
- **REFS** (~3 km RRFS Ensemble, AWS `rrfs_a` enspost NEP; feed configurable to NOMADS para/prod
  per SCN 26-48) replaces **HREF**. REFS is **authoritative in-window** (~6–36 h) for both tier
  and confidence; GEFS owns the longer leads.
- Cadence is **00/06/12/18Z** (was 03/09/15/21Z under SREF/AFD).
- Lightning uses REFS `LTNG` in-window and a **GEFS CAPE×precip member-exceedance proxy**
  (`gefs_p_tstm`) beyond range, since GEFS has no native thunderstorm field.

Bundle/engine/threshold fields are `gefs_*` / `refs_*`. The `sref/` and `href/` packages remain
only for the M0.0 spikes (A/C) and their tests; a post-cutover cleanup deletes them. Threshold
cut points are carried over from the SREF/HREF baseline as a **seeded, uncalibrated** starting
set, pending field calibration against the new ensembles' member statistics (FR-20a — config
change, not code). See the spike-E (REFS) and spike-F (GEFS) reports under `docs/m0.0/`.

## Note on versioning (three axes, do not conflate)

| Axis | Example labels | Meaning |
|---|---|---|
| **Build milestone** | M0.0 … M0.5 (this doc) | Engineering stages of the initial build |
| **Product release** | v0.5.0 (public beta), v1, v1.x, v2 (PRD §5) | Shippable product scope tiers |
| **Document version** | PRD v0.8, this roadmap v0.5 | Revision of a written artifact |

Build milestones M0.0–M0.5 lead up to **product v1**; the **v0.5.0 public beta** is cut at the
end of M0.5 as the first publicly exposed release on the candidate-v1 codebase. The "M" prefix is
deliberate: it keeps build milestones from colliding with product releases and document versions
in conversation and commits.

## Guiding principles

1. **Deterministic core is the product's spine.** The rule engine owns every hazard posture; the language model only frames (PRD FR-13, FR-20). Build and validate the engine in isolation before anything renders it.
2. **Vertical testability at each milestone.** Every milestone ends with something runnable and a defined pass/fail, not just code.
3. **De-risk the two hard unknowns first** (ensemble-over-polygon, upstream HUC-12 trace) — see M0.0. (The ensemble spike was first proven on SREF/HREF, then re-proven on the GEFS/REFS replacements in spikes E/F.)
4. **Thresholds are data, not code.** All hazard thresholds live in versioned config, never hard-coded (PRD FR-20a). Appendix B values are the accepted initial config and are tuned through field testing, not a pre-build redline — so engine logic can be built and validated immediately, and tuned later without code changes.
5. **Reference-only posture from the first artifact.** The disclaimer (PRD Appendix C) ships with the earliest user-facing output, not later.

## Milestone overview

| Milestone | Objective | Primary exit criterion |
|---|---|---|
| **M0.0** | Foundation + de-risk spikes (recommended) | Both hard-unknown spikes demonstrably work on sample data |
| **M0.1** | Data ingest, decision engine, watershed component built and validated | Engine produces correct hazard postures on the validation corpus |
| **M0.1.1** | Ensemble scheduler + persistent cache (EC2-hosted) — *MVP live* | Recurring GEFS/REFS refresh runs on schedule and caches across restarts |
| **M0.2** | SITREP output as `.md` via terminal command | One command turns a mission spec into a complete, disclaimer-bearing `.md` briefing |
| **M0.3** | API functional, passing internal validation | API returns the same briefing the CLI does, with caching/scheduling |
| **M0.4** | PWA framework: map location in → SITREP out | A user picks a point on a map and sees a rendered SITREP |
| **M0.5** | Flesh out PWA → cut **v0.5.0 public beta** | Full IA (PRD §6.8 / Appendix D), offline cache, PDF export; public beta shipped |

---

## M0.0 — Foundation and De-Risk Spikes (recommended addition)

**Objective.** Stand up the repo and prove the two riskiest feasibility questions before committing to M0.1 architecture.

**Deliverables.**
- Repo scaffolding, CI, test harness, secrets handling.
- **Spike A — ensemble over a polygon:** retrieve native SREF GRIB2, extract the ensemble probability fields (P(precip), P(thunder), member spread), and aggregate them over a sample watershed polygon. Confirm data availability and retention for the run cadence. *(Re-proven on GEFS in spike F after the SCN 26-47 EOL — see below.)*
- **Spike B — upstream HUC-12 trace:** given an arbitrary CONUS lat/lon, resolve the containing HUC-12 and trace the upstream contributing watershed from the hosted USGS WBD (PRD FR-2, FR-3).
- Backend decided: **small always-on service on the existing UpstreamWX EC2** (scalable). The M0.0 spike no longer chooses the architecture — it characterizes the ensemble job's resource profile (memory, runtime, cadence) to size EC2 headroom. One-time/batch pre-processing runs on a dev machine; recurring ensemble/AFD refresh runs on the EC2 scheduler (PRD §7).
- **Spike C — HREF same-day high-res supplement (additive de-risk):** confirm the ~3 km HREF convection-allowing ensemble is retrievable on the same NOMADS `ensprod` + `.idx` pattern, extract its neighborhood probability fields (`APCP` for flood; `LTNG`/`REFC` for lightning) over a watershed polygon, and profile its (heavier) per-cycle resource cost. **Resolved YES** (PRD §6.2 FR-7a, `docs/m0.0/spike-c-report.md`). The idx + aggregation code is now shared in `src/upstreamwx/grib/`. *(Re-proven on REFS in spike E after the EOL.)*
- **Spikes E (REFS) + F (GEFS) — EOL transition de-risk:** after SCN 26-47 announced the SREF/HREF retirement, re-prove the same retrieval/aggregation pattern against the durable replacements — GEFS per-member member-exceedance (spike F) and the REFS 3 km same-day supplement (spike E). **Resolved YES** (`docs/m0.0/spike-e-refs-*.md`, `docs/m0.0/spike-f-gefs-*.md`). The shared `grib/` primitives carried over unchanged.

**Exit criteria.** Both core spikes run on sample inputs and produce plausible output; ensemble data-availability risk is resolved yes/no — for both the original SREF/HREF feeds and the GEFS/REFS replacements.

**Why first.** Everything in M0.1 and beyond depends on these two. They are the long poles; failure here changes the architecture. Cheap to spike, expensive to discover late.

---

## M0.1 — Data Ingest, Decision Engine, Watershed Component

**Objective.** The three core subsystems built to a tested state, passing internal validation.

**Recommended internal sequence** (these are not equal-weight or independent):
1. **Watershed component** — promote Spike B to a real module: HUC-12 resolution + upstream trace + cache. Prerequisite for the flood path of both ingest and engine.
2. **Data ingest** — NWS API (AFD, alerts), Open-Meteo (derived fields), the **scheduled GEFS processor** (heaviest backend item, PRD §11.2), the **conditional REFS same-day supplement** (PRD §6.2 FR-7a — reuses the retrieval/aggregation code via the shared `grib` module; fetched only for in-range missions and only the needed forecast hours), SPC convective outlook. Each source behind a stable internal interface so providers can be swapped (PRD §12). The engine selects ensemble signals by lead time — REFS inside the same-day window (≈6–36 h), GEFS beyond — and where both are in range takes the higher tier (FR-19).
3. **Decision engine** — the deterministic rule engine: four hazards, phase × activity applicability matrix (FR-14a), thermal weighting (FR-14b), lightning/cave gating (FR-14c), per-hazard confidence from ensemble member support (FR-17), overall posture as max across applicable hazards (FR-19).

**Cross-cutting deliverables (start here, used by every later milestone):**
- **Threshold config** — Appendix B matrices as versioned config files with provenance (date, rationale, source), loaded by the engine at runtime. Engine logic references config, never hard-coded numbers (PRD FR-20a). Appendix B values are the accepted initial set.
- **Validation corpus** — the oracle for "passing internal validation":
  - *Boundary cases (backbone):* hand-constructed inputs that sit just inside/outside each tier edge, per hazard, fully deterministic and controllable. These are the bulk of the test suite.
  - *Historical replay (realism check):* a small set of documented events (known flash floods, convective days, clear days) replayed from retrievable archived source data, expected to flag the right tier.

**Exit criteria.** Engine produces the expected hazard postures, confidence, and windows across the entire validation corpus; threshold changes are config-only; the ensemble job runs on schedule and caches.

**Build status.** Delivered and hermetically validated: the deterministic engine (`upstreamwx.engine` — four hazards, FR-14a/b/c, FR-17 confidence, FR-19 max), the externalized YAML threshold config (`upstreamwx/data/thresholds/`, FR-20a) with provenance, and the validation corpus (`tests/corpus/`, the exit-criterion oracle). Delivered and live-tested against real services: the watershed promotion with on-disk caching (`upstreamwx.watershed`) and the ingest provider abstraction with live adapters (NWS, Open-Meteo, SPC, GEFS, REFS; `upstreamwx.ingest`). The **recurring ensemble scheduler and persistent cross-restart cache moved to M0.1.1** — an ephemeral dev container cannot validate cadence or cache persistence; that work belongs on the always-on EC2. The on-demand GEFS/REFS *processing logic* those will invoke is built and tested here.

---

## M0.1.1 — Ensemble Scheduler & Persistent Cache (EC2-hosted)

**Objective.** Promote the on-demand GEFS/REFS/watershed processing built in M0.1 to the recurring, always-on backend the PRD assumes (PRD §7, §11.2, FR-12). Hosted on the existing UpstreamWX EC2 (roadmap §M0.0 backend decision), because cadence and cross-restart persistence cannot be exercised in the ephemeral dev environment used for M0.1.

**Deliverables.**
- [x] **Always-on host stood up.** The FastAPI service is deployed on the EC2 instance behind nginx/TLS as a single systemd-managed uvicorn process (`deploy/`), serving the PWA single-origin. This is the MVP live.
- [x] **Scheduler running unattended on the host.** The in-process cycle-aligned refresh loop (`api/scheduler.run_scheduler`) starts with the app lifespan and refreshes registered active missions on each ensemble boundary (00/06/12/18Z); `/v1/health` exposes the current cycle, next cycle, cache size, and active-mission count.
- [x] **Redeploy-durable runtime data dir.** `UPSTREAMWX_DATA_DIR` lives at `/var/lib/upstreamwx`, outside the code tree, so redeploys never clear it — the on-disk **watershed trace cache** and **ensemble grid caches** persist across releases.
- [x] **Bounded live-fetch latency + honest failures.** GEFS/REFS downloads are latency-bounded and surface live-fetch errors rather than silently falling back to sample data (production no longer ships the sample-briefing fallback).
- [x] **Persistent cross-restart ensemble grid cache.** Cycle-keyed on-disk caches for GEFS (`gefs/cache.py`) and REFS (`refs/cache.py`) store the byte-range subsets under `UPSTREAMWX_DATA_DIR`, re-decoded with cfgrib on a hit (bit-identical to live decode), written atomically (temp + `os.replace`) so a failed download leaves no poisoned entry (NFR-6). Survives restart — hermetically tested. The shared core is hoisted into `grib/cache.py` (atomic byte-range subset fetch + retention prune + memory-aware decode LRU).
- [x] **Download-once-per-cycle + proactive warming + retention pruning.** The scheduler (and `deploy/deploy.sh`) pre-warm the GEFS `gefs_warm_fhours` band (default f24–f120 / 6 h — the horizon GEFS owns beyond REFS) via a parallel **download-only** `gefs.warm_cycle`, and warm/prune REFS runs (`refs_cache_keep_cycles`); pruning keeps cycles within the feed's retention window. Warm failures are swallowed (NFR-6) so refresh still serves from whatever is cached.
- [x] **GEFS decode-pool speedup.** Per-member GEFS decode runs in a spawn `ProcessPoolExecutor` owned by the API lifespan (`api_enable_decode_pool`), with the crop pushed into the worker (`gefs.cache._decode_cropped` over the union of watershed + LAoC bboxes) so only a ~KB array crosses the process boundary; broken-pool falls back in-process (NFR-6). CLI/tests keep the in-process path. Output is bit-identical (NFR-4).
- [x] **Watershed warming (latency follow-on).** Cold pour-point delineation (~3–15 s) was the dominant remaining briefing latency. The mission planner warms it the moment coordinates change via `POST /v1/watershed/warm` → `BriefingService.warm_watershed` (bounded background pool, `api_enable_warm`); a single-flight registry in `watershed/cache.py` coalesces a warm and the briefing that needs it onto one trace (atomic disk writes). Engine output unchanged.
- [ ] **REFS production-feed cutover gate.** REFS currently defaults to the **prototype AWS bucket** (`refs_source=aws`) — the only feed validated in-container. SCN 26-48 prod NOMADS (`com/refs/prod` ensprod NEP) requires an operator to set `UPSTREAMWX_REFS_SOURCE=nomads_prod` at/after the 2026-08-31 cutover. There is **no automatic cutover** — the deploy runbook must gate this. (See "Known gaps" under M0.5.)
- [ ] **Persistent rendered-briefing cache.** `api/cache.BriefingCache` is still an in-process `dict` — lost on every restart/redeploy. With ensemble grids persisted, a restart is cheap (regenerate from cached grids), but the rendered briefing objects and the active-mission registry are still ephemeral. The `get`/`put`-by-key interface is the seam.
- [ ] **Smoke-test the NLDI upstream-trace fallback** (`trace_upstream_nldi` / pour-point NLDI), flagged unexercised in the Spike B report — confirm it on the live host.
- [ ] **Cache observability.** Partial. The scheduler logs the warmed-field count per cycle; `/v1/health` reports only the in-process briefing cache size, not ensemble grid-cache state (cycles on disk, last warm, hit/miss). Extend it so the persistent caches are verifiable in production.

**Exit criteria.** Refresh runs unattended on the ensemble cycles *(met)*; a restart loses no cached ensemble cycle *(met — grids persist; rendered briefings still ephemeral)*; the on-demand path is unchanged in output *(met — cached decode is bit-identical)*.

**Build status.** The always-on backend is **deployed and live** (`deploy/` tooling: bootstrap + per-release deploy, systemd hardening, nginx/TLS, redeploy-durable data dir). The cycle-aligned scheduler runs on the host and both the watershed cache and the GEFS/REFS grid caches persist across redeploys. Remaining: the REFS prod-feed cutover gate, a persistent *rendered-briefing* cache, the NLDI-fallback smoke test, and `/v1/health` cache observability.

**Why split out.** Keeps M0.1's exit criterion (engine correct on the corpus) cleanly testable and met, while isolating the genuinely host-dependent scheduling/persistence work so it is not lost or forgotten.

---

## M0.2 — SITREP Output as `.md` via Terminal

**Objective.** A single terminal command turns a mission spec (point, date, window, cave/canyon) into a complete `.md` briefing.

**Recommended split:**
1. **Structured render first** — the engine's structured output rendered to `.md` deterministically (BLUF, phase breakdown, per-hazard postures/confidence/windows, drivers, sources, disclaimer). Golden-file testable: same inputs → byte-identical output.
2. **Haiku framing layer second** — add the natural-language framing (PRD FR-21), strictly constrained to narrate the structured object without changing any posture (FR-20). Isolated so the engine can be validated independent of LLM variability.

**Deliverables.**
- CLI: mission spec in → `.md` out, following the Appendix A skeleton.
- Phase inference (approach = first hour, egress = last hour; FR-9a).
- **Reference-only disclaimer embedded in the output from day one** (Appendix C).

**Exit criteria.** Structured render passes golden-file tests; framed output preserves all postures unchanged; every briefing carries the disclaimer and source links.

**Build status.** Delivered and golden-file tested (`docs/m0.2/README.md`). The framing layer is prepend-only (`sitrep/frame.py` splices a SUMMARY above `## BLUF`, never edits engine lines) and posture-preservation is asserted byte-identical in `tests/test_sitrep_frame.py`.

---

## M0.3 — API Functional, Passing Internal Validation

**Objective.** Wrap the engine + SITREP behind an API, with the caching and scheduling the PRD assumes.

**Deliverables.**
- Endpoint: mission spec → briefing (structured + framed).
- **Server-side generation and caching**, keyed by location/window so reopening costs nothing (PRD §7, §11).
- Scheduled regeneration aligned to ensemble/AFD cycles (FR-12).
- Graceful degradation when a non-mandatory source is down (NFR-6).

**Exit criteria.** API returns briefings identical in content to the CLI for the same inputs; cache hit/miss behaves correctly; scheduled refresh works; validation corpus passes through the API path.

**Build status.** Delivered and hermetically validated (`docs/m0.3/README.md`): the FastAPI service (`upstreamwx.api` — `POST /v1/briefing`, `POST /v1/briefing/pdf`, `POST /v1/briefing/frame` SSE, `POST /v1/watershed/warm`, `GET /v1/health`), the cycle-scoped server-side briefing cache (`upstreamwx.api.cache`, keyed by location/window), and the **00/06/12/18Z** cycle arithmetic plus the refresh pass (`upstreamwx.api.cycles` / `BriefingService.refresh_active`, FR-12). The CLI and API share one generation core (`upstreamwx.sitrep.generate`), so the API is identical to the CLI by construction (FR-13). The **always-on scheduler cadence and cross-restart cache persistence moved to M0.1.1** — an ephemeral dev container cannot validate unattended cadence or restart persistence; the host-independent core (endpoints, cache semantics, cycle math, single refresh pass) is built and tested here.

---

## M0.4 — PWA Framework: Map Location In → SITREP Out

**Objective.** Thinnest end-to-end PWA: pick a point, get a rendered SITREP.

**Deliverables.**
- PWA shell (installable, responsive; NFR-1).
- Map with free-form single-point placement (FR-1) and the **upstream watershed overlay** (FR-38) — leverages the M0.1 trace, already validated.
- Mission editor incl. cave/canyon selector (FR-33).
- Calls the M0.3 API; renders the Overview/BLUF.

**Exit criteria.** A user drops a point, the watershed traces and renders, and a correct SITREP appears. Decision-ownership and disclaimer present (FR-39, FR-40).

**Build status.** Delivered and verified live end-to-end in-container (`docs/m0.4/README.md`). Beyond the thin slice: the map-based **mission planner** (geocode/DMS/GPS, switchable basemaps, long-press marker), the **Radius of Concern** slider clipping the upstream basin before aggregation (`watershed/roc.py`, FR-3), the **Lightning Area of Concern** disk (app-wide pref), and the structured-JSON contract the PWA renders its views from (`sitrep/structured.py`; contract frozen at `frontend/data/sample-briefing.json`).

---

## M0.5 — Flesh Out the PWA → Public Beta (v0.5.0)

**Objective.** Full interface per PRD §6.8 and Appendix D, and cut the **v0.5.0 public beta** — the first publicly exposed release on the candidate-v1 codebase.

**Deliverables.**
- [x] Six-view IA: Overview, Map, Hazards, Briefing, Forecast, Resources (FR-32) — `frontend/js/app.js` `TABS`.
- [x] **Briefing tab** rendering the full Markdown SITREP as HTML (zero-dependency in-browser converter); Haiku-framing attribution banner when framed.
- [x] **Offline cache** of the latest briefing with timestamp indicator (FR-26, FR-41) — network-first shell + data with cache fallback; release-tied service-worker cache busting + non-dismissible "Update available" nudge.
- [x] **PDF export** (FR-27) — server-side `POST /v1/briefing/pdf` via headless Chromium (`sitrep/pdf.py`), running reference-only footer on every page; localStorage `?print=1` fallback when offline.
- [x] **Domain split** — app at `app.upstreamwx.com`, static landing at apex `upstreamwx.com` (`landing/`, mirrors the About view + install CTA + disclaimer).
- [~] **Phase-primary hazard timeline** with severity color (FR-35), confidence hatching + label (FR-36), persistent-vs-windowed display (FR-37) — built; remaining polish tracked below.
- [x] Resources: source/verify links, "how this is calculated" (FR-20), first-run acknowledgment (FR-31).

**Exit criteria for the public beta.** All §6.8 requirements met; offline review works; PDF export carries the disclaimer; suite green and `ruff` clean; version stamped; the known gaps below are either closed or explicitly accepted and disclosed to beta users.

**Build status.** Test suite green (278 passed, 17 network-deselected), `ruff` clean, version bumped to `0.5.0`. The PWA, landing page, and deploy tooling are release-ready. Of the known gaps below, the code/operational/test items (2, 3, 4, 5, 7) are now closed; the remaining ones are the provisional-threshold calibration/UI framing (1), the persistent rendered-briefing cache (6), cache observability (8), the FR-40 disclaimer-persistence judgment (9), and cosmetic SREF/HREF wording (10).

### Known gaps to close or accept before/at the v0.5.0 beta

Surfaced by the v0.5.0 codebase review (see "Critical functions still missing" in the review notes):

1. **Threshold calibration is provisional (product risk, not a code defect).** GEFS/REFS cut points are seeded from the SREF/HREF baseline and uncalibrated against the new ensembles; `gefs_p_tstm` is a CAPE×precip proxy. Acceptable for a *reference-only* beta only if the uncertainty is loud in the UI. Flag explicitly to beta users (FR-20a — tuned via config, no code change).
2. ✅ **REFS prod-feed cutover (operational).** Default is the prototype AWS bucket; no auto-cutover to NOMADS prod at the 2026-08-31 EOL. **Gated:** `deploy.sh` now warns loudly at/after the cutover if still on `aws`, and `deploy/README.md` documents the one-time `UPSTREAMWX_REFS_SOURCE=nomads_prod` flip. (The flip itself remains a deliberate operator action.)
3. ✅ **NFR-6 hole in lightning AFD lookup.** Was a direct `cfg["afd_storm_mode"][mode]` index (KeyError on an out-of-vocab mode). Now a normalized `.get()`; regression-tested (`test_lightning_afd_storm_mode_degrades_gracefully`).
4. ✅ **`refs/extract.load_probability_field` (non-cached) feed selection.** Now resolves the active feed via `refs_feed(settings)` and threads `base`/`subdir` through the URLs, matching the cached loader (and honoring a caller-passed `settings`).
5. ✅ **Unbounded in-memory growth** in `api/service.py` / `api/cache.py`. Both the briefing cache and the engine-result store are now LRU-bounded (`BoundedLRU`, capped by `api_cache_max_entries`, default 512). (`_active` self-prunes when windows end.)
6. **Persistent rendered-briefing cache + active-mission registry** — still ephemeral; a restart drops scheduled refresh until each mission is re-requested (M0.1.1).
7. ✅ **Test coverage gaps.** Added `tests/test_pdf_export.py` (template/asset presence, endpoint wiring + 503 fallback, and a real headless-Chromium render skipped where Chromium is absent) and a network-gated live REFS warm/decode smoke (`test_refs_warm_live`).
8. **Cache observability** — extend `/v1/health` to surface ensemble grid-cache state (M0.1.1).
9. **FR-40 disclaimer persistence (PRD judgment).** The reference-only disclaimer is repeated per-view (present on every screen) but is not a single always-on chrome element; it scrolls with content. Confirm this meets the PRD's "persistent/non-dismissible" bar or add fixed chrome.
10. **Stale SREF/HREF wording in live code/docs.** Cosmetic only: `sref_`/`href_` local-variable names in `flash_flood.py`/`lightning.py`, the `frame.py` system-prompt signal guide, `cycles.py`/`cli.py` docstrings, several `grib/`/`watershed/` docstrings, the nginx config comment, and the `docs/m0.1`–`m0.4` milestone READMEs (which still describe SREF/HREF as the live spine). Sweep during the post-cutover cleanup that deletes `sref/`/`href/`.

---

## Cross-cutting workstreams (span multiple milestones)

- **Validation corpus** — created in M0.1, extended at every milestone; the regression backbone.
- **Threshold config** — established M0.1; Appendix B as accepted initial values, re-seeded for GEFS/REFS, tuned through field testing, each change versioned with provenance, no code change (PRD FR-20a).
- **Reference-only posture** — disclaimer and verify-links present from M0.2 onward, never retrofitted.
- **Provider abstraction** — ingest behind interfaces from M0.1 so Open-Meteo (or a future paid provider) and the SREF→GEFS / HREF→REFS swap land without touching the engine (PRD §12).

## Critical path and long poles

1. **GEFS processor** (M0.0 spike A/F → M0.1 on-demand module → M0.1.1 scheduled job) — heaviest backend component; on the critical path for both flood and lightning. On-demand processing built/validated in M0.1; recurring scheduling + persistent cache delivered in M0.1.1 (EC2). The **REFS same-day supplement** (Spike C/E → M0.1) is *not* a separate long pole: it reuses the retrieval/aggregation code (shared `grib` module), so its incremental cost is the conditional per-hour fetch loop, REFS field selection, and the lead-time-based ensemble selection in the engine.
2. **Upstream watershed trace** (M0.0 spike B → M0.1 module) — prerequisite for the entire flood model and the M0.4 map overlay.
3. **Threshold tuning** (field testing, post-build) — Appendix B values re-seeded for GEFS/REFS are the accepted starting point; field testing refines them via config (FR-20a). Not on the build critical path, but the **most important post-beta workstream** given the seeded cut points.

## Outstanding work (current focus: cut v0.5.0 public beta)

The MVP backend is live; the suite is green and version-stamped. Remaining work, in priority order:

**Pre-beta (close or explicitly accept — see M0.5 "Known gaps"):**
1. ✅ NFR-6 fix in `lightning.py` AFD lookup — normalized `.get()`, regression-tested.
2. ✅ REFS prod-feed cutover gating — deploy-time gate in `deploy.sh` + runbook step (operator still flips the env at cutover).
3. ✅ Bound the in-memory caches in `api/service.py` / `api/cache.py` — LRU-bounded (`api_cache_max_entries`).
4. ✅ PDF-export tests (hermetic wiring + real-Chromium render) + network-gated REFS provider smoke.
5. Loud uncertainty framing in the UI for the provisional thresholds (product — still open).
6. Decide FR-40 disclaimer persistence (PRD judgment — still open).

**Post-beta (M0.1.1 finish + calibration):**
- Persistent rendered-briefing cache + active-mission registry.
- `/v1/health` cache observability; NLDI fallback smoke test on the host.
- **Threshold calibration against GEFS/REFS member statistics** (the headline post-beta workstream).
- Remaining timeline polish (FR-35 color sign-off, FR-36 hatching, FR-37).
- Post-cutover cleanup: delete `sref/`/`href/` and sweep stale SREF/HREF wording.

## Open dependencies (from PRD, still needed)

- Appendix B threshold matrices — **accepted as initial config**, re-seeded for GEFS/REFS (tuned via field testing; FR-20a). No longer a build blocker, but calibration is the key post-beta task.
- Appendix C disclaimer copy — accepted; shipping from M0.2.
- FR-35 severity → color mapping — your review (proposal: Minimal green / Elevated amber / High orange / Extreme red).

## Summary of recommendations vs the proposed ordering

- Ordering endorsed as-is (engine → CLI → API → minimal PWA → full PWA).
- Added: **M0.0** de-risk milestone (later extended with spikes E/F for the SREF/HREF → GEFS/REFS EOL transition).
- Added: **threshold config externalization** in M0.1 (unblocks engine work now).
- Added: **validation corpus** as the defined oracle for "internal validation."
- Added: **structured-render before Haiku-framing** split in M0.2.
- Added: **disclaimer from the first artifact** (M0.2).
- Added: **v0.5.0 public beta** cut at the end of M0.5, with a documented known-gaps list.
- Clarified: build-milestone vs product-release vs document-version numbering.
</content>
</invoke>
