"""SREF source discovery and URL construction (Spike A).

Findings from M0.0 source discovery (2026-06-18), recorded so the logic is grounded:

* **No AWS/GCP open-data mirror exists** for SREF (the ``noaa-sref-pds`` bucket
  returns ``NoSuchBucket``). The authoritative public source is **NOMADS**.
* NOMADS production layout::

      https://nomads.ncep.noaa.gov/pub/data/nccf/com/sref/prod/
          sref.YYYYMMDD/HH/ensprod/sref.tHHz.<grid>.<product>_<freq>.grib2(.idx)

* SREF runs **4 cycles/day at 03/09/15/21 UTC** (27 members, idx tag ``0/26``).
* The ``ensprod`` directory holds pre-computed ensemble products, so we never sum
  raw members ourselves:
    - ``prob``   — probability fields (APCP at mm thresholds = P(precip); CAPE/PLI/CIN
                   probabilities = convective proxies).
    - ``spread`` — ensemble spread (basis for the FR-17 confidence qualifier).
    - ``mean``   — ensemble mean (QPF, CAPE, ...).
* Two grids: ``pgrb132`` (AWIPS 132, ~16 km, 697x553 Lambert — preferred) and
  ``pgrb212`` (~40 km).
* **Full product files are large** (prob ~660 MB, spread/mean ~370 MB), so the
  ``.idx`` sidecar + HTTP Range subsetting (see :mod:`upstreamwx.sref.fetch`)
  is mandatory, not an optimization.
* Retention on NOMADS is short (~2 days of cycles); a production deployment must
  pull each cycle promptly on its schedule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import requests

NOMADS_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/sref/prod"
SREF_CYCLES = (3, 9, 15, 21)  # UTC hours
DEFAULT_GRID = "pgrb132"  # ~16 km Lambert, preferred over pgrb212 (~40 km)
DEFAULT_FREQ = "3hrly"
PRODUCTS = ("prob", "spread", "mean", "max", "min", "p10", "p25", "p50", "p75", "p90")

# Polite identifier; NOMADS rejects some default agents.
USER_AGENT = "UpstreamWX/0.0 (M0.0 SREF spike; +https://upstreamwx.com)"
_HEADERS = {"User-Agent": USER_AGENT}


@dataclass(frozen=True)
class SrefCycle:
    """Identifies one SREF model cycle."""

    date: str  # YYYYMMDD (UTC)
    hour: int  # one of SREF_CYCLES

    @property
    def hh(self) -> str:
        return f"{self.hour:02d}"

    @property
    def init_time(self) -> datetime:
        return datetime.strptime(f"{self.date}{self.hh}", "%Y%m%d%H").replace(
            tzinfo=UTC
        )

    def ensprod_dir(self) -> str:
        return f"{NOMADS_BASE}/sref.{self.date}/{self.hh}/ensprod"

    def product_url(
        self,
        product: str = "prob",
        grid: str = DEFAULT_GRID,
        freq: str = DEFAULT_FREQ,
    ) -> str:
        """URL of an ensprod GRIB2 product file."""
        fname = f"sref.t{self.hh}z.{grid}.{product}_{freq}.grib2"
        return f"{self.ensprod_dir()}/{fname}"

    def idx_url(self, **kw: str) -> str:
        return self.product_url(**kw) + ".idx"


def _exists(url: str, timeout: float = 30.0) -> bool:
    """True if a HEAD/GET on ``url`` returns HTTP 200."""
    try:
        # NOMADS sometimes 405s on HEAD; a tiny ranged GET is reliable.
        resp = requests.get(
            url, headers={**_HEADERS, "Range": "bytes=0-0"}, timeout=timeout, stream=True
        )
        return resp.status_code in (200, 206)
    except requests.RequestException:
        return False


def iter_recent_cycles(now: datetime | None = None, count: int = 8):
    """Yield the most recent SREF cycles, newest first (UTC-aware)."""
    now = now or datetime.now(UTC)
    cur = now.replace(minute=0, second=0, microsecond=0)
    emitted = 0
    # Walk back hour by hour, yielding cycle hours as we pass them.
    probe = cur
    while emitted < count:
        if probe.hour in SREF_CYCLES:
            yield SrefCycle(date=probe.strftime("%Y%m%d"), hour=probe.hour)
            emitted += 1
        probe -= timedelta(hours=1)


def latest_available_cycle(
    now: datetime | None = None,
    max_back: int = 8,
    grid: str = DEFAULT_GRID,
) -> SrefCycle | None:
    """Return the newest SREF cycle whose ``prob`` product is live on NOMADS.

    Accounts for production lag (a cycle's files appear well after its init time)
    and NOMADS' short retention by probing recent cycles newest-first.
    """
    for cycle in iter_recent_cycles(now=now, count=max_back):
        if _exists(cycle.product_url(product="prob", grid=grid)):
            return cycle
    return None


def probe_sources(now: datetime | None = None, max_back: int = 8) -> dict:
    """Diagnostic probe used by the spike report.

    Returns availability of NOMADS cycles and confirms the AWS mirror is absent.
    """
    report: dict = {"nomads_base": NOMADS_BASE, "cycles": [], "aws_mirror": None}

    # AWS Open Data mirror check (documented as absent in M0.0).
    try:
        r = requests.get(
            "https://noaa-sref-pds.s3.amazonaws.com/?list-type=2&max-keys=1",
            headers=_HEADERS,
            timeout=20,
        )
        report["aws_mirror"] = {
            "url": "https://noaa-sref-pds.s3.amazonaws.com",
            "status": r.status_code,
            "exists": "NoSuchBucket" not in r.text and r.status_code == 200,
        }
    except requests.RequestException as exc:  # pragma: no cover - network
        report["aws_mirror"] = {"error": str(exc)}

    for cycle in iter_recent_cycles(now=now, count=max_back):
        url = cycle.product_url(product="prob")
        report["cycles"].append(
            {
                "cycle": f"{cycle.date}/{cycle.hh}Z",
                "prob_url": url,
                "available": _exists(url),
            }
        )
    return report
