"""Ingest provider contract and the normalized bundle the engine consumes.

The engine never imports a provider directly (FR-13, §12): every source fills an
:class:`IngestBundle`, which :func:`to_hazard_inputs` maps onto the engine's
:class:`~upstreamwx.engine.models.HazardInputs`. This keeps providers swappable
(e.g. Open-Meteo for a future paid feed) without touching engine logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..engine.models import HazardInputs, Mission


@dataclass
class IngestBundle:
    """Normalized fields gathered from all sources for one mission/window."""

    # NWS products + AFD (FR-5).
    flash_flood_warning: bool = False
    flash_flood_watch: bool = False
    thunderstorm_warning: bool = False
    afd_text: str | None = None
    afd_convective_mention: bool = False

    # Open-Meteo derived fields (FR-6), already in deg F / mph / inch.
    heat_index_f: float | None = None
    apparent_temp_f: float | None = None
    wind_mph: float | None = None
    measurable_precip: bool = False
    antecedent_precip_24_72h: bool = False

    # SREF ensemble over the upstream domain (FR-7).
    sref_p_precip: float | None = None
    sref_p_tstm: float | None = None
    convective_rate_in_per_hr: float | None = None
    cape_jkg: float | None = None
    member_support: dict[str, float] = field(default_factory=dict)

    # SPC convective outlook category.
    spc_category: str | None = None

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
        thunderstorm_warning=bundle.thunderstorm_warning,
        sref_p_precip=bundle.sref_p_precip,
        sref_p_tstm=bundle.sref_p_tstm,
        measurable_precip=bundle.measurable_precip,
        convective_rate_in_per_hr=bundle.convective_rate_in_per_hr,
        cape_jkg=bundle.cape_jkg,
        member_support=dict(bundle.member_support),
        source_agreement="consistent",
        spc_category=bundle.spc_category,
        afd_convective_mention=bundle.afd_convective_mention,
        heat_index_f=bundle.heat_index_f,
        apparent_temp_f=bundle.apparent_temp_f,
        wind_mph=bundle.wind_mph,
        antecedent_precip_24_72h=bundle.antecedent_precip_24_72h,
        dry_party=dry_party,
    )
