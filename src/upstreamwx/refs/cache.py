"""Persistent, cycle-and-hour-keyed on-disk cache for REFS probability subsets.

The REFS analogue of :mod:`upstreamwx.href.cache`, over the same shared primitive
(:mod:`upstreamwx.grib.cache`). REFS differs from HREF in two ways that shape the cache:

* **4 runs/day** (00/06/12/18Z) on a **3-hourly forecast cadence** (f03-f48 every 3 h, then
  to f60 every 6 h; :data:`upstreamwx.refs.sources.REFS_FHOURS`), vs HREF's two hourly runs.
* The scheduler warms the available forecast hours of each run and keeps several recent runs,
  so a current run still spinning up for a valid time is served from the *previous* run's
  mature forecast (see :mod:`upstreamwx.ingest.refs_selection`).

The cache key ``(cycle, fhour, var, prob)`` deliberately omits the accumulation-window
(``fcst``) descriptor: the request path couples each APCP threshold 1:1 with a window, so
``(var, prob)`` alone disambiguates the message at a given ``fhour`` (:data:`DEFAULT_FIELDS`
asserts uniqueness). The cached artifact is the byte-range subset ``.grib2`` itself, re-decoded
on a hit (bit-identical to the live decode), written atomically (NFR-6).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import requests
import xarray as xr

from ..config import Settings, get_settings
from ..grib.cache import cached_subset, decode_cached, prune_cycle_dirs
from .extract import RefsField, _primary_dataarray, accum_window, open_subset
from .fetch import download_subset, fetch_idx, select_messages
from .sources import DEFAULT_DOMAIN, DEFAULT_PRODUCT, REFS_FHOURS, RefsCycle, refs_feed

logger = logging.getLogger("upstreamwx.refs.cache")


@dataclass(frozen=True)
class FieldSpec:
    """One REFS field to warm: variable, probability threshold, accumulation window.

    ``window_h`` is the accumulation length in hours for QPF fields (the ``fcst`` window is
    derived per forecast hour via :func:`accum_window`); ``0`` means an instantaneous field
    (REFC, LTNG) whose ``fcst`` is left ``None``.
    """

    var: str
    prob: str
    window_h: int


# The fields the request path aggregates (see :mod:`upstreamwx.ingest.refs_provider`): the two
# flash-flood QPF thresholds (each coupled 1:1 with its window), the explicit lightning NEP,
# and the reflectivity fallback — all warmed so the provider's fallback is cache-served.
DEFAULT_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("APCP", ">12.7", 1),  # >=0.5 in / 1 h
    FieldSpec("APCP", ">25.4", 3),  # >=1 in / 3 h
    FieldSpec("LTNG", ">0.08", 0),  # neighborhood P(lightning) — REFS threshold (HREF used 0.2)
    FieldSpec("REFC", ">40", 0),  # composite reflectivity >=40 dBZ (lightning fallback)
)

# The cache key is (cycle, fhour, var, prob); omitting the accum window is only safe while each
# (var, prob) maps to exactly one window. Fail loud if that coupling is ever broken.
assert len({(f.var, f.prob) for f in DEFAULT_FIELDS}) == len(DEFAULT_FIELDS), (
    "REFS DEFAULT_FIELDS must have unique (var, prob): the cache key omits the accum window"
)


def _cache_dir(settings: Settings) -> Path:
    d = settings.data_dir / "refs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cycle_dir(settings: Settings, cycle: RefsCycle) -> Path:
    d = _cache_dir(settings) / f"{cycle.date}_{cycle.hh}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _subset_name(fhour: int, var: str, prob: str) -> str:
    """Sanitized per-hour subset filename (matches the scheme in :mod:`upstreamwx.refs.extract`)."""
    return (
        f"f{fhour:02d}_{var}_{prob}.grib2".replace(" ", "").replace(">", "gt").replace("<", "lt")
    )


def _decode(path: Path) -> xr.DataArray:
    """Decode a cached per-hour subset to its primary DataArray, eagerly loaded into memory.

    ``.load()`` materialises the grid into numpy, then the cfgrib ``Dataset`` is **closed
    explicitly** so the eccodes file handle is released here — while the caller still holds
    ``grib.cache._decode_compute_lock`` around this decode. Letting the ``Dataset`` fall out of
    scope unclosed instead defers the handle teardown to a later garbage-collection on an
    arbitrary thread, which can run concurrently with another in-lock decode; eccodes is not
    thread-safe, and that cross-thread teardown segfaults the worker once REFS decodes alongside
    GEFS's thread pool. Closing in-lock keeps all eccodes activity serialised (NFR-6).
    """
    with open_subset(path) as ds:
        return _primary_dataarray(ds).load()


def load_probability_field_cached(
    cycle: RefsCycle,
    fhour: int,
    var: str,
    prob: str,
    fcst: str | None = None,
    domain: str = DEFAULT_DOMAIN,
    product: str = DEFAULT_PRODUCT,
    *,
    settings: Settings | None = None,
    refresh: bool = False,
) -> RefsField:
    """Load one REFS neighborhood-probability field, using the persistent cache when present.

    On a hit the cached per-hour subset is re-decoded with cfgrib (identical to the live decode,
    so the aggregate is unchanged). On a miss the byte-range subset is fetched and written
    atomically under the cycle dir, then decoded. ``fcst`` narrows an accumulation window for
    QPF (see :func:`accum_window`); leave ``None`` for instantaneous fields. Signature mirrors
    :func:`upstreamwx.refs.extract.load_probability_field` plus ``settings``/``refresh``.
    """
    settings = settings or get_settings()
    path = _cycle_dir(settings, cycle) / _subset_name(fhour, var, prob)
    # Resolve the active feed (AWS / NOMADS) once so the cache write and any live probe agree.
    base, subdir = refs_feed(settings)

    path, selected = cached_subset(
        path,
        idx_url=cycle.idx_url(fhour, product=product, domain=domain, base=base, subdir=subdir),
        grib_url=cycle.product_url(fhour, product=product, domain=domain, base=base, subdir=subdir),
        select=lambda entries: select_messages(entries, var=var, prob=prob, fcst=fcst),
        refresh=refresh,
        what=f"f{fhour:02d} var={var!r} prob={prob!r} fcst={fcst!r}",
        # Pass this module's (patchable) network calls so tests patching
        # ``upstreamwx.refs.cache.*`` still intercept.
        fetch_idx=fetch_idx,
        download_subset=download_subset,
    )

    da = decode_cached(path, _decode)
    return RefsField(
        name=var,
        threshold=prob,
        fhour=fhour,
        data=da,
        grib_path=path,
        # Miss -> the network message count; hit (selected is None) -> a single valid-time
        # message (REFS per-hour files hold one), so the stacked step dim (or 1).
        descriptor_count=len(selected) if selected is not None else int(da.sizes.get("step", 1)),
        extras={"domain": domain, "product": product, "fcst": fcst, "cached": selected is None},
    )


def warm_cycle(
    cycle: RefsCycle,
    *,
    settings: Settings | None = None,
    fmin: int = 3,
    fmax: int = 48,
    fields: tuple[FieldSpec, ...] = DEFAULT_FIELDS,
) -> list[Path]:
    """Pre-pull a cycle's available f``fmin``-f``fmax`` CONUS subsets into the cache.

    Iterates only REFS's published forecast hours (:data:`REFS_FHOURS`, 3-hourly) within the
    band — REFS has no hourly files. Idempotent: already-cached fields are re-decoded, not
    re-downloaded. A field unavailable at a given hour (no matching message, or that hour not
    yet published) is skipped so a partially-published run still warms what it can (NFR-6).
    Returns the cached subset paths.
    """
    settings = settings or get_settings()
    paths: list[Path] = []
    for fhour in REFS_FHOURS:
        if not fmin <= fhour <= fmax:
            continue
        for spec in fields:
            fcst = accum_window(fhour, spec.window_h) if spec.window_h else None
            try:
                field = load_probability_field_cached(
                    cycle, fhour, spec.var, spec.prob, fcst=fcst, settings=settings
                )
            except (
                LookupError, ValueError, TimeoutError, OSError, requests.RequestException
            ) as exc:
                # ValueError covers TruncatedGribError (a subset fetched mid-publish); skip it and
                # let the next tick re-fetch clean rather than sink the warm pass (NFR-6).
                logger.debug("REFS warm skipped f%02d %s %s: %s", fhour, spec.var, spec.prob, exc)
                continue
            paths.append(field.grib_path)
    return paths


def prune_old_cycles(*, settings: Settings | None = None, keep: int = 3) -> list[Path]:
    """Delete cached cycle dirs beyond the newest ``keep``.

    ``keep`` is in REFS *runs* (00/06/12/18Z). The default of 3 guarantees the previous run is
    present to backfill the current run's spin-up even on a missed tick or late publish. Returns
    the removed dirs.
    """
    settings = settings or get_settings()
    return prune_cycle_dirs(_cache_dir(settings), keep=keep)
