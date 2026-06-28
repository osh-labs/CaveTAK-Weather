"""Briefing service: cache-aware generation + the active-mission registry (M0.3).

This is the seam between the HTTP layer and the M0.2 generation core. It:

- serves a cached briefing when one is valid for the current cycle, else generates and
  caches (PRD §7, §11) — so reopening the app is free;
- registers in-range live missions so the scheduler can regenerate them on the SREF/AFD
  cycle (FR-12, :mod:`upstreamwx.api.scheduler`);
- always returns a briefing, marking unavailable sources rather than failing (NFR-6).
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime

from .. import href
from ..config import get_settings
from ..engine.models import BriefingResult, Mission
from ..sitrep.generate import GeneratedBriefing, generate_briefing
from ..sitrep.structured import to_structured
from ..sref import latest_available_cycle, prune_old_cycles, warm_cycle
from ..watershed.cache import _key as watershed_key
from ..watershed.cache import delineate_cached
from .cache import STATIC_TOKEN, BriefingCache, mission_cache_key
from .cycles import cycle_key
from .models import BriefingResponse, MissionSpec

logger = logging.getLogger("upstreamwx.api.service")


@dataclass
class _Registered:
    """A live mission tracked for scheduled refresh while its window is in range."""

    mission: Mission
    frame: bool | None
    key: str


class BriefingService:
    """Orchestrates caching, generation, and refresh registration for briefings."""

    def __init__(self, cache: BriefingCache | None = None) -> None:
        self.cache = cache or BriefingCache()
        self._active: dict[str, _Registered] = {}
        # Stores the engine BriefingResult alongside its cache key so the streaming
        # framing endpoint can call Haiku without re-running ingest.
        self._result_store: dict[str, BriefingResult] = {}
        # Watershed warming: a bounded pool fills the pour-point cache in the background
        # the moment the planner reports a new point, de-duped by cache key so a dragged
        # marker can't flood it. Created in start_warming() (app lifespan), not at import.
        self._warm_pool: ThreadPoolExecutor | None = None
        self._warm_pending: set[str] = set()
        self._warm_lock = threading.Lock()

    # -- request path ---------------------------------------------------------------
    def get_briefing(
        self, spec: MissionSpec, *, now: datetime | None = None
    ) -> BriefingResponse:
        """Return a briefing for ``spec``, from cache when valid or freshly generated."""
        now = now or datetime.now(UTC)
        mission = spec.to_mission()
        inputs = spec.to_inputs()
        key = mission_cache_key(mission, inputs)
        # Explicit inputs are deterministic -> never expire; live briefings are valid
        # for the current SREF cycle only (FR-12).
        token = STATIC_TOKEN if inputs is not None else cycle_key(now)

        cached = self.cache.get(key, token)
        if cached is not None:
            return self._response(cached, token, cached=True)

        # Haiku framing is always deferred to the streaming /v1/briefing/frame endpoint
        # so the structured posture data is returned immediately without waiting for the
        # LLM call.  The engine result is stored in _result_store for the frame endpoint.
        briefing = generate_briefing(mission, inputs=inputs, frame=False, generated_at=now)
        self._result_store[key] = briefing.result
        self.cache.put(key, briefing, token)
        # Track live, still-in-range missions for the scheduler (FR-12). Deterministic
        # offline briefings need no refresh, so they are not registered.
        if inputs is None and now < _as_utc(mission.window_end):
            self._active[key] = _Registered(mission=mission, frame=False, key=key)
        return self._response(briefing, token, cached=False)

    def get_result(self, key: str) -> BriefingResult | None:
        """Return the cached engine result for ``key``, or None on miss.

        Used by the streaming frame endpoint: the ingest pipeline has already run
        for the briefing, so only the Haiku call remains.
        """
        return self._result_store.get(key)

    # -- scheduled refresh ----------------------------------------------------------
    def warm_and_prune(self, *, now: datetime | None = None) -> int:
        """Pre-pull the live SREF + HREF cycles into the persistent cache and prune old ones.

        Run by the scheduler each cycle boundary before :meth:`refresh_active`. SREF's CONUS
        subset is downloaded once so every domain aggregates from the cached grid; HREF's
        f06-f48 are warmed so a mission's spin-up hours are served from a prior run's mature
        forecast (roadmap §M0.1.1, FR-7, FR-12). Each ensemble is warmed independently — a
        missing/unpublished cycle for one does not block the other. Returns the total number
        of fields warmed; 0 when neither is live yet (production lag), which is non-fatal —
        refresh still runs from whatever is cached (NFR-6).
        """
        settings = get_settings()
        warmed = 0

        scycle = latest_available_cycle(now=now)
        if scycle is not None:
            warmed += len(warm_cycle(scycle, settings=settings))
            prune_old_cycles(settings=settings, keep=settings.sref_cache_keep_cycles)

        hcycle = href.latest_available_cycle(now=now)
        if hcycle is not None:
            warmed += len(href.warm_cycle(hcycle, settings=settings))
            href.prune_old_cycles(settings=settings, keep=settings.href_cache_keep_cycles)

        return warmed

    def refresh_active(self, *, now: datetime | None = None) -> int:
        """Regenerate every in-range active mission into the current cycle (FR-12).

        Drops missions whose window has ended. Returns the number regenerated. This is
        the unit the always-on scheduler calls each cycle (see ``scheduler.py``).
        """
        now = now or datetime.now(UTC)
        token = cycle_key(now)
        regenerated = 0
        for key, reg in list(self._active.items()):
            if now >= _as_utc(reg.mission.window_end):
                del self._active[key]  # mission is over; stop refreshing it
                continue
            briefing = generate_briefing(reg.mission, frame=False, generated_at=now)
            self._result_store[key] = briefing.result
            self.cache.put(key, briefing, token)
            regenerated += 1
        return regenerated

    @property
    def active_count(self) -> int:
        return len(self._active)

    # -- watershed warming ----------------------------------------------------------
    def start_warming(self, *, max_workers: int = 2) -> None:
        """Start the background watershed-warming pool (called from the app lifespan)."""
        if self._warm_pool is None:
            self._warm_pool = ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix="ws-warm"
            )

    def stop_warming(self) -> None:
        """Tear the pool down without blocking shutdown on an in-flight trace."""
        pool, self._warm_pool = self._warm_pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def warm_watershed(self, lat: float, lon: float) -> bool:
        """Fire a background pour-point delineation for ``(lat, lon)`` (FR-3).

        Returns True if a warm was submitted, False if warming is disabled or an
        identical point is already in flight. ``radius_km`` is intentionally not a
        parameter: delineation depends only on the pour point, and the Radius-of-Concern
        clip is cheap post-processing the briefing applies later.
        """
        if self._warm_pool is None:
            return False
        key = watershed_key(lat, lon)
        with self._warm_lock:
            if key in self._warm_pending:
                return False
            self._warm_pending.add(key)
        self._warm_pool.submit(self._warm_job, lat, lon, key)
        return True

    def _warm_job(self, lat: float, lon: float, key: str) -> None:
        try:
            delineate_cached(lat, lon)  # populates the disk cache; joins single-flight
        except Exception:
            # Best-effort: a failed warm just means the briefing pays the cold cost (NFR-6).
            logger.exception("watershed warm failed for %s", key)
        finally:
            with self._warm_lock:
                self._warm_pending.discard(key)

    # -- helpers --------------------------------------------------------------------
    def _response(
        self, briefing: GeneratedBriefing, token: str, *, cached: bool
    ) -> BriefingResponse:
        # One serializer builds the whole structured contract (M0.4), including markdown.
        return BriefingResponse(**to_structured(briefing, cached=cached, cache_cycle=token))


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
