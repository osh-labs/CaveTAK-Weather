"""Persistent, cycle/member/hour-keyed on-disk cache for GEFS member subsets.

The GEFS analogue of :mod:`upstreamwx.sref.cache`, over the same shared primitive
(:mod:`upstreamwx.grib.cache`). GEFS ships **per-member grids** (no probability product), so the
cache key gains a ``member`` axis: ``(cycle, member, fhour, var, level)``. The cached artifact is
the byte-range subset ``.grib2`` itself, re-decoded on a hit (bit-identical to the live decode),
written atomically (NFR-6). The request path (:mod:`upstreamwx.ingest.gefs_provider`) fans these
cached loads across members in parallel and computes the exceedance fraction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import requests
import xarray as xr

from ..config import Settings, get_settings
from ..grib.cache import cached_subset, decode_cached, prune_cycle_dirs
from ..grib.idx import download_subset, fetch_idx, select_messages
from .extract import GefsField, _primary_dataarray, open_subset
from .sources import DEFAULT_SET, GEFS_CYCLES, MEMBERS, GefsCycle, gefs_base

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
    """Decode a cached member subset to its primary DataArray, eagerly loaded into memory."""
    return _primary_dataarray(open_subset(path)).load()


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
) -> GefsField:
    """Load one GEFS member's field (uncropped), using the persistent cache when present.

    On a miss the byte-range subset for that member message is fetched and written atomically
    under the cycle dir, then decoded. The provider crops + aggregates afterwards (the cache is
    polygon-agnostic). ``fcst`` selects the accumulation/valid-time window; ``level`` disambiguates
    (e.g. CAPE ``surface`` vs ``180-0 mb above ground``).
    """
    settings = settings or get_settings()
    path = _cycle_dir(settings, cycle) / _subset_name(member, fhour, var, level)
    base = gefs_base(settings)  # honor gefs_base_url override consistently

    path, selected = cached_subset(
        path,
        idx_url=cycle.idx_url(member, fhour, res_set, base),
        grib_url=cycle.member_url(member, fhour, res_set, base),
        select=lambda entries: select_messages(entries, var=var, fcst=fcst, level=level),
        refresh=refresh,
        what=f"{member} f{fhour:03d} var={var!r} level={level!r} fcst={fcst!r}",
        fetch_idx=fetch_idx,
        download_subset=download_subset,
    )

    da = decode_cached(path, _decode)
    return GefsField(
        name=var,
        member=member,
        fhour=fhour,
        data=da,
        grib_path=path,
        extras={"level": level, "fcst": fcst, "res_set": res_set, "cached": selected is None},
    )


def warm_cycle(
    cycle: GefsCycle,
    fhours: tuple[int, ...],
    *,
    settings: Settings | None = None,
    members: tuple[str, ...] = MEMBERS,
    fields: tuple[FieldSpec, ...] = DEFAULT_FIELDS,
    res_set: str = DEFAULT_SET,
) -> list[Path]:
    """Pre-pull a cycle's member subsets for the given forecast hours into the cache.

    GEFS warming is heavy (members x fhours x fields), so the caller passes a bounded ``fhours``
    list (e.g. the next-day lead band). Idempotent and degradation-tolerant: a field missing at
    an hour is skipped (NFR-6). Returns the cached subset paths.
    """
    settings = settings or get_settings()
    paths: list[Path] = []
    for fhour in fhours:
        for spec in fields:
            start = max(fhour - spec.window_h, 0)
            fcst = f"{start}-{fhour} hour acc" if spec.window_h else f"{fhour} hour fcst"
            for member in members:
                try:
                    field = load_member_field_cached(
                        cycle, member, fhour, spec.var, fcst, spec.level, res_set,
                        settings=settings,
                    )
                except (LookupError, TimeoutError, OSError, requests.RequestException) as exc:
                    logger.debug(
                        "GEFS warm skipped %s f%03d %s: %s", member, fhour, spec.var, exc
                    )
                    continue
                paths.append(field.grib_path)
    return paths


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
