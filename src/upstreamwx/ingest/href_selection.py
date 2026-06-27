"""Multi-run HREF forecast-hour selection over a mission window (roadmap §M0.1.1, FR-7a).

HREF runs twice a day (00/12Z) and its first few forecast hours (f01-f05) are spin-up — the
prevailing view is that an *older* run's mature forecast for a given valid time beats the
current run's spin-up for the same time. We exploit that with the persistent cache
(:mod:`upstreamwx.href.cache`): the scheduler warms **f06-f48** of each run and keeps several
recent runs, so for every valid hour we can read from the **freshest cached run whose
forecast hour for that valid time is >= ``fmin``** (6). When the current run is still in
spin-up for a valid time, selection falls through to the previous run's mature hours
automatically — no separate spin-up model (HRRR/Open-Meteo) needed.

The rule is spacing-agnostic: nothing hard-codes the 12 h run cadence; it is pure per-cycle
forecast-hour arithmetic, so it still holds if HREF's cadence changes. The source of truth is
the **cache on disk**, not NOMADS: selection serves only runs the scheduler has already
warmed, which keeps the request path fast (a freshly published but unwarmed run is picked up
on the next scheduler tick, not paid for on a user's click).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..config import Settings, get_settings
from ..href.sources import HrefCycle

# Upper product cap (hours of lead from "now"): beyond this SREF owns the horizon; HREF is a
# same-day supplement (PRD §6.2 FR-7a). The per-run spin-up floor is ``fmin`` below.
MAX_LEAD_H = 36.0
DEFAULT_FMIN = 6
DEFAULT_FMAX = 48


@dataclass(frozen=True)
class ValidTimeSource:
    """The cached run and forecast hour chosen to cover one valid time."""

    valid_time: datetime
    cycle: HrefCycle
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
    max_back: int = 4,
) -> list[HrefCycle]:
    """HREF cycles present (and non-empty) in the on-disk cache, newest-first.

    Reads ``data_dir/href/{date}_{hh}`` dirs the scheduler has warmed. Skips empty or
    malformed dirs and any cycle dated in the future relative to ``now``. Capped at
    ``max_back`` newest (pruning normally keeps fewer). This is the source of truth for
    selection — only warmed runs are served.
    """
    now = _as_utc(now) if now is not None else datetime.now(UTC)
    settings = settings or get_settings()
    root = settings.data_dir / "href"
    if not root.is_dir():
        return []

    cycles: list[HrefCycle] = []
    for d in root.iterdir():
        if not d.is_dir() or not any(d.iterdir()):
            continue
        try:
            date, hh = d.name.split("_")
            cycle = HrefCycle(date=date, hour=int(hh))
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
    cycles: list[HrefCycle],
    fmin: int = DEFAULT_FMIN,
    fmax: int = DEFAULT_FMAX,
) -> list[ValidTimeSource]:
    """Pick, per valid hour in the window, the freshest cached run with fhour in ``[fmin, fmax]``.

    Walks the window hour by hour from ``max(window_start, now)`` (never resolving past valid
    times) up to ``min(window_end, now + MAX_LEAD_H)`` (the same-day cap; SREF owns beyond).
    For each valid hour the newest ``cycles`` entry whose forecast hour is in band wins; a
    current run still in spin-up (fhour < ``fmin``) is skipped, so the previous run's mature
    hour backfills it. Valid hours no cached run covers are omitted (HREF absent there; SREF
    covers). ``cycles`` must be newest-first (as :func:`cached_cycles` returns).
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
            if fmin <= f <= fmax:
                out.append(ValidTimeSource(valid_time=vt, cycle=cycle, fhour=f))
                break  # freshest in-band run wins
        vt += timedelta(hours=1)
    return out
