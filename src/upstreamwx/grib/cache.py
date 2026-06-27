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
import uuid
from collections.abc import Callable
from pathlib import Path

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
