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

import os
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .idx import IdxEntry
from .idx import download_subset as _default_download_subset
from .idx import fetch_idx as _default_fetch_idx


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
# Bounds resident decoded grids; tune via the constant if memory headroom changes. SREF
# CONUS subsets are the heavy entries (~tens of MB each); HREF per-hour subsets are small.
_DECODE_CACHE_MAX = 48
_decoded: OrderedDict[tuple[str, int, int], Any] = OrderedDict()
_decoded_lock = threading.Lock()  # guards the LRU dict (fast)
_decode_compute_lock = threading.Lock()  # serialises the cfgrib decode itself


def decode_cached(path: Path, decode: Callable[[Path], Any]) -> Any:
    """Return the decoded grid for a cached subset file, memoised in-process.

    Without this the byte-range subset on disk is re-decoded with cfgrib on *every* cache hit
    (eccodes parse + xarray dataset build), which dominates the warm request path once the
    download itself is cached — and HREF reads many per-hour files per briefing (roadmap
    §M0.1.1 decoded-grid cache). The decoded grid depends only on the immutable subset file
    and is independent of the aggregation polygon, so we memoise it per ``(path, mtime, size)``
    behind a bounded LRU. ``refresh`` rewrites the file with a new mtime, so it misses the memo
    and re-decodes as intended.

    Thread-safe and decode-serialised: the orchestrator now decodes SREF and HREF concurrently,
    and cfgrib/eccodes is not reliably thread-safe, so a miss takes ``_decode_compute_lock``
    around the decode (re-checking the memo inside, which also dedupes a concurrent miss on the
    same key). Memo *hits* never take that lock, and the network fetch and numpy/regionmask
    aggregation around this call stay fully concurrent — that is where the latency is — so
    serialising only the decode costs nothing material.
    """
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    with _decoded_lock:
        hit = _decoded.get(key)
        if hit is not None:
            _decoded.move_to_end(key)
            return hit
    with _decode_compute_lock:
        # Another thread may have decoded this key while we waited for the lock.
        with _decoded_lock:
            hit = _decoded.get(key)
            if hit is not None:
                _decoded.move_to_end(key)
                return hit
        decoded = decode(path)
        with _decoded_lock:
            _decoded[key] = decoded
            _decoded.move_to_end(key)
            while len(_decoded) > _DECODE_CACHE_MAX:
                _decoded.popitem(last=False)
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
