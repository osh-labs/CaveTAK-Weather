"""Per-hazard rule evaluators (PRD Appendix B §16.1-16.4).

Each evaluator is a pure function of the normalized :class:`HazardInputs` and the
hazard's configured thresholds; it returns a severity plus human-readable drivers
and notes. No threshold is hard-coded here — every cut point is read from config
(FR-20a). The orchestrator in :mod:`upstreamwx.engine.assess` wires them together.
"""

from . import cold_wet, flash_flood, heat, lightning

__all__ = ["flash_flood", "lightning", "heat", "cold_wet"]
