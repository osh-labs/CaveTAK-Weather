"""REFS provider — same-day convection-allowing supplement over the upstream domain (FR-7a).

Thin wrapper over the REFS pipeline (:mod:`upstreamwx.refs`), the HREF-replacement sibling of
:mod:`upstreamwx.ingest.gefs_provider`. REFS is NCEP's ~3 km convection-allowing ensemble; it is
**authoritative** for the same-day window (~36 h) flash-flood and lightning posture, while GEFS
keeps the longer (coarse, global) planning horizon. Where both are in range the engine takes the
higher hazard tier (FR-19). The GEFS<->REFS cross-ensemble agreement (FR-17, §16.5) is computed by
the orchestrator once both ensembles complete (they run concurrently), via
:func:`cross_ensemble_agreement` exported here.

Ingestion reads through the **persistent multi-run cache**. The scheduler warms the published
forecast hours of each REFS run and keeps several recent runs; for each REFS valid time in the
mission window, :mod:`upstreamwx.ingest.refs_selection` picks the freshest cached run whose
forecast hour is in band, so a current run's spin-up is served from the previous run's mature
forecast. The provider fetches each distinct ``(cycle, fhour)`` from the cache and aggregates the
conservative max over the upstream polygon across the window.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from shapely.geometry.base import BaseGeometry

from ..config import Settings, get_settings
from ..engine.models import Mission
from ..refs import accum_window, aggregate_over_polygon, load_probability_field_cached
from ..refs.sources import RefsCycle
from .base import IngestBundle
from .refs_selection import MAX_LEAD_H, cached_cycles, resolve_valid_time_sources

NAME = "refs"

# Flash-flood neighborhood QPF: P(>=0.5 in/1 h) and P(>=1 in/3 h) over the domain.
PRECIP_VAR = "APCP"
PRECIP_1H_PROB = ">12.7"   # 0.5 in
PRECIP_3H_PROB = ">25.4"   # 1 in
# Lightning: explicit neighborhood P(lightning); reflectivity proxy as fallback.
LTNG_VAR, LTNG_PROB = "LTNG", ">0.08"   # REFS lightning NEP threshold (HREF used >0.2)
REFC_VAR, REFC_PROB = "REFC", ">40"     # composite reflectivity >= 40 dBZ

# Cross-ensemble agreement cut points (percent). A "strong" signal in one ensemble with the
# other near "absent" is a material divergence (caps confidence; §16.5).
AGREE_PRESENT_PCT = 20.0
AGREE_STRONG_PCT = 50.0


def cross_ensemble_agreement(
    gefs_p_precip: float | None,
    gefs_p_tstm: float | None,
    refs_p_precip: float | None,
    refs_p_lightning: float | None,
) -> str:
    """Classify GEFS<->REFS concurrence per FR-17/§16.5: ``consistent`` or ``partial``.

    A material divergence on either hazard (one ensemble strong, the other absent) returns
    ``partial`` (caps confidence at Moderate). Anything else is ``consistent``. Note GEFS and
    REFS sample different scales (global ~0.5° vs 3 km convection-allowing), so a strong-vs-absent
    split is genuinely informative about forecast uncertainty.
    """
    for a, b in ((gefs_p_precip, refs_p_precip), (gefs_p_tstm, refs_p_lightning)):
        if a is None or b is None:
            continue
        strong_vs_absent = (a >= AGREE_STRONG_PCT and b < AGREE_PRESENT_PCT) or (
            b >= AGREE_STRONG_PCT and a < AGREE_PRESENT_PCT
        )
        if strong_vs_absent:
            return "partial"
    return "consistent"


def _domain_max(
    cycle: RefsCycle,
    fhour: int,
    var: str,
    prob: str,
    polygon: BaseGeometry,
    *,
    fcst: str | None = None,
    settings: Settings | None = None,
) -> float | None:
    """REFS neighborhood-probability domain max for one cached forecast hour, or None.

    Reads through the persistent cache; a miss (e.g. an hour not warmed yet) re-fetches
    transparently. Returns None when the field is absent (``LookupError``), when the domain
    lies off the REFS grid (``ValueError`` — never sample an unrelated edge cell), or when
    the masked cells are all NaN (the aggregate reports None rather than a NaN that would
    read as "no hazard" downstream).
    """
    try:
        field = load_probability_field_cached(
            cycle, fhour, var=var, prob=prob, fcst=fcst, settings=settings
        )
    except LookupError:
        return None
    try:
        agg = aggregate_over_polygon(field.data, polygon, field_name=var, threshold=prob)
    except ValueError:
        return None
    return agg.max_value


def _run_label(cycle: RefsCycle) -> str:
    return f"{cycle.date}/{cycle.hh}Z"


def _fhour_range(fhours: list[int]) -> str:
    lo, hi = min(fhours), max(fhours)
    return f"f{lo:02d}" if lo == hi else f"f{lo:02d}-f{hi:02d}"


def fetch(
    mission: Mission,
    bundle: IngestBundle,
    polygon: BaseGeometry | None,
    *,
    lightning_polygon: BaseGeometry | None = None,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> None:
    """Populate REFS neighborhood probabilities over the upstream domain (if in range).

    QPF aggregates over ``polygon`` (the upstream watershed/RoC); the lightning neighborhood
    fields aggregate over ``lightning_polygon`` — the Lightning Area of Concern disk around the
    activity (PRD §16.1) — which defaults to ``polygon`` when unset. A ``None`` flood polygon
    (delineation failed) skips QPF but still serves the lightning fields over the LAoC.
    """
    settings = settings or get_settings()
    now = now if now is not None else datetime.now(UTC)
    ltng_polygon = lightning_polygon if lightning_polygon is not None else polygon
    if ltng_polygon is None and polygon is None:
        bundle.sources_ok[NAME] = False
        bundle.notes.append("REFS: no aggregation domain available (watershed and LAoC unset).")
        return

    cycles = cached_cycles(now, settings=settings)
    if not cycles:
        # Source of truth is the cache: nothing warmed yet (cold start / before first tick).
        bundle.sources_ok[NAME] = False
        bundle.notes.append("REFS: no warmed cycle in cache yet (awaiting scheduler warm).")
        return
    age_h = (now - cycles[0].init_time).total_seconds() / 3600.0
    if age_h > settings.ensemble_max_age_h:
        # Freshness gate (data quality first-class): a stalled scheduler must not let a
        # days-old run keep serving as the *authoritative same-day* 3 km signal. Stale REFS
        # degrades loudly to "unavailable"; GEFS (which re-probes live) carries the horizon.
        bundle.sources_ok[NAME] = False
        bundle.notes.append(
            f"REFS: newest warmed cycle is {age_h:.0f} h old "
            f"(> {settings.ensemble_max_age_h:.0f} h freshness bound); treating the "
            "same-day supplement as unavailable rather than serving stale data."
        )
        return

    sources = resolve_valid_time_sources(
        mission.window_start, mission.window_end, now=now, cycles=cycles
    )
    bundle.refs_in_range = bool(sources)
    if not sources:
        bundle.sources_ok[NAME] = True
        bundle.notes.append(
            f"REFS: mission window outside the same-day supplement range (~{MAX_LEAD_H:.0f} h); "
            "GEFS covers this horizon."
        )
        return

    # Fetch each distinct (cycle, fhour) once; aggregate the conservative max across the window
    # (the worst case any covered hour shows over the upstream domain).
    precip_vals: list[float] = []
    ltng_vals: list[float] = []
    for cycle, fhour in sorted(
        {(s.cycle, s.fhour) for s in sources}, key=lambda cf: (cf[0].init_time, cf[1])
    ):
        if polygon is not None:
            p1 = _domain_max(
                cycle, fhour, PRECIP_VAR, PRECIP_1H_PROB, polygon,
                fcst=accum_window(fhour, 1), settings=settings,
            )
            p3 = _domain_max(
                cycle, fhour, PRECIP_VAR, PRECIP_3H_PROB, polygon,
                fcst=accum_window(fhour, 3), settings=settings,
            )
            hour_precip = max((v for v in (p1, p3) if v is not None), default=None)
            if hour_precip is not None:
                precip_vals.append(hour_precip)

        ltng = _domain_max(cycle, fhour, LTNG_VAR, LTNG_PROB, ltng_polygon, settings=settings)
        if ltng is None:
            ltng = _domain_max(cycle, fhour, REFC_VAR, REFC_PROB, ltng_polygon, settings=settings)
        if ltng is not None:
            ltng_vals.append(ltng)

    refs_precip = max(precip_vals, default=None)
    refs_ltng = max(ltng_vals, default=None)

    bundle.refs_p_precip = refs_precip
    bundle.refs_p_lightning = refs_ltng

    # Provenance: which run(s) and forecast hours actually fed the signal. Group valid times by
    # run; the freshest run is primary, older runs only ever cover the freshest run's spin-up
    # hours, so they are labelled spin-up backfills.
    by_cycle: dict[RefsCycle, list[int]] = defaultdict(list)
    for s in sources:
        by_cycle[s.cycle].append(s.fhour)
    ordered = sorted(by_cycle, key=lambda c: c.init_time, reverse=True)  # freshest first
    primary, backfills = ordered[0], ordered[1:]

    bundle.refs_cycle = _run_label(primary)
    if backfills:
        extra = ", ".join(
            f"{c.hh}Z {_fhour_range(by_cycle[c])} spin-up backfill" for c in backfills
        )
        bundle.refs_fhour = f"{_fhour_range(by_cycle[primary])} (+ {extra})"
    else:
        bundle.refs_fhour = _fhour_range(by_cycle[primary])
    bundle.refs_runs = [
        (_run_label(c), min(by_cycle[c]), max(by_cycle[c])) for c in ordered
    ]

    # Neighborhood probability is itself a member-exceedance fraction; REFS is the authoritative
    # same-day ensemble, so its member support drives the confidence qualifier in-window (§16.5).
    # REFS runs on its own bundle; the orchestrator merges member_support with GEFS's (REFS wins
    # in-window) and computes the GEFS<->REFS agreement once both ensembles complete.
    if refs_precip is not None:
        bundle.member_support["flash_flood"] = refs_precip / 100.0
    if refs_ltng is not None:
        bundle.member_support["lightning"] = refs_ltng / 100.0

    note = (
        f"REFS cycle {bundle.refs_cycle} {bundle.refs_fhour}; neighborhood P(QPF) and "
        "P(lightning) over the upstream domain (~3 km same-day supplement)."
    )
    if backfills:
        note += (
            " Spin-up hours backfilled from the prior "
            + ", ".join(f"{c.hh}Z" for c in backfills)
            + " run (mature forecast hours)."
        )
    bundle.notes.append(note)
    bundle.sources_ok[NAME] = True
