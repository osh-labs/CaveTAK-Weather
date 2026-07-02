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
import math
import multiprocessing
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from ..config import get_settings
from ..grib import cache as grib_cache
from ..sitrep.frame import _SYSTEM_PROMPT, DEFAULT_MODEL, _structured_view
from .cache import mission_cache_key
from .cycles import cycle_key, next_cycle
from .models import BriefingResponse, MissionSpec, MissionWindowError, WatershedWarmRequest
from .scheduler import run_scheduler
from .service import BriefingBusy, BriefingService, WarmQueueFull

logger = logging.getLogger("upstreamwx.api")

# Single process-wide service so the cache and active-mission registry are shared across
# requests and the scheduler.
service = BriefingService()

# PDF export hardening: the endpoint renders *client-supplied* JSON in headless Chromium, so
# the raw body is size-capped before parsing (a legitimate structured briefing is well under
# 1 MB even with a large watershed ring) and renders are gated by a small semaphore mirroring
# the service's _gen_sem — Chromium is the most expensive thing this API can launch on the
# ~2 GB production host, so concurrency stays low (do not raise it there).
_PDF_MAX_BODY_BYTES = 2 * 1024 * 1024
_PDF_RENDER_CONCURRENCY = 2
_PDF_BUSY_TIMEOUT_S = 10.0
_pdf_sem = asyncio.Semaphore(_PDF_RENDER_CONCURRENCY)
# Content-Disposition filename whitelist: anything outside [A-Za-z0-9._-] could alter how a
# browser parses the header (quotes, separators), so it is replaced rather than escaped.
_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

# How long shutdown waits for the cancelled scheduler task before abandoning it, so a pass
# stuck in a worker thread can never hang process exit.
_SCHEDULER_SHUTDOWN_TIMEOUT_S = 10.0


# -----------------------------------------------------------------------------------------
# Per-IP rate limiting on the expensive/billable endpoints (H-8), dependency-free.
#
# /v1/briefing itself is deliberately NOT rate-limited here: cache hits must stay cheap, and
# cold generations are already bounded by the service's _gen_sem (BriefingBusy -> 503).
# nginx's edge limit_req (deploy/nginx/upstreamwx.conf) still applies in front; this is the
# app's own defence when it is reached directly or the edge config drifts. Gated by the
# ``api_rate_limits_enabled`` setting (checked per request, default on).
# -----------------------------------------------------------------------------------------
_FRAME_RATE_PER_MIN = 6  # billable Anthropic calls — strictest
_PDF_RATE_PER_MIN = 4  # each render launches headless Chromium
_WARM_RATE_PER_MIN = 12  # each distinct point is a 3-15 s USGS delineation
# LRU cap over tracked client IPs so the limiter itself can never become a memory sink
# (an evicted idle IP simply starts over with a full bucket).
_RATE_LIMIT_MAX_IPS = 4096

# Peers we treat as "our local nginx" for X-Forwarded-For purposes (see _client_ip).
_LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"})


class _TokenBucketLimiter:
    """A small thread-safe per-key token bucket (H-8).

    Capacity equals one minute's budget (``rate_per_min``), refilled continuously, so a
    normal session's burst passes and a sustained flood is held to the configured rate.
    The bucket map is LRU-bounded at ``max_ips`` entries.
    """

    def __init__(self, rate_per_min: float, *, max_ips: int = _RATE_LIMIT_MAX_IPS) -> None:
        self._rate_per_s = rate_per_min / 60.0
        self._capacity = float(rate_per_min)
        self._max_ips = max(1, max_ips)
        # ip -> (tokens remaining, monotonic stamp of the last update)
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def acquire(self, ip: str, *, now: float | None = None) -> int | None:
        """Take one token for ``ip``: None when granted, else whole seconds to Retry-After."""
        now = time.monotonic() if now is None else now
        with self._lock:
            tokens, stamp = self._buckets.pop(ip, (self._capacity, now))
            tokens = min(self._capacity, tokens + (now - stamp) * self._rate_per_s)
            granted = tokens >= 1.0
            if granted:
                tokens -= 1.0
            self._buckets[ip] = (tokens, now)  # (re)insert as most recently used
            while len(self._buckets) > self._max_ips:
                self._buckets.popitem(last=False)
            if granted:
                return None
            return max(1, math.ceil((1.0 - tokens) / self._rate_per_s))

    def __len__(self) -> int:
        with self._lock:
            return len(self._buckets)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


_frame_limiter = _TokenBucketLimiter(_FRAME_RATE_PER_MIN)
_pdf_limiter = _TokenBucketLimiter(_PDF_RATE_PER_MIN)
_warm_limiter = _TokenBucketLimiter(_WARM_RATE_PER_MIN)


def _client_ip(request: Request) -> str:
    """Resolve the client IP the rate limiter keys on (H-8).

    nginx fronts the app on the same host and builds X-Forwarded-For with
    ``$proxy_add_x_forwarded_for`` (deploy/nginx/upstreamwx.conf), i.e. it *appends* the
    peer address it verified — so the RIGHTMOST entry is the one trusted hop; everything
    left of it is client-supplied and spoofable. The header is therefore honored only when
    the direct peer is loopback (the request came through our local nginx). A direct hit
    on uvicorn keys on the socket peer, and a forged XFF from a non-loopback peer is
    ignored — otherwise any client could dodge the limiter with a random header.
    """
    peer = request.client.host if request.client else "unknown"
    if peer in _LOOPBACK_PEERS:
        forwarded = request.headers.get("x-forwarded-for", "")
        hop = forwarded.rsplit(",", 1)[-1].strip()
        if hop:
            return hop
    return peer


def _enforce_rate_limit(limiter: _TokenBucketLimiter, request: Request) -> None:
    """Raise 429 + Retry-After when ``request``'s client is over ``limiter``'s budget."""
    if not get_settings().api_rate_limits_enabled:
        return
    retry_after = limiter.acquire(_client_ip(request))
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded for this endpoint — please retry shortly.",
            headers={"Retry-After": str(retry_after)},
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the cycle-aligned refresh scheduler for the app's lifetime (FR-12)."""
    task: asyncio.Task | None = None
    stop = asyncio.Event()
    settings = get_settings()
    # Fresh rate-limit buckets per app run: a no-op in production (one lifespan per
    # process) that also isolates TestClient contexts from each other without env plumbing.
    for limiter in (_frame_limiter, _pdf_limiter, _warm_limiter):
        limiter.reset()
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
                await asyncio.wait_for(task, timeout=_SCHEDULER_SHUTDOWN_TIMEOUT_S)
            except asyncio.CancelledError:
                pass  # normal path: the task acknowledged the cancel
            except TimeoutError:
                # A refresh pass stuck in a worker thread cannot be interrupted; abandon
                # the task so shutdown completes instead of hanging the process.
                logger.warning(
                    "scheduler task did not exit within %.0fs at shutdown; abandoning it",
                    _SCHEDULER_SHUTDOWN_TIMEOUT_S,
                )
            except Exception:  # shutdown is best-effort; log instead of masking exit
                logger.exception("scheduler task raised during shutdown")


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
            "active_missions_max": settings.api_active_missions_max,
            "warm_pending_max": settings.api_warm_pending_max,
            "rate_limits_enabled": settings.api_rate_limits_enabled,
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
    A live window outside the serviceable forecast horizon is a 422 (H-8); no rate limit here —
    cache hits must stay cheap, and cold generations are already bounded by the busy cap.
    """
    try:
        return service.get_briefing(spec)
    except MissionWindowError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except BriefingBusy:
        raise HTTPException(
            status_code=503,
            detail="The briefing service is busy right now — please retry in a moment.",
            headers={"Retry-After": "10"},
        ) from None


@app.post("/v1/briefing/frame")
async def frame_stream(spec: MissionSpec, request: Request) -> StreamingResponse:
    """Stream the Haiku plain-language narrative for a cached briefing as SSE (FR-21).

    The main ``/v1/briefing`` endpoint always skips Haiku so the structured posture
    data arrives immediately. The PWA calls this endpoint in parallel to stream the
    Risk Discussion text into the collapsed card as it generates.

    Every call is a billable Anthropic request, so this is the strictest per-IP rate
    limit in the API (429 + Retry-After past the budget, H-8). Returns 204 (no body)
    when no Anthropic API key is configured. Returns 404 when the matching briefing has
    not been cached yet — call ``/v1/briefing`` first.

    Each SSE event is ``data: <json>\\n\\n``. Chunks carry ``{"text": "..."}``; the
    terminal event carries ``{"done": true}``.
    """
    _enforce_rate_limit(_frame_limiter, request)
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
async def briefing_pdf(request: Request) -> Response:
    """Render the structured briefing as a downloadable PDF via headless Chromium (FR-27).

    Accepts the ``BriefingResponse`` JSON the PWA already holds in memory, renders it
    through the print-optimised ``frontend/pdf/briefing-pdf.html`` template server-side,
    and returns ``application/pdf`` bytes.  The browser receives an attachment with a
    descriptive filename — the user downloads and prints without any browser URL chrome
    or iOS print-preview trap.

    The payload is client-supplied and is rendered in a real browser, so it is treated as
    hostile: the raw body is rejected past ``_PDF_MAX_BODY_BYTES`` (413), the parsed JSON
    must validate against :class:`BriefingResponse` (whose sub-models block markup in the
    fields the template trusts), and concurrent renders are capped by ``_pdf_sem`` (503
    with ``Retry-After`` when saturated, mirroring ``/v1/briefing``'s busy behaviour).

    Requires ``playwright`` and the pre-installed Chromium (``/opt/pw-browsers/...``).
    Returns 503 when Playwright is unavailable so the client can fall back gracefully.
    """
    from ..sitrep.pdf import render_pdf  # pdf.py imports playwright lazily; always succeeds

    # Rate limit before touching the body: Chromium renders are the most expensive thing
    # this API can launch, so an over-budget client is turned away at zero cost (H-8).
    _enforce_rate_limit(_pdf_limiter, request)
    # Cheap header check first, then an authoritative check on the actual bytes (the
    # Content-Length header is client-supplied and absent on chunked uploads).
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > _PDF_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Briefing payload too large for PDF export.")
    body = await request.body()
    if len(body) > _PDF_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Briefing payload too large for PDF export.")
    try:
        briefing = BriefingResponse.model_validate_json(body)
    except ValidationError as exc:
        # Same 422 shape FastAPI would emit for a typed body parameter; strip context so
        # the response never echoes raw exception objects.
        raise RequestValidationError(
            exc.errors(include_url=False, include_context=False, include_input=False)
        ) from exc

    # Bound concurrent Chromium launches like the service bounds cold generations
    # (_gen_sem): a burst of PDF requests would otherwise fork N browsers and OOM the
    # small host. Wait briefly for a slot, then tell the client to retry.
    try:
        await asyncio.wait_for(_pdf_sem.acquire(), timeout=_PDF_BUSY_TIMEOUT_S)
    except TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="The PDF renderer is busy right now — please retry in a moment.",
            headers={"Retry-After": "10"},
        ) from None
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
    finally:
        _pdf_sem.release()

    raw_name = briefing.mission.name or "briefing"
    # HTTP headers must be latin-1; mission names can contain curly quotes or other
    # non-ASCII Unicode (e.g. U+2019 RIGHT SINGLE QUOTATION MARK from the Haiku framing
    # or copy-pasted place names).  NFKD normalisation converts accented chars to their
    # ASCII base; encode("ascii","ignore") drops anything that doesn't decompose cleanly,
    # then the whitelist replaces every remaining unsafe byte (quotes, separators, control
    # chars) so the name can never break out of the quoted Content-Disposition value.
    ascii_name = unicodedata.normalize("NFKD", raw_name).encode("ascii", "ignore").decode("ascii")
    mission_name = _FILENAME_UNSAFE.sub("_", ascii_name).strip("._") or "briefing"
    filename = f"upstreamwx_{mission_name}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/v1/watershed/warm", status_code=202)
def warm_watershed(req: WatershedWarmRequest, request: Request) -> dict:
    """Pre-warm the pour-point watershed cache for a point (FR-3).

    Fire-and-forget: the planner calls this the moment coordinates change so the upstream
    basin delineates in the background while the user enters mission times. Returns 202
    immediately; the next briefing for the same point then skips the cold 3-15 s trace.

    Backpressure (H-8): coordinates must be inside CONUS (422, enforced by the request
    model), per-IP requests are rate limited (429), and a saturated warm queue returns 503
    with ``Retry-After`` — a refused warm only forfeits the latency win (NFR-6).
    """
    _enforce_rate_limit(_warm_limiter, request)
    try:
        submitted = service.warm_watershed(req.lat, req.lon)
    except WarmQueueFull:
        raise HTTPException(
            status_code=503,
            detail="The watershed warm queue is full — please retry shortly.",
            headers={"Retry-After": "30"},
        ) from None
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
