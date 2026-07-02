"""Persistent, cycle/member/hour-keyed on-disk cache for GEFS member subsets.

The GEFS analogue of :mod:`upstreamwx.sref.cache`, over the same shared primitive
(:mod:`upstreamwx.grib.cache`). GEFS ships **per-member grids** (no probability product), so the
cache key gains a ``member`` axis: ``(cycle, member, fhour, var, level)``. The cached artifact is
the byte-range subset ``.grib2`` itself, re-decoded on a hit (bit-identical to the live decode),
written atomically (NFR-6). The request path (:mod:`upstreamwx.ingest.gefs_provider`) fans these
cached loads across members in parallel and computes the exceedance fraction.
"""

from __future__ import annotations

import functools
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import requests
import xarray as xr

from ..config import Settings, get_settings
from ..grib.cache import cached_subset, decode_cached, prune_cycle_dirs
from ..grib.idx import download_subset, fetch_idx, select_messages
from .extract import GefsField, _primary_dataarray, crop_bbox_normalize, open_subset
from .sources import DEFAULT_SET, GEFS_CYCLES, MEMBERS, GefsCycle, gefs_base

# Network-bound download fan-out for warming (mirrors the request path's member fan-out).
_WARM_MAX_WORKERS = 16

logger = logging.getLogger("upstreamwx.gefs.cache")


@dataclass(frozen=True)
class FieldSpec:
    """One GEFS field to fetch per member: variable, GRIB level, accumulation length (hours).

    ``window_h`` is the APCP accumulation length (the ``fcst`` window is derived per forecast
    hour); ``0`` means an instantaneous field (CAPE) whose ``fcst`` is ``"{fhour} hour fcst"``.
    """

    var: str
    level: str
    window_h: int


# Flash-flood precip (6 h APCP bucket) and the instability input for the lightning proxy.
DEFAULT_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("APCP", "surface", 6),
    FieldSpec("CAPE", "surface", 0),
)


def _cache_dir(settings: Settings) -> Path:
    d = settings.data_dir / "gefs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cycle_dir(settings: Settings, cycle: GefsCycle) -> Path:
    d = _cache_dir(settings) / f"{cycle.date}_{cycle.hh}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _subset_name(member: str, fhour: int, var: str, level: str) -> str:
    lvl = level.replace(" ", "").replace("-", "_")
    return f"{member}_f{fhour:03d}_{var}_{lvl}.grib2"


def _decode(path: Path) -> xr.DataArray:
    """Decode a cached member subset to its primary DataArray, eagerly loaded into memory.

    The cfgrib ``Dataset`` is closed explicitly (not left to GC) so the eccodes file handle is
    released while the caller still holds ``grib.cache._decode_compute_lock`` — see the matching
    note in :func:`upstreamwx.refs.cache._decode`. A deferred, cross-thread handle teardown vs a
    concurrent decode segfaults the worker (eccodes is not thread-safe).
    """
    with open_subset(path) as ds:
        return _primary_dataarray(ds).load()


def _decode_cropped(
    path: Path, bbox: tuple[float, float, float, float], margin: float
) -> xr.DataArray:
    """Decode a member subset and crop+normalize to ``bbox``, returning a small detached array.

    Top-level (hence picklable) so it can also run in the decode :class:`ProcessPoolExecutor` via
    ``functools.partial(_decode_cropped, bbox=..., margin=...)``. Used on **both** paths: in the
    pool worker (the small result is cheap to ship back) and in-process (the default on a small
    host). The trailing ``.copy()`` is load-bearing for the in-process path — xarray slicing returns
    a *view* that pins the whole ~16.5 MB global grid alive, so without it the LRU and the 16-way
    member fan-out would each retain full grids (the OOM that 502'd the 2 GB host). Copying the
    few-hundred-cell crop lets the full grid be freed immediately. The crop is identical to
    :func:`crop_and_normalize` per-polygon; aggregation masks each domain over it unchanged (NFR-4).
    """
    return crop_bbox_normalize(_decode(path), bbox, margin=margin).copy(deep=True)


# Decode failures that indicate a corrupt/truncated on-disk subset (vs. a programming error):
# eccodes/cfgrib surface a truncated GRIB message as EOFError, and a malformed one as ValueError
# or a low-level OSError. These trigger a one-shot re-fetch of the artifact (see
# :func:`load_member_field_cached`); anything else propagates unchanged.
_CORRUPT_SUBSET_ERRORS = (EOFError, ValueError, OSError)


def _discard_subset(path: Path) -> None:
    """Delete a corrupt cached subset (and any ``.idx`` sidecar) so the next fetch re-downloads."""
    for p in (path, path.with_suffix(path.suffix + ".idx")):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:  # best-effort; a stuck file just means the re-fetch overwrites it
            logger.debug("could not remove %s during self-heal: %s", p, exc)


def _ensure_member_subset(
    cycle: GefsCycle,
    member: str,
    fhour: int,
    var: str,
    fcst: str,
    level: str,
    res_set: str = DEFAULT_SET,
    *,
    settings: Settings | None = None,
    refresh: bool = False,
) -> tuple[Path, list | None]:
    """Materialize one member field's byte-range subset on disk (no decode).

    Returns ``(path, selected)`` where ``selected`` is the fetched message list on a miss or
    ``None`` on a cache hit (so callers can record ``cached``). The download half of
    :func:`load_member_field_cached`, factored out so warming can pre-pull subsets without paying
    the (wasted) cfgrib decode — warming only needs the disk artifact.
    """
    settings = settings or get_settings()
    path = _cycle_dir(settings, cycle) / _subset_name(member, fhour, var, level)
    base = gefs_base(settings)  # honor gefs_base_url override consistently
    return cached_subset(
        path,
        idx_url=cycle.idx_url(member, fhour, res_set, base),
        grib_url=cycle.member_url(member, fhour, res_set, base),
        select=lambda entries: select_messages(entries, var=var, fcst=fcst, level=level),
        refresh=refresh,
        what=f"{member} f{fhour:03d} var={var!r} level={level!r} fcst={fcst!r}",
        fetch_idx=fetch_idx,
        download_subset=download_subset,
    )


def load_member_field_cached(
    cycle: GefsCycle,
    member: str,
    fhour: int,
    var: str,
    fcst: str,
    level: str,
    res_set: str = DEFAULT_SET,
    *,
    settings: Settings | None = None,
    refresh: bool = False,
    crop_bbox: tuple[float, float, float, float] | None = None,
    margin: float = 1.0,
    use_pool: bool = False,
) -> GefsField:
    """Load one GEFS member's field, using the persistent cache when present.

    On a miss the byte-range subset for that member message is fetched and written atomically
    under the cycle dir, then decoded. ``fcst`` selects the accumulation/valid-time window;
    ``level`` disambiguates (e.g. CAPE ``surface`` vs ``180-0 mb above ground``).

    When ``crop_bbox`` is given the decode crops+normalizes to it (in the pool worker if
    ``use_pool`` and one is installed, else in-process), so ``data`` comes back as the small cropped
    grid already in [-180, 180) — callers must NOT crop again. Cropping at decode time (not after)
    keeps the retained/in-flight arrays ~KB rather than the 16.5 MB global grid, which is what makes
    GEFS fit in memory on a small host. With ``crop_bbox`` unset it returns the uncropped grid (the
    on-disk subset cache stays polygon-agnostic either way).
    """
    settings = settings or get_settings()
    path, selected = _ensure_member_subset(
        cycle, member, fhour, var, fcst, level, res_set, settings=settings, refresh=refresh
    )

    # Crop in the decode whenever a bbox is supplied — in the pool worker if one is installed, else
    # in-process. Either way the retained/returned array is the small cropped grid, not the 16.5 MB
    # global one (the in-process full-grid retention is what OOM-killed the 2 GB host).
    cropped = crop_bbox is not None

    def _decode_subset() -> xr.DataArray:
        if cropped:
            return decode_cached(
                path,
                functools.partial(_decode_cropped, bbox=crop_bbox, margin=margin),
                use_pool=use_pool,
                key_extra=("crop", crop_bbox, margin),
            )
        return decode_cached(path, _decode, use_pool=use_pool)

    try:
        da = _decode_subset()
    except _CORRUPT_SUBSET_ERRORS as exc:
        # A truncated/corrupt cached subset — most often a byte range fetched while the GRIB
        # file was still publishing — fails to decode (classically EOFError). Left in place it
        # poisons this (cycle, member, fhour) for the life of the cached cycle, degrading GEFS
        # on *every* briefing. Self-heal: drop the bad artifact and re-fetch once (data quality
        # first-class, NFR-6). A second failure propagates so the caller degrades this member
        # and the ensemble quorum carries.
        logger.warning("GEFS subset %s failed to decode (%s); refetching once", path.name, exc)
        _discard_subset(path)
        _ensure_member_subset(
            cycle, member, fhour, var, fcst, level, res_set, settings=settings, refresh=True
        )
        da = _decode_subset()
    return GefsField(
        name=var,
        member=member,
        fhour=fhour,
        data=da,
        grib_path=path,
        extras={
            "level": level,
            "fcst": fcst,
            "res_set": res_set,
            "cached": selected is None,
            "cropped": cropped,
        },
    )


def warm_cycle(
    cycle: GefsCycle,
    fhours: tuple[int, ...],
    *,
    settings: Settings | None = None,
    members: tuple[str, ...] = MEMBERS,
    fields: tuple[FieldSpec, ...] = DEFAULT_FIELDS,
    res_set: str = DEFAULT_SET,
    max_workers: int = _WARM_MAX_WORKERS,
) -> list[Path]:
    """Pre-pull a cycle's member subsets for the given forecast hours into the cache.

    GEFS warming is heavy (members x fhours x fields — ~1000 subsets for the f24-f120 band), so:
    fetches are **download-only** (no cfgrib decode — warming only needs the on-disk subset, the
    decode is wasted work) and **fanned across a thread pool** (network-bound, mirrors the request
    path's member fan-out). The caller passes a bounded ``fhours`` list. Idempotent and
    degradation-tolerant: a field missing at an hour is skipped (NFR-6). Returns the cached paths.
    """
    settings = settings or get_settings()

    tasks: list[tuple[int, FieldSpec, str, str]] = []
    for fhour in fhours:
        for spec in fields:
            start = max(fhour - spec.window_h, 0)
            fcst = f"{start}-{fhour} hour acc" if spec.window_h else f"{fhour} hour fcst"
            for member in members:
                tasks.append((fhour, spec, member, fcst))
    if not tasks:
        return []

    def _warm_one(task: tuple[int, FieldSpec, str, str]) -> Path | None:
        fhour, spec, member, fcst = task
        try:
            path, _ = _ensure_member_subset(
                cycle, member, fhour, spec.var, fcst, spec.level, res_set, settings=settings
            )
            return path
        except (LookupError, TimeoutError, OSError, requests.RequestException) as exc:
            logger.debug("GEFS warm skipped %s f%03d %s: %s", member, fhour, spec.var, exc)
            return None

    with ThreadPoolExecutor(max_workers=min(max_workers, len(tasks))) as pool:
        return [p for p in pool.map(_warm_one, tasks) if p is not None]


def cached_cycles(
    now: datetime | None = None,
    *,
    settings: Settings | None = None,
    max_back: int = 4,
) -> list[GefsCycle]:
    """GEFS cycles present (and non-empty) in the on-disk cache, newest-first.

    Mirrors :func:`upstreamwx.sref.cache.cached_cycles`. Skips empty/malformed dirs, any hour
    that is not a real GEFS cycle, and any cycle dated in the future relative to ``now``.
    """
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    settings = settings or get_settings()
    root = settings.data_dir / "gefs"
    if not root.is_dir():
        return []

    cycles: list[GefsCycle] = []
    for d in root.iterdir():
        if not d.is_dir() or not any(d.iterdir()):
            continue
        try:
            date, hh = d.name.split("_")
            cycle = GefsCycle(date=date, hour=int(hh))
        except (ValueError, KeyError):
            continue
        if cycle.hour in GEFS_CYCLES and cycle.init_time <= now:
            cycles.append(cycle)

    cycles.sort(key=lambda c: c.init_time, reverse=True)
    return cycles[:max_back]


def prune_old_cycles(*, settings: Settings | None = None, keep: int = 4) -> list[Path]:
    """Delete cached GEFS cycle dirs beyond the newest ``keep``."""
    settings = settings or get_settings()
    return prune_cycle_dirs(_cache_dir(settings), keep=keep)
