"""Cold / wet hypothermia evaluator — PRD Appendix B §16.4.

Built on apparent temperature (wind already folded in) under the assumption the
party is wet on egress (FR-16). A dry cave with no immersion may be discounted by
roughly one tier; the wet assumption is always stated so the discount is visible.
"""

from __future__ import annotations

from ..models import HazardInputs, Tier
from ..thresholds import HazardThresholds


def evaluate(
    inputs: HazardInputs, cfg: HazardThresholds, *, dry_party: bool = False
) -> tuple[Tier, list[str], list[str]]:
    drivers: list[str] = []
    notes: list[str] = ["Assumes a wet party on egress."]
    bands = cfg["apparent_temp"]

    at = inputs.apparent_temp_f
    if at is None:
        tier = Tier.MINIMAL
        drivers.append("No apparent-temperature data")
    else:
        if at <= bands["extreme_max"]:
            tier = Tier.EXTREME
        elif at <= bands["high_max"]:
            tier = Tier.HIGH
        elif at <= bands["elevated_max"]:
            tier = Tier.ELEVATED
        else:
            tier = Tier.MINIMAL
        drivers.append(f"Apparent temperature {at:.0f} °F (wet-party basis)")

    if dry_party and tier > Tier.MINIMAL:
        discount = int(cfg["modifiers"]["dry_party_discount_tiers"])
        discounted = Tier(max(Tier.MINIMAL, tier - discount))
        notes.append(
            f"Dry party (no immersion): discounted {tier.label} → {discounted.label}."
        )
        tier = discounted

    return tier, drivers, notes
