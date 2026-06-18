"""Source-agnostic GRIB2 helpers shared by the SREF and HREF processors.

The ``.idx`` byte-range subsetting machinery (:mod:`upstreamwx.grib.idx`) and the
polygon zonal-aggregation (:mod:`upstreamwx.grib.zonal`) are identical for any
NOMADS ``ensprod`` product, so they live here and both ensemble modules re-export
them. Extracted from the SREF spike (M0.0) when HREF was added as the same-day
high-resolution supplement.
"""

from .idx import (
    IdxEntry,
    download_subset,
    fetch_idx,
    parse_idx,
    select_messages,
)
from .zonal import PolygonAggregate, aggregate_over_polygon

__all__ = [
    "IdxEntry",
    "parse_idx",
    "fetch_idx",
    "select_messages",
    "download_subset",
    "PolygonAggregate",
    "aggregate_over_polygon",
]
