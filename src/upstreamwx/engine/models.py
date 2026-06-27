"""Engine domain model — the deterministic types the rule engine consumes and
produces (PRD §6.4, §6.5).

The engine takes a :class:`Mission` plus a normalized :class:`HazardInputs`
feature vector (decoupled from raw providers per FR-13/§12) and emits a
:class:`BriefingResult` mirroring the FR-22 SITREP structure. Everything here is
plain data: identical inputs yield an identical result (NFR-4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum


class ActivityType(StrEnum):
    CANYON = "canyon"
    CAVE = "cave"


class Phase(StrEnum):
    APPROACH = "approach"
    TECHNICAL = "technical"
    EGRESS = "egress"


class Hazard(StrEnum):
    FLASH_FLOOD = "flash_flood"
    LIGHTNING = "lightning"
    HEAT = "heat"
    COLD_WET = "cold_wet"


class Tier(IntEnum):
    """Common severity scale (FR-14). Ordered so FR-19's max is ``max(tiers)``."""

    MINIMAL = 0
    ELEVATED = 1
    HIGH = 2
    EXTREME = 3

    @property
    def label(self) -> str:
        return self.name.capitalize()

    @classmethod
    def from_name(cls, name: str) -> Tier:
        return cls[name.strip().upper()]


class HeatCategory(IntEnum):
    """NWS Heat Index categories (FR-15); heat uses these, not :class:`Tier`."""

    NONE = 0
    CAUTION = 1
    EXTREME_CAUTION = 2
    DANGER = 3
    EXTREME_DANGER = 4

    @property
    def label(self) -> str:
        return self.name.replace("_", " ").title()

    @classmethod
    def from_name(cls, name: str) -> HeatCategory:
        return cls[name.strip().upper().replace(" ", "_")]


class Confidence(IntEnum):
    LOW = 0
    MODERATE = 1
    HIGH = 2

    @property
    def label(self) -> str:
        return self.name.capitalize()


@dataclass
class Mission:
    """A persistent mission (FR-9): activity, location, window, optional phases."""

    activity_type: ActivityType
    lat: float
    lon: float
    window_start: datetime
    window_end: datetime
    approach_end: datetime | None = None   # optional phase marker (FR-9a)
    egress_start: datetime | None = None   # optional phase marker (FR-9a)
    party_size: int | None = None
    route_note: str | None = None
    is_slot: bool = False                  # slot canyon -> conservative flood fallback
    name: str = "mission"
    radius_km: float | None = None         # Radius of Concern: caps the upstream watershed (FR-3)
    # Lightning Area of Concern: aggregate the lightning signal over a disk around the
    # activity (PRD §16.1 "activity location + approach corridor"; §13 principle 4) rather
    # than the upstream watershed. None -> lightning falls back to the flash-flood domain.
    lightning_radius_km: float | None = None


@dataclass
class HazardInputs:
    """Normalized deterministic feature vector the engine evaluates against config.

    This is the contract the ingest layer fills and the unit the validation corpus
    is written in. All probabilities are percent ``[0, 100]``; temperatures deg F.
    """

    # Active NWS products (FR-5). Flash flood products anchor the acute near term;
    # the areal/river flood products (warning/advisory/watch) cover the slower-onset
    # flooding the flash-flood scan alone misses.
    flash_flood_warning: bool = False
    flash_flood_watch: bool = False
    flood_warning: bool = False
    flood_advisory: bool = False
    flood_watch: bool = False
    thunderstorm_warning: bool = False

    # SREF ensemble aggregates over the upstream domain.
    sref_p_precip: float | None = None            # P(precip/thunderstorm), flood
    sref_p_tstm: float | None = None              # P(thunderstorm), lightning
    measurable_precip: bool = False               # measurable forecast precip present
    convective_rate_in_per_hr: float | None = None  # forecast convective rate (slot)
    cape_jkg: float | None = None                 # instability (modulates, not tier)

    # HREF same-day high-resolution overlay over the upstream domain (FR-7a, §16.1/§16.2).
    # Neighborhood ensemble probabilities, percent [0, 100]; None when out of HREF
    # range (~6-36 h). Evaluated on their own cut points; the engine takes the higher
    # of the SREF- and HREF-derived tiers (FR-19).
    href_p_precip: float | None = None            # NEP P(>=0.5"/1h or >=1"/3h), flood
    href_p_lightning: float | None = None         # NEP P(lightning)/P(reflectivity)

    # Confidence inputs (FR-17, §16.5). member_support keyed by Hazard.value, [0, 1].
    member_support: dict[str, float] = field(default_factory=dict)
    source_agreement: str = "consistent"          # consistent | partial | conflict

    # SPC convective outlook category over the window, and AFD signals.
    spc_category: str | None = None               # categorical|enhanced|slight|marginal
    afd_storm_mode: str | None = None             # AFD coverage: isolated | scattered | numerous
    afd_flood_mention: bool = False               # AFD discusses excessive rain/flooding

    # Open-Meteo derived fields (deg F / mph).
    heat_index_f: float | None = None
    apparent_temp_f: float | None = None
    wind_mph: float | None = None
    antecedent_precip_24_72h: bool = False        # significant prior rain

    # Activity modifiers.
    dry_party: bool = False                       # dry cave, no immersion


@dataclass
class HazardPosture:
    """One hazard's assessed posture (tier or heat category) with drivers."""

    hazard: Hazard
    tier: Tier | None = None
    heat_category: HeatCategory | None = None
    confidence: Confidence | None = None
    window_of_concern: tuple[datetime, datetime] | None = None
    drivers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def severity_label(self) -> str:
        if self.hazard is Hazard.HEAT and self.heat_category is not None:
            return self.heat_category.label
        return self.tier.label if self.tier is not None else "n/a"


@dataclass
class PhaseAssessment:
    """Per-phase breakdown (FR-22 item 2): applicable hazards + thermal primary."""

    phase: Phase
    window: tuple[datetime, datetime]
    applicable: list[Hazard]
    thermal_primary: Hazard | None
    postures: dict[Hazard, HazardPosture] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class BriefingResult:
    """Structured engine output (FR-22). Rendered to .md / framed in M0.2."""

    mission: Mission
    overall_tier: Tier
    overall_confidence: Confidence
    bluf: dict[Hazard, HazardPosture]           # per-hazard mission-level posture
    phases: list[PhaseAssessment]
    phases_inferred: bool
    threshold_version: str
    upstream_summary: str | None = None
    notes: list[str] = field(default_factory=list)
