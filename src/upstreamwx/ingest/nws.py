"""NWS API adapter — active alerts + Area Forecast Discussion (FR-5).

The AFD forecaster discussion is available from no other source and is mandatory;
active watches/warnings anchor the near-term flood and lightning postures. All
calls hit ``api.weather.gov`` and require a self-identifying User-Agent.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

from ..config import get_settings
from ..engine.models import Mission
from .base import IngestBundle

API = "https://api.weather.gov"
NAME = "nws"

# The NWS office (CWA) serving a point is static, so cache it process-wide keyed by rounded
# lat/lon — this removes one of the AFD chain's three serial round-trips on every warm call.
# Only successful lookups are cached (a transient failure must retry, not pin a point to None).
_OFFICE_PRECISION = 3
_office_cache: dict[tuple[float, float], str] = {}
_office_lock = threading.Lock()

# Convective language the AFD scan treats as an exposed-phase lightning signal.
_CONVECTIVE_RE = re.compile(
    r"\b(thunderstorm|convection|convective|lightning|isolated|scattered)\b", re.I
)

# Flood/heavy-rain language the AFD scan treats as a flood signal. The forecaster
# discussion routinely flags excessive-rainfall potential ahead of (or alongside)
# any issued product, so a hit raises the flood posture (§16.1). Intentionally a
# coarse positive flag — same philosophy as the convective scan above.
_FLOOD_RE = re.compile(
    r"\b("
    r"excessive rainfall|flash flood(?:ing)?|"
    r"heavy rain(?:fall)?|torrential|training (?:cells|storms|echoes|convection)|"
    r"rainfall rates?|flood(?:ing)? (?:threat|concern|potential|possible|likely|risk)|"
    r"flash flood guidance|ffg"
    r")\b",
    re.I,
)


def _headers() -> dict[str, str]:
    return {"User-Agent": get_settings().nws_user_agent, "Accept": "application/geo+json"}


def _get(url: str, *, timeout: float = 30.0, accept_geojson: bool = True) -> dict:
    headers = _headers()
    if not accept_geojson:
        headers["Accept"] = "application/ld+json"
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def active_alerts(lat: float, lon: float, *, timeout: float = 30.0) -> list[str]:
    """Return active alert event names for the point (e.g. 'Flash Flood Warning')."""
    data = _get(f"{API}/alerts/active?point={lat},{lon}", timeout=timeout)
    return [
        f["properties"]["event"]
        for f in data.get("features", [])
        if f.get("properties", {}).get("event")
    ]


def _office_for(lat: float, lon: float, *, timeout: float = 30.0) -> str | None:
    """Resolve (and cache) the NWS office/CWA serving a point via ``/points`` (FR-5)."""
    key = (round(lat, _OFFICE_PRECISION), round(lon, _OFFICE_PRECISION))
    with _office_lock:
        cached = _office_cache.get(key)
    if cached is not None:
        return cached
    point = _get(f"{API}/points/{key[0]},{key[1]}", timeout=timeout, accept_geojson=False)
    office = point.get("cwa") or point.get("gridId")
    if office:
        with _office_lock:
            _office_cache[key] = office
    return office


def latest_afd(lat: float, lon: float, *, timeout: float = 30.0) -> str | None:
    """Fetch the latest AFD text for the office serving the point, if available."""
    office = _office_for(lat, lon, timeout=timeout)
    if not office:
        return None
    listing = _get(
        f"{API}/products/types/AFD/locations/{office}", timeout=timeout, accept_geojson=False
    )
    products = listing.get("@graph") or listing.get("graph") or []
    if not products:
        return None
    product = _get(products[0]["@id"], timeout=timeout, accept_geojson=False)
    return product.get("productText")


def fetch(mission: Mission, bundle: IngestBundle) -> None:
    """Populate NWS product flags + AFD convective mention on the bundle.

    The active-alerts query and the AFD chain are independent, so they run concurrently —
    NWS is the slowest point provider (four serial round-trips), and it is never response-
    cached, so overlapping the two chains shaves the alerts round-trip off the critical path.
    """
    with ThreadPoolExecutor(max_workers=2) as executor:
        alerts_future = executor.submit(active_alerts, mission.lat, mission.lon)
        afd_future = executor.submit(latest_afd, mission.lat, mission.lon)
        events = alerts_future.result()
        afd = afd_future.result()
    lowered = [e.lower() for e in events]
    bundle.flash_flood_warning = any("flash flood warning" in e for e in lowered)
    bundle.flash_flood_watch = any("flash flood watch" in e for e in lowered)
    bundle.thunderstorm_warning = any("thunderstorm warning" in e for e in lowered)

    # Areal/river flood products. Match the generic "Flood ..." events but exclude
    # the flash-flood family (handled above) and coastal flooding (not an upstream
    # drainage hazard) so neither double-counts nor falsely fires.
    def _flood_event(kind: str) -> bool:
        return any(
            f"flood {kind}" in e and "flash" not in e and "coastal" not in e
            for e in lowered
        )

    bundle.flood_warning = _flood_event("warning")
    bundle.flood_advisory = _flood_event("advisory")
    bundle.flood_watch = _flood_event("watch")

    bundle.afd_text = afd
    bundle.afd_convective_mention = bool(afd and _CONVECTIVE_RE.search(afd))
    bundle.afd_flood_mention = bool(afd and _FLOOD_RE.search(afd))
    bundle.sources_ok[NAME] = True
