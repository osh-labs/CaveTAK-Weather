"""API request/response schema (M0.3).

The request is a mission spec mirroring the CLI arguments, with an optional saved
``HazardInputs`` feature vector for offline/reproducible generation (the corpus path,
FR-25). The response carries the rendered Markdown briefing plus the cache/cycle and
source-availability provenance the PWA (M0.4) needs to show currency and degradation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from ..engine.models import ActivityType, HazardInputs, Mission
from ..timezones import localize_window


def _reject_markup(v: str) -> str:
    """Refuse angle brackets in short display tokens.

    These fields are clock windows / cycle ids the PDF template (FR-27) interpolates into
    HTML; legitimate values never contain markup, so any ``<``/``>`` is a hostile payload
    aimed at the headless-Chromium renderer and is rejected at the model boundary.
    """
    if "<" in v or ">" in v:
        raise ValueError("markup characters are not allowed in this field")
    return v


# A short local clock window ("1400–2100") or hour token ("1300"): bounded and markup-free
# so the PDF template can never be handed HTML where it expects a time string.
ClockToken = Annotated[str, Field(max_length=24), AfterValidator(_reject_markup)]
# A short identifier-ish display token (ensemble cycle id, SPC category).
ShortToken = Annotated[str, Field(max_length=32), AfterValidator(_reject_markup)]


class MissionSpec(BaseModel):
    """A mission briefing request (mirrors the `upstreamwx` CLI flags)."""

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    activity: ActivityType
    start: datetime = Field(description="window start (ISO 8601)")
    end: datetime = Field(description="window end (ISO 8601)")
    name: str = "mission"
    approach_end: datetime | None = Field(default=None, description="phase marker (FR-9a)")
    egress_start: datetime | None = Field(default=None, description="phase marker (FR-9a)")
    party_size: int | None = None
    route_note: str | None = None
    slot: bool = False
    radius_km: float | None = Field(
        default=None,
        ge=1,
        description="Radius of Concern (km): caps the upstream watershed; null = unbounded (FR-3)",
    )
    lightning_radius_km: float | None = Field(
        default=None,
        ge=1,
        description=(
            "Lightning Area of Concern radius (km): aggregate the lightning signal over a disk "
            "around the activity instead of the watershed; null = upstream domain (PRD §16.1)"
        ),
    )
    frame: bool | None = Field(
        default=None,
        description="add Haiku framing; null = frame iff ANTHROPIC_API_KEY is set (FR-21)",
    )
    inputs: dict | None = Field(
        default=None,
        description="optional saved HazardInputs feature vector; skips live ingest (offline)",
    )

    def to_mission(self) -> Mission:
        # The window is entered as local wall-clock time at the trip point; attach the
        # point's IANA zone so the engine's UTC math and the local-time display agree (FR-9).
        start, end, approach_end, egress_start = localize_window(
            self.lat, self.lon, self.start, self.end, self.approach_end, self.egress_start
        )
        return Mission(
            activity_type=self.activity,
            lat=self.lat,
            lon=self.lon,
            window_start=start,
            window_end=end,
            approach_end=approach_end,
            egress_start=egress_start,
            party_size=self.party_size,
            route_note=self.route_note,
            is_slot=self.slot,
            name=self.name,
            radius_km=self.radius_km,
            lightning_radius_km=self.lightning_radius_km,
        )

    def to_inputs(self) -> HazardInputs | None:
        data = self.inputs
        if data is None:
            return None
        if "inputs" in data:  # accept the corpus/CLI {inputs: {...}} envelope too
            data = data["inputs"]
        return HazardInputs(**data)


class WatershedWarmRequest(BaseModel):
    """A request to pre-warm the pour-point watershed cache for a point (FR-3).

    Sent by the mission planner the moment coordinates change so the upstream basin is
    delineated in the background while the user finishes entering the mission. Only the
    point matters: ``radius_km`` is accepted for symmetry with :class:`MissionSpec` but is
    ignored, since the Radius-of-Concern clip runs after delineation.
    """

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_km: float | None = Field(default=None, ge=1, description="ignored for delineation")


class MissionView(BaseModel):
    """The ``mission`` block of the structured contract (sample-briefing.json).

    Typed just tightly enough that the PDF renderer (FR-27) can trust the fields it
    interpolates: coordinates are real numbers, names/labels are bounded strings. Extra
    keys (``huc12``, ``radius_km``, ``phases_inferred``, …) pass through untouched so the
    frozen contract is preserved verbatim.
    """

    model_config = ConfigDict(extra="allow")

    name: str = Field(default="", max_length=200)
    activity: str = Field(default="canyon", max_length=32)
    is_slot: bool = False
    lat: float = Field(default=0.0, ge=-90, le=90)
    lon: float = Field(default=0.0, ge=-180, le=180)
    window_start: str | None = Field(default=None, max_length=64)
    window_end: str | None = Field(default=None, max_length=64)
    timezone: str | None = Field(default=None, max_length=64)
    tz_name: str | None = Field(default=None, max_length=64)


class BlufEntry(BaseModel):
    """One hazard row of the BLUF table (Overview view + PDF hazard summary)."""

    model_config = ConfigDict(extra="allow")

    hazard: str = Field(max_length=32)
    label: str = Field(max_length=32)
    severity_class: str = Field(max_length=32)
    confidence: str | None = Field(default=None, max_length=16)
    window: ClockToken | None = Field(
        default=None, description="local HHMM–HHMM window of concern; null = persistent"
    )
    is_persistent: bool = False


class PhaseCard(BaseModel):
    """One phase card (approach/technical/egress) of the structured contract (FR-9a)."""

    model_config = ConfigDict(extra="allow")

    phase: str = Field(max_length=32)
    window: ClockToken | None = None
    thermal_primary: str | None = Field(default=None, max_length=32)
    lead_label: str | None = Field(default=None, max_length=120)
    applicable: str | None = Field(default=None, max_length=200)
    # Generous cap: joined engine phase notes are sentences, not documents; the template
    # HTML-escapes the note, so the cap only bounds size.
    note: str | None = Field(default=None, max_length=2000)


class ForecastRow(BaseModel):
    """One labelled row of the hourly forecast table (display-only, never an engine input)."""

    model_config = ConfigDict(extra="allow")

    label: str = Field(max_length=32)
    # 512 comfortably covers the longest legitimate hourly series (Open-Meteo tops out at
    # 16 forecast days = 384 h) while still bounding a hostile payload.
    values: list[str | int | float | None] = Field(default_factory=list, max_length=512)


class ForecastTable(BaseModel):
    """The ``forecast_hourly`` table: hour tokens + per-metric rows (Forecast view + PDF)."""

    model_config = ConfigDict(extra="allow")

    hours: list[ClockToken] = Field(default_factory=list, max_length=512)
    rows: list[ForecastRow] = Field(default_factory=list, max_length=16)


class RiskInputsView(BaseModel):
    """Scalar engine inputs echoed for the Forecast view's Risk Analysis section (FR-20).

    Numbers are numbers and the two display strings are bounded, markup-free tokens: the
    PDF template interpolates these into HTML, so the model guarantees no field can
    smuggle markup into the headless render. Display-only — never re-read by the engine.
    """

    model_config = ConfigDict(extra="allow")

    # int | float (not bare float) so the server's rounded integer percentages keep their
    # exact wire format (65, not 65.0) — the contract stays byte-compatible.
    gefs_p_precip: int | float | None = None
    gefs_p_tstm: int | float | None = None
    refs_in_range: bool | None = None
    refs_p_precip: int | float | None = None
    refs_p_lightning: int | float | None = None
    refs_cycle: ShortToken | None = None
    cape_jkg: int | float | None = None
    convective_rate_in_per_hr: int | float | None = None
    spc_category: ShortToken | None = None
    flash_flood_warning: bool | None = None
    flash_flood_watch: bool | None = None
    flood_watch: bool | None = None
    thunderstorm_warning: bool | None = None


class BriefingResponse(BaseModel):
    """A generated (or cached) briefing and its provenance.

    Carries both the Markdown SITREP (``markdown`` — the CLI's artifact) and the
    structured view the PWA renders its five views from (M0.4). The structured fields are
    built by :func:`upstreamwx.sitrep.structured.to_structured`; their shape is the frozen
    contract in ``frontend/data/sample-briefing.json``. Every posture here is the engine's
    verbatim output — the response layer decides nothing (FR-13, FR-20).

    The fields the PDF renderer interpolates (``mission``, ``bluf``, ``phases``,
    ``forecast_hourly``, ``risk_inputs``) are typed sub-models rather than bare dicts:
    ``POST /v1/briefing/pdf`` accepts this schema from the client and renders it in
    headless Chromium, so the model is the first line of defence against briefing JSON
    carrying HTML where the template expects a number or clock window.
    """

    markdown: str
    overall_posture: str
    overall_confidence: str
    threshold_version: str
    generated_at: datetime
    framed: bool
    cached: bool = Field(description="True if served from cache without regenerating")
    cache_cycle: str = Field(
        description="GEFS/REFS cycle token this briefing is current for (newest available run)"
    )
    degraded: bool = Field(description="True if a non-mandatory source was unavailable (NFR-6)")
    sources_ok: dict[str, bool] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    data_quality: dict = Field(
        default_factory=dict,
        description=(
            "First-class availability/provenance: data gaps affecting this briefing plus the "
            "model cycles actually used (NFR-6). Display-only; never re-read by the engine."
        ),
    )

    # Structured view for the PWA (M0.4). See sample-briefing.json for the shape.
    # (hazard_detail / metrics / timeline / resources stay bare dicts: every value the PDF
    # template reads from them is HTML-escaped or mapped through a fixed lookup.)
    mission: MissionView = Field(default_factory=MissionView)
    watershed: dict | None = None
    roc: dict | None = Field(default=None, description="Radius-of-Concern ring; null = unbounded")
    laoc: dict | None = Field(
        default=None, description="Lightning-Area-of-Concern ring; null = upstream domain"
    )
    summary: str | None = None
    bluf: list[BlufEntry] = Field(default_factory=list)
    metrics: list[dict] = Field(default_factory=list)
    phases: list[PhaseCard] = Field(default_factory=list)
    timeline: list[dict] = Field(default_factory=list)
    hazard_detail: list[dict] = Field(default_factory=list)
    forecast_hourly: ForecastTable = Field(default_factory=ForecastTable)
    temp_series: dict = Field(default_factory=dict)
    wind_series: dict = Field(default_factory=dict)
    risk_inputs: RiskInputsView = Field(
        default_factory=RiskInputsView,
        description="scalar engine inputs for the Forecast view (FR-20)",
    )
    resources: list[dict] = Field(default_factory=list)
