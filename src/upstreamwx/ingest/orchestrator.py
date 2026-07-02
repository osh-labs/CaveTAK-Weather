"""Ingestion orchestrator (PRD §11.2 component 4).

Assembles a normalized :class:`IngestBundle` for a mission from every source —
NWS products/AFD, Open-Meteo derived fields, the SREF processor over the upstream
domain, and the SPC outlook — with graceful degradation when a non-mandatory
source is down (NFR-6). The NWS AFD is mandatory (FR-5); its failure is surfaced.

This is the glue from a mission to engine-ready inputs:

    mission -> watershed delineation -> ingest bundle -> HazardInputs -> engine.assess
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields

from shapely.geometry.base import BaseGeometry

from ..engine.models import HazardInputs, Mission
from ..watershed import clip_watershed, delineate_cached, roc_disk
from . import gefs_provider, nws, openmeteo, refs_provider, spc
from .base import IngestBundle, to_hazard_inputs

# Providers that don't need the watershed polygon; (name, module).
_POINT_PROVIDERS = (nws, openmeteo, spc)
# "nws" is the active-alerts check (the authoritative product anchor); "nws_afd" the
# forecaster-discussion chain. Both are FR-5 mandatory; they degrade independently.
MANDATORY = {"nws", "nws_afd"}

# Bundle fields that accumulate from more than one source and must be *combined* on merge
# rather than copied. ``sources_ok`` keys are disjoint per source, so a plain update is exact.
# ``member_support`` keys (flash_flood/lightning) are written by *both* GEFS and REFS; they
# merge per-key by max (commutative, hence order- and timing-independent, NFR-4). When REFS is
# in range it is then made authoritative in-window by the ensemble branch (see below), since
# the 3 km convection-allowing ensemble should drive same-day confidence over coarse GEFS.
# ``notes`` accumulate as a list.
_MERGE_UPDATE_DICT_FIELDS = frozenset({"sources_ok"})
_MERGE_MAX_DICT_FIELDS = frozenset({"member_support"})
_MERGE_LIST_FIELDS = frozenset({"notes"})


def _run_point_provider(provider, mission: Mission) -> IngestBundle:
    """Run one point provider into its own bundle (NFR-6 degradation contained per source).

    Each concurrent task owns a private bundle so the providers — which mutate the bundle in
    place — never race on shared state; results are merged deterministically by the caller.
    """
    bundle = IngestBundle()
    try:
        provider.fetch(mission, bundle)
    except Exception as exc:  # noqa: BLE001 — degrade per NFR-6
        bundle.sources_ok[provider.NAME] = False
        bundle.notes.append(f"{provider.NAME}: unavailable ({type(exc).__name__}).")
    return bundle


def _run_gefs(
    mission: Mission, polygon: BaseGeometry | None, lightning_polygon: BaseGeometry, cycle
) -> IngestBundle:
    """Aggregate GEFS over the domain into a private bundle, degrading on failure (NFR-6).

    Flash-flood fields aggregate over ``polygon`` (the upstream watershed/RoC); the lightning
    proxy aggregates over ``lightning_polygon`` (the LAoC disk when set, else the same polygon).
    """
    bundle = IngestBundle()
    try:
        gefs_provider.fetch(
            mission, bundle, polygon, lightning_polygon=lightning_polygon, cycle=cycle
        )
    except Exception as exc:  # noqa: BLE001
        bundle.sources_ok[gefs_provider.NAME] = False
        bundle.notes.append(f"gefs: unavailable ({type(exc).__name__}).")
    return bundle


def _run_refs(
    mission: Mission, polygon: BaseGeometry | None, lightning_polygon: BaseGeometry
) -> IngestBundle:
    """Aggregate REFS over the domain into a private bundle, degrading on failure (NFR-6).

    As with GEFS, the lightning neighborhood fields aggregate over ``lightning_polygon`` (the
    LAoC disk when set) while QPF aggregates over the flash-flood ``polygon``.
    """
    bundle = IngestBundle()
    try:
        refs_provider.fetch(mission, bundle, polygon, lightning_polygon=lightning_polygon)
    except Exception as exc:  # noqa: BLE001
        bundle.sources_ok[refs_provider.NAME] = False
        bundle.notes.append(f"refs: unavailable ({type(exc).__name__}).")
    return bundle


def _run_watershed_and_ensembles(
    mission: Mission, polygon: BaseGeometry | None, cycle
) -> IngestBundle:
    """Delineate (+ RoC clip) the domain, then aggregate GEFS and REFS over it (FR-3, FR-7/7a).

    Runs as one task into its own bundle, concurrently with the point providers. GEFS and REFS
    are themselves independent ensemble pulls, so they run concurrently here too; the
    GEFS<->REFS cross-ensemble agreement (FR-17, §16.5) is computed once both have completed.
    The cfgrib decode is serialised inside
    :func:`upstreamwx.grib.cache.decode_cached`, so the concurrent ensembles overlap only their
    network fetch and aggregation, which is where the latency lives.
    """
    bundle = IngestBundle()

    # GEFS/REFS aggregate over the upstream domain (needs the watershed polygon). Delineate
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

    # Lightning Area of Concern (PRD §16.1, §13 principle 4): aggregate the lightning signal
    # over a disk around the activity rather than the upstream watershed. The disk is the raw
    # RoC circle — *not* intersected with the basin — because lightning is a local atmospheric
    # hazard independent of flow routing, and for the same reason it does not need the basin:
    # a failed delineation must not silence the lightning ensemble (data quality first-class,
    # NFR-6). Defensive: a disk failure must never crash the briefing; fall back to the
    # flash-flood domain. None radius -> same fallback.
    lightning_polygon = polygon
    if mission.lightning_radius_km:
        try:
            laoc = roc_disk(mission.lat, mission.lon, mission.lightning_radius_km)
            lightning_polygon = laoc
            bundle.laoc_radius_km = mission.lightning_radius_km
            bundle.laoc_disk = laoc
            bundle.laoc_area_km2 = math.pi * mission.lightning_radius_km**2
            bundle.notes.append(
                f"lightning area of concern: P(thunder)/P(lightning) aggregated over a "
                f"{mission.lightning_radius_km:.0f} km disk around the activity."
            )
        except Exception as exc:  # noqa: BLE001
            bundle.notes.append(
                f"lightning area of concern: disk failed ({type(exc).__name__}); "
                "upstream domain used for lightning."
            )
    if polygon is None and lightning_polygon is not None:
        bundle.notes.append(
            "watershed unavailable: flash-flood ensemble fields have no domain; "
            "lightning still aggregates over the Lightning Area of Concern disk."
        )

    if polygon is not None or lightning_polygon is not None:
        # GEFS and REFS are independent aggregations over the same domain; run them
        # concurrently into private bundles, then merge (GEFS first, then REFS) in a fixed
        # order. Both fetches contain their own failures (NFR-6), so neither task raises.
        with ThreadPoolExecutor(max_workers=2) as executor:
            gefs_future = executor.submit(_run_gefs, mission, polygon, lightning_polygon, cycle)
            refs_future = executor.submit(_run_refs, mission, polygon, lightning_polygon)
            gefs_bundle = gefs_future.result()
            refs_bundle = refs_future.result()
        _merge_into(bundle, gefs_bundle)
        _merge_into(bundle, refs_bundle)

        # REFS is authoritative inside its same-day window: where it is in range its 3 km member
        # support drives the confidence qualifier, overriding the per-key max merge with coarse
        # GEFS (the transition's "greater reliance on REFS for the first ~36 h"). Beyond REFS
        # range the merged GEFS support stands. Deterministic — depends only on values (NFR-4).
        if refs_bundle.refs_in_range:
            for key, value in refs_bundle.member_support.items():
                bundle.member_support[key] = value

        # Cross-ensemble agreement now that both signals are present (FR-17, §16.5). With None
        # on either side (a source out of range/unavailable) this resolves to "consistent".
        bundle.source_agreement = refs_provider.cross_ensemble_agreement(
            bundle.gefs_p_precip,
            bundle.gefs_p_tstm,
            bundle.refs_p_precip,
            bundle.refs_p_lightning,
        )

    return bundle


def _merge_into(dest: IngestBundle, src: IngestBundle) -> None:
    """Fold a task's bundle into ``dest`` in a fixed, timing-independent order (NFR-4).

    Provenance dicts/lists accumulate (every group contributes); every other field has exactly
    one owning source, so we copy a value only when the task actually set it (differs from a
    fresh default). Because each field has a single owner and tasks merge in a fixed order, the
    merged bundle — and therefore the engine inputs — are identical regardless of which task
    finished first.
    """
    for f in fields(IngestBundle):
        name = f.name
        if name in _MERGE_UPDATE_DICT_FIELDS:
            getattr(dest, name).update(getattr(src, name))
        elif name in _MERGE_MAX_DICT_FIELDS:
            dest_dict = getattr(dest, name)
            for key, value in getattr(src, name).items():
                dest_dict[key] = max(dest_dict[key], value) if key in dest_dict else value
        elif name in _MERGE_LIST_FIELDS:
            getattr(dest, name).extend(getattr(src, name))
        else:
            value = getattr(src, name)
            if value != getattr(_DEFAULT_BUNDLE, name):
                setattr(dest, name, value)


# A pristine bundle whose field values are the "unset" baseline used by ``_merge_into``.
_DEFAULT_BUNDLE = IngestBundle()


def gather(
    mission: Mission,
    *,
    polygon: BaseGeometry | None = None,
    cycle=None,
) -> IngestBundle:
    """Gather all sources into a bundle; degrade gracefully on non-mandatory failures.

    The point providers (NWS, Open-Meteo, SPC) and the watershed→SREF/HREF chain are mutually
    independent, so they run concurrently — each on a private bundle — collapsing the briefing's
    serial network latency to roughly the slowest single branch instead of their sum. Results
    are merged deterministically (NFR-4); only I/O is parallelised, never the engine.
    """
    bundle = IngestBundle()

    with ThreadPoolExecutor(max_workers=len(_POINT_PROVIDERS) + 1) as executor:
        point_futures = [
            executor.submit(_run_point_provider, provider, mission)
            for provider in _POINT_PROVIDERS
        ]
        ensemble_future = executor.submit(
            _run_watershed_and_ensembles, mission, polygon, cycle
        )
        # Merge in a fixed order (point providers, then the ensemble branch) so notes and
        # source order do not depend on completion timing.
        for future in point_futures:
            _merge_into(bundle, future.result())
        _merge_into(bundle, ensemble_future.result())

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
