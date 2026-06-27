"""Offline tests for multi-run HREF valid-time selection (roadmap §M0.1.1, FR-7a).

Pure logic — no network, no fixtures. Exercises the rule "for each valid hour pick the
freshest cached run whose forecast hour is >= fmin (6)", including the spin-up backfill from
a prior run, the same-day clamp, and the disk-backed ``cached_cycles`` discovery.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from upstreamwx.config import Settings
from upstreamwx.href.sources import HrefCycle
from upstreamwx.ingest.href_selection import (
    MAX_LEAD_H,
    cached_cycles,
    resolve_valid_time_sources,
)


def _runs(*pairs: tuple[str, int]) -> list[HrefCycle]:
    """Build a newest-first cycle list from (date, hour) pairs."""
    return [HrefCycle(date=d, hour=h) for d, h in pairs]


# --- resolve_valid_time_sources ---------------------------------------------

def test_current_run_wins_when_past_spinup() -> None:
    """A window fully past the latest run's spin-up reads entirely from that run."""
    now = datetime(2026, 6, 20, 10, tzinfo=UTC)
    cycles = _runs(("20260620", 0), ("20260619", 12))  # 00Z newest, then prev 12Z
    src = resolve_valid_time_sources(now, now + timedelta(hours=4), now=now, cycles=cycles)
    assert [s.fhour for s in src] == [10, 11, 12, 13, 14]
    assert all(s.cycle.hh == "00" for s in src)  # all from the newest run


def test_previous_run_backfills_spinup() -> None:
    """When the newest run is in spin-up (fhour < 6) for a valid hour, the prior run covers it."""
    now = datetime(2026, 6, 20, 13, tzinfo=UTC)
    cycles = _runs(("20260620", 12), ("20260620", 0))  # 12Z newest (f01-f03 here), prev 00Z
    src = resolve_valid_time_sources(now, now + timedelta(hours=2), now=now, cycles=cycles)
    # 13-15Z would be f01-f03 of the 12Z run (spin-up) -> served from 00Z run at f13-f15.
    assert [(s.cycle.hh, s.fhour) for s in src] == [("00", 13), ("00", 14), ("00", 15)]


def test_mixed_window_uses_each_run_where_freshest() -> None:
    """A window straddling the spin-up boundary mixes prior-run backfill and current-run hours."""
    now = datetime(2026, 6, 20, 14, tzinfo=UTC)
    cycles = _runs(("20260620", 12), ("20260620", 0))
    # 14-19Z: 12Z run is f02-f07. f02-f05 (<6) backfill from 00Z (f14-f17); f06-f07 from 12Z.
    src = resolve_valid_time_sources(now, now + timedelta(hours=5), now=now, cycles=cycles)
    got = [(s.cycle.hh, s.fhour) for s in src]
    assert got == [("00", 14), ("00", 15), ("00", 16), ("00", 17), ("12", 6), ("12", 7)]


def test_valid_hours_beyond_all_horizons_are_omitted() -> None:
    """A valid hour no cached run covers in [fmin, fmax] yields no source (SREF covers)."""
    now = datetime(2026, 6, 20, 0, tzinfo=UTC)
    cycles = _runs(("20260620", 0))  # single run; fmax=48
    # Ask for a 2-hour window but cap fmax low so the late hours fall out of band.
    src = resolve_valid_time_sources(
        now + timedelta(hours=10), now + timedelta(hours=13), now=now, cycles=cycles, fmax=11
    )
    # Only f10, f11 are <= fmax; f12, f13 omitted.
    assert [s.fhour for s in src] == [10, 11]


def test_empty_when_no_run_in_band() -> None:
    """A near-term window where every run is still in spin-up resolves to nothing."""
    now = datetime(2026, 6, 20, 1, tzinfo=UTC)
    cycles = _runs(("20260620", 0))  # 00Z run: 01-03Z is f01-f03 (< fmin)
    src = resolve_valid_time_sources(now, now + timedelta(hours=2), now=now, cycles=cycles)
    assert src == []


def test_max_lead_clamp_hands_off_to_sref() -> None:
    """Valid hours beyond MAX_LEAD_H from now are dropped (SREF owns the longer horizon)."""
    now = datetime(2026, 6, 20, 0, tzinfo=UTC)
    cycles = _runs(("20260620", 0))
    end = now + timedelta(hours=MAX_LEAD_H + 6)  # window extends 6 h past the cap
    src = resolve_valid_time_sources(now, end, now=now, cycles=cycles)
    last_lead = (src[-1].valid_time - now).total_seconds() / 3600.0
    assert last_lead <= MAX_LEAD_H


def test_selection_is_run_cadence_agnostic() -> None:
    """The rule is pure fhour arithmetic — it holds for a non-12 h (e.g. 6 h) cadence."""
    now = datetime(2026, 6, 20, 8, tzinfo=UTC)
    cycles = _runs(("20260620", 6), ("20260620", 0))  # 6-hourly cadence
    # 08-10Z: 06Z run is f02-f04 (spin-up) -> backfill from 00Z at f08-f10.
    src = resolve_valid_time_sources(now, now + timedelta(hours=2), now=now, cycles=cycles)
    assert [(s.cycle.hh, s.fhour) for s in src] == [("00", 8), ("00", 9), ("00", 10)]


# --- cached_cycles (disk discovery) -----------------------------------------

def test_cached_cycles_lists_nonempty_dirs_newest_first(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    root = tmp_path / "href"
    for name in ("20260620_00", "20260619_12", "20260619_00"):
        d = root / name
        d.mkdir(parents=True)
        (d / "f06_APCP_gt12.7.grib2").write_bytes(b"x")
    (root / "20260620_12_empty").mkdir()  # empty + malformed name -> skipped
    (root / "garbage").mkdir()

    now = datetime(2026, 6, 20, 18, tzinfo=UTC)
    cycles = cached_cycles(now, settings=settings)
    assert [(c.date, c.hh) for c in cycles] == [
        ("20260620", "00"),
        ("20260619", "12"),
        ("20260619", "00"),
    ]


def test_cached_cycles_skips_future_dated_runs(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    root = tmp_path / "href"
    for name in ("20260620_00", "20260620_12"):
        d = root / name
        d.mkdir(parents=True)
        (d / "f06_APCP_gt12.7.grib2").write_bytes(b"x")
    now = datetime(2026, 6, 20, 6, tzinfo=UTC)  # before the 12Z run inits
    cycles = cached_cycles(now, settings=settings)
    assert [(c.date, c.hh) for c in cycles] == [("20260620", "00")]


def test_cached_cycles_empty_when_no_cache(tmp_path: Path) -> None:
    now = datetime(2026, 6, 20, tzinfo=UTC)
    assert cached_cycles(now, settings=Settings(data_dir=tmp_path)) == []
