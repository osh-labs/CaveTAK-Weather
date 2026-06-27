"""SREF ensemble retrieval, extraction, and polygon aggregation (PRD §6.2, §7, §12).

Spike A home; promoted to the scheduled SREF processor in M0.1 (the heaviest
backend component, PRD §11.2). Pipeline:

    sources -> fetch (idx byte-range subset) -> extract (cfgrib) -> aggregate (polygon)
"""

from .aggregate import PolygonAggregate, aggregate_over_polygon
from .cache import (
    DEFAULT_FIELDS,
    load_probability_field_cached,
    prune_old_cycles,
    warm_cycle,
)
from .extract import SrefField, load_probability_field, open_subset
from .fetch import IdxEntry, download_subset, fetch_idx, parse_idx, select_messages
from .sources import (
    SrefCycle,
    latest_available_cycle,
    probe_sources,
)

__all__ = [
    "SrefCycle",
    "latest_available_cycle",
    "probe_sources",
    "IdxEntry",
    "parse_idx",
    "fetch_idx",
    "select_messages",
    "download_subset",
    "SrefField",
    "load_probability_field",
    "load_probability_field_cached",
    "open_subset",
    "PolygonAggregate",
    "aggregate_over_polygon",
    "DEFAULT_FIELDS",
    "warm_cycle",
    "prune_old_cycles",
]
