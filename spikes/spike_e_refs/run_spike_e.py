"""Spike E CLI — REFS (RRFS Ensemble) neighborhood probabilities over a polygon.

REFS is NCEP's convection-allowing **replacement for HREF** (both HREF and SREF are
terminated 2026-08-31 12Z, NWS SCN 26-47). It is a 3 km, ~15-member ensemble running
00/06/12/18Z out to f60, and — like HREF — ships pre-computed Neighborhood Ensemble
Probability (NEP) ``prob`` products. This spike de-risks REFS as a same-day flash-flood
and lightning source (PRD §16.1/§16.2, FR-7a), the analogue of Spike C (HREF):

  1. find the latest live REFS cycle on the AWS ``noaa-rrfs-pds`` open-data mirror,
  2. pull a handful of NEP fields at a chosen forecast hour via ``.idx`` byte-range
     subsetting,
  3. aggregate each over the upstream-watershed polygon (max + areal mean), and
  4. print a summary plus a resource profile.

Source discovery (2026-06-29, recorded so the logic is grounded):

* **No NOMADS ``com/`` path** carries REFS yet (no ``com/rrfs`` / ``com/refs``). The
  authoritative public real-time feed is the **AWS** bucket ``noaa-rrfs-pds`` under
  ``rrfs_a/refs.YYYYMMDD/HH/enspost/`` (a parallel copy lives under ``rrfs_public/``).
* File pattern: ``refs.t{HH}z.{product}.f{FH:02d}.{domain}.grib2(.idx)``. The NEP product
  is ``prob``; domains are ``conus``/``ak``/``hi``/``pr``. ``.idx`` sidecars are present,
  so the shared :mod:`upstreamwx.grib` byte-range machinery applies unchanged.
* Cycles **00/06/12/18Z**; members **0/14** (15). The ``prob`` descriptors mirror HREF
  exactly — e.g. ``APCP:surface:11-12 hour acc fcst:prob >12.7`` (1 h),
  ``9-12 hour acc`` (3 h), ``REFC ... prob >40``, ``LTNG ... prob >0.08`` — so the
  HREF ``accum_window`` convention ports verbatim.

Run::

    .venv/bin/python spikes/spike_e_refs/run_spike_e.py \
        --polygon tests/fixtures/buckskin_huc12.geojson --fhour 12
    .venv/bin/python spikes/spike_e_refs/run_spike_e.py --dump-idx --fhour 12
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import geopandas as gpd
import numpy as np
import requests
import xarray as xr
from shapely.ops import unary_union

from upstreamwx.grib import (
    aggregate_over_polygon,
    download_subset,
    fetch_idx,
    select_messages,
)

# AWS open-data mirror is the authoritative public real-time REFS source (no NOMADS
# com/ path exists yet). rrfs_a/ is the operational-parallel stream; rrfs_public/ mirrors it.
AWS_BASE = "https://noaa-rrfs-pds.s3.amazonaws.com/rrfs_a"
REFS_CYCLES = (0, 6, 12, 18)  # UTC hours
DEFAULT_DOMAIN = "conus"
DEFAULT_PRODUCT = "prob"  # Neighborhood Ensemble Probability
N_MEMBERS = 15  # idx tag 0/14

USER_AGENT = "UpstreamWX/0.0 (M0.0 REFS spike; +https://upstreamwx.com)"
_HEADERS = {"User-Agent": USER_AGENT}


@dataclass(frozen=True)
class RefsCycle:
    """Identifies one REFS model cycle on the AWS mirror."""

    date: str  # YYYYMMDD (UTC)
    hour: int  # one of REFS_CYCLES

    @property
    def hh(self) -> str:
        return f"{self.hour:02d}"

    def enspost_dir(self) -> str:
        return f"{AWS_BASE}/refs.{self.date}/{self.hh}/enspost"

    def product_url(
        self, fhour: int, product: str = DEFAULT_PRODUCT, domain: str = DEFAULT_DOMAIN
    ) -> str:
        fname = f"refs.t{self.hh}z.{product}.f{fhour:02d}.{domain}.grib2"
        return f"{self.enspost_dir()}/{fname}"

    def idx_url(self, fhour: int, **kw: str) -> str:
        return self.product_url(fhour, **kw) + ".idx"


def _exists(url: str, timeout: float | tuple[float, float] = (8.0, 15.0)) -> bool:
    """True if a tiny ranged GET on ``url`` returns 200/206 (HEAD is unreliable on S3/NOMADS)."""
    try:
        resp = requests.get(
            url, headers={**_HEADERS, "Range": "bytes=0-0"}, timeout=timeout, stream=True
        )
        return resp.status_code in (200, 206)
    except requests.RequestException:
        return False


def iter_recent_cycles(now: datetime | None = None, count: int = 10):
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
    fhour: int, now: datetime | None = None, max_back: int = 10
) -> RefsCycle | None:
    """Newest REFS cycle whose ``prob`` file for ``fhour`` is live (accounts for production lag)."""
    for cycle in iter_recent_cycles(now=now, count=max_back):
        if _exists(cycle.idx_url(fhour)):
            return cycle
    return None


def accum_window(fhour: int, hours: int = 1) -> str:
    """``.idx`` fcst-window substring for an ``hours``-long accumulation ending at ``fhour``.

    Identical convention to HREF (``upstreamwx.href.accum_window``): REFS labels the 1 h
    bucket at f12 ``"11-12 hour acc"`` and the 3 h bucket ``"9-12 hour acc"``.
    """
    start = max(fhour - hours, 0)
    return f"{start}-{fhour} hour acc"


def build_fields(fhour: int):
    """NEP fields mapped to Appendix B logic at one forecast hour.

    Flash flood (§16.1): neighborhood P(precip) at mm thresholds over 1 h and 3 h windows.
    Lightning/convection (§16.2): P(composite reflectivity), explicit P(lightning), CAPE,
    and updraft helicity as a severe-storm proxy.
    """
    return [
        (f"P(precip>12.7mm/1h ~0.5in) f{fhour:02d}", "APCP", ">12.7", accum_window(fhour, 1)),
        (f"P(precip>25.4mm/1h ~1in) f{fhour:02d}", "APCP", ">25.4", accum_window(fhour, 1)),
        (f"P(precip>25.4mm/3h) f{fhour:02d}", "APCP", ">25.4", accum_window(fhour, 3)),
        (f"P(reflectivity>40dBZ) f{fhour:02d}", "REFC", ">40", None),
        (f"P(lightning>0.08) f{fhour:02d}", "LTNG", ">0.08", None),
        (f"P(CAPE>1000 J/kg) f{fhour:02d}", "CAPE", ">1000", None),
        (f"P(updraft-helicity>75) f{fhour:02d}", "MXUPHL", ">75", None),
    ]


def _load_field(cycle: RefsCycle, fhour: int, var: str, prob: str, fcst: str | None):
    """Fetch (idx subset) and decode one NEP field over the native 3 km grid."""
    idx = fetch_idx(cycle.idx_url(fhour))
    selected = select_messages(idx, var=var, prob=prob, fcst=fcst)
    if not selected:
        raise LookupError(f"no REFS message f{fhour:02d} var={var!r} prob={prob!r} fcst={fcst!r}")
    out_dir = Path(tempfile.mkdtemp(prefix="refs_"))
    out_path = out_dir / f"{var}_{prob}_f{fhour:02d}.grib2".replace(">", "gt").replace(" ", "")
    download_subset(cycle.product_url(fhour), selected, out_path)
    ds = xr.open_dataset(out_path, engine="cfgrib", backend_kwargs={"indexpath": ""})
    names = sorted(ds.data_vars, key=lambda n: ds[n].size, reverse=True)
    if not names:
        raise ValueError("REFS subset has no data variables")
    return ds[names[0]], out_path, len(selected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Spike E: REFS NEP over a polygon")
    parser.add_argument("--polygon", default="tests/fixtures/buckskin_huc12.geojson")
    parser.add_argument("--fhour", type=int, default=12, help="Forecast hour (3-60).")
    parser.add_argument("--dump-idx", action="store_true", help="Print the prob .idx descriptors.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    cycle = latest_available_cycle(args.fhour)
    if cycle is None:
        print("ERROR: no REFS cycle available on the AWS mirror")
        return 1

    if args.dump_idx:
        idx = fetch_idx(cycle.idx_url(args.fhour))
        print(f"REFS {cycle.date}/{cycle.hh}Z prob f{args.fhour:02d} {DEFAULT_DOMAIN}: "
              f"{len(idx)} messages\n")
        for e in idx:
            print(f"  {e.var:8} | {e.level:42} | {e.fcst:22} | {e.prob}")
        return 0

    poly = unary_union(gpd.read_file(args.polygon).geometry.values)

    results = []
    total_bytes = 0
    for label, var, prob, fcst in build_fields(args.fhour):
        t = time.perf_counter()
        try:
            da, path, n_msg = _load_field(cycle, args.fhour, var, prob, fcst)
        except (LookupError, requests.RequestException, TimeoutError) as exc:
            results.append({"field": label, "error": str(exc)})
            continue
        size = os.path.getsize(path)
        total_bytes += size
        agg = aggregate_over_polygon(da, poly, field_name=label, threshold=prob)
        results.append({
            **agg.as_dict(),
            "field": label,
            "messages": n_msg,
            "subset_kb": round(size / 1024, 1),
            "conus_max": round(float(np.nanmax(da.values)), 1),
            "seconds": round(time.perf_counter() - t, 2),
        })

    profile = {
        "source": "AWS noaa-rrfs-pds",
        "cycle": f"{cycle.date}/{cycle.hh}Z",
        "fhour": args.fhour,
        "members": N_MEMBERS,
        "total_subset_kb": round(total_bytes / 1024, 1),
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
    }

    if args.json:
        print(json.dumps({"profile": profile, "results": results}, indent=2, default=str))
        return 0

    print(f"\nREFS cycle: {profile['cycle']}  f{args.fhour:02d}  ({N_MEMBERS} members; "
          f"polygon: {args.polygon})")
    print(f"{'field':38} {'msgs':>4} {'KB':>7} {'poly_max':>9} {'poly_mean':>9} "
          f"{'cells':>5} {'CONUS_max':>9}")
    for r in results:
        if "error" in r:
            print(f"{r['field']:38} ERROR: {r['error']}")
            continue
        flag = " *nearest-cell" if r.get("fallback_nearest_cell") else ""
        print(f"{r['field']:38} {r['messages']:>4} {r['subset_kb']:>7.0f} "
              f"{r['max']:>8.1f}% {r['mean']:>8.1f}% {r['n_cells']:>5} "
              f"{r['conus_max']:>8.1f}%{flag}")
    print(f"\nresource profile: total subset {profile['total_subset_kb']:.0f} KB, "
          f"peak RSS {profile['peak_rss_mb']:.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
