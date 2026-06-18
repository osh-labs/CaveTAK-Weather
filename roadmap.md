---
project: UpstreamWX
type: roadmap
status: draft
version: 0.3
date: 2026-06-16
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

**Exit criteria.** Both spikes run on sample inputs and produce plausible output; SREF data-availability risk is resolved yes/no.

**Why first.** Everything in M0.1 and beyond depends on these two. They are the long poles; failure here changes the architecture. Cheap to spike, expensive to discover late.

---

## M0.1 — Data Ingest, Decision Engine, Watershed Component

**Objective.** The three core subsystems built to a tested state, passing internal validation.

**Recommended internal sequence** (these are not equal-weight or independent):
1. **Watershed component** — promote Spike B to a real module: HUC-12 resolution + upstream trace + cache. Prerequisite for the flood path of both ingest and engine.
2. **Data ingest** — NWS API (AFD, alerts), Open-Meteo (derived fields), the **scheduled SREF processor** (heaviest backend item, PRD §11.2), SPC convective outlook. Each source behind a stable internal interface so providers can be swapped (PRD §12).
3. **Decision engine** — the deterministic rule engine: four hazards, phase × activity applicability matrix (FR-14a), thermal weighting (FR-14b), lightning/cave gating (FR-14c), per-hazard confidence from SREF spread (FR-17), overall posture as max across applicable hazards (FR-19).

**Cross-cutting deliverables (start here, used by every later milestone):**
- **Threshold config** — Appendix B matrices as versioned config files with provenance (date, rationale, source), loaded by the engine at runtime. Engine logic references config, never hard-coded numbers (PRD FR-20a). Appendix B values are the accepted initial set.
- **Validation corpus** — the oracle for "passing internal validation":
  - *Boundary cases (backbone):* hand-constructed inputs that sit just inside/outside each tier edge, per hazard, fully deterministic and controllable. These are the bulk of the test suite.
  - *Historical replay (realism check):* a small set of documented events (known flash floods, convective days, clear days) replayed from retrievable archived source data, expected to flag the right tier.

**Exit criteria.** Engine produces the expected hazard postures, confidence, and windows across the entire validation corpus; threshold changes are config-only; SREF job runs on schedule and caches.

**Dependency / gating input.** None blocking. Appendix B values are accepted as the initial configured set (Chris, M0.0 planning), to be tuned through field testing rather than a pre-build redline. Because thresholds are config (FR-20a), tuning never requires a code change.

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

1. **SREF processor** (M0.0 spike → M0.1 module) — heaviest backend component; on the critical path for both flood and lightning.
2. **Upstream watershed trace** (M0.0 spike → M0.1 module) — prerequisite for the entire flood model and the M0.4 map overlay.
3. **Threshold tuning** (field testing, post-build) — Appendix B values are the accepted starting point; field testing refines them via config (FR-20a). Not on the build critical path.

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
