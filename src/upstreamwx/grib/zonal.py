"""Aggregate a decoded GRIB field over a watershed polygon (shared SREF/HREF).

Appendix B evaluates ensemble probabilities "over the upstream domain". We
rasterize the polygon onto the field's native grid with regionmask and reduce
over the masked cells, reporting both the **max** (the conservative trigger the
tier logic uses) and the **areal mean**. The routine is grid-agnostic — it works
on the SREF ~16 km Lambert grid and the HREF ~3 km Lambert grid alike, because it
keys off each field's 2D ``latitude``/``longitude`` coordinates.

Edge case: a headwater HUC-12 can be smaller than a single grid cell, so zero
cells fall inside. We then fall back to nearest-cell sampling at the polygon
centroid and flag it, so the caller knows the value is point-like, not areal.
(This fallback fires far less often on the 3 km HREF grid than on 16 km SREF.)
The fallback is only valid when the polygon actually lies *on* the grid: a
polygon wholly outside the grid's bounds raises instead of silently returning an
unrelated edge cell's value (NFR-6 — degrade loudly, never fabricate a signal).

Data quality is first-class here: an all-NaN reduction (bitmap-masked region,
truncated subset) yields ``max_value``/``mean_value`` of ``None`` rather than a
NaN that would compare False against every threshold downstream and read as
"no hazard".
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
import regionmask
import xarray as xr
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry


@dataclass
class PolygonAggregate:
    """Result of aggregating one field over one polygon.

    ``max_value``/``mean_value`` are ``None`` when every masked cell is NaN — the
    caller must treat that as "no data", never as zero probability.
    """

    field_name: str
    threshold: str
    n_cells: int
    max_value: float | None
    mean_value: float | None
    fallback_nearest_cell: bool
    per_step: dict  # step label -> {"max":..., "mean":...} when a step dim exists

    def as_dict(self) -> dict:
        return {
            "field": self.field_name,
            "threshold": self.threshold,
            "n_cells": self.n_cells,
            "max": self.max_value,
            "mean": self.mean_value,
            "fallback_nearest_cell": self.fallback_nearest_cell,
            "per_step": self.per_step,
        }


def _as_geometry(polygon: BaseGeometry | dict) -> BaseGeometry:
    if isinstance(polygon, BaseGeometry):
        return polygon
    if isinstance(polygon, dict):
        # Accept a GeoJSON geometry or a Feature.
        if polygon.get("type") == "Feature":
            return shape(polygon["geometry"])
        return shape(polygon)
    raise TypeError(f"Unsupported polygon type: {type(polygon)!r}")


# Small bounded LRU of rasterized polygon masks, keyed by (grid fingerprint, polygon WKB).
# A briefing aggregates the same polygon over many fields on the same grid — HREF runs ~N
# forecast hours × ~4 fields — and the mask depends only on (grid, polygon), not the field
# values, so we compute it once per (SREF/HREF grid) instead of once per call. The WKB bytes
# are part of the key, so dict equality (not just the hash) disambiguates — no risk of a hash
# collision returning the wrong mask. Bounded because only a couple of grids are ever live.
_MASK_CACHE_MAX = 6
_mask_cache: OrderedDict[tuple, xr.DataArray] = OrderedDict()
_mask_lock = threading.Lock()


def _grid_fingerprint(da: xr.DataArray) -> tuple:
    """Cheap identity of a field's grid: shape + corner lon/lat (distinguishes SREF vs HREF)."""
    lon = da["longitude"].values.ravel()
    lat = da["latitude"].values.ravel()
    return (
        tuple(da["longitude"].shape),
        float(lon[0]), float(lon[-1]),
        float(lat[0]), float(lat[-1]),
    )


def _mask_for(da: xr.DataArray, geom: BaseGeometry) -> xr.DataArray:
    """Boolean mask (True inside polygon) on the field's 2D lat/lon grid, memoised per grid.

    regionmask rasterisation dominates :func:`aggregate_over_polygon`; memoising it per
    (grid, polygon) collapses a briefing's dozens of identical rasterisations to one per grid.
    Thread-safe (SREF and HREF aggregate concurrently); the rasterisation runs outside the lock
    so the two grids' masks build in parallel, and a rare double-build of the same key is
    harmless (idempotent).
    """
    key = (_grid_fingerprint(da), geom.wkb)
    with _mask_lock:
        hit = _mask_cache.get(key)
        if hit is not None:
            _mask_cache.move_to_end(key)
            return hit
    # regionmask handles 2D curvilinear lon/lat (the Lambert grids SREF/HREF use).
    mask = region_mask(da, geom)
    with _mask_lock:
        _mask_cache[key] = mask
        _mask_cache.move_to_end(key)
        while len(_mask_cache) > _MASK_CACHE_MAX:
            _mask_cache.popitem(last=False)
    return mask


def region_mask(da: xr.DataArray, geom: BaseGeometry) -> xr.DataArray:
    """Rasterize ``geom`` onto ``da``'s grid (True inside). The uncached primitive."""
    region = regionmask.Regions([geom])
    mask = region.mask(da["longitude"], da["latitude"])
    return mask == 0  # region index 0 inside, NaN outside


def _nan_reduce(values: np.ndarray) -> tuple[float | None, float | None]:
    """(max, mean) ignoring NaN cells; (None, None) when nothing finite remains.

    A NaN that escaped here would compare False against every threshold cut point
    downstream and silently read as "no hazard" — the anti-conservative direction.
    """
    if values.size == 0 or bool(np.isnan(values).all()):
        return None, None
    return float(np.nanmax(values)), float(np.nanmean(values))


def _off_grid(lat: float, lon: float, grid_lat: np.ndarray, grid_lon: np.ndarray) -> bool:
    """True when (lat, lon) lies beyond the grid's bounds (plus one cell of slack).

    A coarse guard, not sub-cell precision: it distinguishes "headwater smaller than a
    cell" (fallback is honest) from "domain nowhere near this grid" (fallback would
    fabricate a value). Longitude uses the shortest angular Δ so -180..180 and 0..360
    conventions both work.
    """
    lat_flat = grid_lat.ravel()
    dlon = np.abs((grid_lon.ravel() - lon + 180) % 360 - 180)
    # One cell of slack at the coarsest plausible spacing of the grids we handle (~0.5°).
    slack = 0.5
    lat_ok = float(lat_flat.min()) - slack <= lat <= float(lat_flat.max()) + slack
    lon_ok = float(dlon.min()) <= slack
    return not (lat_ok and lon_ok)


def aggregate_over_polygon(
    da: xr.DataArray,
    polygon: BaseGeometry | dict,
    field_name: str = "",
    threshold: str = "",
) -> PolygonAggregate:
    """Reduce ``da`` over the polygon; report max + areal mean (and per-step)."""
    geom = _as_geometry(polygon)
    inside = _mask_for(da, geom)
    n_cells = int(inside.sum())

    spatial_dims = [d for d in da.dims if d in ("y", "x", "latitude", "longitude")]
    step_dims = [d for d in da.dims if d not in spatial_dims]

    fallback = False
    if n_cells == 0:
        # Polygon smaller than a grid cell: nearest-cell at centroid — but only when the
        # centroid actually lies on the grid. A polygon wholly off the grid (a mission just
        # outside the REFS domain edge, a degenerate clip) must not be answered with an
        # unrelated edge cell's value; the caller treats the raise as "source unavailable
        # over this domain" rather than a fabricated signal (NFR-6).
        c = geom.centroid
        lat, lon = da["latitude"], da["longitude"]
        if _off_grid(c.y, c.x, lat.values, lon.values):
            raise ValueError(
                f"polygon centroid ({c.y:.3f}, {c.x:.3f}) lies outside the field grid; "
                "refusing nearest-cell fallback"
            )
        fallback = True
        # Shortest angular Δlon so the nearest-cell search is correct whether the
        # grid stores longitude as -180..180 (SREF/REFS) or 0..360 (GEFS).
        if lat.ndim == 2:
            # Curvilinear 2D lat/lon (the SREF/HREF/REFS Lambert grids) on (y, x): one
            # flat argmin over the squared great-circle-ish distance, then unravel.
            dlon = (lon - c.x + 180) % 360 - 180
            dist = (lat - c.y) ** 2 + dlon**2
            flat = int(np.argmin(dist.values))
            ny, nx = lat.shape
            iy, ix = divmod(flat, nx)
            sel = da.isel({spatial_dims[0]: iy, spatial_dims[1]: ix})
        else:
            # Regular 1D lat/lon grid (the GEFS global mesh): the nearest index along each
            # axis independently. The 2D path's ``lat.shape`` unpack assumes curvilinear
            # coords and raised ValueError here, sinking the whole GEFS source on any domain
            # too small to hold a 0.25° cell centre (a 24 km LAoC disk, a clipped RoC).
            iy = int(np.argmin(np.abs(lat.values - c.y)))
            dlon = (lon.values - c.x + 180) % 360 - 180
            ix = int(np.argmin(np.abs(dlon)))
            sel = da.isel({lat.dims[0]: iy, lon.dims[0]: ix})
        masked = sel
        max_value, mean_value = _nan_reduce(sel.values)
    else:
        masked = da.where(inside)
        max_value, mean_value = _nan_reduce(masked.values)

    per_step: dict = {}
    if step_dims and not fallback:
        step_dim = step_dims[0]
        for i in range(da.sizes[step_dim]):
            layer = masked.isel({step_dim: i})
            label = str(da[step_dim].values[i]) if step_dim in da.coords else str(i)
            step_max, step_mean = _nan_reduce(layer.values)
            per_step[label] = {"max": step_max, "mean": step_mean}

    return PolygonAggregate(
        field_name=field_name or str(da.name),
        threshold=threshold,
        n_cells=n_cells,
        max_value=max_value,
        mean_value=mean_value,
        fallback_nearest_cell=fallback,
        per_step=per_step,
    )
