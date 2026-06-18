# Spike C — HREF over a Polygon (same-day high-res supplement)

**Date:** 2026-06-18
**Code:** `src/upstreamwx/href/` (+ shared `src/upstreamwx/grib/`) · CLI `spikes/spike_c_href/run_spike_c.py`

## Feasibility verdict: **YES**

The High-Resolution Ensemble Forecast (HREF) — NCEP's ~3 km convection-allowing
ensemble — is retrievable on the **same NOMADS `ensprod` + `.idx` byte-range
pattern as SREF**, its neighborhood-probability fields cover exactly the
flash-flood and lightning signals UpstreamWX needs, and they aggregate cleanly
over an upstream-watershed polygon at a resolution that resolves a headwater
HUC-12 with real cells instead of a coarse-grid fallback. **The HREF
data-availability risk is resolved: YES.**

HREF is added as a **supplement, not a replacement**: it sharpens the same-day
(≲36 h) window; SREF (~16 km) still owns the longer planning horizon to 87 h
(PRD §6.2 FR-7a, §16.1/§16.2; roadmap M0.1).

## Data source (resolved)

| Question | Finding |
| --- | --- |
| Authoritative source | **NOMADS** — `https://nomads.ncep.noaa.gov/pub/data/nccf/com/href/prod/` (same host as SREF) |
| Layout | `href.YYYYMMDD/ensprod/href.tHHz.<domain>.<product>.fHH.grib2` (+ `.idx`) |
| Cadence | **2 cycles/day at 00/12 UTC** (vs SREF's four), 11 members (idx tag `0/10`) |
| Horizon | **f01–f48**, 1-hourly; UpstreamWX caps use at ~36 h per product intent |
| File granularity | **One file per forecast hour** (unlike SREF's single multi-window file) |
| Products | `prob` (Neighborhood Ensemble Probability — used), plus `mean`, `pmmn`, `sprd`, `avrg`, `lpmm`, `eas` |
| Grid | `conus` ~3 km Lambert, **1025×1473** (2D lat/lon); separate AK/HI/PR domains exist |
| Longitude convention | **0–360°E** (vs SREF's −180..180) — handled in aggregation (see below) |
| Production lag | ~6–7 h after init; `latest_available_cycle()` probes recent cycles newest-first |
| Retention | Short (~2 days) → production must pull each cycle promptly, only the needed fhours |

## Fields extracted (mapped to Appendix B)

The `prob` product is a true **Neighborhood Ensemble Probability (NEP)** field —
the correct way to read 3 km probabilities (a raw grid-point probability is
near-zero even for a well-forecast storm). It carries exactly what the two
HREF-relevant hazards need:

- **Flash flood (§16.1):** `APCP:surface` NEP at mm thresholds (**12.7 ≈ 0.5 in**,
  25.4, 50.8, 76.2, 127) over **1 h / 3 h / 6 h / run-total** accumulation windows.
  The 1-hour bucket maps directly to the slot-canyon fallback (≥0.5 in/hr).
- **Lightning / convection (§16.2):** `REFC` NEP (>10..50 dBZ composite reflectivity)
  as a convective-mode proxy, **plus an explicit `LTNG` NEP** (P(lightning)) — a
  cleaner same-day lightning signal than SREF's `P(tstm)`. `CAPE` NEP bands
  (>500..>3000 J/kg) modulate confidence/severity.
- **Confidence (§16.5):** the `sprd` product gives ensemble spread; SREF↔HREF
  agreement is itself a cross-source confidence cue (FR-17).

> Note vs SREF: HREF APCP NEP does **not** carry the `>6.35 mm` (0.25 in) threshold
> SREF uses; it uses round inch-ish breaks (0.5/1/2/3/5 in). The threshold→tier
> config (FR-20a) for HREF is therefore distinct from SREF's, not a copy.

## Polygon aggregation (shared with SREF)

The zonal reducer is grid-agnostic and now lives in `src/upstreamwx/grib/zonal.py`,
shared by both ensembles. On the 3 km grid the **Buckskin upstream domain holds
~50 HREF cells** (vs ~5 SREF cells at 16 km), so the coarse-grid nearest-cell
fallback is essentially never needed — a concrete resolution win for headwater
basins.

**Bug found & fixed during the spike:** HREF stores longitude as 0–360°E. `regionmask`
auto-wraps (the polygon path was fine), but the manual nearest-cell fallback's
distance search did not, picking a wrong/NaN cell. The fallback now uses the
shortest angular Δlon `((lon − c.x + 180) % 360 − 180)`, correct for both the SREF
(−180..180) and HREF (0..360) conventions. Covered by `test_href_aggregate.py`.

## Live demonstration (cycle 20260618/00Z, f12, Buckskin Gulch upstream domain)

| Field | msgs | subset | poly max | poly mean | cells | CONUS max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| P(precip > 12.7 mm/1h ≈ 0.5 in, slot) | 1 | 215 KB | 0.0% | 0.0% | 50 | 100.0% |
| P(precip > 25.4 mm/1h ≈ 1 in) | 1 | 200 KB | 0.0% | 0.0% | 50 | 89.5% |
| P(precip > 12.7 mm/3h) | 1 | 232 KB | 0.0% | 0.0% | 50 | 100.0% |
| P(reflectivity > 40 dBZ) | 1 | 244 KB | 0.0% | 0.0% | 50 | 100.0% |
| P(lightning > 0.2) | 1 | 365 KB | 0.0% | 0.0% | 50 | 80.0% |
| P(CAPE > 1000 J/kg) | 1 | 336 KB | 0.0% | 0.0% | 50 | 100.0% |

**Plausibility:** the Buckskin/Colorado-Plateau domain was dry on 18 Jun 2026 (0%
across its 50 in-domain 3 km cells), while **CONUS-wide maxima of 80–100%** confirm
the extraction is reading real signal, not zeros. Values are valid probabilities
in [0, 100].

## Resource profile (single cycle, one forecast hour, 6 fields)

| Metric | Value | Note |
| --- | --- | --- |
| Per-message subset | **0.2–0.37 MB** | one NEP message on the 3 km grid; comparable to SREF |
| Total subset (6 fields, f12) | **~1.6 MB** | idx byte-range — vs tens of MB for a full per-hour file |
| Cycle lookup | ~0.3–0.9 s | newest-first probe of recent cycles |
| Peak RSS (whole process) | **~866 MB** | higher than SREF's ~514 MB — 3 km grids are ~1.5 M points each |

**Architecture implications (carry to M0.1):**

- **Per-hour files mean per-hour fetches.** Covering a mission window pulls selected
  messages from several `fHH` files (one idx + a few ranged GETs each). More HTTP
  round-trips than SREF, each tiny. Fetch **only the mission's exposure-hours**, not
  all 48 — the dominant cost lever.
- **Conditional ingestion.** Pull HREF only when an active mission window falls in
  range (≲36 h); otherwise it is wasted compute. SREF stays unconditional.
- **Memory.** Decode fields sequentially and release them; peak RSS scales with how
  many 3 km grids are held at once. Still fits the existing EC2 at the PRD's scale.
- **Cold start.** The first ~3–6 h of HREF have reduced skill (spin-up); the 0–6 h
  window is better served by the HRRR-derived Open-Meteo layer. Lean on HREF in
  ~6–36 h.

## Why this satisfies the same-day supplement goal

`ensprod` NEP fields give convection-allowing flash-flood and lightning probability
inside the same-day window with no in-house member pipeline, reusing the validated
SREF idx+aggregate machinery. Combined with SREF for the longer horizon and the
"show both, higher tier wins" engine posture (FR-19), this is sufficient to add
HREF as a v1.x supplement; HREF-specific tier cut points remain versioned config
(FR-20a), set and tuned in M0.1, not hard-coded here.

## Reproduce

```sh
.venv/bin/python spikes/spike_c_href/run_spike_c.py \
    --polygon tests/fixtures/buckskin_huc12.geojson --fhour 12
```
