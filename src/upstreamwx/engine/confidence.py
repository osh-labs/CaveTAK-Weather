"""Per-hazard confidence qualifier — PRD Appendix B §16.5, FR-17.

Confidence is the SREF member-support fraction for the assigned tier, adjusted by
cross-source agreement: partial disagreement caps confidence at Moderate; a
material conflict forces Low. With no ensemble information available we default to
Moderate rather than overclaiming High.
"""

from __future__ import annotations

from .models import Confidence, Hazard, HazardInputs
from .thresholds import HazardThresholds

_NAME_TO_CONFIDENCE = {
    "low": Confidence.LOW,
    "moderate": Confidence.MODERATE,
    "high": Confidence.HIGH,
}


def confidence_for(
    hazard: Hazard, inputs: HazardInputs, cfg: HazardThresholds
) -> Confidence:
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
        return _NAME_TO_CONFIDENCE[sa["material_conflict_tier"]]
    if agreement in ("partial", "partial_disagreement"):
        cap = _NAME_TO_CONFIDENCE[sa["partial_disagreement_max"]]
        return Confidence(min(base, cap))
    return base
