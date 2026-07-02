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

# GRIB2 message framing (WMO FM 92): every message opens with the ASCII "GRIB" marker
# (Section 0) carrying an 8-byte big-endian total length at bytes 8-15, and closes with
# the ASCII "7777" marker (Section 8). These let us verify a downloaded subset is
# structurally complete *before* it is cached.
_GRIB_MAGIC = b"GRIB"
_GRIB_END = b"7777"
_GRIB_EDITION = 2


class TruncatedGribError(ValueError):
    """A downloaded subset is not a run of complete GRIB2 messages (mid-publish truncation).

    A ``ValueError`` subclass so it flows through the existing per-member degradation path
    (the ensemble quorum carries) rather than sinking the whole source (NFR-6).
    """


def validate_grib2_bytes(
    data: bytes, *, expected_messages: int | None = None, what: str = ""
) -> None:
    """Raise :class:`TruncatedGribError` unless ``data`` is N complete GRIB2 messages.

    Walks the concatenated messages by their self-declared Section 0 length and checks each
    opens with ``GRIB`` (edition 2) and closes with ``7777``. Catches the exact failure behind
    the "gefs: unavailable (EOFError)" incident: an open-ended byte range fetched while the
    source ``.grib2`` was still publishing returns fewer bytes than the message declares, so the
    file writes "successfully" yet decodes to EOF. Validating here keeps the truncated bytes out
    of the cache entirely (§16.1; data quality first-class).
    """
    detail = f" ({what})" if what else ""
    n = len(data)
    if n == 0:
        raise TruncatedGribError(f"empty GRIB subset{detail}")
    offset = count = 0
    while offset < n:
        if data[offset : offset + 4] != _GRIB_MAGIC:
            raise TruncatedGribError(f"bad GRIB magic at byte {offset}{detail}")
        if offset + 16 > n:
            raise TruncatedGribError(f"truncated GRIB header at byte {offset}{detail}")
        if data[offset + 7] != _GRIB_EDITION:
            raise TruncatedGribError(
                f"unexpected GRIB edition {data[offset + 7]} at byte {offset}{detail}"
            )
        total = int.from_bytes(data[offset + 8 : offset + 16], "big")
        if total < 16 or offset + total > n:
            # Declared length runs past the bytes we have -> truncated (the mid-publish case).
            raise TruncatedGribError(
                f"GRIB message at byte {offset} declares {total} bytes but only "
                f"{n - offset} remain{detail}"
            )
        if data[offset + total - 4 : offset + total] != _GRIB_END:
            raise TruncatedGribError(
                f"missing 7777 end marker for message at byte {offset}{detail}"
            )
        offset += total
        count += 1
    if expected_messages is not None and count != expected_messages:
        raise TruncatedGribError(
            f"expected {expected_messages} GRIB messages, found {count}{detail}"
        )


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
    # Verify the concatenated bytes are complete GRIB2 messages before the caller accepts them
    # (cached_subset only os.replace()s a returned path into place). A range fetched mid-publish
    # can write "successfully" yet be truncated — validating here keeps it out of the cache and
    # raises TruncatedGribError (a ValueError) so the member degrades behind the quorum (NFR-6).
    validate_grib2_bytes(
        out_path.read_bytes(), expected_messages=len(selected), what=grib_url
    )
    return out_path
