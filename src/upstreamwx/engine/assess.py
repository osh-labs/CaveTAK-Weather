"""Deterministic assessment orchestrator (PRD FR-13, FR-19, FR-22).

Takes a :class:`Mission` and a normalized :class:`HazardInputs`, evaluates every
applicable hazard per phase (FR-14a), weights the thermal hazards (FR-14b),
computes per-hazard confidence (FR-17), and derives the overall mission posture as
the maximum across applicable hazards (FR-19) while preserving each hazard and
phase separately. Pure and deterministic: identical inputs yield an identical
:class:`BriefingResult` (NFR-4). The language model never touches this path.
"""

from __future__ import annotations

from .confidence import confidence_for
from .hazards import cold_wet, flash_flood, heat, lightning
from .models import (
    ActivityType,
    BriefingResult,
    Confidence,
    Hazard,
    HazardInputs,
    HazardPosture,
    HeatCategory,
    Mission,
    Phase,
    PhaseAssessment,
    Tier,
)
from .phases import (
    CAVE_ISOLATION_NOTE,
    INFERRED_PHASES_NOTE,
    applicable_hazards,
    infer_phases,
    thermal_primary,
)
from .thresholds import ThresholdConfig, load_thresholds

KARST_CAVEAT = (
    "Surface HUC delineation is a proxy for caves; true karst recharge zones may "
    "not follow surface topography (FR-4)."
)

# Severity that warrants showing a window of concern (FR-22): Elevated+ for the
# common scale, Caution+ for heat.
_WINDOW_MIN_TIER = Tier.ELEVATED
_WINDOW_MIN_HEAT = HeatCategory.CAUTION


def _evaluate_hazard(
    hazard: Hazard,
    inputs: HazardInputs,
    mission: Mission,
    config: ThresholdConfig,
    phase: Phase,
    window: tuple,
) -> HazardPosture:
    """Run the hazard's evaluator + confidence and wrap it in a posture."""
    notes: list[str] = []
    tier: Tier | None = None
    category: HeatCategory | None = None

    if hazard is Hazard.FLASH_FLOOD:
        tier, drivers, notes = flash_flood.evaluate(
            inputs, config.flash_flood, is_slot=mission.is_slot
        )
        relevant = tier >= _WINDOW_MIN_TIER
    elif hazard is Hazard.LIGHTNING:
        tier, drivers, notes = lightning.evaluate(inputs, config.lightning)
        relevant = tier >= _WINDOW_MIN_TIER
    elif hazard is Hazard.HEAT:
        category, drivers, notes = heat.evaluate(
            inputs, config.heat, is_approach=(phase is Phase.APPROACH)
        )
        relevant = category >= _WINDOW_MIN_HEAT
    else:  # COLD_WET
        tier, drivers, notes = cold_wet.evaluate(
            inputs, config.cold_wet, dry_party=inputs.dry_party
        )
        relevant = tier >= _WINDOW_MIN_TIER

    return HazardPosture(
        hazard=hazard,
        tier=tier,
        heat_category=category,
        confidence=confidence_for(hazard, inputs, config.confidence),
        window_of_concern=window if relevant else None,
        drivers=drivers,
        notes=notes,
    )


def _severity_rank(posture: HazardPosture) -> int:
    """Within-hazard ordering for picking the worst phase into the BLUF."""
    if posture.hazard is Hazard.HEAT:
        return int(posture.heat_category or HeatCategory.NONE)
    return int(posture.tier or Tier.MINIMAL)


def _heat_to_tier(category: HeatCategory | None, config: ThresholdConfig) -> Tier:
    """Map an NWS heat category onto the common tier scale for the FR-19 max."""
    if not category or category is HeatCategory.NONE:
        return Tier.MINIMAL
    equiv = config.heat["overall_posture_equivalence"]
    return Tier.from_name(equiv[category.name.lower()])


def _posture_as_tier(posture: HazardPosture, config: ThresholdConfig) -> Tier:
    if posture.hazard is Hazard.HEAT:
        return _heat_to_tier(posture.heat_category, config)
    return posture.tier or Tier.MINIMAL


def assess(
    mission: Mission,
    inputs: HazardInputs,
    config: ThresholdConfig | None = None,
) -> BriefingResult:
    """Produce the structured multi-hazard briefing for a mission."""
    config = config or load_thresholds()
    windows, inferred = infer_phases(mission)
    activity = mission.activity_type

    phase_assessments: list[PhaseAssessment] = []
    bluf: dict[Hazard, HazardPosture] = {}

    for phase in (Phase.APPROACH, Phase.TECHNICAL, Phase.EGRESS):
        appl = applicable_hazards(phase, activity)
        window = windows[phase]
        postures: dict[Hazard, HazardPosture] = {}
        phase_notes: list[str] = []
        if activity is ActivityType.CAVE and phase is Phase.TECHNICAL:
            phase_notes.append(CAVE_ISOLATION_NOTE)

        for hazard in appl:
            posture = _evaluate_hazard(hazard, inputs, mission, config, phase, window)
            postures[hazard] = posture
            # BLUF keeps the worst phase per hazard (FR-19 preserves each hazard).
            if hazard not in bluf or _severity_rank(posture) > _severity_rank(bluf[hazard]):
                bluf[hazard] = posture

        phase_assessments.append(
            PhaseAssessment(
                phase=phase,
                window=window,
                applicable=list(appl),
                thermal_primary=thermal_primary(phase, appl),
                postures=postures,
                notes=phase_notes,
            )
        )

    # Overall posture = max across all applicable hazards (FR-19).
    if bluf:
        overall_tier = max(_posture_as_tier(p, config) for p in bluf.values())
        confidences = [
            p.confidence if p.confidence is not None else Confidence.MODERATE
            for p in bluf.values()
        ]
        overall_confidence = Confidence(min(int(c) for c in confidences))
    else:
        overall_tier = Tier.MINIMAL
        overall_confidence = Confidence.MODERATE

    notes: list[str] = []
    if inferred:
        notes.append(INFERRED_PHASES_NOTE)
    if activity is ActivityType.CAVE:
        notes.append(KARST_CAVEAT)

    return BriefingResult(
        mission=mission,
        overall_tier=overall_tier,
        overall_confidence=overall_confidence,
        bluf=bluf,
        phases=phase_assessments,
        phases_inferred=inferred,
        threshold_version=config.version,
        upstream_summary=None,
        notes=notes,
    )
