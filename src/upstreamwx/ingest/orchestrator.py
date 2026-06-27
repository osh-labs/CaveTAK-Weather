"""Ingestion orchestrator (PRD §11.2 component 4).

Assembles a normalized :class:`IngestBundle` for a mission from every source —
NWS products/AFD, Open-Meteo derived fields, the SREF processor over the upstream
domain, and the SPC outlook — with graceful degradation when a non-mandatory
source is down (NFR-6). The NWS AFD is mandatory (FR-5); its failure is surfaced.

This is the glue from a mission to engine-ready inputs:

    mission -> watershed delineation -> ingest bundle -> HazardInputs -> engine.assess
"""

from __future__ import annotations

from shapely.geometry.base import BaseGeometry

from ..engine.models import HazardInputs, Mission
from ..watershed import clip_watershed, delineate_cached
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

    # SREF over the upstream domain (needs the watershed polygon). Delineate
    # pour-point-exact (NLDI raindrop two-step) with the WBD HUC-12 trace as the
    # snap-free fallback; cache on disk so repeat missions reuse it.
    if polygon is None:
        try:
            basin = delineate_cached(mission.lat, mission.lon)
            bundle.upstream = basin
            polygon = basin.polygon
            bundle.sources_ok["watershed"] = True
            domain = basin.flowline_name or (
                f"comid {basin.comid}" if basin.comid else "pour point"
            )
            bundle.notes.append(
                f"watershed: {basin.area_km2:.0f} km² to {domain} via {basin.method}."
            )
        except Exception as exc:  # noqa: BLE001
            bundle.sources_ok["watershed"] = False
            bundle.notes.append(f"watershed: delineation failed ({type(exc).__name__}).")

        # Radius of Concern (FR-3): clip the delineated basin to the user's disk so the
        # SREF/HREF aggregation runs over the bounded domain. Defensive — a clip failure
        # must never crash the briefing (NFR-6); fall back to the full watershed.
        if polygon is not None and mission.radius_km:
            try:
                clip = clip_watershed(polygon, mission.lat, mission.lon, mission.radius_km)
                polygon = clip.kept
                bundle.roc_radius_km = mission.radius_km
                bundle.roc_disk = clip.disk
                bundle.roc_excluded = clip.excluded
                bundle.roc_kept_area_km2 = clip.kept_area_km2
                bundle.notes.append(
                    f"radius of concern: clipped to {clip.kept_area_km2:.0f} km² "
                    f"within {mission.radius_km:.0f} km of origin."
                )
            except Exception as exc:  # noqa: BLE001
                bundle.notes.append(
                    f"radius of concern: clip failed ({type(exc).__name__}); full watershed used."
                )

    bundle.aggregation_polygon = polygon
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
