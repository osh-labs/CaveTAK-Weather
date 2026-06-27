"""Persistent, cycle-and-hour-keyed on-disk cache for HREF probability subsets (roadmap §M0.1.1).

The HREF analogue of :mod:`upstreamwx.sref.cache`, over the same shared primitive
(:mod:`upstreamwx.grib.cache`). HREF differs from SREF in two ways that shape the cache:

* **One file per forecast hour** (f01-f48), not one multi-window file per cycle — so the
  cache key gains an ``fhour`` axis: ``(cycle, fhour, var, prob)``.
* **Two runs/day** (00/12Z). The scheduler warms **f06-f48** of each run and keeps several
  recent runs, so the current run's spin-up hours (f01-f05) are served from the *previous*
  run's mature forecast for the same valid time (see :mod:`upstreamwx.ingest.href_selection`).

The cache key deliberately omits the accumulation-window (``fcst``) descriptor: the request
path couples each APCP threshold 1:1 with a window (``>12.7`` = 1 h, ``>25.4`` = 3 h, per
:mod:`upstreamwx.ingest.href_provider`), so ``(var, prob)`` alone disambiguates the message
at a given ``fhour``. ``fcst`` is still passed to the selector so only that one window's
message is written to the file. :data:`DEFAULT_FIELDS` asserts the ``(var, prob)`` pairs are
unique so a future field that reuses a prob across windows fails loudly instead of colliding.

As with SREF, the cached artifact is the byte-range subset ``.grib2`` itself, re-decoded on a
hit (bit-identical to the live decode), written atomically (NFR-6).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import requests

from ..config import Settings, get_settings
from ..grib.cache import cached_subset, prune_cycle_dirs
from .extract import HrefField, _primary_dataarray, accum_window, open_subset
from .fetch import download_subset, fetch_idx, select_messages
from .sources import DEFAULT_DOMAIN, DEFAULT_PRODUCT, HrefCycle

logger = logging.getLogger("upstreamwx.href.cache")


@dataclass(frozen=True)
class FieldSpec:
    """One HREF field to warm: variable, probability threshold, accumulation window.

    ``window_h`` is the accumulation length in hours for QPF fields (the ``fcst`` window is
    derived per forecast hour via :func:`accum_window`); ``0`` means an instantaneous field
    (REFC, LTNG) whose ``fcst`` is left ``None``.
    """

    var: str
    prob: str
    window_h: int


# The fields the request path aggregates (see :mod:`upstreamwx.ingest.href_provider`): the
# two flash-flood QPF thresholds (each coupled 1:1 with its window), the explicit lightning
# NEP, and the reflectivity fallback — all warmed so the provider's fallback is cache-served.
DEFAULT_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("APCP", ">12.7", 1),  # >=0.5 in / 1 h
    FieldSpec("APCP", ">25.4", 3),  # >=1 in / 3 h
    FieldSpec("LTNG", ">0.2", 0),  # neighborhood P(lightning)
    FieldSpec("REFC", ">40", 0),  # composite reflectivity >=40 dBZ (lightning fallback)
)

# The cache key is (cycle, fhour, var, prob); omitting the accum window is only safe while
# each (var, prob) maps to exactly one window. Fail loud if that coupling is ever broken.
assert len({(f.var, f.prob) for f in DEFAULT_FIELDS}) == len(DEFAULT_FIELDS), (
    "HREF DEFAULT_FIELDS must have unique (var, prob): the cache key omits the accum window"
)


def _cache_dir(settings: Settings) -> Path:
    d = settings.data_dir / "href"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cycle_dir(settings: Settings, cycle: HrefCycle) -> Path:
    d = _cache_dir(settings) / f"{cycle.date}_{cycle.hh}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _subset_name(fhour: int, var: str, prob: str) -> str:
    """Sanitized per-hour subset filename (matches the scheme in :mod:`upstreamwx.href.extract`)."""
    return (
        f"f{fhour:02d}_{var}_{prob}.grib2".replace(" ", "").replace(">", "gt").replace("<", "lt")
    )


def load_probability_field_cached(
    cycle: HrefCycle,
    fhour: int,
    var: str,
    prob: str,
    fcst: str | None = None,
    domain: str = DEFAULT_DOMAIN,
    product: str = DEFAULT_PRODUCT,
    *,
    settings: Settings | None = None,
    refresh: bool = False,
) -> HrefField:
    """Load one HREF neighborhood-probability field, using the persistent cache when present.

    On a hit the cached per-hour subset is re-decoded with cfgrib (identical to the live
    decode, so the aggregate is unchanged). On a miss the byte-range subset is fetched and
    written atomically under the cycle dir, then decoded. ``fcst`` narrows an accumulation
    window for QPF (see :func:`accum_window`); leave ``None`` for instantaneous fields.
    Signature mirrors :func:`upstreamwx.href.extract.load_probability_field` plus
    ``settings``/``refresh`` (roadmap §M0.1.1, FR-7, FR-12).
    """
    settings = settings or get_settings()
    path = _cycle_dir(settings, cycle) / _subset_name(fhour, var, prob)

    path, selected = cached_subset(
        path,
        idx_url=cycle.idx_url(fhour, product=product, domain=domain),
        grib_url=cycle.product_url(fhour, product=product, domain=domain),
        select=lambda entries: select_messages(entries, var=var, prob=prob, fcst=fcst),
        refresh=refresh,
        what=f"f{fhour:02d} var={var!r} prob={prob!r} fcst={fcst!r}",
        # Pass this module's (patchable) network calls so tests patching
        # ``upstreamwx.href.cache.*`` still intercept (roadmap §M0.1.1).
        fetch_idx=fetch_idx,
        download_subset=download_subset,
    )

    da = _primary_dataarray(open_subset(path))
    return HrefField(
        name=var,
        threshold=prob,
        fhour=fhour,
        data=da,
        grib_path=path,
        # Miss -> the network message count; hit (selected is None) -> a single valid-time
        # message (HREF per-hour files hold one), so the stacked step dim (or 1).
        descriptor_count=len(selected) if selected is not None else int(da.sizes.get("step", 1)),
        extras={"domain": domain, "product": product, "fcst": fcst, "cached": selected is None},
    )


def warm_cycle(
    cycle: HrefCycle,
    *,
    settings: Settings | None = None,
    fmin: int = 6,
    fmax: int = 48,
    fields: tuple[FieldSpec, ...] = DEFAULT_FIELDS,
) -> list[Path]:
    """Pre-pull a cycle's f``fmin``-f``fmax`` CONUS subsets into the cache (roadmap §M0.1.1).

    Warms **f06-f48** by default (skipping the spin-up hours, which are backfilled from a
    prior run). Idempotent: already-cached fields are re-decoded, not re-downloaded. A field
    unavailable at a given hour (no matching message, or that hour not yet published) is
    skipped so a partially-published run still warms what it can (NFR-6). Returns the cached
    subset paths.
    """
    settings = settings or get_settings()
    paths: list[Path] = []
    for fhour in range(fmin, fmax + 1):
        for spec in fields:
            fcst = accum_window(fhour, spec.window_h) if spec.window_h else None
            try:
                field = load_probability_field_cached(
                    cycle, fhour, spec.var, spec.prob, fcst=fcst, settings=settings
                )
            except (LookupError, TimeoutError, OSError, requests.RequestException) as exc:
                logger.debug(
                    "HREF warm skipped f%02d %s %s: %s", fhour, spec.var, spec.prob, exc
                )
                continue
            paths.append(field.grib_path)
    return paths


def prune_old_cycles(*, settings: Settings | None = None, keep: int = 3) -> list[Path]:
    """Delete cached cycle dirs beyond the newest ``keep`` (roadmap §M0.1.1).

    ``keep`` is in HREF *runs* (00/12Z). The default of 3 guarantees the previous run is
    present to backfill the current run's spin-up even on a missed tick or late publish.
    Returns the removed dirs.
    """
    settings = settings or get_settings()
    return prune_cycle_dirs(_cache_dir(settings), keep=keep)
