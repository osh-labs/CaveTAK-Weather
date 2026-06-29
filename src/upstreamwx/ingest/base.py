"""Ingest provider contract and the normalized bundle the engine consumes.

The engine never imports a provider directly (FR-13, §12): every source fills an
:class:`IngestBundle`, which :func:`to_hazard_inputs` maps onto the engine's
:class:`~upstreamwx.engine.models.HazardInputs`. This keeps providers swappable
(e.g. Open-Meteo for a future paid feed) without touching engine logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from ..engine.models import HazardInputs, Mission

if TYPE_CHECKING:
    from shapely.geometry.base import BaseGeometry

    from ..watershed import PourpointBasin


@dataclass
class ForecastHourly:
    """Per-hour display forecast over the mission window (FR-6; M0.4 PWA Forecast view).

    Built by the Open-Meteo adapter from the *same* query that feeds the engine's derived
    fields, then carried on the bundle so the API can serialize the Forecast view's table
    and charts (PRD §6.8). The engine never reads this — it is display data only and never
    changes a posture (FR-20). Empty/absent under graceful degradation (NFR-6) or on the
    offline ``inputs`` path. All arrays are index-aligned with ``hours``.
    """

    hours: list[str]                       # local "HHMM" labels, mission-window order
    temp_f: list[float | None]             # air temperature, deg F
    feels_f: list[float | None]            # apparent temperature, deg F
    wind_mph: list[float | None]
    gust_mph: list[float | None]
    precip_pct: list[float | None]         # precipitation probability, percent
    qpf_in: list[float | None]             # quantitative precip forecast, inch
    sky: list[str]                         # WMO weather-code emoji per hour


@dataclass
class IngestBundle:
    """Normalized fields gathered from all sources for one mission/window."""

    # NWS products + AFD (FR-5).
    flash_flood_warning: bool = False
    flash_flood_watch: bool = False
    flood_warning: bool = False
    flood_advisory: bool = False
    flood_watch: bool = False
    thunderstorm_warning: bool = False
    afd_text: str | None = None
    afd_storm_mode: str | None = None          # isolated | scattered | numerous; None if absent
    afd_convective_mention: bool = False        # derived from afd_storm_mode; display only
    afd_flood_mention: bool = False

    # Open-Meteo derived fields (FR-6), already in deg F / mph / inch.
    heat_index_f: float | None = None
    apparent_temp_f: float | None = None
    wind_mph: float | None = None
    measurable_precip: bool = False
    antecedent_precip_24_72h: bool = False

    # Per-hour display forecast over the window (FR-6; M0.4 Forecast view). Display only,
    # not an engine input; None when ingest could not populate it (NFR-6).
    forecast_hourly: ForecastHourly | None = None

    # GEFS ensemble over the upstream domain (FR-7).
    gefs_p_precip: float | None = None
    gefs_p_tstm: float | None = None
    convective_rate_in_per_hr: float | None = None
    cape_jkg: float | None = None
    member_support: dict[str, float] = field(default_factory=dict)

    # REFS same-day high-resolution supplement over the upstream domain (FR-7a).
    # Set only when the mission window is in REFS range (~6-36 h).
    refs_p_precip: float | None = None
    refs_p_lightning: float | None = None
    refs_in_range: bool = False
    refs_cycle: str | None = None  # primary run, "YYYYMMDD/HHZ"; display only
    refs_fhour: str | None = None  # range label, e.g. "f11" or "f06-f30 (+ 00Z f13-f17 ...)"
    # Per contributing run: (run label, min fhour, max fhour), primary first then spin-up
    # backfills. Multi-run when an older run backfills the current run's spin-up; display only.
    refs_runs: list[tuple[str, int, int]] | None = None

    # GEFS<->REFS cross-ensemble agreement (FR-17, §16.5); feeds the confidence
    # qualifier. "consistent" unless an in-range REFS materially diverges from GEFS.
    source_agreement: str = "consistent"

    # SPC convective outlook category.
    spc_category: str | None = None

    # Upstream contributing-watershed domain used for GEFS/REFS aggregation (FR-3),
    # delineated pour-point-exact (NLDI raindrop two-step) with a WBD fallback. Set
    # by the orchestrator unless an explicit polygon override is passed; carries the
    # provenance (method, area, snapped point, flowline) the SITREP header renders.
    upstream: PourpointBasin | None = None

    # Radius of Concern (RoC, FR-3): the upstream watershed clipped to a user-set disk
    # around the mission origin. ``aggregation_polygon`` is the polygon the GEFS/REFS
    # zonal aggregation actually ran over (the clipped ``kept`` when a RoC is set, else
    # the full basin). ``roc_disk``/``roc_excluded`` drive the PWA's dashed-ring and
    # hatched-exclusion rendering; ``roc_excluded`` is None when the basin fits the disk.
    aggregation_polygon: BaseGeometry | None = None
    roc_radius_km: float | None = None
    roc_disk: BaseGeometry | None = None
    roc_excluded: BaseGeometry | None = None
    roc_kept_area_km2: float | None = None

    # Lightning Area of Concern (LAoC): the disk around the activity that the lightning
    # ensemble fields (``gefs_p_tstm``/``refs_p_lightning``) aggregate over instead of the
    # upstream watershed (PRD §16.1, §13 principle 4 — lightning is a point/corridor
    # estimate, not basin-routed). ``laoc_disk`` drives the PWA's yellow ring. None unless
    # the mission sets ``lightning_radius_km``; lightning then falls back to the flood domain.
    laoc_radius_km: float | None = None
    laoc_disk: BaseGeometry | None = None
    laoc_area_km2: float | None = None

    # Provenance / graceful-degradation tracking (NFR-6).
    sources_ok: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class Provider(Protocol):
    """A data source that contributes part of an :class:`IngestBundle`."""

    name: str

    def fetch(self, mission: Mission, bundle: IngestBundle) -> None:
        """Populate the provider's fields on ``bundle`` (in place)."""
        ...


def to_hazard_inputs(bundle: IngestBundle, *, dry_party: bool = False) -> HazardInputs:
    """Map a gathered bundle onto the engine's normalized feature vector."""
    return HazardInputs(
        flash_flood_warning=bundle.flash_flood_warning,
        flash_flood_watch=bundle.flash_flood_watch,
        flood_warning=bundle.flood_warning,
        flood_advisory=bundle.flood_advisory,
        flood_watch=bundle.flood_watch,
        thunderstorm_warning=bundle.thunderstorm_warning,
        gefs_p_precip=bundle.gefs_p_precip,
        gefs_p_tstm=bundle.gefs_p_tstm,
        measurable_precip=bundle.measurable_precip,
        convective_rate_in_per_hr=bundle.convective_rate_in_per_hr,
        cape_jkg=bundle.cape_jkg,
        refs_p_precip=bundle.refs_p_precip,
        refs_p_lightning=bundle.refs_p_lightning,
        member_support=dict(bundle.member_support),
        source_agreement=bundle.source_agreement,
        spc_category=bundle.spc_category,
        afd_storm_mode=bundle.afd_storm_mode,
        afd_flood_mention=bundle.afd_flood_mention,
        heat_index_f=bundle.heat_index_f,
        apparent_temp_f=bundle.apparent_temp_f,
        wind_mph=bundle.wind_mph,
        antecedent_precip_24_72h=bundle.antecedent_precip_24_72h,
        dry_party=dry_party,
    )
