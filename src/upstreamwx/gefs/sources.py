"""GEFS source discovery and URL construction (SREF's post-EOL replacement).

The Global Ensemble Forecast System (GEFS) is NWS's recommended **replacement for SREF**
(terminated 2026-08-31 12Z, NWS SCN 26-47). UpstreamWX uses it as the coarse, global ensemble
for the longer planning horizon and as the backstop beyond REFS range (REFS, ~3 km, is the
authoritative same-day convection-allowing source). See PRD §6.2 FR-7, §16.1/§16.2.

Findings from Spike F source discovery (2026-06-29, docs/m0.0/spike-f-gefs-report.md):

* NOMADS production layout::

      https://nomads.ncep.noaa.gov/pub/data/nccf/com/gens/prod/
          gefs.YYYYMMDD/HH/atmos/<subdir>/<member>.tHHz.<infix>.fFFF(.idx)

  (AWS ``noaa-gefs-pds`` mirrors it.)
* Members: ``gec00`` (control) + ``gep01..gep30`` = **31**; plus ``geavg``/``gespr``.
  **No pre-computed probability product** — exceedance is computed in-house across members
  (:mod:`upstreamwx.gefs.extract`), unlike SREF's ``ensprod`` ``prob`` grids.
* Resolution sets: ``pgrb2sp25`` (**0.25°** "select" — used; best resolution, smallest files),
  ``pgrb2ap5``/``pgrb2bp5`` (0.5°). Member descriptors carry ``ENS=+N`` (not a ``prob`` token).
* Cycles **00/06/12/18 UTC**, to f384. ``APCP`` is bucketed in 6 h, ``CAPE`` instantaneous.
* **Coarse-grid caveat:** 0.25° (~25 km) barely resolves a HUC-12 (≈2 cells); GEFS is a coarse
  backstop, never the primary basin-routed source — that is REFS's role.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import requests

from ..config import Settings, get_settings
from ..grib.idx import _HEADERS

# Operational GEFS endpoint (the long-standing production path; no SCN 26-48 change for GEFS).
NOMADS_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gens/prod"
GEFS_CYCLES = (0, 6, 12, 18)  # UTC hours
DEFAULT_SET = "0p25"  # pgrb2sp25 select subset; ~25 km, carries APCP + CAPE


def gefs_base(settings: Settings | None = None) -> str:
    """Resolve the GEFS base URL: the ``gefs_base_url`` override, else operational NOMADS."""
    return (settings or get_settings()).gefs_base_url or NOMADS_BASE
# Control + 30 perturbed members (31 total).
MEMBERS: tuple[str, ...] = ("gec00",) + tuple(f"gep{n:02d}" for n in range(1, 31))

# set -> (atmos subdir, filename infix)
SETS: dict[str, tuple[str, str]] = {
    "0p25": ("pgrb2sp25", "pgrb2s.0p25"),
    "0p50": ("pgrb2ap5", "pgrb2a.0p50"),
}


@dataclass(frozen=True)
class GefsCycle:
    """Identifies one GEFS model cycle."""

    date: str  # YYYYMMDD (UTC)
    hour: int  # one of GEFS_CYCLES

    @property
    def hh(self) -> str:
        return f"{self.hour:02d}"

    @property
    def init_time(self) -> datetime:
        return datetime.strptime(f"{self.date}{self.hh}", "%Y%m%d%H").replace(tzinfo=UTC)

    def atmos_dir(self, res_set: str = DEFAULT_SET, base: str | None = None) -> str:
        subdir = SETS[res_set][0]
        return f"{base or gefs_base()}/gefs.{self.date}/{self.hh}/atmos/{subdir}"

    def member_url(
        self, member: str, fhour: int, res_set: str = DEFAULT_SET, base: str | None = None
    ) -> str:
        infix = SETS[res_set][1]
        return f"{self.atmos_dir(res_set, base)}/{member}.t{self.hh}z.{infix}.f{fhour:03d}"

    def idx_url(
        self, member: str, fhour: int, res_set: str = DEFAULT_SET, base: str | None = None
    ) -> str:
        return self.member_url(member, fhour, res_set, base) + ".idx"


def _exists(url: str, timeout: float | tuple[float, float] = (8.0, 15.0)) -> bool:
    """True if a ranged GET on ``url`` returns HTTP 200/206 (HEAD is unreliable on NOMADS)."""
    try:
        resp = requests.get(
            url, headers={**_HEADERS, "Range": "bytes=0-0"}, timeout=timeout, stream=True
        )
        return resp.status_code in (200, 206)
    except requests.RequestException:
        return False


def iter_recent_cycles(now: datetime | None = None, count: int = 6):
    """Yield the most recent GEFS cycles, newest first (UTC-aware)."""
    now = now or datetime.now(UTC)
    probe = now.replace(minute=0, second=0, microsecond=0)
    emitted = 0
    while emitted < count:
        if probe.hour in GEFS_CYCLES:
            yield GefsCycle(date=probe.strftime("%Y%m%d"), hour=probe.hour)
            emitted += 1
        probe -= timedelta(hours=1)


def latest_available_cycle(
    now: datetime | None = None,
    max_back: int = 6,
    probe_fhour: int = 6,
    res_set: str = DEFAULT_SET,
) -> GefsCycle | None:
    """Return the newest GEFS cycle whose control member for ``probe_fhour`` is live.

    Probes the control (``gec00``) at a low forecast hour as the cycle's readiness anchor —
    members publish together, so the control's presence signals the run is up. Accounts for
    production lag by probing recent cycles newest-first.
    """
    for cycle in iter_recent_cycles(now=now, count=max_back):
        if _exists(cycle.idx_url("gec00", probe_fhour, res_set)):
            return cycle
    return None


def probe_sources(now: datetime | None = None, max_back: int = 6) -> dict:
    """Diagnostic probe: availability of recent cycles' control member on NOMADS."""
    report: dict = {"nomads_base": NOMADS_BASE, "cycles": []}
    for cycle in iter_recent_cycles(now=now, count=max_back):
        url = cycle.member_url("gec00", 6)
        report["cycles"].append(
            {
                "cycle": f"{cycle.date}/{cycle.hh}Z",
                "gec00_f006_url": url,
                "available": _exists(url),
            }
        )
    return report
