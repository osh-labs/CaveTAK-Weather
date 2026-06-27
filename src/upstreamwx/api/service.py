"""Briefing service: cache-aware generation + the active-mission registry (M0.3).

This is the seam between the HTTP layer and the M0.2 generation core. It:

- serves a cached briefing when one is valid for the current cycle, else generates and
  caches (PRD §7, §11) — so reopening the app is free;
- registers in-range live missions so the scheduler can regenerate them on the SREF/AFD
  cycle (FR-12, :mod:`upstreamwx.api.scheduler`);
- always returns a briefing, marking unavailable sources rather than failing (NFR-6).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from ..config import get_settings
from ..engine.models import Mission
from ..sitrep.generate import GeneratedBriefing, generate_briefing
from ..sitrep.structured import to_structured
from ..sref import latest_available_cycle, prune_old_cycles, warm_cycle
from .cache import STATIC_TOKEN, BriefingCache, mission_cache_key
from .cycles import cycle_key
from .models import BriefingResponse, MissionSpec


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

        briefing = generate_briefing(mission, inputs=inputs, frame=spec.frame, generated_at=now)
        self.cache.put(key, briefing, token)
        # Track live, still-in-range missions for the scheduler (FR-12). Deterministic
        # offline briefings need no refresh, so they are not registered.
        if inputs is None and now < _as_utc(mission.window_end):
            self._active[key] = _Registered(mission=mission, frame=spec.frame, key=key)
        return self._response(briefing, token, cached=False)

    # -- scheduled refresh ----------------------------------------------------------
    def warm_and_prune(self, *, now: datetime | None = None) -> int:
        """Pre-pull the live SREF cycle into the persistent cache and prune old cycles.

        Run by the scheduler each cycle boundary before :meth:`refresh_active`, so the
        cycle's CONUS subset is downloaded once and every domain aggregates from the cached
        grid (roadmap §M0.1.1, FR-7, FR-12). Returns the number of fields warmed; 0 if no
        cycle is live yet on NOMADS (production lag), which is non-fatal — refresh still runs
        from whatever is cached (NFR-6).
        """
        cycle = latest_available_cycle(now=now)
        if cycle is None:
            return 0
        settings = get_settings()
        warmed = warm_cycle(cycle, settings=settings)
        prune_old_cycles(settings=settings, keep=settings.sref_cache_keep_cycles)
        return len(warmed)

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
            briefing = generate_briefing(reg.mission, frame=reg.frame, generated_at=now)
            self.cache.put(key, briefing, token)
            regenerated += 1
        return regenerated

    @property
    def active_count(self) -> int:
        return len(self._active)

    # -- helpers --------------------------------------------------------------------
    def _response(
        self, briefing: GeneratedBriefing, token: str, *, cached: bool
    ) -> BriefingResponse:
        # One serializer builds the whole structured contract (M0.4); the service only
        # supplies the cache provenance and the Markdown artifact.
        structured = to_structured(briefing, cached=cached, cache_cycle=token)
        return BriefingResponse(markdown=briefing.markdown, **structured)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
