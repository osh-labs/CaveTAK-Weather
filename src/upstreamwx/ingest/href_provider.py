"""HREF provider — same-day high-resolution supplement over the upstream domain (FR-7a).

Thin wrapper over the Spike C HREF pipeline (``upstreamwx.href``), the sibling of
:mod:`upstreamwx.ingest.sref_provider`. HREF is NCEP's ~3 km convection-allowing
ensemble; it sharpens the flash-flood and lightning signal inside the **same-day
window (~6-36 h)** while SREF keeps the longer planning horizon. Where both are in
range the engine takes the higher hazard tier (FR-19); this provider also records
SREF<->HREF agreement as the cross-ensemble confidence cue (FR-17, §16.5).

Because HREF publishes one file per forecast hour, ingestion is **conditional and
window-scoped**: we resolve every forecast hour covering the mission window, fetch
each one, and aggregate the conservative max over the upstream polygon across all
hours. Heavy/scheduled orchestration is deferred to M0.1.1 alongside the SREF scheduler.
"""

from __future__ import annotations

from datetime import UTC, datetime

from shapely.geometry.base import BaseGeometry

from ..engine.models import Mission
from ..href import (
    accum_window,
    aggregate_over_polygon,
    latest_available_cycle,
    load_probability_field,
)
from ..href.sources import MAX_FHOUR
from .base import IngestBundle

NAME = "href"

# Same-day supplement band (hours of lead from "now"): HREF's 0-6 h is left to the
# HRRR-derived Open-Meteo layer (spin-up), and beyond ~36 h SREF takes over.
MIN_USEFUL_LEAD_H = 6.0
MAX_LEAD_H = 36.0

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


def _as_utc(value: datetime) -> datetime:
    """Treat a naive datetime as UTC so it can be compared with the cycle/now clock.

    Mission windows from the engine and CLI are timezone-naive, while the HREF cycle
    init time and ``now`` are UTC-aware; without this the subtraction raises a
    TypeError (offset-naive vs offset-aware).
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def forecast_hours_for_window(
    cycle_init: datetime,
    window_start: datetime,
    window_end: datetime,
    *,
    now: datetime | None = None,
) -> tuple[list[int], bool]:
    """Resolve all HREF forecast hours covering a mission window, and in-range flag.

    Returns ``(fhours, in_range)``. ``fhours`` is the sorted list of cycle-relative
    forecast hours spanning the window, each clamped to ``[1, MAX_FHOUR]``.
    ``in_range`` is True only when the window's lead from ``now`` falls in the
    same-day supplement band and the window overlaps the HREF horizon; otherwise
    ``fhours`` is empty and SREF alone covers the window.
    """
    now = _as_utc(now) if now is not None else datetime.now(UTC)
    cycle_init = _as_utc(cycle_init)
    window_start = _as_utc(window_start)
    window_end = _as_utc(window_end)

    raw_start = round((window_start - cycle_init).total_seconds() / 3600.0)
    raw_end = round((window_end - cycle_init).total_seconds() / 3600.0)

    in_horizon = raw_start <= MAX_FHOUR and raw_end >= 1
    lead_start_h = (window_start - now).total_seconds() / 3600.0
    in_band = lead_start_h <= MAX_LEAD_H and (window_end - now).total_seconds() > 0
    in_range = in_horizon and in_band

    if not in_range:
        return [], False

    fhour_start = min(max(raw_start, 1), MAX_FHOUR)
    fhour_end = min(max(raw_end, 1), MAX_FHOUR)
    return list(range(fhour_start, fhour_end + 1)), True


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
    cycle, fhour: int, var: str, prob: str, polygon, *, fcst: str | None = None
) -> float | None:
    """HREF neighborhood-probability domain max for one forecast hour, or None if absent."""
    try:
        field = load_probability_field(cycle, fhour, var=var, prob=prob, fcst=fcst)
    except LookupError:
        return None
    agg = aggregate_over_polygon(field.data, polygon, field_name=var, threshold=prob)
    return agg.max_value


def fetch(
    mission: Mission,
    bundle: IngestBundle,
    polygon: BaseGeometry,
    *,
    cycle=None,
    now: datetime | None = None,
) -> None:
    """Populate HREF neighborhood probabilities over the upstream domain (if in range)."""
    cycle = cycle or latest_available_cycle()
    if cycle is None:
        bundle.sources_ok[NAME] = False
        bundle.notes.append("HREF: no available cycle on NOMADS (retention/lag).")
        return

    fhours, in_range = forecast_hours_for_window(
        cycle.init_time, mission.window_start, mission.window_end, now=now
    )
    bundle.href_in_range = in_range
    if not in_range:
        bundle.sources_ok[NAME] = True
        bundle.notes.append(
            "HREF: mission window outside the same-day supplement range "
            f"(~{MIN_USEFUL_LEAD_H:.0f}-{MAX_LEAD_H:.0f} h); SREF covers this horizon."
        )
        return

    # Collect per-hour domain maxima; take the worst case across the window.
    precip_vals: list[float] = []
    ltng_vals: list[float] = []
    for fhour in fhours:
        p1 = _domain_max(
            cycle, fhour, PRECIP_VAR, PRECIP_1H_PROB, polygon, fcst=accum_window(fhour, 1)
        )
        p3 = _domain_max(
            cycle, fhour, PRECIP_VAR, PRECIP_3H_PROB, polygon, fcst=accum_window(fhour, 3)
        )
        hour_precip = max((v for v in (p1, p3) if v is not None), default=None)
        if hour_precip is not None:
            precip_vals.append(hour_precip)

        ltng = _domain_max(cycle, fhour, LTNG_VAR, LTNG_PROB, polygon)
        if ltng is None:
            ltng = _domain_max(cycle, fhour, REFC_VAR, REFC_PROB, polygon)
        if ltng is not None:
            ltng_vals.append(ltng)

    href_precip = max(precip_vals, default=None)
    href_ltng = max(ltng_vals, default=None)

    bundle.href_p_precip = href_precip
    bundle.href_p_lightning = href_ltng
    bundle.href_cycle = f"{cycle.date}/{cycle.hh}Z"
    bundle.href_fhour = (
        f"f{fhours[0]:02d}" if len(fhours) == 1 else f"f{fhours[0]:02d}-f{fhours[-1]:02d}"
    )

    # Neighborhood probability is itself a member-exceedance fraction; let the
    # stronger ensemble inform member support for the confidence qualifier (§16.5).
    if href_precip is not None:
        prior = bundle.member_support.get("flash_flood", 0.0)
        bundle.member_support["flash_flood"] = max(prior, href_precip / 100.0)
    if href_ltng is not None:
        prior = bundle.member_support.get("lightning", 0.0)
        bundle.member_support["lightning"] = max(prior, href_ltng / 100.0)

    # Cross-ensemble agreement vs the SREF signal already in the bundle (FR-17).
    bundle.source_agreement = cross_ensemble_agreement(
        bundle.sref_p_precip, bundle.sref_p_tstm, href_precip, href_ltng
    )

    cold_start = fhours[0] < MIN_USEFUL_LEAD_H
    bundle.notes.append(
        f"HREF cycle {bundle.href_cycle} {bundle.href_fhour}; neighborhood P(QPF) and "
        "P(lightning) over the upstream domain (~3 km same-day supplement)."
        + (" Note: window begins within HREF spin-up (<6 h); treat as supporting only."
           if cold_start else "")
    )
    bundle.sources_ok[NAME] = True
