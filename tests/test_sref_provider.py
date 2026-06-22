"""Offline tests for SREF step-window filtering (FR-7, §16.5).

Tests the ``_filter_steps`` helper and the ``_domain_max`` window-scoping behavior
introduced to fix the bug where the provider collapsed all 87 SREF forecast hours into
a single max regardless of the mission window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import xarray as xr

from upstreamwx.ingest.sref_provider import _filter_steps


def _make_step_da(step_hours: list[int], values: list[float]) -> xr.DataArray:
    """Build a minimal (step, y, x) DataArray for testing."""
    steps = np.array(step_hours, dtype="timedelta64[h]")
    data = np.array([[[v]] for v in values], dtype=float)  # shape (step, y=1, x=1)
    lat = np.array([[45.0]])
    lon = np.array([[-111.0]])
    return xr.DataArray(
        data,
        dims=["step", "y", "x"],
        coords={
            "step": steps,
            "latitude": (["y", "x"], lat),
            "longitude": (["y", "x"], lon),
        },
    )


CYCLE_INIT = datetime(2026, 6, 20, 9, tzinfo=UTC)  # 09Z SREF cycle


# --- _filter_steps: accumulation fields (freq_h=3) --------------------------

def test_filter_keeps_only_overlapping_steps() -> None:
    # Steps at 3h, 6h, 9h. High value only in step 9h.
    # Mission covers hours 0–6 from cycle init → steps 3h and 6h overlap, 9h does not.
    da = _make_step_da([3, 6, 9], [10.0, 20.0, 80.0])
    window_start = CYCLE_INIT
    window_end = CYCLE_INIT + timedelta(hours=6)
    result = _filter_steps(da, CYCLE_INIT, window_start, window_end, freq_h=3)
    assert set(result["step"].values.tolist()) == {
        np.timedelta64(3, "h"),
        np.timedelta64(6, "h"),
    }
    assert float(result.max()) == pytest.approx(20.0)


def test_filter_includes_step_whose_window_straddles_mission_start() -> None:
    # Mission starts at hour 2. Step 3h covers [0, 3], which overlaps [2, 5].
    da = _make_step_da([3, 6, 9], [50.0, 10.0, 10.0])
    window_start = CYCLE_INIT + timedelta(hours=2)
    window_end = CYCLE_INIT + timedelta(hours=5)
    result = _filter_steps(da, CYCLE_INIT, window_start, window_end, freq_h=3)
    step_vals = {int(s / np.timedelta64(1, "h")) for s in result["step"].values}
    assert 3 in step_vals  # [0,3] overlaps [2,5]
    assert 6 in step_vals  # [3,6] overlaps [2,5]
    assert 9 not in step_vals


def test_filter_fallback_when_window_before_first_step() -> None:
    # Mission window ends before the cycle init (entirely in the past relative to cycle).
    # Should fall back to the first step without raising.
    da = _make_step_da([3, 6, 9], [10.0, 20.0, 30.0])
    window_start = CYCLE_INIT - timedelta(hours=2)
    window_end = CYCLE_INIT - timedelta(hours=1)
    result = _filter_steps(da, CYCLE_INIT, window_start, window_end, freq_h=3)
    assert result.sizes["step"] == 1
    assert float(result.values[0, 0, 0]) == pytest.approx(10.0)  # first step value


def test_filter_raises_when_window_beyond_last_step() -> None:
    # Mission window starts after the last available SREF step → error condition.
    da = _make_step_da([3, 6, 9], [10.0, 20.0, 30.0])
    window_start = CYCLE_INIT + timedelta(hours=10)
    window_end = CYCLE_INIT + timedelta(hours=12)
    with pytest.raises(ValueError, match="No SREF steps overlap"):
        _filter_steps(da, CYCLE_INIT, window_start, window_end, freq_h=3)


# --- _filter_steps: instantaneous fields (freq_h=0) -------------------------

def test_filter_instantaneous_includes_steps_within_window() -> None:
    # CAPE snapshots at 3h, 6h, 9h. Mission covers 4–7h → steps 6h only.
    da = _make_step_da([3, 6, 9], [5.0, 90.0, 5.0])
    window_start = CYCLE_INIT + timedelta(hours=4)
    window_end = CYCLE_INIT + timedelta(hours=7)
    result = _filter_steps(da, CYCLE_INIT, window_start, window_end, freq_h=0)
    step_vals = {int(s / np.timedelta64(1, "h")) for s in result["step"].values}
    assert step_vals == {6}
    assert float(result.max()) == pytest.approx(90.0)


def test_filter_instantaneous_fallback_before_first_snapshot() -> None:
    # Mission window ends before the first CAPE snapshot (step 3h).
    da = _make_step_da([3, 6, 9], [10.0, 20.0, 30.0])
    window_start = CYCLE_INIT + timedelta(hours=1)
    window_end = CYCLE_INIT + timedelta(hours=2)
    result = _filter_steps(da, CYCLE_INIT, window_start, window_end, freq_h=0)
    assert result.sizes["step"] == 1
    assert float(result.values[0, 0, 0]) == pytest.approx(10.0)


def test_filter_instantaneous_raises_beyond_last_snapshot() -> None:
    da = _make_step_da([3, 6, 9], [10.0, 20.0, 30.0])
    window_start = CYCLE_INIT + timedelta(hours=10)
    window_end = CYCLE_INIT + timedelta(hours=12)
    with pytest.raises(ValueError, match="No SREF steps overlap"):
        _filter_steps(da, CYCLE_INIT, window_start, window_end, freq_h=0)


# --- _domain_max: verify filter is called when window args are provided ------

def test_domain_max_applies_window_filter() -> None:
    """_domain_max must not include out-of-window steps in the aggregate."""
    from upstreamwx.ingest.sref_provider import _domain_max

    # Build a mock SrefField whose DataArray has a high value only in step 9h.
    da = _make_step_da([3, 6, 9], [10.0, 20.0, 80.0])
    mock_field = MagicMock()
    mock_field.data = da

    mock_polygon = MagicMock()

    # Cycle whose init_time matches CYCLE_INIT.
    mock_cycle = MagicMock()
    mock_cycle.init_time = CYCLE_INIT

    # Mission window covers only hours 0–6 (steps 3h and 6h).
    window_start = CYCLE_INIT
    window_end = CYCLE_INIT + timedelta(hours=6)

    with (
        patch("upstreamwx.ingest.sref_provider.load_probability_field", return_value=mock_field),
        patch(
            "upstreamwx.ingest.sref_provider.aggregate_over_polygon",
            side_effect=lambda da_, polygon, **kw: MagicMock(max_value=float(da_.max())),
        ),
    ):
        result = _domain_max(
            mock_cycle, "APCP", ">6.35", mock_polygon,
            window_start=window_start, window_end=window_end, freq_h=3,
        )

    # Without filtering, max would be 80.0 (step 9h). With filtering, it must be 20.0.
    assert result == pytest.approx(20.0)


def test_domain_max_without_window_args_uses_all_steps() -> None:
    """When window args are absent, behavior is unchanged (no filtering)."""
    from upstreamwx.ingest.sref_provider import _domain_max

    da = _make_step_da([3, 6, 9], [10.0, 20.0, 80.0])
    mock_field = MagicMock()
    mock_field.data = da
    mock_polygon = MagicMock()
    mock_cycle = MagicMock()

    with (
        patch("upstreamwx.ingest.sref_provider.load_probability_field", return_value=mock_field),
        patch(
            "upstreamwx.ingest.sref_provider.aggregate_over_polygon",
            side_effect=lambda da_, polygon, **kw: MagicMock(max_value=float(da_.max())),
        ),
    ):
        result = _domain_max(mock_cycle, "APCP", ">6.35", mock_polygon)

    assert result == pytest.approx(80.0)  # all steps, including the 9h outlier
