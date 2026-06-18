# Spike A — SREF over a Polygon (M0.0)

**Date:** 2026-06-18
**Code:** `src/upstreamwx/sref/` · CLI `spikes/spike_a_sref/run_spike_a.py`

## Feasibility verdict: **YES**

Native SREF GRIB2 is retrievable on a usable cadence, the ensemble probability
and spread fields needed by Appendix B are extractable, and they aggregate
cleanly over an upstream-watershed polygon. **The SREF data-availability risk is
resolved: YES.**

## Data source (resolved)

| Question | Finding |
| --- | --- |
| AWS / GCP open-data mirror? | **No.** The `noaa-sref-pds` S3 bucket returns `NoSuchBucket`; no SREF NODD mirror exists. |
| Authoritative source | **NOMADS** — `https://nomads.ncep.noaa.gov/pub/data/nccf/com/sref/prod/` |
| Layout | `sref.YYYYMMDD/HH/ensprod/sref.tHHz.<grid>.<product>_<freq>.grib2` (+ `.idx`) |
| Cadence | **4 cycles/day at 03/09/15/21 UTC**, 27 members (idx tag `0/26`) |
| Products used | `prob` (probabilities), `spread` (ensemble spread), `mean` (QPF/CAPE) — pre-computed in `ensprod`, so we never sum raw members |
| Grid | `pgrb132` (AWIPS 132, **~16 km**, 697×553 Lambert; 2D lat/lon) — preferred over `pgrb212` (~40 km) |
| Retention | Short (~2 days of cycles on NOMADS) → production must pull each cycle promptly |
| Production lag | Files appear well after init; `latest_available_cycle()` probes recent cycles newest-first |

**Caveat observed:** the bare `sref/prod/` directory and the *current-day* path can
transiently return 502/403 (directory listing throttling, or a cycle not yet
posted). Constructing the dated `ensprod` URL directly and probing newest-first is
reliable — the most recent fully-available cycle resolved in ~1.4 s.

## Fields extracted (mapped to Appendix B)

The `prob` product carries exactly what the hazard logic needs:

- **Flash flood (§16.1):** `APCP:surface` probability at mm thresholds
  (0.25, 1.27, 2.54, **6.35 ≈ 0.25 in**, **12.7 ≈ 0.5 in/3h slot threshold**, 25.4, …)
  over 3-hourly (and longer) accumulation windows.
- **Lightning / convection (§16.2):** `CAPE:surface` probability (>250…>4000 J/kg),
  plus `PLI` (lifted index), `CIN`, and categorical precip-type fields as proxies.
- **Confidence (§16.5):** the `spread` product provides ensemble spread directly;
  member-support fractions are derivable from the probability fields (27 members).

## Polygon aggregation

`regionmask` rasterizes the watershed polygon onto the native SREF grid; we report
**max** (the conservative trigger Appendix B uses) and **areal mean**, per forecast
step. A headwater polygon smaller than a ~16 km cell triggers a **nearest-cell
fallback** at the centroid, flagged in the output (`fallback_nearest_cell`).

## Live demonstration (cycle 20260617/15Z, Buckskin Gulch upstream domain)

| Field | msgs | subset | poly max | poly mean | cells | CONUS max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| P(precip > 0.25 mm/3h) | 1 | 248 KB | 0.0% | 0.0% | 5 | 100.0% |
| P(precip > 6.35 mm/3h ≈ 0.25 in) | 1 | 69 KB | 0.0% | 0.0% | 5 | 100.0% |
| P(precip > 12.7 mm/3h ≈ 0.5 in, slot) | 1 | 28 KB | 0.0% | 0.0% | 5 | 96.2% |
| P(CAPE > 500 J/kg) | 30 | 3.7 MB | 0.0% | 0.0% | 5 | 100.0% |
| P(CAPE > 1000 J/kg) | 30 | 3.7 MB | 0.0% | 0.0% | 5 | 100.0% |

**Plausibility:** the Buckskin/Colorado-Plateau domain was dry on 17 Jun 2026 (0%
across all fields over its 5 in-domain grid cells), while the **CONUS-wide maxima of
96–100%** confirm the extraction is reading real signal, not returning zeros. Values
are valid probabilities in [0, 100].

## Why this satisfies v1 (per PRD Appendix B)

`ensprod` probability fields cover the SREF planning horizon for both the flash-flood
and lightning models without an in-house member pipeline. Combined with NWS
watch/warning ingest (M0.1), this is sufficient for v1; the quantitative QPF-vs-FFG
refinement remains deferred to v1.x as the PRD specifies.

## Limitations / notes for M0.1

- **~16 km grid vs small headwater HUC-12s:** few or zero cells inside; the
  nearest-cell fallback is coarse. Coverage-weighted aggregation (`exactextract`,
  already an optional dep) is the refinement.
- **CAPE fields are larger** (no accumulation subsetting → all forecast hours): ~3.7 MB
  / ~30 messages vs ~30–250 KB for a single precip window. Select specific forecast
  hours in M0.1 to trim.
- **Threshold→tier mapping** is intentionally not implemented here (that is the M0.1
  rule engine, driven by versioned config per FR-20a). Spike A only proves extraction.

## Reproduce

```sh
.venv/bin/python spikes/spike_a_sref/run_spike_a.py \
    --polygon tests/fixtures/buckskin_huc12.geojson
```
