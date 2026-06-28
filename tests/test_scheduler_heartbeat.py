"""Tests for the scheduler's dead-man's-switch monitoring ping (FR-12 monitoring).

The ping is best-effort: it no-ops when unconfigured, builds the right Healthchecks.io
URL per outcome, and never propagates an exception into the scheduler.
"""

from __future__ import annotations

import asyncio

import upstreamwx.api.scheduler as scheduler


def test_ping_noops_when_url_unset(monkeypatch):
    """No URL configured -> no HTTP call at all."""
    calls: list = []
    monkeypatch.setattr("requests.get", lambda *a, **k: calls.append((a, k)))
    asyncio.run(scheduler._ping(None))
    assert calls == []


def test_ping_builds_suffixed_url(monkeypatch):
    """The base URL gets the success/start/fail suffix, trailing slash normalized."""
    seen: list[str] = []
    monkeypatch.setattr("requests.get", lambda url, **k: seen.append(url))
    asyncio.run(scheduler._ping("https://hc-ping.com/abc/", "/start"))
    asyncio.run(scheduler._ping("https://hc-ping.com/abc", ""))
    asyncio.run(scheduler._ping("https://hc-ping.com/abc", "/fail"))
    assert seen == [
        "https://hc-ping.com/abc/start",
        "https://hc-ping.com/abc",
        "https://hc-ping.com/abc/fail",
    ]


def test_ping_swallows_request_errors(monkeypatch):
    """A monitoring outage must never raise into the scheduler loop."""
    def boom(*_a, **_k):
        raise RuntimeError("network down")

    monkeypatch.setattr("requests.get", boom)
    # Should complete without raising.
    asyncio.run(scheduler._ping("https://hc-ping.com/abc"))
