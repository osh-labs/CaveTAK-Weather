"""Phase inference + applicability matrix tests (FR-9a, FR-14a/b/c)."""

from __future__ import annotations

from datetime import datetime

from upstreamwx.engine.models import ActivityType, Hazard, Mission, Phase
from upstreamwx.engine.phases import (
    applicable_hazards,
    infer_phases,
    thermal_primary,
)


def _mission(activity=ActivityType.CANYON, **kw) -> Mission:
    return Mission(
        activity_type=activity,
        lat=37.0,
        lon=-112.0,
        window_start=datetime(2026, 6, 20, 8, 0),
        window_end=datetime(2026, 6, 20, 18, 0),
        **kw,
    )


def test_phase_inference_first_and_last_hour():
    windows, inferred = infer_phases(_mission())
    assert inferred is True
    assert windows[Phase.APPROACH] == (datetime(2026, 6, 20, 8), datetime(2026, 6, 20, 9))
    assert windows[Phase.EGRESS] == (datetime(2026, 6, 20, 17), datetime(2026, 6, 20, 18))
    assert windows[Phase.TECHNICAL] == (datetime(2026, 6, 20, 9), datetime(2026, 6, 20, 17))


def test_explicit_phase_markers_not_inferred():
    m = _mission(
        approach_end=datetime(2026, 6, 20, 10),
        egress_start=datetime(2026, 6, 20, 16),
    )
    windows, inferred = infer_phases(m)
    assert inferred is False
    assert windows[Phase.TECHNICAL] == (datetime(2026, 6, 20, 10), datetime(2026, 6, 20, 16))


def test_lightning_excluded_from_technical_span_both_activities():
    # FR-14c: lightning never applies in the technical span.
    for activity in (ActivityType.CANYON, ActivityType.CAVE):
        assert Hazard.LIGHTNING not in applicable_hazards(Phase.TECHNICAL, activity)


def test_cave_technical_is_flash_flood_only():
    # FR-14a/c: cave interior isolated from surface weather.
    assert applicable_hazards(Phase.TECHNICAL, ActivityType.CAVE) == (Hazard.FLASH_FLOOD,)


def test_canyon_technical_has_flood_and_thermal_no_lightning():
    appl = applicable_hazards(Phase.TECHNICAL, ActivityType.CANYON)
    assert Hazard.FLASH_FLOOD in appl
    assert Hazard.HEAT in appl and Hazard.COLD_WET in appl
    assert Hazard.LIGHTNING not in appl


def test_approach_and_egress_have_lightning_and_both_thermals():
    for phase in (Phase.APPROACH, Phase.EGRESS):
        for activity in (ActivityType.CANYON, ActivityType.CAVE):
            appl = applicable_hazards(phase, activity)
            assert Hazard.LIGHTNING in appl
            assert Hazard.HEAT in appl and Hazard.COLD_WET in appl


def test_thermal_weighting_by_phase():
    # FR-14b: heat leads on approach, cold leads on egress.
    appl_app = applicable_hazards(Phase.APPROACH, ActivityType.CANYON)
    appl_egr = applicable_hazards(Phase.EGRESS, ActivityType.CANYON)
    assert thermal_primary(Phase.APPROACH, appl_app) is Hazard.HEAT
    assert thermal_primary(Phase.EGRESS, appl_egr) is Hazard.COLD_WET


def test_thermal_primary_none_for_cave_technical():
    appl = applicable_hazards(Phase.TECHNICAL, ActivityType.CAVE)
    assert thermal_primary(Phase.TECHNICAL, appl) is None
