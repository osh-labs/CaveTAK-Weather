"""Watershed polygon generation latency benchmarks (FR-2, FR-3).

Measures cold (no cache) and warm (disk-cache hit) wall-clock latency for both
resolution paths across 10 geographically and physiographically diverse CONUS
locations spanning canyon country, Appalachians, Rockies, Great Plains, Pacific
Coast, and Interior Highlands.

Run with:
    pytest tests/test_watershed_latency.py -m network -v -s
All tests require live network access and are deselected by the default
``addopts = -m 'not network'`` in pyproject.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass

import pytest

from upstreamwx.config import Settings
from upstreamwx.watershed import delineate_cached, resolve_and_trace_cached

# ---------------------------------------------------------------------------
# Coordinate dataset — 10 CONUS points across major physiographic divisions.
# ---------------------------------------------------------------------------
LOCATIONS: list[dict] = [
    # Colorado Plateau — entrenched canyon, Spike B reference point
    {"name": "Buckskin Gulch, UT", "lat": 37.0192, "lon": -111.9889,
     "region": "Colorado Plateau", "huc2": "14"},
    # Allegheny Highlands — high-relief Appalachian headwaters
    {"name": "Blackwater Falls, WV", "lat": 39.1173, "lon": -79.4960,
     "region": "Appalachians", "huc2": "05"},
    # Northern Rocky Mountains — continental divide, glacier-carved
    {"name": "Glacier NP, MT", "lat": 48.6960, "lon": -113.7180,
     "region": "Northern Rockies", "huc2": "10"},
    # Black Hills — isolated uplift at Great Plains margin
    {"name": "Wind Cave, SD", "lat": 43.5578, "lon": -103.4826,
     "region": "Black Hills", "huc2": "10"},
    # Edwards Plateau — karst, flash-flood-prone Texas Hill Country
    {"name": "Hamilton Pool, TX", "lat": 30.3410, "lon": -98.1260,
     "region": "Edwards Plateau", "huc2": "12"},
    # Cumberland Plateau — deeply incised SE gorge system
    {"name": "Sipsey Fork, AL", "lat": 34.3249, "lon": -87.4236,
     "region": "Cumberland Plateau", "huc2": "06"},
    # Pacific Coast Ranges — steep short-basin coastal rivers
    {"name": "Prairie Creek, CA", "lat": 41.3985, "lon": -124.0375,
     "region": "Pacific Coast", "huc2": "18"},
    # Central Idaho / Sawtooth — large wilderness headwaters
    {"name": "Salmon River, ID", "lat": 45.1775, "lon": -114.9310,
     "region": "Central Rockies", "huc2": "17"},
    # Cumberland Escarpment — high-gradient gorge straddling KY/TN
    {"name": "Big South Fork, TN", "lat": 36.4978, "lon": -84.7059,
     "region": "Cumberland Escarpment", "huc2": "05"},
    # Interior Highlands — Ouachita Mountains, flat-to-rolling transition
    {"name": "Ouachita NF, AR", "lat": 34.7100, "lon": -94.4500,
     "region": "Interior Highlands", "huc2": "11"},
]


@dataclass
class _TimingRow:
    name: str
    region: str
    cold_s: float | None = None
    warm_s: float | None = None
    area_km2: float | None = None
    method: str | None = None
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.error is None


def _print_table(path_label: str, rows: list[_TimingRow]) -> None:
    """Print a formatted latency summary to stdout (visible with pytest -s)."""
    hdr = (
        f"  {'Location':<28} {'Region':<22}"
        f" {'Cold s':>7} {'Warm s':>7} {'Area km²':>9} {'Method':<16} Status"
    )
    divider = "-" * len(hdr)
    print(f"\n{'=' * len(hdr)}")
    print(f"  WATERSHED LATENCY — {path_label}")
    print("=" * len(hdr))
    print(hdr)
    print(divider)
    for r in rows:
        if r.success:
            cold = f"{r.cold_s:7.2f}"
            warm = f"{r.warm_s:7.3f}"
            area = f"{r.area_km2:9.0f}" if r.area_km2 is not None else f"{'—':>9}"
            method = (r.method or "—")[:15]
            status = "OK"
        else:
            cold = f"{r.cold_s:7.2f}" if r.cold_s else "   —   "
            warm = "   —   "
            area = f"{'—':>9}"
            method = "—"
            status = f"ERR: {(r.error or '')[:20]}"
        print(f"  {r.name:<28} {r.region:<22} {cold} {warm} {area} {method:<16} {status}")
    print(divider)

    cold_vals = [r.cold_s for r in rows if r.success and r.cold_s is not None]
    if cold_vals:
        p90_idx = max(0, int(len(cold_vals) * 0.9) - 1)
        print(
            f"  Cold  n={len(cold_vals):2d} "
            f" min={min(cold_vals):.2f}s"
            f"  median={statistics.median(cold_vals):.2f}s"
            f"  p90={sorted(cold_vals)[p90_idx]:.2f}s"
            f"  max={max(cold_vals):.2f}s"
        )
    warm_vals = [r.warm_s for r in rows if r.success and r.warm_s is not None]
    if warm_vals:
        print(
            f"  Warm  n={len(warm_vals):2d} "
            f" min={min(warm_vals):.3f}s"
            f"  median={statistics.median(warm_vals):.3f}s"
            f"  max={max(warm_vals):.3f}s  (disk-cache reads)"
        )
    fail_n = sum(1 for r in rows if not r.success)
    if fail_n:
        print(f"  Failures: {fail_n}/{len(rows)}")
    print("=" * len(hdr) + "\n")


# ---------------------------------------------------------------------------
# Path A: HUC-12 upstream trace via USGS WBD tohuc-graph
# ---------------------------------------------------------------------------

@pytest.mark.network
def test_watershed_latency_huc_trace(tmp_path: pytest.TempPathFactory) -> None:
    """Cold + warm latency for HUC-12 upstream trace across all 10 locations (FR-2)."""
    settings = Settings(data_dir=tmp_path)
    rows: list[_TimingRow] = []

    for loc in LOCATIONS:
        row = _TimingRow(name=loc["name"], region=loc["region"])
        lat, lon = loc["lat"], loc["lon"]

        # cold: force live fetch, bypass any on-disk cache
        t0 = time.perf_counter()
        try:
            trace = resolve_and_trace_cached(lat=lat, lon=lon, settings=settings, refresh=True)
            row.cold_s = time.perf_counter() - t0
            row.area_km2 = trace.area_km2
            row.method = trace.method
        except Exception as exc:
            row.cold_s = time.perf_counter() - t0
            row.error = str(exc)[:80]
            rows.append(row)
            continue

        # warm: second call should hit the GeoJSON on disk
        t0 = time.perf_counter()
        try:
            resolve_and_trace_cached(lat=lat, lon=lon, settings=settings)
            row.warm_s = time.perf_counter() - t0
        except Exception as exc:
            row.error = f"warm-cache failed: {exc}"

        rows.append(row)

    _print_table("HUC-12 upstream trace  (resolve_and_trace_cached)", rows)

    successes = sum(r.success for r in rows)
    assert successes >= 8, (
        f"HUC trace succeeded for only {successes}/{len(rows)} locations; "
        f"failures: {[r.name for r in rows if not r.success]}"
    )

    # Warm cache reads must be sub-second — they are pure disk I/O.
    slow_warm = [r for r in rows if r.success and r.warm_s is not None and r.warm_s > 1.0]
    assert not slow_warm, (
        f"Cache reads unexpectedly slow (>1 s): {[r.name for r in slow_warm]}"
    )


# ---------------------------------------------------------------------------
# Path B: NLDI pour-point delineation (raindrop snap + split-catchment)
# ---------------------------------------------------------------------------

@pytest.mark.network
def test_watershed_latency_pourpoint(tmp_path: pytest.TempPathFactory) -> None:
    """Cold + warm latency for NLDI pour-point delineation across all 10 locations (FR-3)."""
    settings = Settings(data_dir=tmp_path)
    rows: list[_TimingRow] = []

    for loc in LOCATIONS:
        row = _TimingRow(name=loc["name"], region=loc["region"])
        lat, lon = loc["lat"], loc["lon"]

        # cold: bypass disk cache to measure live API round-trips
        t0 = time.perf_counter()
        try:
            basin = delineate_cached(lat=lat, lon=lon, settings=settings, refresh=True)
            row.cold_s = time.perf_counter() - t0
            row.area_km2 = basin.area_km2
            row.method = basin.method
        except Exception as exc:
            row.cold_s = time.perf_counter() - t0
            row.error = str(exc)[:80]
            rows.append(row)
            continue

        # warm: cache hit
        t0 = time.perf_counter()
        try:
            delineate_cached(lat=lat, lon=lon, settings=settings)
            row.warm_s = time.perf_counter() - t0
        except Exception as exc:
            row.error = f"warm-cache failed: {exc}"

        rows.append(row)

    _print_table("NLDI pour-point delineation  (delineate_cached)", rows)

    successes = sum(r.success for r in rows)
    # NLDI has a fallback to WBD; both count as success.
    assert successes >= 7, (
        f"Pour-point delineation succeeded for only {successes}/{len(rows)} locations; "
        f"failures: {[r.name for r in rows if not r.success]}"
    )

    slow_warm = [r for r in rows if r.success and r.warm_s is not None and r.warm_s > 1.0]
    assert not slow_warm, (
        f"Cache reads unexpectedly slow (>1 s): {[r.name for r in slow_warm]}"
    )


# ---------------------------------------------------------------------------
# Side-by-side: both paths for each location in one pass
# ---------------------------------------------------------------------------

@pytest.mark.network
def test_watershed_latency_compare(tmp_path: pytest.TempPathFactory) -> None:
    """Compare HUC-trace vs pour-point cold latency side-by-side for all 10 locations."""

    @dataclass
    class _CompRow:
        name: str
        region: str
        huc_s: float | None = None
        huc_area: float | None = None
        huc_method: str | None = None
        huc_err: str | None = None
        pp_s: float | None = None
        pp_area: float | None = None
        pp_method: str | None = None
        pp_err: str | None = None

    settings = Settings(data_dir=tmp_path)
    rows: list[_CompRow] = []

    for loc in LOCATIONS:
        row = _CompRow(name=loc["name"], region=loc["region"])
        lat, lon = loc["lat"], loc["lon"]

        t0 = time.perf_counter()
        try:
            trace = resolve_and_trace_cached(lat=lat, lon=lon, settings=settings, refresh=True)
            row.huc_s = time.perf_counter() - t0
            row.huc_area = trace.area_km2
            row.huc_method = trace.method
        except Exception as exc:
            row.huc_s = time.perf_counter() - t0
            row.huc_err = str(exc)[:45]

        t0 = time.perf_counter()
        try:
            basin = delineate_cached(lat=lat, lon=lon, settings=settings, refresh=True)
            row.pp_s = time.perf_counter() - t0
            row.pp_area = basin.area_km2
            row.pp_method = basin.method
        except Exception as exc:
            row.pp_s = time.perf_counter() - t0
            row.pp_err = str(exc)[:45]

        rows.append(row)

    col_hdr = (
        f"  {'Location':<26} {'Region':<20}"
        f"  {'HUC s':>6} {'HUC km²':>8} {'HUC method':<14}"
        f"  {'PP s':>6} {'PP km²':>8} {'PP method'}"
    )
    div = "-" * len(col_hdr)
    print(f"\n{'=' * len(col_hdr)}")
    print("  WATERSHED LATENCY COMPARISON — cold (no cache), both paths")
    print("=" * len(col_hdr))
    print(col_hdr)
    print(div)
    for r in rows:
        huc_t = f"{r.huc_s:6.2f}" if r.huc_s and not r.huc_err else "  FAIL"
        huc_a = f"{r.huc_area:8.0f}" if r.huc_area else f"{'—':>8}"
        huc_m = (r.huc_method or r.huc_err or "—")[:13]
        pp_t = f"{r.pp_s:6.2f}" if r.pp_s and not r.pp_err else "  FAIL"
        pp_a = f"{r.pp_area:8.0f}" if r.pp_area else f"{'—':>8}"
        pp_m = (r.pp_method or r.pp_err or "—")[:14]
        print(
            f"  {r.name:<26} {r.region:<20}"
            f"  {huc_t} {huc_a} {huc_m:<14}"
            f"  {pp_t} {pp_a} {pp_m}"
        )
    print(div)
    huc_times = [r.huc_s for r in rows if r.huc_s and not r.huc_err]
    pp_times = [r.pp_s for r in rows if r.pp_s and not r.pp_err]
    if huc_times:
        print(
            f"  HUC trace  — median {statistics.median(huc_times):.2f}s"
            f"  max {max(huc_times):.2f}s  (n={len(huc_times)})"
        )
    if pp_times:
        print(
            f"  Pour-point — median {statistics.median(pp_times):.2f}s"
            f"  max {max(pp_times):.2f}s  (n={len(pp_times)})"
        )
    print("=" * len(col_hdr) + "\n")
