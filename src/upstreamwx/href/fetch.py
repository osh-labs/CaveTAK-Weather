"""GRIB2 ``.idx`` byte-range subsetting for HREF (re-export of the shared primitive).

Identical machinery to SREF — lives in :mod:`upstreamwx.grib.idx` and is shared.
This module mirrors the ``upstreamwx.sref.fetch`` import surface for the HREF side.
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
