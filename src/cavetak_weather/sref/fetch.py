"""GRIB2 retrieval via ``.idx`` byte-range subsetting (Spike A).

SREF ensprod files are 350-660 MB. Each ``.grib2`` has a ``.idx`` sidecar listing
every GRIB message's byte offset and a human-readable descriptor, e.g.::

    186:23632071:d=2026061715:APCP:surface:0-3 hour acc fcst:prob >12.7:prob fcst 0/26:...

We parse the index, select the handful of messages we need, and issue HTTP Range
requests for only those byte spans — turning a ~660 MB download into a few hundred
KB. The concatenated message bytes form a valid GRIB2 file readable by cfgrib.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import requests

from .sources import _HEADERS


@dataclass(frozen=True)
class IdxEntry:
    """One GRIB message as described by a ``.idx`` line."""

    num: int
    start: int
    descriptor: str  # full ":"-joined remainder (var:level:fcst:prob:...)
    end: int | None = None  # exclusive; None means "to end of file"

    @property
    def var(self) -> str:
        parts = self.descriptor.split(":")
        return parts[1] if len(parts) > 1 else ""

    @property
    def level(self) -> str:
        parts = self.descriptor.split(":")
        return parts[2] if len(parts) > 2 else ""

    @property
    def fcst(self) -> str:
        parts = self.descriptor.split(":")
        return parts[3] if len(parts) > 3 else ""

    @property
    def prob(self) -> str:
        parts = self.descriptor.split(":")
        return parts[4] if len(parts) > 4 else ""

    def range_header(self) -> str:
        if self.end is None:
            return f"bytes={self.start}-"
        return f"bytes={self.start}-{self.end - 1}"


def parse_idx(text: str) -> list[IdxEntry]:
    """Parse ``.idx`` text into entries with computed end offsets.

    Index line format: ``num:start_byte:d=YYYYMMDDHH:VAR:LEVEL:FCST:PROB:...``
    """
    raw: list[tuple[int, int, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = line.split(":")
        if len(fields) < 3:
            continue
        try:
            num = int(fields[0])
            start = int(fields[1])
        except ValueError:
            continue
        descriptor = ":".join(fields[2:])
        raw.append((num, start, descriptor))

    raw.sort(key=lambda r: r[1])  # by byte offset
    entries: list[IdxEntry] = []
    for i, (num, start, desc) in enumerate(raw):
        end = raw[i + 1][1] if i + 1 < len(raw) else None
        entries.append(IdxEntry(num=num, start=start, descriptor=desc, end=end))
    return entries


def fetch_idx(idx_url: str, timeout: float = 30.0) -> list[IdxEntry]:
    """Download and parse a ``.idx`` sidecar."""
    resp = requests.get(idx_url, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return parse_idx(resp.text)


def select_messages(
    entries: list[IdxEntry],
    var: str | None = None,
    level: str | None = None,
    fcst: str | None = None,
    prob: str | None = None,
) -> list[IdxEntry]:
    """Filter index entries by substring match on each field (case-insensitive)."""

    def match(value: str, needle: str | None) -> bool:
        return needle is None or needle.lower() in value.lower()

    return [
        e
        for e in entries
        if match(e.var, var)
        and match(e.level, level)
        and match(e.fcst, fcst)
        and match(e.prob, prob)
    ]


def download_subset(
    grib_url: str,
    selected: list[IdxEntry],
    out_path: str | Path,
    timeout: float = 120.0,
) -> Path:
    """Fetch the byte spans for ``selected`` messages and write a valid GRIB2 file.

    Returns the output path. Issues one Range request per message (NOMADS does not
    reliably support multipart ranges) and concatenates in index order.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as fh:
        for entry in sorted(selected, key=lambda e: e.start):
            resp = requests.get(
                grib_url,
                headers={**_HEADERS, "Range": entry.range_header()},
                timeout=timeout,
                stream=True,
            )
            resp.raise_for_status()
            for chunk in resp.iter_content(chunk_size=1 << 16):
                fh.write(chunk)
    return out_path
