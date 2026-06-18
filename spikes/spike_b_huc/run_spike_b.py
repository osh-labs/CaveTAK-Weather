"""Spike B CLI: lat/lon -> containing HUC-12 -> dissolved upstream watershed.

Example:
    .venv/bin/python spikes/spike_b_huc/run_spike_b.py \
        --lat 37.0192 --lon -111.9889 --name "Buckskin Gulch" \
        --out tests/fixtures/buckskin_huc12.geojson

Uses live USGS WBD / NLDI services (PRD FR-2, FR-3).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import geopandas as gpd

from cavetak_weather.watershed import HucResult, UpstreamTrace, resolve_huc12, trace_upstream


def run(lat: float, lon: float, name: str | None = None) -> tuple[HucResult, UpstreamTrace, float]:
    """Resolve the HUC and trace upstream; returns (huc, trace, latency_seconds)."""
    start = time.perf_counter()
    huc = resolve_huc12(lat, lon)
    trace = trace_upstream(huc)
    latency = time.perf_counter() - start
    return huc, trace, latency


def _write_geojson(trace: UpstreamTrace, out: Path, simplify_m: float | None = None) -> None:
    gdf = gpd.GeoDataFrame(
        {
            "origin_huc12": [trace.origin_huc12],
            "huc_level": [trace.huc_level],
            "n_upstream": [len(trace.upstream_huc_ids)],
            "area_km2": [round(trace.area_km2, 3)],
            "method": [trace.method],
        },
        geometry=[trace.polygon],
        crs=4326,
    )
    if simplify_m:
        geom = gdf.to_crs(5070).geometry.simplify(simplify_m).to_crs(4326)
        gdf = gdf.set_geometry(geom)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(gdf.to_json(drop_id=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Spike B: HUC-12 + upstream watershed trace")
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--name", type=str, default=None)
    parser.add_argument("--out", type=Path, default=None, help="GeoJSON output path")
    parser.add_argument(
        "--simplify",
        type=float,
        default=None,
        help="simplify tolerance in metres (EPSG:5070) before writing",
    )
    args = parser.parse_args(argv)

    huc, trace, latency = run(args.lat, args.lon, args.name)

    summary = {
        "name": args.name,
        "lat": args.lat,
        "lon": args.lon,
        "huc_id": huc.huc_id,
        "huc_level": huc.huc_level,
        "huc_name": huc.name,
        "n_upstream_huc": len(trace.upstream_huc_ids),
        "area_km2": round(trace.area_km2, 2),
        "method": trace.method,
        "latency_s": round(latency, 2),
        "notes": trace.notes,
    }
    print(json.dumps(summary, indent=2))

    if args.out:
        _write_geojson(trace, args.out, simplify_m=args.simplify)
        print(f"wrote upstream polygon -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
