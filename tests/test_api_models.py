"""Response-model hardening tests (C-2 defence for the PDF renderer, FR-27).

``POST /v1/briefing/pdf`` renders *client-supplied* ``BriefingResponse`` JSON in headless
Chromium, so the fields the print template interpolates (mission, bluf, phases,
forecast_hourly, risk_inputs) are typed sub-models rather than bare dicts. These tests pin
both halves of that contract:

* hostile payloads — markup where the template expects a clock window or a number — are
  rejected at validation time (422 at the endpoint), and
* the frozen structured contract (``frontend/data/sample-briefing.json``) still validates
  and round-trips unchanged for legitimate briefings.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from upstreamwx.api.models import (
    BlufEntry,
    BriefingResponse,
    ForecastTable,
    MissionView,
    PhaseCard,
    RiskInputsView,
)

_SAMPLE = Path(__file__).resolve().parents[1] / "frontend" / "data" / "sample-briefing.json"

# A short XSS probe that fits every max_length in play, so rejection is attributable to
# the markup check rather than the size cap.
_XSS = "<img src=x onerror=x>"


def _sample() -> dict:
    data = json.loads(_SAMPLE.read_text())
    data.pop("_comment", None)
    return data


# -- the frozen contract still validates (no false positives) -----------------------------
def test_sample_briefing_validates_and_round_trips():
    """The committed contract passes the typed models and dumps back unchanged."""
    resp = BriefingResponse.model_validate(_sample())
    dumped = resp.model_dump(mode="json")
    data = _sample()
    # The PDF-critical blocks round-trip exactly — including extra mission keys the
    # sub-model does not declare (huc12, radius_km, phases_inferred, ...).
    assert dumped["mission"] == data["mission"]
    assert dumped["bluf"] == data["bluf"]
    assert dumped["phases"] == data["phases"]
    assert dumped["forecast_hourly"] == data["forecast_hourly"]
    assert dumped["risk_inputs"] == data["risk_inputs"]
    # And nothing was dropped from the top level.
    assert set(data) <= set(dumped)


def test_offline_degraded_shapes_still_validate():
    """The offline/degraded shapes to_structured emits (empty tables) stay accepted (NFR-6)."""
    data = _sample()
    data["forecast_hourly"] = {"hours": [], "rows": []}
    data["risk_inputs"] = {}
    data["watershed"] = None
    resp = BriefingResponse.model_validate(data)
    assert resp.forecast_hourly.hours == []
    assert resp.risk_inputs.gefs_p_precip is None


# -- markup is rejected where the template treats values as trusted -----------------------
@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["bluf"][0].__setitem__("window", _XSS),
        lambda d: d["phases"][0].__setitem__("window", _XSS),
        lambda d: d["forecast_hourly"]["hours"].__setitem__(0, _XSS),
        lambda d: d["risk_inputs"].__setitem__("refs_cycle", _XSS),
        lambda d: d["risk_inputs"].__setitem__("spc_category", _XSS),
        lambda d: d["risk_inputs"].__setitem__("gefs_p_precip", _XSS),
        lambda d: d["risk_inputs"].__setitem__("cape_jkg", "<b>1</b>"),
        lambda d: d["mission"].__setitem__("lat", _XSS),
    ],
    ids=[
        "bluf-window",
        "phase-window",
        "forecast-hour",
        "refs-cycle",
        "spc-category",
        "gefs-p-precip",
        "cape-jkg",
        "mission-lat",
    ],
)
def test_markup_payloads_rejected(mutate):
    data = _sample()
    mutate(data)
    with pytest.raises(ValidationError):
        BriefingResponse.model_validate(data)


def test_oversized_window_rejected():
    """Length caps back up the markup check: clock windows are short tokens."""
    with pytest.raises(ValidationError):
        BlufEntry(hazard="lightning", label="High", severity_class="sev-high", window="1" * 25)


def test_sub_model_defaults_match_contract_shapes():
    """Defaults keep the response constructible field-by-field (service path unchanged)."""
    assert ForecastTable().model_dump() == {"hours": [], "rows": []}
    assert MissionView().name == ""
    assert RiskInputsView().refs_in_range is None
    assert PhaseCard(phase="approach").window is None
