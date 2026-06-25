"""Golden-file tests for the deterministic SITREP renderer (roadmap M0.2 exit criterion).

The structured render must be byte-identical for identical inputs and must always carry
the disclaimer and source links. Two representative scenarios are covered: a canyon with
the HREF same-day supplement in range, and a benign cave (no bundle, no HREF).

Regenerate goldens after an intentional format change with::

    .venv/bin/python -m tests.gen_sitrep_goldens
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from shapely.geometry import Point

from upstreamwx.engine.assess import assess
from upstreamwx.engine.models import ActivityType, HazardInputs, Mission
from upstreamwx.ingest.base import IngestBundle
from upstreamwx.sitrep import DISCLAIMER, render_md
from upstreamwx.watershed import PourpointBasin, UpstreamTrace

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "sitrep"


def canyon_href_case() -> tuple[dict, str]:
    """Canyon, slot, HREF in range, with an upstream trace — exercises every section."""
    mission = Mission(
        activity_type=ActivityType.CANYON,
        lat=37.0192,
        lon=-111.9889,
        window_start=datetime(2026, 6, 20, 8),
        window_end=datetime(2026, 6, 20, 18),
        name="Buckskin Gulch",
        is_slot=True,
    )
    inputs = HazardInputs(
        sref_p_precip=65, measurable_precip=True, sref_p_tstm=50,
        href_p_precip=45, href_p_lightning=35,
        heat_index_f=95, apparent_temp_f=92, wind_mph=8, cape_jkg=1200,
        spc_category="slight", afd_convective_mention=True, source_agreement="partial",
    )
    bundle = IngestBundle(
        sref_p_precip=65, sref_p_tstm=50, measurable_precip=True, cape_jkg=1200,
        href_p_precip=45, href_p_lightning=35, href_in_range=True,
        href_cycle="20260620/12Z", href_fhour="f09-f16", source_agreement="partial",
        heat_index_f=95, apparent_temp_f=92, wind_mph=8, spc_category="slight",
        afd_convective_mention=True, notes=["openmeteo: ok"],
    )
    upstream = UpstreamTrace(
        origin_huc12="150100021304",
        upstream_huc_ids=["150100021301", "150100021302", "150100021303"],
        polygon=Point(-111.9889, 37.0192).buffer(0.1),
        area_km2=842.5,
        method="tohuc-graph",
    )
    result = assess(mission, inputs)
    return {"result": result, "upstream": upstream, "bundle": bundle}, "canyon_href.md"


def cave_minimal_case() -> tuple[dict, str]:
    """Benign cave, no bundle, no HREF — exercises the cave-isolation + karst notes."""
    mission = Mission(
        activity_type=ActivityType.CAVE,
        lat=37.0,
        lon=-112.0,
        window_start=datetime(2026, 6, 20, 8),
        window_end=datetime(2026, 6, 20, 18),
        name="Dry Cave",
    )
    inputs = HazardInputs(sref_p_precip=5, sref_p_tstm=5, heat_index_f=70, apparent_temp_f=70)
    result = assess(mission, inputs)
    return {"result": result}, "cave_minimal.md"


CASES = [canyon_href_case, cave_minimal_case]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.__name__)
def test_render_matches_golden(case):
    kwargs, filename = case()
    rendered = render_md(**kwargs)
    expected = (GOLDEN_DIR / filename).read_text()
    assert rendered == expected, f"render drifted from golden {filename}; regenerate if intended"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.__name__)
def test_render_is_deterministic(case):
    kwargs, _ = case()
    assert render_md(**kwargs) == render_md(**kwargs)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.__name__)
def test_render_carries_disclaimer_and_sources(case):
    kwargs, _ = case()
    rendered = render_md(**kwargs)
    assert DISCLAIMER in rendered
    assert "## SOURCES (verify)" in rendered
    assert "https://api.weather.gov/alerts/active" in rendered
    assert "## DISCLAIMER" in rendered


def _pourpoint_render(basin: PourpointBasin) -> str:
    mission = Mission(
        activity_type=ActivityType.CANYON,
        lat=37.2794,
        lon=-112.9481,
        window_start=datetime(2026, 6, 20, 8),
        window_end=datetime(2026, 6, 20, 18),
        name="Zion Narrows",
    )
    result = assess(mission, HazardInputs(sref_p_precip=20, measurable_precip=True))
    return render_md(result, upstream=basin)


def test_render_pourpoint_basin_header_and_summary():
    """A PourpointBasin renders the flowline/area header and a pour-point summary."""
    basin = PourpointBasin(
        lat=37.2794, lon=-112.9481, snapped_lat=37.2792, snapped_lon=-112.945,
        polygon=Point(-112.945, 37.2792).buffer(0.1), area_km2=747.2,
        method="nldi-raindrop-split", comid=10025834,
        flowline_name="North Fork Virgin River",
    )
    rendered = _pourpoint_render(basin)
    assert "North Fork Virgin River (~747 km², nldi-raindrop-split)" in rendered
    summary = "watershed to North Fork Virgin River: ~747 km², delineated via nldi-raindrop-split"
    assert summary in rendered


def test_render_pourpoint_fallback_uses_method_without_flowline():
    """The WBD fallback basin (no flowline/comid) still renders cleanly."""
    basin = PourpointBasin(
        lat=35.0, lon=-82.0, snapped_lat=35.0, snapped_lon=-82.0,
        polygon=Point(-82.0, 35.0).buffer(0.1), area_km2=175.6,
        method="wbd-huc12-fallback",
    )
    rendered = _pourpoint_render(basin)
    assert "pour point (~176 km², wbd-huc12-fallback)" in rendered
    assert "watershed to the pour point: ~176 km², delineated via wbd-huc12-fallback" in rendered
