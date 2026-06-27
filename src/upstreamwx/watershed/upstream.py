"""Upstream contributing-watershed trace (PRD FR-3).

Given a containing HUC-12 (:class:`~upstreamwx.watershed.huc.HucResult`),
deterministically collect every HUC-12 that drains *into* it and dissolve their
boundaries into a single upstream domain polygon. This polygon is the
aggregation domain consumed by the SREF processor (Spike A).

Primary method (``tohuc-graph``):
  Each WBD HUC-12 carries a ``tohuc`` attribute naming the HUC-12 immediately
  downstream of it. We fetch every HUC-12 in the surrounding HU8 (widening to
  HU6 if the origin sits near an HU8 edge), build a directed graph of
  downstream->upstream edges with networkx, and collect all descendants of the
  origin. This is order-independent and fully reproducible given a WBD snapshot.

Fallback method (``nldi-ut``):
  If the WBD attribute walk yields nothing usable, navigate upstream
  tributaries from the nearest NHDPlus flowline via the USGS NLDI service and
  aggregate the returned basin geometry.

Areas are computed in EPSG:5070 (CONUS Albers equal-area).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx
from shapely.geometry.base import BaseGeometry

from .huc import HucResult, _field, _water_data

_EQUAL_AREA_CRS = 5070  # NAD83 / CONUS Albers


@dataclass
class UpstreamTrace:
    """The dissolved upstream contributing watershed for an origin HUC-12."""

    origin_huc12: str
    upstream_huc_ids: list[str]
    polygon: BaseGeometry  # dissolved upstream domain, incl. the origin, EPSG:4326
    area_km2: float
    method: str  # "tohuc-graph" | "nldi-ut"
    huc_level: int = 12
    notes: list[str] = field(default_factory=list)


def _tohuc_of(row) -> str | None:
    """Read a HUC-12's downstream id, tolerant of casing/spelling variants."""
    return _field(row, ("tohuc", "tohuc12", "to_huc", "downstream"))


def _huc_id_of(row, level: int) -> str | None:
    return _field(row, (f"huc{level}", "huc12", "huc10", "huc"))


def _area_km2(gdf) -> float:
    """Dissolved area in km^2 via an equal-area projection."""
    return float(gdf.to_crs(_EQUAL_AREA_CRS).geometry.union_all().area / 1e6)


def _fetch_region(level: int, prefix: str):
    """Fetch all HUC-{level} features whose id starts with ``prefix`` from WBD."""
    gdf = _water_data(f"wbd{level}").byfilter(f"huc{level} LIKE '{prefix}%'")
    return None if gdf is None or gdf.empty else gdf.to_crs(4326)


def _build_graph(gdf, origin_id: str, id_col: str) -> nx.DiGraph:
    """Directed graph of downstream -> upstream edges over the fetched region."""
    ids = set(gdf[id_col])
    graph = nx.DiGraph()
    graph.add_nodes_from(sorted(ids))
    for _, row in gdf.sort_values(id_col).iterrows():
        down = _tohuc_of(row)
        hid = _huc_id_of(row, len(origin_id))
        if hid is not None and down is not None and down in ids:
            graph.add_edge(down, hid)
    return graph


def _upstream_set(gdf, origin_id: str, id_col: str) -> set[str]:
    """All HUC ids in ``gdf`` that drain into ``origin_id`` (inclusive), via tohuc."""
    graph = _build_graph(gdf, origin_id, id_col)
    if origin_id not in graph:
        return set()
    # descendants in the downstream->upstream graph == contributing area.
    return nx.descendants(graph, origin_id) | {origin_id}


def _trace_tohuc(origin: HucResult) -> UpstreamTrace | None:
    """Primary deterministic tohuc graph walk with region auto-widening.

    Fetches the surrounding HU8 first. If any HUC-12 *outside* the fetched
    region drains (via ``tohuc``) into the current upstream set, the watershed
    is truncated at the region boundary, so we widen the fetch to HU6 then HU4
    and recompute. This correctly captures cross-HU8 inflows (common for
    plains rivers whose headwaters sit in an adjacent subbasin).
    """
    level = origin.huc_level
    origin_id = origin.huc_id
    notes: list[str] = []

    gdf = None
    upstream: set[str] = set()
    # Prefix lengths to try, widest scope last. HUC-12 ids are 12 chars; HU8/6/4
    # are 8/6/4-char prefixes.
    prefixes = [p for p in (8, 6, 4) if p < len(origin_id)]
    for i, prefix_len in enumerate(prefixes):
        candidate = _fetch_region(level, origin_id[:prefix_len])
        if candidate is None:
            continue
        id_col = _id_col(candidate, level)
        if origin_id not in set(candidate[id_col]):
            continue
        ups = _upstream_set(candidate, origin_id, id_col)
        if not ups:
            continue
        gdf, upstream = candidate, ups
        # Truncation check: does anything outside this region drain into the set?
        if i + 1 < len(prefixes) and _has_external_inflow(
            level, origin_id, id_col, candidate, ups
        ):
            notes.append(
                f"HU{prefix_len} fetch truncated upstream set; widened to "
                f"HU{prefixes[i + 1]}"
            )
            continue
        break

    if gdf is None or not upstream:
        return None

    id_col = _id_col(gdf, level)
    upstream_ids = sorted(upstream)
    sub = gdf[gdf[id_col].isin(upstream)]
    dissolved = sub.dissolve()
    polygon = dissolved.geometry.iloc[0]

    return UpstreamTrace(
        origin_huc12=origin_id,
        upstream_huc_ids=upstream_ids,
        polygon=polygon,
        area_km2=_area_km2(sub),
        method="tohuc-graph",
        huc_level=level,
        notes=notes,
    )


def _has_external_inflow(level, origin_id, id_col, gdf, upstream) -> bool:
    """True if real contributing area was cut off at the fetched region boundary.

    External area can only enter at the *headwater leaves* of the in-region
    upstream graph — members of the upstream set that have no in-region
    upstream neighbour. For each such leaf we probe WBD (single-attribute
    ``tohuc = '<leaf>'`` equality, the only filter form this GeoServer WFS
    reliably accepts) and check whether any returned HUC lies outside the
    fetched region.
    """
    graph = _build_graph(gdf, origin_id, id_col)
    in_region = set(gdf[id_col])
    # Leaves: upstream nodes with no upstream neighbour inside the region.
    leaves = sorted(h for h in upstream if not (set(graph.successors(h)) & upstream))
    wd = _water_data(f"wbd{level}")
    for leaf in leaves:
        try:
            ext = wd.byfilter(f"tohuc = '{leaf}'")
        except Exception:  # noqa: BLE001 - probe failure: skip this leaf
            continue
        if ext is None or ext.empty:
            continue
        if any(h not in in_region for h in ext[_id_col(ext, level)]):
            return True
    return False


def _id_col(gdf, level: int) -> str:
    lower = {c.lower(): c for c in gdf.columns}
    for cand in (f"huc{level}", "huc12", "huc10", "huc"):
        if cand in lower:
            return lower[cand]
    raise KeyError(f"no HUC id column found in {list(gdf.columns)}")


def trace_upstream_nldi(origin: HucResult) -> UpstreamTrace | None:
    """Fallback / cross-check: NLDI upstream-tributaries basin from nearest flowline."""
    try:
        from pynhd import NLDI
    except Exception:  # noqa: BLE001
        return None

    try:
        nldi = NLDI()
        comid = nldi.comid_byloc((origin.lon, origin.lat))
        feature_id = str(comid[comid.columns[0]].iloc[0]) if hasattr(comid, "columns") else None
        if feature_id is None:
            return None
        basin = nldi.get_basins(feature_id, fsource="comid")
    except Exception:  # noqa: BLE001
        return None

    if basin is None or basin.empty:
        return None

    basin = basin.to_crs(4326)
    dissolved = basin.dissolve()
    return UpstreamTrace(
        origin_huc12=origin.huc_id,
        upstream_huc_ids=[],
        polygon=dissolved.geometry.iloc[0],
        area_km2=_area_km2(basin),
        method="nldi-ut",
        huc_level=origin.huc_level,
        notes=["NLDI upstream-tributaries basin (no discrete HUC-12 ids)"],
    )


def trace_upstream(origin: HucResult) -> UpstreamTrace:
    """Trace the upstream contributing watershed for a containing HUC.

    Tries the deterministic ``tohuc`` graph walk first, then falls back to the
    NLDI upstream-tributaries basin.

    Raises:
        ValueError: if neither method produces an upstream domain.
    """
    result = _trace_tohuc(origin)
    if result is not None and result.upstream_huc_ids:
        return result

    fallback = trace_upstream_nldi(origin)
    if fallback is not None:
        return fallback

    if result is not None:
        return result

    raise ValueError(f"Could not trace upstream watershed for HUC {origin.huc_id}")
