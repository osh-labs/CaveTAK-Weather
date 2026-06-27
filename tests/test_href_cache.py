"""Offline tests for the persistent HREF grid cache (roadmap §M0.1.1, FR-7, FR-12).

Hermetic: a temp ``data_dir`` plus the committed ``href_sample_subset.grib2`` fixture stand
in for a real NOMADS pull. Network is patched out — ``fetch_idx``/``select_messages`` are
stubbed and ``download_subset`` copies the fixture — so these exercise the cache semantics
(hit/miss, atomic write, warm f06-f48, prune, cross-restart) without touching the network.
The cache is the HREF analogue of ``test_sref_cache.py``, keyed by ``(cycle, fhour, var, prob)``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from upstreamwx.config import Settings
from upstreamwx.href.cache import (
    DEFAULT_FIELDS,
    FieldSpec,
    _cycle_dir,
    _subset_name,
    load_probability_field_cached,
    prune_old_cycles,
    warm_cycle,
)
from upstreamwx.href.sources import HrefCycle

CYCLE = HrefCycle(date="20260617", hour=12)
FHOUR = 6
FIELD = ("APCP", ">12.7")  # (var, prob)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings rooted at a temp data dir; also export it so ``get_settings()`` agrees."""
    monkeypatch.setenv("UPSTREAMWX_DATA_DIR", str(tmp_path))
    return Settings(data_dir=tmp_path)


def _place_fixture(dest: Path, fixtures_dir: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(fixtures_dir / "href_sample_subset.grib2", dest)


def _fake_download(fixtures_dir: Path):
    """A ``download_subset`` stand-in that copies the fixture to the requested out path."""

    def _dl(grib_url, selected, out_path, *args, **kwargs):
        shutil.copy(fixtures_dir / "href_sample_subset.grib2", out_path)
        return Path(out_path)

    return _dl


def test_cache_hit_skips_download(settings: Settings, fixtures_dir: Path) -> None:
    """A pre-existing cached subset is decoded from disk without any network call."""
    path = _cycle_dir(settings, CYCLE) / _subset_name(FHOUR, *FIELD)
    _place_fixture(path, fixtures_dir)

    with (
        patch("upstreamwx.href.cache.fetch_idx", side_effect=AssertionError("network!")),
        patch("upstreamwx.href.cache.download_subset", side_effect=AssertionError("network!")),
    ):
        field = load_probability_field_cached(CYCLE, FHOUR, *FIELD, settings=settings)

    assert {"y", "x"} <= set(field.data.dims)
    assert field.grib_path == path
    assert field.extras["cached"] is True
    assert field.fhour == FHOUR


def test_cache_miss_writes_then_hit(settings: Settings, fixtures_dir: Path) -> None:
    """First access downloads + writes the subset; the second hits disk (no re-download)."""
    path = _cycle_dir(settings, CYCLE) / _subset_name(FHOUR, *FIELD)
    with (
        patch("upstreamwx.href.cache.fetch_idx", return_value=[]),
        patch("upstreamwx.href.cache.select_messages", return_value=["msg"]),
        patch(
            "upstreamwx.href.cache.download_subset", side_effect=_fake_download(fixtures_dir)
        ) as dl,
    ):
        first = load_probability_field_cached(CYCLE, FHOUR, *FIELD, settings=settings)
        assert first.extras["cached"] is False
        assert path.is_file()
        second = load_probability_field_cached(CYCLE, FHOUR, *FIELD, settings=settings)

    assert second.extras["cached"] is True
    assert dl.call_count == 1  # the cached read did not download


def test_atomic_no_partial_on_error(settings: Settings, fixtures_dir: Path) -> None:
    """A failed download leaves no final entry and no leftover temp file (NFR-6)."""
    cdir = _cycle_dir(settings, CYCLE)
    path = cdir / _subset_name(FHOUR, *FIELD)

    def _dl_then_fail(grib_url, selected, out_path, *a, **k):
        Path(out_path).write_bytes(b"partial")  # a partial temp write...
        raise RuntimeError("connection reset")  # ...then the download dies

    with (
        patch("upstreamwx.href.cache.fetch_idx", return_value=[]),
        patch("upstreamwx.href.cache.select_messages", return_value=["msg"]),
        patch("upstreamwx.href.cache.download_subset", side_effect=_dl_then_fail),
        pytest.raises(RuntimeError, match="connection reset"),
    ):
        load_probability_field_cached(CYCLE, FHOUR, *FIELD, settings=settings)

    assert not path.is_file()
    assert list(cdir.glob("*.tmp.*")) == []


def test_cross_restart_reads_disk(settings: Settings, fixtures_dir: Path, tmp_path: Path) -> None:
    """A fresh Settings on the same data_dir reads the cached cycle — restart loses nothing."""
    with (
        patch("upstreamwx.href.cache.fetch_idx", return_value=[]),
        patch("upstreamwx.href.cache.select_messages", return_value=["msg"]),
        patch("upstreamwx.href.cache.download_subset", side_effect=_fake_download(fixtures_dir)),
    ):
        load_probability_field_cached(CYCLE, FHOUR, *FIELD, settings=settings)

    reborn = Settings(data_dir=tmp_path)  # simulate a process restart on the same dir
    with (
        patch("upstreamwx.href.cache.fetch_idx", side_effect=AssertionError("network!")),
        patch("upstreamwx.href.cache.download_subset", side_effect=AssertionError("network!")),
    ):
        field = load_probability_field_cached(CYCLE, FHOUR, *FIELD, settings=reborn)
    assert field.extras["cached"] is True


def test_warm_cycle_pulls_band_idempotently(settings: Settings, fixtures_dir: Path) -> None:
    """warm_cycle pulls every field across f[min..max] once; a re-run re-reads, no re-download."""
    fields = (FieldSpec("APCP", ">12.7", 1), FieldSpec("LTNG", ">0.2", 0))
    with (
        patch("upstreamwx.href.cache.fetch_idx", return_value=[]),
        patch("upstreamwx.href.cache.select_messages", return_value=["msg"]),
        patch(
            "upstreamwx.href.cache.download_subset", side_effect=_fake_download(fixtures_dir)
        ) as dl,
    ):
        paths = warm_cycle(CYCLE, settings=settings, fmin=6, fmax=9, fields=fields)
        # 4 forecast hours (f06-f09) x 2 fields = 8 cached subsets.
        assert len(paths) == 8
        assert all(p.is_file() for p in paths)
        assert dl.call_count == 8
        warm_cycle(CYCLE, settings=settings, fmin=6, fmax=9, fields=fields)  # all cached now

    assert dl.call_count == 8  # second warm downloaded nothing


def test_warm_cycle_skips_unavailable_fields(settings: Settings, fixtures_dir: Path) -> None:
    """A field missing at a forecast hour (LookupError) is skipped; the rest still warm (NFR-6)."""
    fields = (FieldSpec("APCP", ">12.7", 1),)

    calls = {"n": 0}

    def _flaky_select(entries, **kw):
        calls["n"] += 1
        return [] if calls["n"] == 1 else ["msg"]  # first fhour has no message

    with (
        patch("upstreamwx.href.cache.fetch_idx", return_value=[]),
        patch("upstreamwx.href.cache.select_messages", side_effect=_flaky_select),
        patch("upstreamwx.href.cache.download_subset", side_effect=_fake_download(fixtures_dir)),
    ):
        paths = warm_cycle(CYCLE, settings=settings, fmin=6, fmax=8, fields=fields)

    assert len(paths) == 2  # f06 skipped (empty select), f07 + f08 warmed


def test_prune_keeps_n_newest(settings: Settings) -> None:
    """prune_old_cycles deletes all but the newest ``keep`` cycle dirs."""
    root = settings.data_dir / "href"
    for n in ("20260616_12", "20260617_00", "20260617_12"):
        d = root / n
        d.mkdir(parents=True)
        (d / "f06_APCP_gt12.7.grib2").write_bytes(b"x")

    removed = prune_old_cycles(settings=settings, keep=2)

    remaining = sorted(p.name for p in root.iterdir())
    assert remaining == ["20260617_00", "20260617_12"]  # the two newest
    assert sorted(p.name for p in removed) == ["20260616_12"]


def test_default_fields_have_unique_var_prob() -> None:
    """The cache key omits the accum window, so (var, prob) must be unique (guards collisions)."""
    keys = [(f.var, f.prob) for f in DEFAULT_FIELDS]
    assert len(keys) == len(set(keys))


@pytest.mark.network
def test_warm_cycle_live(settings: Settings) -> None:
    from upstreamwx.href import latest_available_cycle

    cycle = latest_available_cycle()
    assert cycle is not None, "no live HREF cycle found on NOMADS"
    paths = warm_cycle(cycle, settings=settings, fmin=6, fmax=8)
    assert paths and all(p.is_file() for p in paths)
