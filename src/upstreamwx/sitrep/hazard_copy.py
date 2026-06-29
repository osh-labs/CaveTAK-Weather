"""User-facing per-hazard threshold-logic copy for the PWA Hazards view (FR-20, §6.8).

The Hazards view shows, per hazard, the *definition* of the tier ladder behind the
posture so a trip leader can see how the number was reached and verify it (FR-20, the
"how this is calculated" requirement). This is descriptive prose — it cites Appendix B
section numbers per the repo's docstring-citation convention — and is **not** a threshold
cut point: the actual numbers remain data in ``data/thresholds/*.yaml`` (FR-20a). The
frontend strips the parenthetical citations before display (``thresholdLogicHtml``).

Posture, drivers, confidence, and assumptions are all produced by the deterministic
engine and serialized live; only this static framing copy lives here.
"""

from __future__ import annotations

from ..engine.models import Hazard

# One ladder-definition line per hazard, semicolon-separated tiers (the frontend splits on
# ";" into list items). Mirrors the Appendix B matrices the engine evaluates (FR-20a).
HAZARD_LOGIC: dict[Hazard, str] = {
    Hazard.FLASH_FLOOD: (
        "Extreme = active Flash Flood Warning; "
        "High = Watch OR GEFS ≥60% w/ convective rate; "
        "Elevated = GEFS 20–59% OR REFS NEP(≥0.5 in/1 h) 20–39% "
        "(Appendix B §16.1)."
    ),
    Hazard.LIGHTNING: (
        "Extreme = GEFS P(tstm) ≥80% (≥85% when REFS in-window) OR "
        "REFS P(lightning) ≥45% OR SPC Moderate+ during any exposed phase; "
        "High = GEFS 40–79% OR REFS 20–44%; "
        "Elevated = GEFS 15–39% OR REFS 8–19% OR AFD convective mention "
        "(Appendix B §16.2). GEFS P(tstm) is a CAPE×precip member-exceedance "
        "proxy — coarser than SREF; read conservatively. "
        "Assessed over the Lightning Area of Concern around the activity, "
        "not the upstream watershed (PRD §16.1). "
        "Cave interior excluded for the technical span."
    ),
    Hazard.HEAT: (
        "Uses NWS Heat Index categories (FR-15): Caution 80–90 °F, "
        "Extreme Caution 90–103 °F, Danger 103–124 °F, "
        "Extreme Danger ≥125 °F. Surface hazard only — cave interior "
        "excluded."
    ),
    Hazard.COLD_WET: (
        "Minimal = apparent temp >60 °F for a wet party (Appendix B §16.4). "
        "Elevated band starts below 60 °F. Bands are warmer than dry-cold thresholds "
        "because wet clothing loses insulation."
    ),
}
