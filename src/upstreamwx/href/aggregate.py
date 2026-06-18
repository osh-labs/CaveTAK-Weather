"""Polygon aggregation of an HREF field (re-export of the shared primitive).

The zonal reduction is grid-agnostic and shared with SREF, so it lives in
:mod:`upstreamwx.grib.zonal`. Re-exported here so the HREF module mirrors the SREF
import surface (``upstreamwx.href.aggregate``).
"""

from __future__ import annotations

from ..grib.zonal import PolygonAggregate, aggregate_over_polygon

__all__ = ["PolygonAggregate", "aggregate_over_polygon"]
