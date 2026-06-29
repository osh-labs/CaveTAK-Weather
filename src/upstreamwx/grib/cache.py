"""Shared persistent on-disk cache primitive for GRIB2 byte-range subsets (roadmap §M0.1.1).

Both ensembles fetch the same way — pull a ``.idx`` sidecar, select the handful of
messages we need, HTTP-range those byte spans into a small ``.grib2`` — and both want the
result persisted so a cycle's CONUS subset is fetched once and reused by later requests and
after a restart (PRD §7, §11.2, FR-7, FR-12). SREF (one multi-window file per cycle) and
HREF (one file per forecast hour) differ only in URL construction, the message selector, the
on-disk path layout, and the decoded wrapper type. This module owns the part that is
identical: the atomic fetch-or-hit and the retention prune. :mod:`upstreamwx.sref.cache` and
:mod:`upstreamwx.href.cache` supply the rest.

The cached artifact is the byte-range subset ``.grib2`` itself, re-decoded with cfgrib on a
hit, so the on-demand output is bit-for-bit identical to the live path and needs no new
serialization. Writes are atomic (temp file + ``os.replace``) so a partial or failed
download (NFR-6 degradation) never leaves a poisoned entry and a concurrent reader sees
either no file or the complete one.

Patch-target stability: the two network calls (``fetch_idx``, ``download_subset``) are
injected by the caller so each cache module's tests can patch them on *their own* module
(``upstreamwx.sref.cache.*`` / ``upstreamwx.href.cache.*``) and still intercept. Message
selection is supplied as a ``select`` closure the caller composes from its own
(patchable) ``select_messages``.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Hashable
from concurrent.futures import Executor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any

from .idx import IdxEntry
from .idx import download_subset as _default_download_subset
from .idx import fetch_idx as _default_fetch_idx

logger = logging.getLogger("upstreamwx.grib.cache")


def cached_subset(
    path: Path,
    *,
    idx_url: str,
    grib_url: str,
    select: Callable[[list[IdxEntry]], list[IdxEntry]],
    refresh: bool = False,
    what: str = "",
    fetch_idx: Callable[[str], list[IdxEntry]] = _default_fetch_idx,
    download_subset: Callable[..., Path] = _default_download_subset,
) -> tuple[Path, list[IdxEntry] | None]:
    """Materialize the byte-range subset at ``path``; return ``(path, selected)``.

    On a hit (``path`` exists and not ``refresh``) returns ``(path, None)`` with no network —
    a ``None`` selected list signals "served from cache". On a miss fetches the ``.idx``,
    applies ``select``, atomically downloads the selected messages (temp file + ``os.replace``,
    with a ``finally`` unlink so a failed download leaves no temp), and returns
    ``(path, selected)`` so the caller can record the message count. Raises ``LookupError``
    when ``select`` yields no messages (``what`` is folded in for a useful diagnostic).
    """
    if path.is_file() and not refresh:
        return path, None

    selected = select(fetch_idx(idx_url))
    if not selected:
        detail = f" ({what})" if what else ""
        raise LookupError(f"No GRIB messages selected from {idx_url}{detail}")

    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: download to a unique temp file in the same dir, then os.replace into
    # place so a concurrent reader never sees a partial file and a failed download leaves no
    # poisoned entry (NFR-6).
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        download_subset(grib_url, selected, tmp)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return path, selected


# In-process LRU of decoded grids, keyed by the immutable subset file (roadmap §M0.1.1).
# Eviction is memory-budget-based with a count backstop: entry sizes are now very mixed —
# GEFS member grids cropped in the decode pool are ~KB while REFS/SREF native grids are
# multi-MB — so a flat count cap either thrashes (too low for a GEFS briefing's ~500 cropped
# entries) or risks OOM (too high once a few MB-scale grids land). We instead cap the *bytes*
# resident and keep a generous count backstop. ``_DECODE_CACHE_MAX_BYTES`` is overwritten from
# settings by the API at startup (:func:`upstreamwx.config.Settings.decode_cache_max_bytes`).
_DECODE_CACHE_MAX = 1024  # count backstop (governs tiny/non-array entries, e.g. tests)
_DECODE_CACHE_MAX_BYTES = 512 * 1024 * 1024  # ~512 MiB resident decoded-grid budget
_NOMINAL_ENTRY_BYTES = 1  # size charged to a non-array entry (no ``nbytes``)
_decoded: OrderedDict[tuple[str, int, int, Hashable], Any] = OrderedDict()
_decoded_sizes: dict[tuple[str, int, int, Hashable], int] = {}  # per-entry byte charge
_decoded_bytes = 0  # running sum of _decoded_sizes.values()
_decoded_lock = threading.Lock()  # guards the LRU dict + size bookkeeping (fast)
_decode_compute_lock = threading.Lock()  # serialises the *in-process* cfgrib decode

# Out-of-process decode pool (spawn), owned by the API lifespan. eccodes is not thread-safe,
# so the in-process path above serialises every decode on ``_decode_compute_lock``; routing the
# (heavy, per-member) GEFS decodes to a process pool lets them run truly in parallel — each
# worker is its own single-threaded interpreter, so no cross-thread eccodes hazard. The pool is
# absent in the CLI and the test suite (they never run the lifespan), so those keep the
# in-process path unchanged. See :func:`set_decode_pool`.
_decode_pool: Executor | None = None
_decode_pool_lock = threading.Lock()  # guards the pool reference only


def set_decode_pool(pool: Executor | None) -> None:
    """Install the process pool used for out-of-process GRIB decode (API lifespan)."""
    global _decode_pool
    with _decode_pool_lock:
        _decode_pool = pool


def shutdown_decode_pool(wait: bool = False) -> None:
    """Detach and shut down the decode pool (idempotent; lifespan teardown / broken-pool)."""
    global _decode_pool
    with _decode_pool_lock:
        pool, _decode_pool = _decode_pool, None
    if pool is not None:
        pool.shutdown(wait=wait, cancel_futures=True)


def decode_pool_enabled() -> bool:
    """True when a decode pool is installed (callers gate ``use_pool`` on this)."""
    with _decode_pool_lock:
        return _decode_pool is not None


def configure_decode_cache(
    *, max_bytes: int | None = None, max_entries: int | None = None
) -> None:
    """Apply runtime limits to the decoded-grid LRU (called from the API lifespan)."""
    global _DECODE_CACHE_MAX_BYTES, _DECODE_CACHE_MAX
    if max_bytes is not None:
        _DECODE_CACHE_MAX_BYTES = max_bytes
    if max_entries is not None:
        _DECODE_CACHE_MAX = max_entries


def _entry_size(obj: Any) -> int:
    """Byte charge for a cache entry: a grid's ``nbytes``, else a nominal 1 (test stand-ins)."""
    n = getattr(obj, "nbytes", None)
    return int(n) if isinstance(n, int) else _NOMINAL_ENTRY_BYTES


def _store(key: tuple[str, int, int, Hashable], decoded: Any) -> None:
    """Insert ``decoded`` under ``key`` and evict oldest until under both caps (LRU)."""
    global _decoded_bytes
    size = _entry_size(decoded)
    with _decoded_lock:
        if key in _decoded:  # overwrite: drop the old charge first
            _decoded_bytes -= _decoded_sizes.pop(key, 0)
            del _decoded[key]
        _decoded[key] = decoded
        _decoded_sizes[key] = size
        _decoded_bytes += size
        _decoded.move_to_end(key)
        # Keep at least the just-inserted entry even if it alone exceeds the byte budget
        # (otherwise an oversized grid would never be cached and re-decode every call).
        while len(_decoded) > 1 and (
            len(_decoded) > _DECODE_CACHE_MAX or _decoded_bytes > _DECODE_CACHE_MAX_BYTES
        ):
            old_key, _ = _decoded.popitem(last=False)
            _decoded_bytes -= _decoded_sizes.pop(old_key, 0)


def _clear_decoded() -> None:
    """Reset the decoded-grid memo and its size bookkeeping (used by tests)."""
    global _decoded_bytes
    with _decoded_lock:
        _decoded.clear()
        _decoded_sizes.clear()
        _decoded_bytes = 0


def _decode_in_process(
    key: tuple[str, int, int, Hashable], path: Path, decode: Callable[[Path], Any]
) -> Any:
    """Decode under the global compute lock (eccodes-serialised), dedupe + memoise."""
    with _decode_compute_lock:
        # Another thread may have decoded this key while we waited for the lock.
        with _decoded_lock:
            hit = _decoded.get(key)
            if hit is not None:
                _decoded.move_to_end(key)
                return hit
        decoded = decode(path)
        _store(key, decoded)
    return decoded


def decode_cached(
    path: Path,
    decode: Callable[[Path], Any],
    *,
    use_pool: bool = False,
    key_extra: Hashable = None,
) -> Any:
    """Return the decoded grid for a cached subset file, memoised in-process.

    Without this the byte-range subset on disk is re-decoded with cfgrib on *every* cache hit
    (eccodes parse + xarray dataset build), which dominates the warm request path once the
    download itself is cached — and REFS reads many per-hour files per briefing (roadmap
    §M0.1.1 decoded-grid cache). The decoded grid depends only on the immutable subset file
    (and, when the caller pre-crops in the decode worker, the crop ``key_extra``), so we
    memoise it per ``(path, mtime, size, key_extra)`` behind a bounded LRU. ``refresh`` rewrites
    the file with a new mtime, so it misses the memo and re-decodes as intended.

    ``use_pool`` routes the decode to the out-of-process decode pool when one is installed
    (:func:`set_decode_pool`). Cross-process decode is eccodes-safe, so that path does **not**
    take ``_decode_compute_lock`` — the GEFS member fetches then decode truly in parallel. With
    no pool installed (CLI, tests) it falls through to the in-process, lock-serialised path,
    which is byte-for-byte the previous behaviour. ``decode`` must be picklable to use the pool
    (a module-level function or a ``functools.partial`` of one); a closure silently can't, but
    such callers never pass ``use_pool=True``.

    ``key_extra`` is folded into the memo key so two crops of the same file (different bbox) are
    distinct entries; it is ``None`` for every legacy caller, leaving their keys unchanged.
    """
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size, key_extra)
    with _decoded_lock:
        hit = _decoded.get(key)
        if hit is not None:
            _decoded.move_to_end(key)
            return hit

    pool = None
    if use_pool:
        with _decode_pool_lock:
            pool = _decode_pool

    if pool is None:
        return _decode_in_process(key, path, decode)

    # Pool path: no compute lock (each worker is its own interpreter). A concurrent miss on the
    # same key may double-decode once — harmless and rare; the LRU store stays guarded.
    try:
        decoded = pool.submit(decode, path).result()
    except BrokenProcessPool:
        # NFR-6 degrade: a crashed worker must not sink the briefing. Drop the broken pool and
        # fall back to the in-process decode for this and subsequent calls (until restart).
        logger.warning("decode pool broken; falling back to in-process decode", exc_info=True)
        shutdown_decode_pool(wait=False)
        return _decode_in_process(key, path, decode)
    _store(key, decoded)
    return decoded


def prune_cycle_dirs(root: Path, *, keep: int) -> list[Path]:
    """Delete cycle subdirs of ``root`` beyond the newest ``keep`` (roadmap §M0.1.1).

    Cycle dir names sort lexicographically by ``YYYYMMDD_HH``, so the newest are the
    largest. Bounds disk use to NOMADS's ~2-day retention horizon. Returns the removed dirs.
    """
    if not root.is_dir():
        return []
    cycle_dirs = sorted((d for d in root.iterdir() if d.is_dir()), reverse=True)
    removed: list[Path] = []
    for d in cycle_dirs[keep:]:
        for f in d.iterdir():
            f.unlink()
        d.rmdir()
        removed.append(d)
    return removed
