"""Spike F CLI — GEFS member-exceedance probabilities over a polygon.

GEFS is NWS's recommended **replacement for SREF** (terminated 2026-08-31 12Z, NWS
SCN 26-47). Unlike SREF/HREF/REFS, GEFS ships **per-member grids only** — there is no
pre-computed exceedance-probability ``ensprod``/NEP product — so this spike prototypes
the missing piece the production provider will need: compute ``P(field > threshold)``
ourselves as the **member-exceedance fraction** over the upstream-watershed polygon.
It also prototypes the **lightning proxy** (GEFS carries no thunderstorm-probability
field) as the member co-occurrence of instability (CAPE) and precip.

  1. find the latest live GEFS cycle on NOMADS,
  2. for APCP and CAPE, subset that field from each member via ``.idx`` byte-range,
     aggregate the member's max over the polygon, and reduce to an exceedance fraction,
  3. derive a CAPE x precip lightning proxy from the same per-member maxima, and
  4. print a summary plus a resource profile (the 31-member fetch cost is the key result).

Source discovery (2026-06-29, recorded so the logic is grounded):

* NOMADS production layout::

      https://nomads.ncep.noaa.gov/pub/data/nccf/com/gens/prod/
          gefs.YYYYMMDD/HH/atmos/pgrb2sp25/{member}.tHHz.pgrb2s.0p25.fFFF(.idx)

* Members: ``gec00`` (control) + ``gep01..gep30`` = **31**; plus ``geavg`` (mean) and
  ``gespr`` (spread). **No probability product.** Cycles **00/06/12/18Z**, to f384.
* Resolution sets: ``pgrb2sp25`` (0.25 deg "select", used here — best resolution, smallest
  files), ``pgrb2ap5``/``pgrb2bp5`` (0.5 deg). Member descriptors carry ``ENS=+N`` not a
  ``prob`` token, e.g. ``APCP:surface:18-24 hour acc fcst:ENS=+1`` (6 h bucket),
  ``CAPE:surface:24 hour fcst:ENS=+1``.
* Caveat to confirm: 0.25 deg (~25 km) cells vs a small HUC-12 basin -> nearest-cell fallback
  is expected (GEFS cannot resolve a headwater watershed; that coarseness is itself a finding).

Run::

    .venv/bin/python spikes/spike_f_gefs/run_spike_f.py \
        --polygon tests/fixtures/buckskin_huc12.geojson --fhour 24 --members 31
    .venv/bin/python spikes/spike_f_gefs/run_spike_f.py --members 5 --dump-idx
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

from upstreamwx.grib import aggregate_over_polygon, download_subset, fetch_idx, select_messages

NOMADS_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gens/prod"
GEFS_CYCLES = (0, 6, 12, 18)  # UTC hours
DEFAULT_SET = "0p25"  # pgrb2sp25 select subset; ~25 km, carries APCP + CAPE
ALL_MEMBERS = ["gec00"] + [f"gep{n:02d}" for n in range(1, 31)]  # 31

# Lightning-proxy thresholds (J/kg, mm) — instability AND precip co-occurring in a member.
CAPE_PROXY_JKG = 1000.0
PRECIP_PROXY_MM = 2.5

USER_AGENT = "UpstreamWX/0.0 (M0.0 GEFS spike; +https://upstreamwx.com)"
_HEADERS = {"User-Agent": USER_AGENT}

_SETS = {  # set -> (subdir, file infix)
    "0p25": ("pgrb2sp25", "pgrb2s.0p25"),
    "0p50": ("pgrb2ap5", "pgrb2a.0p50"),
}


@dataclass(frozen=True)
class GefsCycle:
    date: str  # YYYYMMDD (UTC)
    hour: int

    @property
    def hh(self) -> str:
        return f"{self.hour:02d}"

    def atmos_dir(self, res_set: str) -> str:
        subdir = _SETS[res_set][0]
        return f"{NOMADS_BASE}/gefs.{self.date}/{self.hh}/atmos/{subdir}"

    def member_url(self, member: str, fhour: int, res_set: str) -> str:
        infix = _SETS[res_set][1]
        return f"{self.atmos_dir(res_set)}/{member}.t{self.hh}z.{infix}.f{fhour:03d}"

    def idx_url(self, member: str, fhour: int, res_set: str) -> str:
        return self.member_url(member, fhour, res_set) + ".idx"


def _exists(url: str, timeout: float | tuple[float, float] = (8.0, 15.0)) -> bool:
    try:
        resp = requests.get(
            url, headers={**_HEADERS, "Range": "bytes=0-0"}, timeout=timeout, stream=True
        )
        return resp.status_code in (200, 206)
    except requests.RequestException:
        return False


def iter_recent_cycles(now: datetime | None = None, count: int = 10):
    now = now or datetime.now(UTC)
    probe = now.replace(minute=0, second=0, microsecond=0)
    emitted = 0
    while emitted < count:
        if probe.hour in GEFS_CYCLES:
            yield GefsCycle(date=probe.strftime("%Y%m%d"), hour=probe.hour)
            emitted += 1
        probe -= timedelta(hours=1)


def latest_available_cycle(
    fhour: int, res_set: str, now: datetime | None = None, max_back: int = 10
) -> GefsCycle | None:
    """Newest GEFS cycle whose control member for ``fhour`` is live (a full run is produced)."""
    for cycle in iter_recent_cycles(now=now, count=max_back):
        if _exists(cycle.idx_url("gec00", fhour, res_set)):
            return cycle
    return None


def acc_window(fhour: int, hours: int = 6) -> str:
    """GEFS APCP accumulation-window substring (0.25 deg APCP is 6 h bucketed; f24 -> "18-24")."""
    start = max(fhour - hours, 0)
    return f"{start}-{fhour} hour acc"


def _crop_and_normalize(da: xr.DataArray, poly) -> xr.DataArray:
    """Crop a global 0-360 GEFS grid to the polygon's neighborhood and shift lon to [-180,180).

    GEFS is a global regular lat/lon grid with longitude in 0-360 and descending latitude.
    The watershed polygon uses -180..180, so we (1) crop with 0-360 bounds (monotonic slice,
    cheap — avoids masking ~1M global points per member), then (2) reassign lon to -180..180
    so regionmask matches the polygon's frame.
    """
    minx, miny, maxx, maxy = poly.bounds
    m = 1.0  # degrees of margin
    lo, hi = (minx - m) % 360, (maxx + m) % 360
    da = da.sel(longitude=slice(lo, hi), latitude=slice(maxy + m, miny - m))
    da = da.assign_coords(longitude=(((da["longitude"] + 180) % 360) - 180))
    return da


def _member_poly_max(cycle, member, fhour, res_set, var, fcst, level, poly):
    """Subset one field from one member, aggregate its max over the polygon.

    Returns ``(poly_max, fallback_nearest_cell, n_bytes)``.
    """
    idx = fetch_idx(cycle.idx_url(member, fhour, res_set))
    selected = select_messages(idx, var=var, fcst=fcst, level=level)
    if not selected:
        raise LookupError(f"{member}: no message var={var!r} fcst={fcst!r} level={level!r}")
    out = Path(tempfile.mkdtemp(prefix="gefs_")) / f"{member}_{var}_f{fhour:03d}.grib2"
    download_subset(cycle.member_url(member, fhour, res_set), selected, out)
    ds = xr.open_dataset(out, engine="cfgrib", backend_kwargs={"indexpath": ""})
    names = sorted(ds.data_vars, key=lambda n: ds[n].size, reverse=True)
    da = _crop_and_normalize(ds[names[0]], poly)
    agg = aggregate_over_polygon(da, poly, field_name=var, threshold="max")
    size = os.path.getsize(out)
    return agg.max_value, agg.fallback_nearest_cell, size


def _exceedance(values: list[float], threshold: float) -> float:
    """Member-exceedance fraction as a percent — the probability SREF's ensprod used to ship."""
    if not values:
        return float("nan")
    return 100.0 * sum(v > threshold for v in values) / len(values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Spike F: GEFS member-exceedance over a polygon")
    parser.add_argument("--polygon", default="tests/fixtures/buckskin_huc12.geojson")
    parser.add_argument("--fhour", type=int, default=24)
    parser.add_argument("--members", type=int, default=31, help="How many members to fetch (1-31).")
    parser.add_argument("--set", dest="res_set", default=DEFAULT_SET, choices=tuple(_SETS))
    parser.add_argument("--dump-idx", action="store_true", help="Print a member's APCP/CAPE idx.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    cycle = latest_available_cycle(args.fhour, args.res_set)
    if cycle is None:
        print("ERROR: no complete GEFS cycle available on NOMADS")
        return 1

    if args.dump_idx:
        idx = fetch_idx(cycle.idx_url("gec00", args.fhour, args.res_set))
        print(f"GEFS {cycle.date}/{cycle.hh}Z gec00 {args.res_set} f{args.fhour:03d}: "
              f"{len(idx)} messages (APCP/CAPE shown)\n")
        for e in idx:
            if e.var in ("APCP", "CAPE"):
                print(f"  {e.var:6} | {e.level:26} | {e.fcst:22} | {e.prob}")
        return 0

    poly = unary_union(gpd.read_file(args.polygon).geometry.values)
    members = ALL_MEMBERS[: max(1, min(args.members, len(ALL_MEMBERS)))]
    apcp_fcst = acc_window(args.fhour, 6)

    # (var, level, fcst) per field we reduce across members.
    fields = {
        "APCP": ("surface", apcp_fcst),
        "CAPE": ("surface", f"{args.fhour} hour fcst"),
    }
    per_member: dict[str, list[float]] = {"APCP": [], "CAPE": []}
    nearest_any = {"APCP": False, "CAPE": False}
    total_bytes = 0
    errors: list[str] = []
    t0 = time.perf_counter()
    for member in members:
        for var, (level, fcst) in fields.items():
            try:
                vmax, nearest, size = _member_poly_max(
                    cycle, member, args.fhour, args.res_set, var, fcst, level, poly
                )
            except (LookupError, requests.RequestException, TimeoutError, ValueError) as exc:
                errors.append(f"{member}/{var}: {exc}")
                continue
            per_member[var].append(vmax)
            nearest_any[var] = nearest_any[var] or nearest
            total_bytes += size
    elapsed = time.perf_counter() - t0

    # Flash-flood exceedance probabilities + the CAPE x precip lightning proxy.
    apcp, cape = per_member["APCP"], per_member["CAPE"]
    paired = list(zip(apcp, cape, strict=False)) if len(apcp) == len(cape) else []
    proxy = (
        100.0 * sum(a > PRECIP_PROXY_MM and c > CAPE_PROXY_JKG for a, c in paired) / len(paired)
        if paired else float("nan")
    )
    probs = {
        "P(precip>12.7mm/6h)": _exceedance(apcp, 12.7),
        "P(precip>25.4mm/6h)": _exceedance(apcp, 25.4),
        "P(CAPE>1000 J/kg)": _exceedance(cape, CAPE_PROXY_JKG),
        "P(CAPE>2000 J/kg)": _exceedance(cape, 2000.0),
        f"lightning proxy P(CAPE>{CAPE_PROXY_JKG:.0f} & precip>{PRECIP_PROXY_MM})": proxy,
    }

    profile = {
        "source": "NOMADS gens",
        "cycle": f"{cycle.date}/{cycle.hh}Z",
        "fhour": args.fhour,
        "set": args.res_set,
        "members_fetched": len(apcp),
        "fetch_seconds": round(elapsed, 1),
        "per_member_ms": round(1000 * elapsed / max(1, 2 * len(members)), 0),
        "total_subset_kb": round(total_bytes / 1024, 1),
        "nearest_cell_fallback": nearest_any,
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        "errors": errors,
    }

    if args.json:
        print(json.dumps({"profile": profile, "probabilities": probs,
                          "apcp_member_max_mm": apcp, "cape_member_max_jkg": cape},
                         indent=2, default=str))
        return 0

    print(f"\nGEFS cycle: {profile['cycle']}  f{args.fhour:03d}  {args.res_set}  "
          f"({len(apcp)}/{len(members)} members; polygon: {args.polygon})")
    fb = "  *nearest-cell (polygon < grid cell)" if any(nearest_any.values()) else ""
    print(f"per-member poly-max over the watershed{fb}")
    print(f"  APCP/6h mm:  min {min(apcp, default=0):.1f}  "
          f"max {max(apcp, default=0):.1f}  mean {np.mean(apcp) if apcp else 0:.1f}")
    print(f"  CAPE  J/kg:  min {min(cape, default=0):.0f}  "
          f"max {max(cape, default=0):.0f}  mean {np.mean(cape) if cape else 0:.0f}")
    print("\nmember-exceedance probabilities (computed; GEFS ships no prob product):")
    for k, v in probs.items():
        print(f"  {k:52} {v:5.1f}%")
    print(f"\nresource profile: {len(apcp)} members x 2 fields in {profile['fetch_seconds']}s "
          f"(~{profile['per_member_ms']:.0f} ms/subset), "
          f"total {profile['total_subset_kb']:.0f} KB, "
          f"peak RSS {profile['peak_rss_mb']:.0f} MB")
    if errors:
        print(f"  {len(errors)} fetch error(s); first: {errors[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
