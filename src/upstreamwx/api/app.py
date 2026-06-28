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
import json as _json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..config import get_settings
from ..sitrep.frame import _SYSTEM_PROMPT, DEFAULT_MODEL, _structured_view
from .cache import mission_cache_key
from .cycles import cycle_key, next_cycle
from .models import BriefingResponse, MissionSpec, WatershedWarmRequest
from .scheduler import run_scheduler
from .service import BriefingService

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
    try:
        yield
    finally:
        service.stop_warming()
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
    """Liveness probe plus the current/next refresh cycle and cache size."""
    return {
        "status": "ok",
        "cycle": cycle_key(),
        "next_cycle": next_cycle().isoformat(),
        "cached_briefings": len(service.cache),
        "active_missions": service.active_count,
    }


@app.post("/v1/briefing", response_model=BriefingResponse)
def briefing(spec: MissionSpec) -> BriefingResponse:
    """Generate (or return a cached) briefing for a mission spec.

    Non-mandatory source outages degrade gracefully (NFR-6): the briefing still renders
    with the missing input marked unavailable in ``sources_ok``/``degraded`` rather than
    erroring.
    """
    return service.get_briefing(spec)


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


@app.post("/v1/watershed/warm", status_code=202)
def warm_watershed(req: WatershedWarmRequest) -> dict:
    """Pre-warm the pour-point watershed cache for a point (FR-3).

    Fire-and-forget: the planner calls this the moment coordinates change so the upstream
    basin delineates in the background while the user enters mission times. Returns 202
    immediately; the next briefing for the same point then skips the cold 3-15 s trace.
    """
    submitted = service.warm_watershed(req.lat, req.lon)
    return {"status": "submitted" if submitted else "noop"}


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
