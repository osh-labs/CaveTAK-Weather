"""Offline tests for the Open-Meteo transient-failure retry (NFR-6).

Open-Meteo intermittently 5xx's or drops the connection; ``_query`` retries transient
failures with backoff but fails fast on permanent client errors. ``time.sleep`` is
patched out so these stay fast and hermetic.
"""

from __future__ import annotations

import requests

from upstreamwx.ingest import openmeteo


class _FakeResponse:
    def __init__(self, status: int, payload: dict | None = None):
        self.status_code = status
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.exceptions.HTTPError(f"{self.status_code}")
            err.response = self  # type: ignore[attr-defined]
            raise err

    def json(self):
        return self._payload


def _patch_sleep(monkeypatch):
    monkeypatch.setattr(openmeteo.time, "sleep", lambda _s: None)


def test_query_retries_then_succeeds(monkeypatch):
    _patch_sleep(monkeypatch)
    calls = {"n": 0}

    def fake_get(*_a, **_k):
        calls["n"] += 1
        if calls["n"] < 3:
            return _FakeResponse(503)  # transient
        return _FakeResponse(200, {"hourly": {"time": []}})

    monkeypatch.setattr(openmeteo.requests, "get", fake_get)
    assert openmeteo._query(33.0, -84.0) == {"hourly": {"time": []}}
    assert calls["n"] == 3


def test_query_gives_up_after_max_attempts(monkeypatch):
    _patch_sleep(monkeypatch)
    calls = {"n": 0}

    def fake_get(*_a, **_k):
        calls["n"] += 1
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(openmeteo.requests, "get", fake_get)
    try:
        openmeteo._query(33.0, -84.0)
        raised = False
    except requests.exceptions.ConnectionError:
        raised = True
    assert raised
    assert calls["n"] == openmeteo._MAX_ATTEMPTS


def test_query_does_not_retry_client_error(monkeypatch):
    _patch_sleep(monkeypatch)
    calls = {"n": 0}

    def fake_get(*_a, **_k):
        calls["n"] += 1
        return _FakeResponse(400)  # permanent client error

    monkeypatch.setattr(openmeteo.requests, "get", fake_get)
    try:
        openmeteo._query(33.0, -84.0)
        raised = False
    except requests.exceptions.HTTPError:
        raised = True
    assert raised
    assert calls["n"] == 1  # no retry on 4xx
