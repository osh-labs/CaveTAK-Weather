"""Refresh-cycle arithmetic and the scheduler refresh pass (M0.3, FR-12).

Pure/host-independent pieces of the scheduling story — boundary math and a single
refresh pass — are unit-tested here. The always-on loop cadence and cross-restart
persistence are EC2-validated (roadmap §M0.1.1), not in this container.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from upstreamwx.api.cache import STATIC_TOKEN, BriefingCache, mission_cache_key
from upstreamwx.api.cycles import (
    current_cycle,
    cycle_key,
    next_cycle,
    seconds_until_next_cycle,
)
from upstreamwx.api.models import MissionSpec
from upstreamwx.api.service import BriefingService
from upstreamwx.engine.models import HazardInputs
from upstreamwx.sitrep import generate as generate_mod


def _utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=UTC)


@pytest.mark.parametrize(
    "now,expected_hour,expected_day",
    [
        (_utc(2026, 6, 18, 16), 15, 18),   # mid-afternoon -> 15Z
        (_utc(2026, 6, 18, 3), 3, 18),     # exactly on a boundary -> that boundary
        (_utc(2026, 6, 18, 1), 21, 17),    # before first boundary -> prior day's 21Z
        (_utc(2026, 6, 18, 9, 59), 9, 18),
        (_utc(2026, 6, 18, 23), 21, 18),
    ],
)
def test_current_cycle(now, expected_hour, expected_day):
    c = current_cycle(now)
    assert (c.hour, c.day) == (expected_hour, expected_day)


@pytest.mark.parametrize(
    "now,expected_hour,expected_day",
    [
        (_utc(2026, 6, 18, 16), 21, 18),
        (_utc(2026, 6, 18, 3), 9, 18),     # strictly after, not equal
        (_utc(2026, 6, 18, 23), 3, 19),    # rolls to next day's first boundary
        (_utc(2026, 6, 18, 1), 3, 18),
    ],
)
def test_next_cycle(now, expected_hour, expected_day):
    n = next_cycle(now)
    assert (n.hour, n.day) == (expected_hour, expected_day)


def test_cycle_key_and_seconds():
    assert cycle_key(_utc(2026, 6, 18, 16)) == "2026-06-18T15Z"
    assert seconds_until_next_cycle(_utc(2026, 6, 18, 16)) == 5 * 3600  # 16Z -> 21Z


def test_naive_datetime_treated_as_utc():
    assert current_cycle(datetime(2026, 6, 18, 16)).hour == 15


def test_cache_hit_miss_by_cycle():
    """A cached live briefing is valid for its cycle and misses in the next (FR-12)."""
    cache = BriefingCache()

    class _Stub:
        markdown = "x"

    key = "k"
    cache.put(key, _Stub(), token="2026-06-18T15Z")
    assert cache.get(key, "2026-06-18T15Z") is not None   # hit, same cycle
    assert cache.get(key, "2026-06-18T21Z") is None       # miss, new cycle
    assert cache.get("other", "2026-06-18T15Z") is None   # miss, unknown key
    # A static entry is valid regardless of cycle (deterministic inputs).
    cache.put("s", _Stub(), token=STATIC_TOKEN)
    assert cache.get("s", "2026-06-18T21Z") is not None


def test_mission_key_stability_and_sensitivity():
    spec = MissionSpec(
        lat=37.0192, lon=-111.9889, activity="canyon",
        start="2026-06-20T08:00", end="2026-06-20T18:00", slot=True,
    )
    m1 = spec.to_mission()
    m2 = spec.model_copy(update={"name": "renamed"}).to_mission()
    assert mission_cache_key(m1) == mission_cache_key(m2)  # name is not part of identity
    moved = spec.model_copy(update={"lat": 38.0}).to_mission()
    assert mission_cache_key(m1) != mission_cache_key(moved)  # location is


def _offline_service(monkeypatch) -> BriefingService:
    """A service whose generator never hits the network (forces empty inputs)."""
    real = generate_mod.generate_briefing

    def offline(mission, *, inputs=None, frame=None, generated_at=None, cycle=None):
        return real(mission, inputs=inputs or HazardInputs(), frame=False,
                    generated_at=generated_at)

    monkeypatch.setattr("upstreamwx.api.service.generate_briefing", offline)
    return BriefingService()


def test_scheduler_refreshes_active_in_range_missions(monkeypatch):
    service = _offline_service(monkeypatch)
    spec = MissionSpec(
        lat=37.0, lon=-112.0, activity="canyon",
        start="2026-06-20T08:00", end="2026-06-20T18:00", frame=False,
    )  # inputs=None -> a live mission, registered for refresh

    before = _utc(2026, 6, 19, 12)  # before the window -> in range, gets registered
    first = service.get_briefing(spec, now=before)
    assert first.cached is False
    assert service.active_count == 1

    # A refresh pass in the next cycle regenerates it and updates the cache token.
    next_pass = _utc(2026, 6, 19, 16)
    assert service.refresh_active(now=next_pass) == 1
    served = service.get_briefing(spec, now=next_pass)
    assert served.cached is True
    assert served.cache_cycle == cycle_key(next_pass)


def test_scheduler_drops_ended_missions(monkeypatch):
    service = _offline_service(monkeypatch)
    spec = MissionSpec(
        lat=37.0, lon=-112.0, activity="canyon",
        start="2026-06-20T08:00", end="2026-06-20T18:00", frame=False,
    )
    service.get_briefing(spec, now=_utc(2026, 6, 19, 12))
    assert service.active_count == 1
    # A pass after the window has ended drops the mission and refreshes nothing. The
    # 18:00 window end is local (this point is Mountain time), so it ends ~00:00-01:00Z
    # on the 21st; use a later UTC `now` that is unambiguously past it.
    assert service.refresh_active(now=_utc(2026, 6, 21, 12)) == 0
    assert service.active_count == 0


def test_offline_inputs_mission_not_registered(monkeypatch):
    """Deterministic (explicit-inputs) briefings need no scheduled refresh."""
    service = _offline_service(monkeypatch)
    spec = MissionSpec(
        lat=37.0, lon=-112.0, activity="canyon",
        start="2026-06-20T08:00", end="2026-06-20T18:00",
        frame=False, inputs={"sref_p_precip": 50},
    )
    service.get_briefing(spec, now=_utc(2026, 6, 19, 12))
    assert service.active_count == 0
