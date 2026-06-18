"""Offline tests for SREF GRIB2 extraction + idx parsing (Spike A).

Run hermetically against the committed ``sref_sample_subset.grib2`` fixture and a
synthetic ``.idx`` string. A ``network``-marked test exercises the live NOMADS
path and is deselected by default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cavetak_weather.sref.extract import open_subset
from cavetak_weather.sref.fetch import parse_idx, select_messages

SAMPLE_IDX = (
    "182:23009013:d=2026061715:APCP:surface:0-3 hour acc fcst:prob >0.25:prob fcst 0/26:x\n"
    "186:23632071:d=2026061715:APCP:surface:0-3 hour acc fcst:prob >12.7:prob fcst 0/26:x\n"
    "4:132959:d=2026061715:CAPE:surface:anl:prob >250:prob fcst 0/26:x\n"
)


def test_parse_idx_computes_end_offsets() -> None:
    entries = parse_idx(SAMPLE_IDX)
    assert len(entries) == 3
    # Sorted by byte offset; ends are the next entry's start, last is open-ended.
    by_start = sorted(entries, key=lambda e: e.start)
    assert by_start[0].end == by_start[1].start
    assert by_start[-1].end is None
    assert by_start[-1].range_header().endswith("-")


def test_select_messages_filters_by_field() -> None:
    entries = parse_idx(SAMPLE_IDX)
    apcp = select_messages(entries, var="APCP")
    assert len(apcp) == 2
    slot = select_messages(entries, var="APCP", prob=">12.7")
    assert len(slot) == 1 and slot[0].num == 186
    cape = select_messages(entries, var="CAPE")
    assert len(cape) == 1


def test_open_sample_subset(fixtures_dir: Path) -> None:
    da = open_subset(fixtures_dir / "sref_sample_subset.grib2")
    # Single APCP probability field, possibly stacked on step, on the pgrb132 grid.
    var = list(da.data_vars)[0]
    field = da[var]
    assert {"y", "x"} <= set(field.dims)
    assert field.sizes["y"] == 553 and field.sizes["x"] == 697
    assert {"latitude", "longitude"} <= set(field.coords)
    # Probabilities are percentages in [0, 100].
    vmin, vmax = float(field.min()), float(field.max())
    assert 0.0 <= vmin <= vmax <= 100.0


@pytest.mark.network
def test_live_latest_cycle_and_field() -> None:
    from cavetak_weather.sref import latest_available_cycle, load_probability_field

    cycle = latest_available_cycle()
    assert cycle is not None, "no live SREF cycle found on NOMADS"
    field = load_probability_field(cycle, var="APCP", prob=">12.7", fcst="0-3 hour acc")
    assert field.descriptor_count >= 1
    assert {"y", "x"} <= set(field.data.dims)
