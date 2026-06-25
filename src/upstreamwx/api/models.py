"""API request/response schema (M0.3).

The request is a mission spec mirroring the CLI arguments, with an optional saved
``HazardInputs`` feature vector for offline/reproducible generation (the corpus path,
FR-25). The response carries the rendered Markdown briefing plus the cache/cycle and
source-availability provenance the PWA (M0.4) needs to show currency and degradation.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from ..engine.models import ActivityType, HazardInputs, Mission
from ..timezones import localize_window


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
        )

    def to_inputs(self) -> HazardInputs | None:
        data = self.inputs
        if data is None:
            return None
        if "inputs" in data:  # accept the corpus/CLI {inputs: {...}} envelope too
            data = data["inputs"]
        return HazardInputs(**data)


class BriefingResponse(BaseModel):
    """A generated (or cached) briefing and its provenance.

    Carries both the Markdown SITREP (``markdown`` — the CLI's artifact) and the
    structured view the PWA renders its five views from (M0.4). The structured fields are
    built by :func:`upstreamwx.sitrep.structured.to_structured`; their shape is the frozen
    contract in ``frontend/data/sample-briefing.json``. Every posture here is the engine's
    verbatim output — the response layer decides nothing (FR-13, FR-20).
    """

    markdown: str
    overall_posture: str
    overall_confidence: str
    threshold_version: str
    generated_at: datetime
    framed: bool
    cached: bool = Field(description="True if served from cache without regenerating")
    cache_cycle: str = Field(description="SREF cycle id this briefing is current for")
    degraded: bool = Field(description="True if a non-mandatory source was unavailable (NFR-6)")
    sources_ok: dict[str, bool] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    # Structured view for the PWA (M0.4). See sample-briefing.json for the shape.
    mission: dict = Field(default_factory=dict)
    watershed: dict | None = None
    summary: str | None = None
    bluf: list[dict] = Field(default_factory=list)
    metrics: list[dict] = Field(default_factory=list)
    phases: list[dict] = Field(default_factory=list)
    timeline: list[dict] = Field(default_factory=list)
    hazard_detail: list[dict] = Field(default_factory=list)
    forecast_hourly: dict = Field(default_factory=dict)
    temp_series: dict = Field(default_factory=dict)
    wind_series: dict = Field(default_factory=dict)
    resources: list[dict] = Field(default_factory=list)
