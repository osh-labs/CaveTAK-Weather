"""Integration tests for the assessment orchestrator (FR-19, FR-22, NFR-4)."""

from __future__ import annotations

from datetime import datetime

from upstreamwx.engine import (
    ActivityType,
    Confidence,
    Hazard,
    HazardInputs,
    Mission,
    Phase,
    Tier,
    assess,
)


def _mission(activity=ActivityType.CANYON, **kw) -> Mission:
    return Mission(
        activity_type=activity,
        lat=37.0,
        lon=-112.0,
        window_start=datetime(2026, 6, 20, 8),
        window_end=datetime(2026, 6, 20, 18),
        **kw,
    )


def test_overall_posture_is_max_across_applicable_hazards():
    # Flash flood High in the technical span; everything else benign.
    inputs = HazardInputs(
        sref_p_precip=65, measurable_precip=True,
        sref_p_tstm=5, heat_index_f=70, apparent_temp_f=70,
    )
    result = assess(_mission(), inputs)
    assert result.overall_tier is Tier.HIGH
    assert result.bluf[Hazard.FLASH_FLOOD].tier is Tier.HIGH


def test_high_lightning_not_hidden_behind_minimal_flood():
    # FR-19: a High lightning posture on approach must surface separately.
    inputs = HazardInputs(
        sref_p_precip=2, sref_p_tstm=80, heat_index_f=70, apparent_temp_f=70,
    )
    result = assess(_mission(), inputs)
    assert result.overall_tier is Tier.EXTREME  # P(tstm) 80% -> Extreme lightning
    assert result.bluf[Hazard.LIGHTNING].tier is Tier.EXTREME
    assert result.bluf[Hazard.FLASH_FLOOD].tier is Tier.MINIMAL


def test_cave_technical_only_evaluates_flash_flood():
    inputs = HazardInputs(sref_p_precip=65, measurable_precip=True, sref_p_tstm=80)
    result = assess(_mission(ActivityType.CAVE), inputs)
    technical = next(p for p in result.phases if p.phase is Phase.TECHNICAL)
    assert list(technical.postures.keys()) == [Hazard.FLASH_FLOOD]
    assert any("only flash flood" in n for n in technical.notes)
    # Karst caveat present at the mission level (FR-4).
    assert any("karst" in n.lower() for n in result.notes)


def test_heat_extreme_danger_drives_overall_extreme_via_equivalence():
    inputs = HazardInputs(heat_index_f=130, apparent_temp_f=80, sref_p_precip=0)
    result = assess(_mission(), inputs)
    assert result.bluf[Hazard.HEAT].heat_category.label == "Extreme Danger"
    assert result.overall_tier is Tier.EXTREME


def test_all_benign_is_minimal():
    inputs = HazardInputs(
        sref_p_precip=0, sref_p_tstm=0, heat_index_f=65, apparent_temp_f=72,
    )
    result = assess(_mission(), inputs)
    assert result.overall_tier is Tier.MINIMAL


def test_overall_confidence_is_min_across_hazards():
    inputs = HazardInputs(
        sref_p_precip=65, measurable_precip=True,
        member_support={"flash_flood": 0.9, "lightning": 0.2},
        sref_p_tstm=50,
    )
    result = assess(_mission(), inputs)
    # Lightning member support 0.2 -> Low drags the overall confidence down.
    assert result.overall_confidence is Confidence.LOW


def test_determinism_identical_inputs_identical_result():
    inputs = HazardInputs(sref_p_precip=65, measurable_precip=True, sref_p_tstm=50)
    a = assess(_mission(), inputs)
    b = assess(_mission(), inputs)
    assert a.overall_tier is b.overall_tier
    assert a.overall_confidence is b.overall_confidence
    assert {h: p.severity_label for h, p in a.bluf.items()} == {
        h: p.severity_label for h, p in b.bluf.items()
    }


def test_threshold_version_recorded():
    result = assess(_mission(), HazardInputs())
    # Provenance string carries each hazard's configured version (FR-20a, NFR-4).
    assert "flash_flood=" in result.threshold_version
    assert "lightning=" in result.threshold_version
