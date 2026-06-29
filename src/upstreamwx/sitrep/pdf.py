"""Server-side PDF export via headless Chromium / Playwright (FR-27).

Renders the existing print-optimised template (frontend/pdf/briefing-pdf.html) with
the live structured briefing injected as ``window.__BRIEFING__``, bypassing the browser
print dialog entirely.  The caller receives raw PDF bytes suitable for streaming back
as ``application/pdf``.

Why headless render instead of a pure-Python PDF library:
  The template already contains the complete, reviewed layout (running footer on every
  page, severity colour ladder, mission metadata, phase breakdown, hourly table, source
  drill-down, reference-only disclaimer).  Replicating that in a library would duplicate
  and inevitably drift.  Rendering the template guarantees PDF and PWA/print views stay
  in sync with zero extra maintenance surface.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

logger = logging.getLogger("upstreamwx.pdf")

# Explicit paths checked before falling back to PATH / Playwright auto-detection.
# Ordered from most-specific (versioned dev-container path) to most-generic.
_CHROMIUM_CANDIDATES = [
    # Claude Code managed dev container (PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers)
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/opt/pw-browsers/chromium/chrome-linux/chrome",
]

# System binary names searched via PATH when no explicit path matches.
# Covers: Playwright-managed install, system apt/dnf package, snap wrapper.
_CHROMIUM_WHICH = ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable"]

# The print template relative to this package (src/upstreamwx/sitrep/pdf.py).
_TEMPLATE = Path(__file__).resolve().parents[3] / "frontend" / "pdf" / "briefing-pdf.html"


def _chromium_path() -> str | None:
    """Return a usable Chromium binary path, or None to let Playwright auto-detect.

    Search order:
    1. Explicit hardcoded paths (dev container, common install locations)
    2. PATH via shutil.which (covers apt/dnf system packages, snap wrappers,
       and Playwright-managed installs when PLAYWRIGHT_BROWSERS_PATH is set)
    3. None → Playwright searches its own registry (works if `playwright install`
       succeeded for this distro)
    """
    for p in _CHROMIUM_CANDIDATES:
        if Path(p).exists():
            return p
    for name in _CHROMIUM_WHICH:
        found = shutil.which(name)
        if found:
            return found
    return None


async def render_pdf(briefing: dict) -> bytes:
    """Render *briefing* through the HTML template and return PDF bytes (FR-27).

    Uses Playwright's async API with the pre-installed headless Chromium.  The
    structured briefing is injected as ``window.__BRIEFING__`` via an init script
    executed before the page's own boot sequence, so the template reads it directly
    without localStorage or a network fetch.

    ``@page { margin: 0 18mm }`` in the template suppresses the browser's own URL /
    date headers and supplies the reference-only footer through the in-document
    ``<tfoot>`` that repeats on every printed page (Safari-safe layout table trick).
    Playwright's ``display_header_footer=False`` removes any remaining Chromium chrome.
    """
    from playwright.async_api import async_playwright

    template_path = _TEMPLATE
    if not template_path.exists():
        raise FileNotFoundError(f"PDF template not found: {template_path}")

    exe = _chromium_path()
    launch_kwargs: dict = {
        "headless": True,
        # --no-sandbox: Chromium's renderer sandbox uses Linux user namespaces, which the
        # production systemd unit restricts (RestrictNamespaces=true).  We only ever load
        # a local file:// URL we generate, so losing the sandbox here has no security impact.
        # --disable-dev-shm-usage: avoids /dev/shm exhaustion in constrained environments
        # (systemd PrivateTmp, containers); Chromium falls back to /tmp instead.
        "args": ["--no-sandbox", "--disable-dev-shm-usage"],
    }
    if exe:
        launch_kwargs["executable_path"] = exe

    briefing_json = json.dumps(briefing)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        try:
            page = await browser.new_page()
            # Inject before any page script runs so boot() sees window.__BRIEFING__.
            await page.add_init_script(f"window.__BRIEFING__ = {briefing_json};")
            await page.goto(
                template_path.as_uri(),
                wait_until="networkidle",
                timeout=30_000,
            )
            pdf_bytes = await page.pdf(
                format="Letter",
                print_background=True,
                display_header_footer=False,
                # Let the template's @page CSS own all geometry (size, margins,
                # running footer placement).  prefer_css_page_size=True prevents
                # Playwright's own margin defaults from overriding @page rules.
                prefer_css_page_size=True,
            )
        finally:
            await browser.close()

    logger.info("pdf rendered: %d bytes", len(pdf_bytes))
    return pdf_bytes
