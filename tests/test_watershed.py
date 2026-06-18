"""Tests for the Spike B watershed module.

Offline tests load the committed Buckskin Gulch fixture and require no network.
The single ``@pytest.mark.network`` test exercises the live USGS WBD path and is
deselected by default (``addopts = -m 'not network'`` in pyproject).
"""

from __future__ import annotations

import geopandas as gpd
import pytest
from shapely.geometry.base import BaseGeometry


def test_fixture_is_valid_nonempty_polygon(fixtures_dir):
    gdf = gpd.read_file(fixtures_dir / "buckskin_huc12.geojson")
    assert len(gdf) == 1
    assert str(gdf.crs).upper().endswith("4326")

    geom: BaseGeometry = gdf.geometry.iloc[0]
    assert geom.is_valid
    assert not geom.is_empty
    assert geom.geom_type in ("Polygon", "MultiPolygon")


def test_fixture_has_plausible_area(fixtures_dir):
    gdf = gpd.read_file(fixtures_dir / "buckskin_huc12.geojson")
    # Equal-area (CONUS Albers) area must be positive and sizable for Buckskin.
    area_km2 = gdf.to_crs(5070).geometry.iloc[0].area / 1e6
    assert area_km2 > 0
    assert 500 < area_km2 < 5000  # Buckskin upstream domain ~1263 km^2


def test_fixture_metadata(fixtures_dir):
    gdf = gpd.read_file(fixtures_dir / "buckskin_huc12.geojson")
    row = gdf.iloc[0]
    assert row["origin_huc12"] == "140700070505"
    assert int(row["huc_level"]) == 12
    assert int(row["n_upstream"]) >= 2
    assert row["method"] == "tohuc-graph"


@pytest.mark.network
def test_resolve_and_trace_live():
    from upstreamwx.watershed import resolve_huc12, trace_upstream

    lat, lon = 37.0192, -111.9889  # Buckskin Gulch
    huc = resolve_huc12(lat, lon)
    assert huc.huc_level in (10, 12)
    assert huc.huc_id.startswith("1407")

    trace = trace_upstream(huc)
    assert trace.origin_huc12 == huc.huc_id
    assert len(trace.upstream_huc_ids) >= 2
    assert trace.area_km2 > 0
    assert trace.polygon.is_valid and not trace.polygon.is_empty
    assert trace.method in ("tohuc-graph", "nldi-ut")
