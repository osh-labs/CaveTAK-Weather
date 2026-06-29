"""GEFS provider — global ensemble over the upstream domain via in-house member exceedance (FR-7).

The SREF-replacement sibling of :mod:`upstreamwx.ingest.refs_provider`. GEFS ships **per-member
grids only** (no probability product), so this provider computes ``P(field > threshold)`` itself
as the **member-exceedance fraction** over the upstream domain — the value SREF's ``ensprod`` used
to hand us pre-baked. Member fetches are **fanned across a thread pool** (Spike F found a 31-member
sequential fetch overruns the per-call budget; ~16-way concurrency keeps it in budget).

GEFS has **no native thunderstorm-probability field**, so the lightning signal
(``gefs_p_tstm``) is a derived **convective proxy**: the per-member co-occurrence of instability
(CAPE) and precip over the Lightning Area of Concern. REFS (3 km) is authoritative for the same-day
lightning/flash-flood posture; GEFS is the coarse backstop beyond REFS range (the engine takes the
higher tier where both are in range, FR-19).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import requests
from shapely.geometry.base import BaseGeometry

from ..config import Settings, get_settings
from ..engine.models import Mission
from ..gefs import (
    aggregate_over_polygon,
    cached_cycles,
    crop_and_normalize,
    latest_available_cycle,
    load_member_field_cached,
)
from ..gefs.sources import MEMBERS, GefsCycle
from ..grib.cache import decode_pool_enabled
from .base import IngestBundle

logger = logging.getLogger("upstreamwx.ingest.gefs_provider")

NAME = "gefs"

# Flash-flood precip proxy: P(6-h APCP > 6.35 mm ≈ 0.25 in) over the watershed domain.
PRECIP_VAR, PRECIP_LEVEL = "APCP", "surface"
PRECIP_THRESH_MM = 6.35
# Instability for the lightning proxy: surface CAPE over the LAoC.
CAPE_VAR, CAPE_LEVEL = "CAPE", "surface"
# Lightning proxy: a member is "convective" when instability AND precip co-occur.
PROXY_CAPE_JKG = 1000.0
PROXY_PRECIP_MM = 2.5

# Cost guards (Spike F): GEFS is per-member, so bound the forecast-hour sample and fan the
# member fetches. 6-hourly steps align with the APCP 6 h bucket and halve the fetch count.
GEFS_STEP_H = 6
MAX_FHOURS = 8
MAX_WORKERS = 16


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _select_fhours(cycle: GefsCycle, window_start: datetime, window_end: datetime) -> list[int]:
    """GEFS forecast hours (6-hourly) whose 6 h APCP bucket overlaps the mission window.

    A bucket ending at ``f`` covers [f-6, f] in hours from cycle init; it overlaps [t0, t1] when
    ``f > t0`` and ``f - 6 < t1``. Bounded to :data:`MAX_FHOURS` by even subsampling. Falls back
    to the single nearest in-range hour if the window lands between steps or outside the horizon.
    """
    t0 = (_as_utc(window_start) - cycle.init_time).total_seconds() / 3600.0
    t1 = (_as_utc(window_end) - cycle.init_time).total_seconds() / 3600.0
    hours = [f for f in range(GEFS_STEP_H, 384 + 1, GEFS_STEP_H) if f > t0 and f - GEFS_STEP_H < t1]
    if not hours:
        mid = max(GEFS_STEP_H, min(384, round((t0 + t1) / 2 / GEFS_STEP_H) * GEFS_STEP_H))
        return [mid]
    if len(hours) > MAX_FHOURS:
        step = (len(hours) - 1) / (MAX_FHOURS - 1)
        hours = [hours[round(i * step)] for i in range(MAX_FHOURS)]
    return sorted(set(hours))


def _poly_max(field, polygon: BaseGeometry, var: str) -> float | None:
    da = crop_and_normalize(field.data, polygon)
    return aggregate_over_polygon(da, polygon, field_name=var, threshold="max").max_value


def _poly_max_precropped(field, polygon: BaseGeometry, var: str) -> float | None:
    """Domain max for a field already cropped+normalized in the decode pool (no re-crop).

    Re-running :func:`crop_and_normalize` on coords already shifted to [-180, 180) would
    double-shift longitude and corrupt the grid, so the pool path skips the crop. The worker
    cropped to the *union* of the watershed + LAoC bboxes, so masking each domain over the
    cropped grid is identical to cropping the full grid per domain (NFR-4).
    """
    return aggregate_over_polygon(field.data, polygon, field_name=var, threshold="max").max_value


def _union_bounds(
    p: BaseGeometry, q: BaseGeometry | None
) -> tuple[float, float, float, float]:
    """Bounding box covering both polygons (minx, miny, maxx, maxy)."""
    if q is None or q is p:
        return p.bounds
    ax, ay, axx, ayy = p.bounds
    bx, by, bxx, byy = q.bounds
    return (min(ax, bx), min(ay, by), max(axx, bxx), max(ayy, byy))


def _member_sample(
    cycle: GefsCycle,
    member: str,
    fhour: int,
    polygon: BaseGeometry,
    ltng_polygon: BaseGeometry,
    *,
    settings: Settings,
    crop_bbox: tuple[float, float, float, float] | None = None,
    use_pool: bool = False,
) -> tuple[float | None, float | None, float | None]:
    """One member at one fhour: (apcp over watershed, apcp over LAoC, cape over LAoC).

    Each field is fetched once (cache-through) and reduced per domain. Missing fields -> None so
    a partially-published member degrades gracefully (NFR-6). The apcp-over-LAoC reuses the
    watershed value when the two domains coincide (no Lightning Area of Concern set).

    When ``use_pool`` + ``crop_bbox`` are set the decode is cropped in the pool worker, so the
    returned field is already in the polygon frame and is reduced via :func:`_poly_max_precropped`
    (no re-crop); otherwise the legacy in-process decode + per-domain :func:`_poly_max` is used.
    """
    start = max(fhour - GEFS_STEP_H, 0)
    apcp_fcst = f"{start}-{fhour} hour acc"
    cape_fcst = f"{fhour} hour fcst"
    same_domain = ltng_polygon is polygon
    pmax = _poly_max_precropped if (use_pool and crop_bbox is not None) else _poly_max

    apcp_flood = apcp_ltng = cape_ltng = None
    try:
        af = load_member_field_cached(
            cycle, member, fhour, PRECIP_VAR, apcp_fcst, PRECIP_LEVEL,
            settings=settings, crop_bbox=crop_bbox, use_pool=use_pool,
        )
        apcp_flood = pmax(af, polygon, PRECIP_VAR)
        apcp_ltng = apcp_flood if same_domain else pmax(af, ltng_polygon, PRECIP_VAR)
    except LookupError:
        pass
    try:
        cf = load_member_field_cached(
            cycle, member, fhour, CAPE_VAR, cape_fcst, CAPE_LEVEL,
            settings=settings, crop_bbox=crop_bbox, use_pool=use_pool,
        )
        cape_ltng = pmax(cf, ltng_polygon, CAPE_VAR)
    except LookupError:
        pass
    return apcp_flood, apcp_ltng, cape_ltng


def _resolve_cycle(cycle, *, settings=None):
    """Pick the GEFS cycle to read: freshest warmed-in-cache, else a live NOMADS probe."""
    if cycle is not None:
        return cycle
    cached = cached_cycles(settings=settings)
    if cached:
        return cached[0]
    return latest_available_cycle()


def fetch(
    mission: Mission,
    bundle: IngestBundle,
    polygon: BaseGeometry,
    *,
    lightning_polygon: BaseGeometry | None = None,
    cycle=None,
    settings: Settings | None = None,
) -> None:
    """Populate GEFS exceedance probabilities + the derived lightning proxy over the domain.

    Flash-flood precip exceedance aggregates over ``polygon`` (the upstream watershed/RoC); the
    lightning proxy aggregates over ``lightning_polygon`` (the Lightning Area of Concern), which
    defaults to ``polygon``. Member fetches for every (fhour, member) are fanned across a thread
    pool; the conservative max exceedance across the window's forecast hours is taken (the worst
    case any covered hour shows), mirroring the REFS aggregation.
    """
    settings = settings or get_settings()
    cycle = _resolve_cycle(cycle, settings=settings)
    if cycle is None:
        bundle.sources_ok[NAME] = False
        bundle.notes.append("GEFS: no available cycle on NOMADS (retention/lag).")
        return
    ltng_polygon = lightning_polygon if lightning_polygon is not None else polygon

    fhours = _select_fhours(cycle, mission.window_start, mission.window_end)

    # When a decode pool is installed (API), decode each member in a worker process and crop there
    # to the union of the watershed + LAoC bboxes — true-parallel decode with a ~KB result instead
    # of the 16.5 MB global grid. With no pool (CLI/tests) this stays the in-process path.
    use_pool = decode_pool_enabled()
    crop_bbox = _union_bounds(polygon, ltng_polygon) if use_pool else None

    # Fan (fhour, member) member fetches across a thread pool; network + aggregation run
    # concurrently (Spike F: keeps the 31-member fetch in budget), decode runs in the pool above
    # (or serialized in-process when no pool is installed).
    tasks = [(f, m) for f in fhours for m in MEMBERS]
    sample_t = tuple[float | None, float | None, float | None]
    samples: dict[int, list[sample_t]] = {f: [] for f in fhours}
    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(
                    _member_sample, cycle, m, f, polygon, ltng_polygon,
                    settings=settings, crop_bbox=crop_bbox, use_pool=use_pool,
                ): f
                for f, m in tasks
            }
            for fut, f in futures.items():
                samples[f].append(fut.result())
    except (TimeoutError, requests.RequestException, OSError) as exc:
        bundle.sources_ok[NAME] = False
        bundle.notes.append(f"GEFS: member fetch failed ({exc}); ensemble unavailable.")
        return

    # Per forecast hour: member-exceedance fraction (percent). Conservative max across the window.
    precip_pcts: list[float] = []
    proxy_pcts: list[float] = []
    for f in fhours:
        rows = samples[f]
        apcp_flood = [a for a, _, _ in rows if a is not None]
        paired = [(al, c) for _, al, c in rows if al is not None and c is not None]
        if apcp_flood:
            precip_pcts.append(
                100.0 * sum(a > PRECIP_THRESH_MM for a in apcp_flood) / len(apcp_flood)
            )
        if paired:
            proxy_pcts.append(
                100.0
                * sum(a > PROXY_PRECIP_MM and c > PROXY_CAPE_JKG for a, c in paired)
                / len(paired)
            )

    gefs_precip = max(precip_pcts, default=None)
    gefs_tstm = max(proxy_pcts, default=None)
    if gefs_precip is None and gefs_tstm is None:
        bundle.sources_ok[NAME] = False
        bundle.notes.append("GEFS: no member fields available over the domain for this window.")
        return

    bundle.gefs_p_precip = gefs_precip
    bundle.gefs_p_tstm = gefs_tstm
    # Exceedance fraction doubles as member support for the confidence qualifier (§16.5). REFS
    # wins in-window at the orchestrator merge; GEFS support carries beyond REFS range.
    if gefs_precip is not None:
        bundle.member_support["flash_flood"] = gefs_precip / 100.0
    if gefs_tstm is not None:
        bundle.member_support["lightning"] = gefs_tstm / 100.0

    fh = f"f{min(fhours):03d}" if len(fhours) == 1 else f"f{min(fhours):03d}-f{max(fhours):03d}"
    bundle.notes.append(
        f"GEFS cycle {cycle.date}/{cycle.hh}Z {fh}; member-exceedance P(precip) and a "
        "CAPE×precip lightning proxy over the upstream domain (~0.25° global ensemble)."
    )
    bundle.sources_ok[NAME] = True
