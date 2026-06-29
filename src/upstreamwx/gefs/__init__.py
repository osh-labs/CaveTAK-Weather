"""GEFS ensemble retrieval, member-exceedance extraction, and polygon aggregation (PRD §6.2).

The Global Ensemble Forecast System (GEFS) is NWS's recommended **replacement for SREF**
(terminated 2026-08-31, NWS SCN 26-47). Unlike SREF/HREF/REFS it ships **per-member grids only**,
so the probability is computed in-house as the member-exceedance fraction over the upstream
domain. GEFS is the coarse, global ensemble for the longer planning horizon and the backstop
beyond REFS range. Pipeline:

    sources -> fetch (idx byte-range subset, per member) -> extract (cfgrib, crop+normalize)
            -> member-exceedance over polygon (ingest.gefs_provider)
"""

from ..grib.zonal import PolygonAggregate, aggregate_over_polygon
from .cache import (
    DEFAULT_FIELDS,
    FieldSpec,
    cached_cycles,
    load_member_field_cached,
    prune_old_cycles,
    warm_cycle,
)
from .extract import GefsField, crop_and_normalize, open_subset, threshold_value
from .sources import (
    MEMBERS,
    GefsCycle,
    iter_recent_cycles,
    latest_available_cycle,
    probe_sources,
)

__all__ = [
    "GefsCycle",
    "MEMBERS",
    "iter_recent_cycles",
    "latest_available_cycle",
    "probe_sources",
    "GefsField",
    "crop_and_normalize",
    "threshold_value",
    "open_subset",
    "load_member_field_cached",
    "cached_cycles",
    "PolygonAggregate",
    "aggregate_over_polygon",
    "DEFAULT_FIELDS",
    "FieldSpec",
    "warm_cycle",
    "prune_old_cycles",
]
