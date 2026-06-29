"""REFS source discovery and URL construction (HREF's post-EOL replacement).

The Rapid Ensemble Forecast System (REFS, the RRFS Ensemble) is NCEP's ~3 km
convection-allowing ensemble — the **replacement for HREF**, which (with SREF) is
terminated 2026-08-31 12Z (NWS SCN 26-47). UpstreamWX uses REFS to sharpen the
**same-day (~36 h)** flash-flood and lightning signal that GEFS carries at coarse
global scale; GEFS owns the longer planning horizon. See PRD §6.2 FR-7a, §16.1/§16.2.

**Source feeds (NWS SCN 26-48).** The REFS feed is selectable via ``refs_source`` (see
:data:`REFS_FEEDS` and :func:`refs_feed`); the directory/filename layout is identical across
feeds, only the base host and the ensemble-product subdir differ:

      <base>/refs.YYYYMMDD/CC/<subdir>/refs.tCCz.<product>.fFF.<domain>.grib2(.idx)

* **Production (authoritative, live 2026-08-31 12Z):**
  ``https://nomads.ncep.noaa.gov/pub/data/nccf/com/refs/prod`` + subdir ``ensprod``.
* **Pre-implementation parallel (since ~2026-06-09):** ``…/com/refs/para`` + ``ensprod``.
* **AWS RRFS *prototype* bucket (default pre-cutover, the only feed validated end-to-end in
  the build container):** ``https://noaa-rrfs-pds.s3.amazonaws.com/rrfs_a`` + subdir ``enspost``.

Other findings from Spike E (2026-06-29, docs/m0.0/spike-e-refs-report.md):

* REFS runs **4 cycles/day at 00/06/12/18 UTC** (vs HREF's two). The AWS prototype's idx member
  tag was ``0/14`` (15); operational membership (SCN 26-48: 5 RRFS + 2 HRRR, time-lagged) may
  differ — irrelevant here, the precomputed ``prob`` NEP is member-count-agnostic.
* **One file per forecast hour per domain** (``conus``/``ak``/``hi``/``pr``), like HREF,
  but on a **3-hourly cadence**: products at **f03-f48 every 3 h, then to f60 every 6 h**
  (vs HREF's hourly f01-f48). :data:`REFS_FHOURS` enumerates the available hours.
* The ``prob`` product is a **Neighborhood Ensemble Probability (NEP)** field,
  byte-compatible with HREF's descriptor grammar — the ``accum_window`` convention is shared.
  It carries exactly what the two REFS-relevant hazards need:
    - ``APCP``  — neighborhood P(1h/3h/6h/run-total precip > mm thresholds) → flash flood.
    - ``REFC``  — neighborhood P(composite reflectivity > 10..50 dBZ) → convection proxy.
    - ``LTNG``  — neighborhood P(lightning > 0.08) → an explicit lightning signal.
    - ``CAPE``  — instability probability bands; plus ``MXUPHL``/``HLCY`` severe proxies.
* Retention on AWS is long, but the scheduler still pulls each cycle promptly, fetching only
  the forecast hours the active mission window needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import requests

from ..config import Settings, get_settings
from ..grib.idx import _HEADERS

REFS_CYCLES = (0, 6, 12, 18)  # UTC hours
DEFAULT_DOMAIN = "conus"  # ~3 km Lambert CONUS grid (separate AK/HI/PR domains exist)
DEFAULT_PRODUCT = "prob"  # ensemble-product NEP (Neighborhood Ensemble Probability)
MAX_FHOUR = 60  # CONUS horizon; UpstreamWX leans on ≲36 h per product intent (FR-7a)

# Available forecast hours: f03-f48 every 3 h, then to f60 every 6 h (REFS cadence).
REFS_FHOURS: tuple[int, ...] = tuple(range(3, 49, 3)) + (54, 60)
PRODUCTS = ("prob", "mean", "pmmn", "lpmm", "sprd", "avrg", "eas", "ffri")

# REFS source feeds (NWS SCN 26-48): (base URL, ensemble-product subdir). The directory and
# filename layout below is identical across feeds — only the host/base and the subdir differ
# (the AWS prototype bucket uses ``enspost``; the NOMADS production paths use ``ensprod``).
REFS_FEEDS: dict[str, tuple[str, str]] = {
    "aws": ("https://noaa-rrfs-pds.s3.amazonaws.com/rrfs_a", "enspost"),
    "nomads_para": ("https://nomads.ncep.noaa.gov/pub/data/nccf/com/refs/para", "ensprod"),
    "nomads_prod": ("https://nomads.ncep.noaa.gov/pub/data/nccf/com/refs/prod", "ensprod"),
}


def refs_feed(settings: Settings | None = None) -> tuple[str, str]:
    """Resolve the active REFS ``(base_url, subdir)`` from settings.

    ``refs_source`` selects a profile in :data:`REFS_FEEDS`; ``refs_base_url`` / ``refs_subdir``
    raw overrides take precedence. See :mod:`upstreamwx.config` for the cutover note.
    """
    settings = settings or get_settings()
    base, subdir = REFS_FEEDS[settings.refs_source]
    return (settings.refs_base_url or base, settings.refs_subdir or subdir)


@dataclass(frozen=True)
class RefsCycle:
    """Identifies one REFS model cycle (feed-agnostic; URLs resolve the active feed)."""

    date: str  # YYYYMMDD (UTC)
    hour: int  # one of REFS_CYCLES

    @property
    def hh(self) -> str:
        return f"{self.hour:02d}"

    @property
    def init_time(self) -> datetime:
        return datetime.strptime(f"{self.date}{self.hh}", "%Y%m%d%H").replace(tzinfo=UTC)

    def ensprod_dir(self, base: str | None = None, subdir: str | None = None) -> str:
        """``{base}/refs.{date}/{cc}/{subdir}`` — the cycle's ensemble-product directory."""
        if base is None or subdir is None:
            fb, fs = refs_feed()
            base, subdir = base or fb, subdir or fs
        return f"{base}/refs.{self.date}/{self.hh}/{subdir}"

    def product_url(
        self,
        fhour: int,
        product: str = DEFAULT_PRODUCT,
        domain: str = DEFAULT_DOMAIN,
        base: str | None = None,
        subdir: str | None = None,
    ) -> str:
        """URL of an ensemble-product GRIB2 file for one forecast hour (SCN 26-48 scheme)."""
        fname = f"refs.t{self.hh}z.{product}.f{fhour:02d}.{domain}.grib2"
        return f"{self.ensprod_dir(base, subdir)}/{fname}"

    def idx_url(self, fhour: int, **kw: str) -> str:
        return self.product_url(fhour, **kw) + ".idx"


def _exists(url: str, timeout: float | tuple[float, float] = (8.0, 15.0)) -> bool:
    """True if a ranged GET on ``url`` returns HTTP 200/206.

    A ``(connect, read)`` pair so probing back through several candidate cycles fails fast
    when the mirror is unreachable instead of stacking full timeouts (NFR-6). A tiny ranged
    GET is more reliable than HEAD across both S3 and NOMADS.
    """
    try:
        resp = requests.get(
            url, headers={**_HEADERS, "Range": "bytes=0-0"}, timeout=timeout, stream=True
        )
        return resp.status_code in (200, 206)
    except requests.RequestException:
        return False


def iter_recent_cycles(now: datetime | None = None, count: int = 6):
    """Yield the most recent REFS cycles, newest first (UTC-aware)."""
    now = now or datetime.now(UTC)
    probe = now.replace(minute=0, second=0, microsecond=0)
    emitted = 0
    while emitted < count:
        if probe.hour in REFS_CYCLES:
            yield RefsCycle(date=probe.strftime("%Y%m%d"), hour=probe.hour)
            emitted += 1
        probe -= timedelta(hours=1)


def latest_available_cycle(
    now: datetime | None = None,
    max_back: int = 6,
    probe_fhour: int = 3,
    domain: str = DEFAULT_DOMAIN,
    *,
    settings: Settings | None = None,
) -> RefsCycle | None:
    """Return the newest REFS cycle whose ``prob`` product is live on the active feed.

    Accounts for production lag (a cycle's files appear well after its init time) by probing
    recent cycles newest-first. Probes a low forecast hour (f03, the first REFS output) as the
    cycle's readiness anchor. The feed (AWS / NOMADS) is resolved once via :func:`refs_feed`.
    """
    base, subdir = refs_feed(settings)
    for cycle in iter_recent_cycles(now=now, count=max_back):
        if _exists(
            cycle.product_url(probe_fhour, product="prob", domain=domain, base=base, subdir=subdir)
        ):
            return cycle
    return None


def probe_sources(now: datetime | None = None, max_back: int = 6) -> dict:
    """Diagnostic probe: availability of recent cycles on the active feed."""
    base, subdir = refs_feed()
    report: dict = {"base": base, "subdir": subdir, "cycles": []}
    for cycle in iter_recent_cycles(now=now, count=max_back):
        url = cycle.product_url(3, product="prob", base=base, subdir=subdir)
        report["cycles"].append(
            {
                "cycle": f"{cycle.date}/{cycle.hh}Z",
                "prob_f03_url": url,
                "available": _exists(url),
            }
        )
    return report
