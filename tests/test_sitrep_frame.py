"""Tests for the Haiku framing layer — the FR-20 guarantee that framing never changes
a posture. Offline tests use a mocked client; one network-gated test exercises live Haiku.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from upstreamwx.config import get_settings
from upstreamwx.engine.assess import assess
from upstreamwx.engine.models import ActivityType, HazardInputs, Mission
from upstreamwx.sitrep import frame as frame_mod
from upstreamwx.sitrep import frame_briefing, render_md


def _result():
    mission = Mission(
        activity_type=ActivityType.CANYON, lat=37.0, lon=-112.0,
        window_start=datetime(2026, 6, 20, 8), window_end=datetime(2026, 6, 20, 18),
        name="Test",
    )
    inputs = HazardInputs(gefs_p_precip=65, measurable_precip=True, gefs_p_tstm=50,
                          heat_index_f=95, apparent_temp_f=92)
    return assess(mission, inputs)


class _FakeClient:
    """Minimal stand-in for anthropic.Anthropic recording the request and returning text."""

    def __init__(self, text: str):
        self._text = text
        self.messages = SimpleNamespace(create=self._create)
        self.last_kwargs = None

    def _create(self, **kwargs):
        self.last_kwargs = kwargs
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self._text)])


def test_framing_skipped_without_key(monkeypatch):
    """No key and no client -> structured render returned unchanged (graceful)."""
    monkeypatch.setattr(frame_mod, "get_settings", lambda: SimpleNamespace(anthropic_api_key=None))
    result = _result()
    structured = render_md(result)
    assert frame_briefing(result, structured) == structured


def test_framing_prepends_summary_and_preserves_structure():
    result = _result()
    structured = render_md(result)
    client = _FakeClient("Plain-language overview of the mission hazards.")

    framed = frame_briefing(result, structured, client=client)

    # Summary added above the BLUF, structured content below left byte-for-byte intact.
    assert "## SUMMARY" in framed
    assert "Plain-language overview" in framed
    assert framed.index("## SUMMARY") < framed.index("## BLUF")
    bluf_onward = structured[structured.index("## BLUF"):]
    assert bluf_onward in framed  # every structured posture line unchanged (FR-20)


def test_framing_sends_model_and_structured_json():
    result = _result()
    client = _FakeClient("narrative")
    frame_briefing(result, render_md(result), client=client)
    assert client.last_kwargs["model"] == frame_mod.DEFAULT_MODEL
    # The structured object is what the model narrates; postures are in the payload.
    payload = client.last_kwargs["messages"][0]["content"]
    assert "overall_posture" in payload and "flash_flood" in payload


def test_empty_narrative_falls_back_to_structured():
    result = _result()
    structured = render_md(result)
    assert frame_briefing(result, structured, client=_FakeClient("   ")) == structured


@pytest.mark.network
def test_live_haiku_preserves_all_postures():
    """Live Haiku framing must not alter any structured posture line (FR-20)."""
    if not get_settings().anthropic_api_key:
        pytest.skip("ANTHROPIC_API_KEY not set")
    result = _result()
    structured = render_md(result)
    framed = frame_briefing(result, structured)
    assert structured[structured.index("## BLUF"):] in framed
