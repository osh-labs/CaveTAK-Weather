"""HREF ensemble retrieval, extraction, and polygon aggregation (PRD §6.2 FR-7a).

Spike C home — the same-day (~36 h) high-resolution supplement to the SREF
processor. HREF is NCEP's ~3 km convection-allowing ensemble; its neighborhood
probabilities sharpen the flash-flood and lightning signal inside the same-day
window, while SREF (~16 km) still owns the longer planning horizon. Pipeline
mirrors SREF, over shared GRIB primitives:

    sources -> fetch (idx byte-range subset) -> extract (cfgrib) -> aggregate (polygon)
"""

from ..grib.zonal import PolygonAggregate, aggregate_over_polygon
from .extract import HrefField, accum_window, load_probability_field, open_subset
from .sources import (
    HrefCycle,
    iter_recent_cycles,
    latest_available_cycle,
    probe_sources,
)

__all__ = [
    "HrefCycle",
    "iter_recent_cycles",
    "latest_available_cycle",
    "probe_sources",
    "HrefField",
    "accum_window",
    "load_probability_field",
    "open_subset",
    "PolygonAggregate",
    "aggregate_over_polygon",
]
