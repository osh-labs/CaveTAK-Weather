"""Hermetic tests for upstream-trace completeness (H-1, FR-3).

The WBD trace must never *silently* understate the flash-flood domain. These tests
fake the single WFS seam (``upstream._water_data``) with canned GeoDataFrames and pin:

- mid-region external inflow (a tributary confluencing inside the region from an
  adjacent HU8 — the Paria-into-Colorado topology) triggers widening, not a drop;
- a probe failure fails toward widening and, at the widest level, toward
  ``complete=False`` — never toward "no inflow";
- inflow (or a probe failure) at the widest HU4 fetch flags truncation risk;
- the "widened to HUX" note appears only when the wider fetch actually succeeded;
- a zero-edge tohuc graph over a non-empty region fails loudly instead of returning
  an origin-only polygon posing as the watershed.

No network; any live test carries ``@pytest.mark.network`` (none here).
"""

from __future__ import annotations

import geopandas as gpd
import pytest
from shapely.geometry import box

from upstreamwx.watershed import upstream
from upstreamwx.watershed.huc import HucResult
from upstreamwx.watershed.upstream import (
    UpstreamGraphError,
    UpstreamTrace,
    _trace_tohuc,
    trace_upstream,
)

ORIGIN = "101000010101"  # HU8 10100001 / HU6 101000 / HU4 1010

# The H-1(a) topology: B -> A -> O in-region, and X (adjacent HU8 10100002) also
# drains into A. A has an in-region upstream neighbour, so the old leaf-only probe
# never checked it and X's whole watershed (X, Y) was silently dropped.
MID_INFLOW = [
    (ORIGIN, "101000010900"),
    ("101000010900", None),  # downstream of the origin; not in the upstream set
    ("101000010102", ORIGIN),  # A — mid-set confluence node
    ("101000010103", "101000010102"),  # B — in-region headwater leaf above A
    ("101000020201", "101000010102"),  # X — external tributary into A (adjacent HU8)
    ("101000020202", "101000020201"),  # Y — X's headwater
]

# A minimal in-region world with no external inflow anywhere.
SIMPLE = [
    (ORIGIN, "101000010900"),
    ("101000010900", None),
    ("101000010102", ORIGIN),
]

# Z drains into the origin from a different HU4 (2010...) — invisible to every
# widening level, so it must surface as complete=False at the HU4 ceiling.
CROSS_HU4 = [
    *SIMPLE,
    ("201000010101", ORIGIN),
]


def _world(rows: list[tuple[str, str | None]]) -> gpd.GeoDataFrame:
    """A canned WBD 'world': (huc12, tohuc) rows with distinct unit squares."""
    geoms = [box(float(i), 0.0, float(i) + 0.9, 0.9) for i in range(len(rows))]
    return gpd.GeoDataFrame(
        {"huc12": [r[0] for r in rows], "tohuc": [r[1] for r in rows], "geometry": geoms},
        crs=4326,
    )


class FakeWaterData:
    """Stands in for ``pynhd.WaterData``: answers region LIKE fetches and tohuc probes."""

    def __init__(
        self,
        world: gpd.GeoDataFrame,
        *,
        probe_error: Exception | None = None,
        in_rejected: bool = False,
        region_prefixes: set[str] | None = None,
        region_errors: set[str] | None = None,
    ) -> None:
        self.world = world
        self.probe_error = probe_error  # raise this on every tohuc probe
        self.in_rejected = in_rejected  # reject `tohuc IN (...)`, allow equality
        self.region_prefixes = region_prefixes  # if set, only these LIKE prefixes hit
        self.region_errors = region_errors or set()  # LIKE prefixes that raise
        self.probe_filters: list[str] = []

    def byfilter(self, cql: str):
        if cql.startswith("huc12 LIKE"):
            prefix = cql.split("'")[1].rstrip("%")
            if prefix in self.region_errors:
                raise RuntimeError(f"WFS 502 for region {prefix}")
            if self.region_prefixes is not None and prefix not in self.region_prefixes:
                return self.world.iloc[0:0]  # empty -> _fetch_region returns None
            return self.world[self.world["huc12"].str.startswith(prefix)].copy()
        self.probe_filters.append(cql)
        if self.probe_error is not None:
            raise self.probe_error
        if cql.startswith("tohuc IN"):
            if self.in_rejected:
                raise RuntimeError("IN filter rejected by WFS")
            ids = cql.split("'")[1::2]
            return self.world[self.world["tohuc"].isin(ids)].copy()
        if cql.startswith("tohuc ="):
            target = cql.split("'")[1]
            return self.world[self.world["tohuc"] == target].copy()
        raise AssertionError(f"unexpected CQL filter: {cql}")


def _origin() -> HucResult:
    return HucResult(
        huc_id=ORIGIN, huc_level=12, geometry=box(0, 0, 1, 1), lat=45.0, lon=-100.0
    )


def _patch(monkeypatch: pytest.MonkeyPatch, fake: FakeWaterData) -> None:
    monkeypatch.setattr(upstream, "_water_data", lambda layer: fake)


def test_mid_region_external_inflow_triggers_widening(monkeypatch):
    """H-1(a): inflow at a non-leaf node must be detected and the region widened."""
    _patch(monkeypatch, FakeWaterData(_world(MID_INFLOW)))
    trace = _trace_tohuc(_origin())
    assert trace is not None
    # The external tributary and its headwater are captured after widening to HU6.
    assert "101000020201" in trace.upstream_huc_ids
    assert "101000020202" in trace.upstream_huc_ids
    assert any("widened to HU6" in n for n in trace.notes)
    assert trace.complete is True
    assert trace.completeness_notes == []


def test_in_filter_rejection_falls_back_to_per_node_probes(monkeypatch):
    """A WFS that rejects `tohuc IN (...)` still detects mid-region inflow."""
    fake = FakeWaterData(_world(MID_INFLOW), in_rejected=True)
    _patch(monkeypatch, fake)
    trace = _trace_tohuc(_origin())
    assert trace is not None
    assert "101000020201" in trace.upstream_huc_ids
    assert trace.complete is True
    # Both filter forms were exercised.
    assert any(f.startswith("tohuc IN") for f in fake.probe_filters)
    assert any(f.startswith("tohuc =") for f in fake.probe_filters)


def test_probe_failure_fails_toward_widening_and_flags_at_ceiling(monkeypatch):
    """H-1(b)+(c): probe errors mean *possible* inflow — widen, then flag at HU4."""
    _patch(monkeypatch, FakeWaterData(_world(SIMPLE), probe_error=RuntimeError("WFS 502")))
    trace = _trace_tohuc(_origin())
    assert trace is not None
    # Kept widening rather than accepting the HU8 result as verified...
    assert any("widened to HU6" in n for n in trace.notes)
    assert any("widened to HU4" in n for n in trace.notes)
    # ... and at the widest level the unverifiable result is flagged, not blessed.
    assert trace.complete is False
    assert any("probe failed" in n for n in trace.completeness_notes)


def test_inflow_at_widest_level_flags_truncation_risk(monkeypatch):
    """H-1(c): tohuc links cross HU4 boundaries; inflow at HU4 -> complete=False."""
    _patch(monkeypatch, FakeWaterData(_world(CROSS_HU4)))
    trace = _trace_tohuc(_origin())
    assert trace is not None
    assert trace.complete is False
    assert any("HU4" in n and "upstream area may be missing" in n
               for n in trace.completeness_notes)
    # The cross-HU4 tributary itself cannot be captured, only flagged.
    assert "201000010101" not in trace.upstream_huc_ids


@pytest.mark.parametrize("wider_fails_via", ["empty", "error"])
def test_widen_note_only_after_successful_wider_fetch(monkeypatch, wider_fails_via):
    """If widening fails, the truncated result must not claim it was widened."""
    kwargs = (
        {"region_prefixes": {"10100001"}}
        if wider_fails_via == "empty"
        else {"region_errors": {"101000", "1010"}}
    )
    _patch(monkeypatch, FakeWaterData(_world(CROSS_HU4), **kwargs))
    trace = _trace_tohuc(_origin())
    assert trace is not None
    assert not any("widened" in n for n in trace.notes)  # the pre-existing note bug
    assert trace.complete is False
    assert any("widening failed" in n for n in trace.completeness_notes)


def test_true_headwater_origin_stays_complete(monkeypatch):
    """A genuine headwater (edges exist, none upstream of origin) is not an error."""
    world = [
        (ORIGIN, "101000010900"),
        ("101000010900", None),
    ]
    _patch(monkeypatch, FakeWaterData(_world(world)))
    trace = _trace_tohuc(_origin())
    assert trace is not None
    assert trace.upstream_huc_ids == [ORIGIN]
    assert trace.complete is True
    assert trace.completeness_notes == []


def test_zero_edge_graph_fails_loudly(monkeypatch):
    """A tohuc walk with zero edges over fetched features must raise, not return
    an origin-only polygon posing as the watershed (H-1)."""
    world = [(ORIGIN, None), ("101000010102", None)]  # tohuc attribute effectively gone
    _patch(monkeypatch, FakeWaterData(_world(world)))
    with pytest.raises(UpstreamGraphError):
        _trace_tohuc(_origin())


def test_zero_edge_graph_degrades_to_nldi_then_reraises(monkeypatch):
    """trace_upstream: the graph error degrades to the NLDI fallback (NFR-6) and
    only surfaces when the fallback also fails."""
    world = [(ORIGIN, None), ("101000010102", None)]
    _patch(monkeypatch, FakeWaterData(_world(world)))

    nldi_trace = UpstreamTrace(
        origin_huc12=ORIGIN,
        upstream_huc_ids=[],
        polygon=box(0, 0, 1, 1),
        area_km2=42.0,
        method="nldi-ut",
    )
    monkeypatch.setattr(upstream, "trace_upstream_nldi", lambda origin: nldi_trace)
    assert trace_upstream(_origin()).method == "nldi-ut"

    monkeypatch.setattr(upstream, "trace_upstream_nldi", lambda origin: None)
    with pytest.raises(UpstreamGraphError):
        trace_upstream(_origin())


def test_wbd_fallback_propagates_completeness(monkeypatch):
    """The pour-point WBD fallback carries the trace's truncation-risk contract."""
    from upstreamwx.watershed import huc, pourpoint
    from upstreamwx.watershed import upstream as upstream_mod

    incomplete = UpstreamTrace(
        origin_huc12=ORIGIN,
        upstream_huc_ids=[ORIGIN],
        polygon=box(0, 0, 1, 1),
        area_km2=42.0,
        method="tohuc-graph",
        complete=False,
        completeness_notes=["external inflow beyond the widest HU4 fetch"],
    )
    monkeypatch.setattr(pourpoint, "delineate_pourpoint", lambda lat, lon: None)
    monkeypatch.setattr(huc, "resolve_huc12", lambda lat, lon: object())
    monkeypatch.setattr(upstream_mod, "trace_upstream", lambda origin: incomplete)

    basin = pourpoint.delineate(45.0, -100.0)
    assert basin.method == pourpoint.WBD_FALLBACK_METHOD
    assert basin.complete is False
    assert basin.completeness_notes == ["external inflow beyond the widest HU4 fetch"]
