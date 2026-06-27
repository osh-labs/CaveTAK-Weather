"""SPC convective outlook adapter — Day 1 categorical risk at a point.

Cross-checks the lightning posture against the Storm Prediction Center categorical
outlook (§16.2). We fetch the Day 1 categorical GeoJSON and locate the mission
point, normalizing the SPC label to the engine's ``spc_category`` vocabulary.
Best-effort: returns no category (graceful) if the product or point lookup fails.
"""

from __future__ import annotations

import threading
from time import monotonic

import requests
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry

from ..engine.models import Mission
from .base import IngestBundle

DAY1_CAT = "https://www.spc.noaa.gov/products/outlook/day1otlk_cat.nolyr.geojson"
NAME = "spc"

# SPC categorical labels -> engine spc_category keys (see lightning.yaml).
_LABEL_MAP = {
    "TSTM": "general_thunder",
    "MRGL": "marginal",
    "SLGT": "slight",
    "ENH": "enhanced",
    "MDT": "moderate",
    "HIGH": "high",
}

# The Day-1 categorical product is identical for every mission and reissues only a handful of
# times a day, so the multi-MB download + per-feature shapely parse is cached process-wide for
# a short TTL and reused across briefings. Tunable; sibling of the in-process decode/mask caches.
_OUTLOOK_TTL_S = 600.0
_outlook_lock = threading.Lock()
# (fetched_at_monotonic, [(category_key, geometry), ...]); None until first load.
_outlook_cache: tuple[float, list[tuple[str, BaseGeometry]]] | None = None


def _label_of(props: dict) -> str | None:
    for key in ("LABEL", "LABEL2", "DN"):
        val = props.get(key)
        if isinstance(val, str) and val.strip().upper() in _LABEL_MAP:
            return val.strip().upper()
    return None


def _download_outlook(timeout: float) -> list[tuple[str, BaseGeometry]]:
    """Download + parse the Day-1 categorical outlook into (category_key, geometry) pairs."""
    resp = requests.get(DAY1_CAT, timeout=timeout)
    resp.raise_for_status()
    features: list[tuple[str, BaseGeometry]] = []
    for feature in resp.json().get("features", []):
        label = _label_of(feature.get("properties", {}))
        geom = feature.get("geometry")
        if label and geom:
            features.append((_LABEL_MAP[label], shape(geom)))
    return features


def _load_outlook(
    *, timeout: float = 30.0, ttl: float = _OUTLOOK_TTL_S
) -> list[tuple[str, BaseGeometry]]:
    """Return the parsed outlook, served from the in-process cache within its TTL.

    Collapses the per-briefing download + shapely parse to one fetch per ``ttl`` (the product
    is mission-independent). On a refresh failure a still-held outlook is served stale rather
    than failing (NFR-6). The download runs outside the lock so concurrent requests don't
    serialize on it; a rare double-download on a simultaneous cold miss is harmless.
    """
    global _outlook_cache
    now = monotonic()
    with _outlook_lock:
        cached = _outlook_cache
    if cached is not None and (now - cached[0]) < ttl:
        return cached[1]
    try:
        features = _download_outlook(timeout)
    except requests.RequestException:
        if cached is not None:
            return cached[1]  # serve the stale outlook rather than fail (NFR-6)
        raise
    with _outlook_lock:
        _outlook_cache = (now, features)
    return features


def category_at(lat: float, lon: float, *, timeout: float = 30.0) -> str | None:
    """Return the normalized SPC category covering the point, or None."""
    point = Point(lon, lat)
    best = None
    best_rank = -1
    ranks = list(_LABEL_MAP.values())  # severity order, low -> high
    for category, geom in _load_outlook(timeout=timeout):
        if geom.contains(point):
            rank = ranks.index(category)
            if rank > best_rank:
                best, best_rank = category, rank
    return best


def fetch(mission: Mission, bundle: IngestBundle) -> None:
    """Populate the SPC categorical risk on the bundle."""
    bundle.spc_category = category_at(mission.lat, mission.lon)
    bundle.sources_ok[NAME] = True
