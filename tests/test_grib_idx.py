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
from upstreamwx.grib.idx import IdxEntry, download_subset


def _entries(n: int) -> list[IdxEntry]:
    # n contiguous 10-byte messages.
    return [
        IdxEntry(num=i + 1, start=i * 10, descriptor="d:APCP", end=i * 10 + 10)
        for i in range(n)
    ]


def _mock_session(body: bytes = b"xxxxxxxxxx"):
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
    assert out.read_bytes() == b"xxxxxxxxxx" * 5


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
