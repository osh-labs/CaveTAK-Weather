"""Offline tests for mission-local timezone resolution (FR-9, §6.4).

The window a user enters is local wall-clock time at the trip point; these tests pin
that we attach the point's IANA zone (so the engine's UTC math and the SITREP's
local-time display agree) and that aware datetimes and unresolvable points are handled.
timezonefinder is offline, so the whole module is hermetic — no ``network`` marker.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import yaml

from upstreamwx.api.models import MissionSpec
from upstreamwx.api.service import BriefingService
from upstreamwx.timezones import localize, localize_window, resolve_zone

# Buckskin Gulch (the repo's canonical canyon point) sits in Utah -> America/Denver.
BUCKSKIN_LAT, BUCKSKIN_LON = 37.0192, -111.9889


def test_resolve_zone_for_contiguous_us_point() -> None:
    assert resolve_zone(BUCKSKIN_LAT, BUCKSKIN_LON).key == "America/Denver"


def test_resolve_zone_respects_dst_for_the_window_date() -> None:
    zone = resolve_zone(BUCKSKIN_LAT, BUCKSKIN_LON)
    # Mountain Daylight in June (-6 h), Mountain Standard in January (-7 h).
    assert localize(datetime(2026, 6, 25, 8), zone).utcoffset() == timedelta(hours=-6)
    assert localize(datetime(2026, 1, 15, 8), zone).utcoffset() == timedelta(hours=-7)


def test_resolve_zone_handles_no_dst_zone() -> None:
    # Arizona (Phoenix) does not observe DST; offset is -7 h year-round.
    zone = resolve_zone(33.45, -112.07)
    assert zone.key == "America/Phoenix"
    assert localize(datetime(2026, 6, 25, 8), zone).utcoffset() == timedelta(hours=-7)


def test_localize_attaches_zone_to_naive_datetime() -> None:
    zone = resolve_zone(BUCKSKIN_LAT, BUCKSKIN_LON)
    out = localize(datetime(2026, 6, 25, 8, 0), zone)
    assert out.tzinfo is not None
    assert out.hour == 8  # wall-clock preserved, not shifted
    assert out.astimezone(UTC).hour == 14  # 08:00 MDT == 14:00 UTC


def test_localize_passes_aware_datetime_through_unchanged() -> None:
    zone = resolve_zone(BUCKSKIN_LAT, BUCKSKIN_LON)
    aware = datetime(2026, 6, 25, 8, 0, tzinfo=UTC)
    assert localize(aware, zone) is aware


def test_localize_none_is_none() -> None:
    assert localize(None, resolve_zone(BUCKSKIN_LAT, BUCKSKIN_LON)) is None


def test_localize_window_localizes_all_markers_and_preserves_none() -> None:
    start, end, approach, egress = localize_window(
        BUCKSKIN_LAT,
        BUCKSKIN_LON,
        datetime(2026, 6, 25, 8),
        datetime(2026, 6, 25, 18),
        approach_end=datetime(2026, 6, 25, 9),
        egress_start=None,
    )
    assert start.utcoffset() == timedelta(hours=-6)
    assert end.utcoffset() == timedelta(hours=-6)
    assert approach.utcoffset() == timedelta(hours=-6)
    assert egress is None


def test_mission_spec_to_mission_is_local_aware() -> None:
    """The API request boundary localizes the naive window to the point's zone."""
    spec = MissionSpec(
        lat=BUCKSKIN_LAT,
        lon=BUCKSKIN_LON,
        activity="canyon",
        start="2026-06-25T08:00",
        end="2026-06-25T18:00",
        name="Buckskin Gulch",
    )
    mission = spec.to_mission()
    assert mission.window_start.utcoffset() == timedelta(hours=-6)
    # 08:00 local is 14:00Z — the value the SREF/HREF cycle math actually consumes.
    assert mission.window_start.astimezone(UTC) == datetime(2026, 6, 25, 14, tzinfo=UTC)


def test_structured_contract_carries_local_zone(tmp_path) -> None:
    """The PWA contract reports the mission-local abbreviation + IANA zone (FR-9)."""
    inputs = yaml.safe_load(open("tests/fixtures/sitrep/sample_inputs.yaml"))
    spec = MissionSpec(
        lat=BUCKSKIN_LAT,
        lon=BUCKSKIN_LON,
        activity="canyon",
        start="2026-06-25T08:00",
        end="2026-06-25T18:00",
        name="Buckskin Gulch",
        frame=False,
        inputs=inputs,
    )
    resp = BriefingService().get_briefing(spec)
    # mission is a typed MissionView (extra="allow"), so attribute access.
    assert resp.mission.timezone == "MDT"
    assert resp.mission.tz_name == "America/Denver"
    assert resp.mission.window_start == "2026-06-25T08:00:00-06:00"
