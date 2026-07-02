"""On-disk cache for HUC resolution + upstream tracing (M0.1 promotion of Spike B).

The live USGS WBD trace is deterministic but slow (≈3 s for compact basins, up to
≈15 s for plains rivers needing region widening). M0.1 promotes it to a cached
module: a point's dissolved upstream domain is resolved once and reused. The cache
is a GeoJSON Feature per rounded lat/lon under ``Settings.data_dir/watershed``.

Note: this is a *process/disk* cache for on-demand use; the persistent
cross-restart cache the scheduler relies on is an M0.1.1 / EC2 concern.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import TypeVar

from shapely.geometry import mapping, shape

from ..config import Settings, get_settings
from .huc import resolve_huc12
from .pourpoint import WBD_FALLBACK_METHOD, PourpointBasin, delineate
from .upstream import UpstreamTrace, trace_upstream

logger = logging.getLogger("upstreamwx.watershed.cache")

_T = TypeVar("_T")

# Rounding precision for the cache key. The cache exists ONLY for identical-point
# reuse (planner warm -> the briefing seconds later; the scheduled 6 h refresh;
# reopening a saved mission): two *different* user-entered coordinates must never
# share a basin (H-9a — at 3 decimals, pins on opposite sides of a drainage divide
# shared a key). 6 decimals is ~10 cm — effectively exact for UI coordinates while
# still tolerating float round-trip formatting.
_KEY_PRECISION = 6

# WBD-fallback basins are coarse (24-54 % over-inclusive per Spike D) and are only
# cached because the exact NLDI path was down at delineation time; once an entry is
# this old we retry the exact path instead of pinning the fallback forever (H-9b).
# 6 h matches the scheduled-refresh cadence, so a live mission gets one upgrade
# attempt per ensemble cycle.
_FALLBACK_TTL_S = 6 * 3600

# How long a single-flight waiter blocks on the owner's Future. Generously above
# the worst observed live delineation (~15 s, plus retries/backoff) so it fires
# only when the owner is truly stuck (hung socket, dead thread); the waiter then
# evicts the stuck entry and delineates itself instead of hanging forever (H-9f).
_WAITER_TIMEOUT_S = 120.0


def _cache_dir(settings: Settings) -> Path:
    d = settings.data_dir / "watershed"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _key(lat: float, lon: float) -> str:
    return f"{round(lat, _KEY_PRECISION)}_{round(lon, _KEY_PRECISION)}"


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically so a concurrent reader never sees a
    half-written file (NFR-6). Watershed warming may delineate the same basin while a
    briefing reads/writes it; an os.replace of a temp file makes the swap indivisible.
    """
    # A unique temp name (not a fixed ".tmp" suffix) so two concurrent writers to the same
    # key — e.g. a refresh racing the single-flight owner — never clobber each other's temp.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            # fsync before the rename: without it, a host crash shortly after
            # os.replace can leave a zero-length file at the final path (H-9e).
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _to_feature(trace: UpstreamTrace) -> dict:
    return {
        "type": "Feature",
        "geometry": mapping(trace.polygon),
        "properties": {
            "origin_huc12": trace.origin_huc12,
            "upstream_huc_ids": trace.upstream_huc_ids,
            "area_km2": trace.area_km2,
            "method": trace.method,
            "huc_level": trace.huc_level,
            "notes": trace.notes,
            "complete": trace.complete,
            "completeness_notes": trace.completeness_notes,
        },
    }


def _from_feature(feature: dict) -> UpstreamTrace:
    props = feature["properties"]
    return UpstreamTrace(
        origin_huc12=props["origin_huc12"],
        upstream_huc_ids=props["upstream_huc_ids"],
        polygon=shape(feature["geometry"]),
        area_km2=props["area_km2"],
        method=props["method"],
        huc_level=props.get("huc_level", 12),
        notes=props.get("notes", []),
        # Pre-H-1 cache files lack the completeness fields; default to complete.
        complete=props.get("complete", True),
        completeness_notes=props.get("completeness_notes", []),
    )


def _read_feature(path: Path, parse: Callable[[dict], _T]) -> _T | None:
    """Read + parse a cached feature file, self-healing corruption (H-9c).

    A corrupt/empty/unparsable file used to raise out of every subsequent
    briefing for its key. Instead: log, unlink the bad file (best effort), and
    return None so the caller falls through to a live delineation.
    """
    try:
        return parse(json.loads(path.read_text()))
    except FileNotFoundError:
        return None  # lost a race with a concurrent unlink; treat as a miss
    except Exception:  # noqa: BLE001 - a bad cache file must never poison its key
        logger.warning("corrupt watershed cache file %s; removing it", path, exc_info=True)
        try:
            path.unlink()
        except OSError:
            pass
        return None


def _write_feature(path: Path, feature: dict) -> None:
    """Best-effort cache write: the result is already in memory, so a disk
    failure (e.g. disk full) must degrade to an uncached success, never fail
    the briefing (H-9d, NFR-6)."""
    try:
        _atomic_write(path, json.dumps(feature))
    except Exception:  # noqa: BLE001
        logger.warning("watershed cache write failed for %s; serving uncached", path,
                       exc_info=True)


def _resolve_and_trace(lat: float, lon: float, *, retries: int = 3) -> UpstreamTrace:
    """Live resolve + trace with simple backoff on transient USGS failures."""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return trace_upstream(resolve_huc12(lat, lon))
        except Exception as exc:  # transient WFS 403/502, timeouts (Spike B notes)
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"watershed trace failed for {lat},{lon}: {last_exc}") from last_exc


def resolve_and_trace_cached(
    lat: float,
    lon: float,
    *,
    settings: Settings | None = None,
    refresh: bool = False,
) -> UpstreamTrace:
    """Return the upstream trace for a point, using the on-disk cache when present."""
    settings = settings or get_settings()
    path = _cache_dir(settings) / f"{_key(lat, lon)}.geojson"

    if path.is_file() and not refresh:
        cached = _read_feature(path, _from_feature)
        if cached is not None:
            return cached

    trace = _resolve_and_trace(lat, lon)
    _write_feature(path, _to_feature(trace))
    return trace


# --------------------------------------------------------------------------- #
# Pour-point basin cache (NLDI raindrop two-step, with WBD fallback).
# --------------------------------------------------------------------------- #
# Single-flight registry: at most one live delineation per cache key. A briefing that
# arrives while a planner-triggered warm is still tracing the same point joins the
# in-flight Future instead of starting a duplicate 3-15 s NLDI/USGS trace. This is what
# makes "warm the cache the moment coordinates are entered" safe for the quick user who
# generates before the warm finishes.
_inflight_lock = threading.Lock()
_inflight: dict[str, Future[PourpointBasin]] = {}


def _pourpoint_dir(settings: Settings) -> Path:
    d = settings.data_dir / "watershed" / "pourpoint"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _basin_to_feature(basin: PourpointBasin) -> dict:
    return {
        "type": "Feature",
        "geometry": mapping(basin.polygon),
        "properties": {
            "lat": basin.lat,
            "lon": basin.lon,
            "snapped_lat": basin.snapped_lat,
            "snapped_lon": basin.snapped_lon,
            "area_km2": basin.area_km2,
            "method": basin.method,
            "comid": basin.comid,
            "flowline_name": basin.flowline_name,
            "notes": basin.notes,
            "complete": basin.complete,
            "completeness_notes": basin.completeness_notes,
        },
    }


def _basin_from_feature(feature: dict) -> PourpointBasin:
    props = feature["properties"]
    return PourpointBasin(
        lat=props["lat"],
        lon=props["lon"],
        snapped_lat=props["snapped_lat"],
        snapped_lon=props["snapped_lon"],
        polygon=shape(feature["geometry"]),
        area_km2=props["area_km2"],
        method=props["method"],
        comid=props.get("comid"),
        flowline_name=props.get("flowline_name"),
        notes=props.get("notes", []),
        # Pre-H-1 cache files lack the completeness fields; default to complete.
        complete=props.get("complete", True),
        completeness_notes=props.get("completeness_notes", []),
    )


def _fallback_expired(basin: PourpointBasin, path: Path) -> bool:
    """True when a cached WBD-fallback basin is past its upgrade-retry TTL (H-9b)."""
    if basin.method != WBD_FALLBACK_METHOD:
        return False
    try:
        age_s = time.time() - path.stat().st_mtime
    except OSError:
        return False  # cannot age the file; keep serving rather than churn (NFR-6)
    return age_s > _FALLBACK_TTL_S


def delineate_cached(
    lat: float,
    lon: float,
    *,
    settings: Settings | None = None,
    refresh: bool = False,
) -> PourpointBasin:
    """Return the pour-point basin for a point, using the on-disk cache when present.

    Concurrent callers for the same point (e.g. a background warm and the briefing that
    needs it) are coalesced: the first becomes the owner and runs the live delineation;
    the rest wait on its result. ``refresh=True`` always starts a fresh trace rather than
    joining an in-flight one.

    A cached WBD-fallback basin older than the upgrade TTL triggers a fresh delineation
    attempt; if that fails, the stale fallback is served best-effort (H-9b, NFR-6).
    """
    settings = settings or get_settings()
    key = _key(lat, lon)
    path = _pourpoint_dir(settings) / f"{key}.geojson"

    # Fast path: a warm disk file. Lock-free — read-only (a corrupt file is
    # unlinked by _read_feature and reads as a miss, H-9c).
    stale_fallback: PourpointBasin | None = None
    if path.is_file() and not refresh:
        cached = _read_feature(path, _basin_from_feature)
        if cached is not None:
            if not _fallback_expired(cached, path):
                return cached
            # Expired WBD fallback: attempt the exact path below, but keep the
            # stale basin to serve if the fresh delineation fails.
            stale_fallback = cached

    # Decide owner vs waiter under the registry lock.
    with _inflight_lock:
        # Re-check the disk inside the lock: the owner may have finished and written the
        # file between our fast-path check above and acquiring the lock. (Skipped when
        # we are retrying an expired fallback — that file is the one we want replaced.)
        if stale_fallback is None and path.is_file() and not refresh:
            cached = _read_feature(path, _basin_from_feature)
            if cached is not None and not _fallback_expired(cached, path):
                return cached
        existing = _inflight.get(key)
        if existing is not None and not refresh:
            fut, owner = existing, False
        else:
            fut, owner = Future(), True
            _inflight[key] = fut

    if not owner:
        try:
            return fut.result(timeout=_WAITER_TIMEOUT_S)
        except TimeoutError:
            # The owner is stuck (H-9f): evict its entry (if still registered) and
            # retry from the top — we either find the file it eventually wrote,
            # join a newer flight, or become the owner ourselves.
            logger.warning("watershed single-flight owner stuck >%.0fs for %s; retrying",
                           _WAITER_TIMEOUT_S, key)
            with _inflight_lock:
                if _inflight.get(key) is fut:
                    del _inflight[key]
            return delineate_cached(lat, lon, settings=settings)
        except BaseException:
            if stale_fallback is not None:
                return stale_fallback  # owner's retry failed; serve the stale fallback
            raise

    # The slow trace runs outside the lock so waiters never block the registry.
    try:
        basin = delineate(lat, lon)
    except BaseException as exc:
        fut.set_exception(exc)
        with _inflight_lock:
            # Identity check: a concurrent refresh may have replaced our entry.
            if _inflight.get(key) is fut:
                del _inflight[key]
        if stale_fallback is not None:
            return stale_fallback  # upgrade attempt failed; keep the stale fallback
        raise

    # Resolve waiters and release the registry BEFORE touching the disk: the basin
    # already exists in memory, so a cache-write failure (disk full) must not fan
    # an exception to every waiter after a successful delineation (H-9d).
    fut.set_result(basin)
    with _inflight_lock:
        if _inflight.get(key) is fut:
            del _inflight[key]
    _write_feature(path, _basin_to_feature(basin))
    return basin
