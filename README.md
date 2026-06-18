# UpstreamWX

Mission-specific, multi-hazard weather briefings for **caving and canyoneering**
across the contiguous US. UpstreamWX synthesizes NWS products, Open-Meteo
derived fields, and in-house **SREF + HREF ensemble** processing into a BLUF/SITREP
covering four life-safety hazards — **flash flooding, lightning, heat stress, and
cold/wet hypothermia** — with **upstream-watershed aggregation** for flash flood as
the technical centerpiece.

It is a free, donation-supported PWA and is **reference-only**: it surfaces hazard
assessment and links to authoritative sources for verification; it never issues a
go/no-go decision.

See [`UpstreamWX-PRD-v0.8.md`](UpstreamWX-PRD-v0.8.md) and
[`roadmap.md`](roadmap.md).

## Status — M0.0 (Foundation & De-Risk Spikes)

Both hard-unknown feasibility spikes are **resolved YES** against live data
(2026-06-18), plus an additive **Spike C** that de-risks the HREF same-day
high-resolution supplement. See [`docs/m0.0/`](docs/m0.0/).

- **Spike A — SREF over a polygon** (`src/upstreamwx/sref/`): native SREF GRIB2
  is retrievable from NOMADS `ensprod`; P(precip)/P(CAPE)/spread extract and aggregate
  over a watershed polygon via `.idx` byte-range subsetting. → [report](docs/m0.0/spike-a-report.md)
- **Spike C — HREF same-day supplement** (`src/upstreamwx/href/`): the ~3 km HREF
  convection-allowing ensemble is retrievable on the same NOMADS `ensprod` + `.idx`
  pattern; neighborhood P(QPF)/P(lightning)/P(reflectivity) sharpen the same-day
  (≈6–36 h) flood/lightning signal, reusing the shared `grib` module. → [report](docs/m0.0/spike-c-report.md)
- **Spike B — upstream HUC-12 trace** (`src/upstreamwx/watershed/`): arbitrary
  CONUS lat/lon → containing HUC-12 → deterministic upstream contributing-area trace
  from USGS WBD. → [report](docs/m0.0/spike-b-report.md)
- **SREF resource profile** for EC2 sizing → [report](docs/m0.0/resource-profile.md)

## Quickstart

```sh
uv venv --python 3.11
uv pip install -e ".[dev]"

# Spike B — resolve + trace an upstream watershed (live USGS WBD)
.venv/bin/python spikes/spike_b_huc/run_spike_b.py \
    --lat 37.0192 --lon -111.9889 --name "Buckskin Gulch" \
    --out tests/fixtures/buckskin_huc12.geojson

# Spike A — SREF probabilities aggregated over that polygon (live NOMADS)
.venv/bin/python spikes/spike_a_sref/run_spike_a.py \
    --polygon tests/fixtures/buckskin_huc12.geojson

# Spike C — HREF same-day high-res probabilities over that polygon (live NOMADS)
.venv/bin/python spikes/spike_c_href/run_spike_c.py \
    --polygon tests/fixtures/buckskin_huc12.geojson --fhour 12
```

## Tests & lint

```sh
.venv/bin/pytest          # offline, hermetic (committed fixtures; network tests deselected)
.venv/bin/pytest -m network   # opt-in live-service tests (NOMADS/USGS)
.venv/bin/ruff check .
```

## Layout

```
src/upstreamwx/   backend package
  watershed/           Spike B -> M0.1 watershed module (HUC-12 + upstream trace)
  grib/                shared GRIB2 idx byte-range + polygon zonal aggregation
  sref/                Spike A -> M0.1 SREF processor (fetch/extract/aggregate)
  href/                Spike C -> M0.1 HREF same-day supplement (~3 km, ≈6-36 h)
  engine/ ingest/ sitrep/   placeholders for M0.1-M0.2
spikes/                runnable de-risk CLIs
tests/                 hermetic suite + committed fixtures
docs/m0.0/             spike reports + resource profile
frontend/              reserved for the PWA (M0.4)
```

Licensed under GPL-3.0 (see `LICENSE`).
