"""Hermetic tests for the watershed cache hardening (H-9, FR-3, NFR-6).

The cache exists ONLY for identical-point reuse (planner warm -> the briefing
seconds later; the scheduled 6 h refresh; reopening a saved mission). These tests
pin, fully offline via the ``wscache.delineate`` seam:

- key precision: two points differing at the 4th decimal never share a basin;
- the H-1 completeness fields round-trip through both cache layers, and legacy
  (pre-H-1) cache files read back as complete;
- corrupt cache files self-heal instead of poisoning the key;
- a cache-write failure never fails a briefing whose basin is already in memory;
- expired WBD-fallback entries get an upgrade re-attempt (stale served on failure);
- single-flight waiters time out on a stuck owner and delineate themselves.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest
from shapely.geometry import Polygon, box

from upstreamwx.config import Settings
from upstreamwx.watershed import cache as wscache
from upstreamwx.watershed.cache import _key, delineate_cached, resolve_and_trace_cached
from upstreamwx.watershed.pourpoint import WBD_FALLBACK_METHOD, PourpointBasin
from upstreamwx.watershed.upstream import UpstreamTrace

# A point away from other test sets so nothing collides.
LAT, LON = 39.2500, -106.2500


def _fake_basin(lat: float, lon: float, *, method: str = "nldi-raindrop-split",
                complete: bool = True, completeness_notes: list[str] | None = None,
                ) -> PourpointBasin:
    poly = Polygon(
        [(lon - 0.01, lat - 0.01), (lon + 0.01, lat - 0.01),
         (lon + 0.01, lat + 0.01), (lon - 0.01, lat + 0.01)]
    )
    return PourpointBasin(
        lat=lat, lon=lon, snapped_lat=lat, snapped_lon=lon,
        polygon=poly, area_km2=poly.area, method=method,
        complete=complete, completeness_notes=completeness_notes or [],
    )


def _basin_path(tmp_path: Path) -> Path:
    return tmp_path / "watershed" / "pourpoint" / f"{_key(LAT, LON)}.geojson"


def _trace_path(tmp_path: Path) -> Path:
    return tmp_path / "watershed" / f"{_key(LAT, LON)}.geojson"


def _age_file(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture(autouse=True)
def _clean_registry():
    """The single-flight registry is process-wide; isolate each test."""
    with wscache._inflight_lock:
        wscache._inflight.clear()
    yield
    with wscache._inflight_lock:
        wscache._inflight.clear()


# --------------------------------------------------------------------------- #
# (a) Key precision — identical-point reuse only
# --------------------------------------------------------------------------- #
def test_key_precision_separates_fourth_decimal_points():
    """Two pins differing at the 4th decimal (~11 m) must never share a basin."""
    assert _key(37.0192, -111.9889) != _key(37.0193, -111.9889)
    assert _key(37.0192, -111.9889) != _key(37.0192, -111.9888)
    # Identical UI coordinates (float round-trip formatting included) still reuse.
    assert _key(37.0192, -111.9889) == _key(float("37.0192000"), float("-111.9889000"))


# --------------------------------------------------------------------------- #
# Completeness fields (H-1 contract) round-trip through both cache layers
# --------------------------------------------------------------------------- #
def test_basin_completeness_fields_round_trip(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    calls = 0

    def fake_delineate(lat, lon):
        nonlocal calls
        calls += 1
        return _fake_basin(lat, lon, complete=False,
                           completeness_notes=["probe failed at the widest HU4 fetch"])

    monkeypatch.setattr(wscache, "delineate", fake_delineate)
    delineate_cached(LAT, LON, settings=settings)
    second = delineate_cached(LAT, LON, settings=settings)

    assert calls == 1  # second call served from disk
    assert second.complete is False
    assert second.completeness_notes == ["probe failed at the widest HU4 fetch"]


def test_trace_completeness_fields_round_trip(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    calls = 0

    def fake_trace(lat, lon):
        nonlocal calls
        calls += 1
        return UpstreamTrace(
            origin_huc12="101000010101",
            upstream_huc_ids=["101000010101"],
            polygon=box(0, 0, 1, 1),
            area_km2=42.0,
            method="tohuc-graph",
            complete=False,
            completeness_notes=["widening failed"],
        )

    monkeypatch.setattr(wscache, "_resolve_and_trace", fake_trace)
    resolve_and_trace_cached(LAT, LON, settings=settings)
    second = resolve_and_trace_cached(LAT, LON, settings=settings)

    assert calls == 1
    assert second.complete is False
    assert second.completeness_notes == ["widening failed"]


def test_legacy_cache_files_default_to_complete(tmp_path, monkeypatch):
    """Pre-H-1 cache files lack the completeness fields -> default True / []."""
    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(wscache, "delineate", _fake_basin)
    delineate_cached(LAT, LON, settings=settings)  # writes the modern shape

    # Strip the new properties, emulating an old file.
    path = _basin_path(tmp_path)
    feature = json.loads(path.read_text())
    del feature["properties"]["complete"]
    del feature["properties"]["completeness_notes"]
    path.write_text(json.dumps(feature))

    basin = delineate_cached(LAT, LON, settings=settings)
    assert basin.complete is True
    assert basin.completeness_notes == []


# --------------------------------------------------------------------------- #
# (c) Corrupt cache files self-heal
# --------------------------------------------------------------------------- #
def test_corrupt_basin_cache_self_heals(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    path = _basin_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")  # a poisoned key

    calls = 0

    def fake_delineate(lat, lon):
        nonlocal calls
        calls += 1
        return _fake_basin(lat, lon)

    monkeypatch.setattr(wscache, "delineate", fake_delineate)
    basin = delineate_cached(LAT, LON, settings=settings)
    assert basin.area_km2 > 0
    assert calls == 1
    # The bad file was replaced by a valid one; the next read is a disk hit.
    json.loads(path.read_text())
    delineate_cached(LAT, LON, settings=settings)
    assert calls == 1


def test_corrupt_trace_cache_self_heals(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    path = _trace_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("")  # empty file (e.g. pre-fsync crash artifact)

    trace = UpstreamTrace(
        origin_huc12="101000010101",
        upstream_huc_ids=["101000010101"],
        polygon=box(0, 0, 1, 1),
        area_km2=42.0,
        method="tohuc-graph",
    )
    monkeypatch.setattr(wscache, "_resolve_and_trace", lambda lat, lon: trace)
    result = resolve_and_trace_cached(LAT, LON, settings=settings)
    assert result.origin_huc12 == "101000010101"
    json.loads(path.read_text())  # healed on disk


# --------------------------------------------------------------------------- #
# (d) Cache-write failure never fails the briefing
# --------------------------------------------------------------------------- #
def test_write_failure_still_returns_basin_to_owner_and_waiters(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)

    def slow_delineate(lat, lon):
        time.sleep(0.2)  # hold the trace so waiters join mid-flight
        return _fake_basin(lat, lon)

    def full_disk(path, text):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(wscache, "delineate", slow_delineate)
    monkeypatch.setattr(wscache, "_atomic_write", full_disk)

    n = 4
    barrier = threading.Barrier(n)
    results: list[PourpointBasin] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            basin = delineate_cached(LAT, LON, settings=settings)
        except BaseException as exc:  # noqa: BLE001 — we assert on it
            with lock:
                errors.append(exc)
        else:
            with lock:
                results.append(basin)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []  # a disk-full write must not fan out as a failure
    assert len(results) == n
    assert not wscache._inflight  # registry cleaned up


def test_trace_write_failure_still_returns_trace(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    trace = UpstreamTrace(
        origin_huc12="101000010101",
        upstream_huc_ids=["101000010101"],
        polygon=box(0, 0, 1, 1),
        area_km2=42.0,
        method="tohuc-graph",
    )
    monkeypatch.setattr(wscache, "_resolve_and_trace", lambda lat, lon: trace)
    monkeypatch.setattr(
        wscache, "_atomic_write",
        lambda path, text: (_ for _ in ()).throw(OSError(28, "No space left on device")),
    )
    assert resolve_and_trace_cached(LAT, LON, settings=settings).area_km2 == 42.0


# --------------------------------------------------------------------------- #
# (b) Expired WBD-fallback entries are re-attempted, stale served on failure
# --------------------------------------------------------------------------- #
def _boom(lat, lon):
    raise RuntimeError("nldi down")


def test_expired_fallback_upgrades_to_exact(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(
        wscache, "delineate", lambda lat, lon: _fake_basin(lat, lon, method=WBD_FALLBACK_METHOD)
    )
    first = delineate_cached(LAT, LON, settings=settings)
    assert first.method == WBD_FALLBACK_METHOD

    _age_file(_basin_path(tmp_path), wscache._FALLBACK_TTL_S + 60)
    # NLDI recovered: the read path must retry rather than pin the fallback forever.
    monkeypatch.setattr(wscache, "delineate", _fake_basin)
    upgraded = delineate_cached(LAT, LON, settings=settings)
    assert upgraded.method == "nldi-raindrop-split"

    # The upgrade was persisted; later reads are exact disk hits, no re-trace.
    monkeypatch.setattr(wscache, "delineate", _boom)
    assert delineate_cached(LAT, LON, settings=settings).method == "nldi-raindrop-split"


def test_expired_fallback_served_stale_when_retry_fails(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(
        wscache, "delineate", lambda lat, lon: _fake_basin(lat, lon, method=WBD_FALLBACK_METHOD)
    )
    delineate_cached(LAT, LON, settings=settings)
    _age_file(_basin_path(tmp_path), wscache._FALLBACK_TTL_S + 60)

    monkeypatch.setattr(wscache, "delineate", _boom)
    stale = delineate_cached(LAT, LON, settings=settings)  # best effort (NFR-6)
    assert stale.method == WBD_FALLBACK_METHOD
    assert not wscache._inflight


def test_fresh_fallback_is_not_reattempted(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    calls = 0

    def counting_fallback(lat, lon):
        nonlocal calls
        calls += 1
        return _fake_basin(lat, lon, method=WBD_FALLBACK_METHOD)

    monkeypatch.setattr(wscache, "delineate", counting_fallback)
    delineate_cached(LAT, LON, settings=settings)
    delineate_cached(LAT, LON, settings=settings)  # within TTL: pure disk hit
    assert calls == 1


def test_exact_basins_never_expire(tmp_path, monkeypatch):
    """The TTL applies only to fallback-quality entries, not exact ones."""
    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(wscache, "delineate", _fake_basin)
    delineate_cached(LAT, LON, settings=settings)
    _age_file(_basin_path(tmp_path), wscache._FALLBACK_TTL_S + 60)

    monkeypatch.setattr(wscache, "delineate", _boom)
    assert delineate_cached(LAT, LON, settings=settings).method == "nldi-raindrop-split"


# --------------------------------------------------------------------------- #
# (f) Single-flight waiters time out on a stuck owner
# --------------------------------------------------------------------------- #
def test_waiter_times_out_on_stuck_owner_and_self_delineates(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(wscache, "_WAITER_TIMEOUT_S", 0.2)
    release = threading.Event()
    calls = 0
    lock = threading.Lock()

    def delineate_stub(lat, lon):
        nonlocal calls
        with lock:
            calls += 1
            first = calls == 1
        if first:
            release.wait(timeout=5)  # the stuck owner (hung socket analogue)
        return _fake_basin(lat, lon)

    monkeypatch.setattr(wscache, "delineate", delineate_stub)

    owner = threading.Thread(target=lambda: delineate_cached(LAT, LON, settings=settings))
    owner.start()
    try:
        assert _wait_until(lambda: _key(LAT, LON) in wscache._inflight)
        # The waiter must not hang forever: it evicts the stuck entry and traces itself.
        basin = delineate_cached(LAT, LON, settings=settings)
        assert basin.area_km2 > 0
        assert calls == 2
    finally:
        release.set()
        owner.join(timeout=5)
    assert not owner.is_alive()
