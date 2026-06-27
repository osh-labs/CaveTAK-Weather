"""Tests for the M0.4 structured serializer (:mod:`upstreamwx.sitrep.structured`).

The serializer maps the engine result + ingest bundle onto the PWA's JSON contract. The
oracle is the committed ``frontend/data/sample-briefing.json``: the serialized output must
carry exactly that top-level shape, and the derived fields (severity classes, the
hazard × phase timeline grid, watershed GeoJSON, forecast table/series) must match the
engine's postures verbatim — the serializer never decides anything (FR-13, FR-20).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from shapely.geometry import Polygon

from upstreamwx.engine.assess import assess
from upstreamwx.engine.models import ActivityType, Hazard, HazardInputs, Mission
from upstreamwx.ingest.base import ForecastHourly, IngestBundle
from upstreamwx.sitrep.generate import GeneratedBriefing
from upstreamwx.sitrep.structured import to_structured
from upstreamwx.watershed import PourpointBasin

_SAMPLE = Path(__file__).resolve().parents[1] / "frontend" / "data" / "sample-briefing.json"
_MT = timezone(timedelta(hours=-6))


def _mission() -> Mission:
    return Mission(
        activity_type=ActivityType.CANYON,
        lat=37.0192,
        lon=-111.9889,
        window_start=datetime(2026, 6, 20, 8, 0, tzinfo=_MT),
        window_end=datetime(2026, 6, 20, 18, 0, tzinfo=_MT),
        name="Buckskin Gulch",
    )


def _inputs() -> HazardInputs:
    # Enough signal to light up flash flood + lightning + heat.
    return HazardInputs(
        sref_p_precip=65.0,
        sref_p_tstm=92.0,
        measurable_precip=True,
        spc_category="marginal",
        afd_convective_mention=True,
        afd_flood_mention=True,
        heat_index_f=87.0,
        apparent_temp_f=72.0,
        member_support={"flash_flood": 0.65, "lightning": 0.92},
    )


def _bundle() -> IngestBundle:
    poly = Polygon([(-111.99, 37.01), (-111.98, 37.01), (-111.98, 37.02), (-111.99, 37.02)])
    basin = PourpointBasin(
        lat=37.0192,
        lon=-111.9889,
        snapped_lat=37.0190,
        snapped_lon=-111.9885,
        polygon=poly,
        area_km2=12.95,  # ~5.0 sq mi
        method="nldi-raindrop-split",
        flowline_name="Buckskin Gulch",
    )
    fh = ForecastHourly(
        hours=["0800", "0900", "1000"],
        temp_f=[70.0, 80.0, 85.0],
        feels_f=[71.0, 82.0, 87.0],
        wind_mph=[5.0, 12.0, 20.0],
        gust_mph=[10.0, 22.0, 38.0],
        precip_pct=[10.0, 40.0, 71.0],
        qpf_in=[0.0, 0.2, 0.6],
        sky=["☀️", "⛅", "🌧️"],
    )
    return IngestBundle(
        sref_p_precip=65.0,
        sref_p_tstm=92.0,
        upstream=basin,
        forecast_hourly=fh,
        sources_ok={"nws": True, "open_meteo": True, "sref": True, "watershed": True},
    )


def _generated(*, with_bundle: bool) -> GeneratedBriefing:
    mission, inputs = _mission(), _inputs()
    result = assess(mission, inputs)
    return GeneratedBriefing(
        markdown="# stub\n",
        result=result,
        generated_at=datetime(2026, 6, 20, 12, 0, tzinfo=UTC),
        framed=False,
        bundle=_bundle() if with_bundle else None,
    )


def _sample_keys() -> set[str]:
    return set(json.loads(_SAMPLE.read_text())) - {"_comment"}


def test_top_level_shape_matches_contract() -> None:
    s = to_structured(_generated(with_bundle=True), cached=False, cache_cycle="2026062009z")
    assert set(s) == _sample_keys()


def test_section_keys_match_sample() -> None:
    sample = json.loads(_SAMPLE.read_text())
    s = to_structured(_generated(with_bundle=True), cached=True, cache_cycle="2026062009z")
    assert set(s["mission"]) == set(sample["mission"])
    assert set(s["bluf"][0]) == set(sample["bluf"][0])
    assert set(s["metrics"][0]) == set(sample["metrics"][0])
    assert set(s["phases"][0]) == set(sample["phases"][0])
    assert set(s["hazard_detail"][0]) == set(sample["hazard_detail"][0])
    assert set(s["resources"][0]) == set(sample["resources"][0])
    assert set(s["forecast_hourly"]) == set(sample["forecast_hourly"])
    assert s["cached"] is True


def test_bluf_and_timeline_track_engine_postures() -> None:
    gen = _generated(with_bundle=True)
    s = to_structured(gen, cached=False, cache_cycle="c")
    # Every BLUF entry mirrors the engine's posture verbatim (FR-20).
    for entry in s["bluf"]:
        posture = gen.result.bluf[Hazard(entry["hazard"])]
        assert entry["label"] == posture.severity_label
        assert entry["is_persistent"] == (posture.window_of_concern is None)
    # Timeline is a 4-hazard × 3-phase grid; cells are either applicable:false or carry a
    # severity token usable as a `bar-{token}` class.
    assert len(s["timeline"]) == 4
    for row in s["timeline"]:
        assert len(row["cells"]) == 3
        for cell in row["cells"]:
            if cell.get("applicable") is False:
                continue
            assert cell["severity"]
            assert "confidence" in cell


def test_severity_classes_use_heat_and_tier_vocabulary() -> None:
    s = to_structured(_generated(with_bundle=True), cached=False, cache_cycle="c")
    by_hazard = {h["hazard"]: h for h in s["bluf"]}
    assert by_hazard["heat"]["severity_class"].startswith("heat-")
    assert by_hazard["flash_flood"]["severity_class"].startswith("sev-")


def test_watershed_geojson_and_forecast_populated() -> None:
    s = to_structured(_generated(with_bundle=True), cached=False, cache_cycle="c")
    assert s["watershed"]["geometry"]["type"] == "Polygon"
    assert s["watershed"]["area_sq_mi"] == 5.0
    assert s["forecast_hourly"]["hours"] == ["0800", "0900", "1000"]
    assert s["temp_series"]["air"] == [70.0, 80.0, 85.0]
    # Metric cards derive from the hourly series + SREF aggregate.
    metrics = {m["label"]: m for m in s["metrics"]}
    assert metrics["Temp"]["value"] == "85"
    assert metrics["Wind"]["sub"] == "Gust 38"
    assert metrics["T-storm"]["value"] == "92"


def test_roc_clip_surfaces_clipped_geometry_and_ring() -> None:
    """A Radius of Concern on the bundle -> clipped watershed + excluded + roc ring (FR-3)."""
    from upstreamwx.watershed import clip_watershed

    gen = _generated(with_bundle=True)
    full = gen.bundle.upstream.polygon
    mission = gen.result.mission
    mission.radius_km = 1.0
    clip = clip_watershed(full, mission.lat, mission.lon, 1.0)  # 1 km -> bisects the test basin
    gen.bundle.aggregation_polygon = clip.kept
    gen.bundle.roc_radius_km = 1.0
    gen.bundle.roc_disk = clip.disk
    gen.bundle.roc_excluded = clip.excluded
    gen.bundle.roc_kept_area_km2 = clip.kept_area_km2

    s = to_structured(gen, cached=False, cache_cycle="c")
    # Watershed geometry is the clipped (kept) basin, with the hatchable remainder alongside.
    assert s["watershed"]["excluded_geometry"] is not None
    assert s["watershed"]["area_sq_mi"] == round(clip.kept_area_km2 * 0.386102, 1)
    # The ring carries radius (km + mi) and the disk geometry centered on the mission point.
    assert set(s["roc"]) == {"radius_km", "radius_mi", "center", "geometry"}
    assert s["roc"]["center"] == [mission.lon, mission.lat]
    assert s["roc"]["geometry"]["type"] == "Polygon"
    assert s["mission"]["radius_km"] == 1.0


def test_offline_path_degrades_gracefully() -> None:
    """No bundle (the `inputs` path) -> stable shape with null/empty display fields."""
    s = to_structured(_generated(with_bundle=False), cached=False, cache_cycle="c")
    assert set(s) == _sample_keys()
    assert s["watershed"] is None
    assert s["forecast_hourly"] == {"hours": [], "rows": []}
    assert all(m["value"] == "n/a" for m in s["metrics"])
    assert s["bluf"]  # postures still present from the engine result
