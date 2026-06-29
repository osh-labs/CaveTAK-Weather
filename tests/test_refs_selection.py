"""REFS multi-run valid-time selection over a mission window (FR-7a).

The REFS analogue of the old HREF selection tests. REFS runs 00/06/12/18Z on a 3-hourly
forecast cadence (f03-f48 step 3, then 54/60), so selection must (a) only pick valid times that
land on a published REFS forecast hour, and (b) backfill a current run's spin-up hours from the
previous run's mature forecast. Pure datetime arithmetic — no network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from upstreamwx.ingest.refs_selection import MAX_LEAD_H, resolve_valid_time_sources
from upstreamwx.refs.sources import REFS_FHOURS, RefsCycle


def _utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=UTC)


def test_picks_freshest_run_and_only_published_fhours():
    now = _utc(2026, 6, 20, 12)
    cycles = [RefsCycle("20260620", 12), RefsCycle("20260620", 6)]  # newest-first
    out = resolve_valid_time_sources(
        now, _utc(2026, 6, 20, 21), now=now, cycles=cycles
    )
    by_vt = {s.valid_time.hour: (s.cycle.hour, s.fhour) for s in out}

    # 15Z/18Z/21Z land on the 12Z run's published f03/f06/f09.
    assert by_vt[15] == (12, 3)
    assert by_vt[18] == (12, 6)
    assert by_vt[21] == (12, 9)
    # 13Z/14Z fall between REFS's 3-hourly outputs for both runs -> no source.
    assert 13 not in by_vt and 14 not in by_vt
    # Every chosen forecast hour is actually published by REFS.
    assert all(s.fhour in set(REFS_FHOURS) for s in out)


def test_spinup_backfilled_from_previous_run():
    # 12Z run is still in spin-up for the 12Z valid time (f00 < fmin); the 06Z run's mature
    # f06 backfills it.
    now = _utc(2026, 6, 20, 12)
    cycles = [RefsCycle("20260620", 12), RefsCycle("20260620", 6)]
    out = resolve_valid_time_sources(now, _utc(2026, 6, 20, 12), now=now, cycles=cycles)
    assert [(s.cycle.hour, s.fhour) for s in out] == [(6, 6)]


def test_window_capped_at_max_lead():
    # Nothing beyond now + MAX_LEAD_H is resolved (GEFS owns that horizon).
    now = _utc(2026, 6, 20, 0)
    cycles = [RefsCycle("20260620", 0)]
    out = resolve_valid_time_sources(
        now, _utc(2026, 6, 22, 0), now=now, cycles=cycles
    )
    assert out, "expected some in-range sources"
    assert max(s.valid_time for s in out) <= now + timedelta(hours=MAX_LEAD_H)


def test_no_cycles_yields_no_sources():
    now = _utc(2026, 6, 20, 12)
    assert resolve_valid_time_sources(now, _utc(2026, 6, 20, 18), now=now, cycles=[]) == []
