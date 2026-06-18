"""Lightning tier evaluator — PRD Appendix B §16.2 (approach/egress only).

Primary basis is SREF P(thunderstorm) over the exposure window, cross-checked
against SPC outlook and AFD. Any one condition can trigger a tier; the assigned
tier is the max across triggers. CAPE modulates confidence/severity context only
and never sets the tier.
"""

from __future__ import annotations

from ..models import HazardInputs, Tier
from ..thresholds import HazardThresholds


def evaluate(inputs: HazardInputs, cfg: HazardThresholds) -> tuple[Tier, list[str], list[str]]:
    drivers: list[str] = []
    notes: list[str] = []

    # Active (severe) thunderstorm warning overrides everything.
    if inputs.thunderstorm_warning:
        tier = Tier.from_name(cfg["products"]["thunderstorm_warning_tier"])
        return tier, ["Active (severe) thunderstorm warning"], notes

    candidates: list[tuple[Tier, str]] = []

    bands = cfg["sref_ptstm"]
    p = inputs.sref_p_tstm
    _ptstm_bands = (("EXTREME", "extreme_min"), ("HIGH", "high_min"), ("ELEVATED", "elevated_min"))
    if p is not None:
        for tier_name, key in _ptstm_bands:
            if p >= bands[key]:
                drv = f"SREF P(tstm) {p:.0f}% ≥ {bands[key]}%"
                candidates.append((Tier.from_name(tier_name), drv))
                break

    if inputs.spc_category:
        mapped = cfg["spc_category"].get(inputs.spc_category.strip().lower())
        if mapped:
            candidates.append(
                (Tier.from_name(mapped), f"SPC {inputs.spc_category} risk over window")
            )

    if inputs.afd_convective_mention:
        candidates.append(
            (
                Tier.from_name(cfg["afd_convective_mention_tier"]),
                "AFD mentions isolated/scattered afternoon convection",
            )
        )

    if candidates:
        tier = max(c[0] for c in candidates)
        drivers.extend(d for _, d in candidates)
    else:
        tier = Tier.MINIMAL
        drivers.append(f"SREF P(tstm) below {bands['elevated_min']}%; no convective mention")

    # CAPE context (instability) — modulates confidence/severity, not the tier.
    cape = inputs.cape_jkg
    if cape is not None:
        b = cfg["cape_bands_jkg"]
        if cape < b["minimal_max"]:
            label = "minimal"
        elif cape < b["marginal_max"]:
            label = "marginal"
        elif cape < b["moderate_max"]:
            label = "moderate"
        else:
            label = "strong"
        notes.append(f"CAPE {cape:.0f} J/kg ({label} instability) — context only.")

    return tier, drivers, notes
