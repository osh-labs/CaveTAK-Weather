"""Offline tests for the HREF provider (FR-7a) and the engine HREF overlay.

Cross-ensemble agreement and the engine evaluators are tested directly. The provider's
multi-run orchestration (read per (cycle, fhour) from the cache, conservative-max aggregate,
spin-up-backfill provenance) is tested hermetically by faking the cached loader and the
polygon reducer. A ``network``-marked end-to-end test exercises the live NOMADS path and is
deselected by default.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from upstreamwx.config import Settings
from upstreamwx.engine.hazards import flash_flood, lightning
from upstreamwx.engine.models import ActivityType, HazardInputs, Mission, Tier
from upstreamwx.engine.thresholds import load_thresholds
from upstreamwx.ingest.base import IngestBundle
from upstreamwx.ingest.href_provider import cross_ensemble_agreement, fetch

CONFIG = load_thresholds()


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


# --- provider multi-run orchestration ---------------------------------------

def _mission(now: datetime, *, lead_h: float, dur_h: float) -> Mission:
    start = now + timedelta(hours=lead_h)
    return Mission(
        activity_type=ActivityType.CANYON,
        lat=37.0192,
        lon=-111.9889,
        window_start=start,
        window_end=start + timedelta(hours=dur_h),
        is_slot=True,
        name="Buckskin",
    )


def _seed_cycle_dirs(data_dir: Path, *names: str) -> None:
    for name in names:
        d = data_dir / "href" / name
        d.mkdir(parents=True)
        (d / "f06_APCP_gt12.7.grib2").write_bytes(b"x")  # non-empty so cached_cycles sees it


def test_no_warmed_cycle_degrades_gracefully(tmp_path: Path) -> None:
    """Empty cache -> source marked unavailable, briefing continues (NFR-6)."""
    settings = Settings(data_dir=tmp_path)
    bundle = IngestBundle()
    fetch(_mission(datetime(2026, 6, 20, 10, tzinfo=UTC), lead_h=2, dur_h=2),
          bundle, MagicMock(), now=datetime(2026, 6, 20, 10, tzinfo=UTC), settings=settings)
    assert bundle.sources_ok["href"] is False
    assert not bundle.href_in_range
    assert any("no warmed cycle" in n for n in bundle.notes)


def test_window_beyond_band_marks_sref_covers(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    _seed_cycle_dirs(tmp_path, "20260620_00")
    bundle = IngestBundle()
    now = datetime(2026, 6, 20, 10, tzinfo=UTC)
    fetch(_mission(now, lead_h=60, dur_h=2), bundle, MagicMock(), now=now, settings=settings)
    assert bundle.sources_ok["href"] is True
    assert not bundle.href_in_range
    assert any("SREF covers" in n for n in bundle.notes)


def test_multi_run_backfill_populates_bundle(tmp_path: Path) -> None:
    """A window straddling spin-up reads two runs and records the backfill provenance."""
    settings = Settings(data_dir=tmp_path)
    _seed_cycle_dirs(tmp_path, "20260620_12", "20260620_00")  # 12Z newest, 00Z prior
    # 14-19Z: 12Z run is f02-f07; f02-f05 (spin-up) backfill from 00Z (f14-f17), f06-f07 from 12Z.
    now = datetime(2026, 6, 20, 14, tzinfo=UTC)
    bundle = IngestBundle()
    bundle.sref_p_precip, bundle.sref_p_tstm = 40.0, 30.0

    field = MagicMock()
    agg = MagicMock(max_value=45.0)
    with (
        patch("upstreamwx.ingest.href_provider.load_probability_field_cached", return_value=field),
        patch("upstreamwx.ingest.href_provider.aggregate_over_polygon", return_value=agg),
    ):
        fetch(_mission(now, lead_h=0, dur_h=5), bundle, MagicMock(), now=now, settings=settings)

    assert bundle.sources_ok["href"] is True
    assert bundle.href_in_range
    assert bundle.href_p_precip == 45.0
    assert bundle.href_p_lightning == 45.0
    # Two runs contributed; the fresh 12Z run is primary, the prior 00Z run backfills spin-up.
    assert bundle.href_runs is not None and len(bundle.href_runs) == 2
    assert bundle.href_cycle == "20260620/12Z"  # freshest run is primary
    assert bundle.href_runs[0][0] == "20260620/12Z"
    assert "spin-up backfill" in (bundle.href_fhour or "")
    assert "00Z" in (bundle.href_fhour or "")
    assert any("backfilled from the prior" in n for n in bundle.notes)
    # member support folds in the HREF exceedance fraction (§16.5).
    assert bundle.member_support["flash_flood"] == pytest.approx(0.45)
    assert bundle.member_support["lightning"] == pytest.approx(0.45)


def test_single_run_provenance_has_no_backfill_label(tmp_path: Path) -> None:
    """A window fully past spin-up reads one run; the label stays the simple fXX-fYY form."""
    settings = Settings(data_dir=tmp_path)
    _seed_cycle_dirs(tmp_path, "20260620_00")
    now = datetime(2026, 6, 20, 10, tzinfo=UTC)  # 12-14Z = f12-f14 of the 00Z run
    bundle = IngestBundle()
    field, agg = MagicMock(), MagicMock(max_value=20.0)
    with (
        patch("upstreamwx.ingest.href_provider.load_probability_field_cached", return_value=field),
        patch("upstreamwx.ingest.href_provider.aggregate_over_polygon", return_value=agg),
    ):
        fetch(_mission(now, lead_h=2, dur_h=2), bundle, MagicMock(), now=now, settings=settings)

    assert bundle.href_runs is not None and len(bundle.href_runs) == 1
    assert "backfill" not in (bundle.href_fhour or "")
    assert bundle.href_cycle == "20260620/00Z"


# --- scheduler warm wiring ---------------------------------------------------

def test_warm_and_prune_warms_href(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BriefingService.warm_and_prune warms the live HREF run and prunes to keep_cycles."""
    from upstreamwx.api.service import BriefingService
    from upstreamwx.href.sources import HrefCycle

    monkeypatch.setenv("UPSTREAMWX_DATA_DIR", str(tmp_path))
    hcycle = HrefCycle(date="20260620", hour=12)
    with (
        patch("upstreamwx.api.service.latest_available_cycle", return_value=None),  # SREF idle
        patch("upstreamwx.api.service.href.latest_available_cycle", return_value=hcycle),
        patch(
            "upstreamwx.api.service.href.warm_cycle", return_value=[Path("a"), Path("b")]
        ) as warm,
        patch("upstreamwx.api.service.href.prune_old_cycles") as prune,
    ):
        n = BriefingService().warm_and_prune()

    assert n == 2
    warm.assert_called_once()
    prune.assert_called_once()
    assert prune.call_args.kwargs["keep"] == 3  # href_cache_keep_cycles default


@pytest.mark.network
def test_live_provider_populates_bundle(tmp_path: Path) -> None:
    from upstreamwx.href import latest_available_cycle, warm_cycle

    settings = Settings(data_dir=tmp_path)
    cycle = latest_available_cycle()
    assert cycle is not None, "no live HREF cycle on NOMADS"
    warm_cycle(cycle, settings=settings, fmin=11, fmax=13)  # warm a small band into the cache

    import geopandas as gpd
    from shapely.ops import unary_union

    poly = unary_union(gpd.read_file("tests/fixtures/buckskin_huc12.geojson").geometry.values)
    # now placed so the window lands at ~f11-f13 of this cycle.
    now = cycle.init_time + timedelta(hours=11)
    mission = _mission(now, lead_h=0, dur_h=2)
    bundle = IngestBundle()
    fetch(mission, bundle, poly, now=now, settings=settings)
    assert bundle.sources_ok["href"] is True
    assert bundle.href_in_range
    assert isinstance(bundle.href_fhour, str) and bundle.href_fhour
