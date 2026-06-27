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
    # When HREF is available in-window, use a higher SREF Extreme threshold so the
    # higher-resolution same-day ensemble is the dominant Extreme trigger (§16.2).
    href_in_window = inputs.href_p_lightning is not None
    sref_extreme_key = "extreme_min_with_href" if href_in_window else "extreme_min"
    _ptstm_bands = (
        ("EXTREME", sref_extreme_key), ("HIGH", "high_min"), ("ELEVATED", "elevated_min")
    )
    _href_bands = (("EXTREME", "extreme_min"), ("HIGH", "high_min"), ("ELEVATED", "elevated_min"))
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

    if inputs.afd_storm_mode is not None:
        mode_tier = Tier.from_name(cfg["afd_storm_mode"][inputs.afd_storm_mode])
        candidates.append((mode_tier, f"AFD: {inputs.afd_storm_mode} convection"))

    # HREF same-day overlay (FR-7a, §16.2): HREF neighborhood P(lightning)/P(reflectivity)
    # on its own cut points, added as another candidate; the max across all wins.
    hp = inputs.href_p_lightning
    if hp is not None:
        hb = cfg["href_convection"]
        for tier_name, key in _href_bands:
            if hp >= hb[key]:
                candidates.append(
                    (
                        Tier.from_name(tier_name),
                        f"HREF neighborhood P(convection) {hp:.0f}% ≥ {hb[key]}% (~3 km same-day)",
                    )
                )
                break

    if candidates:
        tier = max(c[0] for c in candidates)
        drivers.extend(d for _, d in candidates)
    else:
        tier = Tier.MINIMAL
        drivers.append(f"SREF P(tstm) below {bands['elevated_min']}%; no convective mention")

    # Contextual ceiling: when the AFD describes routine coverage (isolated/scattered),
    # cap the final tier unless HREF P(lightning) exceeds the override threshold — the
    # same-day high-res ensemble can see more than an AFD written hours earlier (§16.2).
    ceiling_cfg = cfg.get("afd_ceiling", {})
    ceiling_key = ceiling_cfg.get(inputs.afd_storm_mode) if inputs.afd_storm_mode else None
    if ceiling_key:
        ceiling = Tier.from_name(ceiling_key)
        href_min: float = ceiling_cfg["href_override_min"]
        href_overrides = (
            inputs.href_p_lightning is not None and inputs.href_p_lightning > href_min
        )
        if not href_overrides and tier > ceiling:
            hp_str = (
                "n/a" if inputs.href_p_lightning is None
                else f"{inputs.href_p_lightning:.0f}%"
            )
            notes.append(
                f"Tier capped at {ceiling.label}: AFD describes {inputs.afd_storm_mode} "
                f"convection (HREF ≥{href_min:.0f}% would override; current: {hp_str})."
            )
            tier = ceiling

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
