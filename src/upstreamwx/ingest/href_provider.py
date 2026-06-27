"""HREF provider — same-day high-resolution supplement over the upstream domain (FR-7a).

Thin wrapper over the Spike C HREF pipeline (``upstreamwx.href``), the sibling of
:mod:`upstreamwx.ingest.sref_provider`. HREF is NCEP's ~3 km convection-allowing
ensemble; it sharpens the flash-flood and lightning signal inside the **same-day
window (~36 h)** while SREF keeps the longer planning horizon. Where both are in
range the engine takes the higher hazard tier (FR-19). The SREF<->HREF cross-ensemble
agreement (FR-17, §16.5) is computed by the orchestrator once both ensembles complete
(they run concurrently), using :func:`cross_ensemble_agreement` exported here.

Ingestion reads through the **persistent multi-run cache** (roadmap §M0.1.1). The scheduler
warms f06-f48 of each HREF run and keeps several recent runs; for each valid hour in the
mission window, :mod:`upstreamwx.ingest.href_selection` picks the freshest cached run whose
forecast hour is >= 6, so the current run's spin-up hours are served from the *previous*
run's mature forecast (no separate spin-up model needed). The provider then fetches each
distinct ``(cycle, fhour)`` from the cache and aggregates the conservative max over the
upstream polygon across the window.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from shapely.geometry.base import BaseGeometry

from ..config import Settings, get_settings
from ..engine.models import Mission
from ..href import accum_window, aggregate_over_polygon, load_probability_field_cached
from ..href.sources import HrefCycle
from .base import IngestBundle
from .href_selection import MAX_LEAD_H, cached_cycles, resolve_valid_time_sources

NAME = "href"

# Flash-flood neighborhood QPF: P(>=0.5 in/1 h) and P(>=1 in/3 h) over the domain.
PRECIP_VAR = "APCP"
PRECIP_1H_PROB = ">12.7"   # 0.5 in
PRECIP_3H_PROB = ">25.4"   # 1 in
# Lightning: explicit neighborhood P(lightning); reflectivity proxy as fallback.
LTNG_VAR, LTNG_PROB = "LTNG", ">0.2"
REFC_VAR, REFC_PROB = "REFC", ">40"   # composite reflectivity >= 40 dBZ

# Cross-ensemble agreement cut points (percent). A "strong" signal in one ensemble
# with the other near "absent" is a material divergence (caps confidence; §16.5).
AGREE_PRESENT_PCT = 20.0
AGREE_STRONG_PCT = 50.0


def cross_ensemble_agreement(
    sref_p_precip: float | None,
    sref_p_tstm: float | None,
    href_p_precip: float | None,
    href_p_lightning: float | None,
) -> str:
    """Classify SREF<->HREF concurrence per FR-17/§16.5: ``consistent`` or ``partial``.

    A material divergence on either hazard (one ensemble strong, the other absent)
    returns ``partial`` (caps confidence at Moderate). Anything else is ``consistent``.
    """
    for a, b in ((sref_p_precip, href_p_precip), (sref_p_tstm, href_p_lightning)):
        if a is None or b is None:
            continue
        strong_vs_absent = (a >= AGREE_STRONG_PCT and b < AGREE_PRESENT_PCT) or (
            b >= AGREE_STRONG_PCT and a < AGREE_PRESENT_PCT
        )
        if strong_vs_absent:
            return "partial"
    return "consistent"


def _domain_max(
    cycle: HrefCycle,
    fhour: int,
    var: str,
    prob: str,
    polygon: BaseGeometry,
    *,
    fcst: str | None = None,
    settings: Settings | None = None,
) -> float | None:
    """HREF neighborhood-probability domain max for one cached forecast hour, or None.

    Reads through the persistent cache; a miss (e.g. a hour not warmed yet) re-fetches
    transparently. Returns None when the field is absent (``LookupError``).
    """
    try:
        field = load_probability_field_cached(
            cycle, fhour, var=var, prob=prob, fcst=fcst, settings=settings
        )
    except LookupError:
        return None
    agg = aggregate_over_polygon(field.data, polygon, field_name=var, threshold=prob)
    return agg.max_value


def _run_label(cycle: HrefCycle) -> str:
    return f"{cycle.date}/{cycle.hh}Z"


def _fhour_range(fhours: list[int]) -> str:
    lo, hi = min(fhours), max(fhours)
    return f"f{lo:02d}" if lo == hi else f"f{lo:02d}-f{hi:02d}"


def fetch(
    mission: Mission,
    bundle: IngestBundle,
    polygon: BaseGeometry,
    *,
    lightning_polygon: BaseGeometry | None = None,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> None:
    """Populate HREF neighborhood probabilities over the upstream domain (if in range).

    QPF aggregates over ``polygon`` (the upstream watershed/RoC); the lightning neighborhood
    fields aggregate over ``lightning_polygon`` — the Lightning Area of Concern disk around
    the activity (PRD §16.1) — which defaults to ``polygon`` when unset.
    """
    settings = settings or get_settings()
    now = now if now is not None else datetime.now(UTC)
    ltng_polygon = lightning_polygon if lightning_polygon is not None else polygon

    cycles = cached_cycles(now, settings=settings)
    if not cycles:
        # Source of truth is the cache: nothing warmed yet (cold start / before first tick).
        bundle.sources_ok[NAME] = False
        bundle.notes.append("HREF: no warmed cycle in cache yet (awaiting scheduler warm).")
        return

    sources = resolve_valid_time_sources(
        mission.window_start, mission.window_end, now=now, cycles=cycles
    )
    bundle.href_in_range = bool(sources)
    if not sources:
        bundle.sources_ok[NAME] = True
        bundle.notes.append(
            f"HREF: mission window outside the same-day supplement range (~{MAX_LEAD_H:.0f} h); "
            "SREF covers this horizon."
        )
        return

    # Fetch each distinct (cycle, fhour) once; aggregate the conservative max across the
    # window (the worst case any covered hour shows over the upstream domain).
    precip_vals: list[float] = []
    ltng_vals: list[float] = []
    for cycle, fhour in sorted(
        {(s.cycle, s.fhour) for s in sources}, key=lambda cf: (cf[0].init_time, cf[1])
    ):
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

    href_precip = max(precip_vals, default=None)
    href_ltng = max(ltng_vals, default=None)

    bundle.href_p_precip = href_precip
    bundle.href_p_lightning = href_ltng

    # Provenance: which run(s) and forecast hours actually fed the signal. Group valid times
    # by run; the **freshest** run is primary, older runs only ever cover the freshest run's
    # spin-up hours (for any later valid time the freshest run is in-band and wins), so they
    # are labelled spin-up backfills.
    by_cycle: dict[HrefCycle, list[int]] = defaultdict(list)
    for s in sources:
        by_cycle[s.cycle].append(s.fhour)
    ordered = sorted(by_cycle, key=lambda c: c.init_time, reverse=True)  # freshest first
    primary, backfills = ordered[0], ordered[1:]

    bundle.href_cycle = _run_label(primary)
    if backfills:
        extra = ", ".join(
            f"{c.hh}Z {_fhour_range(by_cycle[c])} spin-up backfill" for c in backfills
        )
        bundle.href_fhour = f"{_fhour_range(by_cycle[primary])} (+ {extra})"
    else:
        bundle.href_fhour = _fhour_range(by_cycle[primary])
    bundle.href_runs = [
        (_run_label(c), min(by_cycle[c]), max(by_cycle[c])) for c in ordered
    ]

    # Neighborhood probability is itself a member-exceedance fraction; let the
    # stronger ensemble inform member support for the confidence qualifier (§16.5). HREF runs
    # on its own bundle, so this records HREF's support; the orchestrator merges it with SREF's
    # per-key by max, and computes the SREF<->HREF agreement once both ensembles complete.
    if href_precip is not None:
        bundle.member_support["flash_flood"] = href_precip / 100.0
    if href_ltng is not None:
        bundle.member_support["lightning"] = href_ltng / 100.0

    note = (
        f"HREF cycle {bundle.href_cycle} {bundle.href_fhour}; neighborhood P(QPF) and "
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
