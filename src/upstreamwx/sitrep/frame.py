"""Haiku natural-language framing (M0.2 stage 2; PRD FR-21).

Adds a plain-language BLUF narrative on top of the deterministic structured render
(:mod:`upstreamwx.sitrep.render`). The model is **strictly constrained to narrate**:
it may not add, remove, or alter any posture, tier, confidence, number, or window
(FR-20). To make that guarantee structural, framing only *prepends* a SUMMARY block —
every authoritative line produced by the renderer is left byte-for-byte untouched
below it, so the engine remains the sole source of every posture.
"""

from __future__ import annotations

import json

from ..config import get_settings
from ..engine.models import BriefingResult, Hazard, HeatCategory, Tier

# FR-21: natural-language framing only, via Claude Haiku.
DEFAULT_MODEL = "claude-haiku-4-5"

_SUMMARY_HEADING = "## SUMMARY (plain language)"
_INSERT_BEFORE = "## BLUF"

_SYSTEM_PROMPT = (
    "You are a wilderness-weather briefing writer for a caving and canyoneering hazard tool.\n"
    'You will receive a structured JSON hazard assessment, including per-hazard "drivers"\n'
    "lists that explain WHY each hazard is at its assessed tier.\n\n"
    "Write a plain-language briefing paragraph (3–5 sentences) a trip leader can read at a"
    " glance.\n\n"
    "STRUCTURE — follow this order:\n\n"
    "1. OPENER (one sentence): State the overall posture and the primary hazard driving it.\n"
    '   Pattern: "The overall [activity] hazard posture is [overall_posture], driven primarily\n'
    '   by [driving_hazard] risk [window if provided]."\n'
    '   The JSON field "driving_hazard" tells you which hazard to name first.\n\n'
    "2. DRIVER SENTENCES (one per non-Minimal hazard): Explain WHY that hazard is at its\n"
    "   tier using plain English translations of the drivers list.\n"
    "   Signal translation guide:\n"
    '   • "SREF P(tstm) X% ≥ Y%" → "ensemble models show X% probability of thunderstorms"\n'
    '   • "SREF P(precip/thunder) X%" → "ensemble models show X% probability of precipitation"\n'
    '   • "HREF neighborhood P(convection) X% (~3 km same-day)"\n'
    '     → "high-res models show X% probability of convective/lightning activity"\n'
    '   • "HREF neighborhood P(QPF) X%" → "high-res models show X% probability of heavy precip"\n'
    '   • "AFD: [coverage] convection" → "the area forecast discussion describes [coverage]'
    ' convection"\n'
    '   • "AFD discusses excessive rainfall / flooding potential"\n'
    '     → "the area forecast discussion highlights excessive rainfall and flooding potential"\n'
    '   • "AFD excessive-rainfall / flooding discussion concurs"\n'
    '     → "the area forecast discussion also notes flooding potential"\n'
    '   • "SPC [Category] risk over window" → "Storm Prediction Center rates convective risk'
    ' as [Category]"\n'
    '   • Keep active warning/watch/advisory names as-is (e.g. "Active Flash Flood Warning").\n\n'
    "3. CONFIDENCE/CAVEAT CLOSE (optional — omit if confidence is Moderate or High and\n"
    "   no notable caveats exist): mention overall confidence if Low, or one key caveat\n"
    '   from the hazard "notes" fields.\n\n'
    "STRICT RULES:\n"
    "- Narrate ONLY what is in the JSON. Do not add, remove, soften, or escalate any hazard\n"
    "  posture, tier, confidence level, number, or time window.\n"
    "- Never give a go / no-go recommendation. This tool is reference-only.\n"
    "- Do not invent data, sources, or advice not present in the JSON.\n"
    '- Format time windows as 12-hour local time (e.g., "10:00 AM to 2:00 PM"), using the\n'
    "  time portion of the ISO timestamps; omit the date.\n"
    '- Skip Minimal-posture hazards unless their "notes" contain notable caveats.\n'
    "- Output the briefing paragraph only — no headings, no bullet lists, no preamble."
)


def _severity_rank(posture) -> int:
    """Numeric severity rank for picking the driving hazard (mirrors assess._severity_rank)."""
    if posture.hazard is Hazard.HEAT:
        return int(posture.heat_category or HeatCategory.NONE)
    return int(posture.tier or Tier.MINIMAL)


def _structured_view(result: BriefingResult) -> dict:
    """Compact JSON-serializable view of the engine result for the framer.

    Includes per-hazard drivers and notes so the model can explain WHY each
    posture is what it is (FR-21), and a driving_hazard field naming the
    highest-severity hazard to anchor the opening sentence.
    """
    bluf = {}
    driving_hazard: str | None = None
    best_rank = -1

    for hazard in Hazard:
        posture = result.bluf.get(hazard)
        if posture is None:
            continue
        window = posture.window_of_concern
        rank = _severity_rank(posture)
        bluf[hazard.value] = {
            "posture": posture.severity_label,
            "confidence": posture.confidence.label if posture.confidence else None,
            "window_of_concern": (
                [window[0].isoformat(), window[1].isoformat()] if window else None
            ),
            "drivers": posture.drivers,
            "notes": posture.notes,
        }
        if rank > best_rank:
            best_rank = rank
            driving_hazard = hazard.value

    return {
        "activity_type": result.mission.activity_type.value,
        "overall_posture": result.overall_tier.label,
        "overall_confidence": result.overall_confidence.label,
        "driving_hazard": driving_hazard,
        "phases_inferred": result.phases_inferred,
        "hazards": bluf,
    }


def _splice_summary(structured_md: str, narrative: str) -> str:
    """Insert the SUMMARY block above the BLUF; leave all structured lines untouched."""
    block = f"{_SUMMARY_HEADING}\n\n{narrative}\n\n"
    idx = structured_md.find(_INSERT_BEFORE)
    if idx == -1:  # defensive: no BLUF heading — append at top after the title block
        return structured_md
    return structured_md[:idx] + block + structured_md[idx:]


def frame_briefing(
    result: BriefingResult,
    structured_md: str,
    *,
    client=None,
    model: str = DEFAULT_MODEL,
) -> str:
    """Return ``structured_md`` with a Haiku-written plain-language summary prepended.

    The structured Markdown is treated as authoritative and is never modified — only a
    SUMMARY section is added above the BLUF (FR-20). If no client and no
    ``ANTHROPIC_API_KEY`` is available, framing is skipped and ``structured_md`` is
    returned unchanged (graceful degradation).
    """
    if client is None:
        api_key = get_settings().anthropic_api_key
        if not api_key:
            return structured_md
        import anthropic  # lazy: keep anthropic out of the import path when unused

        client = anthropic.Anthropic(api_key=api_key)

    response = client.messages.create(
        model=model,
        max_tokens=500,
        system=_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": json.dumps(_structured_view(result), sort_keys=True, indent=2),
            }
        ],
    )
    narrative = "".join(b.text for b in response.content if b.type == "text").strip()
    if not narrative:
        return structured_md
    return _splice_summary(structured_md, narrative)
