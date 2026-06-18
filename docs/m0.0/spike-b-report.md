# Spike B — Upstream HUC-12 Watershed Trace (M0.0)

**Date:** 2026-06-18
**PRD refs:** FR-2 (lat/lon → containing HUC-12), FR-3 (upstream contributing watershed)
**Code:** `src/upstreamwx/watershed/{huc,upstream}.py`, CLI `spikes/spike_b_huc/run_spike_b.py`

## Goal

Given an arbitrary CONUS lat/lon, deterministically (1) resolve the containing
HUC-12 and (2) trace the **upstream contributing watershed** — the set of
hydrologic units that drain *into* the point — and emit a dissolved upstream
polygon (the aggregation domain Spike A / SREF will consume).

## Service & method

- **HUC resolution:** USGS Watershed Boundary Dataset, `wbd12` layer via
  `pynhd.WaterData` (HyRiver 0.19.4), point geometry query against the GeoServer
  WFS (api.water.usgs.gov / hydro.nationalmap.gov). Falls back to `wbd10`
  (HUC-10) if no HUC-12 covers the point (FR-2).
- **Upstream trace — PRIMARY (`tohuc-graph`):** each WBD HUC-12 carries a
  `tohuc` attribute naming the immediately downstream HUC-12. We fetch all
  HUC-12s in the surrounding **HU8** (a `huc12 LIKE '<HU8>%'` filter), build a
  directed graph of *downstream → upstream* edges with networkx, and collect
  `descendants(origin) ∪ {origin}` — exactly the units draining into the point.
  Their polygons are dissolved (geopandas `dissolve` / `union_all`).
- **Region auto-widening:** a HUC-12's contributing area can extend beyond its
  HU8 (common on the plains). After computing the HU8 upstream set we probe the
  *headwater leaves* of that set (`tohuc = '<leaf>'` equality queries — the only
  filter form this GeoServer WFS reliably accepts; `IN (...)`/`OR`-chains return
  HTTP 403). If any leaf receives inflow from a HUC outside the fetched region,
  the set was truncated, so we widen to **HU6** (then **HU4**) and recompute.
- **Fallback (`nldi-ut`):** if the `tohuc` walk yields nothing, navigate
  upstream-tributaries from the nearest NHDPlus flowline via USGS NLDI
  (`NLDI.get_basins`) and use that basin. (Not needed for any test point.)
- **Area:** computed in **EPSG:5070** (NAD83 / CONUS Albers, equal-area).
- WBD attribute lookups are case-insensitive and tolerate `tohuc` /
  `tohuc12` / `to_huc` spelling variants.

## Per-coordinate results (live run, 2026-06-18)

| Location | lat, lon | HUC-12 | HUC name | # upstream HUC-12 | Area (km²) | Method | Latency |
| --- | --- | --- | --- | ---: | ---: | --- | ---: |
| Buckskin Gulch, UT | 37.0192, -111.9889 | 140700070505 | Cottonwood Cove-Buckskin Gulch | 14 | 1263 | tohuc-graph | ~3.5 s |
| Zion Narrows / Virgin R., UT | 37.285, -112.948 | 150100080109 | Lower North Fork Virgin River | 9 | 927 | tohuc-graph | ~3.3 s |
| Antelope Canyon, AZ | 36.862, -111.374 | 140700060704 | Lower Antelope Creek | 3 | 366 | tohuc-graph | ~2.8 s |
| Kansas control (plains) | 38.5, -98.0 | 102600080101 | Wiley Creek-Smoky Hill River | 179 | 20264 | tohuc-graph (widened HU8→HU6) | ~15 s |

All four resolved a HUC-12 (no HUC-10 fallback needed) and traced a non-empty,
valid upstream polygon. The three slot-canyon points aggregate multiple upstream
headwater HUC-12s with sizable area, as expected.

### Note on the Kansas control

At HU8 scope the Smoky Hill River origin showed only **1** upstream HUC-12 — a
truncation artifact: its real headwaters sit in an **adjacent HU8** (`10260006`)
within the same HU6. The leaf-inflow probe detected the external inflow
(`102600060608 → origin`) and widened the fetch to **HU6**, yielding the true
**179**-HUC, ~20,264 km² contributing area (entirely within HU6 `1026`,
confirming HU6 is complete). This is the expected behavior for a large
through-flowing plains river vs. a self-contained canyon basin, and validates the
widening logic.

## Determinism

The `tohuc` graph walk is order-independent: node iteration is sorted and the
upstream set is `nx.descendants` over a fixed graph, so identical input lat/lon +
WBD snapshot ⇒ identical upstream HUC-12 set and area. Verified by running the
Kansas trace twice — identical id list and area to floating-point equality. The
committed fixture polygon is simplified (30 m, EPSG:5070) only for size; the
underlying HUC set is exact.

## Fixture (Spike A contract)

`tests/fixtures/buckskin_huc12.geojson` — single-feature GeoJSON, EPSG:4326,
dissolved Buckskin Gulch upstream domain (origin `140700070505`, 14 HUC-12s,
~1263 km²), generated live on 2026-06-18. Provenance in
`tests/fixtures/README.md`. ~26 KB. This is the aggregation domain Spike A
(SREF) consumes.

## Validation

- `.venv/bin/pytest -q` (offline): **3 passed, 1 deselected** (green).
- `@pytest.mark.network` live test (resolve + trace Buckskin): **1 passed**.
- `.venv/bin/ruff check src/upstreamwx/watershed spikes/spike_b_huc tests`:
  **All checks passed.**

## Limitations / caveats

- **WFS filter restrictions:** the USGS GeoServer rejects `IN (...)` and
  `OR`-chained CQL (HTTP 403); the truncation probe is built from single-equality
  `tohuc = '<leaf>'` queries per headwater leaf to stay within what the service
  accepts.
- **Widening cost:** for through-flowing rivers the HU6 fetch is larger
  (hundreds of features) and slower (~10–15 s vs ~3 s); HU6 fetches up to ~800
  features in the cases tested. M0.1 should add a local WBD GeoPackage / cache so
  the trace is fast and offline-reproducible.
- **HU4 ceiling:** widening stops at HU4. A watershed spanning multiple HU4
  regions (very large rivers, not the canyoneering use case) would still be
  capped; flagged in `UpstreamTrace.notes` only when an HU6/HU8 widen occurred.
- **NLDI fallback unexercised:** the `nldi-ut` path is implemented but no test
  point required it; it should be smoke-tested before relying on it in M0.1.

## FEASIBILITY VERDICT

**YES.** We can resolve an arbitrary CONUS lat/lon to its containing HUC-12 and
deterministically trace the upstream contributing watershed on demand, live, from
USGS WBD via HyRiver — in ~3 s for compact canyon basins (and ~15 s for large
plains rivers requiring region widening). Output is a valid dissolved polygon
with an equal-area km² figure, ready as the SREF aggregation domain. FR-2 and
FR-3 are feasible as specified.
