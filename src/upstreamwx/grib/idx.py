"""GRIB2 retrieval via ``.idx`` byte-range subsetting (shared SREF/HREF primitive).

NOMADS ``ensprod`` files are large (SREF 350-660 MB; an HREF per-hour file is
tens of MB). Each ``.grib2`` has a ``.idx`` sidecar listing every GRIB message's
byte offset and a human-readable descriptor, e.g.::

    186:23632071:d=2026061715:APCP:surface:0-3 hour acc fcst:prob >12.7:prob fcst 0/26:...
    49:14370185:d=2026061800:APCP:surface:11-12 hour acc fcst:prob >12.7:prob fcst 0/10:Nbhd Prob

We parse the index, select the handful of messages we need, and issue HTTP Range
requests for only those byte spans — turning a multi-hundred-MB download into a
few hundred KB. The concatenated message bytes form a valid GRIB2 file readable
by cfgrib. The descriptor grammar is identical across SREF and HREF, so the same
code drives both ensembles.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import requests

# Polite identifier shared across NOMADS pulls; NOMADS rejects some default agents.
USER_AGENT = "UpstreamWX/0.0 (M0.0 ensemble spikes; +https://upstreamwx.com)"
_HEADERS = {"User-Agent": USER_AGENT}


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


def fetch_idx(
    idx_url: str, timeout: float | tuple[float, float] = (10.0, 30.0)
) -> list[IdxEntry]:
    """Download and parse a ``.idx`` sidecar.

    ``timeout`` is a ``(connect, read)`` pair so an unreachable NOMADS fails fast on
    connect rather than blocking the whole briefing's latency budget (NFR-6).
    """
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
    timeout: float | tuple[float, float] = (10.0, 30.0),
    max_seconds: float = 60.0,
) -> Path:
    """Fetch the byte spans for ``selected`` messages and write a valid GRIB2 file.

    Returns the output path. Issues one Range request per message (NOMADS does not
    reliably support multipart ranges) and concatenates in index order.

    ``timeout`` is a ``(connect, read)`` pair so an unreachable NOMADS fails fast on
    connect rather than blocking. Because we loop one request per message, a slow but
    reachable host could still stack well past any single timeout, so ``max_seconds``
    caps the *total* wall-clock for the whole subset: once exceeded we raise
    ``TimeoutError`` and let the caller degrade that ensemble gracefully (NFR-6)
    rather than overrun the briefing's latency budget (and the front proxy timeout).
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max_seconds
    # One Session for the whole subset so keep-alive reuses a single TCP+TLS
    # connection across all ~N range requests, instead of a fresh handshake per
    # message (the dominant cost when a field spans the full forecast horizon).
    with requests.Session() as session, out_path.open("wb") as fh:
        session.headers.update(_HEADERS)
        for entry in sorted(selected, key=lambda e: e.start):
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"GRIB subset exceeded {max_seconds:.0f}s budget "
                    f"({grib_url}); degrading this ensemble."
                )
            resp = session.get(
                grib_url,
                headers={"Range": entry.range_header()},
                timeout=timeout,
                stream=True,
            )
            resp.raise_for_status()
            for chunk in resp.iter_content(chunk_size=1 << 16):
                fh.write(chunk)
    return out_path
