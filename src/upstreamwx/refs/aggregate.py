"""Polygon aggregation of a REFS field (re-export of the shared primitive).

The zonal reduction is grid-agnostic and shared across the ensembles, so it lives in
:mod:`upstreamwx.grib.zonal`. Re-exported here so the REFS module mirrors the HREF import
surface (``upstreamwx.refs.aggregate``).
"""

from __future__ import annotations

from ..grib.zonal import PolygonAggregate, aggregate_over_polygon

__all__ = ["PolygonAggregate", "aggregate_over_polygon"]
