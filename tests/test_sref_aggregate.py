"""Offline tests for polygon aggregation of SREF fields (Spike A)."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
from shapely.geometry import box
from shapely.ops import unary_union

from upstreamwx.sref.aggregate import aggregate_over_polygon
from upstreamwx.sref.extract import open_subset


def _sample_field(fixtures_dir: Path):
    ds = open_subset(fixtures_dir / "sref_sample_subset.grib2")
    return ds[list(ds.data_vars)[0]]


def test_aggregate_over_buckskin(fixtures_dir: Path) -> None:
    field = _sample_field(fixtures_dir)
    poly = unary_union(
        gpd.read_file(fixtures_dir / "buckskin_huc12.geojson").geometry.values
    )
    agg = aggregate_over_polygon(field, poly, field_name="P(APCP>12.7)", threshold=">12.7")
    # Buckskin (~1263 km²) covers several ~16 km SREF cells.
    assert agg.n_cells >= 1
    assert not agg.fallback_nearest_cell
    # Probabilities stay within [0, 100]; mean never exceeds max.
    assert 0.0 <= agg.mean_value <= agg.max_value <= 100.0
    # Multi-step fixture yields per-step breakdown.
    assert agg.per_step


def test_tiny_polygon_triggers_nearest_cell_fallback(fixtures_dir: Path) -> None:
    field = _sample_field(fixtures_dir)
    # A ~1 km box (smaller than a 16 km grid cell) over the Buckskin area.
    tiny = box(-111.99, 37.01, -111.98, 37.02)
    agg = aggregate_over_polygon(field, tiny, field_name="tiny", threshold=">12.7")
    assert agg.fallback_nearest_cell
    assert 0.0 <= agg.max_value <= 100.0
