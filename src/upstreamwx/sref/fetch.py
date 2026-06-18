"""GRIB2 ``.idx`` byte-range subsetting for SREF (re-export of the shared primitive).

The idx parsing / message selection / range download logic is identical for any
NOMADS ``ensprod`` product, so it lives in :mod:`upstreamwx.grib.idx` and is shared
with the HREF processor. This module preserves the historical
``upstreamwx.sref.fetch`` import surface used by Spike A and its tests.
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
