"""Open-Meteo adapter — derived numerical forecast fields (FR-6).

Open-Meteo serves HRRR-derived output as JSON and is free for non-commercial use.
We pull apparent temperature, precipitation, CAPE, and wind over the mission
window in US units, then reduce per the hazard that needs them: heat takes the
window max apparent temperature; cold/wet takes the egress-relevant minimum.
"""

from __future__ import annotations

import time
from datetime import datetime

import requests

from ..engine.models import Mission
from .base import IngestBundle

API = "https://api.open-meteo.com/v1/forecast"
NAME = "open_meteo"

_HOURLY = "temperature_2m,apparent_temperature,precipitation,cape,wind_speed_10m"
# A measurable-precip / antecedent-wetness floor (inches over a window).
_MEASURABLE_IN = 0.01
_ANTECEDENT_IN = 0.25

# Open-Meteo is a free best-effort endpoint that intermittently 5xx's or drops the
# connection; retry transient failures with exponential backoff before degrading (NFR-6).
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_S = 0.5


def _is_transient(exc: requests.exceptions.RequestException) -> bool:
    """Transient = worth retrying: connection/timeout, or HTTP 429 / 5xx."""
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        return status == 429 or status >= 500  # client 4xx (other than 429) is permanent
    return True  # ConnectionError, Timeout, etc.


def _query(lat: float, lon: float, *, timeout: float = 30.0, attempts: int = _MAX_ATTEMPTS) -> dict:
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": _HOURLY,
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "past_days": 3,
        "forecast_days": 3,
    }
    last_exc: requests.exceptions.RequestException | None = None
    for attempt in range(attempts):
        try:
            resp = requests.get(API, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as exc:
            if not _is_transient(exc):
                raise
            last_exc = exc
            if attempt + 1 < attempts:
                time.sleep(_BACKOFF_BASE_S * (2**attempt))
    assert last_exc is not None  # loop ran at least once
    raise last_exc


def _in_window(times: list[str], start: datetime, end: datetime) -> list[int]:
    idx = []
    for i, t in enumerate(times):
        ts = datetime.fromisoformat(t)
        if start <= ts <= end:
            idx.append(i)
    return idx


def fetch(mission: Mission, bundle: IngestBundle) -> None:
    """Populate derived thermal / precip fields on the bundle for the window."""
    data = _query(mission.lat, mission.lon)
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    apparent = hourly.get("apparent_temperature", [])
    precip = hourly.get("precipitation", [])

    window = _in_window(times, mission.window_start, mission.window_end)
    if window:
        app_vals = [apparent[i] for i in window if apparent[i] is not None]
        if app_vals:
            # Heat uses the hottest hour; cold/wet uses the coldest (egress-relevant).
            bundle.heat_index_f = max(app_vals)
            bundle.apparent_temp_f = min(app_vals)
        win_precip = sum(precip[i] for i in window if precip[i] is not None)
        bundle.measurable_precip = win_precip >= _MEASURABLE_IN

    # Antecedent wetness: significant precip in the hours before the window.
    prior = [i for i, t in enumerate(times) if datetime.fromisoformat(t) < mission.window_start]
    prior_precip = sum(precip[i] for i in prior[-72:] if precip[i] is not None)
    bundle.antecedent_precip_24_72h = prior_precip >= _ANTECEDENT_IN

    bundle.sources_ok[NAME] = True
