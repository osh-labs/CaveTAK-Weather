"""Open GEFS member GRIB2 subsets, crop to a domain, and decode (SREF replacement).

GEFS ships **per-member grids only** (no pre-computed probability product), so unlike SREF we
read one member's field at a time and compute exceedance across members ourselves
(:mod:`upstreamwx.ingest.gefs_provider`). GEFS is a **global** regular lat/lon grid with
longitude in 0-360 and descending latitude; this module crops to the polygon's neighborhood and
shifts longitude to [-180, 180) so the shared :mod:`upstreamwx.grib.zonal` aggregation (which
keys off the field's 2D lat/lon) matches the watershed polygon's frame.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import xarray as xr
from shapely.geometry.base import BaseGeometry


@dataclass
class GefsField:
    """A decoded GEFS member field, cropped to the domain neighborhood."""

    name: str
    member: str
    fhour: int
    data: xr.DataArray  # 2D lat/lon, cropped + longitude-normalized
    grib_path: Path
    extras: dict = field(default_factory=dict)


def threshold_value(prob: str) -> float:
    """Parse a numeric threshold from a ``">12.7"`` / ``">1000"`` style token."""
    return float(prob.lstrip("><=").strip())


def open_subset(grib_path: str | Path) -> xr.Dataset:
    """Open a GRIB2 subset with cfgrib (no on-disk index written)."""
    return xr.open_dataset(grib_path, engine="cfgrib", backend_kwargs={"indexpath": ""})


def _primary_dataarray(ds: xr.Dataset) -> xr.DataArray:
    """Return the single (largest) data variable from a homogeneous subset dataset."""
    names = list(ds.data_vars)
    if not names:
        raise ValueError("GRIB subset contains no data variables")
    names.sort(key=lambda n: ds[n].size, reverse=True)
    return ds[names[0]]


def crop_and_normalize(
    da: xr.DataArray, poly: BaseGeometry, *, margin: float = 1.0
) -> xr.DataArray:
    """Crop a global 0-360 GEFS grid to the polygon's neighborhood and shift lon to [-180, 180).

    Crops with 0-360 bounds first (a monotonic, cheap slice — avoids masking ~1 M global points),
    then reassigns longitude to -180..180 so regionmask matches the polygon's frame. Latitude is
    descending on GEFS, so the slice runs north->south. Mirrors the Spike F prototype.
    """
    minx, miny, maxx, maxy = poly.bounds
    lo, hi = (minx - margin) % 360, (maxx + margin) % 360
    da = da.sel(longitude=slice(lo, hi), latitude=slice(maxy + margin, miny - margin))
    da = da.assign_coords(longitude=(((da["longitude"] + 180) % 360) - 180))
    return da
