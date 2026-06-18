# Test fixtures — provenance

Committed offline sample data so the suite runs hermetically (no network).

## `buckskin_huc12.geojson`

Dissolved **upstream contributing watershed** polygon for **Buckskin Gulch, UT**,
produced by Spike B (`spikes/spike_b_huc/run_spike_b.py`).

| Field | Value |
| --- | --- |
| What | Single-feature GeoJSON: the dissolved union of the origin HUC-12 and every HUC-12 draining into it |
| Query point | lat **37.0192**, lon **-111.9889** (Buckskin Gulch upstream trailhead area) |
| Origin HUC-12 | `140700070505` (Cottonwood Cove-Buckskin Gulch) |
| Upstream HUC-12 count | 14 (incl. origin) |
| Area | ~1263 km² (EPSG:5070 CONUS Albers equal-area) |
| Method | `tohuc-graph` — deterministic WBD `tohuc` graph walk |
| CRS | EPSG:4326 (WGS84) |
| Source service | USGS Watershed Boundary Dataset, `wbd12` layer via `pynhd.WaterData` (GeoServer WFS at api.water.usgs.gov / hydro.nationalmap.gov) |
| Library version | pynhd / pygeohydro / pygeoutils 0.19.4 |
| Generated | 2026-06-18 |
| Geometry | Simplified with a 30 m tolerance (in EPSG:5070) before writing, to keep the file small while staying valid |

### Regenerate

```sh
.venv/bin/python spikes/spike_b_huc/run_spike_b.py \
    --lat 37.0192 --lon -111.9889 --name "Buckskin Gulch" \
    --out tests/fixtures/buckskin_huc12.geojson --simplify 30
```

The WBD `tohuc` walk is order-independent, so identical input + WBD snapshot
yields an identical upstream set (and, modulo simplification, polygon).

## `sref_sample_subset.grib2`

A tiny SREF ensemble-probability GRIB2 subset, produced by Spike A
(`spikes/spike_a_sref/run_spike_a.py`) via `.idx` byte-range subsetting, so the
extraction + aggregation logic is testable offline.

| Field | Value |
| --- | --- |
| What | P(3-h precip > 12.7 mm ≈ 0.5 in) — the conservative slot-canyon flash-flood threshold (Appendix B §16.1) |
| Forecast windows | 0-3, 3-6, 6-9, 9-12 hour accumulation (stacks on `step`; dims `step=4, y=553, x=697`) |
| Grid | `pgrb132` (AWIPS 132, ~16 km Lambert; 2D lat/lon coords) |
| Source | NOMADS `sref/prod/sref.20260617/15/ensprod/sref.t15z.pgrb132.prob_3hrly.grib2` |
| Variable | `APCP:surface` probability (`prob >12.7`), 27-member ensemble (idx tag `0/26`) |
| Size | ~107 KB (4 GRIB messages, byte-range subset of a ~660 MB full file) |
| Generated | 2026-06-18 |

### Regenerate

```sh
.venv/bin/python - <<'PY'
from cavetak_weather.sref import latest_available_cycle
from cavetak_weather.sref.fetch import fetch_idx, select_messages, download_subset
cyc = latest_available_cycle()
idx = fetch_idx(cyc.idx_url(product="prob"))
windows = {"0-3 hour acc fcst","3-6 hour acc fcst","6-9 hour acc fcst","9-12 hour acc fcst"}
sel = [e for e in select_messages(idx, var="APCP", prob=">12.7") if e.fcst in windows]
download_subset(cyc.product_url(product="prob"), sel, "tests/fixtures/sref_sample_subset.grib2")
PY
```

(Exact values differ by cycle; the test asserts structure and plausibility, not specific values.)
