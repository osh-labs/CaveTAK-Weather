"""Ingestion orchestrator (PRD §11.2 component 4).

Assembles a normalized :class:`IngestBundle` for a mission from every source —
NWS products/AFD, Open-Meteo derived fields, the SREF processor over the upstream
domain, and the SPC outlook — with graceful degradation when a non-mandatory
source is down (NFR-6). The NWS AFD is mandatory (FR-5); its failure is surfaced.

This is the glue from a mission to engine-ready inputs:

    mission -> watershed trace -> ingest bundle -> HazardInputs -> engine.assess
"""

from __future__ import annotations

from shapely.geometry.base import BaseGeometry

from ..engine.models import HazardInputs, Mission
from ..watershed import resolve_and_trace_cached
from . import href_provider, nws, openmeteo, spc, sref_provider
from .base import IngestBundle, to_hazard_inputs

# Providers that don't need the watershed polygon; (name, module).
_POINT_PROVIDERS = (nws, openmeteo, spc)
MANDATORY = {"nws"}


def gather(
    mission: Mission,
    *,
    polygon: BaseGeometry | None = None,
    cycle=None,
) -> IngestBundle:
    """Gather all sources into a bundle; degrade gracefully on non-mandatory failures."""
    bundle = IngestBundle()

    # Point providers (NWS, Open-Meteo, SPC).
    for provider in _POINT_PROVIDERS:
        try:
            provider.fetch(mission, bundle)
        except Exception as exc:  # noqa: BLE001 — degrade per NFR-6
            bundle.sources_ok[provider.NAME] = False
            bundle.notes.append(f"{provider.NAME}: unavailable ({type(exc).__name__}).")

    # SREF over the upstream domain (needs the watershed polygon).
    if polygon is None:
        try:
            polygon = resolve_and_trace_cached(mission.lat, mission.lon).polygon
        except Exception as exc:  # noqa: BLE001
            bundle.sources_ok["watershed"] = False
            bundle.notes.append(f"watershed: trace failed ({type(exc).__name__}).")
    if polygon is not None:
        try:
            sref_provider.fetch(mission, bundle, polygon, cycle=cycle)
        except Exception as exc:  # noqa: BLE001
            bundle.sources_ok[sref_provider.NAME] = False
            bundle.notes.append(f"sref: unavailable ({type(exc).__name__}).")

        # HREF same-day supplement (FR-7a): conditional, runs after SREF so it can
        # record SREF<->HREF agreement; degrades gracefully like any non-mandatory source.
        try:
            href_provider.fetch(mission, bundle, polygon)
        except Exception as exc:  # noqa: BLE001
            bundle.sources_ok[href_provider.NAME] = False
            bundle.notes.append(f"href: unavailable ({type(exc).__name__}).")

    failed_mandatory = [s for s in MANDATORY if bundle.sources_ok.get(s) is False]
    if failed_mandatory:
        bundle.notes.append(f"WARNING: mandatory source(s) unavailable: {failed_mandatory}.")
    return bundle


def gather_inputs(
    mission: Mission,
    *,
    polygon: BaseGeometry | None = None,
    cycle=None,
) -> tuple[HazardInputs, IngestBundle]:
    """Convenience: gather a bundle and map it to engine HazardInputs."""
    # Conservative default: assume a wet party (FR-16). A dry cave with no
    # immersion is a per-mission override the caller passes explicitly later.
    bundle = gather(mission, polygon=polygon, cycle=cycle)
    return to_hazard_inputs(bundle, dry_party=False), bundle
