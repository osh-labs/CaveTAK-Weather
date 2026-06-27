"""Persistent, cycle-keyed on-disk cache for SREF probability subsets (roadmap §M0.1.1).

The PRD's SREF processor is a *scheduled* backend that pulls each cycle once and serves
every active domain from the cached grid (PRD §7, §11.2, FR-7, FR-12). The M0.1 on-demand
path (:mod:`upstreamwx.sref.extract`) re-downloads a byte-range subset on every call into a
throwaway tempdir, so nothing survives within a process — let alone across the restart the
always-on EC2 host must tolerate. This module is the deferred persistence layer: it mirrors
the watershed on-disk cache (:mod:`upstreamwx.watershed.cache`), keying decoded SREF grids
by cycle under ``Settings.data_dir / "sref"`` so a cycle's CONUS subset is fetched once and
reused — by later requests *and* after a restart — while NOMADS still retains it (~2 days,
see :mod:`upstreamwx.sref.sources`).

What is cached is the byte-range subset ``.grib2`` itself, re-decoded with cfgrib on a hit.
That keeps the on-demand output bit-for-bit identical (the decode is the same one the live
path runs) and needs no new serialization. Writes are atomic (temp file + ``os.replace``)
so a partial or failed download (NFR-6 degradation) never leaves a poisoned cache entry and
a concurrent reader sees either no file or the complete one.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings, get_settings
from ..grib.cache import cached_subset, prune_cycle_dirs
from .extract import SrefField, _primary_dataarray, open_subset
from .fetch import download_subset, fetch_idx, select_messages
from .sources import SrefCycle

# The (var, prob, freq) fields the request path aggregates (see
# :mod:`upstreamwx.ingest.sref_provider`): P(APCP>6.35 mm/3h) precip proxy and
# P(CAPE>1000 J/kg) thunderstorm proxy. Both live in the 3hrly ``prob`` product. Kept here
# so ``warm_cycle`` pre-pulls exactly what the request path will read (they stay in lockstep).
DEFAULT_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("APCP", ">6.35", "3hrly"),
    ("CAPE", ">1000", "3hrly"),
)


def _cache_dir(settings: Settings) -> Path:
    d = settings.data_dir / "sref"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cycle_dir(settings: Settings, cycle: SrefCycle) -> Path:
    d = _cache_dir(settings) / f"{cycle.date}_{cycle.hh}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _subset_name(var: str, prob: str, freq: str) -> str:
    """Sanitized subset filename, matching the scheme in :mod:`upstreamwx.sref.extract`."""
    return f"{var}_{prob}_{freq}.grib2".replace(" ", "").replace(">", "gt").replace("<", "lt")


def load_probability_field_cached(
    cycle: SrefCycle,
    var: str,
    prob: str,
    freq: str = "3hrly",
    grid: str = "pgrb132",
    fcst: str | None = None,
    *,
    settings: Settings | None = None,
    refresh: bool = False,
) -> SrefField:
    """Load one SREF probability field, using the persistent cycle cache when present.

    On a hit the cached subset is re-decoded with cfgrib (identical to the live decode, so
    the aggregate is unchanged). On a miss the byte-range subset is fetched and written
    atomically (temp file + ``os.replace``) under the cycle dir, then decoded. ``refresh``
    forces a re-fetch. Signature mirrors :func:`upstreamwx.sref.extract.load_probability_field`
    plus ``settings``/``refresh`` (roadmap §M0.1.1, FR-7, FR-12).
    """
    settings = settings or get_settings()
    path = _cycle_dir(settings, cycle) / _subset_name(var, prob, freq)

    path, selected = cached_subset(
        path,
        idx_url=cycle.idx_url(product="prob", grid=grid, freq=freq),
        grib_url=cycle.product_url(product="prob", grid=grid, freq=freq),
        select=lambda entries: select_messages(entries, var=var, prob=prob, fcst=fcst),
        refresh=refresh,
        what=f"var={var!r} prob={prob!r} fcst={fcst!r}",
        # Pass this module's (patchable) network calls so tests patching
        # ``upstreamwx.sref.cache.*`` still intercept (roadmap §M0.1.1).
        fetch_idx=fetch_idx,
        download_subset=download_subset,
    )

    da = _primary_dataarray(open_subset(path))
    return SrefField(
        name=var,
        threshold=prob,
        data=da,
        grib_path=path,
        # Miss -> the network message count; hit (selected is None) -> the per-window count,
        # which equals the stacked step dimension for these single-threshold fields.
        descriptor_count=len(selected) if selected is not None else int(da.sizes.get("step", 1)),
        extras={"freq": freq, "grid": grid, "fcst": fcst, "cached": selected is None},
    )


def warm_cycle(
    cycle: SrefCycle,
    *,
    settings: Settings | None = None,
    fields: tuple[tuple[str, str, str], ...] = DEFAULT_FIELDS,
) -> list[Path]:
    """Pre-pull a cycle's CONUS subsets into the cache (roadmap §M0.1.1).

    Idempotent: fields already cached are re-decoded, not re-downloaded. Returns the cached
    subset paths. Called by the scheduler each cycle so a domain request never pays the
    download (the "download once per cycle, aggregate every domain" pattern).
    """
    settings = settings or get_settings()
    return [
        load_probability_field_cached(cycle, var, prob, freq, settings=settings).grib_path
        for var, prob, freq in fields
    ]


def prune_old_cycles(*, settings: Settings | None = None, keep: int = 4) -> list[Path]:
    """Delete cached cycle dirs beyond the newest ``keep`` (roadmap §M0.1.1).

    Bounds disk use to NOMADS's ~2-day retention horizon. Cycle dir names sort
    lexicographically by ``YYYYMMDD_HH``, so the newest are simply the largest. Returns the
    removed dirs.
    """
    settings = settings or get_settings()
    return prune_cycle_dirs(_cache_dir(settings), keep=keep)
