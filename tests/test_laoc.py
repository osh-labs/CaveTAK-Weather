"""Tests for the Lightning Area of Concern (LAoC) — PRD §16.1, §13 principle 4.

Lightning is a point/corridor estimate, not a basin-routed one: its ensemble signal must
aggregate over a disk around the activity, not the upstream watershed. These tests pin the
orchestrator wiring (the lightning fields aggregate over the LAoC disk while flash flood
stays on the watershed), the disk fallback when no radius is set, the structured ring
emitted to the PWA, and the cache key folding both radii in.
"""

from __future__ import annotations

from datetime import UTC, datetime

from shapely.geometry import box

from upstreamwx.api.cache import mission_cache_key
from upstreamwx.engine.models import ActivityType, Mission
from upstreamwx.ingest import gefs_provider, orchestrator, refs_provider
from upstreamwx.ingest.base import IngestBundle
from upstreamwx.sitrep.structured import _laoc
from upstreamwx.watershed import roc_disk
from upstreamwx.watershed.pourpoint import PourpointBasin

_LAT, _LON = 37.0192, -111.9889
_W_START = datetime(2026, 6, 20, 8, 0, tzinfo=UTC)
_W_END = datetime(2026, 6, 20, 18, 0, tzinfo=UTC)


def _mission(**kw) -> Mission:
    base = dict(
        activity_type=ActivityType.CANYON,
        lat=_LAT,
        lon=_LON,
        window_start=_W_START,
        window_end=_W_END,
    )
    base.update(kw)
    return Mission(**base)


def _stub_delineation(monkeypatch):
    """Replace live delineation with a fixed box basin; return that polygon."""
    poly = box(_LON - 1.0, _LAT - 1.0, _LON + 1.0, _LAT + 1.0)
    basin = PourpointBasin(
        lat=_LAT, lon=_LON, snapped_lat=_LAT, snapped_lon=_LON,
        polygon=poly, area_km2=poly.area, method="test",
    )
    monkeypatch.setattr(orchestrator, "delineate_cached", lambda lat, lon: basin)
    return poly


def _capture_providers(monkeypatch) -> dict:
    """Stub SREF/HREF fetch to record the (precip, lightning) polygons they were handed."""
    captured: dict = {}

    def fake_gefs(mission, bundle, polygon, *, lightning_polygon=None, cycle=None):
        captured["gefs_precip"] = polygon
        captured["gefs_ltng"] = lightning_polygon
        bundle.sources_ok["gefs"] = True

    def fake_refs(mission, bundle, polygon, *, lightning_polygon=None, now=None, settings=None):
        captured["refs_precip"] = polygon
        captured["refs_ltng"] = lightning_polygon
        bundle.sources_ok["refs"] = True

    monkeypatch.setattr(gefs_provider, "fetch", fake_gefs)
    monkeypatch.setattr(refs_provider, "fetch", fake_refs)
    return captured


def test_lightning_aggregates_over_laoc_disk(monkeypatch) -> None:
    """With a lightning radius set, the lightning fields use the disk; precip uses the basin."""
    poly = _stub_delineation(monkeypatch)
    captured = _capture_providers(monkeypatch)

    bundle = orchestrator._run_watershed_and_ensembles(
        _mission(lightning_radius_km=20.0), None, None
    )

    expected_disk = roc_disk(_LAT, _LON, 20.0)
    # Flash-flood precip aggregates over the full watershed; lightning over the LAoC disk.
    assert captured["gefs_precip"].equals(poly)
    assert captured["refs_precip"].equals(poly)
    assert captured["gefs_ltng"].equals(expected_disk)
    assert captured["refs_ltng"].equals(expected_disk)
    assert not captured["gefs_ltng"].equals(poly)
    # The disk is surfaced on the bundle for the PWA ring.
    assert bundle.laoc_radius_km == 20.0
    assert bundle.laoc_disk is not None and bundle.laoc_disk.equals(expected_disk)
    assert bundle.laoc_area_km2 > 0


def test_no_lightning_radius_falls_back_to_watershed(monkeypatch) -> None:
    """Without a radius, lightning aggregates over the same domain as flash flood (back-compat)."""
    poly = _stub_delineation(monkeypatch)
    captured = _capture_providers(monkeypatch)

    bundle = orchestrator._run_watershed_and_ensembles(_mission(), None, None)

    assert captured["gefs_ltng"].equals(poly)
    assert captured["refs_ltng"].equals(poly)
    assert bundle.laoc_disk is None
    assert bundle.laoc_radius_km is None


def test_laoc_structured_ring() -> None:
    """``_laoc`` emits a GeoJSON ring only when the bundle carries a disk (PRD §16.1)."""
    mission = _mission(lightning_radius_km=24.14)
    assert _laoc(None, mission) is None
    bundle = IngestBundle()
    assert _laoc(bundle, mission) is None  # radius/disk unset -> no ring

    bundle.laoc_radius_km = 24.14
    bundle.laoc_disk = roc_disk(_LAT, _LON, 24.14)
    out = _laoc(bundle, mission)
    assert out is not None
    assert out["radius_km"] == 24.14
    assert out["radius_mi"] == round(24.14 / 1.609344, 1)
    assert out["center"] == [mission.lon, mission.lat]  # GeoJSON (lon, lat)
    assert out["geometry"]["type"] in {"Polygon", "MultiPolygon"}


def test_cache_key_varies_by_roc_and_laoc() -> None:
    """Two requests differing only by a radius must not collide on one cache entry."""
    base = mission_cache_key(_mission())
    roc = mission_cache_key(_mission(radius_km=30.0))
    laoc = mission_cache_key(_mission(lightning_radius_km=24.0))
    both = mission_cache_key(_mission(radius_km=30.0, lightning_radius_km=24.0))
    assert len({base, roc, laoc, both}) == 4
