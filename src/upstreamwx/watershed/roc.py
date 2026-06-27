"""Radius-of-Concern clipping for the upstream watershed (PRD FR-3, §12).

The upstream contributing watershed can sprawl far beyond a party's operational
exposure — for a point near a large river system it may reach hundreds of miles
upstream. A mission-level **Radius of Concern** (RoC) caps that domain: the full
watershed is delineated, then clipped to a disk of the user-specified radius
centered on the mission origin, and the *clipped* polygon is what the SREF/HREF
zonal aggregation runs over.

This is pure pre-ingest geometry. It never imports a provider or touches the
deterministic engine boundary (FR-13, §12), and identical inputs yield identical
output (NFR-4). Distances use an azimuthal-equidistant (AEQD) projection centered
on the point so the disk is a true circle of ``radius_km`` even at 200 mi; areas
reuse the EPSG:5070 equal-area convention shared with ``pourpoint.py`` / ``upstream.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from pyproj import Transformer
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform

# Equal-area reprojection for km^2 (NAD83 / CONUS Albers), matching pourpoint.py / upstream.py.
_TO_ALBERS = Transformer.from_crs(4326, 5070, always_xy=True).transform

# Segments per quarter-circle on the disk boundary; 32 (128 segments) is a smooth ring
# even at 200 mi while keeping the per-briefing GeoJSON payload modest.
_DISK_QUAD_SEGS = 32


@dataclass(frozen=True)
class ClipResult:
    """The outcome of clipping a watershed polygon to a Radius-of-Concern disk."""

    kept: BaseGeometry              # watershed ∩ disk, EPSG:4326 (the aggregation domain)
    excluded: BaseGeometry | None   # watershed − disk, EPSG:4326; None when basin ⊆ disk
    disk: BaseGeometry              # the RoC disk, EPSG:4326 (the dashed ring on the map)
    kept_area_km2: float


def _aeqd_transformers(lat: float, lon: float) -> tuple:
    """Forward/inverse transforms for an AEQD CRS centered on ``(lat, lon)``.

    Azimuthal-equidistant (not the equal-area Albers used for *areas*) so distances
    measured radially from the center are true — the disk is a real circle of the
    requested radius rather than a distorted ellipse at high radii.
    """
    crs = f"+proj=aeqd +lat_0={lat} +lon_0={lon} +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
    fwd = Transformer.from_crs(4326, crs, always_xy=True).transform
    inv = Transformer.from_crs(crs, 4326, always_xy=True).transform
    return fwd, inv


def roc_disk(lat: float, lon: float, radius_km: float) -> BaseGeometry:
    """Build the Radius-of-Concern disk as an EPSG:4326 polygon (true-distance circle)."""
    _, inv = _aeqd_transformers(lat, lon)
    disk_local = Point(0.0, 0.0).buffer(radius_km * 1000.0, quad_segs=_DISK_QUAD_SEGS)
    return shp_transform(inv, disk_local)


def clip_watershed(
    polygon: BaseGeometry, lat: float, lon: float, radius_km: float
) -> ClipResult:
    """Clip ``polygon`` to the RoC disk; return kept/excluded/disk + the kept area (km²).

    ``kept`` falls back to the full polygon if the intersection is empty or degenerate
    (defensive — a valid basin always contains its own pour point, the disk center).
    ``excluded`` is ``None`` when the basin already fits inside the disk (nothing to hatch).
    """
    disk = roc_disk(lat, lon, radius_km)
    kept = polygon.intersection(disk)
    if kept.is_empty or kept.area <= 0:
        kept = polygon
    excluded = polygon.difference(disk)
    if excluded.is_empty or excluded.area <= 0:
        excluded = None
    kept_area_km2 = float(shp_transform(_TO_ALBERS, kept).area / 1e6)
    return ClipResult(kept=kept, excluded=excluded, disk=disk, kept_area_km2=kept_area_km2)
