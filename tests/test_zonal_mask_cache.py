"""Offline tests for the memoised polygon mask in zonal aggregation (latency follow-up).

regionmask rasterisation dominates :func:`aggregate_over_polygon`, and a briefing aggregates
the same polygon over many fields on one grid (HREF: ~N hours × ~4 fields). The mask depends
only on (grid, polygon), so it is memoised; these assert the memo reuses it (same result, no
re-rasterise) and re-rasterises when the polygon changes — using the committed SREF fixture.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
from shapely.geometry import box
from shapely.ops import unary_union

import upstreamwx.grib.zonal as zonal
from upstreamwx.grib.zonal import aggregate_over_polygon
from upstreamwx.sref.extract import open_subset


def _sample_field(fixtures_dir: Path):
    ds = open_subset(fixtures_dir / "sref_sample_subset.grib2")
    return ds[list(ds.data_vars)[0]]


def _spy_on_rasterize(monkeypatch):
    calls = {"n": 0}
    real = zonal.region_mask

    def counting(da, geom):
        calls["n"] += 1
        return real(da, geom)

    monkeypatch.setattr(zonal, "region_mask", counting)
    zonal._mask_cache.clear()
    return calls


def test_repeated_aggregation_rasterizes_mask_once(fixtures_dir: Path, monkeypatch) -> None:
    """Same (grid, polygon) aggregated repeatedly rasterises once; results are identical."""
    field = _sample_field(fixtures_dir)
    poly = unary_union(gpd.read_file(fixtures_dir / "buckskin_huc12.geojson").geometry.values)
    calls = _spy_on_rasterize(monkeypatch)

    first = aggregate_over_polygon(field, poly, threshold=">12.7")
    second = aggregate_over_polygon(field, poly, threshold=">12.7")

    assert calls["n"] == 1  # mask rasterised once, reused on the second call
    assert (first.n_cells, first.max_value, first.mean_value) == (
        second.n_cells, second.max_value, second.mean_value
    )


def test_different_polygon_rebuilds_mask(fixtures_dir: Path, monkeypatch) -> None:
    """A different polygon on the same grid is a cache miss and rasterises again."""
    field = _sample_field(fixtures_dir)
    poly = unary_union(gpd.read_file(fixtures_dir / "buckskin_huc12.geojson").geometry.values)
    calls = _spy_on_rasterize(monkeypatch)

    aggregate_over_polygon(field, poly, threshold=">12.7")
    aggregate_over_polygon(field, box(-112.5, 36.5, -111.5, 37.5), threshold=">12.7")

    assert calls["n"] == 2  # distinct polygons -> distinct masks
