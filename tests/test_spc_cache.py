"""Offline tests for the in-process SPC Day-1 outlook cache (latency follow-up).

The categorical outlook is identical for every mission, so it is downloaded + shapely-parsed
once and reused across briefings within a TTL. These assert the cache collapses the download,
refreshes after the TTL, serves stale on a refresh failure (NFR-6), and still resolves the
point-in-polygon correctly — all with the network stubbed, no real clock.
"""

from __future__ import annotations

import pytest
import requests
from shapely.geometry import box

from upstreamwx.ingest import spc


@pytest.fixture(autouse=True)
def _reset_cache():
    spc._outlook_cache = None
    yield
    spc._outlook_cache = None


def test_outlook_downloaded_once_within_ttl(monkeypatch):
    calls = {"n": 0}

    def fake_download(timeout):
        calls["n"] += 1
        return [("slight", box(-112.0, 36.0, -111.0, 38.0))]

    monkeypatch.setattr(spc, "_download_outlook", fake_download)

    first = spc.category_at(37.0, -111.5)
    second = spc.category_at(37.0, -111.5)

    assert first == second == "slight"
    assert calls["n"] == 1  # second call served from the cache


def test_outlook_refreshes_after_ttl(monkeypatch):
    calls = {"n": 0}
    clock = {"t": 1000.0}

    def fake_download(timeout):
        calls["n"] += 1
        return [("slight", box(-112.0, 36.0, -111.0, 38.0))]

    monkeypatch.setattr(spc, "_download_outlook", fake_download)
    monkeypatch.setattr(spc, "monotonic", lambda: clock["t"])

    spc.category_at(37.0, -111.5)
    clock["t"] += spc._OUTLOOK_TTL_S + 1  # age past the TTL
    spc.category_at(37.0, -111.5)

    assert calls["n"] == 2  # re-downloaded once the cache went stale


def test_outlook_serves_stale_on_refresh_failure(monkeypatch):
    state = {"fail": False}
    clock = {"t": 0.0}

    def fake_download(timeout):
        if state["fail"]:
            raise requests.RequestException("SPC down")
        return [("enhanced", box(-112.0, 36.0, -111.0, 38.0))]

    monkeypatch.setattr(spc, "_download_outlook", fake_download)
    monkeypatch.setattr(spc, "monotonic", lambda: clock["t"])

    assert spc.category_at(37.0, -111.5) == "enhanced"  # primes the cache
    clock["t"] += spc._OUTLOOK_TTL_S + 1  # expire it
    state["fail"] = True
    assert spc.category_at(37.0, -111.5) == "enhanced"  # stale served, no raise (NFR-6)


def test_first_load_failure_propagates(monkeypatch):
    def fake_download(timeout):
        raise requests.RequestException("SPC down")

    monkeypatch.setattr(spc, "_download_outlook", fake_download)
    # No prior outlook to fall back on -> the failure surfaces so the orchestrator degrades.
    with pytest.raises(requests.RequestException):
        spc.category_at(37.0, -111.5)


def test_point_outside_all_polygons_returns_none(monkeypatch):
    monkeypatch.setattr(
        spc, "_download_outlook", lambda timeout: [("slight", box(-100.0, 40.0, -99.0, 41.0))]
    )
    assert spc.category_at(37.0, -111.5) is None


def test_highest_severity_polygon_wins(monkeypatch):
    # Overlapping outlooks at a point: the most severe category is reported.
    monkeypatch.setattr(
        spc,
        "_download_outlook",
        lambda timeout: [
            ("marginal", box(-112.0, 36.0, -111.0, 38.0)),
            ("enhanced", box(-112.0, 36.0, -111.0, 38.0)),
        ],
    )
    assert spc.category_at(37.0, -111.5) == "enhanced"
