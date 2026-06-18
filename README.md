# CaveTAK-Weather

Mission-specific, multi-hazard weather briefings for **caving and canyoneering**
across the contiguous US. CaveTAK-Weather synthesizes NWS products, Open-Meteo
derived fields, and in-house **SREF ensemble** processing into a BLUF/SITREP
covering four life-safety hazards — **flash flooding, lightning, heat stress, and
cold/wet hypothermia** — with **upstream-watershed aggregation** for flash flood as
the technical centerpiece.

It is a free, donation-supported PWA and is **reference-only**: it surfaces hazard
assessment and links to authoritative sources for verification; it never issues a
go/no-go decision.

See [`CaveTak-Weather-PRD-v0.8.md`](CaveTak-Weather-PRD-v0.8.md) and
[`roadmap.md`](roadmap.md).

## Status — M0.0 (Foundation & De-Risk Spikes)

Both hard-unknown feasibility spikes are **resolved YES** against live data
(2026-06-18). See [`docs/m0.0/`](docs/m0.0/).

- **Spike A — SREF over a polygon** (`src/cavetak_weather/sref/`): native SREF GRIB2
  is retrievable from NOMADS `ensprod`; P(precip)/P(CAPE)/spread extract and aggregate
  over a watershed polygon via `.idx` byte-range subsetting. → [report](docs/m0.0/spike-a-report.md)
- **Spike B — upstream HUC-12 trace** (`src/cavetak_weather/watershed/`): arbitrary
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
```

## Tests & lint

```sh
.venv/bin/pytest          # offline, hermetic (committed fixtures; network tests deselected)
.venv/bin/pytest -m network   # opt-in live-service tests (NOMADS/USGS)
.venv/bin/ruff check .
```

## Layout

```
src/cavetak_weather/   backend package
  watershed/           Spike B -> M0.1 watershed module (HUC-12 + upstream trace)
  sref/                Spike A -> M0.1 SREF processor (fetch/extract/aggregate)
  engine/ ingest/ sitrep/   placeholders for M0.1-M0.2
spikes/                runnable de-risk CLIs
tests/                 hermetic suite + committed fixtures
docs/m0.0/             spike reports + resource profile
frontend/              reserved for the PWA (M0.4)
```

Licensed under GPL-3.0 (see `LICENSE`).
