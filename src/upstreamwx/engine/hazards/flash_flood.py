"""Flash flood tier evaluator — PRD Appendix B §16.1.

Active NWS flood products anchor the near term (they already encode the
QPF-vs-FFG determination); GEFS probability over the upstream domain covers the
planning horizon. Coverage spans the acute flash-flood family *and* the slower
areal/river Flood Warning/Advisory/Watch, plus an AFD excessive-rainfall signal —
all of which raise (never lower) the GEFS/REFS-derived posture. Modifiers:
antecedent-wetness bump and the conservative slot fallback.
"""

from __future__ import annotations

from ..models import HazardInputs, Tier
from ..thresholds import HazardThresholds

# Active flood products, in display order, paired with the input flag and the
# config key carrying their tier. Each active product is a candidate tier; the
# engine takes the max across all of them and the GEFS/REFS signal.
_PRODUCTS = (
    ("flash_flood_warning", "flash_flood_warning_tier",
     "Active Flash Flood Warning over area or upstream domain"),
    ("flash_flood_watch", "flash_flood_watch_tier", "Active Flash Flood Watch"),
    ("flood_warning", "flood_warning_tier",
     "Active Flood Warning (areal/river flooding occurring or imminent)"),
    ("flood_advisory", "flood_advisory_tier",
     "Active Flood Advisory (minor/nuisance flooding)"),
    ("flood_watch", "flood_watch_tier", "Active Flood Watch (conditions favorable for flooding)"),
)


def _gefs_tier(inputs: HazardInputs, prob: dict) -> tuple[Tier, str]:
    """GEFS P(precip/thunderstorm) over the upstream domain -> (tier, driver).

    A missing probability is a data gap, not a dry forecast — the driver says so
    explicitly and the confidence layer floors the qualifier (data quality first-class).
    An *unknown* ``measurable_precip`` (surface feed down) applies the Elevated band
    conservatively rather than letting the gap read as "dry" and gate the band off.
    """
    p = inputs.gefs_p_precip
    if p is None:
        return Tier.MINIMAL, (
            "DATA GAP: no ensemble precip signal available over the upstream domain "
            "(feed unavailable or window out of range) — flood tier is unassessed, not low"
        )
    if p >= prob["high_min"]:
        return Tier.HIGH, (
            f"GEFS P(precip/thunder) {p:.0f}% ≥ {prob['high_min']}% over upstream domain"
        )
    if p >= prob["elevated_min"] and inputs.measurable_precip is not False:
        if inputs.measurable_precip is None:
            return Tier.ELEVATED, (
                f"GEFS P(precip/thunder) {p:.0f}% in {prob['elevated_min']}-{prob['high_min']}% "
                "band; surface precip signal unavailable, band applied conservatively"
            )
        return Tier.ELEVATED, (
            f"GEFS P(precip/thunder) {p:.0f}% in {prob['elevated_min']}-{prob['high_min']}% "
            "band with measurable forecast precip"
        )
    if p >= prob["elevated_min"]:
        return Tier.MINIMAL, (
            f"GEFS P(precip/thunder) {p:.0f}% in the {prob['elevated_min']}-"
            f"{prob['high_min']}% band but no measurable forecast precip; dry upstream"
        )
    return Tier.MINIMAL, (
        f"GEFS P(precip/thunder) {p:.0f}% below {prob['elevated_min']}%; dry upstream"
    )


def evaluate(
    inputs: HazardInputs, cfg: HazardThresholds, *, is_slot: bool = False
) -> tuple[Tier, list[str], list[str]]:
    drivers: list[str] = []
    notes: list[str] = []
    products = cfg["products"]
    prob = cfg["gefs_probability"]
    mods = cfg["modifiers"]

    # Active products anchor the near term but only raise the posture — a lesser
    # product (e.g. a Flood Advisory) must never suppress a stronger GEFS signal.
    tier = Tier.MINIMAL
    product_active = False
    if not inputs.nws_products_available:
        # The alerts check never ran; the product flags are unchecked, not clear (NFR-6).
        notes.append(
            "DATA GAP: NWS active-alert check unavailable — flood products could not be "
            "verified for this briefing."
        )
    for flag, tier_key, driver in _PRODUCTS:
        if getattr(inputs, flag):
            product_active = True
            tier = max(tier, Tier.from_name(products[tier_key]))
            drivers.append(driver)

    # GEFS planning-horizon signal. Shown on its own when no product anchors the
    # near term, and additionally whenever it raises a product-set posture.
    sref_tier, sref_driver = _gefs_tier(inputs, prob)
    if not product_active or sref_tier > tier:
        drivers.append(sref_driver)
    tier = max(tier, sref_tier)

    # AFD forecaster discussion of excessive rainfall / flooding raises the posture
    # to at least the configured floor (coarse positive signal, §16.1).
    if inputs.afd_flood_mention:
        floor = Tier.from_name(cfg["afd_flood_mention_tier"])
        if floor > tier:
            tier = floor
            drivers.append(
                "AFD discusses excessive rainfall / flooding potential over the area"
            )
        elif tier > Tier.MINIMAL:
            drivers.append("AFD excessive-rainfall / flooding discussion concurs")

    # REFS same-day high-resolution overlay (FR-7a, §16.1): evaluate REFS neighborhood
    # P(QPF) on its own cut points and take the higher tier (FR-19). None out of range.
    hp = inputs.refs_p_precip
    if hp is not None:
        hb = cfg["refs_probability"]
        href_tier = Tier.MINIMAL
        if hp >= hb["high_min"]:
            href_tier = Tier.HIGH
        elif hp >= hb["elevated_min"]:
            href_tier = Tier.ELEVATED
        if href_tier > tier:
            drivers.append(
                f"REFS neighborhood P(QPF) {hp:.0f}% over upstream domain "
                f"(~3 km, same-day) raises flood tier to {href_tier.label}"
            )
            tier = href_tier
        elif href_tier > Tier.MINIMAL:
            drivers.append(
                f"REFS neighborhood P(QPF) {hp:.0f}% concurs at {href_tier.label}"
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
    # the configured threshold forces at least the configured floor tier. When the rate
    # feed is down the safeguard is *unevaluated* — say so rather than staying silent.
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
    elif is_slot and rate is None:
        notes.append(
            "DATA GAP: forecast convective rate unavailable — the conservative slot-canyon "
            "fallback could not be evaluated."
        )

    return tier, drivers, notes
