"""Deterministic structured renderer: :class:`BriefingResult` -> Markdown (M0.2 stage 1).

Renders the engine's structured output into the Appendix A SITREP skeleton (PRD §15)
as Markdown. This stage is **purely deterministic** (NFR-4): identical inputs yield
byte-identical output, so it is golden-file testable and validated independently of the
LLM framing layer (:mod:`upstreamwx.sitrep.frame`). The language model never runs here
and can never change a posture (FR-20).

Every render carries the reference-only disclaimer (Appendix C, §17.2) and the
verify-against-NWS source links (FR-26, FR-40) from day one.
"""

from __future__ import annotations

from datetime import datetime

from ..engine.models import (
    BriefingResult,
    Hazard,
    HazardPosture,
    Phase,
    PhaseAssessment,
)
from ..ingest.base import IngestBundle
from ..watershed import PourpointBasin, UpstreamTrace
from .sources import build_source_links

# Reference-only disclaimer (PRD Appendix C §17.2 + the short §15 line). Embedded in
# every render, non-negotiable from day one (FR-31, FR-40).
DISCLAIMER = (
    "Reference only. Not a decision-making tool. Verify against NWS.\n\n"
    "Planning reference only — not a forecast, not a decision. Conditions change fast "
    "and models can be wrong. Verify against the official NWS sources linked above, and "
    "let what you see in the field overrule this briefing. The go/no-go decision is "
    "yours and your party's."
)

# Human-readable hazard names for the briefing (skeleton §15 ordering).
_HAZARD_LABEL: dict[Hazard, str] = {
    Hazard.FLASH_FLOOD: "Flash flood",
    Hazard.LIGHTNING: "Lightning",
    Hazard.HEAT: "Heat",
    Hazard.COLD_WET: "Cold/wet",
}
# Fixed BLUF / drivers ordering across hazards (skeleton §15).
_HAZARD_ORDER: tuple[Hazard, ...] = (
    Hazard.FLASH_FLOOD,
    Hazard.LIGHTNING,
    Hazard.HEAT,
    Hazard.COLD_WET,
)
_PHASE_LABEL: dict[Phase, str] = {
    Phase.APPROACH: "Approach",
    Phase.TECHNICAL: "Technical span",
    Phase.EGRESS: "Egress",
}


def _dt(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def _window(window: tuple[datetime, datetime]) -> str:
    return f"{_dt(window[0])}–{_dt(window[1])}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f}%"


def _num(value: float | None, unit: str) -> str:
    return "n/a" if value is None else f"{value:.0f} {unit}"


def _yn(value: bool) -> str:
    return "yes" if value else "no"


def _posture_window(posture: HazardPosture) -> str:
    if posture.window_of_concern is None:
        return "—"
    return _window(posture.window_of_concern)


def render_md(
    result: BriefingResult,
    *,
    upstream: UpstreamTrace | PourpointBasin | None = None,
    bundle: IngestBundle | None = None,
    generated_at: datetime | None = None,
) -> str:
    """Render a :class:`BriefingResult` to a Markdown SITREP (Appendix A skeleton).

    ``upstream`` supplies the HUC-12 domain for the header and watershed summary;
    ``bundle`` supplies the numeric SOURCE DATA drill-down (including the HREF block
    when in range). Both are optional — the render is complete from the engine result
    alone. ``generated_at`` is the only time-varying input and is rendered in a single
    header line; omit it (the default) for byte-identical golden output.
    """
    mission = result.mission
    used_href = bool(bundle and bundle.href_in_range)
    lines: list[str] = []

    # ---- Header -----------------------------------------------------------------
    lines.append("# UPSTREAMWX — MISSION BRIEFING")
    lines.append("")
    lines.append(
        f"**Mission:** {mission.name}  |  "
        f"**Type:** {mission.activity_type.value.capitalize()}  |  "
        f"**Window:** {_window((mission.window_start, mission.window_end))}"
    )
    domain = _domain_label(upstream)
    lines.append(
        f"**Location:** {mission.lat:.4f}, {mission.lon:.4f}  |  **Upstream domain:** {domain}"
    )
    if generated_at is not None:
        lines.append("")
        lines.append(f"_Generated {_dt(generated_at)} UTC · thresholds {result.threshold_version}_")

    # ---- BLUF -------------------------------------------------------------------
    lines.append("")
    lines.append("## BLUF")
    lines.append("")
    lines.append(
        f"**OVERALL POSTURE: {result.overall_tier.label}**  ·  "
        f"Confidence: {result.overall_confidence.label}"
    )
    lines.append("")
    lines.append("| Hazard | Posture | Confidence | Window of concern |")
    lines.append("|---|---|---|---|")
    for hazard in _HAZARD_ORDER:
        posture = result.bluf.get(hazard)
        if posture is None:
            continue
        label = _HAZARD_LABEL[hazard]
        if hazard is Hazard.COLD_WET:
            label += " (assumes wet egress)"
        conf = posture.confidence.label if posture.confidence is not None else "—"
        lines.append(
            f"| {label} | {posture.severity_label} | {conf} | {_posture_window(posture)} |"
        )

    # ---- Phase breakdown --------------------------------------------------------
    lines.append("")
    lines.append("## PHASE BREAKDOWN")
    for phase_assessment in result.phases:
        _render_phase(lines, phase_assessment)

    # ---- Key drivers ------------------------------------------------------------
    lines.append("")
    lines.append("## KEY DRIVERS (per active hazard)")
    for hazard in _HAZARD_ORDER:
        posture = result.bluf.get(hazard)
        if posture is None:
            continue
        lines.append("")
        lines.append(f"### {_HAZARD_LABEL[hazard]}")
        items = list(posture.drivers) + list(posture.notes)
        if items:
            lines.extend(f"- {item}" for item in items)
        else:
            lines.append("- (no signal)")

    # ---- Upstream watershed summary ---------------------------------------------
    lines.append("")
    lines.append("## UPSTREAM WATERSHED SUMMARY")
    lines.append("")
    lines.append(_watershed_summary(result, upstream))

    # ---- Source data drill-down -------------------------------------------------
    lines.append("")
    lines.append("## SOURCE DATA (drill-down)")
    lines.append("")
    lines.append(f"Threshold config: {result.threshold_version}")
    _render_source_data(lines, bundle)

    # ---- Notes ------------------------------------------------------------------
    if result.notes:
        lines.append("")
        lines.append("## NOTES")
        lines.extend(f"- {note}" for note in result.notes)

    # ---- Sources (verify) -------------------------------------------------------
    links = build_source_links(mission.lat, mission.lon, used_href=used_href)
    lines.append("")
    lines.append("## SOURCES (verify)")
    lines.append("")
    lines.append(f"- NWS active alerts: {links.active_alerts}")
    lines.append(f"- NWS point forecast / AFD: {links.nws_point_forecast}")
    lines.append(f"- Model source (SREF): {links.sref_model}")
    if links.href_model is not None:
        lines.append(f"- Model source (HREF, same-day): {links.href_model}")

    # ---- Disclaimer -------------------------------------------------------------
    lines.append("")
    lines.append("## DISCLAIMER")
    lines.append("")
    lines.append(DISCLAIMER)

    return "\n".join(lines) + "\n"


def _render_phase(lines: list[str], phase_assessment: PhaseAssessment) -> None:
    label = _PHASE_LABEL[phase_assessment.phase]
    lines.append("")
    lines.append(f"### {label} ({_window(phase_assessment.window)})")
    if not phase_assessment.applicable:
        lines.append("- (no hazards applicable)")
    for hazard in phase_assessment.applicable:
        posture = phase_assessment.postures.get(hazard)
        severity = posture.severity_label if posture is not None else "n/a"
        suffix = " (primary)" if hazard is phase_assessment.thermal_primary else ""
        lines.append(f"- {_HAZARD_LABEL[hazard]}: {severity}{suffix}")
    for note in phase_assessment.notes:
        lines.append(f"- _{note}_")


def _pourpoint_where(basin: PourpointBasin, *, fallback: str) -> str:
    """Human label for what a pour-point basin drains to (stream name / comid)."""
    if basin.flowline_name:
        return basin.flowline_name
    return f"comid {basin.comid}" if basin.comid else fallback


def _domain_label(upstream: UpstreamTrace | PourpointBasin | None) -> str:
    """One-line upstream-domain label for the header, for either delineation type."""
    if upstream is None:
        return "(not resolved)"
    if isinstance(upstream, UpstreamTrace):
        return (
            f"HUC-12 {upstream.origin_huc12} "
            f"(+{len(upstream.upstream_huc_ids)} upstream, {upstream.area_km2:.0f} km²)"
        )
    where = _pourpoint_where(upstream, fallback="pour point")
    return f"{where} (~{upstream.area_km2:.0f} km², {upstream.method})"


def _watershed_summary(
    result: BriefingResult, upstream: UpstreamTrace | PourpointBasin | None
) -> str:
    if result.upstream_summary:
        return result.upstream_summary
    if upstream is None:
        return "Upstream watershed not resolved for this briefing."
    if isinstance(upstream, UpstreamTrace):
        return (
            f"Flash-flood assessment aggregates over the upstream contributing watershed "
            f"of HUC-12 {upstream.origin_huc12}: {len(upstream.upstream_huc_ids)} upstream "
            f"HUC-12 unit(s), ~{upstream.area_km2:.0f} km², traced via {upstream.method}."
        )
    where = _pourpoint_where(upstream, fallback="the pour point")
    return (
        f"Flash-flood assessment aggregates over the upstream contributing watershed to "
        f"{where}: ~{upstream.area_km2:.0f} km², delineated via {upstream.method}."
    )


def _render_source_data(lines: list[str], bundle: IngestBundle | None) -> None:
    if bundle is None:
        lines.append("")
        lines.append("Source field detail unavailable (rendered from engine result only).")
        return

    lines.append("")
    lines.append("Active NWS products:")
    lines.append(f"- Flash Flood Warning: {_yn(bundle.flash_flood_warning)}")
    lines.append(f"- Flash Flood Watch: {_yn(bundle.flash_flood_watch)}")
    lines.append(f"- Flood Warning: {_yn(bundle.flood_warning)}")
    lines.append(f"- Flood Advisory: {_yn(bundle.flood_advisory)}")
    lines.append(f"- Flood Watch: {_yn(bundle.flood_watch)}")
    lines.append(f"- Thunderstorm Warning: {_yn(bundle.thunderstorm_warning)}")
    lines.append(f"- AFD convective mention: {_yn(bundle.afd_convective_mention)}")
    lines.append(f"- AFD excessive-rain / flood mention: {_yn(bundle.afd_flood_mention)}")
    lines.append(f"- SPC outlook: {bundle.spc_category or 'n/a'}")

    lines.append("")
    lines.append("SREF ensemble (upstream domain):")
    lines.append(f"- P(precip/thunder): {_pct(bundle.sref_p_precip)}")
    lines.append(f"- P(thunderstorm): {_pct(bundle.sref_p_tstm)}")
    lines.append(f"- Convective rate: {_num(bundle.convective_rate_in_per_hr, 'in/hr')}")
    lines.append(f"- CAPE: {_num(bundle.cape_jkg, 'J/kg')}")

    if bundle.href_in_range:
        fhour = bundle.href_fhour or "n/a"
        lines.append("")
        lines.append(f"HREF same-day supplement (cycle {bundle.href_cycle or 'n/a'} {fhour}):")
        lines.append(f"- Neighborhood P(QPF): {_pct(bundle.href_p_precip)}")
        lines.append(f"- Neighborhood P(lightning): {_pct(bundle.href_p_lightning)}")
    lines.append("")
    lines.append(f"Cross-ensemble agreement: {bundle.source_agreement}")

    lines.append("")
    lines.append("Derived fields (Open-Meteo):")
    lines.append(f"- Heat index: {_num(bundle.heat_index_f, '°F')}")
    lines.append(f"- Apparent temp: {_num(bundle.apparent_temp_f, '°F')}")
    lines.append(f"- Wind: {_num(bundle.wind_mph, 'mph')}")
    lines.append(f"- Antecedent precip (24–72 h): {_yn(bundle.antecedent_precip_24_72h)}")

    if bundle.notes:
        lines.append("")
        lines.append("Source availability:")
        lines.extend(f"- {note}" for note in bundle.notes)
