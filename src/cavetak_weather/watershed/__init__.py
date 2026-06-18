"""Watershed resolution and upstream tracing (PRD FR-2, FR-3).

Spike B home; promoted to a real module (HUC-12 resolve + upstream trace + cache)
in M0.1. The upstream contributing-area polygon produced here is the aggregation
domain consumed by the SREF processor (``cavetak_weather.sref``).
"""

from .huc import HucResult, resolve_huc12
from .upstream import UpstreamTrace, trace_upstream

__all__ = ["HucResult", "resolve_huc12", "UpstreamTrace", "trace_upstream"]
