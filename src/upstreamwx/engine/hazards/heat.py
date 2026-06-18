"""Heat stress evaluator — PRD Appendix B §16.3, FR-15.

Maps the ambient heat index onto the established NWS Heat Index categories. On the
approach phase, exertion under load is advised to run effective strain one category
hotter (advisory note only; the NWS band itself is unchanged).
"""

from __future__ import annotations

from ..models import HazardInputs, HeatCategory
from ..thresholds import HazardThresholds


def evaluate(
    inputs: HazardInputs, cfg: HazardThresholds, *, is_approach: bool = False
) -> tuple[HeatCategory, list[str], list[str]]:
    drivers: list[str] = []
    notes: list[str] = []
    cats = cfg["categories"]

    hi = inputs.heat_index_f
    if hi is None:
        category = HeatCategory.NONE
        drivers.append("No heat-index data")
    else:
        if hi >= cats["extreme_danger_min"]:
            category = HeatCategory.EXTREME_DANGER
        elif hi >= cats["danger_min"]:
            category = HeatCategory.DANGER
        elif hi >= cats["extreme_caution_min"]:
            category = HeatCategory.EXTREME_CAUTION
        elif hi >= cats["caution_min"]:
            category = HeatCategory.CAUTION
        else:
            category = HeatCategory.NONE
        drivers.append(f"Heat index {hi:.0f} °F → {category.label}")

    if is_approach and category is not HeatCategory.NONE:
        notes.append(
            "Approach involves exertion under load: effective heat strain runs one "
            "category hotter than the ambient heat index suggests."
        )

    return category, drivers, notes
