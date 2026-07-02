"""Lightning tier evaluator — PRD Appendix B §16.2 (approach/egress only).

Primary basis is GEFS P(thunderstorm) over the exposure window, cross-checked
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
    if not inputs.nws_products_available:
        # The alerts check never ran; "no warning" is unchecked, not verified (NFR-6).
        notes.append(
            "DATA GAP: NWS active-alert check unavailable — thunderstorm warnings could "
            "not be verified for this briefing."
        )

    candidates: list[tuple[Tier, str]] = []

    # Normalize the AFD storm-mode key once; an out-of-vocabulary mode degrades to
    # "no AFD signal" rather than crashing assess() (NFR-6), mirroring spc_category.
    afd_mode = inputs.afd_storm_mode.strip().lower() if inputs.afd_storm_mode else None

    bands = cfg["gefs_ptstm"]
    p = inputs.gefs_p_tstm
    # When REFS is available in-window, use a higher GEFS Extreme threshold so the
    # higher-resolution same-day ensemble is the dominant Extreme trigger (§16.2).
    refs_in_window = inputs.refs_p_lightning is not None
    gefs_extreme_key = "extreme_min_with_refs" if refs_in_window else "extreme_min"
    _ptstm_bands = (
        ("EXTREME", gefs_extreme_key), ("HIGH", "high_min"), ("ELEVATED", "elevated_min")
    )
    _refs_bands = (("EXTREME", "extreme_min"), ("HIGH", "high_min"), ("ELEVATED", "elevated_min"))
    if p is not None:
        for tier_name, key in _ptstm_bands:
            if p >= bands[key]:
                drv = f"GEFS P(tstm) {p:.0f}% ≥ {bands[key]}%"
                candidates.append((Tier.from_name(tier_name), drv))
                break

    if inputs.spc_category:
        mapped = cfg["spc_category"].get(inputs.spc_category.strip().lower())
        if mapped:
            candidates.append(
                (Tier.from_name(mapped), f"SPC {inputs.spc_category} risk over window")
            )

    if afd_mode is not None:
        mapped_mode = cfg["afd_storm_mode"].get(afd_mode)
        if mapped_mode:
            mode_tier = Tier.from_name(mapped_mode)
            candidates.append((mode_tier, f"AFD: {inputs.afd_storm_mode} convection"))

    # REFS same-day overlay (FR-7a, §16.2): REFS neighborhood P(lightning)/P(reflectivity)
    # on its own cut points, added as another candidate; the max across all wins.
    hp = inputs.refs_p_lightning
    if hp is not None:
        hb = cfg["refs_convection"]
        for tier_name, key in _refs_bands:
            if hp >= hb[key]:
                candidates.append(
                    (
                        Tier.from_name(tier_name),
                        f"REFS neighborhood P(convection) {hp:.0f}% ≥ {hb[key]}% (~3 km same-day)",
                    )
                )
                break

    if candidates:
        tier = max(c[0] for c in candidates)
        drivers.extend(d for _, d in candidates)
    elif p is None and inputs.refs_p_lightning is None:
        # No ensemble signal existed to evaluate — a data gap, not a quiet forecast (NFR-6).
        tier = Tier.MINIMAL
        drivers.append(
            "DATA GAP: no ensemble thunderstorm signal available over the exposure area "
            "(feed unavailable or window out of range) — lightning tier is unassessed, not low"
        )
    else:
        tier = Tier.MINIMAL
        drivers.append(f"GEFS P(tstm) below {bands['elevated_min']}%; no convective mention")

    # Contextual ceiling: when the AFD describes routine coverage (isolated/scattered),
    # cap the final tier unless REFS P(lightning) exceeds the override threshold — the
    # same-day high-res ensemble can see more than an AFD written hours earlier (§16.2).
    ceiling_cfg = cfg.get("afd_ceiling", {})
    ceiling_key = ceiling_cfg.get(afd_mode) if afd_mode else None
    if ceiling_key:
        ceiling = Tier.from_name(ceiling_key)
        href_min: float = ceiling_cfg["refs_override_min"]
        # >= to match the configured contract ("REFS >= 60% bypasses") and the note below.
        href_overrides = (
            inputs.refs_p_lightning is not None and inputs.refs_p_lightning >= href_min
        )
        if not href_overrides and tier > ceiling:
            hp_str = (
                "n/a" if inputs.refs_p_lightning is None
                else f"{inputs.refs_p_lightning:.0f}%"
            )
            notes.append(
                f"Tier capped at {ceiling.label}: AFD describes {inputs.afd_storm_mode} "
                f"convection (REFS ≥{href_min:.0f}% would override; current: {hp_str})."
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
