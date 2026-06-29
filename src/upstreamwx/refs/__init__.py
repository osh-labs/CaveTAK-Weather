"""REFS ensemble retrieval, extraction, and polygon aggregation (PRD §6.2 FR-7a).

The Rapid Ensemble Forecast System (REFS, the RRFS Ensemble) is NCEP's ~3 km
convection-allowing ensemble and the **replacement for HREF** (terminated 2026-08-31,
NWS SCN 26-47). Its neighborhood probabilities sharpen the flash-flood and lightning
signal inside the same-day window, while GEFS (~0.5°) owns the longer planning horizon.
Pipeline mirrors HREF, over shared GRIB primitives:

    sources -> fetch (idx byte-range subset) -> extract (cfgrib) -> aggregate (polygon)
"""

from ..grib.zonal import PolygonAggregate, aggregate_over_polygon
from .cache import (
    DEFAULT_FIELDS,
    FieldSpec,
    load_probability_field_cached,
    prune_old_cycles,
    warm_cycle,
)
from .extract import RefsField, accum_window, load_probability_field, open_subset
from .sources import (
    REFS_FHOURS,
    RefsCycle,
    iter_recent_cycles,
    latest_available_cycle,
    probe_sources,
)

__all__ = [
    "RefsCycle",
    "REFS_FHOURS",
    "iter_recent_cycles",
    "latest_available_cycle",
    "probe_sources",
    "RefsField",
    "accum_window",
    "load_probability_field",
    "load_probability_field_cached",
    "open_subset",
    "PolygonAggregate",
    "aggregate_over_polygon",
    "DEFAULT_FIELDS",
    "FieldSpec",
    "warm_cycle",
    "prune_old_cycles",
]
