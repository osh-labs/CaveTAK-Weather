"""Offline tests for polygon aggregation of HREF fields (Spike C).

Shares the grid-agnostic zonal reducer with SREF; the point here is that the
finer ~3 km grid covers a headwater HUC-12 with many real cells (no nearest-cell
fallback), unlike the coarse SREF grid.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
from shapely.geometry import box
from shapely.ops import unary_union

from upstreamwx.href.aggregate import aggregate_over_polygon
from upstreamwx.href.extract import open_subset


def _sample_field(fixtures_dir: Path):
    ds = open_subset(fixtures_dir / "href_sample_subset.grib2")
    return ds[list(ds.data_vars)[0]]


def test_aggregate_over_buckskin_resolves_many_cells(fixtures_dir: Path) -> None:
    field = _sample_field(fixtures_dir)
    poly = unary_union(
        gpd.read_file(fixtures_dir / "buckskin_huc12.geojson").geometry.values
    )
    agg = aggregate_over_polygon(field, poly, field_name="P(APCP>12.7/1h)", threshold=">12.7")
    # Buckskin (~1263 km²) holds many ~3 km HREF cells — far more than the ~5 SREF
    # cells at 16 km — so the coarse-grid nearest-cell fallback is not needed.
    assert agg.n_cells >= 20
    assert not agg.fallback_nearest_cell
    assert 0.0 <= agg.mean_value <= agg.max_value <= 100.0


def test_tiny_polygon_triggers_nearest_cell_fallback(fixtures_dir: Path) -> None:
    field = _sample_field(fixtures_dir)
    # A ~300 m box, smaller than even a 3 km grid cell, forces the fallback.
    tiny = box(-111.9895, 37.0190, -111.9885, 37.0200)
    agg = aggregate_over_polygon(field, tiny, field_name="tiny", threshold=">12.7")
    assert agg.fallback_nearest_cell
    assert 0.0 <= agg.max_value <= 100.0
