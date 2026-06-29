"""PDF export tests (FR-27).

Covers the server-side PDF path that previously had zero automated coverage:

* the print template and display-config the renderer depends on are present (a moved/renamed
  template would otherwise break PDF silently — the renderer raises only at request time);
* the ``POST /v1/briefing/pdf`` endpoint wiring — content type, attachment filename, and the
  graceful 503 fallback the PWA relies on when Chromium is unavailable (NFR-6);
* an actual headless-Chromium render end-to-end, **skipped** where no Chromium is reachable so
  the hermetic suite still runs everywhere while real environments (dev container, prod host)
  get true coverage. This is intentionally not a ``network`` test — it hits no live service,
  only local Chromium.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from upstreamwx.api.app import app, service
from upstreamwx.api.models import BriefingResponse, MissionSpec
from upstreamwx.sitrep import pdf as pdf_mod
from upstreamwx.sitrep.generate import generate_briefing
from upstreamwx.sitrep.pdf import _TEMPLATE, _chromium_path, render_pdf
from upstreamwx.sitrep.structured import to_structured

FIXTURES = Path(__file__).parent / "fixtures" / "sitrep"
SAMPLE_INPUTS = yaml.safe_load((FIXTURES / "sample_inputs.yaml").read_text())["inputs"]
FIXED_NOW = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)


def _spec(**overrides) -> MissionSpec:
    base = dict(
        lat=37.0192,
        lon=-111.9889,
        activity="canyon",
        start="2026-06-20T08:00",
        end="2026-06-20T18:00",
        name="Buckskin Gulch",
        slot=True,
        frame=False,
        inputs=SAMPLE_INPUTS,
    )
    base.update(overrides)
    return MissionSpec(**base)


def _structured_briefing() -> dict:
    """Build the JSON the PDF endpoint accepts, via the same path the API uses."""
    spec = _spec()
    gen = generate_briefing(
        spec.to_mission(), inputs=spec.to_inputs(), frame=False, generated_at=FIXED_NOW
    )
    resp = BriefingResponse(**to_structured(gen, cached=False, cache_cycle="static"))
    return resp.model_dump(mode="json")


@pytest.fixture
def client():
    os.environ["UPSTREAMWX_API_ENABLE_SCHEDULER"] = "0"
    service.cache.clear()
    with TestClient(app) as c:
        yield c
    service.cache.clear()


# -- template/assets present (hermetic) ---------------------------------------------------
def test_pdf_template_and_display_config_present():
    """The renderer reads these at request time; assert they exist so a move fails loudly here."""
    assert _TEMPLATE.exists(), f"PDF template missing: {_TEMPLATE}"
    display_config = _TEMPLATE.parent.parent / "data" / "display-config.json"
    assert display_config.exists(), f"display-config missing: {display_config}"


def test_chromium_path_no_crash():
    """``_chromium_path`` must always return a str path or None, never raise."""
    result = _chromium_path()
    assert result is None or isinstance(result, str)


# -- endpoint wiring (hermetic, render mocked) --------------------------------------------
def test_pdf_endpoint_returns_pdf(client, monkeypatch):
    async def _fake_render(briefing: dict) -> bytes:
        assert isinstance(briefing, dict) and briefing.get("mission")
        return b"%PDF-1.4 fake-bytes"

    # The endpoint does `from ..sitrep.pdf import render_pdf` at call time, so patching the
    # attribute on the module is enough.
    monkeypatch.setattr(pdf_mod, "render_pdf", _fake_render)

    resp = client.post("/v1/briefing/pdf", json=_structured_briefing())
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert "attachment" in resp.headers["content-disposition"]
    assert "Buckskin_Gulch" in resp.headers["content-disposition"]
    assert resp.content.startswith(b"%PDF")


def test_pdf_endpoint_template_missing_returns_503(client, monkeypatch):
    """A missing template surfaces as a 503 so the PWA falls back to the print path (NFR-6)."""
    async def _raise_missing(briefing: dict) -> bytes:
        raise FileNotFoundError("PDF template not found: /nope/briefing-pdf.html")

    monkeypatch.setattr(pdf_mod, "render_pdf", _raise_missing)
    resp = client.post("/v1/briefing/pdf", json=_structured_briefing())
    assert resp.status_code == 503


def test_pdf_endpoint_playwright_missing_returns_503(client, monkeypatch):
    """A missing playwright package surfaces as 503, not 500.

    render_pdf() imports playwright lazily at call time; the endpoint's outer ImportError
    check wraps that call so a ModuleNotFoundError maps to 503 (PWA falls back to the
    localStorage → ?print=1 path, NFR-6).  The earlier bug caught ImportError only around
    the pdf.py module import, which always succeeds.
    """
    async def _raise_import(briefing: dict) -> bytes:
        raise ModuleNotFoundError("No module named 'playwright'")

    monkeypatch.setattr(pdf_mod, "render_pdf", _raise_import)
    resp = client.post("/v1/briefing/pdf", json=_structured_briefing())
    assert resp.status_code == 503


# -- real render (skipped when no Chromium is reachable) ----------------------------------
def _chromium_available() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    # An explicit binary, or trust Playwright's own registry to auto-detect.
    return _chromium_path() is not None


@pytest.mark.skipif(not _chromium_available(), reason="no headless Chromium available")
def test_render_pdf_real_produces_pdf_bytes():
    pdf_bytes = asyncio.run(render_pdf(_structured_briefing()))
    assert pdf_bytes[:5] == b"%PDF-"
    # A real one-or-two page briefing is comfortably over a few KB; guards an empty render.
    assert len(pdf_bytes) > 2000
