"""PDF export tests (FR-27).

Covers the server-side PDF path that previously had zero automated coverage:

* the print template and display-config the renderer depends on are present (a moved/renamed
  template would otherwise break PDF silently — the renderer raises only at request time);
* the ``POST /v1/briefing/pdf`` endpoint wiring — content type, attachment filename, and the
  graceful 503 fallback the PWA relies on when Chromium is unavailable (NFR-6);
* the C-2 hardening: request size cap (413), render-concurrency gate (503), whitelisted
  Content-Disposition filename, typed-model rejection of markup payloads (422), and the
  Playwright request gate that keeps the rendered page off file:// and the network;
* an actual headless-Chromium render end-to-end, **skipped** where no Chromium is reachable so
  the hermetic suite still runs everywhere while real environments (dev container, prod host)
  get true coverage. This is intentionally not a ``network`` test — it hits no live service,
  only local Chromium.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from upstreamwx.api.app import app, service
from upstreamwx.api.models import BriefingResponse, MissionSpec
from upstreamwx.sitrep import pdf as pdf_mod
from upstreamwx.sitrep.generate import generate_briefing
from upstreamwx.sitrep.pdf import (
    _TEMPLATE,
    _allowed_request_paths,
    _chromium_path,
    _is_allowed_request,
    render_pdf,
)
from upstreamwx.sitrep.structured import to_structured

# The package re-exports the FastAPI instance as `app`, shadowing the submodule under
# `import upstreamwx.api.app as ...`; resolve the module itself for monkeypatching.
app_mod = importlib.import_module("upstreamwx.api.app")

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


def test_pdf_endpoint_non_ascii_mission_name(client, monkeypatch):
    """Mission names with curly quotes / non-ASCII must not crash Content-Disposition.

    U+2019 (RIGHT SINGLE QUOTATION MARK) in a place name like "Robber’s Roost"
    caused a UnicodeEncodeError when Starlette encoded the header value as latin-1.
    """
    async def _fake_render(briefing: dict) -> bytes:
        return b"%PDF-1.4 fake-bytes"

    monkeypatch.setattr(pdf_mod, "render_pdf", _fake_render)

    payload = _structured_briefing()
    payload["mission"]["name"] = "Robber’s Roost"  # curly apostrophe
    resp = client.post("/v1/briefing/pdf", json=payload)
    assert resp.status_code == 200
    # curly apostrophe dropped; remaining ASCII chars preserved in filename
    assert "Robbers_Roost" in resp.headers["content-disposition"]


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


# -- C-2 hardening: size cap, concurrency gate, filename whitelist, payload validation ----
def test_pdf_endpoint_payload_too_large_returns_413(client):
    """Bodies past the ~2 MB cap are refused before JSON parsing (abuse guard, C-2)."""
    big = b'{"markdown": "' + b"a" * (2 * 1024 * 1024) + b'"}'
    resp = client.post(
        "/v1/briefing/pdf", content=big, headers={"content-type": "application/json"}
    )
    assert resp.status_code == 413


def test_pdf_endpoint_busy_returns_503_with_retry_after(client, monkeypatch):
    """A saturated render semaphore returns 503 + Retry-After (mirrors /v1/briefing)."""
    monkeypatch.setattr(app_mod, "_pdf_sem", asyncio.Semaphore(0))
    monkeypatch.setattr(app_mod, "_PDF_BUSY_TIMEOUT_S", 0.05)
    resp = client.post("/v1/briefing/pdf", json=_structured_briefing())
    assert resp.status_code == 503
    assert resp.headers.get("retry-after") == "10"


def test_pdf_endpoint_filename_is_whitelisted(client, monkeypatch):
    """Content-Disposition filename chars are whitelisted to [A-Za-z0-9._-] (C-2).

    Quotes and separators in a mission name could otherwise alter how a browser parses
    the header (filename smuggling / header confusion).
    """
    async def _fake_render(briefing: dict) -> bytes:
        return b"%PDF-1.4 fake-bytes"

    monkeypatch.setattr(pdf_mod, "render_pdf", _fake_render)
    payload = _structured_briefing()
    payload["mission"]["name"] = 'evil"; x=$(rm) \\..\\ name'
    resp = client.post("/v1/briefing/pdf", json=payload)
    assert resp.status_code == 200
    m = re.search(r'filename="([^"]+)"', resp.headers["content-disposition"])
    assert m, resp.headers["content-disposition"]
    assert re.fullmatch(r"[A-Za-z0-9._-]+", m.group(1)), m.group(1)
    assert m.group(1).startswith("upstreamwx_") and m.group(1).endswith(".pdf")


def test_pdf_endpoint_rejects_markup_payload_with_422(client):
    """Markup where the template expects a clock window fails model validation (C-2)."""
    payload = _structured_briefing()
    payload["bluf"][0]["window"] = '<img src=x onerror=alert(1)>'
    resp = client.post("/v1/briefing/pdf", json=payload)
    assert resp.status_code == 422


def test_pdf_endpoint_rejects_non_numeric_risk_input_with_422(client):
    """risk_inputs numbers must be numbers — string payloads are refused (C-2)."""
    payload = _structured_briefing()
    payload["risk_inputs"]["gefs_p_precip"] = "<b>99</b>"
    resp = client.post("/v1/briefing/pdf", json=payload)
    assert resp.status_code == 422


# -- C-2 hardening: the rendered page's request gate --------------------------------------
def test_request_gate_allows_only_template_and_logo():
    """The page may load the template and its logo; file:// and http(s) are aborted."""
    allowed = _allowed_request_paths(_TEMPLATE)
    assert _is_allowed_request(_TEMPLATE.as_uri(), allowed)
    assert _is_allowed_request((_TEMPLATE.parent / "logo-light.png").as_uri(), allowed)
    assert not _is_allowed_request("file:///etc/passwd", allowed)
    assert not _is_allowed_request("file:///etc/../etc/passwd", allowed)
    assert not _is_allowed_request((_TEMPLATE.parent.parent / "index.html").as_uri(), allowed)
    assert not _is_allowed_request("https://example.com/exfil", allowed)
    assert not _is_allowed_request("http://169.254.169.254/latest/meta-data/", allowed)
    assert not _is_allowed_request("data:text/html,<script>1</script>", allowed)


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


# -- template escaping under a hostile briefing (defence in depth, skipped w/o Chromium) --
_XSS = '<img src=x onerror="window.__pwned=1">'


def _hostile_briefing() -> dict:
    """A briefing with markup in every field the template historically trusted.

    Built *past* the pydantic layer on purpose: this exercises the template's own esc()
    discipline so the ?print=1 client path (which never crosses the API model) is covered.
    """
    b = _structured_briefing()
    b["mission"]["name"] = _XSS
    b["mission"]["timezone"] = _XSS
    b["generated_at"] = _XSS  # fmtWhen() passes unparseable input through
    b["cache_cycle"] = _XSS
    b["summary"] = _XSS
    b["bluf"][0]["window"] = _XSS
    b["phases"][0]["window"] = _XSS
    b["forecast_hourly"] = {"hours": [_XSS], "rows": [{"label": _XSS, "values": [_XSS]}]}
    b["risk_inputs"] = {**b["risk_inputs"], "refs_cycle": _XSS, "gefs_p_precip": _XSS}
    return b


async def _dom_after_hostile_render() -> tuple[object, int, str]:
    """Load the template exactly like render_pdf does, but inspect the DOM, not the PDF."""
    import json

    from playwright.async_api import async_playwright

    launch_kwargs: dict = {
        "headless": True,
        "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-crash-reporter"],
    }
    exe = _chromium_path()
    if exe:
        launch_kwargs["executable_path"] = exe
    allowed = _allowed_request_paths(_TEMPLATE)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        try:
            page = await browser.new_page()
            await page.add_init_script(
                f"window.__BRIEFING__ = {json.dumps(_hostile_briefing())};"
            )
            await page.add_init_script("window.__DISPLAY_CONFIG__ = {};")

            async def _gate(route):
                if _is_allowed_request(route.request.url, allowed):
                    await route.continue_()
                else:
                    await route.abort()

            await page.route("**/*", _gate)
            await page.goto(_TEMPLATE.as_uri(), wait_until="networkidle", timeout=30_000)
            pwned = await page.evaluate("window.__pwned === undefined ? null : window.__pwned")
            # The only <img> in the document must be the masthead logo.
            injected_imgs = await page.evaluate(
                "Array.from(document.images).filter(i => !i.src.endsWith('logo-light.png')).length"
            )
            sheet_text = await page.inner_text("#sheet")
        finally:
            await browser.close()
    return pwned, injected_imgs, sheet_text


@pytest.mark.skipif(not _chromium_available(), reason="no headless Chromium available")
def test_template_escapes_hostile_briefing_fields():
    """Injected markup renders as inert text: no element creation, no script execution."""
    pwned, injected_imgs, sheet_text = asyncio.run(_dom_after_hostile_render())
    assert pwned is None, "onerror payload executed — template escaping regressed"
    assert injected_imgs == 0, "hostile <img> element reached the DOM"
    # The payload is still *displayed* (escaped), proving it went through esc(), not dropped.
    assert "<img src=x" in sheet_text
