"""Scheduled briefing regeneration aligned to the SREF/AFD cycle (PRD FR-12).

A background task that, on each SREF cycle boundary (:mod:`upstreamwx.api.cycles`),
regenerates the cached briefing for every active in-range mission so the PWA always
fetches a current briefing without paying generation cost on open (PRD §7, §11).

Scope note (roadmap §M0.1.1): the *always-on* cadence and cross-restart persistence are
host-dependent and validated on the EC2 instance, not in the ephemeral dev container.
What lives here is the host-independent machinery — boundary arithmetic
(``cycles``) and a single refresh pass (``BriefingService.refresh_active``) — both
directly unit-testable. :func:`run_scheduler` is the thin asyncio loop that drives them
on the real host; it does no work itself beyond sleeping to the next boundary.
"""

from __future__ import annotations

import asyncio
import logging

from ..config import get_settings
from .cycles import seconds_until_next_cycle
from .service import BriefingService

logger = logging.getLogger("upstreamwx.api.scheduler")

# How long to wait on a monitoring ping before giving up. Monitoring must never slow or
# block the scheduler, so this is short and all failures are swallowed.
_PING_TIMEOUT = 10


async def _ping(url: str | None, suffix: str = "") -> None:
    """Best-effort dead-man's-switch ping (Healthchecks.io semantics, FR-12 monitoring).

    ``suffix`` is "" (success), "/start", or "/fail". No-ops when ``url`` is unset. Runs
    the blocking request off the event loop and never raises — a monitoring outage must not
    affect refresh.
    """
    if not url:
        return
    target = url.rstrip("/") + suffix
    try:
        import requests

        await asyncio.to_thread(requests.get, target, timeout=_PING_TIMEOUT)
    except Exception:  # noqa: BLE001 — monitoring must never affect the scheduler
        logger.debug("healthcheck ping failed: %s", target, exc_info=True)


async def run_scheduler(service: BriefingService, *, stop: asyncio.Event | None = None) -> None:
    """Sleep to each SREF cycle boundary, then refresh active missions (FR-12).

    Runs until ``stop`` is set (or forever). Each refresh failure is logged and
    swallowed so a single bad cycle never kills the loop.
    """
    stop = stop or asyncio.Event()
    hc_url = get_settings().healthcheck_url
    while not stop.is_set():
        delay = seconds_until_next_cycle()
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
            return  # stop was signalled while waiting for the next boundary
        except TimeoutError:
            pass  # boundary reached — time to refresh
        # Tell the monitor a cycle has begun; if the run hangs or the process dies, the
        # missing success ping below is what trips the dead-man's-switch alert (FR-12).
        await _ping(hc_url, "/start")
        cycle_ok = True
        # Warm the persistent SREF + HREF grid caches for the new cycle first, so the refresh
        # below (and any incoming request) aggregates from the cached grids rather than
        # re-downloading per domain (roadmap §M0.1.1). A warm failure (NOMADS lag) is
        # logged and swallowed so refresh still runs from whatever is cached (NFR-6) — and,
        # consistent with that, a warm failure alone does not fail the heartbeat.
        # Both calls are synchronous and network/GRIB heavy (minutes on a cold cycle), so
        # they run via to_thread: executed inline they starve the event loop, freezing
        # /v1/health and every briefing request for the whole pass.
        try:
            warmed = await asyncio.to_thread(service.warm_and_prune)
            logger.info("scheduled warm cached %d ensemble field(s)", warmed)
        except Exception:  # noqa: BLE001 — a warm failure must not block refresh
            logger.exception("scheduled ensemble warm failed")
        try:
            count = await asyncio.to_thread(service.refresh_active)
            logger.info("scheduled refresh regenerated %d briefing(s)", count)
        except Exception:  # noqa: BLE001 — one bad cycle must not kill the scheduler
            cycle_ok = False
            logger.exception("scheduled refresh failed")
        # Success ping keeps the check green; a fail ping surfaces a broken cycle promptly
        # rather than waiting for the whole period+grace to lapse.
        await _ping(hc_url, "" if cycle_ok else "/fail")
