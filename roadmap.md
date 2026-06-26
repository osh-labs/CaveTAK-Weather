---
project: UpstreamWX
type: roadmap
status: draft
version: 0.4
date: 2026-06-25
host: upstreamwx.com
owner: Chris Lee
related: UpstreamWX PRD v0.8
---

# UpstreamWX — Development Roadmap (Draft)

## Purpose

Sequence the build from core logic to a fleshed-out PWA. Milestones below follow the ordering proposed by Chris, with additive de-risking recommendations folded in (M0.0, threshold config, validation corpus, structured-vs-framing split, early disclaimer). The underlying engineering sequence is unchanged: deterministic engine first, then CLI, then API, then minimal PWA, then full PWA.

## Note on versioning (three axes, do not conflate)

| Axis | Example labels | Meaning |
|---|---|---|
| **Build milestone** | M0.0 … M0.5 (this doc) | Engineering stages of the initial build |
| **Product release** | v1, v1.x, v2 (PRD §5) | Shippable product scope tiers |
| **Document version** | PRD v0.8, this roadmap v0.3 | Revision of a written artifact |

Build milestones M0.0–M0.5 lead up to **product v1**. The "M" prefix is deliberate: it keeps build milestones from colliding with product releases (v1) and document versions (v0.x) in conversation and commits.

## Guiding principles

1. **Deterministic core is the product's spine.** The rule engine owns every hazard posture; the language model only frames (PRD FR-13, FR-20). Build and validate the engine in isolation before anything renders it.
2. **Vertical testability at each milestone.** Every milestone ends with something runnable and a defined pass/fail, not just code.
3. **De-risk the two hard unknowns first** (SREF-over-polygon, upstream HUC-12 trace) — see M0.0.
4. **Thresholds are data, not code.** All hazard thresholds live in versioned config, never hard-coded (PRD FR-20a). Appendix B values are the accepted initial config and are tuned through field testing, not a pre-build redline — so engine logic can be built and validated immediately, and tuned later without code changes.
5. **Reference-only posture from the first artifact.** The disclaimer (PRD Appendix C) ships with the earliest user-facing output, not later.

## Milestone overview

| Milestone | Objective | Primary exit criterion |
|---|---|---|
| **M0.0** | Foundation + de-risk spikes (recommended) | Both hard-unknown spikes demonstrably work on sample data |
| **M0.1** | Data ingest, decision engine, watershed component built and validated | Engine produces correct hazard postures on the validation corpus |
| **M0.1.1** | SREF scheduler + persistent cache (EC2-hosted) — *in progress; MVP live* | Recurring SREF/AFD refresh runs on schedule and caches across restarts |
| **M0.2** | SITREP output as `.md` via terminal command | One command turns a mission spec into a complete, disclaimer-bearing `.md` briefing |
| **M0.3** | API functional, passing internal validation | API returns the same briefing the CLI does, with caching/scheduling |
| **M0.4** | PWA framework: map location in → SITREP out | A user picks a point on a map and sees a rendered SITREP |
| **M0.5** | Flesh out PWA | Full IA (PRD §6.8 / Appendix D), offline cache, PDF export |

---

## M0.0 — Foundation and De-Risk Spikes (recommended addition)

**Objective.** Stand up the repo and prove the two riskiest feasibility questions before committing to M0.1 architecture.

**Deliverables.**
- Repo scaffolding, CI, test harness, secrets handling.
- **Spike A — SREF over a polygon:** retrieve native SREF GRIB2, extract the ensemble probability fields (P(precip), P(thunder), member spread), and aggregate them over a sample watershed polygon. Confirm data availability and retention for the run cadence.
- **Spike B — upstream HUC-12 trace:** given an arbitrary CONUS lat/lon, resolve the containing HUC-12 and trace the upstream contributing watershed from the hosted USGS WBD (PRD FR-2, FR-3).
- Backend decided: **small always-on service on the existing UpstreamWX EC2** (scalable). The M0.0 spike no longer chooses the architecture — it characterizes the SREF job's resource profile (memory, runtime, cadence) to size EC2 headroom. One-time/batch pre-processing runs on a dev machine; recurring SREF/AFD refresh runs on the EC2 scheduler (PRD §7).
- **Spike C — HREF same-day high-res supplement (additive de-risk):** confirm the ~3 km HREF convection-allowing ensemble is retrievable on the same NOMADS `ensprod` + `.idx` pattern, extract its neighborhood probability fields (`APCP` for flood; `LTNG`/`REFC` for lightning) over a watershed polygon, and profile its (heavier) per-cycle resource cost. **Resolved YES** (PRD §6.2 FR-7a, `docs/m0.0/spike-c-report.md`). The idx + aggregation code is now shared in `src/upstreamwx/grib/`, reused by both SREF and HREF.

**Exit criteria.** Both spikes run on sample inputs and produce plausible output; SREF data-availability risk is resolved yes/no.

**Why first.** Everything in M0.1 and beyond depends on these two. They are the long poles; failure here changes the architecture. Cheap to spike, expensive to discover late.

---

## M0.1 — Data Ingest, Decision Engine, Watershed Component

**Objective.** The three core subsystems built to a tested state, passing internal validation.

**Recommended internal sequence** (these are not equal-weight or independent):
1. **Watershed component** — promote Spike B to a real module: HUC-12 resolution + upstream trace + cache. Prerequisite for the flood path of both ingest and engine.
2. **Data ingest** — NWS API (AFD, alerts), Open-Meteo (derived fields), the **scheduled SREF processor** (heaviest backend item, PRD §11.2), the **conditional HREF same-day supplement** (PRD §6.2 FR-7a — reuses the SREF retrieval/aggregation code via the shared `grib` module; fetched only for in-range missions and only the needed forecast hours), SPC convective outlook. Each source behind a stable internal interface so providers can be swapped (PRD §12). The engine selects ensemble signals by lead time — HREF inside the same-day window (≈6–36 h), SREF beyond — and where both are in range takes the higher tier (FR-19).
3. **Decision engine** — the deterministic rule engine: four hazards, phase × activity applicability matrix (FR-14a), thermal weighting (FR-14b), lightning/cave gating (FR-14c), per-hazard confidence from SREF spread (FR-17), overall posture as max across applicable hazards (FR-19).

**Cross-cutting deliverables (start here, used by every later milestone):**
- **Threshold config** — Appendix B matrices as versioned config files with provenance (date, rationale, source), loaded by the engine at runtime. Engine logic references config, never hard-coded numbers (PRD FR-20a). Appendix B values are the accepted initial set.
- **Validation corpus** — the oracle for "passing internal validation":
  - *Boundary cases (backbone):* hand-constructed inputs that sit just inside/outside each tier edge, per hazard, fully deterministic and controllable. These are the bulk of the test suite.
  - *Historical replay (realism check):* a small set of documented events (known flash floods, convective days, clear days) replayed from retrievable archived source data, expected to flag the right tier.

**Exit criteria.** Engine produces the expected hazard postures, confidence, and windows across the entire validation corpus; threshold changes are config-only; SREF job runs on schedule and caches.

**Dependency / gating input.** None blocking. Appendix B values are accepted as the initial configured set (Chris, M0.0 planning), to be tuned through field testing rather than a pre-build redline. Because thresholds are config (FR-20a), tuning never requires a code change.

**Build status (this pass).** Delivered and hermetically validated: the deterministic engine (`upstreamwx.engine` — four hazards, FR-14a/b/c, FR-17 confidence, FR-19 max), the externalized YAML threshold config (`upstreamwx/data/thresholds/`, FR-20a) with provenance, and the validation corpus (`tests/corpus/`, the exit-criterion oracle). Delivered and live-tested against real services: the watershed promotion with on-disk caching (`upstreamwx.watershed.resolve_and_trace_cached`) and the ingest provider abstraction with live adapters (NWS, Open-Meteo, SPC, SREF; `upstreamwx.ingest`). The **recurring SREF scheduler and persistent cross-restart cache moved to M0.1.1** — an ephemeral dev container cannot validate cadence or cache persistence; that work belongs on the always-on EC2. The on-demand SREF *processing logic* those will invoke is built and tested here.

---

## M0.1.1 — SREF Scheduler & Persistent Cache (EC2-hosted)

**Objective.** Promote the on-demand SREF/watershed processing built in M0.1 to the recurring, always-on backend the PRD assumes (PRD §7, §11.2, FR-12). Hosted on the existing UpstreamWX EC2 (roadmap §M0.0 backend decision), because cadence and cross-restart persistence cannot be exercised in the ephemeral dev environment used for M0.1.

**Deliverables (carried over from M0.1).**
- [x] **Always-on host stood up.** The FastAPI service is deployed on the EC2 instance behind nginx/TLS as a single systemd-managed uvicorn process (`deploy/`), serving the PWA single-origin at **upstreamwx.com**. This is the MVP now live.
- [x] **Scheduler running unattended on the host.** The in-process cycle-aligned refresh loop (`api/scheduler.run_scheduler`) starts with the app lifespan and refreshes registered active missions on each SREF boundary (03/09/15/21Z); `/v1/health` exposes the current cycle, next cycle, cache size, and active-mission count.
- [x] **Redeploy-durable runtime data dir.** `UPSTREAMWX_DATA_DIR` lives at `/var/lib/upstreamwx`, outside the code tree, so redeploys never clear it — the on-disk **watershed trace cache** now persists across releases (important given NOMADS's ~2-day SREF retention).
- [x] **Bounded live-fetch latency + honest failures.** SREF/HREF downloads are latency-bounded and surface live-fetch errors rather than silently falling back to sample data (production no longer ships the sample-briefing fallback).
- [~] **Persistent cross-restart SREF-grid cache.** **In PR #39 (open, not yet merged).** A new cycle-keyed on-disk cache (`sref/cache.py`, `load_probability_field_cached`) stores the byte-range SREF subsets under `UPSTREAMWX_DATA_DIR/sref`, re-decoded with cfgrib on a hit (bit-identical to the live decode), written atomically (temp + `os.replace`) so a failed download leaves no poisoned entry (NFR-6). Survives restart — hermetically tested (`tests/test_sref_cache.py`). *Note: this caches the SREF grids, not the rendered briefings — see the still-in-process `BriefingCache` below.*
- [~] **Download-once-per-cycle SREF processor.** **In PR #39 (open).** `ingest/sref_provider._domain_max` now reads through that cache, so a cycle's CONUS subset is downloaded once and every domain aggregates from the cached grid (the M0.0 resource-profile pattern). Output unchanged; provider tests re-pointed at the cached loader.
- [~] **Proactive cycle pull + retention pruning.** **In PR #39 (open).** `BriefingService.warm_and_prune` pre-pulls the live cycle's field set (`warm_cycle`) and prunes cycles beyond `sref_cache_keep_cycles` (default 4 ≈ NOMADS's 2-day window); the scheduler runs it each boundary *before* `refresh_active`, swallowing warm failures (NFR-6) so refresh still serves from whatever is cached.
- [ ] **Persistent rendered-briefing cache.** Still outstanding. `api/cache.BriefingCache` is an in-process `dict` — lost on every restart/redeploy. With the SREF grids now persisted (PR #39), a restart is cheap (regenerate from cached grids), but the rendered briefing objects themselves are still ephemeral.
- [ ] **Persistent HREF subset cache.** Still outstanding. PR #39 covers SREF only; the HREF same-day path (`href/extract.py`) still downloads into a throwaway tempdir. Same `sref/cache.py` pattern should be applied to HREF (or generalized in the shared `grib/` module).
- [ ] **Smoke-test the NLDI upstream-trace fallback** (`trace_upstream_nldi` / pour-point NLDI), flagged unexercised in the Spike B report — confirm it on the live host.
- [ ] **Cache observability.** Partial. The scheduler logs the warmed-field count per cycle; `/v1/health` still reports only the in-process briefing cache size, not SREF grid-cache state (cycles on disk, last warm). Extend it so the persistent cache can be verified in production.

**Exit criteria.** Refresh runs unattended on the SREF/AFD cycles *(met)*; a restart loses no cached cycle *(SREF grids: met in PR #39 once merged; rendered briefings: not yet)*; the on-demand path is unchanged in output *(met — cached decode is bit-identical)*.

**Build status (this pass).** The always-on backend is **deployed and live at upstreamwx.com** (`deploy/` tooling: bootstrap + per-release deploy, systemd hardening, nginx/TLS, redeploy-durable data dir). The cycle-aligned scheduler runs on the host and the watershed cache persists across redeploys — so the *scheduling cadence* half of M0.1.1 is satisfied in production. The **server-side SREF *caching*** half is now largely built in **PR #39 (open)**: a persistent, cross-restart, cycle-keyed SREF-grid cache with download-once-per-cycle warming and retention pruning. Remaining after #39 merges: a persistent *rendered-briefing* cache, the equivalent HREF subset cache, the NLDI-fallback smoke test, and `/v1/health` cache observability.

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

---

## M0.3 — API Functional, Passing Internal Validation

**Objective.** Wrap the engine + SITREP behind an API, with the caching and scheduling the PRD assumes.

**Deliverables.**
- Endpoint: mission spec → briefing (structured + framed).
- **Server-side generation and caching**, keyed by location/window so reopening costs nothing (PRD §7, §11).
- Scheduled regeneration aligned to SREF/AFD cycles (FR-12).
- Graceful degradation when a non-mandatory source is down (NFR-6).

**Exit criteria.** API returns briefings identical in content to the CLI for the same inputs; cache hit/miss behaves correctly; scheduled refresh works; validation corpus passes through the API path.

**Build status (this pass).** Delivered and hermetically validated (`docs/m0.3/README.md`): the FastAPI service (`upstreamwx.api` — `POST /v1/briefing`, `GET /v1/health`), the cycle-scoped server-side briefing cache (`upstreamwx.api.cache`, keyed by location/window), and the SREF/AFD cycle arithmetic plus the refresh pass (`upstreamwx.api.cycles` / `BriefingService.refresh_active`, FR-12). The CLI and API now share one generation core (`upstreamwx.sitrep.generate`), so the API is identical to the CLI by construction (FR-13). The **always-on scheduler cadence and cross-restart cache persistence moved to M0.1.1** for the same reason the SREF scheduler did — an ephemeral dev container cannot validate unattended cadence or restart persistence; the host-independent core (endpoint, cache semantics, cycle math, single refresh pass) is built and tested here.

---

## M0.4 — PWA Framework: Map Location In → SITREP Out

**Objective.** Thinnest end-to-end PWA: pick a point, get a rendered SITREP.

**Deliverables.**
- PWA shell (installable, responsive; NFR-1).
- Map with free-form single-point placement (FR-1) and the **upstream watershed overlay** (FR-38) — leverages the M0.1 trace, already validated.
- Mission editor incl. cave/canyon selector (FR-33).
- Calls the M0.3 API; renders the Overview/BLUF.

**Exit criteria.** A user drops a point, the watershed traces and renders, and a correct SITREP appears. Decision-ownership and disclaimer present (FR-39, FR-40).

**Note.** Most risk here is UI wiring; the hard backend pieces are already validated upstream.

---

## M0.5 — Flesh Out the PWA

**Objective.** Full interface per PRD §6.8 and Appendix D.

**Deliverables.**
- Five-view IA: Overview, Forecast, Map, Hazards, Resources (FR-32).
- **Phase-primary hazard timeline** with severity color (FR-35), confidence hatching + label (FR-36), persistent-vs-windowed display (FR-37).
- Forecast detail views and charts.
- Resources: source/verify links, "how this is calculated" (FR-20), first-run acknowledgment (FR-31).
- **Offline cache** of the latest briefing with timestamp indicator (FR-26, FR-41).
- **PDF export** (FR-27).

**Exit criteria.** All §6.8 requirements met; offline review works; PDF export carries the disclaimer; this is the candidate **product v1**.

---

## Cross-cutting workstreams (span multiple milestones)

- **Validation corpus** — created in M0.1, extended at every milestone; the regression backbone.
- **Threshold config** — established M0.1; Appendix B as accepted initial values, tuned through field testing, each change versioned with provenance, no code change (PRD FR-20a).
- **Reference-only posture** — disclaimer and verify-links present from M0.2 onward, never retrofitted.
- **Provider abstraction** — ingest behind interfaces from M0.1 so Open-Meteo (or a future paid provider) can be swapped (PRD §12).

## Critical path and long poles

1. **SREF processor** (M0.0 spike → M0.1 on-demand module → M0.1.1 scheduled job) — heaviest backend component; on the critical path for both flood and lightning. On-demand processing built/validated in M0.1; recurring scheduling + persistent cache deferred to M0.1.1 (EC2). The **HREF same-day supplement** (Spike C → M0.1) is *not* a separate long pole: it reuses the SREF retrieval/aggregation code (shared `grib` module), so its incremental cost is the conditional per-hour fetch loop, HREF field selection, and the lead-time-based ensemble selection in the engine.
2. **Upstream watershed trace** (M0.0 spike → M0.1 module) — prerequisite for the entire flood model and the M0.4 map overlay.
3. **Threshold tuning** (field testing, post-build) — Appendix B values are the accepted starting point; field testing refines them via config (FR-20a). Not on the build critical path.

## Outstanding work (current focus: M0.1.1 SREF server caching)

The MVP backend is live at upstreamwx.com; the remaining build work, in priority order:

**M0.1.1 — finish the SREF server cache (active):**

*In flight — **PR #39** (`claude/sref-caching-remote-server`, open, not yet merged):*
- ✅ Persistent cross-restart **SREF-grid** cache, keyed by cycle, atomic writes (`sref/cache.py`).
- ✅ Download-once-per-cycle processor: the provider reads through the cache; one CONUS pull serves every domain.
- ✅ Proactive cycle warming + retention pruning each scheduler boundary (`warm_and_prune`, `sref_cache_keep_cycles`).
- ✅ Hermetic tests (hit/miss, atomic-on-error, cross-restart, warm, prune, scheduler cadence) + one `network` live-warm test.
- **Next step:** review/merge #39, then validate the warm/prune cadence on the live EC2 host (the one thing the hermetic suite can't exercise).

*Still outstanding after #39 merges:*
1. **Persistent rendered-briefing cache.** `api/cache.BriefingCache` is still in-process. Lower priority now that the grids persist (restart → cheap regenerate), but back it on disk if we want zero-work warm starts. The `get`/`put`-by-key interface is already the seam.
2. **Persistent HREF subset cache.** #39 is SREF-only; apply the same cache to the HREF same-day path (`href/extract.py`), ideally generalized into the shared `grib/` module so SREF and HREF share one implementation.
3. **Smoke-test the NLDI upstream-trace fallback** on the live host (`trace_upstream_nldi` / pour-point NLDI), still flagged unexercised since Spike B.
4. **Cache observability.** Extend `/v1/health` (and/or logs) to surface SREF grid-cache state — cycles on disk, last warm, hit/miss — so the persistent cache is verifiable in production.

**M0.5 — flesh out the PWA (next milestone, not yet started):**
- Offline cache of the latest briefing with timestamp indicator (FR-26, FR-41).
- PDF export carrying the disclaimer (FR-27).
- Remaining timeline polish: phase-primary hazard timeline severity color (FR-35, pending the color-mapping sign-off below), confidence hatching + label (FR-36), persistent-vs-windowed display (FR-37).
- Forecast detail views/charts and the Resources "how this is calculated" + first-run acknowledgment (FR-20, FR-31).

## Open dependencies (from PRD, still needed)

- Appendix B threshold matrices — **accepted as initial config** (tuned via field testing; FR-20a). No longer a build blocker.
- Appendix C disclaimer copy — your review (used from M0.2).
- FR-35 severity → color mapping — your review (used from M0.5; proposal: Minimal green / Elevated amber / High orange / Extreme red).

## Summary of recommendations vs the proposed ordering

- Ordering endorsed as-is (engine → CLI → API → minimal PWA → full PWA).
- Added: **M0.0** de-risk milestone.
- Added: **threshold config externalization** in M0.1 (unblocks engine work now).
- Added: **validation corpus** as the defined oracle for "internal validation."
- Added: **structured-render before Haiku-framing** split in M0.2.
- Added: **disclaimer from the first artifact** (M0.2).
- Clarified: build-milestone vs product-release vs document-version numbering.
