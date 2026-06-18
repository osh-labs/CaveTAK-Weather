"""Tests for the pour-point delineation module (Spike D -> module).

Offline tests monkeypatch the single NLDI HTTP seam (``_execute_process``) with
canned GeoJSON, so the parsing, area, and WBD-fallback logic run with no network.
The ``@pytest.mark.network`` tests exercise the live NLDI pygeoapi processes and
are deselected by default (``addopts = -m 'not network'`` in pyproject).
"""

from __future__ import annotations

import pytest

from upstreamwx.watershed import pourpoint
from upstreamwx.watershed.pourpoint import (
    PourpointBasin,
    delineate,
    delineate_pourpoint,
    raindrop_snap,
)


def _square(lon0: float, lat0: float, d: float = 0.09) -> dict:
    """A small closed square polygon geometry (CCW) around (lon0, lat0)."""
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [lon0, lat0],
                [lon0 + d, lat0],
                [lon0 + d, lat0 + d],
                [lon0, lat0 + d],
                [lon0, lat0],
            ]
        ],
    }


def _flowtrace_fc(snapped_lon: float, snapped_lat: float, *, comid=123, name="Test River") -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "id": "nhdFlowline",
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
                "properties": {"comid": comid, "gnis_name": name},
            },
            {
                "id": "raindropPath",
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [snapped_lon - 0.01, snapped_lat - 0.01],
                        [snapped_lon, snapped_lat],
                    ],
                },
                "properties": {},
            },
        ],
    }


def _splitcatchment_fc(geom: dict | None) -> dict:
    feats = []
    if geom is not None:
        feats.append({"id": "drainageBasin", "type": "Feature", "geometry": geom, "properties": {}})
    feats.append(
        {"id": "catchment", "type": "Feature", "geometry": _square(0, 0, 0.01), "properties": {}}
    )
    return {"type": "FeatureCollection", "features": feats}


def _patch_nldi(monkeypatch, flowtrace_fc: dict, split_fc: dict) -> None:
    """Route the two process calls to canned responses."""

    def fake(process, lat, lon, extra, **kw):  # noqa: ARG001
        if process == "nldi-flowtrace":
            return flowtrace_fc
        if process == "nldi-splitcatchment":
            return split_fc
        raise AssertionError(f"unexpected process {process}")

    monkeypatch.setattr(pourpoint, "_execute_process", fake)


def test_raindrop_snap_parses_snapped_point_and_flowline(monkeypatch):
    ft = _flowtrace_fc(-112.945, 37.2792, comid=10025834, name="North Fork Virgin River")
    _patch_nldi(monkeypatch, ft, _splitcatchment_fc(None))
    snap = raindrop_snap(37.2794, -112.9481)
    assert snap.snapped is True
    assert snap.lon == pytest.approx(-112.945)
    assert snap.lat == pytest.approx(37.2792)
    assert snap.comid == 10025834
    assert snap.flowline_name == "North Fork Virgin River"


def test_raindrop_snap_handles_empty_path(monkeypatch):
    fc = _flowtrace_fc(0, 0)
    # Drop the raindropPath: point already on the network.
    fc["features"] = [f for f in fc["features"] if f["id"] != "raindropPath"]
    _patch_nldi(monkeypatch, fc, _splitcatchment_fc(None))
    snap = raindrop_snap(35.0, -82.0)
    assert snap.snapped is False
    assert (snap.lat, snap.lon) == (35.0, -82.0)


def test_delineate_pourpoint_success(monkeypatch):
    _patch_nldi(
        monkeypatch,
        _flowtrace_fc(-81.927, 35.95, comid=9751596, name="Linville River"),
        _splitcatchment_fc(_square(-82.0, 35.9)),
    )
    basin = delineate_pourpoint(35.9499, -81.9271)
    assert isinstance(basin, PourpointBasin)
    assert basin.method == "nldi-raindrop-split"
    assert basin.comid == 9751596
    assert basin.flowline_name == "Linville River"
    assert basin.area_km2 > 0
    assert basin.polygon.is_valid and not basin.polygon.is_empty
    assert basin.snapped_lat == pytest.approx(35.95)


def test_delineate_pourpoint_none_when_no_basin(monkeypatch):
    _patch_nldi(monkeypatch, _flowtrace_fc(-81.927, 35.95), _splitcatchment_fc(None))
    assert delineate_pourpoint(35.9499, -81.9271) is None


def test_delineate_pourpoint_none_on_network_error(monkeypatch):
    def boom(*a, **k):  # noqa: ARG001
        raise RuntimeError("NLDI down")

    monkeypatch.setattr(pourpoint, "_execute_process", boom)
    assert delineate_pourpoint(35.9499, -81.9271) is None


def test_delineate_falls_back_to_wbd(monkeypatch):
    # NLDI path fails ...
    monkeypatch.setattr(pourpoint, "delineate_pourpoint", lambda lat, lon: None)

    # ... so the WBD trace is used. Stub its two service calls.
    from shapely.geometry import shape

    from upstreamwx.watershed import huc, upstream
    from upstreamwx.watershed.upstream import UpstreamTrace

    fake_trace = UpstreamTrace(
        origin_huc12="030501010302",
        upstream_huc_ids=["030501010302", "030501010301"],
        polygon=shape(_square(-82.0, 35.9)),
        area_km2=175.6,
        method="tohuc-graph",
        huc_level=12,
        notes=["widened HU8->HU6"],
    )
    monkeypatch.setattr(huc, "resolve_huc12", lambda lat, lon: object())
    monkeypatch.setattr(upstream, "trace_upstream", lambda origin: fake_trace)

    basin = delineate(35.9499, -81.9271)
    assert basin.method == "wbd-huc12-fallback"
    assert basin.area_km2 == pytest.approx(175.6)
    assert basin.comid is None
    assert any("origin HUC-12 030501010302" in n for n in basin.notes)
    assert any("widened HU8->HU6" in n for n in basin.notes)


def test_delineate_no_fallback_raises(monkeypatch):
    monkeypatch.setattr(pourpoint, "delineate_pourpoint", lambda lat, lon: None)
    with pytest.raises(ValueError, match="fallback disabled"):
        delineate(35.9499, -81.9271, allow_fallback=False)


def test_delineate_cached_round_trip(monkeypatch, tmp_path):
    """First call delineates and writes; second reads from disk without re-calling."""
    from shapely.geometry import shape

    from upstreamwx.config import Settings
    from upstreamwx.watershed import cache

    calls = {"n": 0}

    def fake_delineate(lat, lon, **kw):  # noqa: ARG001
        calls["n"] += 1
        return PourpointBasin(
            lat=lat,
            lon=lon,
            snapped_lat=lat,
            snapped_lon=lon,
            polygon=shape(_square(-82.0, 35.9)),
            area_km2=114.3,
            method="nldi-raindrop-split",
            comid=9751596,
            flowline_name="Linville River",
            notes=["n1"],
        )

    monkeypatch.setattr(cache, "delineate", fake_delineate)
    settings = Settings(data_dir=tmp_path)

    first = cache.delineate_cached(35.9499, -81.9271, settings=settings)
    second = cache.delineate_cached(35.9499, -81.9271, settings=settings)

    assert calls["n"] == 1  # second call served from disk
    assert second.method == first.method == "nldi-raindrop-split"
    assert second.comid == 9751596
    assert second.flowline_name == "Linville River"
    assert second.area_km2 == pytest.approx(114.3)
    assert second.polygon.is_valid and not second.polygon.is_empty
    assert second.notes == ["n1"]


@pytest.mark.network
@pytest.mark.parametrize(
    ("name", "lat", "lon", "lo", "hi", "expect_name"),
    [
        ("Zion Narrows (raw, unsnappable on str900)", 37.2794, -112.9481, 650, 850, "Virgin River"),
        ("Linville Gorge", 35.9499, -81.9271, 95, 135, "Linville"),
    ],
)
def test_delineate_live(name, lat, lon, lo, hi, expect_name):
    basin = delineate(lat, lon)
    assert basin.method == "nldi-raindrop-split", name
    assert lo < basin.area_km2 < hi, f"{name}: area {basin.area_km2} km^2 out of range"
    assert basin.comid is not None
    assert basin.polygon.is_valid and not basin.polygon.is_empty
    assert expect_name in (basin.flowline_name or "")
