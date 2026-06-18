"""Deterministic multi-hazard rule engine (PRD §6.4, FR-13).

The engine owns every hazard posture; the language model only frames its output
(M0.2). It loads externalized threshold config (FR-20a), evaluates the four
hazards per phase and activity type (FR-14a-c), computes per-hazard confidence
(FR-17), and derives the overall mission posture as the max across applicable
hazards (FR-19), deterministically (NFR-4).
"""

from .assess import assess
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
from .thresholds import ThresholdConfig, load_thresholds

__all__ = [
    "assess",
    "load_thresholds",
    "ThresholdConfig",
    "Mission",
    "HazardInputs",
    "HazardPosture",
    "PhaseAssessment",
    "BriefingResult",
    "ActivityType",
    "Phase",
    "Hazard",
    "Tier",
    "HeatCategory",
    "Confidence",
]
