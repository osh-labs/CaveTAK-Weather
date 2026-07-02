"""GEFS subset cache self-heal — corrupt on-disk artifacts must not poison a cycle.

A byte-range subset fetched while the GRIB file was still publishing can be truncated;
eccodes then fails to decode it (classically ``EOFError``). Left in place it degrades GEFS
on every briefing for the life of the cached cycle. :func:`load_member_field_cached` drops
the bad artifact and re-fetches once (data quality first-class, NFR-6). Hermetic — no I/O.
"""

from __future__ import annotations

import pytest

from upstreamwx.config import Settings
from upstreamwx.gefs import cache as gcache
from upstreamwx.gefs.sources import GefsCycle


def _stub_subset(monkeypatch, tmp_path):
    """Make _ensure_member_subset return a fake on-disk path without any download."""
    path = tmp_path / "gep01.f024.APCP.surface.grib2"
    path.write_bytes(b"stub")
    calls: list[bool] = []

    def fake_ensure(cycle, member, fhour, var, fcst, level, res_set="0p25", *, settings=None,
                    refresh=False):
        calls.append(refresh)
        path.write_bytes(b"stub")  # a "re-fetch" re-materializes the file
        return path, None

    monkeypatch.setattr(gcache, "_ensure_member_subset", fake_ensure)
    return path, calls


def test_corrupt_subset_self_heals_and_refetches(monkeypatch, tmp_path):
    path, ensure_calls = _stub_subset(monkeypatch, tmp_path)
    discarded: list = []
    monkeypatch.setattr(gcache, "_discard_subset", lambda p: discarded.append(p))

    seen = {"n": 0}

    def flaky_decode(p, fn, *, use_pool=False, key_extra=None):
        seen["n"] += 1
        if seen["n"] == 1:
            raise EOFError("end of file reached while decoding GRIB message")
        return "DECODED-OK"

    monkeypatch.setattr(gcache, "decode_cached", flaky_decode)

    field = gcache.load_member_field_cached(
        GefsCycle("20260620", 0), "gep01", 24, "APCP", "0-24 hour acc", "surface",
        settings=Settings(data_dir=tmp_path),
    )

    assert field.data == "DECODED-OK"
    assert seen["n"] == 2                      # decoded again after the heal
    assert discarded == [path]                 # the bad artifact was dropped
    assert ensure_calls[-1] is True            # re-fetched with refresh=True


def test_persistently_corrupt_subset_raises_for_the_member(monkeypatch, tmp_path):
    """A second decode failure propagates so the member degrades (quorum carries the ensemble)."""
    _stub_subset(monkeypatch, tmp_path)
    monkeypatch.setattr(gcache, "_discard_subset", lambda p: None)

    def always_eof(p, fn, *, use_pool=False, key_extra=None):
        raise EOFError("still truncated")

    monkeypatch.setattr(gcache, "decode_cached", always_eof)

    with pytest.raises(EOFError):
        gcache.load_member_field_cached(
            GefsCycle("20260620", 0), "gep01", 24, "APCP", "0-24 hour acc", "surface",
            settings=Settings(data_dir=tmp_path),
        )


def test_discard_subset_removes_file_and_idx(tmp_path):
    subset = tmp_path / "gep01.f024.APCP.surface.grib2"
    idx = tmp_path / "gep01.f024.APCP.surface.grib2.idx"
    subset.write_bytes(b"x")
    idx.write_bytes(b"y")
    gcache._discard_subset(subset)
    assert not subset.exists() and not idx.exists()
    # Idempotent: a second call on already-gone files is a no-op, not an error.
    gcache._discard_subset(subset)


def test_warm_cycle_skips_truncated_subset(monkeypatch, tmp_path):
    """A TruncatedGribError (ValueError) during warming skips that subset rather than raising
    out of the whole warm pass — regression guard for the download-time validation change."""
    from upstreamwx.grib.idx import TruncatedGribError

    def boom(*a, **k):
        raise TruncatedGribError("truncated mid-publish")

    monkeypatch.setattr(gcache, "_ensure_member_subset", boom)
    paths = gcache.warm_cycle(
        GefsCycle("20260620", 0), (24,),
        settings=Settings(data_dir=tmp_path), members=("gec00", "gep01"),
    )
    assert paths == []  # every subset skipped, no exception escaped the pass
