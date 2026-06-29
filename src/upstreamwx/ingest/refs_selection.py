"""Multi-run REFS forecast-hour selection over a mission window (FR-7a).

The REFS analogue of :mod:`upstreamwx.ingest.href_selection`. REFS runs four times a day
(00/06/12/18Z) on a **3-hourly forecast cadence** (:data:`upstreamwx.refs.sources.REFS_FHOURS`).
As with HREF, an *older* run's mature forecast for a given valid time can beat the current run's
spin-up, so for every REFS valid time we read from the **freshest cached run whose forecast hour
is in band** (``[fmin, fmax]`` and published in ``REFS_FHOURS``). When the current run is still
in spin-up for a valid time, selection falls through to the previous run's mature hours
automatically.

The source of truth is the **cache on disk**, not the AWS mirror: selection serves only runs the
scheduler has already warmed, keeping the request path fast (a freshly published but unwarmed run
is picked up on the next scheduler tick, not on a user's click).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..config import Settings, get_settings
from ..refs.sources import REFS_FHOURS, RefsCycle

# Upper product cap (hours of lead from "now"): beyond this GEFS owns the horizon; REFS is the
# same-day convection-allowing supplement (PRD §6.2 FR-7a). REFS reaches f60, but UpstreamWX
# leans on it for the first ~36 h per the transition design (it may be raised toward 48-60).
MAX_LEAD_H = 36.0
DEFAULT_FMIN = 3  # REFS first output is f03
DEFAULT_FMAX = 48
_AVAILABLE = frozenset(REFS_FHOURS)


@dataclass(frozen=True)
class ValidTimeSource:
    """The cached run and forecast hour chosen to cover one valid time."""

    valid_time: datetime
    cycle: RefsCycle
    fhour: int


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _ceil_hour(dt: datetime) -> datetime:
    if dt.minute or dt.second or dt.microsecond:
        return dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return dt


def cached_cycles(
    now: datetime | None = None,
    *,
    settings: Settings | None = None,
    max_back: int = 5,
) -> list[RefsCycle]:
    """REFS cycles present (and non-empty) in the on-disk cache, newest-first.

    Reads ``data_dir/refs/{date}_{hh}`` dirs the scheduler has warmed. Skips empty or malformed
    dirs and any cycle dated in the future relative to ``now``. Capped at ``max_back`` newest.
    This is the source of truth for selection — only warmed runs are served.
    """
    now = _as_utc(now) if now is not None else datetime.now(UTC)
    settings = settings or get_settings()
    root = settings.data_dir / "refs"
    if not root.is_dir():
        return []

    cycles: list[RefsCycle] = []
    for d in root.iterdir():
        if not d.is_dir() or not any(d.iterdir()):
            continue
        try:
            date, hh = d.name.split("_")
            cycle = RefsCycle(date=date, hour=int(hh))
        except (ValueError, KeyError):
            continue
        if cycle.init_time <= now:
            cycles.append(cycle)

    cycles.sort(key=lambda c: c.init_time, reverse=True)
    return cycles[:max_back]


def resolve_valid_time_sources(
    window_start: datetime,
    window_end: datetime,
    *,
    now: datetime | None = None,
    cycles: list[RefsCycle],
    fmin: int = DEFAULT_FMIN,
    fmax: int = DEFAULT_FMAX,
) -> list[ValidTimeSource]:
    """Pick, per REFS valid time in the window, the freshest cached run with an in-band fhour.

    Walks the window hour by hour from ``max(window_start, now)`` (never resolving past valid
    times) up to ``min(window_end, now + MAX_LEAD_H)``. For each candidate valid hour the newest
    ``cycles`` entry whose forecast hour is **published** (in ``REFS_FHOURS``) and within
    ``[fmin, fmax]`` wins; valid hours that fall between REFS's 3-hourly outputs (no in-band
    published fhour from any run) are omitted. ``cycles`` must be newest-first (as
    :func:`cached_cycles` returns).
    """
    now = _as_utc(now) if now is not None else datetime.now(UTC)
    window_start = _as_utc(window_start)
    window_end = _as_utc(window_end)

    end = min(window_end, now + timedelta(hours=MAX_LEAD_H))
    vt = _ceil_hour(max(window_start, now))

    out: list[ValidTimeSource] = []
    while vt <= end:
        for cycle in cycles:  # newest-first
            f = round((vt - cycle.init_time).total_seconds() / 3600.0)
            if fmin <= f <= fmax and f in _AVAILABLE:
                out.append(ValidTimeSource(valid_time=vt, cycle=cycle, fhour=f))
                break  # freshest in-band run wins
        vt += timedelta(hours=1)
    return out
