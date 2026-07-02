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
    # Completeness contract (H-1): False whenever upstream area may be missing —
    # external inflow detected (or a completeness probe failed) at the widest fetch
    # level, or a widening the trace wanted could not be performed. Each False cause
    # appends a human-readable reason so the SITREP can surface truncation risk
    # instead of silently understating the flash-flood domain (FR-3).
    complete: bool = True
    completeness_notes: list[str] = field(default_factory=list)


class UpstreamGraphError(RuntimeError):
    """The WBD tohuc walk produced no graph edges over a non-empty region.

    Zero edges over fetched features means the ``tohuc`` attribute is missing or
    renamed (e.g. the HU10 fallback layer lacking ToHUC) — the walk would return an
    origin-only "basin" posing as the watershed. That must fail loudly (H-1), not
    silently understate the flash-flood domain.
    """


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


def _trace_tohuc(origin: HucResult) -> UpstreamTrace | None:
    """Primary deterministic tohuc graph walk with region auto-widening.

    Fetches the surrounding HU8 first. If any HUC-12 *outside* the fetched
    region drains (via ``tohuc``) into the current upstream set, the watershed
    is truncated at the region boundary, so we widen the fetch to HU6 then HU4
    and recompute. This correctly captures cross-HU8 inflows (common for
    plains rivers whose headwaters sit in an adjacent subbasin).

    Completeness (H-1): the external-inflow check runs at *every* accepted
    level, including the widest — tohuc links legitimately cross HU4/HU2
    boundaries. Inflow (or a failed probe, which must never read as "no
    inflow") at the widest level, or a widening whose wider fetch fails, marks
    the trace ``complete=False`` with an explanatory completeness note.

    Raises:
        UpstreamGraphError: if a fetched region has features but the tohuc walk
            produced zero graph edges (missing/renamed attribute).
    """
    level = origin.huc_level
    origin_id = origin.huc_id
    notes: list[str] = []
    complete = True
    completeness_notes: list[str] = []

    gdf = None
    upstream: set[str] = set()
    # Set when a level detected truncation risk and we moved on to a wider fetch.
    # The "widened to HUX" note is recorded only once the wider fetch actually
    # succeeds — appending it up front would let a failed wider fetch return the
    # truncated result carrying a note claiming it was widened.
    widen_pending: tuple[int, str] | None = None  # (from prefix_len, reason)
    # Prefix lengths to try, widest scope last. HUC-12 ids are 12 chars; HU8/6/4
    # are 8/6/4-char prefixes.
    prefixes = [p for p in (8, 6, 4) if p < len(origin_id)]
    for i, prefix_len in enumerate(prefixes):
        try:
            candidate = _fetch_region(level, origin_id[:prefix_len])
        except Exception:
            if gdf is None:
                raise  # no usable narrower result yet; let the caller's retry handle it
            # A widening fetch failed: fall back to the narrower result, which the
            # widen_pending bookkeeping below marks complete=False (H-1b).
            candidate = None
        if candidate is None:
            continue
        id_col = _id_col(candidate, level)
        graph = _build_graph(candidate, origin_id, id_col)
        if graph.number_of_edges() == 0 and len(candidate) > 0:
            raise UpstreamGraphError(
                f"tohuc walk built zero edges over {len(candidate)} HUC-{level} "
                f"features (prefix {origin_id[:prefix_len]}); tohuc attribute "
                "missing or renamed — refusing to return an origin-only basin"
            )
        if origin_id not in graph:
            continue
        # descendants in the downstream->upstream graph == contributing area.
        ups = nx.descendants(graph, origin_id) | {origin_id}
        gdf, upstream = candidate, ups
        if widen_pending is not None:
            from_len, reason = widen_pending
            notes.append(
                f"HU{from_len} fetch truncated upstream set ({reason}); "
                f"widened to HU{prefix_len}"
            )
            widen_pending = None
        # Truncation check: does anything outside this region drain into the set?
        inflow, probe_failed = _external_inflow(level, id_col, candidate, ups)
        if not inflow and not probe_failed:
            break
        reason = "external inflow detected" if inflow else "completeness probe failed"
        if i + 1 < len(prefixes):
            # Fail toward widening: a failed probe is treated as possible inflow.
            widen_pending = (prefix_len, reason)
            continue
        # Widest fetch level — nowhere left to widen: flag truncation risk.
        complete = False
        if inflow:
            completeness_notes.append(
                f"external inflow into the upstream set beyond the widest "
                f"HU{prefix_len} fetch (tohuc links can cross HU4/HU2 boundaries); "
                "upstream area may be missing"
            )
        else:
            completeness_notes.append(
                f"completeness probe failed at the widest HU{prefix_len} fetch; "
                "could not verify that no upstream area was cut off"
            )
        break

    if gdf is None or not upstream:
        return None

    if widen_pending is not None:
        # A wider region was wanted but every wider fetch failed: the accepted
        # narrower result may be missing upstream area.
        from_len, reason = widen_pending
        complete = False
        completeness_notes.append(
            f"{reason} at the HU{from_len} fetch but widening failed; "
            "result may be truncated at the region boundary"
        )

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
        complete=complete,
        completeness_notes=completeness_notes,
    )


# Ids per ``tohuc IN (...)`` probe. 12-char ids quote to ~16 bytes each, so 50 keeps
# the CQL filter comfortably inside WFS GET request-size limits while covering a
# large upstream set in a handful of round-trips.
_PROBE_CHUNK = 50


def _zero_matched_errors() -> tuple[type[Exception], ...]:
    """Exception types pynhd raises for a *successful* zero-feature response.

    ``WaterData.byfilter`` raises ``ZeroMatchedError`` when the filter matches
    nothing — for an inflow probe that legitimately means "no external inflow"
    and must not be confused with a transient WFS failure (H-1b). Lazy import to
    keep ``pynhd`` off the import path for offline, fixture-only work.
    """
    try:
        from pynhd.exceptions import ZeroMatchedError
    except Exception:  # noqa: BLE001 - pynhd absent in offline fixture-only envs
        return ()
    return (ZeroMatchedError,)


def _external_inflow(level: int, id_col: str, gdf, upstream: set[str]) -> tuple[bool, bool]:
    """Probe WBD for contributing area cut off at the fetched region boundary (H-1a).

    External inflow can join the upstream set at *any* node, not only headwater
    leaves — a tributary from an adjacent HU8 confluences mid-region (the
    Paria-into-Colorado topology) — so every member of the upstream set is
    probed: a WBD feature whose ``tohuc`` targets the set but which lies outside
    the fetched region means upstream area is missing.

    Probes are chunked ``tohuc IN (...)`` set-difference queries; if the WFS
    rejects the IN form for a chunk, we retry that chunk with per-node
    ``tohuc = '<id>'`` equality (the one filter form this GeoServer WFS is known
    to accept). Returns ``(inflow_found, probe_failed)``: a probe failure is
    reported separately because it must never read as "no inflow" (H-1b) — the
    caller fails toward widening, or flags truncation risk at the widest level.
    """
    in_region = set(gdf[id_col])
    targets = sorted(upstream)
    zero_matched = _zero_matched_errors()
    probe_failed = False
    wd = _water_data(f"wbd{level}")

    def _probe(cql: str):
        """One WFS probe: a GeoDataFrame, None for zero matches, or raises."""
        try:
            ext = wd.byfilter(cql)
        except zero_matched:
            return None  # a real "nothing drains into these ids" answer
        return None if ext is None or ext.empty else ext

    for start in range(0, len(targets), _PROBE_CHUNK):
        chunk = targets[start : start + _PROBE_CHUNK]
        in_list = ", ".join(f"'{t}'" for t in chunk)
        try:
            ext = _probe(f"tohuc IN ({in_list})")
        except Exception:  # noqa: BLE001 - IN rejected/failed: per-node equality
            ext = None
            for target in chunk:
                try:
                    one = _probe(f"tohuc = '{target}'")
                except Exception:  # noqa: BLE001 - H-1b: possible inflow, never "none"
                    probe_failed = True
                    continue
                if one is not None and any(
                    h not in in_region for h in one[_id_col(one, level)]
                ):
                    return True, probe_failed
        if ext is not None and any(h not in in_region for h in ext[_id_col(ext, level)]):
            return True, probe_failed
    return False, probe_failed


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
    NLDI upstream-tributaries basin. A broken tohuc graph (zero edges over a
    non-empty region) degrades to NLDI (NFR-6) but re-raises if the fallback
    also fails — an origin-only polygon must never pose as the watershed (H-1).

    Raises:
        UpstreamGraphError: if the tohuc walk built no graph edges and the NLDI
            fallback also failed.
        ValueError: if neither method produces an upstream domain.
    """
    graph_error: UpstreamGraphError | None = None
    try:
        result = _trace_tohuc(origin)
    except UpstreamGraphError as exc:
        graph_error, result = exc, None
    if result is not None and result.upstream_huc_ids:
        return result

    fallback = trace_upstream_nldi(origin)
    if fallback is not None:
        return fallback

    if graph_error is not None:
        raise graph_error

    if result is not None:
        return result

    raise ValueError(f"Could not trace upstream watershed for HUC {origin.huc_id}")
