"""Mission-local timezone resolution (FR-9, §6.4).

Mission windows are entered as local wall-clock times at the trip location (the PWA
sends a naive ``HH:MM`` the user typed against the map point; the CLI takes the same
naive ISO string). The engine and ingest layer do all SREF/HREF cycle/step
arithmetic in **UTC**, and the SITREP renders the window in **local** time with its
UTC-offset label (``sitrep.structured._tz_label``, ``ingest.openmeteo`` display
series). Both only behave correctly when the window datetimes are *timezone-aware in
the mission's own zone*, so we resolve the IANA zone for the point and attach it to
any naive window datetime at the request boundary (``api.models`` / ``sitrep.cli``).

Resolution is fully offline (timezonefinder's bundled polygons). A point with no
resolvable zone (e.g. offshore) falls back to UTC so a briefing still renders rather
than crashing (NFR-6).
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC_ZONE = ZoneInfo("UTC")


@lru_cache(maxsize=1)
def _finder():
    """Lazily build the (heavy) TimezoneFinder singleton, reused across requests.

    A missing ``timezonefinder`` is a broken deployment, not a per-point edge case:
    we let the ImportError propagate rather than silently treat every mission as UTC,
    which would mis-time every briefing for a safety-critical product.
    """
    from timezonefinder import TimezoneFinder

    return TimezoneFinder()


@lru_cache(maxsize=256)
def resolve_zone(lat: float, lon: float) -> ZoneInfo:
    """Resolve the IANA timezone for a point, falling back to UTC when unknown (NFR-6).

    Cached because a single mission resolves the same point for all four window
    datetimes and the PWA re-fetches the same point on every edit. Only a genuinely
    unresolvable point (no zone, or a name absent from the zoneinfo db) degrades to
    UTC; an import/config failure surfaces via :func:`_finder`.
    """
    name = _finder().timezone_at(lat=lat, lng=lon)
    if not name:
        return UTC_ZONE
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return UTC_ZONE


def localize(dt: datetime | None, zone: ZoneInfo) -> datetime | None:
    """Interpret a naive datetime as wall-clock time in ``zone``; pass aware dt through.

    An explicit offset the caller already supplied (tz-aware) is respected as-is.
    """
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=zone)


def localize_window(
    lat: float,
    lon: float,
    start: datetime,
    end: datetime,
    approach_end: datetime | None = None,
    egress_start: datetime | None = None,
) -> tuple[datetime, datetime, datetime | None, datetime | None]:
    """Localize a mission's window/phase markers to the point's local zone (FR-9).

    Returns the four datetimes with any naive value made tz-aware in the resolved
    zone, so the engine's UTC math and the SITREP's local-time display agree.
    """
    zone = resolve_zone(lat, lon)
    return (
        localize(start, zone),
        localize(end, zone),
        localize(approach_end, zone),
        localize(egress_start, zone),
    )
