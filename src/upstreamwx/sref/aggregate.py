"""Aggregate a SREF field over a watershed polygon (Spike A).

Appendix B evaluates SREF probabilities "over the upstream domain". We rasterize
the polygon onto the native SREF grid with regionmask and reduce over the masked
cells, reporting both the **max** (conservative trigger used by the tier logic)
and the **areal mean**.

Edge case: a headwater HUC-12 can be smaller than a single ~16 km SREF cell, so
zero cells fall inside. We then fall back to nearest-cell sampling at the polygon
centroid and flag it, so the caller knows the value is point-like, not areal.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import regionmask
import xarray as xr
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry


@dataclass
class PolygonAggregate:
    """Result of aggregating one field over one polygon."""

    field_name: str
    threshold: str
    n_cells: int
    max_value: float
    mean_value: float
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


def _mask_for(da: xr.DataArray, geom: BaseGeometry) -> xr.DataArray:
    """Boolean mask (True inside polygon) on the field's 2D lat/lon grid."""
    region = regionmask.Regions([geom])
    # regionmask handles 2D curvilinear lon/lat (SREF Lambert grid).
    mask = region.mask(da["longitude"], da["latitude"])
    return mask == 0  # region index 0 inside, NaN outside


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
        # Polygon smaller than a grid cell: nearest-cell at centroid.
        fallback = True
        c = geom.centroid
        dist = (da["latitude"] - c.y) ** 2 + (da["longitude"] - c.x) ** 2
        flat = int(np.argmin(dist.values))
        ny, nx = da["latitude"].shape
        iy, ix = divmod(flat, nx)
        sel = da.isel({spatial_dims[0]: iy, spatial_dims[1]: ix}) if len(
            spatial_dims
        ) == 2 else da
        masked = sel
        max_value = float(np.nanmax(sel.values))
        mean_value = float(np.nanmean(sel.values))
    else:
        masked = da.where(inside)
        max_value = float(masked.max().values)
        mean_value = float(masked.mean().values)

    per_step: dict = {}
    if step_dims and not fallback:
        step_dim = step_dims[0]
        for i in range(da.sizes[step_dim]):
            layer = masked.isel({step_dim: i})
            label = str(da[step_dim].values[i]) if step_dim in da.coords else str(i)
            per_step[label] = {
                "max": float(layer.max().values),
                "mean": float(layer.mean().values),
            }

    return PolygonAggregate(
        field_name=field_name or str(da.name),
        threshold=threshold,
        n_cells=n_cells,
        max_value=max_value,
        mean_value=mean_value,
        fallback_nearest_cell=fallback,
        per_step=per_step,
    )
