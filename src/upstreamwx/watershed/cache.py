"""On-disk cache for HUC resolution + upstream tracing (M0.1 promotion of Spike B).

The live USGS WBD trace is deterministic but slow (≈3 s for compact basins, up to
≈15 s for plains rivers needing region widening). M0.1 promotes it to a cached
module: a point's dissolved upstream domain is resolved once and reused. The cache
is a GeoJSON Feature per rounded lat/lon under ``Settings.data_dir/watershed``.

Note: this is a *process/disk* cache for on-demand use; the persistent
cross-restart cache the scheduler relies on is an M0.1.1 / EC2 concern.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from shapely.geometry import mapping, shape

from ..config import Settings, get_settings
from .huc import resolve_huc12
from .upstream import UpstreamTrace, trace_upstream

# Rounding precision for the cache key (~100 m at CONUS latitudes is plenty for a
# HUC-12 domain, which is far larger).
_KEY_PRECISION = 3


def _cache_dir(settings: Settings) -> Path:
    d = settings.data_dir / "watershed"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _key(lat: float, lon: float) -> str:
    return f"{round(lat, _KEY_PRECISION)}_{round(lon, _KEY_PRECISION)}"


def _to_feature(trace: UpstreamTrace) -> dict:
    return {
        "type": "Feature",
        "geometry": mapping(trace.polygon),
        "properties": {
            "origin_huc12": trace.origin_huc12,
            "upstream_huc_ids": trace.upstream_huc_ids,
            "area_km2": trace.area_km2,
            "method": trace.method,
            "huc_level": trace.huc_level,
            "notes": trace.notes,
        },
    }


def _from_feature(feature: dict) -> UpstreamTrace:
    props = feature["properties"]
    return UpstreamTrace(
        origin_huc12=props["origin_huc12"],
        upstream_huc_ids=props["upstream_huc_ids"],
        polygon=shape(feature["geometry"]),
        area_km2=props["area_km2"],
        method=props["method"],
        huc_level=props.get("huc_level", 12),
        notes=props.get("notes", []),
    )


def _resolve_and_trace(lat: float, lon: float, *, retries: int = 3) -> UpstreamTrace:
    """Live resolve + trace with simple backoff on transient USGS failures."""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return trace_upstream(resolve_huc12(lat, lon))
        except Exception as exc:  # transient WFS 403/502, timeouts (Spike B notes)
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"watershed trace failed for {lat},{lon}: {last_exc}") from last_exc


def resolve_and_trace_cached(
    lat: float,
    lon: float,
    *,
    settings: Settings | None = None,
    refresh: bool = False,
) -> UpstreamTrace:
    """Return the upstream trace for a point, using the on-disk cache when present."""
    settings = settings or get_settings()
    path = _cache_dir(settings) / f"{_key(lat, lon)}.geojson"

    if path.is_file() and not refresh:
        return _from_feature(json.loads(path.read_text()))

    trace = _resolve_and_trace(lat, lon)
    path.write_text(json.dumps(_to_feature(trace)))
    return trace
