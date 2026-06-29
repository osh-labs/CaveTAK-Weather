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
It is **capacity-bounded** as an LRU (``maxsize``) so an always-on host cannot grow it
without limit — an evicted briefing simply regenerates on the next request (NFR-6).
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Generic, TypeVar

from ..engine.models import HazardInputs, Mission
from ..sitrep.generate import GeneratedBriefing

# Validity token for deterministic (explicit-inputs) briefings: never expires.
STATIC_TOKEN = "static"

# Default LRU capacity when a caller does not pass one (kept in step with the
# ``api_cache_max_entries`` setting so a bare ``BriefingCache()`` is still bounded).
DEFAULT_MAX_ENTRIES = 512

_V = TypeVar("_V")


class BoundedLRU(Generic[_V]):
    """Thread-safe string-keyed LRU with a hard entry cap.

    A minimal building block for the in-process caches: ``get`` refreshes recency,
    ``put`` evicts the least-recently-used entries past ``maxsize``. Bounding these
    maps stops the unbounded per-mission growth that would otherwise leak on an
    always-on host (M0.1.1).
    """

    def __init__(self, maxsize: int = DEFAULT_MAX_ENTRIES) -> None:
        self._maxsize = max(1, maxsize)
        self._store: OrderedDict[str, _V] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> _V | None:
        with self._lock:
            if key not in self._store:
                return None
            self._store.move_to_end(key)
            return self._store[key]

    def put(self, key: str, value: _V) -> None:
        with self._lock:
            self._store[key] = value
            self._store.move_to_end(key)
            while len(self._store) > self._maxsize:
                self._store.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


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
    """Thread-safe in-process briefing cache with cycle-scoped validity, LRU-bounded."""

    def __init__(self, maxsize: int = DEFAULT_MAX_ENTRIES) -> None:
        self._store: BoundedLRU[_Entry] = BoundedLRU(maxsize)

    def get(self, key: str, token: str) -> GeneratedBriefing | None:
        """Return the cached briefing iff present and valid for ``token`` (else None)."""
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.token == STATIC_TOKEN or entry.token == token:
            return entry.briefing
        return None

    def put(self, key: str, briefing: GeneratedBriefing, token: str) -> None:
        self._store.put(key, _Entry(briefing=briefing, token=token))

    def __len__(self) -> int:
        return len(self._store)

    def clear(self) -> None:
        self._store.clear()
