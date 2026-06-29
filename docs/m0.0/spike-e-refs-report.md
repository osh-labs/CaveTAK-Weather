# Spike E — REFS over a Polygon (HREF replacement, post-EOL)

**Date:** 2026-06-29
**Code:** CLI `spikes/spike_e_refs/run_spike_e.py` (reuses shared `src/upstreamwx/grib/`)
**Why:** SREF **and HREF** are terminated **2026-08-31 12Z** (NWS SCN 26-47). REFS (the RRFS
Ensemble) is NCEP's convection-allowing **replacement for HREF**. This spike de-risks it as the
same-day flash-flood/lightning source, the post-EOL analogue of Spike C.

## Feasibility verdict: **YES (drop-in for HREF)**

REFS is retrievable on the **same `prob` (Neighborhood Ensemble Probability) + `.idx`
byte-range pattern as HREF**, its NEP descriptors are **byte-for-byte compatible** with the
HREF convention (the `accum_window` helper ports verbatim), it carries every flash-flood and
lightning field UpstreamWX needs (plus more), and it aggregates cleanly over an upstream
watershed at 3 km with real cells. The HREF→REFS transition is a near-mechanical provider swap.
**REFS data-availability risk: resolved YES.**

REFS becomes the **authoritative** convection-allowing ensemble for the same-day window
(extendable from HREF's ~36 h toward REFS's f60); GEFS (Spike F) is the coarse backstop beyond
range (PRD §16.1/§16.2, FR-7a, FR-19).

## Data source (resolved)

| Question | Finding |
| --- | --- |
| Authoritative source | **AWS open-data** `noaa-rrfs-pds` — **no NOMADS `com/` path carries REFS** (checked: no `com/rrfs`, `com/refs`, `com/para`). |
| Layout | `s3://noaa-rrfs-pds/rrfs_a/refs.YYYYMMDD/HH/enspost/refs.tHHz.<product>.fFH.<domain>.grib2` (+ `.idx`). A mirror lives under `rrfs_public/`. |
| HTTPS base | `https://noaa-rrfs-pds.s3.amazonaws.com/rrfs_a/refs.<date>/<hh>/enspost/` |
| Cadence | **4 cycles/day at 00/06/12/18 UTC** (vs HREF's two) |
| Members | **15** (idx tag `0/14`; vs HREF's 11) |
| Horizon | **f03→f48 at 3 h, then to f60 at 6 h** (vs HREF f01–f48 hourly) |
| File granularity | **one file per forecast hour per domain** (`conus`/`ak`/`hi`/`pr`), as HREF |
| Products | `prob` (NEP — used), plus `mean`, `pmmn`, `lpmm`, `sprd`, `avrg`, `eas`, `ffri` |
| `.idx` sidecars | **present** — shared `grib/idx.py` byte-range machinery applies unchanged |

## Fields extracted (mapped to Appendix B; live `prob` f12 descriptors)

The `prob` product is a true NEP field carrying exactly the HREF-relevant signals — the
descriptor grammar is identical to HREF, so cut-point selection is mechanical:

- **Flash flood (§16.1):** `APCP:surface` NEP at **12.7 / 25.4 / 50.8 / 76.2 / 127 / 203.2 mm**
  over **1 h** (`11-12 hour acc`), **3 h** (`9-12 hour acc`), **6 h** (`6-12 hour acc`) and
  run-total windows — the exact `accum_window(fhour, hours)` labels HREF already builds.
- **Lightning / convection (§16.2):** explicit **`LTNG` NEP** (`prob >0.08` — note HREF used
  `>0.2`), **`REFC`** composite reflectivity (`>10..>50 dBZ`), **`CAPE`** (`>500..>3000`),
  plus **`MXUPHL`** updraft helicity and **`HLCY`** storm-relative helicity as severe proxies.
- **Bonus over HREF:** **`PWAT`** (precipitable water `>25/37.5/50 mm`) and **`CIN`** bands —
  directly relevant to heavy-rain/flash-flood framing.
- **Confidence (§16.5):** `sprd` gives ensemble spread; REFS↔GEFS agreement is a cross-source
  confidence cue (FR-17), replacing today's SREF↔HREF check.

> Like HREF vs SREF, REFS APCP NEP uses round metric breaks (12.7/25.4/...) and an `LTNG`
> threshold of `>0.08`. The threshold→tier config (FR-20a) for REFS is therefore its own
> versioned block, seeded from HREF's and re-baselined — not a copy.

## Polygon aggregation (shared `grib/zonal.py`, unchanged)

The grid-agnostic zonal reducer worked on the REFS 3 km grid with no code change. The
**Buckskin upstream domain holds 138 REFS cells** — no coarse-grid nearest-cell fallback, a
strong resolution win for headwater basins (comparable to HREF's behavior).

## Live demonstration (cycle 20260629/00Z, f12, Buckskin Gulch upstream domain)

| Field | msgs | subset | poly max | poly mean | cells | CONUS max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| P(precip > 12.7 mm/1h ≈ 0.5 in) | 1 | 91 KB | 0.0% | 0.0% | 138 | 63.5% |
| P(precip > 25.4 mm/1h ≈ 1 in) | 1 | 51 KB | 0.0% | 0.0% | 138 | 56.0% |
| P(precip > 25.4 mm/3h) | 1 | 88 KB | 0.0% | 0.0% | 138 | 65.0% |
| P(reflectivity > 40 dBZ) | 1 | 169 KB | 0.0% | 0.0% | 138 | 78.5% |
| P(lightning > 0.08) | 1 | 62 KB | 0.0% | 0.0% | 138 | 73.0% |
| P(CAPE > 1000 J/kg) | 1 | 437 KB | 0.0% | 0.0% | 138 | 100.0% |
| P(updraft-helicity > 75) | 1 | 33 KB | 0.0% | 0.0% | 138 | 67.0% |

**Plausibility:** the Buckskin/Colorado-Plateau domain was dry at this valid time (0% across
its 138 in-domain 3 km cells), while **CONUS-wide maxima of 56–100%** confirm the extraction
reads real signal, not zeros. Values are valid probabilities in [0, 100].

## Resource profile (single cycle, one forecast hour, 7 fields)

| Metric | Value | Note |
| --- | --- | --- |
| Per-message subset | **0.03–0.44 MB** | one NEP message on the 3 km grid; comparable to HREF |
| Total subset (7 fields, f12) | **~0.93 MB** | idx byte-range vs a multi-MB full per-hour file |
| End-to-end wall time | **~20 s** | includes cfgrib import + newest-first cycle probe |
| Peak RSS (whole process) | **~981 MB** | 3 km CONUS fields are ~1.9 M points; matches HREF order |

**Architecture implications (carry to the transition build):**

- **Provider is a near-clone of `href/`.** Same per-hour-file model, same NEP `prob` product,
  same `accum_window` labels. Retarget base URL to AWS S3, set cycles `(0,6,12,18)`, members
  15, `LTNG` threshold `>0.08`, and extend the lead-time cap toward f48–f60. The
  `href_selection.resolve_valid_time_sources` spin-up/backfill logic ports directly.
- **AWS, not NOMADS.** REFS's public source is S3 (the reverse of SREF/HREF). The fetch is a
  plain ranged GET, so `grib/idx.py` needs no change; only the URL builder differs.
- **Fetch only exposure-hours**, decode sequentially and release — same memory discipline as HREF.
- **Cold start.** REFS products begin at f03; the 0–3 h nowcast window stays with the
  HRRR-derived Open-Meteo layer. Lean on REFS in ~3–36 h (extendable to 60).

## Reproduce

```sh
.venv/bin/python spikes/spike_e_refs/run_spike_e.py --dump-idx --fhour 12
.venv/bin/python spikes/spike_e_refs/run_spike_e.py \
    --polygon tests/fixtures/buckskin_huc12.geojson --fhour 12
```

## Addendum (2026-06-29) — production endpoint vs the validated prototype

The spike validated REFS against the **AWS RRFS "[Prototype]" bucket** (`noaa-rrfs-pds/rrfs_a`,
subdir `enspost`) — the only REFS feed reachable for end-to-end validation in the build container.
**SCN 26-48** (the RRFS/REFS *implementation* notice) gives the authoritative NOMADS paths:

| Feed | Base | Subdir | Status |
| --- | --- | --- | --- |
| **Production** | `…/com/refs/prod/` | **`ensprod`** | authoritative, live 2026-08-31 12Z |
| **Pre-impl. parallel** | `…/com/refs/para/` | **`ensprod`** | since ~2026-06-09 |
| AWS prototype (validated here) | `noaa-rrfs-pds/rrfs_a/` | **`enspost`** | dev/validation default |

Layout is otherwise identical: `refs.YYYYMMDD/CC/<subdir>/refs.tCCz.<type>.fFF.<dom>.grib2`,
`type∈{mean,sprd,pmmn,lpmm,avrg,prob,eas}` (+ `ffri`, conus-only), `dom∈{conus,ak,hi,pr}`, cycles
00/06/12/18, to f60 — matching what the provider selects. **The one real difference is the subdir
(`enspost` → `ensprod`) plus the host.** The provider therefore reads its feed from config
(`refs_source`, default `aws`); flip to `nomads_prod` at cutover. The NOMADS `refs/{para,prod}`
paths returned **HTTP 403 from the build container** (egress policy / not-yet-serving), so the
`ensprod` layout must be confirmed from a network-unrestricted host before the default is changed.
Operational membership (5 RRFS + 2 HRRR, time-lagged) may differ from the prototype's idx tag
`0/14` — immaterial, since we read the precomputed `prob` NEP, not individual members.
