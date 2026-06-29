# Spike F — GEFS over a Polygon (SREF replacement, post-EOL)

**Date:** 2026-06-29
**Code:** CLI `spikes/spike_f_gefs/run_spike_f.py` (reuses shared `src/upstreamwx/grib/`)
**Why:** SREF is terminated **2026-08-31 12Z** (NWS SCN 26-47); NWS recommends **GEFS** as the
replacement ensemble. Unlike SREF/HREF/REFS, **GEFS ships no pre-computed probability product**,
so this spike prototypes the two pieces the production provider must add: a **member-exceedance
reducer** and a **derived lightning proxy**.

## Feasibility verdict: **YES — with caveats**

GEFS is retrievable on the standard NOMADS `.idx` byte-range pattern and carries the fields we
need (APCP, CAPE), and the member-exceedance reducer reproduces the `P(field > threshold)`
signal SREF's `ensprod` used to ship. But GEFS is **not a drop-in** for SREF — it requires new
provider machinery and re-baselined thresholds, and it is fundamentally **coarse**. It is the
right **longer-range / coarse backstop** beyond REFS range, *not* a basin-resolving source.

## Data source (resolved)

| Question | Finding |
| --- | --- |
| Authoritative source | **NOMADS** `https://nomads.ncep.noaa.gov/pub/data/nccf/com/gens/prod/` (AWS `noaa-gefs-pds` mirrors it). |
| Layout | `gefs.YYYYMMDD/HH/atmos/<set>/<member>.tHHz.<infix>.fFFF` (+ `.idx`) |
| Sets | `pgrb2sp25` (**0.25°** "select" — used; best resolution, smallest files), `pgrb2ap5`/`pgrb2bp5` (0.5°) |
| Members | **31** — `gec00` (control) + `gep01..gep30`; plus `geavg` (mean), `gespr` (spread) |
| **Probability product** | **NONE.** Only per-member grids + mean/spread. Exceedance must be computed in-house. |
| Cadence | **4 cycles/day at 00/06/12/18 UTC**, out to f384 |
| `.idx` sidecars | present (one per member file) — `grib/idx.py` applies unchanged |
| Member tag | `ENS=+N` (perturbed) / `ENS=low-res ctl` (control) — **not** a `prob` token |

## Fields & the in-house reducer

Per-member descriptors (live, `gec00` 0.25° f024):

- `APCP:surface:18-24 hour acc fcst` — **6-hour accumulation buckets** (coarser than SREF's 3 h
  and REFS's 1 h/3 h; a real temporal-resolution loss for short-fuse QPF).
- `CAPE:surface:24 hour fcst` and `CAPE:180-0 mb above ground:24 hour fcst` (instantaneous).

**Member-exceedance reducer (prototyped, validated):** for each member, subset the field via
`.idx`, aggregate its **max over the polygon** (shared `aggregate_over_polygon`), then reduce
to `P = (#members exceeding threshold) / N` — the probability SREF handed us pre-baked. The
diagnostic confirmed correctness: a member's global CAPE max of **4914 J/kg** decodes fine while
the **basin** max is ~0 at the same valid time (the Colorado Plateau was genuinely quiescent),
i.e. the reducer tracks the field, it is not zeroing it.

**Lightning proxy (prototyped):** GEFS has **no thunderstorm-probability field**. The spike
derives one as the per-member **co-occurrence of instability and precip** —
`P(CAPE > 1000 J/kg AND APCP > 2.5 mm)` across members. Mechanically validated; the thresholds
are placeholders to be **calibrated and provenance-stamped** in the transition (FR-20a), and the
resulting confidence must be treated as lower than SREF's direct `P(tstm)`.

## Coarse-grid finding (the central caveat)

At **0.25°** (~25 km) the Buckskin upstream domain holds only **2 cells** — just enough to avoid
the nearest-cell fallback, but barely. At **0.5°** it would fall to a single nearest-cell point.
**GEFS cannot resolve a headwater watershed.** This empirically confirms the transition design:
**REFS (3 km, 138 cells) is authoritative inside its window; GEFS is the coarse ensemble beyond
range / on REFS outage**, never the primary flash-flood domain.

GEFS is a **global 0–360° lon, descending-latitude** grid. The spike crops to the polygon
neighborhood with 0–360 bounds (cheap, avoids masking ~1 M global points/member) then shifts lon
to −180..180 so `regionmask` matches the polygon frame (`_crop_and_normalize`). The production
provider needs the same normalization.

## Live demonstration (cycle 20260628/18Z, f024, 31 members, Buckskin)

| Computed probability | Value |
| --- | ---: |
| P(precip > 12.7 mm/6h) | 0.0% |
| P(precip > 25.4 mm/6h) | 0.0% |
| P(CAPE > 1000 J/kg) | 0.0% |
| P(CAPE > 2000 J/kg) | 0.0% |
| lightning proxy P(CAPE > 1000 & precip > 2.5) | 0.0% |

All-zero reflects genuinely quiet weather over the basin this cycle (global CAPE max 4914 J/kg
elsewhere confirms live decode); the deliverable here is the **mechanism + resource profile**,
not a convective case.

## Resource profile (the key result)

| Metric | Value | Note |
| --- | --- | --- |
| Per-member subset | **~1.3 MB avg** | one APCP + one CAPE message at 0.25° |
| Per-subset wall time | **~1.34 s** | dominated by per-request HTTP RTT through the proxy |
| **31 members × 2 fields (62 subsets), sequential** | **83.1 s** | ⚠️ **exceeds the 60 s `download_subset` budget** |
| Total subset bytes | **~25 MB** | 62 messages |
| Peak RSS | **~296 MB** | low — the global field is cropped before aggregation |

**Architecture implications (carry to the transition build):**

- **⚠️ Parallelize member fetches.** 62 sequential subsets blow the 60 s budget. The production
  GEFS provider **must** fan member fetches across a `ThreadPoolExecutor` (the same pattern
  `ingest/orchestrator.py` already uses for SREF+HREF). At ~16-way concurrency, 62 subsets
  collapse to ~4 waves ≈ 5–7 s — comfortably in budget. Also: fetch **only the fields and the
  few exposure-hours** a mission needs, and consider caching warmed member subsets per cycle.
- **New reducer is required code**, not config: the member→exceedance step (validated here) is
  the one piece with no SREF/HREF analogue. Keep it in `grib/` (or `gefs/aggregate.py`) so it is
  unit-testable on a committed member fixture.
- **Re-baseline thresholds.** GEFS 6 h APCP buckets and a 31-member coarse ensemble produce
  different probability statistics than SREF's 27-member 16 km `ensprod`; the `gefs_*` threshold
  blocks need their own calibration + provenance (FR-20a), not a copy of SREF's.
- **Lightning is the weakest link.** No native `P(tstm)`; the CAPE×precip proxy needs calibration
  and honest (lower) confidence. Inside REFS range, prefer REFS `LTNG`; use the GEFS proxy only
  beyond it.

## Reproduce

```sh
.venv/bin/python spikes/spike_f_gefs/run_spike_f.py --members 5 --dump-idx
.venv/bin/python spikes/spike_f_gefs/run_spike_f.py \
    --polygon tests/fixtures/buckskin_huc12.geojson --fhour 24 --members 31
```
