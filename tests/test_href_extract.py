"""Offline tests for HREF GRIB2 extraction + idx parsing (Spike C).

Run hermetically against the committed ``href_sample_subset.grib2`` fixture and a
synthetic ``.idx`` string (HREF's 11-member ``0/10`` neighbourhood-probability
grammar). A ``network``-marked test exercises the live NOMADS path and is
deselected by default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from upstreamwx.href.extract import accum_window, open_subset
from upstreamwx.href.fetch import parse_idx, select_messages

# Real HREF prob descriptors (00Z f06): 1h/3h/run-total APCP windows + REFC + LTNG.
SAMPLE_IDX = (
    "49:14468844:d=2026061800:APCP:surface:5-6 hour acc fcst:prob >12.7:prob fcst 0/10:Nbhd\n"
    "53:15292308:d=2026061800:APCP:surface:3-6 hour acc fcst:prob >12.7:prob fcst 0/10:Nbhd\n"
    "9:2297069:d=2026061800:REFC:entire atmosphere:6 hour fcst:prob >40:prob fcst 0/10:Nbhd\n"
    "48:13996244:d=2026061800:LTNG:surface:6 hour fcst:prob >0.2:prob fcst 0/10:Nbhd\n"
)


def test_accum_window_brackets() -> None:
    assert accum_window(6, 1) == "5-6 hour acc"
    assert accum_window(6, 3) == "3-6 hour acc"
    assert accum_window(6, 6) == "0-6 hour acc"
    # Clamps at run start.
    assert accum_window(2, 6) == "0-2 hour acc"


def test_parse_idx_computes_end_offsets() -> None:
    entries = parse_idx(SAMPLE_IDX)
    assert len(entries) == 4
    by_start = sorted(entries, key=lambda e: e.start)
    assert by_start[0].end == by_start[1].start
    assert by_start[-1].end is None
    assert by_start[-1].range_header().endswith("-")


def test_select_messages_filters_by_window_and_var() -> None:
    entries = parse_idx(SAMPLE_IDX)
    # The 1-hour slot bucket is distinguished from the 3-hour one by fcst window.
    slot_1h = select_messages(entries, var="APCP", prob=">12.7", fcst=accum_window(6, 1))
    assert len(slot_1h) == 1 and slot_1h[0].num == 49
    window_3h = select_messages(entries, var="APCP", prob=">12.7", fcst=accum_window(6, 3))
    assert len(window_3h) == 1 and window_3h[0].num == 53
    assert len(select_messages(entries, var="REFC")) == 1
    assert len(select_messages(entries, var="LTNG")) == 1


def test_open_sample_subset(fixtures_dir: Path) -> None:
    ds = open_subset(fixtures_dir / "href_sample_subset.grib2")
    field = ds[list(ds.data_vars)[0]]
    # Single neighbourhood-probability field on the ~3 km HREF CONUS Lambert grid.
    assert {"y", "x"} <= set(field.dims)
    assert field.sizes["y"] == 1025 and field.sizes["x"] == 1473
    assert {"latitude", "longitude"} <= set(field.coords)
    # Probabilities are percentages in [0, 100].
    vmin, vmax = float(field.min()), float(field.max())
    assert 0.0 <= vmin <= vmax <= 100.0


@pytest.mark.network
def test_live_latest_cycle_and_field() -> None:
    from upstreamwx.href import latest_available_cycle, load_probability_field

    cycle = latest_available_cycle()
    assert cycle is not None, "no live HREF cycle found on NOMADS"
    field = load_probability_field(
        cycle, 12, var="APCP", prob=">12.7", fcst=accum_window(12, 1)
    )
    assert field.descriptor_count >= 1
    assert {"y", "x"} <= set(field.data.dims)
