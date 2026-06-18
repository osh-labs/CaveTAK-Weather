"""Phase inference and the phase x activity applicability matrix.

Implements FR-9a (phase inference), FR-14a (which hazards apply per phase and
activity), FR-14b (phase-dependent thermal weighting) and FR-14c (lightning /
cave gating). This is pure logic over the mission; no thresholds involved.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .models import ActivityType, Hazard, Mission, Phase

_HOUR = timedelta(hours=1)

# FR-14a applicability matrix. Lightning is excluded from the technical span for
# both activity types (FR-14c); a cave technical span evaluates flash flood only.
_APPLICABILITY: dict[tuple[Phase, ActivityType], tuple[Hazard, ...]] = {
    (Phase.APPROACH, ActivityType.CANYON): (Hazard.LIGHTNING, Hazard.HEAT, Hazard.COLD_WET),
    (Phase.APPROACH, ActivityType.CAVE): (Hazard.LIGHTNING, Hazard.HEAT, Hazard.COLD_WET),
    (Phase.TECHNICAL, ActivityType.CANYON): (Hazard.FLASH_FLOOD, Hazard.HEAT, Hazard.COLD_WET),
    (Phase.TECHNICAL, ActivityType.CAVE): (Hazard.FLASH_FLOOD,),
    (Phase.EGRESS, ActivityType.CANYON): (Hazard.LIGHTNING, Hazard.COLD_WET, Hazard.HEAT),
    (Phase.EGRESS, ActivityType.CAVE): (Hazard.LIGHTNING, Hazard.COLD_WET, Hazard.HEAT),
}

# FR-14b: thermal hazard weighted-primary by phase (both still computed/shown).
_THERMAL_PRIMARY: dict[Phase, Hazard] = {
    Phase.APPROACH: Hazard.HEAT,
    Phase.TECHNICAL: Hazard.HEAT,   # in-slot heat leads when present (cold secondary)
    Phase.EGRESS: Hazard.COLD_WET,
}

CAVE_ISOLATION_NOTE = (
    "Cave interior treated as isolated from surface weather; only flash flood is "
    "evaluated for the technical span (FR-14c)."
)
INFERRED_PHASES_NOTE = (
    "Phases inferred from the overall window: approach = first hour, egress = last "
    "hour, technical span = everything in between (FR-9a)."
)


def infer_phases(mission: Mission) -> tuple[dict[Phase, tuple[datetime, datetime]], bool]:
    """Resolve each phase's (start, end) window; flag if inferred (FR-9a).

    When explicit phase markers are absent we infer approach = the first hour and
    egress = the last hour, with the technical span in between.
    """
    start, end = mission.window_start, mission.window_end
    explicit = mission.approach_end is not None and mission.egress_start is not None
    if explicit:
        approach_end = mission.approach_end
        egress_start = mission.egress_start
    else:
        approach_end = min(start + _HOUR, end)
        egress_start = max(end - _HOUR, approach_end)

    windows = {
        Phase.APPROACH: (start, approach_end),
        Phase.TECHNICAL: (approach_end, egress_start),
        Phase.EGRESS: (egress_start, end),
    }
    return windows, not explicit


def applicable_hazards(phase: Phase, activity: ActivityType) -> tuple[Hazard, ...]:
    """Hazards applicable to a phase for an activity type (FR-14a / FR-14c)."""
    return _APPLICABILITY[(phase, activity)]


def thermal_primary(phase: Phase, applicable: tuple[Hazard, ...]) -> Hazard | None:
    """Weighted-primary thermal hazard for the phase, if any apply (FR-14b)."""
    primary = _THERMAL_PRIMARY.get(phase)
    if primary in applicable:
        return primary
    # Fall back to the other thermal hazard if only it is applicable.
    for thermal in (Hazard.HEAT, Hazard.COLD_WET):
        if thermal in applicable:
            return thermal
    return None
