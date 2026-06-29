"""UpstreamWX briefing API (M0.3).

Wraps the deterministic engine + M0.2 SITREP behind HTTP, with the server-side caching
and scheduled regeneration the PRD assumes (§7, §11; FR-12). The endpoint returns the
same briefing the CLI does for the same inputs (roadmap §M0.3) because both drive
:func:`upstreamwx.sitrep.generate.generate_briefing`.

Run locally::

    uvicorn upstreamwx.api.app:app --reload

Endpoints:
- ``POST /v1/briefing`` — mission spec -> briefing (structured + framed), cached.
- ``GET  /v1/health``   — liveness + the current refresh cycle.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json as _json
import logging
import multiprocessing
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..config import get_settings
from ..grib import cache as grib_cache
from ..sitrep.frame import _SYSTEM_PROMPT, DEFAULT_MODEL, _structured_view
from .cache import mission_cache_key
from .cycles import cycle_key, next_cycle
from .models import BriefingResponse, MissionSpec, WatershedWarmRequest
from .scheduler import run_scheduler
from .service import BriefingBusy, BriefingService

logger = logging.getLogger("upstreamwx.api")

# Single process-wide service so the cache and active-mission registry are shared across
# requests and the scheduler.
service = BriefingService()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the cycle-aligned refresh scheduler for the app's lifetime (FR-12)."""
    task: asyncio.Task | None = None
    stop = asyncio.Event()
    settings = get_settings()
    if settings.api_enable_scheduler:
        task = asyncio.create_task(run_scheduler(service, stop=stop))
        logger.info("briefing refresh scheduler started")
    if settings.api_enable_warm:
        service.start_warming()
        logger.info("watershed warm pool started")
    if settings.api_enable_decode_pool:
        # Spawn (not fork): briefings run inside nested thread pools, and fork() of a
        # multi-threaded process can deadlock on inherited locks. Spawn starts clean workers.
        grib_cache.configure_decode_cache(max_bytes=settings.decode_cache_max_bytes)
        pool = concurrent.futures.ProcessPoolExecutor(
            max_workers=settings.decode_pool_workers,
            mp_context=multiprocessing.get_context("spawn"),
        )
        grib_cache.set_decode_pool(pool)
        logger.info("GEFS decode pool started (%d spawn workers)", settings.decode_pool_workers)
    try:
        yield
    finally:
        service.stop_warming()
        grib_cache.shutdown_decode_pool(wait=False)
        if task is not None:
            stop.set()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutdown best-effort
                pass


app = FastAPI(
    title="UpstreamWX Briefing API",
    version="0.3",
    summary="Mission-specific multi-hazard weather briefings (reference only).",
    lifespan=lifespan,
)


@app.get("/v1/health")
def health() -> dict:
    """Liveness probe plus the current/next refresh cycle, cache size, release, and limits.

    ``release`` is the deployed version stamped into ``frontend/version.json`` by
    ``deploy/deploy.sh`` (docs/deployment-workflow.md). It makes "what's running" knowable
    from a curl — the field an uptime check and a rollback both want to confirm.

    ``limits`` echoes the effective runtime resource controls so "what is this box actually
    configured to do" is a one-curl check instead of sourcing the env file. ``decode_pool`` is
    the *actual* installed state (reflects the opt-in setting and any broken-pool fallback), not
    just the configured flag.
    """
    settings = get_settings()
    return {
        "status": "ok",
        "release": _release(),
        "cycle": cycle_key(),
        "next_cycle": next_cycle().isoformat(),
        "cached_briefings": len(service.cache),
        "active_missions": service.active_count,
        "limits": {
            "decode_pool": grib_cache.decode_pool_enabled(),
            "decode_pool_workers": settings.decode_pool_workers,
            "decode_cache_max_bytes": settings.decode_cache_max_bytes,
            "briefing_max_concurrency": settings.briefing_max_concurrency,
            "briefing_busy_timeout_s": settings.briefing_busy_timeout_s,
            "gefs_warm_fhours": len(settings.gefs_warm_fhours),
        },
    }


@app.post("/v1/briefing", response_model=BriefingResponse)
def briefing(spec: MissionSpec) -> BriefingResponse:
    """Generate (or return a cached) briefing for a mission spec.

    Non-mandatory source outages degrade gracefully (NFR-6): the briefing still renders
    with the missing input marked unavailable in ``sources_ok``/``degraded`` rather than
    erroring.

    When the host is at its concurrent-generation cap, returns 503 with ``Retry-After`` so the
    PWA shows a "busy — retry" banner instead of every request piling on and OOM-ing the host.
    """
    try:
        return service.get_briefing(spec)
    except BriefingBusy:
        raise HTTPException(
            status_code=503,
            detail="The briefing service is busy right now — please retry in a moment.",
            headers={"Retry-After": "10"},
        ) from None


@app.post("/v1/briefing/frame")
async def frame_stream(spec: MissionSpec) -> StreamingResponse:
    """Stream the Haiku plain-language narrative for a cached briefing as SSE (FR-21).

    The main ``/v1/briefing`` endpoint always skips Haiku so the structured posture
    data arrives immediately. The PWA calls this endpoint in parallel to stream the
    Risk Discussion text into the collapsed card as it generates.

    Returns 204 (no body) when no Anthropic API key is configured. Returns 404 when
    the matching briefing has not been cached yet — call ``/v1/briefing`` first.

    Each SSE event is ``data: <json>\\n\\n``. Chunks carry ``{"text": "..."}``; the
    terminal event carries ``{"done": true}``.
    """
    api_key = get_settings().anthropic_api_key
    if not api_key:
        return Response(status_code=204)

    key = mission_cache_key(spec.to_mission(), spec.to_inputs())
    result = service.get_result(key)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="No cached briefing for this spec — call /v1/briefing first.",
        )

    payload = _json.dumps(_structured_view(result), sort_keys=True, indent=2)

    async def generate():
        try:
            import anthropic as anthropic_lib

            client = anthropic_lib.AsyncAnthropic(api_key=api_key)
            async with client.messages.stream(
                model=DEFAULT_MODEL,
                max_tokens=500,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": payload}],
            ) as stream:
                async for text in stream.text_stream:
                    yield f"data: {_json.dumps({'text': text})}\n\n"
        except Exception:
            logger.exception("frame stream failed")
        yield 'data: {"done":true}\n\n'

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/v1/briefing/pdf")
async def briefing_pdf(briefing: BriefingResponse) -> Response:
    """Render the structured briefing as a downloadable PDF via headless Chromium (FR-27).

    Accepts the ``BriefingResponse`` JSON the PWA already holds in memory, renders it
    through the print-optimised ``frontend/pdf/briefing-pdf.html`` template server-side,
    and returns ``application/pdf`` bytes.  The browser receives an attachment with a
    descriptive filename — the user downloads and prints without any browser URL chrome
    or iOS print-preview trap.

    Requires ``playwright`` and the pre-installed Chromium (``/opt/pw-browsers/...``).
    Returns 503 when Playwright is unavailable so the client can fall back gracefully.
    """
    from ..sitrep.pdf import render_pdf  # pdf.py imports playwright lazily; always succeeds

    try:
        pdf_bytes = await render_pdf(briefing.model_dump(mode="json"))
    except ImportError as exc:
        # render_pdf() does `from playwright.async_api import async_playwright` at call time,
        # so a missing playwright package raises here — map to 503 so the PWA falls back to
        # the localStorage → ?print=1 path (NFR-6).
        raise HTTPException(
            status_code=503,
            detail="Server-side PDF rendering unavailable (playwright not installed).",
        ) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("pdf render failed")
        # Do NOT echo exc directly — Playwright surfaces the full Chromium launch log
        # (flags, pids, error lines) which is useless and alarming to end users.
        raise HTTPException(
            status_code=500, detail="PDF render failed — check server logs for details."
        ) from exc

    mission_name = (briefing.mission.get("name") or "briefing").replace(" ", "_")
    filename = f"upstreamwx_{mission_name}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/v1/watershed/warm", status_code=202)
def warm_watershed(req: WatershedWarmRequest) -> dict:
    """Pre-warm the pour-point watershed cache for a point (FR-3).

    Fire-and-forget: the planner calls this the moment coordinates change so the upstream
    basin delineates in the background while the user enters mission times. Returns 202
    immediately; the next briefing for the same point then skips the cold 3-15 s trace.
    """
    submitted = service.warm_watershed(req.lat, req.lon)
    return {"status": "submitted" if submitted else "noop"}


def _release() -> str:
    """Return the deployed release stamped in ``frontend/version.json``, or ``"dev"``.

    Written by ``deploy/deploy.sh`` at deploy time (git-ignored, regenerated per deploy).
    Best-effort: an unstamped checkout (local dev) just reports ``"dev"``.
    """
    fe = _frontend_dir()
    if fe is not None:
        try:
            data = _json.loads((fe / "version.json").read_text())
            return str(data.get("version") or "dev")
        except (OSError, ValueError):
            pass
    return "dev"


def _frontend_dir() -> Path | None:
    """Resolve the PWA directory to serve, or None to disable static serving (M0.4)."""
    configured = get_settings().frontend_dir
    if configured is not None:
        # An explicit empty value disables serving (decoupled deployment).
        return configured if str(configured) else None
    # Default: the repo's frontend/ relative to this package (src/upstreamwx/api/app.py).
    default = Path(__file__).resolve().parents[3] / "frontend"
    return default if default.is_dir() else None


# Serve the PWA single-origin (M0.4): the API routes above are registered first, so this
# catch-all mount only handles non-API paths. ``html=True`` serves index.html at "/".
_pwa = _frontend_dir()
if _pwa is not None:
    app.mount("/", StaticFiles(directory=_pwa, html=True), name="pwa")
    logger.info("serving PWA from %s", _pwa)


def main() -> None:
    """Console entry point: serve the API with uvicorn (``upstreamwx-api``)."""
    import uvicorn

    uvicorn.run("upstreamwx.api.app:app", host="0.0.0.0", port=8000)  # noqa: S104
