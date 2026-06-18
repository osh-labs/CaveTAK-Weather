"""Spike A CLI — SREF ensemble probabilities aggregated over a watershed polygon.

Given an upstream-watershed polygon (e.g. Spike B's output), this:
  1. finds the latest live SREF cycle on NOMADS,
  2. pulls a handful of probability fields via ``.idx`` byte-range subsetting,
  3. aggregates each over the polygon (max + areal mean), and
  4. prints a summary plus a resource profile.

Run::

    .venv/bin/python spikes/spike_a_sref/run_spike_a.py \
        --polygon tests/fixtures/buckskin_huc12.geojson
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import time

import geopandas as gpd
import numpy as np
from shapely.ops import unary_union

from cavetak_weather.sref import (
    aggregate_over_polygon,
    latest_available_cycle,
    load_probability_field,
)

# Fields mapped to Appendix B logic: flash-flood P(precip) thresholds (mm/3h) and
# lightning/convective proxies via CAPE probabilities.
FIELDS = [
    ("P(precip>0.25mm/3h)", "APCP", ">0.25", "0-3 hour acc"),
    ("P(precip>6.35mm/3h ~0.25in)", "APCP", ">6.35", "0-3 hour acc"),
    ("P(precip>12.7mm/3h ~0.5in slot)", "APCP", ">12.7", "0-3 hour acc"),
    ("P(CAPE>500 J/kg)", "CAPE", ">500", None),
    ("P(CAPE>1000 J/kg)", "CAPE", ">1000", None),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Spike A: SREF over a polygon")
    parser.add_argument(
        "--polygon",
        default="tests/fixtures/buckskin_huc12.geojson",
        help="Path to a (multi)polygon GeoJSON of the upstream watershed domain.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)

    poly = unary_union(gpd.read_file(args.polygon).geometry.values)

    t0 = time.perf_counter()
    cycle = latest_available_cycle()
    if cycle is None:
        print("ERROR: no SREF cycle available on NOMADS")
        return 1
    cycle_dt = time.perf_counter() - t0

    results = []
    total_bytes = 0
    for label, var, prob, fcst in FIELDS:
        t = time.perf_counter()
        try:
            f = load_probability_field(cycle, var=var, prob=prob, fcst=fcst)
        except LookupError as exc:
            results.append({"field": label, "error": str(exc)})
            continue
        size = os.path.getsize(f.grib_path)
        total_bytes += size
        agg = aggregate_over_polygon(f.data, poly, field_name=label, threshold=prob)
        conus_max = float(np.nanmax(f.data.values))
        results.append(
            {
                **agg.as_dict(),
                "field": label,
                "messages": f.descriptor_count,
                "subset_kb": round(size / 1024, 1),
                "conus_max": round(conus_max, 1),
                "seconds": round(time.perf_counter() - t, 2),
            }
        )

    profile = {
        "cycle": f"{cycle.date}/{cycle.hh}Z",
        "cycle_lookup_s": round(cycle_dt, 2),
        "total_subset_kb": round(total_bytes / 1024, 1),
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
    }

    if args.json:
        print(json.dumps({"profile": profile, "results": results}, indent=2, default=str))
        return 0

    print(f"\nSREF cycle: {profile['cycle']}  (polygon: {args.polygon})")
    print(f"{'field':34} {'msgs':>4} {'KB':>7} {'poly_max':>9} {'poly_mean':>9} "
          f"{'cells':>5} {'CONUS_max':>9}")
    for r in results:
        if "error" in r:
            print(f"{r['field']:34} ERROR: {r['error']}")
            continue
        flag = " *nearest-cell" if r.get("fallback_nearest_cell") else ""
        print(f"{r['field']:34} {r['messages']:>4} {r['subset_kb']:>7.0f} "
              f"{r['max']:>8.1f}% {r['mean']:>8.1f}% {r['n_cells']:>5} "
              f"{r['conus_max']:>8.1f}%{flag}")
    print(f"\nresource profile: total subset {profile['total_subset_kb']:.0f} KB, "
          f"peak RSS {profile['peak_rss_mb']:.0f} MB, cycle lookup {profile['cycle_lookup_s']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
