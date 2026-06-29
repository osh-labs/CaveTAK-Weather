"""Structured-JSON serializer: :class:`GeneratedBriefing` -> the PWA contract (M0.4).

The PWA (``frontend/``) renders five views from a structured briefing object, not from
the Markdown SITREP. This module maps the deterministic engine result
(:class:`~upstreamwx.engine.models.BriefingResult`) plus the ingest
:class:`~upstreamwx.ingest.base.IngestBundle` onto exactly the JSON shape the frontend
consumes (the committed ``frontend/data/sample-briefing.json`` is the frozen contract).

It is the API analogue of :mod:`upstreamwx.sitrep.render` (which produces Markdown): pure,
deterministic, and never a place a posture is decided — every tier/category/confidence is
taken verbatim from the engine result (FR-13, FR-20, NFR-4). Display-only fields (the
hourly forecast table and charts, metric cards) come from the bundle and never feed back
into the engine.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from shapely.geometry import mapping

from ..engine.models import (
    BriefingResult,
    Hazard,
    HazardPosture,
    HeatCategory,
    Phase,
    PhaseAssessment,
    Tier,
)
from ..ingest.base import IngestBundle
from ..watershed import PourpointBasin, UpstreamTrace
from .frame import _INSERT_BEFORE, _SUMMARY_HEADING
from .hazard_copy import HAZARD_LOGIC
from .sources import build_source_links

if TYPE_CHECKING:
    from .generate import GeneratedBriefing

# Fixed cross-view hazard ordering and labels (matches render.py / the frontend).
_HAZARD_ORDER: tuple[Hazard, ...] = (
    Hazard.FLASH_FLOOD,
    Hazard.LIGHTNING,
    Hazard.HEAT,
    Hazard.COLD_WET,
)
_HAZARD_LABEL: dict[Hazard, str] = {
    Hazard.FLASH_FLOOD: "Flash flood",
    Hazard.LIGHTNING: "Lightning",
    Hazard.HEAT: "Heat",
    Hazard.COLD_WET: "Cold/wet",
}
# The Hazards-view timeline header is fixed to these three phases, in this order.
_PHASE_SEQ: tuple[Phase, ...] = (Phase.APPROACH, Phase.TECHNICAL, Phase.EGRESS)
_KM2_TO_SQ_MI = 0.386102


# ---- small field helpers --------------------------------------------------------------
def severity_class(posture: HazardPosture) -> str:
    """CSS posture-chip class: ``sev-*`` for the Tier ladder, ``heat-*`` for heat."""
    if posture.hazard is Hazard.HEAT and posture.heat_category is not None:
        if posture.heat_category is HeatCategory.NONE:
            return "sev-minimal"
        return f"heat-{posture.heat_category.name.lower()}"
    tier = posture.tier if posture.tier is not None else Tier.MINIMAL
    return f"sev-{tier.name.lower()}"


def _severity_token(posture: HazardPosture) -> str:
    """Lowercase severity token for the timeline bar class (``bar-{token}``)."""
    if posture.hazard is Hazard.HEAT and posture.heat_category is not None:
        if posture.heat_category is HeatCategory.NONE:
            return "minimal"
        return posture.heat_category.name.lower()
    tier = posture.tier if posture.tier is not None else Tier.MINIMAL
    return tier.name.lower()


def _rank(posture: HazardPosture) -> int:
    """Comparable severity rank so a phase's lead hazard can be chosen."""
    if posture.hazard is Hazard.HEAT and posture.heat_category is not None:
        return posture.heat_category.value
    return posture.tier.value if posture.tier is not None else 0


def _fmt_window(window: tuple[datetime, datetime] | None) -> str | None:
    """Local ``HHMM–HHMM`` clock window, or None for a persistent hazard."""
    if window is None:
        return None
    return f"{window[0]:%H%M}–{window[1]:%H%M}"


def _tz_label(dt: datetime) -> str:
    """Short display timezone label for a window datetime (e.g. ``MDT``); ``UTC`` if naive.

    The window is localized to the mission's own zone at the request boundary
    (:mod:`upstreamwx.timezones`), so the abbreviation reflects the trip location and
    its DST state on the window date — not the server's clock.
    """
    return dt.tzname() or "UTC"


def _tz_name(dt: datetime) -> str | None:
    """IANA zone key for the window datetime, so the PWA can format in the mission's
    local time regardless of the viewer's browser timezone (FR-9). None if unzoned."""
    return getattr(dt.tzinfo, "key", None)


def _area_sq_mi(area_km2: float) -> float:
    return round(area_km2 * _KM2_TO_SQ_MI, 1)


def _huc_ids(upstream: UpstreamTrace | PourpointBasin | None) -> list[str]:
    if isinstance(upstream, UpstreamTrace):
        return list(upstream.upstream_huc_ids)
    return []  # pour-point delineation carries no HUC-12 id list


# ---- section builders -----------------------------------------------------------------
def _bluf(result: BriefingResult) -> list[dict]:
    out: list[dict] = []
    for hazard in _HAZARD_ORDER:
        p = result.bluf.get(hazard)
        if p is None:
            continue
        out.append(
            {
                "hazard": hazard.value,
                "label": p.severity_label,
                "severity_class": severity_class(p),
                "confidence": p.confidence.label if p.confidence is not None else None,
                "window": _fmt_window(p.window_of_concern),
                "is_persistent": p.window_of_concern is None,
            }
        )
    return out


def _phase(pa: PhaseAssessment) -> dict:
    lead = max(
        (pa.postures[h] for h in pa.applicable if h in pa.postures),
        key=_rank,
        default=None,
    )
    lead_label = (
        f"{_HAZARD_LABEL[lead.hazard]} — {lead.severity_label}" if lead is not None else None
    )
    applicable = ", ".join(
        _HAZARD_LABEL[h] + (" ↑" if h is Hazard.HEAT else "") for h in pa.applicable
    )
    return {
        "phase": pa.phase.value,
        "window": _fmt_window(pa.window),
        "thermal_primary": pa.thermal_primary.value if pa.thermal_primary else None,
        "lead_label": lead_label,
        "applicable": applicable,
        "note": " ".join(pa.notes) if pa.notes else None,
    }


def _timeline(result: BriefingResult) -> list[dict]:
    by_phase = {pa.phase: pa for pa in result.phases}
    rows: list[dict] = []
    for hazard in _HAZARD_ORDER:
        cells: list[dict] = []
        for phase in _PHASE_SEQ:
            pa = by_phase.get(phase)
            if pa is None or hazard not in pa.applicable:
                cells.append({"phase": phase.value, "applicable": False})
                continue
            p = pa.postures.get(hazard)
            cells.append(
                {
                    "phase": phase.value,
                    "severity": _severity_token(p) if p else "minimal",
                    "confidence": p.confidence.label.lower() if p and p.confidence else "moderate",
                    "persistent": bool(p and p.window_of_concern is None),
                }
            )
        rows.append({"hazard": hazard.value, "cells": cells})
    return rows


def _hazard_detail(result: BriefingResult) -> list[dict]:
    out: list[dict] = []
    for hazard in _HAZARD_ORDER:
        p = result.bluf.get(hazard)
        if p is None:
            continue
        out.append(
            {
                "hazard": hazard.value,
                "label": p.severity_label,
                "severity_class": severity_class(p),
                "confidence": p.confidence.label if p.confidence is not None else "Moderate",
                "drivers": list(p.drivers),
                "logic": HAZARD_LOGIC.get(hazard, ""),
                "assumptions": list(p.notes),
            }
        )
    return out


_KM_PER_MI = 1.609344


def _watershed(
    upstream: UpstreamTrace | PourpointBasin | None, bundle: IngestBundle | None = None
) -> dict | None:
    if upstream is None:
        return None
    # When a Radius of Concern clipped the basin, surface the *clipped* geometry and area
    # (the domain that actually fed the aggregation, FR-3) plus the excluded remainder so
    # the PWA can hatch it; otherwise the full delineated watershed.
    clipped = bundle is not None and bundle.roc_radius_km and bundle.aggregation_polygon is not None
    if clipped:
        geometry = mapping(bundle.aggregation_polygon)
        area_km2 = (
            bundle.roc_kept_area_km2
            if bundle.roc_kept_area_km2 is not None
            else upstream.area_km2
        )
    else:
        geometry = mapping(upstream.polygon)
        area_km2 = upstream.area_km2
    excluded = bundle.roc_excluded if bundle is not None else None
    return {
        "huc12": _huc_ids(upstream),
        "area_sq_mi": _area_sq_mi(area_km2),
        "geometry": geometry,
        "excluded_geometry": mapping(excluded) if excluded is not None else None,
    }


def _roc(bundle: IngestBundle | None, mission) -> dict | None:
    """The Radius-of-Concern ring (FR-3): center + radius + disk geometry, or None."""
    if bundle is None or not bundle.roc_radius_km or bundle.roc_disk is None:
        return None
    return {
        "radius_km": round(bundle.roc_radius_km, 3),
        "radius_mi": round(bundle.roc_radius_km / _KM_PER_MI, 1),
        "center": [mission.lon, mission.lat],  # GeoJSON order (lon, lat)
        "geometry": mapping(bundle.roc_disk),
    }


def _laoc(bundle: IngestBundle | None, mission) -> dict | None:
    """The Lightning-Area-of-Concern ring (PRD §16.1): center + radius + disk geometry, or None.

    The disk the lightning ensemble fields aggregated over — the PWA renders it as a yellow
    ring distinct from the orange RoC. None unless the mission set a lightning radius.
    """
    if bundle is None or not bundle.laoc_radius_km or bundle.laoc_disk is None:
        return None
    return {
        "radius_km": round(bundle.laoc_radius_km, 3),
        "radius_mi": round(bundle.laoc_radius_km / _KM_PER_MI, 1),
        "center": [mission.lon, mission.lat],  # GeoJSON order (lon, lat)
        "geometry": mapping(bundle.laoc_disk),
    }


def _metrics(bundle: IngestBundle | None) -> list[dict]:
    fh = bundle.forecast_hourly if bundle is not None else None

    def mx(arr: list | None) -> float | None:
        vals = [v for v in (arr or []) if v is not None]
        return max(vals) if vals else None

    def sm(arr: list | None) -> float | None:
        vals = [v for v in (arr or []) if v is not None]
        return sum(vals) if vals else None

    def s(v: float | None, fmt: str = "{:.0f}") -> str:
        return "n/a" if v is None else fmt.format(v)

    temp = mx(fh.temp_f) if fh else None
    feels = mx(fh.feels_f) if fh else None
    wind = mx(fh.wind_mph) if fh else None
    gust = mx(fh.gust_mph) if fh else None
    precip = mx(fh.precip_pct) if fh else None
    qpf = sm(fh.qpf_in) if fh else None
    tstm = bundle.gefs_p_tstm if bundle is not None else None
    def card(label: str, icon: str, value: str, unit: str, sub: str) -> dict:
        return {"label": label, "icon": icon, "value": value, "unit": unit, "sub": sub}

    return [
        card("Temp", "heat", s(temp), "°F", f"Feels {s(feels)}°"),
        card("Wind", "cold_wet", s(wind), "mph", f"Gust {s(gust)}"),
        card("Precip", "flash_flood", s(precip), "%", f"{s(qpf, '{:.1f}')} in"),
        card("T-storm", "lightning", s(tstm), "%", "GEFS P(tstm)"),
    ]


def _risk_inputs(bundle: IngestBundle | None) -> dict:
    """Scalar engine-input fields for the Forecast view's Risk Analysis section (FR-20).

    These are the raw probability and physical-parameter inputs the deterministic engine
    reads to decide hazard tiers. Displaying them in the Forecast view lets users verify
    the engine's reasoning against the drivers shown in the Hazards view. Display-only —
    never re-read by the engine, never changes a posture (FR-13, NFR-4).
    """
    if bundle is None:
        return {}

    def pct(v: float | None) -> int | None:
        return round(v) if v is not None else None

    return {
        "gefs_p_precip": pct(bundle.gefs_p_precip),
        "gefs_p_tstm": pct(bundle.gefs_p_tstm),
        "refs_in_range": bundle.refs_in_range,
        "refs_p_precip": pct(bundle.refs_p_precip) if bundle.refs_in_range else None,
        "refs_p_lightning": pct(bundle.refs_p_lightning) if bundle.refs_in_range else None,
        "refs_cycle": bundle.refs_cycle,
        "cape_jkg": round(bundle.cape_jkg) if bundle.cape_jkg is not None else None,
        "convective_rate_in_per_hr": (
            round(bundle.convective_rate_in_per_hr, 3)
            if bundle.convective_rate_in_per_hr is not None
            else None
        ),
        "spc_category": bundle.spc_category,
        "flash_flood_warning": bundle.flash_flood_warning,
        "flash_flood_watch": bundle.flash_flood_watch,
        "flood_watch": bundle.flood_watch,
        "thunderstorm_warning": bundle.thunderstorm_warning,
    }


def _forecast(bundle: IngestBundle | None) -> tuple[dict, dict, dict]:
    """Return (forecast_hourly table, temp_series, wind_series); empty under degradation."""
    fh = bundle.forecast_hourly if bundle is not None else None
    if fh is None:
        return {"hours": [], "rows": []}, {"air": [], "feels": []}, {"wind": [], "gust": []}

    def row(label: str, arr: list, fmt: str = "{:.0f}") -> dict:
        return {"label": label, "values": ["" if v is None else fmt.format(v) for v in arr]}

    forecast_hourly = {
        "hours": list(fh.hours),
        "rows": [
            {"label": "Sky", "values": list(fh.sky)},
            row("Temp °F", fh.temp_f),
            row("Feels °F", fh.feels_f),
            row("Wind mph", fh.wind_mph),
            row("Gust mph", fh.gust_mph),
            row("Precip %", fh.precip_pct),
            row("QPF in", fh.qpf_in, "{:.1f}"),
        ],
    }
    temp_series = {"air": list(fh.temp_f), "feels": list(fh.feels_f)}
    wind_series = {"wind": list(fh.wind_mph), "gust": list(fh.gust_mph)}
    return forecast_hourly, temp_series, wind_series


def _resources(lat: float, lon: float, threshold_version: str, *, used_refs: bool) -> list[dict]:
    links = build_source_links(lat, lon, used_refs=used_refs)
    model_sub = "Open-Meteo (HRRR-derived) · GEFS (member exceedance)" + (
        " + REFS (3 km enspost NEP, same-day)" if used_refs else ""
    )
    return [
        {
            "icon": "doc",
            "title": "NWS Area Forecast Discussion",
            "sub": "Verify forecaster reasoning",
            "url": links.nws_point_forecast,
        },
        {
            "icon": "alert",
            "title": "Active alerts (watches / warnings)",
            "sub": "api.weather.gov active alerts for the point",
            "url": links.active_alerts,
        },
        {
            "icon": "model",
            "title": "Model & ensemble source",
            "sub": model_sub,
            "url": links.gefs_model,
        },
        {
            "icon": "calc",
            "title": "How this is calculated",
            "sub": f"Versioned threshold matrices — {threshold_version}",
            "url": "#",
        },
    ]


def _summary(markdown: str, framed: bool) -> str | None:
    """Pull the Haiku SUMMARY prose out of the framed Markdown (None when not framed)."""
    if not framed:
        return None
    start = markdown.find(_SUMMARY_HEADING)
    if start == -1:
        return None
    end = markdown.find(_INSERT_BEFORE, start)
    if end == -1:
        return None
    text = markdown[start + len(_SUMMARY_HEADING) : end].strip()
    return text or None


def to_structured(gen: GeneratedBriefing, *, cached: bool, cache_cycle: str) -> dict:
    """Map a generated briefing onto the PWA's structured JSON contract (M0.4).

    ``cached``/``cache_cycle`` are the service's cache provenance. The returned dict
    covers every :class:`~upstreamwx.api.models.BriefingResponse` field including
    ``markdown`` (the full Markdown SITREP used by the Briefing tab in the PWA).
    """
    result = gen.result
    bundle = gen.bundle
    mission = result.mission
    upstream = bundle.upstream if bundle is not None else None
    used_refs = bool(bundle is not None and bundle.refs_in_range)

    forecast_hourly, temp_series, wind_series = _forecast(bundle)
    risk_inputs = _risk_inputs(bundle)
    return {
        "markdown": gen.markdown,
        "mission": {
            "name": mission.name,
            "activity": mission.activity_type.value,
            "is_slot": mission.is_slot,
            "lat": mission.lat,
            "lon": mission.lon,
            "radius_km": mission.radius_km,
            "huc12": _huc_ids(upstream),
            "window_start": mission.window_start.isoformat(),
            "window_end": mission.window_end.isoformat(),
            "phases_inferred": result.phases_inferred,
            "timezone": _tz_label(mission.window_start),
            "tz_name": _tz_name(mission.window_start),
        },
        "watershed": _watershed(upstream, bundle),
        "roc": _roc(bundle, mission),
        "laoc": _laoc(bundle, mission),
        "overall_posture": result.overall_tier.label,
        "overall_confidence": result.overall_confidence.label,
        "threshold_version": result.threshold_version,
        "generated_at": gen.generated_at,
        "framed": gen.framed,
        "cached": cached,
        "cache_cycle": cache_cycle,
        "degraded": gen.degraded,
        "sources_ok": gen.sources_ok,
        "warnings": list(gen.warnings),
        "summary": _summary(gen.markdown, gen.framed),
        "bluf": _bluf(result),
        "metrics": _metrics(bundle),
        "phases": [_phase(pa) for pa in result.phases],
        "timeline": _timeline(result),
        "hazard_detail": _hazard_detail(result),
        "forecast_hourly": forecast_hourly,
        "temp_series": temp_series,
        "wind_series": wind_series,
        "risk_inputs": risk_inputs,
        "resources": _resources(
            mission.lat, mission.lon, result.threshold_version, used_refs=used_refs
        ),
    }
