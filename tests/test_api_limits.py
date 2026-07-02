"""Request-validation, bounded-resource, and rate-limit tests (H-8).

Pre-launch hardening of the public API surface (docs/code-review-2026-07-02.md H-8):

* ``MissionSpec`` structural validation — window ordering/length, CONUS bounds, radius
  caps — rejects malformed requests with 422 before any ingest cost is spent;
* wall-clock currency (`ensure_current`) applies only to LIVE specs, so the deterministic
  offline ``inputs`` path (FR-25) never expires as real time passes;
* the active-mission refresh registry is capped (evicting the soonest-ending window);
* the watershed warm queue refuses past its cap (503 + Retry-After);
* the per-IP token-bucket limiter throttles the expensive/billable endpoints (429 +
  Retry-After), keys on the trusted client IP only, and is itself LRU-bounded.

All offline — no network, no LLM.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from upstreamwx.api.app import app, service
from upstreamwx.api.cache import mission_cache_key
from upstreamwx.api.models import (
    MAX_RADIUS_KM,
    MissionSpec,
    MissionWindowError,
)
from upstreamwx.api.service import BriefingService, WarmQueueFull

# The package re-exports the FastAPI instance as `app`, shadowing the submodule under
# `import upstreamwx.api.app as ...`; resolve the module itself for its private helpers.
app_mod = importlib.import_module("upstreamwx.api.app")

FIXTURES = Path(__file__).parent / "fixtures" / "sitrep"
SAMPLE_INPUTS = yaml.safe_load((FIXTURES / "sample_inputs.yaml").read_text())["inputs"]
FIXED_NOW = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)


def _base(**overrides) -> dict:
    """A valid offline request payload (raw dict so invalid variants can be built)."""
    base = dict(
        lat=37.0192,
        lon=-111.9889,
        activity="canyon",
        start="2026-06-20T08:00",
        end="2026-06-20T18:00",
        name="Buckskin Gulch",
        slot=True,
        frame=False,
        inputs=SAMPLE_INPUTS,
    )
    base.update(overrides)
    return base


@pytest.fixture
def client(monkeypatch):
    """A TestClient with background loops off; lifespan entry resets the rate buckets."""
    monkeypatch.setenv("UPSTREAMWX_API_ENABLE_SCHEDULER", "0")
    monkeypatch.setenv("UPSTREAMWX_API_ENABLE_WARM", "0")
    service.cache.clear()
    with TestClient(app) as c:
        yield c
    service.cache.clear()


# -- MissionSpec structural validation (422 at the endpoint) ------------------------------
def test_end_before_start_rejected(client):
    payload = _base(start="2026-06-20T18:00", end="2026-06-20T08:00")
    resp = client.post("/v1/briefing", json=payload)
    assert resp.status_code == 422
    assert "end must be after" in resp.text


def test_window_longer_than_seven_days_rejected(client):
    payload = _base(start="2026-06-20T08:00", end="2026-06-28T09:00")
    resp = client.post("/v1/briefing", json=payload)
    assert resp.status_code == 422
    assert "7 days" in resp.text


def test_out_of_conus_point_rejected(client):
    for lat, lon in [(60.0, -111.9889), (37.0, -160.0), (20.0, -111.9), (37.0, -60.0)]:
        resp = client.post("/v1/briefing", json=_base(lat=lat, lon=lon))
        assert resp.status_code == 422, (lat, lon)


def test_oversize_radii_rejected(client):
    assert client.post("/v1/briefing", json=_base(radius_km=400)).status_code == 422
    assert client.post("/v1/briefing", json=_base(lightning_radius_km=400)).status_code == 422
    # The PRD's documented max slider stop (200 mi ~ 322 km) itself is accepted.
    ok = client.post("/v1/briefing", json=_base(radius_km=MAX_RADIUS_KM))
    assert ok.status_code == 200


def test_warm_endpoint_rejects_out_of_conus(client):
    """/v1/watershed/warm carries the same CONUS bounds (no USGS spend outside coverage)."""
    resp = client.post("/v1/watershed/warm", json={"lat": 55.0, "lon": -111.9})
    assert resp.status_code == 422
    resp = client.post("/v1/watershed/warm", json={"lat": 37.0, "lon": -140.0})
    assert resp.status_code == 422


# -- wall-clock currency: live specs only; the offline inputs path never expires ----------
def test_offline_inputs_spec_with_past_dates_still_briefs(client):
    """The pinned-date offline fixtures (FR-25) stay valid as real time passes (NFR-4)."""
    resp = client.post("/v1/briefing", json=_base())
    assert resp.status_code == 200
    assert resp.json()["cache_cycle"] == "static"


def test_ensure_current_rejects_far_future_start():
    spec = MissionSpec(**_base(inputs=None))  # live; window is 2026-06-20
    with pytest.raises(MissionWindowError, match="GEFS forecast horizon"):
        spec.ensure_current(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))  # start > now + 10 d


def test_ensure_current_rejects_long_ended_window():
    spec = MissionSpec(**_base(inputs=None))
    with pytest.raises(MissionWindowError, match="ended more than"):
        spec.ensure_current(datetime(2026, 6, 25, 12, 0, tzinfo=UTC))  # ended > 24 h ago


def test_ensure_current_allows_underway_and_recent_windows():
    spec = MissionSpec(**_base(inputs=None))
    spec.ensure_current(datetime(2026, 6, 20, 12, 0, tzinfo=UTC))  # underway
    spec.ensure_current(datetime(2026, 6, 21, 8, 0, tzinfo=UTC))  # ended < 24 h ago
    spec.ensure_current(datetime(2026, 6, 12, 12, 0, tzinfo=UTC))  # starts in 8 d (< 10 d)


def test_ensure_current_skipped_for_inputs_specs():
    """A saved feature vector is a deterministic replay — never 'stale' (FR-25)."""
    spec = MissionSpec(**_base())  # inputs supplied
    spec.ensure_current(datetime(2030, 1, 1, tzinfo=UTC))  # years later: still fine


def test_live_stale_window_rejected_at_endpoint(client):
    """The service enforces currency for live specs before any ingest (H-8) -> 422."""
    now = datetime.now(UTC).replace(tzinfo=None)
    start = (now - timedelta(days=10)).isoformat(timespec="minutes")
    end = (now - timedelta(days=9)).isoformat(timespec="minutes")
    resp = client.post("/v1/briefing", json=_base(start=start, end=end, inputs=None))
    assert resp.status_code == 422
    assert "stale" in resp.text


def test_live_beyond_horizon_window_rejected_at_endpoint(client):
    now = datetime.now(UTC).replace(tzinfo=None)
    start = (now + timedelta(days=30)).isoformat(timespec="minutes")
    end = (now + timedelta(days=30, hours=10)).isoformat(timespec="minutes")
    resp = client.post("/v1/briefing", json=_base(start=start, end=end, inputs=None))
    assert resp.status_code == 422
    assert "horizon" in resp.text


# -- bounded active-mission registry -------------------------------------------------------
def _offline_service(monkeypatch) -> BriefingService:
    """A service whose generator never hits the network and whose cycle token is pinned."""
    from upstreamwx.engine.models import HazardInputs
    from upstreamwx.sitrep import generate as generate_mod

    real = generate_mod.generate_briefing

    def offline(mission, *, inputs=None, frame=None, generated_at=None, cycle=None):
        return real(
            mission, inputs=inputs or HazardInputs(), frame=False, generated_at=generated_at
        )

    monkeypatch.setattr("upstreamwx.api.service.generate_briefing", offline)
    svc = BriefingService()
    monkeypatch.setattr(svc, "_cycle_token", lambda now: "2026-06-19T06Z")
    return svc


def test_active_registry_evicts_soonest_ending_at_cap(monkeypatch):
    """At ``api_active_missions_max`` the entry whose window ends first is evicted (H-8)."""
    monkeypatch.setenv("UPSTREAMWX_API_ACTIVE_MISSIONS_MAX", "3")
    svc = _offline_service(monkeypatch)

    def live(lat: float, end_hour: int) -> MissionSpec:
        return MissionSpec(
            **_base(
                lat=lat,
                start="2026-06-20T08:00",
                end=f"2026-06-20T{end_hour:02d}:00",
                inputs=None,
            )
        )

    # Distinct locations -> distinct cache keys; window ends at 10/14/16/12 local.
    specs = [live(37.01, 10), live(37.02, 14), live(37.03, 16)]
    for spec in specs:
        svc.get_briefing(spec, now=FIXED_NOW)
    assert svc.active_count == 3

    fourth = live(37.04, 12)
    svc.get_briefing(fourth, now=FIXED_NOW)
    assert svc.active_count == 3  # capped, not grown

    keys = {mission_cache_key(s.to_mission()) for s in [specs[1], specs[2], fourth]}
    evicted = mission_cache_key(specs[0].to_mission())  # ends 10:00 — the soonest
    assert set(svc._active) == keys
    assert evicted not in svc._active


def test_active_registry_reregistration_does_not_evict(monkeypatch):
    """Re-briefing an already-registered mission at the cap must not evict a peer."""
    monkeypatch.setenv("UPSTREAMWX_API_ACTIVE_MISSIONS_MAX", "2")
    svc = _offline_service(monkeypatch)
    a = MissionSpec(**_base(lat=37.01, inputs=None))
    b = MissionSpec(**_base(lat=37.02, inputs=None))
    svc.get_briefing(a, now=FIXED_NOW)
    svc.get_briefing(b, now=FIXED_NOW)
    svc.cache.clear()  # force regeneration so registration re-runs
    svc.get_briefing(a, now=FIXED_NOW)
    assert svc.active_count == 2


# -- bounded watershed warm queue ----------------------------------------------------------
def test_warm_queue_full_raises(monkeypatch, tmp_path):
    """Past ``api_warm_pending_max`` pending points, warm_watershed raises WarmQueueFull."""
    import threading

    from upstreamwx.watershed import cache as wscache

    monkeypatch.setenv("UPSTREAMWX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("UPSTREAMWX_API_WARM_PENDING_MAX", "1")
    release = threading.Event()

    def blocking_delineate(lat, lon):  # holds the single pending slot until released
        release.wait(timeout=3)
        raise RuntimeError("no basin in this test")

    monkeypatch.setattr(wscache, "delineate", blocking_delineate)
    svc = BriefingService()
    svc.start_warming()
    try:
        assert svc.warm_watershed(37.01, -111.98) is True
        with pytest.raises(WarmQueueFull):
            svc.warm_watershed(37.02, -111.97)  # distinct point; queue is at capacity
        # A duplicate of the in-flight point is still a dedupe no-op, not a refusal.
        assert svc.warm_watershed(37.01, -111.98) is False
    finally:
        release.set()
        svc.stop_warming()


def test_warm_endpoint_maps_queue_full_to_503(client, monkeypatch):
    def _full(lat, lon):
        raise WarmQueueFull()

    monkeypatch.setattr(service, "warm_watershed", _full)
    resp = client.post("/v1/watershed/warm", json={"lat": 37.0192, "lon": -111.9889})
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "30"


# -- per-IP token-bucket rate limiting ------------------------------------------------------
def test_bucket_burst_then_429_with_refill():
    lim = app_mod._TokenBucketLimiter(6)
    for _ in range(6):
        assert lim.acquire("a", now=0.0) is None  # one minute's burst passes
    retry = lim.acquire("a", now=0.0)
    assert isinstance(retry, int) and retry >= 1  # 7th within the minute is refused
    # 6/min refills one token per 10 s: the same client is granted again after 10 s.
    assert lim.acquire("a", now=10.0) is None


def test_bucket_ips_are_independent():
    lim = app_mod._TokenBucketLimiter(2)
    assert lim.acquire("a", now=0.0) is None
    assert lim.acquire("a", now=0.0) is None
    assert lim.acquire("a", now=0.0) is not None  # "a" exhausted
    assert lim.acquire("b", now=0.0) is None  # "b" unaffected


def test_bucket_store_is_lru_bounded():
    """The limiter can never become the memory sink it exists to prevent (H-8)."""
    lim = app_mod._TokenBucketLimiter(6, max_ips=4)
    for i in range(10):
        lim.acquire(f"ip-{i}", now=float(i))
    assert len(lim) <= 4


def _fake_request(peer: str | None, xff: str | None = None):
    headers = {} if xff is None else {"x-forwarded-for": xff}
    client = None if peer is None else SimpleNamespace(host=peer)
    return SimpleNamespace(client=client, headers=headers)


def test_client_ip_honors_xff_only_from_loopback_peer():
    # Behind our nginx (loopback peer): trust the entry nginx appended — the RIGHTMOST —
    # never the client-supplied leftmost ($proxy_add_x_forwarded_for appends $remote_addr).
    req = _fake_request("127.0.0.1", xff="6.6.6.6, 203.0.113.9")
    assert app_mod._client_ip(req) == "203.0.113.9"
    req = _fake_request("::1", xff="203.0.113.9")
    assert app_mod._client_ip(req) == "203.0.113.9"
    # Direct (non-loopback) peer: a forged XFF is ignored; the socket peer is the key.
    req = _fake_request("198.51.100.7", xff="6.6.6.6")
    assert app_mod._client_ip(req) == "198.51.100.7"
    # Loopback with no header (curl on the host) and a missing client scope degrade sanely.
    assert app_mod._client_ip(_fake_request("127.0.0.1")) == "127.0.0.1"
    assert app_mod._client_ip(_fake_request(None)) == "unknown"


def test_frame_endpoint_rate_limited_429(client, monkeypatch):
    """The billable frame endpoint refuses the N+1th request in a minute (H-8)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    payload = _base()
    for _ in range(app_mod._FRAME_RATE_PER_MIN):
        resp = client.post("/v1/briefing/frame", json=payload)
        assert resp.status_code in {204, 404}  # within budget: normal handling
    resp = client.post("/v1/briefing/frame", json=payload)
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1


def test_pdf_endpoint_rate_limited_429(client):
    """PDF renders are throttled before the body is even read (H-8)."""
    for _ in range(app_mod._PDF_RATE_PER_MIN):
        resp = client.post("/v1/briefing/pdf", json={})  # invalid body -> 422, but counted
        assert resp.status_code == 422
    resp = client.post("/v1/briefing/pdf", json={})
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


def test_warm_endpoint_rate_limited_429(client, monkeypatch):
    monkeypatch.setattr(service, "warm_watershed", lambda lat, lon: False)
    payload = {"lat": 37.0192, "lon": -111.9889}
    for _ in range(app_mod._WARM_RATE_PER_MIN):
        assert client.post("/v1/watershed/warm", json=payload).status_code == 202
    resp = client.post("/v1/watershed/warm", json=payload)
    assert resp.status_code == 429


def test_rate_limits_disabled_by_setting(client, monkeypatch):
    """UPSTREAMWX_API_RATE_LIMITS_ENABLED=0 turns the limiter off (checked per request)."""
    monkeypatch.setenv("UPSTREAMWX_API_RATE_LIMITS_ENABLED", "0")
    monkeypatch.setattr(service, "warm_watershed", lambda lat, lon: False)
    payload = {"lat": 37.0192, "lon": -111.9889}
    for _ in range(app_mod._WARM_RATE_PER_MIN * 2):
        assert client.post("/v1/watershed/warm", json=payload).status_code == 202


def test_briefing_endpoint_not_rate_limited(client):
    """/v1/briefing stays governed by _gen_sem/BriefingBusy only — cache hits stay cheap."""
    payload = _base()
    for _ in range(20):  # far past any per-minute budget; almost all are cache hits
        assert client.post("/v1/briefing", json=payload).status_code == 200


# -- structural validation is also enforced at model construction -------------------------
def test_mission_spec_constructor_rejects_bad_windows():
    with pytest.raises(ValidationError):
        MissionSpec(**_base(start="2026-06-20T18:00", end="2026-06-20T08:00"))
    with pytest.raises(ValidationError):
        MissionSpec(**_base(end="2026-06-30T08:00"))  # > 7 days
    with pytest.raises(ValidationError):
        MissionSpec(**_base(lat=23.0))
    with pytest.raises(ValidationError):
        MissionSpec(**_base(lightning_radius_km=MAX_RADIUS_KM + 1))
