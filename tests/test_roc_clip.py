"""Tests for Radius-of-Concern clipping (:mod:`upstreamwx.watershed.roc`, FR-3).

The clip is pure pre-ingest geometry: it bounds the delineated watershed to a disk of
the user's radius before SREF/HREF aggregation. These tests pin the disk's true-distance
radius, the kept ⊆ disk / kept ∪ excluded ≈ full invariants, the basin-already-inside
short-circuit (no exclusion), and determinism (NFR-4).
"""

from __future__ import annotations

from shapely.geometry import Point, box

from upstreamwx.watershed import clip_watershed, roc_disk
from upstreamwx.watershed.roc import _aeqd_transformers

# A point in southern Utah; a generous box around it as a stand-in watershed.
_LAT, _LON = 37.0192, -111.9889


def _disk_radius_km(disk, lat: float, lon: float) -> float:
    """Max radial distance (km) from the center to the disk boundary, in AEQD meters."""
    fwd, _ = _aeqd_transformers(lat, lon)
    from shapely.ops import transform as shp_transform

    local = shp_transform(fwd, disk)
    cx, cy = 0.0, 0.0
    coords = list(local.exterior.coords)
    return max(((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 for x, y in coords) / 1000.0


def test_roc_disk_radius_is_true_distance() -> None:
    disk = roc_disk(_LAT, _LON, 50.0)
    # AEQD makes the disk a true circle: its max radius is ~50 km (well within 0.5 %).
    assert abs(_disk_radius_km(disk, _LAT, _LON) - 50.0) < 0.25


def test_clip_keeps_inside_and_excludes_outside() -> None:
    full = box(_LON - 1.0, _LAT - 1.0, _LON + 1.0, _LAT + 1.0)  # ~150 km across
    clip = clip_watershed(full, _LAT, _LON, 20.0)
    # Kept is bounded by the disk; excluded is what the disk cut away.
    assert clip.kept.area < full.area
    assert clip.excluded is not None
    assert clip.kept.within(clip.disk.buffer(1e-9))
    # Kept ∪ excluded reconstitutes the full basin (areas, to rounding).
    union_area = clip.kept.union(clip.excluded).area
    assert abs(union_area - full.area) < full.area * 1e-6
    assert clip.kept_area_km2 > 0


def test_basin_inside_radius_has_no_exclusion() -> None:
    # A tiny basin well within a 50 mi (80 km) disk -> nothing is clipped away.
    tiny = Point(_LON, _LAT).buffer(0.01)  # ~1 km
    clip = clip_watershed(tiny, _LAT, _LON, 80.0)
    assert clip.excluded is None
    assert abs(clip.kept.area - tiny.area) < tiny.area * 1e-6


def test_clip_is_deterministic() -> None:
    full = box(_LON - 0.5, _LAT - 0.5, _LON + 0.5, _LAT + 0.5)
    a = clip_watershed(full, _LAT, _LON, 30.0)
    b = clip_watershed(full, _LAT, _LON, 30.0)
    assert a.kept.equals(b.kept)
    assert a.kept_area_km2 == b.kept_area_km2
