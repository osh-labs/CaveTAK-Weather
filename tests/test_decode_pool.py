"""Tests for the out-of-process GEFS decode pool seam and the memory-aware decode LRU.

eccodes is not thread-safe, so :func:`upstreamwx.grib.cache.decode_cached` serializes the
in-process decode on a global lock. The decode *pool* (installed by the API lifespan) routes the
heavy per-member GEFS decodes to worker processes instead, cropping inside the worker so only a
small array crosses the process boundary. These cover: the routing seam (pool used, compute lock
NOT held), the ``key_extra`` memo discrimination, broken-pool degradation (NFR-6), a real spawn
pool end-to-end, the crop-in-worker determinism (NFR-4), and the ``crop_and_normalize`` refactor.
"""

from __future__ import annotations

import concurrent.futures
import functools
import multiprocessing
import os
import pickle
import types
from pathlib import Path

import numpy as np
import xarray as xr
from shapely.geometry import box

import upstreamwx.grib.cache as gc
from upstreamwx.gefs.cache import _decode_cropped
from upstreamwx.gefs.extract import crop_and_normalize, crop_bbox_normalize
from upstreamwx.ingest.gefs_provider import _poly_max, _poly_max_precropped, _union_bounds


class _FakeFuture:
    def __init__(self, fn, args):
        self._fn, self._args = fn, args

    def result(self):
        return self._fn(*self._args)


class _FakeExecutor:
    """Runs the callable inline but records the submit and the compute-lock state at submit time."""

    def __init__(self) -> None:
        self.submits: list = []
        self.lock_held_at_submit: bool | None = None

    def submit(self, fn, *args):
        self.lock_held_at_submit = gc._decode_compute_lock.locked()
        self.submits.append((fn, args))
        return _FakeFuture(fn, args)

    def shutdown(self, wait=False, cancel_futures=False):  # noqa: ARG002 — pool API
        pass


def teardown_function() -> None:
    """Detach any pool and clear the memo so tests don't leak state into each other."""
    gc.shutdown_decode_pool(wait=True)
    gc._clear_decoded()


def test_lifespan_does_not_create_pool_by_default(monkeypatch) -> None:
    """Default settings must NOT start a decode pool — it OOMs small hosts (api_enable_decode_pool).

    Opt-in only: with the flag on, the lifespan installs a pool; off (default), it does not.
    """
    from fastapi.testclient import TestClient

    from upstreamwx.api.app import app as fastapi_app

    # Avoid the background scheduler/warm pool doing work during the smoke test. get_settings()
    # re-reads env every call, so monkeypatch.setenv before startup is enough (no cache to clear).
    monkeypatch.setenv("UPSTREAMWX_API_ENABLE_SCHEDULER", "0")
    monkeypatch.setenv("UPSTREAMWX_API_ENABLE_WARM", "0")

    with TestClient(fastapi_app):
        assert gc.decode_pool_enabled() is False  # default: no spawn workers
    assert gc.decode_pool_enabled() is False

    monkeypatch.setenv("UPSTREAMWX_API_ENABLE_DECODE_POOL", "1")
    monkeypatch.setenv("UPSTREAMWX_DECODE_POOL_WORKERS", "1")
    with TestClient(fastapi_app):
        assert gc.decode_pool_enabled() is True  # opt-in: pool installed
    assert gc.decode_pool_enabled() is False  # torn down on shutdown


def test_use_pool_without_installed_pool_is_in_process(tmp_path: Path) -> None:
    """``use_pool=True`` with no pool installed falls through to the in-process memoised decode."""
    gc.set_decode_pool(None)
    gc._clear_decoded()
    calls: list[Path] = []

    def decode(p: Path):
        calls.append(p)
        return f"d::{p.read_text()}"

    p = tmp_path / "s.grib2"
    p.write_text("A")
    first = gc.decode_cached(p, decode, use_pool=True)
    second = gc.decode_cached(p, decode, use_pool=True)
    assert first == "d::A"
    assert second is first
    assert len(calls) == 1  # closure works (never reaches a pool) and is memoised


def test_pool_path_routes_to_executor_without_compute_lock(tmp_path: Path) -> None:
    """The pool path submits to the executor, does NOT hold the compute lock, and memoises."""
    fake = _FakeExecutor()
    gc.set_decode_pool(fake)
    gc._clear_decoded()

    def decode(p: Path):
        return f"d::{p.read_text()}"

    p = tmp_path / "s.grib2"
    p.write_text("A")
    key_extra = ("crop", (1.0, 2.0, 3.0, 4.0), 1.0)
    out = gc.decode_cached(p, decode, use_pool=True, key_extra=key_extra)

    assert out == "d::A"
    assert len(fake.submits) == 1 and fake.submits[0][0] is decode
    assert fake.lock_held_at_submit is False  # cross-process decode must not take the lock
    # second call is served from the memo — no new submit
    again = gc.decode_cached(p, decode, use_pool=True, key_extra=key_extra)
    assert again is out and len(fake.submits) == 1


def test_broken_pool_falls_back_in_process_and_detaches(tmp_path: Path) -> None:
    """A BrokenProcessPool degrades to the in-process decode and clears the pool (NFR-6)."""
    from concurrent.futures.process import BrokenProcessPool

    class _BrokenExecutor:
        def submit(self, fn, *args):  # noqa: ARG002
            raise BrokenProcessPool("worker crashed")

        def shutdown(self, wait=False, cancel_futures=False):  # noqa: ARG002
            pass

    gc.set_decode_pool(_BrokenExecutor())
    gc._clear_decoded()
    calls: list[Path] = []

    def decode(p: Path):
        calls.append(p)
        return f"d::{p.read_text()}"

    p = tmp_path / "s.grib2"
    p.write_text("A")
    out = gc.decode_cached(p, decode, use_pool=True)
    assert out == "d::A" and len(calls) == 1
    assert gc.decode_pool_enabled() is False  # the broken pool was detached


def test_key_extra_distinguishes_entries(tmp_path: Path) -> None:
    """Two crops of the same file (different bbox) are distinct memo entries."""
    gc.set_decode_pool(None)
    gc._clear_decoded()
    calls: list[Path] = []

    def decode(p: Path):
        calls.append(p)
        return object()

    p = tmp_path / "s.grib2"
    p.write_text("A")
    a = gc.decode_cached(p, decode, key_extra=("crop", (1.0, 2.0, 3.0, 4.0), 1.0))
    b = gc.decode_cached(p, decode, key_extra=("crop", (9.0, 9.0, 9.0, 9.0), 1.0))
    c = gc.decode_cached(p, decode, key_extra=("crop", (1.0, 2.0, 3.0, 4.0), 1.0))
    assert len(calls) == 2  # two distinct key_extra -> two decodes
    assert c is a and b is not a


def test_real_spawn_pool_decodes_through_cache(tmp_path: Path) -> None:
    """A real spawn ProcessPoolExecutor round-trips a decode through ``decode_cached``.

    Proves the lifespan's pool wiring works end to end: the callable pickles, runs in a worker
    process, and its result returns. ``os.path.getsize`` is a picklable stdlib stand-in for the
    real (GRIB-reading) decode, so this needs no committed GRIB fixture.
    """
    ctx = multiprocessing.get_context("spawn")
    pool = concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=ctx)
    gc.set_decode_pool(pool)
    gc._clear_decoded()
    p = tmp_path / "s.grib2"
    p.write_bytes(b"x" * 123)
    size = gc.decode_cached(p, os.path.getsize, use_pool=True, key_extra="size")
    assert size == 123


def _synthetic_global_grid() -> xr.DataArray:
    """A GEFS-like global 0-360 grid (ascending lon, descending lat) covering CONUS."""
    lons = np.arange(0.0, 360.0, 1.0)
    lats = np.arange(60.0, 20.0, -1.0)
    rng = np.random.default_rng(0)
    return xr.DataArray(
        rng.random((lats.size, lons.size)),
        dims=("latitude", "longitude"),
        coords={"latitude": lats, "longitude": lons},
    )


def test_pool_crop_aggregate_matches_in_process() -> None:
    """Crop-to-union-then-mask == decode-full-then-crop-per-domain, bit-for-bit (NFR-4).

    The pool worker crops to the union bbox of the watershed + LAoC; the in-process path crops the
    full grid per domain. Aggregating each domain must give the identical value either way.
    """
    da = _synthetic_global_grid()
    watershed = box(-112.0, 36.0, -110.0, 38.0)
    laoc = box(-111.5, 36.5, -110.5, 37.5)
    bbox = _union_bounds(watershed, laoc)

    full = types.SimpleNamespace(data=da)
    cropped = types.SimpleNamespace(data=crop_bbox_normalize(da, bbox))
    for poly in (watershed, laoc):
        in_proc = _poly_max(full, poly, "APCP")
        pooled = _poly_max_precropped(cropped, poly, "APCP")
        assert in_proc is not None
        assert pooled == in_proc  # exact equality — NFR-4 determinism


def test_crop_and_normalize_matches_bbox_core() -> None:
    """``crop_and_normalize(da, poly)`` is exactly the bbox core over ``poly.bounds`` (refactor)."""
    da = _synthetic_global_grid()
    poly = box(-112.0, 36.0, -110.0, 38.0)
    xr.testing.assert_identical(crop_and_normalize(da, poly), crop_bbox_normalize(da, poly.bounds))


def test_decode_cropped_partial_is_picklable() -> None:
    """The decode-pool callable (a partial of a top-level fn) round-trips through pickle."""
    part = functools.partial(_decode_cropped, bbox=(-112.0, 36.0, -110.0, 38.0), margin=1.0)
    restored = pickle.loads(pickle.dumps(part))
    assert restored.func is _decode_cropped
    assert restored.keywords["bbox"] == (-112.0, 36.0, -110.0, 38.0)
    assert restored.keywords["margin"] == 1.0
