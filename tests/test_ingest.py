"""Ingest tests: hermetic mapping + graceful degradation, and live smoke tests."""

from __future__ import annotations

from datetime import datetime

import pytest
from shapely.geometry import box

from upstreamwx.engine.models import ActivityType, Mission
from upstreamwx.ingest import gather, to_hazard_inputs
from upstreamwx.ingest.base import IngestBundle


def _mission(activity=ActivityType.CANYON) -> Mission:
    return Mission(
        activity_type=activity,
        lat=37.0192,
        lon=-111.9889,
        window_start=datetime(2026, 6, 20, 8),
        window_end=datetime(2026, 6, 20, 18),
        name="Buckskin",
    )


def test_to_hazard_inputs_maps_fields():
    bundle = IngestBundle(
        flash_flood_warning=True,
        sref_p_precip=55.0,
        sref_p_tstm=30.0,
        heat_index_f=98.0,
        apparent_temp_f=44.0,
        spc_category="slight",
        afd_convective_mention=True,
        member_support={"flash_flood": 0.55},
    )
    inputs = to_hazard_inputs(bundle, dry_party=True)
    assert inputs.flash_flood_warning is True
    assert inputs.sref_p_precip == 55.0
    assert inputs.heat_index_f == 98.0
    assert inputs.spc_category == "slight"
    assert inputs.dry_party is True
    assert inputs.member_support == {"flash_flood": 0.55}


def test_gather_degrades_gracefully(monkeypatch):
    # All sources raise; gather must not raise and must flag the failures (NFR-6).
    from upstreamwx.ingest import nws, openmeteo, spc, sref_provider

    def boom(*a, **k):
        raise RuntimeError("down")

    for mod in (nws, openmeteo, spc, sref_provider):
        monkeypatch.setattr(mod, "fetch", boom)

    bundle = gather(_mission(), polygon=box(-112.0, 37.0, -111.9, 37.1))
    assert bundle.sources_ok.get("nws") is False
    assert bundle.sources_ok.get("sref") is False
    # NWS is mandatory: its failure is surfaced as a warning.
    assert any("mandatory source" in n for n in bundle.notes)
    # Engine still gets usable (empty) inputs rather than crashing.
    inputs = to_hazard_inputs(bundle)
    assert inputs.sref_p_precip is None


# --- Live smoke tests (services reachable in dev env; opt-in) -------------------


@pytest.mark.network
def test_nws_live():
    from upstreamwx.ingest import nws

    bundle = IngestBundle()
    nws.fetch(_mission(), bundle)
    assert bundle.sources_ok["nws"] is True
    assert isinstance(bundle.flash_flood_warning, bool)


@pytest.mark.network
def test_openmeteo_live():
    from upstreamwx.ingest import openmeteo

    bundle = IngestBundle()
    openmeteo.fetch(_mission(), bundle)
    assert bundle.sources_ok["open_meteo"] is True
    assert bundle.heat_index_f is not None


@pytest.mark.network
def test_spc_live():
    from upstreamwx.ingest import spc

    bundle = IngestBundle()
    spc.fetch(_mission(), bundle)
    assert bundle.sources_ok["spc"] is True
    # Category may legitimately be None (point outside any outlook polygon).


@pytest.mark.network
def test_sref_provider_live(fixtures_dir):
    import json

    from shapely.geometry import shape

    from upstreamwx.ingest import sref_provider

    geojson = json.loads((fixtures_dir / "buckskin_huc12.geojson").read_text())
    polygon = shape(geojson["features"][0]["geometry"])
    bundle = IngestBundle()
    sref_provider.fetch(_mission(), bundle, polygon)
    if bundle.sources_ok.get("sref"):
        assert 0.0 <= bundle.sref_p_precip <= 100.0


@pytest.mark.network
def test_watershed_cache_live(tmp_path):
    from upstreamwx.config import Settings
    from upstreamwx.watershed import resolve_and_trace_cached

    settings = Settings(data_dir=tmp_path)
    m = _mission()
    first = resolve_and_trace_cached(m.lat, m.lon, settings=settings)
    assert first.origin_huc12 == "140700070505"
    # Second call is served from the on-disk cache.
    cache_file = next((tmp_path / "watershed").glob("*.geojson"))
    assert cache_file.is_file()
    second = resolve_and_trace_cached(m.lat, m.lon, settings=settings)
    assert second.upstream_huc_ids == first.upstream_huc_ids
