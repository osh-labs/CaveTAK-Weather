"""Concurrency-cap tests for the briefing service (graceful 503 under load).

The cap bounds concurrent COLD generations so a burst of distinct missions can't OOM/thrash a small
host; cache hits bypass it, and a saturated cap raises ``BriefingBusy`` -> HTTP 503 with
``Retry-After`` so the PWA shows a retry banner. All offline (deterministic ``--inputs`` path; no
network, no LLM).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from upstreamwx.api.app import app, service
from upstreamwx.api.cache import BriefingCache
from upstreamwx.api.models import MissionSpec
from upstreamwx.api.service import BriefingBusy, BriefingService

FIXTURES = Path(__file__).parent / "fixtures" / "sitrep"
SAMPLE_INPUTS = yaml.safe_load((FIXTURES / "sample_inputs.yaml").read_text())["inputs"]
FIXED_NOW = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)


def _spec(**overrides) -> MissionSpec:
    base = dict(
        lat=37.0192, lon=-111.9889, activity="canyon",
        start="2026-06-20T08:00", end="2026-06-20T18:00",
        name="Buckskin Gulch", slot=True, frame=False, inputs=SAMPLE_INPUTS,
    )
    base.update(overrides)
    return MissionSpec(**base)


def _service(monkeypatch, *, maxconc: int = 1, timeout: float = 0.05) -> BriefingService:
    """A service whose cap is read from env at construction (re-read each get_settings call)."""
    monkeypatch.setenv("UPSTREAMWX_BRIEFING_MAX_CONCURRENCY", str(maxconc))
    monkeypatch.setenv("UPSTREAMWX_BRIEFING_BUSY_TIMEOUT_S", str(timeout))
    return BriefingService(cache=BriefingCache(maxsize=64))


def test_busy_raises_when_cap_saturated(monkeypatch) -> None:
    """A cache miss with no free generation slot raises BriefingBusy after the busy timeout."""
    svc = _service(monkeypatch, maxconc=1, timeout=0.05)
    assert svc._gen_sem.acquire()  # occupy the only slot
    try:
        with pytest.raises(BriefingBusy):
            svc.get_briefing(_spec(name="Saturated"), now=FIXED_NOW)
    finally:
        svc._gen_sem.release()


def test_cache_hit_bypasses_cap(monkeypatch) -> None:
    """A cached briefing returns even when the generation cap is fully saturated."""
    svc = _service(monkeypatch, maxconc=1, timeout=0.05)
    spec = _spec(name="Cached")
    first = svc.get_briefing(spec, now=FIXED_NOW)  # populates the cache (miss)
    assert first.cached is False
    assert svc._gen_sem.acquire()  # saturate the cap — no generation slot is free
    try:
        again = svc.get_briefing(spec, now=FIXED_NOW)  # same spec -> cache hit, no slot needed
        assert again.cached is True
    finally:
        svc._gen_sem.release()


def test_cap_disabled_when_zero(monkeypatch) -> None:
    """max_concurrency <= 0 disables the cap entirely; generation still works."""
    svc = _service(monkeypatch, maxconc=0, timeout=0.05)
    assert svc._gen_sem is None
    assert svc.get_briefing(_spec(name="Uncapped"), now=FIXED_NOW).cached is False


def test_endpoint_maps_busy_to_503(monkeypatch) -> None:
    """/v1/briefing maps BriefingBusy -> 503 + Retry-After (the PWA's retry-banner cue)."""
    monkeypatch.setenv("UPSTREAMWX_API_ENABLE_SCHEDULER", "0")
    monkeypatch.setenv("UPSTREAMWX_API_ENABLE_WARM", "0")

    def _busy(spec, **kw):
        raise BriefingBusy()

    monkeypatch.setattr(service, "get_briefing", _busy)
    with TestClient(app) as c:
        resp = c.post("/v1/briefing", json=_spec().model_dump(mode="json"))
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "10"
    assert "busy" in resp.json()["detail"].lower()
