"""NWS API adapter — active alerts + Area Forecast Discussion (FR-5).

The AFD forecaster discussion is available from no other source and is mandatory;
active watches/warnings anchor the near-term flood and lightning postures. All
calls hit ``api.weather.gov`` and require a self-identifying User-Agent.
"""

from __future__ import annotations

import re

import requests

from ..config import get_settings
from ..engine.models import Mission
from .base import IngestBundle

API = "https://api.weather.gov"
NAME = "nws"

# Convective language the AFD scan treats as an exposed-phase lightning signal.
_CONVECTIVE_RE = re.compile(
    r"\b(thunderstorm|convection|convective|lightning|isolated|scattered)\b", re.I
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


def latest_afd(lat: float, lon: float, *, timeout: float = 30.0) -> str | None:
    """Fetch the latest AFD text for the office serving the point, if available."""
    point = _get(f"{API}/points/{lat},{lon}", timeout=timeout, accept_geojson=False)
    office = point.get("cwa") or point.get("gridId")
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
    """Populate NWS product flags + AFD convective mention on the bundle."""
    events = active_alerts(mission.lat, mission.lon)
    lowered = [e.lower() for e in events]
    bundle.flash_flood_warning = any("flash flood warning" in e for e in lowered)
    bundle.flash_flood_watch = any("flash flood watch" in e for e in lowered)
    bundle.thunderstorm_warning = any("thunderstorm warning" in e for e in lowered)

    afd = latest_afd(mission.lat, mission.lon)
    bundle.afd_text = afd
    bundle.afd_convective_mention = bool(afd and _CONVECTIVE_RE.search(afd))
    bundle.sources_ok[NAME] = True
