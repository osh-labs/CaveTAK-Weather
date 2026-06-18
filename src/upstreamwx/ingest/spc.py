"""SPC convective outlook adapter — Day 1 categorical risk at a point.

Cross-checks the lightning posture against the Storm Prediction Center categorical
outlook (§16.2). We fetch the Day 1 categorical GeoJSON and locate the mission
point, normalizing the SPC label to the engine's ``spc_category`` vocabulary.
Best-effort: returns no category (graceful) if the product or point lookup fails.
"""

from __future__ import annotations

import requests
from shapely.geometry import Point, shape

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


def _label_of(props: dict) -> str | None:
    for key in ("LABEL", "LABEL2", "DN"):
        val = props.get(key)
        if isinstance(val, str) and val.strip().upper() in _LABEL_MAP:
            return val.strip().upper()
    return None


def category_at(lat: float, lon: float, *, timeout: float = 30.0) -> str | None:
    """Return the normalized SPC category covering the point, or None."""
    resp = requests.get(DAY1_CAT, timeout=timeout)
    resp.raise_for_status()
    point = Point(lon, lat)
    best = None
    best_rank = -1
    ranks = list(_LABEL_MAP)  # severity order, low -> high
    for feature in resp.json().get("features", []):
        label = _label_of(feature.get("properties", {}))
        if not label:
            continue
        geom = feature.get("geometry")
        if geom and shape(geom).contains(point):
            rank = ranks.index(label)
            if rank > best_rank:
                best, best_rank = _LABEL_MAP[label], rank
    return best


def fetch(mission: Mission, bundle: IngestBundle) -> None:
    """Populate the SPC categorical risk on the bundle."""
    bundle.spc_category = category_at(mission.lat, mission.lon)
    bundle.sources_ok[NAME] = True
