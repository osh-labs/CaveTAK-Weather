"""Open-Meteo adapter — derived numerical forecast fields (FR-6).

Open-Meteo serves HRRR-derived output as JSON and is free for non-commercial use.
We pull apparent temperature, precipitation, CAPE, and wind over the mission
window in US units, then reduce per the hazard that needs them: heat takes the
window max apparent temperature; cold/wet takes the egress-relevant minimum.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import requests

from ..engine.models import Mission
from .base import ForecastHourly, IngestBundle

API = "https://api.open-meteo.com/v1/forecast"
NAME = "open_meteo"

# Engine-feeding fields plus the extra display series the Forecast view renders (FR-6).
# Display additions (gusts / precip probability / weather code) do not feed the engine.
_HOURLY = (
    "temperature_2m,apparent_temperature,precipitation,precipitation_probability,"
    "cape,wind_speed_10m,wind_gusts_10m,weather_code"
)
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
        # Cover the full planning horizon the ensembles serve (GEFS 0.25° reaches f240 =
        # 10 days). At 3 days, a day-4 mission silently read as "dry"/no-thermal-data and
        # the missing `measurable_precip` gated the GEFS Elevated flood band off.
        "forecast_days": 16,
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


def _to_utc_naive(dt: datetime) -> datetime:
    """Return a UTC-naive datetime for comparing with Open-Meteo's naive UTC times."""
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _in_window(times: list[str], start: datetime, end: datetime) -> list[int]:
    start_cmp = _to_utc_naive(start)
    end_cmp   = _to_utc_naive(end)
    idx = []
    for i, t in enumerate(times):
        ts = datetime.fromisoformat(t)
        if start_cmp <= ts <= end_cmp:
            idx.append(i)
    return idx


# WMO weather-code -> sky emoji for the Forecast-view table (display only). Ranges map
# the standard WMO 4677 code groups (clear / cloud / fog / drizzle-rain / snow / thunder).
def _sky_emoji(code: float | None) -> str:
    if code is None:
        return "—"
    c = int(code)
    if c == 0:
        return "☀️"
    if c in (1, 2):
        return "⛅"
    if c == 3:
        return "☁️"
    if c in (45, 48):
        return "🌫️"
    if c in (51, 53, 55, 56, 57, 80):
        return "🌦️"
    if c in (61, 63, 65, 66, 67, 81, 82):
        return "🌧️"
    if c in (71, 73, 75, 77, 85, 86):
        return "🌨️"
    if c in (95, 96, 99):
        return "⛈️"
    return "⛅"


def _build_forecast_hourly(mission: Mission, hourly: dict, window: list[int]) -> ForecastHourly:
    """Assemble the per-hour display series for the window, localized to the mission tz."""
    offset = mission.window_start.utcoffset() or timedelta(0)

    def col(name: str) -> list:
        return hourly.get(name, [])

    times, temp = col("time"), col("temperature_2m")
    feels, wind = col("apparent_temperature"), col("wind_speed_10m")
    gust, ppct = col("wind_gusts_10m"), col("precipitation_probability")
    qpf, wcode = col("precipitation"), col("weather_code")

    def at(arr: list, i: int):
        return arr[i] if i < len(arr) else None

    hours: list[str] = []
    for i in window:
        local = datetime.fromisoformat(times[i]) + offset  # times are naive UTC
        hours.append(local.strftime("%H%M"))
    return ForecastHourly(
        hours=hours,
        temp_f=[at(temp, i) for i in window],
        feels_f=[at(feels, i) for i in window],
        wind_mph=[at(wind, i) for i in window],
        gust_mph=[at(gust, i) for i in window],
        precip_pct=[at(ppct, i) for i in window],
        qpf_in=[at(qpf, i) for i in window],
        sky=[_sky_emoji(at(wcode, i)) for i in window],
    )


def fetch(mission: Mission, bundle: IngestBundle) -> None:
    """Populate derived thermal / precip / convective fields on the bundle for the window.

    Coverage is first-class: the fields are only set from hours the fetched series actually
    covers, and a window the series does not (fully) cover leaves the precip booleans ``None``
    ("unknown") instead of a ``False`` that downstream reads as "dry" — a missing value must
    never present as a benign one (NFR-6).
    """
    data = _query(mission.lat, mission.lon)
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    apparent = hourly.get("apparent_temperature", [])
    precip = hourly.get("precipitation", [])
    cape = hourly.get("cape", [])
    wind = hourly.get("wind_speed_10m", [])

    window = _in_window(times, mission.window_start, mission.window_end)
    covered = _window_covered(times, mission.window_start, mission.window_end)
    if window:
        app_vals = [apparent[i] for i in window if i < len(apparent) and apparent[i] is not None]
        if app_vals:
            # Heat uses the hottest hour; cold/wet uses the coldest (egress-relevant).
            bundle.heat_index_f = max(app_vals)
            bundle.apparent_temp_f = min(app_vals)
        cape_vals = [cape[i] for i in window if i < len(cape) and cape[i] is not None]
        if cape_vals:
            bundle.cape_jkg = max(cape_vals)  # lightning instability context (§16.2)
        wind_vals = [wind[i] for i in window if i < len(wind) and wind[i] is not None]
        if wind_vals:
            bundle.wind_mph = max(wind_vals)
        precip_vals = [precip[i] for i in window if i < len(precip) and precip[i] is not None]
        # Hourly QPF is inch over one hour, i.e. an in/hr rate: its window max is the
        # forecast convective rate the slot-canyon fallback reads (§16.1 slot modifier).
        if precip_vals:
            bundle.convective_rate_in_per_hr = max(precip_vals)
        win_precip = sum(precip_vals)
        if win_precip >= _MEASURABLE_IN:
            bundle.measurable_precip = True
        else:
            # A dry *covered* window is a real False; a partially covered one is unknown —
            # the uncovered remainder could hold the precip the Elevated flood band gates on.
            bundle.measurable_precip = False if covered else None
        # Display series for the PWA Forecast view (does not feed the engine).
        bundle.forecast_hourly = _build_forecast_hourly(mission, hourly, window)
    else:
        bundle.measurable_precip = None
        bundle.notes.append(
            "open_meteo: mission window outside the fetched forecast range; "
            "thermal/precip fields unavailable."
        )
    if window and not covered:
        bundle.notes.append(
            "open_meteo: forecast series only partially covers the mission window; "
            "thermal/precip fields reflect the covered hours only."
        )

    # Antecedent wetness: significant precip in the hours before the window. Same asymmetry:
    # observed wetness is a real True; "no wetness seen" is only a real False when the prior
    # hours were actually in the fetched series.
    start_cmp = _to_utc_naive(mission.window_start)
    prior = [i for i, t in enumerate(times) if datetime.fromisoformat(t) < start_cmp]
    prior_precip = sum(precip[i] for i in prior[-72:] if i < len(precip) and precip[i] is not None)
    if prior_precip >= _ANTECEDENT_IN:
        bundle.antecedent_precip_24_72h = True
    else:
        bundle.antecedent_precip_24_72h = False if prior else None

    bundle.sources_ok[NAME] = True


def _window_covered(times: list[str], start: datetime, end: datetime) -> bool:
    """True when the fetched hourly series spans the whole mission window."""
    if not times:
        return False
    first = datetime.fromisoformat(times[0])
    last = datetime.fromisoformat(times[-1])
    return first <= _to_utc_naive(start) and _to_utc_naive(end) <= last
