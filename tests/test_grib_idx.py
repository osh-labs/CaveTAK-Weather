"""Offline tests for the shared GRIB byte-range downloader (grib/idx.py).

These cover the resilience behavior that keeps a slow/unreachable NOMADS from
blowing the briefing's latency budget (NFR-6): a single keep-alive Session across
all range requests, and a total wall-clock deadline that degrades the ensemble
rather than stacking per-message timeouts. Fully offline — requests is mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from upstreamwx.grib import idx as idx_mod
from upstreamwx.grib.idx import (
    IdxEntry,
    TruncatedGribError,
    download_subset,
    validate_grib2_bytes,
)


def _grib_msg(total: int = 32, edition: int = 2) -> bytes:
    """A structurally-complete GRIB2 message: `GRIB` header + declared length + `7777`."""
    assert total >= 20
    head = b"GRIB" + b"\x00\x00" + b"\x00" + bytes([edition]) + total.to_bytes(8, "big")
    return head + b"\x00" * (total - 20) + b"7777"


_MSG = _grib_msg()


def _entries(n: int) -> list[IdxEntry]:
    # n contiguous messages, each the size of the mock body.
    step = len(_MSG)
    return [
        IdxEntry(num=i + 1, start=i * step, descriptor="d:APCP", end=i * step + step)
        for i in range(n)
    ]


def _mock_session(body: bytes = _MSG):
    """A Session context manager whose .get returns `body` in one chunk."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.iter_content.return_value = [body]
    session = MagicMock()
    session.get.return_value = resp
    session.headers = {}
    ctx = MagicMock()
    ctx.__enter__.return_value = session
    ctx.__exit__.return_value = False
    return ctx, session


def test_download_subset_reuses_one_session_for_all_messages(tmp_path) -> None:
    """All range requests go through a single Session (keep-alive), not requests.get."""
    ctx, session = _mock_session()
    out = tmp_path / "out.grib2"
    with patch.object(idx_mod.requests, "Session", return_value=ctx) as mk_session:
        download_subset("http://nomads/x.grib2", _entries(5), out)
    mk_session.assert_called_once()        # exactly one connection for the whole subset
    assert session.get.call_count == 5     # one range request per message
    assert out.read_bytes() == _MSG * 5


def test_download_subset_enforces_total_deadline(tmp_path) -> None:
    """The total-time budget raises TimeoutError instead of issuing every request."""
    ctx, session = _mock_session()
    out = tmp_path / "out.grib2"
    # monotonic jumps 100s per call, so the 60s budget is exceeded immediately.
    ticks = iter([0.0, 1000.0, 2000.0, 3000.0, 4000.0, 5000.0])
    with (
        patch.object(idx_mod.requests, "Session", return_value=ctx),
        patch.object(idx_mod.time, "monotonic", lambda: next(ticks)),
    ):
        with pytest.raises(TimeoutError, match="exceeded"):
            download_subset("http://nomads/x.grib2", _entries(5), out, max_seconds=60.0)
    assert session.get.call_count == 0     # bailed before any (further) request


# --- GRIB2 completeness validation (mid-publish truncation guard) ------------------------

def test_validate_accepts_single_and_multi_message() -> None:
    validate_grib2_bytes(_MSG, expected_messages=1)
    validate_grib2_bytes(_grib_msg(24) + _grib_msg(40), expected_messages=2)


def test_validate_rejects_empty() -> None:
    with pytest.raises(TruncatedGribError, match="empty"):
        validate_grib2_bytes(b"", expected_messages=1)


def test_validate_rejects_bad_magic() -> None:
    with pytest.raises(TruncatedGribError, match="magic"):
        validate_grib2_bytes(b"NOPE" + _MSG[4:], expected_messages=1)


def test_validate_rejects_truncated_body() -> None:
    """Declared length exceeds the bytes actually present — the mid-publish case."""
    truncated = _MSG[:-6]  # lose the 7777 and part of the body; declared length now overshoots
    with pytest.raises(TruncatedGribError, match="declares|truncated"):
        validate_grib2_bytes(truncated, expected_messages=1)


def test_validate_rejects_missing_end_marker() -> None:
    msg = bytearray(_MSG)
    msg[-4:] = b"XXXX"  # right length, wrong trailer
    with pytest.raises(TruncatedGribError, match="7777"):
        validate_grib2_bytes(bytes(msg), expected_messages=1)


def test_validate_rejects_message_count_mismatch() -> None:
    with pytest.raises(TruncatedGribError, match="expected 3"):
        validate_grib2_bytes(_MSG, expected_messages=3)


def test_validate_rejects_unexpected_edition() -> None:
    with pytest.raises(TruncatedGribError, match="edition"):
        validate_grib2_bytes(_grib_msg(edition=1), expected_messages=1)


def test_download_subset_rejects_truncated_download(tmp_path) -> None:
    """A truncated range response makes download_subset raise instead of caching garbage."""
    ctx, _session = _mock_session(body=_MSG[:-6])  # every range returns a truncated message
    out = tmp_path / "out.grib2"
    with patch.object(idx_mod.requests, "Session", return_value=ctx):
        with pytest.raises(TruncatedGribError):
            download_subset("http://nomads/x.grib2", _entries(1), out)


def test_truncated_grib_error_is_valuerror() -> None:
    # Flows through the per-member degradation path (which catches ValueError), not a crash.
    assert issubclass(TruncatedGribError, ValueError)
