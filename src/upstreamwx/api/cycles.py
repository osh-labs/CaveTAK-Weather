"""Refresh-cycle arithmetic aligned to the GEFS/REFS + AFD update cadence (PRD FR-12).

Briefings are cached and regenerated on the model cycle, not per request, so reopening
the app costs nothing (PRD §7, §11). The SREF ensemble runs every six hours at
03/09/15/21Z (roadmap §M0.1.1); AFDs are issued roughly twice daily and updated as
needed, comfortably inside that cadence. We therefore key cache validity and the
scheduler to the SREF boundary: a briefing generated for cycle *C* is current until the
next boundary, when the scheduler regenerates it (FR-12).

Pure datetime math — no I/O — so it is deterministic and unit-testable. The always-on
scheduler that *acts* on these boundaries is host-dependent (EC2) and lives in
:mod:`upstreamwx.api.scheduler`; persisting the cache across restarts is M0.1.1 work.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# GEFS/REFS run cycle (UTC hours); both run 00/06/12/18Z. The refresh cadence anchors here.
ENSEMBLE_CYCLE_HOURS: tuple[int, ...] = (0, 6, 12, 18)


def _as_utc(now: datetime) -> datetime:
    """Normalize to an aware UTC datetime (treat naive as UTC)."""
    return now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)


def current_cycle(now: datetime | None = None) -> datetime:
    """The most recent ensemble cycle boundary at or before ``now`` (UTC, hour-floored)."""
    now = _as_utc(now or datetime.now(UTC))
    floor = now.replace(minute=0, second=0, microsecond=0)
    for hour in reversed(ENSEMBLE_CYCLE_HOURS):
        if floor.hour >= hour:
            return floor.replace(hour=hour)
    # Before the day's first boundary -> the last boundary of the previous day.
    prev = floor - timedelta(days=1)
    return prev.replace(hour=ENSEMBLE_CYCLE_HOURS[-1])


def next_cycle(now: datetime | None = None) -> datetime:
    """The next ensemble cycle boundary strictly after ``now`` (UTC)."""
    now = _as_utc(now or datetime.now(UTC))
    floor = now.replace(minute=0, second=0, microsecond=0)
    for hour in ENSEMBLE_CYCLE_HOURS:
        candidate = floor.replace(hour=hour)
        if candidate > now:
            return candidate
    # Past the day's last boundary -> the first boundary of the next day.
    nxt = floor + timedelta(days=1)
    return nxt.replace(hour=ENSEMBLE_CYCLE_HOURS[0])


def cycle_key(now: datetime | None = None) -> str:
    """Stable string id for the current cycle, e.g. ``2026-06-18T15Z``."""
    return current_cycle(now).strftime("%Y-%m-%dT%HZ")


def seconds_until_next_cycle(now: datetime | None = None) -> float:
    """Seconds from ``now`` until the next refresh boundary (>= 0)."""
    now = _as_utc(now or datetime.now(UTC))
    return max(0.0, (next_cycle(now) - now).total_seconds())
