"""Offline tests for the HREF provider (FR-7a) and the engine HREF overlay.

The pure helpers (in-range/forecast-hour resolution, cross-ensemble agreement) are
tested directly. The flash-flood and lightning evaluators are tested with the
``href_*`` inputs set, confirming "show both, higher tier wins" (FR-19) without
touching the SREF-only corpus. A ``network``-marked end-to-end test exercises the
live NOMADS path and is deselected by default.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from upstreamwx.engine.hazards import flash_flood, lightning
from upstreamwx.engine.models import HazardInputs, Tier
from upstreamwx.engine.thresholds import load_thresholds
from upstreamwx.ingest.href_provider import (
    cross_ensemble_agreement,
    forecast_hour_for_window,
)

CONFIG = load_thresholds()


# --- in-range / forecast-hour resolution ------------------------------------

def test_forecast_hour_in_same_day_window() -> None:
    now = datetime(2026, 6, 18, 6, tzinfo=UTC)
    cycle_init = datetime(2026, 6, 18, 0, tzinfo=UTC)  # 00Z cycle
    start = datetime(2026, 6, 18, 17, tzinfo=UTC)      # window ~11-13 h out
    end = datetime(2026, 6, 18, 19, tzinfo=UTC)
    fhour, in_range = forecast_hour_for_window(cycle_init, start, end, now=now)
    assert in_range
    assert fhour == 18  # midpoint 18Z is f18 off the 00Z cycle


def test_window_beyond_supplement_band_is_out_of_range() -> None:
    now = datetime(2026, 6, 18, 6, tzinfo=UTC)
    cycle_init = datetime(2026, 6, 18, 0, tzinfo=UTC)
    start = now + timedelta(hours=60)  # well past the ~36 h band and HREF horizon
    end = start + timedelta(hours=2)
    fhour, in_range = forecast_hour_for_window(cycle_init, start, end, now=now)
    assert not in_range and fhour is None


def test_past_window_is_out_of_range() -> None:
    now = datetime(2026, 6, 18, 18, tzinfo=UTC)
    cycle_init = datetime(2026, 6, 18, 0, tzinfo=UTC)
    start = now - timedelta(hours=5)
    end = now - timedelta(hours=3)
    _fhour, in_range = forecast_hour_for_window(cycle_init, start, end, now=now)
    assert not in_range


# --- cross-ensemble agreement (FR-17, §16.5) --------------------------------

def test_agreement_consistent_when_both_signal() -> None:
    assert cross_ensemble_agreement(55.0, 50.0, 60.0, 55.0) == "consistent"


def test_agreement_partial_on_material_divergence() -> None:
    # SREF strong precip, HREF essentially dry -> divergence caps confidence.
    assert cross_ensemble_agreement(70.0, 10.0, 5.0, 8.0) == "partial"


def test_agreement_consistent_when_a_signal_missing() -> None:
    assert cross_ensemble_agreement(None, None, 80.0, 80.0) == "consistent"


# --- engine HREF overlay: "show both, higher tier wins" (FR-19) -------------

def test_href_raises_flood_tier_above_sref() -> None:
    # SREF dry (Minimal), HREF neighborhood QPF clears its High band -> High.
    inputs = HazardInputs(sref_p_precip=5, measurable_precip=False, href_p_precip=55)
    tier, drivers, _notes = flash_flood.evaluate(inputs, CONFIG.flash_flood)
    assert tier is Tier.HIGH
    assert any("HREF" in d for d in drivers)


def test_href_does_not_lower_a_higher_sref_tier() -> None:
    # SREF High; HREF below its Elevated band -> stays High (higher wins, never lowers).
    inputs = HazardInputs(sref_p_precip=65, measurable_precip=True, href_p_precip=5)
    tier, _drivers, _notes = flash_flood.evaluate(inputs, CONFIG.flash_flood)
    assert tier is Tier.HIGH


def test_href_raises_lightning_tier() -> None:
    inputs = HazardInputs(sref_p_tstm=10, href_p_lightning=65)  # HREF clears Extreme band
    tier, drivers, _notes = lightning.evaluate(inputs, CONFIG.lightning)
    assert tier is Tier.EXTREME
    assert any("HREF" in d for d in drivers)


def test_no_href_inputs_leaves_evaluators_unchanged() -> None:
    # Backward compatibility: href_* None -> identical to the SREF-only path.
    inputs = HazardInputs(sref_p_precip=25, measurable_precip=True)
    tier, _d, _n = flash_flood.evaluate(inputs, CONFIG.flash_flood)
    assert tier is Tier.ELEVATED


@pytest.mark.network
def test_live_provider_populates_bundle() -> None:
    from upstreamwx.engine.models import ActivityType, Mission
    from upstreamwx.href import latest_available_cycle
    from upstreamwx.ingest.base import IngestBundle
    from upstreamwx.ingest.href_provider import fetch

    cycle = latest_available_cycle()
    assert cycle is not None, "no live HREF cycle on NOMADS"
    # Mission window placed ~12 h after the cycle init so it lands in range.
    start = cycle.init_time + timedelta(hours=11)
    mission = Mission(
        activity_type=ActivityType.CANYON,
        lat=37.0192,
        lon=-111.9889,
        window_start=start,
        window_end=start + timedelta(hours=2),
        is_slot=True,
        name="Buckskin",
    )
    import geopandas as gpd
    from shapely.ops import unary_union

    poly = unary_union(
        gpd.read_file("tests/fixtures/buckskin_huc12.geojson").geometry.values
    )
    bundle = IngestBundle()
    fetch(mission, bundle, poly, cycle=cycle, now=start - timedelta(hours=11))
    assert bundle.sources_ok["href"] is True
    assert bundle.href_in_range
    assert bundle.href_fhour is not None
