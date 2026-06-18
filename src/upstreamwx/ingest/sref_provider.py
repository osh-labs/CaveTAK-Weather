"""SREF provider — ensemble probability over the upstream domain (FR-7).

Thin wrapper over the M0.0 SREF pipeline (``upstreamwx.sref``): find the latest
cycle, subset the probability fields, and aggregate the conservative max over the
upstream watershed polygon. The ensemble probability is itself the fraction of
members exceeding the threshold, so it doubles as the member-support input for the
confidence qualifier (§16.5).

Heavy/scheduled orchestration (cron at the SREF cadence, persistent multi-cycle
cache) is deferred to M0.1.1 / the EC2 instance; this is the on-demand processing
logic those will invoke.
"""

from __future__ import annotations

from shapely.geometry.base import BaseGeometry

from ..engine.models import Mission
from ..sref import aggregate_over_polygon, latest_available_cycle, load_probability_field
from .base import IngestBundle

NAME = "sref"

# Precip-probability proxy: P(3-h accumulation > 6.35 mm ≈ 0.25 in) over the domain.
PRECIP_VAR, PRECIP_PROB, PRECIP_FREQ = "APCP", ">6.35", "3hrly"
# Thunderstorm proxy: P(CAPE > 1000 J/kg) — convective instability over the domain.
TSTM_VAR, TSTM_PROB = "CAPE", ">1000"


def _domain_max(cycle, var, prob, polygon, *, freq=None) -> float | None:
    field = load_probability_field(cycle, var=var, prob=prob, freq=freq or "3hrly")
    agg = aggregate_over_polygon(field.data, polygon, field_name=var, threshold=prob)
    return agg.max_value


def fetch(mission: Mission, bundle: IngestBundle, polygon: BaseGeometry, *, cycle=None) -> None:
    """Populate SREF probabilities + member support over the upstream domain."""
    cycle = cycle or latest_available_cycle()
    if cycle is None:
        bundle.sources_ok[NAME] = False
        bundle.notes.append("SREF: no available cycle on NOMADS (retention/lag).")
        return

    p_precip = _domain_max(cycle, PRECIP_VAR, PRECIP_PROB, polygon, freq=PRECIP_FREQ)
    p_tstm = _domain_max(cycle, TSTM_VAR, TSTM_PROB, polygon)

    bundle.sref_p_precip = p_precip
    bundle.sref_p_tstm = p_tstm
    # Probability == fraction of members exceeding the threshold == member support.
    if p_precip is not None:
        bundle.member_support["flash_flood"] = p_precip / 100.0
    if p_tstm is not None:
        bundle.member_support["lightning"] = p_tstm / 100.0
    bundle.notes.append(
        f"SREF cycle {cycle.date}/{cycle.hh}Z; P(precip>6.35mm/3h) and P(CAPE>1000) "
        "used as precip/thunderstorm proxies over the upstream domain."
    )
    bundle.sources_ok[NAME] = True
