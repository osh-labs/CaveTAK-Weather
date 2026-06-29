"""GRIB2 ``.idx`` byte-range subsetting for REFS (re-export of the shared primitive).

Identical machinery to SREF/HREF/GEFS — lives in :mod:`upstreamwx.grib.idx` and is shared.
This module mirrors the ``upstreamwx.href.fetch`` import surface for the REFS side.
"""

from __future__ import annotations

from ..grib.idx import (
    IdxEntry,
    download_subset,
    fetch_idx,
    parse_idx,
    select_messages,
)

__all__ = [
    "IdxEntry",
    "parse_idx",
    "fetch_idx",
    "select_messages",
    "download_subset",
]
