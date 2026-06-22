"""SREF provider — ensemble probability over the upstream domain (FR-7).

Thin wrapper over the M0.0 SREF pipeline (``upstreamwx.sref``): find the latest
cycle, subset the probability fields, filter to the steps that overlap the mission
window, and aggregate the conservative max over the upstream watershed polygon.
The ensemble probability is itself the fraction of members exceeding the threshold,
so it doubles as the member-support input for the confidence qualifier (§16.5).

Heavy/scheduled orchestration (cron at the SREF cadence, persistent multi-cycle
cache) is deferred to M0.1.1 / the EC2 instance; this is the on-demand processing
logic those will invoke.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import xarray as xr
from shapely.geometry.base import BaseGeometry

from ..engine.models import Mission
from ..sref import aggregate_over_polygon, latest_available_cycle, load_probability_field
from .base import IngestBundle

NAME = "sref"

# Precip-probability proxy: P(3-h accumulation > 6.35 mm ≈ 0.25 in) over the domain.
PRECIP_VAR, PRECIP_PROB, PRECIP_FREQ = "APCP", ">6.35", "3hrly"
# Thunderstorm proxy: P(CAPE > 1000 J/kg) — convective instability over the domain.
TSTM_VAR, TSTM_PROB = "CAPE", ">1000"


def _as_utc(dt: datetime) -> datetime:
    """Coerce a naïve datetime to UTC-aware for comparison with the cycle clock."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _filter_steps(
    da: xr.DataArray,
    cycle_init: datetime,
    window_start: datetime,
    window_end: datetime,
    *,
    freq_h: int,
) -> xr.DataArray:
    """Filter a step-indexed DataArray to steps that overlap the mission window.

    For accumulation fields (freq_h > 0): a step S covers [S − freq_h, S] in hours
    since cycle init. The window [t0, t1] overlaps when S > t0 AND (S − freq_h) < t1.
    For instantaneous fields (freq_h == 0): include steps where t0 ≤ S ≤ t1.

    Fallback when no steps match:
    - Window entirely before the first accumulation period start (cycle-relative t1 ≤ 0
      for accumulation, or t1 < step_h[0] for instantaneous) → use the first step as
      the nearest future snapshot.
    - Any other empty-mask condition (e.g., window beyond the SREF horizon) → raise
      ValueError so the caller can mark SREF unavailable via graceful degradation (NFR-6).
    """
    t0 = (_as_utc(window_start) - _as_utc(cycle_init)).total_seconds() / 3600.0
    t1 = (_as_utc(window_end) - _as_utc(cycle_init)).total_seconds() / 3600.0
    step_h = (da["step"].values / np.timedelta64(1, "h")).astype(float)

    if freq_h > 0:
        mask = (step_h > t0) & ((step_h - freq_h) < t1)
    else:
        mask = (step_h >= t0) & (step_h <= t1)

    if mask.any():
        return da.isel(step=mask)

    # No steps survived. Determine which fallback applies.
    first_window_start = 0.0 if freq_h > 0 else float(step_h[0])
    if t1 <= first_window_start:
        # Window is entirely before the first available step; use it as the nearest future value.
        return da.isel(step=[0])

    raise ValueError(
        f"No SREF steps overlap the mission window "
        f"({t0:.1f}–{t1:.1f} h from cycle init); "
        f"available steps cover {step_h[0]:.0f}–{step_h[-1]:.0f} h. "
        "Ensure the mission window falls within the SREF forecast horizon."
    )


def _domain_max(
    cycle,
    var: str,
    prob: str,
    polygon: BaseGeometry,
    *,
    freq: str | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    freq_h: int = 3,
) -> float | None:
    """Fetch, optionally window-filter, and spatially aggregate one SREF probability field."""
    field = load_probability_field(cycle, var=var, prob=prob, freq=freq or "3hrly")
    da = field.data
    if window_start is not None and window_end is not None and "step" in da.dims:
        da = _filter_steps(da, cycle.init_time, window_start, window_end, freq_h=freq_h)
    agg = aggregate_over_polygon(da, polygon, field_name=var, threshold=prob)
    return agg.max_value


def fetch(mission: Mission, bundle: IngestBundle, polygon: BaseGeometry, *, cycle=None) -> None:
    """Populate SREF probabilities + member support over the upstream domain."""
    cycle = cycle or latest_available_cycle()
    if cycle is None:
        bundle.sources_ok[NAME] = False
        bundle.notes.append("SREF: no available cycle on NOMADS (retention/lag).")
        return

    try:
        p_precip = _domain_max(
            cycle, PRECIP_VAR, PRECIP_PROB, polygon,
            freq=PRECIP_FREQ,
            window_start=mission.window_start,
            window_end=mission.window_end,
            freq_h=3,
        )
        p_tstm = _domain_max(
            cycle, TSTM_VAR, TSTM_PROB, polygon,
            window_start=mission.window_start,
            window_end=mission.window_end,
            freq_h=0,  # CAPE is an instantaneous snapshot, not an accumulation
        )
    except ValueError as exc:
        bundle.sources_ok[NAME] = False
        bundle.notes.append(f"SREF: {exc}")
        return

    bundle.sref_p_precip = p_precip
    bundle.sref_p_tstm = p_tstm
    # Probability == fraction of members exceeding the threshold == member support.
    if p_precip is not None:
        bundle.member_support["flash_flood"] = p_precip / 100.0
    if p_tstm is not None:
        bundle.member_support["lightning"] = p_tstm / 100.0
    bundle.notes.append(
        f"SREF cycle {cycle.date}/{cycle.hh}Z; P(precip>6.35mm/3h) and P(CAPE>1000) "
        "used as precip/thunderstorm proxies over the upstream domain "
        f"(window {mission.window_start}–{mission.window_end})."
    )
    bundle.sources_ok[NAME] = True
