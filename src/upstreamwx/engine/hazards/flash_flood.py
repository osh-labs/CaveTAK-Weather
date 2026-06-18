"""Flash flood tier evaluator — PRD Appendix B §16.1.

Active NWS flood products anchor the near term (they already encode the
QPF-vs-FFG determination); SREF probability over the upstream domain covers the
planning horizon. Modifiers: antecedent-wetness bump and the conservative slot
fallback.
"""

from __future__ import annotations

from ..models import HazardInputs, Tier
from ..thresholds import HazardThresholds


def evaluate(
    inputs: HazardInputs, cfg: HazardThresholds, *, is_slot: bool = False
) -> tuple[Tier, list[str], list[str]]:
    drivers: list[str] = []
    notes: list[str] = []
    products = cfg["products"]
    prob = cfg["sref_probability"]
    mods = cfg["modifiers"]

    # Active products override SREF probability.
    if inputs.flash_flood_warning:
        tier = Tier.from_name(products["flash_flood_warning_tier"])
        drivers.append("Active Flash Flood Warning over area or upstream domain")
    elif inputs.flash_flood_watch:
        tier = Tier.from_name(products["flash_flood_watch_tier"])
        drivers.append("Active Flash Flood Watch")
    else:
        tier = Tier.MINIMAL
        p = inputs.sref_p_precip
        if p is None:
            drivers.append("No active flood products; no SREF precip signal")
        elif p >= prob["high_min"]:
            tier = Tier.HIGH
            drivers.append(
                f"SREF P(precip/thunder) {p:.0f}% ≥ {prob['high_min']}% over upstream domain"
            )
        elif p >= prob["elevated_min"] and inputs.measurable_precip:
            tier = Tier.ELEVATED
            drivers.append(
                f"SREF P(precip/thunder) {p:.0f}% in {prob['elevated_min']}-{prob['high_min']}% "
                "band with measurable forecast precip"
            )
        else:
            drivers.append(
                f"SREF P(precip/thunder) {p:.0f}% below {prob['elevated_min']}%; dry upstream"
            )

    # Antecedent wetness bumps an existing precip-driven posture up one level.
    # Applied only when a base signal already exists (>= Elevated): a saturated
    # basin with a dry incoming forecast is still Minimal flood risk.
    if inputs.antecedent_precip_24_72h and tier >= Tier.ELEVATED:
        bumped = Tier(min(Tier.EXTREME, tier + int(mods["antecedent_wetness_bump_tiers"])))
        if bumped != tier:
            notes.append(
                f"Antecedent wetness (significant prior 24-72h rain): bumped {tier.label} "
                f"→ {bumped.label}."
            )
            tier = bumped

    # Slot fallback: slots flood at low totals, so a forecast convective rate over
    # the configured threshold forces at least the configured floor tier.
    rate = inputs.convective_rate_in_per_hr
    if is_slot and rate is not None and rate > mods["slot_rate_in_per_hr"]:
        floor = Tier.from_name(mods["slot_fallback_min_tier"])
        if tier < floor:
            notes.append(
                f"Slot fallback: forecast convective rate {rate:.2f} in/hr > "
                f"{mods['slot_rate_in_per_hr']} in/hr; raised to at least {floor.label} "
                "(intentionally conservative)."
            )
            tier = floor

    return tier, drivers, notes
