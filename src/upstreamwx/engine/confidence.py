"""Per-hazard confidence qualifier — PRD Appendix B §16.5, FR-17.

Confidence is the GEFS member-support fraction for the assigned tier, adjusted by
cross-source agreement: partial disagreement caps confidence at Moderate; a
material conflict forces Low. With no ensemble information available we default to
Moderate rather than overclaiming High — but when the hazard's *primary driver*
itself is missing (the ensemble/thermal feed is down, not merely quiet), confidence
is forced to the configured floor: a posture computed without its primary input must
never present as moderately confident (data quality first-class, NFR-6).
"""

from __future__ import annotations

from .models import Confidence, Hazard, HazardInputs
from .thresholds import HazardThresholds

_NAME_TO_CONFIDENCE = {
    "low": Confidence.LOW,
    "moderate": Confidence.MODERATE,
    "high": Confidence.HIGH,
}


def _primary_available(hazard: Hazard, inputs: HazardInputs) -> bool:
    """Whether the hazard's primary basis (Appendix B §16.1-§16.4) was actually present."""
    if hazard is Hazard.FLASH_FLOOD:
        # Ensemble signal over the domain, or a verified NWS product check anchoring it.
        return (
            inputs.gefs_p_precip is not None
            or inputs.refs_p_precip is not None
            or (inputs.nws_products_available and _any_flood_product(inputs))
        )
    if hazard is Hazard.LIGHTNING:
        return (
            inputs.gefs_p_tstm is not None
            or inputs.refs_p_lightning is not None
            or (inputs.nws_products_available and inputs.thunderstorm_warning)
        )
    if hazard is Hazard.HEAT:
        return inputs.heat_index_f is not None
    return inputs.apparent_temp_f is not None  # COLD_WET


def _any_flood_product(inputs: HazardInputs) -> bool:
    return (
        inputs.flash_flood_warning
        or inputs.flash_flood_watch
        or inputs.flood_warning
        or inputs.flood_advisory
        or inputs.flood_watch
    )


def confidence_for(
    hazard: Hazard, inputs: HazardInputs, cfg: HazardThresholds
) -> Confidence:
    if not _primary_available(hazard, inputs):
        return _NAME_TO_CONFIDENCE[cfg["missing_primary_confidence"]]

    ms = inputs.member_support.get(hazard.value)
    bands = cfg["member_support"]

    if ms is None:
        base = Confidence.MODERATE
    elif ms >= bands["high_min"]:
        base = Confidence.HIGH
    elif ms >= bands["moderate_min"]:
        base = Confidence.MODERATE
    else:
        base = Confidence.LOW

    agreement = (inputs.source_agreement or "consistent").strip().lower()
    sa = cfg["source_agreement"]
    if agreement in ("conflict", "material_conflict"):
        base = _NAME_TO_CONFIDENCE[sa["material_conflict_tier"]]
    elif agreement in ("partial", "partial_disagreement"):
        cap = _NAME_TO_CONFIDENCE[sa["partial_disagreement_max"]]
        base = Confidence(min(base, cap))

    # A possibly-truncated upstream trace caps flash-flood confidence: the basin the
    # ensemble aggregated over may be missing contributing area (data quality, v1.2).
    if hazard is Hazard.FLASH_FLOOD and not inputs.domain_complete:
        cap = _NAME_TO_CONFIDENCE[cfg["incomplete_domain_max"]]
        base = Confidence(min(base, cap))
    return base
