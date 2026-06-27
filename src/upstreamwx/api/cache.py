"""Server-side briefing cache, keyed by location/window (PRD §7, §11; FR-25).

Briefings are generated server-side and cached so reopening the app — or a scheduled
refresh hitting the same mission — costs nothing (roadmap §M0.3). A cache entry is
**valid for one SREF cycle** (:mod:`upstreamwx.api.cycles`): a request in a new cycle
misses and regenerates, which is exactly the cache-aligned refresh FR-12 wants. Requests
that pin an explicit ``HazardInputs`` are deterministic (no live data), so their entries
never expire — same inputs reproduce the same briefing record (FR-25, NFR-4).

This is an in-process store. Cross-restart persistence is deliberately deferred to
M0.1.1 (EC2): an ephemeral dev container cannot validate it, and the interface here
(``get``/``put`` keyed by a stable string) is what a persistent backend would implement.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass

from ..engine.models import HazardInputs, Mission
from ..sitrep.generate import GeneratedBriefing

# Validity token for deterministic (explicit-inputs) briefings: never expires.
STATIC_TOKEN = "static"


def mission_cache_key(mission: Mission, inputs: HazardInputs | None = None) -> str:
    """Stable key identifying a mission's briefing (location + window + activity).

    Coordinates are rounded to ~11 m so a reopened pin at the same spot hits. When an
    explicit feature vector is supplied it is folded in, so two different saved inputs at
    one location/window do not collide. The Radius of Concern and Lightning Area of Concern
    radii are folded in too: both change the aggregation domain (and therefore the postures),
    so two requests differing only by a radius must not collide on one cache entry.
    """
    parts = [
        mission.activity_type.value,
        f"{mission.lat:.4f}",
        f"{mission.lon:.4f}",
        mission.window_start.isoformat(),
        mission.window_end.isoformat(),
        mission.approach_end.isoformat() if mission.approach_end else "-",
        mission.egress_start.isoformat() if mission.egress_start else "-",
        "slot" if mission.is_slot else "open",
        f"roc={mission.radius_km}",
        f"laoc={mission.lightning_radius_km}",
    ]
    if inputs is not None:
        parts.append(repr(sorted(vars(inputs).items())))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


@dataclass
class _Entry:
    briefing: GeneratedBriefing
    token: str  # the cycle id (or STATIC_TOKEN) this entry is valid for


class BriefingCache:
    """Thread-safe in-process briefing cache with cycle-scoped validity."""

    def __init__(self) -> None:
        self._store: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def get(self, key: str, token: str) -> GeneratedBriefing | None:
        """Return the cached briefing iff present and valid for ``token`` (else None)."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if entry.token == STATIC_TOKEN or entry.token == token:
                return entry.briefing
            return None

    def put(self, key: str, briefing: GeneratedBriefing, token: str) -> None:
        with self._lock:
            self._store[key] = _Entry(briefing=briefing, token=token)

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
