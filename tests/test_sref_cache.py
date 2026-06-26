"""Offline tests for the persistent SREF grid cache (roadmap §M0.1.1, FR-7, FR-12).

Hermetic: a temp ``data_dir`` plus the committed ``sref_sample_subset.grib2`` fixture stand
in for a real NOMADS pull. Network access is patched out — ``fetch_idx``/``select_messages``
are stubbed and ``download_subset`` copies the fixture — so these exercise the cache
semantics (hit/miss, atomic write, warm, prune, cross-restart) without touching the network.
A ``network``-marked test does the one live warm and is deselected by default.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from upstreamwx.config import Settings
from upstreamwx.sref.cache import (
    DEFAULT_FIELDS,
    _cycle_dir,
    _subset_name,
    load_probability_field_cached,
    prune_old_cycles,
    warm_cycle,
)
from upstreamwx.sref.sources import SrefCycle

CYCLE = SrefCycle(date="20260617", hour=15)
FIELD = ("APCP", ">6.35", "3hrly")


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings rooted at a temp data dir; also export it so ``get_settings()`` agrees."""
    monkeypatch.setenv("UPSTREAMWX_DATA_DIR", str(tmp_path))
    return Settings(data_dir=tmp_path)


def _place_fixture(dest: Path, fixtures_dir: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(fixtures_dir / "sref_sample_subset.grib2", dest)


def _fake_download(fixtures_dir: Path):
    """A ``download_subset`` stand-in that copies the fixture to the requested out path."""

    def _dl(grib_url, selected, out_path, *args, **kwargs):
        shutil.copy(fixtures_dir / "sref_sample_subset.grib2", out_path)
        return Path(out_path)

    return _dl


def test_cache_hit_skips_download(settings: Settings, fixtures_dir: Path) -> None:
    """A pre-existing cached subset is decoded from disk without any network call."""
    path = _cycle_dir(settings, CYCLE) / _subset_name(*FIELD)
    _place_fixture(path, fixtures_dir)

    with (
        patch("upstreamwx.sref.cache.fetch_idx", side_effect=AssertionError("network!")),
        patch("upstreamwx.sref.cache.download_subset", side_effect=AssertionError("network!")),
    ):
        field = load_probability_field_cached(CYCLE, *FIELD, settings=settings)

    assert {"y", "x"} <= set(field.data.dims)
    assert field.grib_path == path
    assert field.extras["cached"] is True


def test_cache_miss_writes_then_hit(settings: Settings, fixtures_dir: Path) -> None:
    """First access downloads + writes the subset; the second hits disk (no re-download)."""
    path = _cycle_dir(settings, CYCLE) / _subset_name(*FIELD)
    with (
        patch("upstreamwx.sref.cache.fetch_idx", return_value=[]),
        patch("upstreamwx.sref.cache.select_messages", return_value=["msg"]),
        patch(
            "upstreamwx.sref.cache.download_subset", side_effect=_fake_download(fixtures_dir)
        ) as dl,
    ):
        first = load_probability_field_cached(CYCLE, *FIELD, settings=settings)
        assert first.extras["cached"] is False
        assert path.is_file()
        second = load_probability_field_cached(CYCLE, *FIELD, settings=settings)

    assert second.extras["cached"] is True
    assert dl.call_count == 1  # the cached read did not download


def test_atomic_no_partial_on_error(settings: Settings, fixtures_dir: Path) -> None:
    """A failed download leaves no final entry and no leftover temp file (NFR-6)."""
    cdir = _cycle_dir(settings, CYCLE)
    path = cdir / _subset_name(*FIELD)

    def _dl_then_fail(grib_url, selected, out_path, *a, **k):
        Path(out_path).write_bytes(b"partial")  # a partial temp write...
        raise RuntimeError("connection reset")  # ...then the download dies

    with (
        patch("upstreamwx.sref.cache.fetch_idx", return_value=[]),
        patch("upstreamwx.sref.cache.select_messages", return_value=["msg"]),
        patch("upstreamwx.sref.cache.download_subset", side_effect=_dl_then_fail),
        pytest.raises(RuntimeError, match="connection reset"),
    ):
        load_probability_field_cached(CYCLE, *FIELD, settings=settings)

    assert not path.is_file()
    assert list(cdir.glob("*.tmp.*")) == []


def test_cross_restart_reads_disk(settings: Settings, fixtures_dir: Path, tmp_path: Path) -> None:
    """A fresh Settings on the same data_dir reads the cached cycle — restart loses nothing."""
    with (
        patch("upstreamwx.sref.cache.fetch_idx", return_value=[]),
        patch("upstreamwx.sref.cache.select_messages", return_value=["msg"]),
        patch("upstreamwx.sref.cache.download_subset", side_effect=_fake_download(fixtures_dir)),
    ):
        load_probability_field_cached(CYCLE, *FIELD, settings=settings)

    # Simulate a process restart: a brand-new Settings pointed at the same dir, no network.
    reborn = Settings(data_dir=tmp_path)
    with (
        patch("upstreamwx.sref.cache.fetch_idx", side_effect=AssertionError("network!")),
        patch("upstreamwx.sref.cache.download_subset", side_effect=AssertionError("network!")),
    ):
        field = load_probability_field_cached(CYCLE, *FIELD, settings=reborn)
    assert field.extras["cached"] is True


def test_warm_idempotent(settings: Settings, fixtures_dir: Path) -> None:
    """warm_cycle pulls missing fields once and skips already-cached ones on re-run."""
    with (
        patch("upstreamwx.sref.cache.fetch_idx", return_value=[]),
        patch("upstreamwx.sref.cache.select_messages", return_value=["msg"]),
        patch(
            "upstreamwx.sref.cache.download_subset", side_effect=_fake_download(fixtures_dir)
        ) as dl,
    ):
        paths = warm_cycle(CYCLE, settings=settings, fields=(FIELD,))
        assert len(paths) == 1 and paths[0].is_file()
        warm_cycle(CYCLE, settings=settings, fields=(FIELD,))  # second run: all cached

    assert dl.call_count == 1


def test_prune_keeps_n_newest(settings: Settings) -> None:
    """prune_old_cycles deletes all but the newest ``keep`` cycle dirs."""
    root = settings.data_dir / "sref"
    names = ["20260616_21", "20260617_03", "20260617_09", "20260617_15"]
    for n in names:
        d = root / n
        d.mkdir(parents=True)
        (d / "APCP_gt6.35_3hrly.grib2").write_bytes(b"x")

    removed = prune_old_cycles(settings=settings, keep=2)

    remaining = sorted(p.name for p in root.iterdir())
    assert remaining == ["20260617_09", "20260617_15"]  # the two newest
    assert sorted(p.name for p in removed) == ["20260616_21", "20260617_03"]


# --- warm_and_prune + scheduler cadence (no real clock) ---------------------

def test_warm_and_prune_warms_live_cycle(settings: Settings, fixtures_dir: Path) -> None:
    """The service warms the live cycle's full field set and reports the count."""
    from upstreamwx.api.service import BriefingService

    with (
        patch("upstreamwx.api.service.latest_available_cycle", return_value=CYCLE),
        patch("upstreamwx.sref.cache.fetch_idx", return_value=[]),
        patch("upstreamwx.sref.cache.select_messages", return_value=["msg"]),
        patch("upstreamwx.sref.cache.download_subset", side_effect=_fake_download(fixtures_dir)),
    ):
        warmed = BriefingService().warm_and_prune()

    assert warmed == len(DEFAULT_FIELDS)
    assert (settings.data_dir / "sref" / "20260617_15").is_dir()


def test_warm_and_prune_no_live_cycle(settings: Settings) -> None:
    """No live cycle on NOMADS is non-fatal — warm reports 0 and does not raise (NFR-6)."""
    from upstreamwx.api.service import BriefingService

    with patch("upstreamwx.api.service.latest_available_cycle", return_value=None):
        assert BriefingService().warm_and_prune() == 0


def test_scheduler_fires_warm_then_refresh() -> None:
    """One cycle boundary drives warm_and_prune then refresh_active (FR-12)."""
    from upstreamwx.api.scheduler import run_scheduler

    stop = asyncio.Event()
    service = MagicMock()
    service.warm_and_prune.return_value = 2

    def _refresh() -> int:
        stop.set()  # end the loop after the first boundary fires
        return 1

    service.refresh_active.side_effect = _refresh

    with patch("upstreamwx.api.scheduler.seconds_until_next_cycle", return_value=0.0):
        asyncio.run(run_scheduler(service, stop=stop))

    service.warm_and_prune.assert_called_once()
    service.refresh_active.assert_called_once()


@pytest.mark.network
def test_warm_cycle_live(settings: Settings) -> None:
    from upstreamwx.sref import latest_available_cycle

    cycle = latest_available_cycle()
    assert cycle is not None, "no live SREF cycle found on NOMADS"
    paths = warm_cycle(cycle, settings=settings)
    assert paths and all(p.is_file() for p in paths)
