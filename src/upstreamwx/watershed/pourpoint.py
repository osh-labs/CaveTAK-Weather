"""Pour-point watershed delineation via the NLDI raindrop trace (PRD FR-3).

Spike D (``docs/m0.0/spike-d-streamstats-report.md``) established that the right
way to delineate the *exact* upstream contributing area for an arbitrary activity
point is a two-step NLDI call:

  1. ``nldi-flowtrace`` — follow the 30 m NHDPlus V2 flow-direction grid downhill
     from the raw point until it hits a stream line (a hydrologically correct
     "raindrop" snap onto the network). This fixes the snap problem that defeats
     both the StreamStats str900 grid snap and a blind lat/lon nudge.
  2. ``nldi-splitcatchment`` — split the local NHD catchment at the snapped point
     and return the full upstream ``drainageBasin``.

This is pour-point exact, needs no state/region code, and is stateless. It
matches USGS StreamStats SS-Delineate to ~1 % (Spike D) while being faster and
dependency-light. When the NLDI path fails (network error, or a point that will
not snap), we fall back to the deterministic WBD HUC-12 ``tohuc`` trace
(:func:`upstreamwx.watershed.trace_upstream`) — coarser (it returns the whole
containing HUC-12 plus upstream units, over-including 24-54 % on the Spike D test
points) but snap-free and reproducible.

Both services are public USGS NLDI pygeoapi processes
(``api.water.usgs.gov/nldi/pygeoapi``), the same code packaged as the
``nldi-flowtools`` library.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests
from pyproj import Transformer
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

_NLDI_PROCESSES = "https://api.water.usgs.gov/nldi/pygeoapi/processes"

# Equal-area reprojection for km^2 (NAD83 / CONUS Albers), matching upstream.py.
_TO_ALBERS = Transformer.from_crs(4326, 5070, always_xy=True).transform

_MAX_ATTEMPTS = 3
_BACKOFF_BASE_S = 0.5

# The coarse WBD trace method tag. Named so the cache layer can recognise (and
# TTL-expire) fallback-quality entries without duplicating the string (H-9b).
WBD_FALLBACK_METHOD = "wbd-huc12-fallback"


@dataclass
class PourpointBasin:
    """The upstream contributing watershed delineated to an exact pour point."""

    lat: float  # raw query point (WGS84 / EPSG:4326)
    lon: float
    snapped_lat: float  # on-network point after the raindrop snap
    snapped_lon: float
    polygon: BaseGeometry  # upstream drainage basin, EPSG:4326
    area_km2: float
    method: str  # "nldi-raindrop-split" | WBD_FALLBACK_METHOD
    comid: int | None = None
    flowline_name: str | None = None
    notes: list[str] = field(default_factory=list)
    # Completeness contract (H-1): the NLDI-exact path validates its own basin
    # (degenerate results return None and fall back), so it stays True; the WBD
    # path propagates the upstream trace's truncation-risk flag + reasons.
    complete: bool = True
    completeness_notes: list[str] = field(default_factory=list)


@dataclass
class SnapResult:
    """The on-network point a raw coordinate snaps to via the raindrop trace."""

    lat: float
    lon: float
    comid: int | None
    flowline_name: str | None
    snapped: bool  # False if the raindrop path was empty (already on the network)


def _area_km2(geom: BaseGeometry) -> float:
    """Area in km^2 via an equal-area projection (EPSG:5070)."""
    return transform(_TO_ALBERS, geom).area / 1e6


def _is_transient(exc: requests.exceptions.RequestException) -> bool:
    """Transient = worth retrying: connection/timeout, or HTTP 429 / 5xx."""
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        return status == 429 or status >= 500
    return True  # ConnectionError, Timeout, etc.


def _execute_process(
    process: str,
    lat: float,
    lon: float,
    extra: dict[str, str],
    *,
    timeout: float = 60.0,
    attempts: int = _MAX_ATTEMPTS,
) -> dict:
    """POST a job to an NLDI pygeoapi process and return the GeoJSON result.

    The NLDI processes use the older OGC API - Processes body shape: a list of
    ``{id, value, type}`` input objects. Transient failures are retried with
    exponential backoff (NFR-6); a permanent client error (4xx other than 429)
    propagates immediately.
    """
    inputs = [
        {"id": "lat", "value": str(lat), "type": "text/plain"},
        {"id": "lon", "value": str(lon), "type": "text/plain"},
        *({"id": k, "value": v, "type": "text/plain"} for k, v in extra.items()),
    ]
    url = f"{_NLDI_PROCESSES}/{process}/execution"

    last_exc: requests.exceptions.RequestException | None = None
    for attempt in range(attempts):
        try:
            resp = requests.post(url, json={"inputs": inputs}, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as exc:
            if not _is_transient(exc):
                raise
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(_BACKOFF_BASE_S * 2**attempt)
    raise RuntimeError(f"NLDI {process} failed for ({lat}, {lon}): {last_exc}") from last_exc


def _feature(fc: dict, feature_id: str) -> dict | None:
    """Return the GeoJSON feature with the given ``id`` from a FeatureCollection."""
    for feat in fc.get("features", []):
        if feat.get("id") == feature_id:
            return feat
    return None


def raindrop_snap(lat: float, lon: float) -> SnapResult:
    """Snap a raw point onto the NHD network via the ``nldi-flowtrace`` raindrop path.

    The end of the ``raindropPath`` line is the on-network point; the
    ``nhdFlowline`` it intersects carries the COMID and stream name, which double
    as a sanity check (a non-degenerate basin on the expected main stem).
    """
    fc = _execute_process("nldi-flowtrace", lat, lon, {"direction": "none"})
    s_lat, s_lon, snapped = lat, lon, False
    rp = _feature(fc, "raindropPath")
    coords = (rp or {}).get("geometry", {}).get("coordinates") or []
    if coords:
        s_lon, s_lat = coords[-1][0], coords[-1][1]
        snapped = True
    props = (_feature(fc, "nhdFlowline") or {}).get("properties") or {}
    return SnapResult(
        lat=s_lat,
        lon=s_lon,
        comid=props.get("comid"),
        flowline_name=props.get("gnis_name"),
        snapped=snapped,
    )


def _split_catchment_basin(lat: float, lon: float) -> BaseGeometry | None:
    """Upstream ``drainageBasin`` polygon from ``nldi-splitcatchment``, or None."""
    fc = _execute_process("nldi-splitcatchment", lat, lon, {"upstream": "True"})
    feat = _feature(fc, "drainageBasin")
    if feat is None or not feat.get("geometry"):
        return None  # missing drainageBasin == the split failed at the snapped point
    return shape(feat["geometry"])


def delineate_pourpoint(lat: float, lon: float) -> PourpointBasin | None:
    """Delineate the exact upstream basin via raindrop snap + split-catchment.

    Returns ``None`` (rather than raising) if the NLDI path cannot produce a
    usable basin, so callers can fall back. Transport errors that survive the
    retrying executor are caught here too, so a single attempt degrades cleanly.
    """
    try:
        snap = raindrop_snap(lat, lon)
        polygon = _split_catchment_basin(snap.lat, snap.lon)
    except Exception:  # noqa: BLE001 - any failure degrades to the fallback path
        return None
    if polygon is None or polygon.is_empty or not polygon.is_valid:
        return None
    notes: list[str] = []
    if not snap.snapped:
        notes.append("raindrop path empty; used raw point (already on the network?)")
    return PourpointBasin(
        lat=lat,
        lon=lon,
        snapped_lat=snap.lat,
        snapped_lon=snap.lon,
        polygon=polygon,
        area_km2=_area_km2(polygon),
        method="nldi-raindrop-split",
        comid=snap.comid,
        flowline_name=snap.flowline_name,
        notes=notes,
    )


def _wbd_fallback(lat: float, lon: float) -> PourpointBasin:
    """Coarse, snap-free fallback: the deterministic WBD HUC-12 ``tohuc`` trace."""
    from .huc import resolve_huc12
    from .upstream import trace_upstream

    trace = trace_upstream(resolve_huc12(lat, lon))
    notes = [
        "WBD HUC-12 fallback: whole-HUC-12 contributing area (coarse, over-inclusive "
        "vs the exact pour point)",
        f"origin HUC-12 {trace.origin_huc12}, {len(trace.upstream_huc_ids)} upstream units",
        *trace.notes,
    ]
    return PourpointBasin(
        lat=lat,
        lon=lon,
        snapped_lat=lat,
        snapped_lon=lon,
        polygon=trace.polygon,
        area_km2=trace.area_km2,
        method=WBD_FALLBACK_METHOD,
        comid=None,
        flowline_name=None,
        notes=notes,
        complete=trace.complete,
        completeness_notes=list(trace.completeness_notes),
    )


def delineate(lat: float, lon: float, *, allow_fallback: bool = True) -> PourpointBasin:
    """Delineate the upstream contributing watershed for a point.

    Tries the pour-point-exact NLDI raindrop two-step first; on failure falls back
    to the deterministic (coarser) WBD HUC-12 trace unless ``allow_fallback`` is
    False.

    Raises:
        ValueError: if pour-point delineation fails and the fallback is disabled
            or also fails.
    """
    basin = delineate_pourpoint(lat, lon)
    if basin is not None:
        return basin
    if allow_fallback:
        return _wbd_fallback(lat, lon)
    raise ValueError(f"pour-point delineation failed for ({lat}, {lon}) and fallback disabled")
