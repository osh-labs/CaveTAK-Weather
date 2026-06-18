# M0.0 — Foundation & De-Risk Spikes: Findings

**Date:** 2026-06-18 · **Branch:** `claude/relaxed-knuth-yhbz95`

M0.0 stands up the repo and proves the two riskiest feasibility questions before
committing to M0.1 architecture (roadmap §M0.0). **Both are resolved YES.**

## Exit-criteria status

| Exit criterion (roadmap §M0.0) | Status |
| --- | --- |
| Repo scaffolding, test harness, secrets handling | ✅ `pyproject.toml` (uv/hatchling, `src/` layout), `ruff`, `pytest`, `.env.example`, `.gitignore` |
| **Spike A — SREF over a polygon** runs on sample input, plausible output | ✅ [spike-a-report.md](spike-a-report.md) |
| **SREF data-availability risk resolved (yes/no)** | ✅ **YES** — NOMADS `ensprod`, source/cadence/retention pinned |
| **Spike B — upstream HUC-12 trace** runs on sample input, plausible output | ✅ [spike-b-report.md](spike-b-report.md) |
| Backend SREF resource profile for EC2 sizing | ✅ [resource-profile.md](resource-profile.md) — fits existing EC2 |
| **Spike C — HREF same-day high-res supplement** (additive de-risk) | ✅ [spike-c-report.md](spike-c-report.md) — HREF resolved **YES** on NOMADS, same idx pattern |
| **Spike D — StreamStats API probe** (exploratory; Effective-QPF de-risk) | ◐ [spike-d-streamstats-report.md](spike-d-streamstats-report.md) — delineation/characteristics **YES**; CN/travel-time coverage **uneven by state** (see report) |
| Offline fixtures committed; tests pass with no network; lint clean | ✅ `pytest` 14 passed / 3 network deselected; `ruff` clean |
| CI | ⏸ Deferred by request; structure (ruff + offline pytest) keeps it a trivial add |

## Architecture implications for M0.1

- **Watershed (Spike B → module):** the deterministic WBD `tohuc` graph walk over
  `pynhd.WaterData` works and is reproducible. M0.1 should add a local WBD
  GeoPackage cache (large-river HU6 widening is the slow path) and promote the trace
  to a cached module.
- **SREF (Spike A → scheduled job):** use NOMADS `ensprod` probability/spread
  products with `.idx` byte-range subsetting. Download the needed CONUS field set
  **once per cycle**, then aggregate every active domain from the cached grid — so
  recurring cost scales with cycles/day, not domains. Fits the existing UpstreamWX EC2.
- **HREF (Spike C → same-day supplement):** the ~3 km convection-allowing ensemble
  is reachable on the **same NOMADS `ensprod` + `.idx`** pattern; its neighborhood
  probabilities (`APCP` for flood, `REFC`/`LTNG` for lightning) sharpen the same-day
  (≲36 h) window while SREF keeps the longer horizon. The idx + polygon-aggregation
  code is now shared in `src/upstreamwx/grib/`; HREF is fetched **conditionally** and
  only for the mission's exposure-hours (per-hour files). Engine combines the two
  "show both, higher tier wins" (FR-19) with HREF-specific config thresholds (FR-20a).
- **Coarse grid caveat:** the ~16 km SREF grid undersamples small headwater HUC-12s;
  the 3 km HREF grid largely resolves this (≈50 vs ≈5 cells over Buckskin), and
  coverage-weighted aggregation (`exactextract`) remains the SREF-side v1.x refinement.
  The nearest-cell fallback is in place, flagged, and now longitude-convention safe.
- **No threshold→tier logic yet** (intentional): that is the M0.1 deterministic rule
  engine, driven by versioned config (FR-20a). The spikes only prove data feasibility.

## Reproduce

See the repo `README.md` Quickstart, or each report's "Reproduce" section.
