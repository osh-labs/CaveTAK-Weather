"""Polygon aggregation of a SREF field (re-export of the shared primitive).

The zonal reduction is grid-agnostic and shared with HREF, so it lives in
:mod:`upstreamwx.grib.zonal`. This module preserves the historical
``upstreamwx.sref.aggregate`` import surface used by Spike A and its tests.
"""

from __future__ import annotations

from ..grib.zonal import PolygonAggregate, aggregate_over_polygon

__all__ = ["PolygonAggregate", "aggregate_over_polygon"]
