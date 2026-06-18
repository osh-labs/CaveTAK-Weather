"""lat/lon -> containing HUC-12 via live USGS WBD (PRD FR-2).

Primary path: the HyRiver ``pynhd.WaterData`` "wbd12" layer, a point spatial
query against the USGS Watershed Boundary Dataset served from
hydro.nationalmap.gov. Falls back to the "wbd10" (HUC-10) layer if no HUC-12
covers the point.

The WBD attribute names are lowercase (``huc12``, ``tohuc``, ``areasqkm``); we
still normalise field lookups defensively so this keeps working if the service
casing changes.
"""

from __future__ import annotations

from dataclasses import dataclass

from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

# WBD layer names exposed by pynhd.WaterData, ordered HUC-12 first then the
# HUC-10 fallback. (level, layer, id-field-candidates)
_WBD_LAYERS = (
    (12, "wbd12", ("huc12",)),
    (10, "wbd10", ("huc10",)),
)


def _field(row, candidates: tuple[str, ...]) -> str | None:
    """Case-insensitive lookup of the first matching attribute in a GeoDataFrame row."""
    lower = {str(k).lower(): k for k in row.index}
    for cand in candidates:
        key = lower.get(cand.lower())
        if key is not None and row[key] is not None:
            return str(row[key])
    return None


@dataclass
class HucResult:
    """A resolved hydrologic unit containing a query point."""

    huc_id: str
    huc_level: int  # 12 or 10
    geometry: BaseGeometry
    lat: float
    lon: float
    name: str | None = None
    tohuc: str | None = None
    area_km2: float | None = None
    source: str = "USGS WBD (pynhd.WaterData)"


def resolve_huc12(lat: float, lon: float) -> HucResult:
    """Resolve the HUC-12 (or HUC-10 fallback) containing ``(lat, lon)``.

    Args:
        lat: Latitude in decimal degrees (WGS84 / EPSG:4326).
        lon: Longitude in decimal degrees.

    Returns:
        A :class:`HucResult` for the smallest available containing hydrologic unit.

    Raises:
        ValueError: if neither a HUC-12 nor a HUC-10 can be resolved.
    """
    from pynhd import WaterData

    point = Point(lon, lat)
    # A tiny buffer makes the geometry query robust to the point landing on a
    # shared HU boundary; we then pick the polygon that actually contains it.
    query_geom = point.buffer(1e-4)

    last_err: Exception | None = None
    for level, layer, id_fields in _WBD_LAYERS:
        try:
            gdf = WaterData(layer).bygeom(query_geom)
        except Exception as exc:  # noqa: BLE001 - try next layer / fallback
            last_err = exc
            continue
        if gdf is None or gdf.empty:
            continue

        gdf = gdf.to_crs(4326)
        containing = gdf[gdf.geometry.contains(point)]
        row = (containing if not containing.empty else gdf).iloc[0]

        huc_id = _field(row, id_fields)
        if huc_id is None:
            continue

        area = _field(row, ("areasqkm",))
        return HucResult(
            huc_id=huc_id,
            huc_level=level,
            geometry=row.geometry,
            lat=lat,
            lon=lon,
            name=_field(row, ("name",)),
            tohuc=_field(row, ("tohuc",)),
            area_km2=float(area) if area is not None else None,
        )

    raise ValueError(
        f"Could not resolve a HUC-12 or HUC-10 for ({lat}, {lon}) from USGS WBD"
        + (f": {last_err}" if last_err else "")
    )
