"""HREF source discovery and URL construction (Spike C — same-day high-res supplement).

The High-Resolution Ensemble Forecast (HREF) is NCEP's ~3 km convection-allowing
ensemble. UpstreamWX uses it to sharpen the **same-day (≲36 h)** flash-flood and
lightning signal that SREF (~16 km) carries at coarser scale; SREF still owns the
longer planning horizon (to 87 h). See PRD §6.2 FR-7a, §16.1/§16.2.

Findings from M0.0 source discovery (2026-06-18), recorded so the logic is grounded:

* Authoritative public source is **NOMADS**, same host and ``ensprod`` pattern as
  SREF (no AWS/GCP NODD mirror is used here)::

      https://nomads.ncep.noaa.gov/pub/data/nccf/com/href/prod/
          href.YYYYMMDD/ensprod/href.tHHz.<domain>.<product>.fHH.grib2(.idx)

* HREF runs **2 cycles/day at 00/12 UTC** (vs SREF's four), reflecting the higher
  cost of convection-allowing members. Member tag in the idx is ``0/10`` (11 members).
* **One file per forecast hour** (``fHH``), unlike SREF's single multi-window file —
  so covering a mission window means iterating idx+range over several hourly files.
* Forecast horizon on the CONUS domain: **f01–f48**, 1-hourly (essential for
  convective timing). UpstreamWX caps its use at ~36 h per product intent.
* The ``ensprod`` ``prob`` product is a **Neighborhood Ensemble Probability (NEP)**
  field — the correct way to read 3 km probabilities (a raw grid-point probability
  is near-zero even for a well-forecast storm). It carries exactly what the two
  HREF-relevant hazards need:
    - ``APCP``  — neighborhood P(1h/3h/6h/run-total precip > 0.5/1/2/3/5 in) → flash flood.
    - ``REFC``  — neighborhood P(composite reflectivity > 10..50 dBZ) → convection proxy.
    - ``LTNG``  — neighborhood P(lightning) → an explicit lightning signal.
    - ``CAPE``  — instability probability bands (modulates confidence/severity).
  Companion products ``sprd`` (spread) and ``mean`` support the FR-17 confidence cue.
* **Cold-start caveat:** the first ~3-6 h of HREF have reduced skill (spin-up); the
  0-6 h window is better served by the HRRR-derived Open-Meteo layer. UpstreamWX
  leans on HREF in the ~6-36 h band.
* Retention on NOMADS is short (~2 days of cycles); a production deployment must
  pull each cycle promptly on its schedule, fetching only the forecast hours the
  active mission window needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import requests

from ..grib.idx import _HEADERS

NOMADS_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/href/prod"
HREF_CYCLES = (0, 12)  # UTC hours (00Z/12Z)
DEFAULT_DOMAIN = "conus"  # ~3 km Lambert CONUS grid (separate AK/HI/PR domains exist)
DEFAULT_PRODUCT = "prob"  # ensprod Neighborhood Ensemble Probability
MAX_FHOUR = 48  # CONUS horizon; UpstreamWX uses ≲36 h per product intent
PRODUCTS = ("prob", "mean", "pmmn", "avrg", "lpmm", "eas", "sprd")


@dataclass(frozen=True)
class HrefCycle:
    """Identifies one HREF model cycle."""

    date: str  # YYYYMMDD (UTC)
    hour: int  # one of HREF_CYCLES

    @property
    def hh(self) -> str:
        return f"{self.hour:02d}"

    @property
    def init_time(self) -> datetime:
        return datetime.strptime(f"{self.date}{self.hh}", "%Y%m%d%H").replace(
            tzinfo=UTC
        )

    def ensprod_dir(self) -> str:
        return f"{NOMADS_BASE}/href.{self.date}/ensprod"

    def product_url(
        self,
        fhour: int,
        product: str = DEFAULT_PRODUCT,
        domain: str = DEFAULT_DOMAIN,
    ) -> str:
        """URL of an ensprod GRIB2 product file for one forecast hour."""
        fname = f"href.t{self.hh}z.{domain}.{product}.f{fhour:02d}.grib2"
        return f"{self.ensprod_dir()}/{fname}"

    def idx_url(self, fhour: int, **kw: str) -> str:
        return self.product_url(fhour, **kw) + ".idx"


def _exists(url: str, timeout: float | tuple[float, float] = (8.0, 15.0)) -> bool:
    """True if a ranged GET on ``url`` returns HTTP 200/206.

    A ``(connect, read)`` pair so probing back through several candidate cycles
    fails fast when NOMADS is unreachable instead of stacking full timeouts (NFR-6).
    """
    try:
        # NOMADS sometimes 405s on HEAD; a tiny ranged GET is reliable.
        resp = requests.get(
            url, headers={**_HEADERS, "Range": "bytes=0-0"}, timeout=timeout, stream=True
        )
        return resp.status_code in (200, 206)
    except requests.RequestException:
        return False


def iter_recent_cycles(now: datetime | None = None, count: int = 4):
    """Yield the most recent HREF cycles, newest first (UTC-aware)."""
    now = now or datetime.now(UTC)
    probe = now.replace(minute=0, second=0, microsecond=0)
    emitted = 0
    while emitted < count:
        if probe.hour in HREF_CYCLES:
            yield HrefCycle(date=probe.strftime("%Y%m%d"), hour=probe.hour)
            emitted += 1
        probe -= timedelta(hours=1)


def latest_available_cycle(
    now: datetime | None = None,
    max_back: int = 4,
    probe_fhour: int = 1,
    domain: str = DEFAULT_DOMAIN,
) -> HrefCycle | None:
    """Return the newest HREF cycle whose ``prob`` product is live on NOMADS.

    Accounts for production lag (a cycle's files appear well after its init time —
    HREF lags ~6-7 h) and NOMADS' short retention by probing recent cycles
    newest-first. Probes a low forecast hour as the cycle's readiness anchor.
    """
    for cycle in iter_recent_cycles(now=now, count=max_back):
        if _exists(cycle.product_url(probe_fhour, product="prob", domain=domain)):
            return cycle
    return None


def probe_sources(now: datetime | None = None, max_back: int = 4) -> dict:
    """Diagnostic probe used by the spike report: availability of recent cycles."""
    report: dict = {"nomads_base": NOMADS_BASE, "cycles": []}
    for cycle in iter_recent_cycles(now=now, count=max_back):
        url = cycle.product_url(1, product="prob")
        report["cycles"].append(
            {
                "cycle": f"{cycle.date}/{cycle.hh}Z",
                "prob_f01_url": url,
                "available": _exists(url),
            }
        )
    return report
