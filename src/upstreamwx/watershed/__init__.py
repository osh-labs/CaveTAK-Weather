"""Watershed resolution and upstream tracing (PRD FR-2, FR-3).

Spike B home; promoted to a real module (HUC-12 resolve + upstream trace + cache)
in M0.1. The upstream contributing-area polygon produced here is the aggregation
domain consumed by the SREF processor (``upstreamwx.sref``).

Two delineation paths are available:

- :func:`delineate` — pour-point-exact NLDI raindrop snap + split-catchment
  (Spike D), the preferred domain for Effective-QPF aggregation, with an
  automatic fall back to the WBD trace below.
- :func:`trace_upstream` — the deterministic, snap-free WBD HUC-12 ``tohuc``
  trace (coarser; the fallback / cross-check).
"""

from .cache import delineate_cached, resolve_and_trace_cached
from .huc import HucResult, resolve_huc12
from .pourpoint import (
    PourpointBasin,
    SnapResult,
    delineate,
    delineate_pourpoint,
    raindrop_snap,
)
from .upstream import UpstreamTrace, trace_upstream

__all__ = [
    "HucResult",
    "resolve_huc12",
    "UpstreamTrace",
    "trace_upstream",
    "resolve_and_trace_cached",
    "PourpointBasin",
    "SnapResult",
    "delineate",
    "delineate_pourpoint",
    "raindrop_snap",
    "delineate_cached",
]
