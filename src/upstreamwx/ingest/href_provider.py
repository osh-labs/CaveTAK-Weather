"""HREF provider — same-day high-resolution supplement over the upstream domain (FR-7a).

Thin wrapper over the Spike C HREF pipeline (``upstreamwx.href``), the sibling of
:mod:`upstreamwx.ingest.sref_provider`. HREF is NCEP's ~3 km convection-allowing
ensemble; it sharpens the flash-flood and lightning signal inside the **same-day
window (~6-36 h)** while SREF keeps the longer planning horizon. Where both are in
range the engine takes the higher hazard tier (FR-19); this provider also records
SREF<->HREF agreement as the cross-ensemble confidence cue (FR-17, §16.5).

Because HREF publishes one file per forecast hour, ingestion is **conditional and
window-scoped**: we resolve the forecast hour covering the mission window, fetch
just the neighborhood-probability fields that hour needs, and aggregate the
conservative max over the upstream polygon. Heavy/scheduled orchestration is
deferred to M0.1.1 alongside the SREF scheduler.
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


def forecast_hour_for_window(
    cycle_init: datetime,
    window_start: datetime,
    window_end: datetime,
    *,
    now: datetime | None = None,
) -> tuple[int | None, bool]:
    """Resolve the HREF forecast hour covering a mission window, and in-range flag.

    Returns ``(fhour, in_range)``. ``fhour`` is the cycle-relative hour nearest the
    window midpoint, clamped to ``[1, MAX_FHOUR]``. ``in_range`` is True only when
    the window's lead from ``now`` falls in the same-day supplement band and the
    forecast hour is within the HREF horizon; otherwise SREF alone covers it.
    """
    now = now or datetime.now(UTC)
    mid = window_start + (window_end - window_start) / 2
    fhour = round((mid - cycle_init).total_seconds() / 3600.0)

    lead_start_h = (window_start - now).total_seconds() / 3600.0
    in_horizon = 1 <= fhour <= MAX_FHOUR
    in_band = lead_start_h <= MAX_LEAD_H and (window_end - now).total_seconds() > 0
    in_range = in_horizon and in_band
    fhour = min(max(fhour, 1), MAX_FHOUR)
    return (fhour if in_range else None), in_range


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


def _domain_max(cycle, fhour, var, prob, polygon, *, fcst=None) -> float | None:
    """HREF neighborhood-probability domain max for one field, or None if absent."""
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

    fhour, in_range = forecast_hour_for_window(
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

    # Flash flood: max of the 1 h (>=0.5 in) and 3 h (>=1 in) neighborhood QPF.
    p1 = _domain_max(cycle, fhour, PRECIP_VAR, PRECIP_1H_PROB, polygon, fcst=accum_window(fhour, 1))
    p3 = _domain_max(cycle, fhour, PRECIP_VAR, PRECIP_3H_PROB, polygon, fcst=accum_window(fhour, 3))
    href_precip = max([v for v in (p1, p3) if v is not None], default=None)

    # Lightning: explicit P(lightning); fall back to the reflectivity proxy.
    href_ltng = _domain_max(cycle, fhour, LTNG_VAR, LTNG_PROB, polygon)
    if href_ltng is None:
        href_ltng = _domain_max(cycle, fhour, REFC_VAR, REFC_PROB, polygon)

    bundle.href_p_precip = href_precip
    bundle.href_p_lightning = href_ltng
    bundle.href_cycle = f"{cycle.date}/{cycle.hh}Z"
    bundle.href_fhour = fhour

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

    cold = fhour < MIN_USEFUL_LEAD_H
    bundle.notes.append(
        f"HREF cycle {bundle.href_cycle} f{fhour:02d}; neighborhood P(QPF) and "
        "P(lightning) over the upstream domain (~3 km same-day supplement)."
        + (" Note: within HREF spin-up window (<6 h); treat as supporting only." if cold else "")
    )
    bundle.sources_ok[NAME] = True
