"""Spike D CLI: probe the USGS StreamStats v2 API surface for the
travel-time-bounded + Curve-Number-weighted Effective-QPF architecture.

This spike characterises the *cache-on-miss* StreamStats integration: when a
user drops a new pin we call StreamStats once, write the result to the local
cache, and every later briefing reads the cache. StreamStats is never in the
hot path.

It exercises, end to end, the services that replaced the legacy StreamStats
Services (decommissioned 2026-01-30):

  * pourpoint snap   GET  /pourpoint/v1/snap/str900
  * SS-Delineate     GET  /ss-delineate/v1/delineate/sshydro/{region}
  * SS-Hydro         POST /ss-hydro/v1/basin-characteristics/calculate-using-ssdelineate/
  * Runoff Modeling  GET  /runoffmodelingservices/tr55      (TR-55 / SCS Q(P,CN))
  * NLDI flowtrace   POST /nldi/pygeoapi/processes/nldi-flowtrace/execution

and emits a side-by-side comparison of two contrasting hydrologic regimes:

    Zion Narrows, UT  (desert SW slot canyon)  37.2794, -112.9481
    Linville Gorge, NC (Appalachian gorge)      35.9499,  -81.9271

Pure stdlib (urllib) on purpose: this is a probe, not production code, and it
must run without the project's heavy geo stack.

Example:
    .venv/bin/python spikes/spike_d_streamstats/run_spike_d.py \
        --out docs/m0.0/spike-d-streamstats.json
"""

from __future__ import annotations

import argparse
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

SS = "https://streamstats.usgs.gov"
NLDI = "https://api.water.usgs.gov/nldi/pygeoapi/processes"

SQMI_TO_SQKM = 2.589988

# Test points (raw, from the integration brief).
TEST_POINTS = [
    {"name": "Zion Narrows, UT", "region": "UT", "lat": 37.2794, "lon": -112.9481},
    {"name": "Linville Gorge, NC", "region": "NC", "lat": 35.9499, "lon": -81.9271},
]


# --------------------------------------------------------------------------- #
# HTTP helpers (retry with exponential backoff on transient failures).
# --------------------------------------------------------------------------- #
def _request(method: str, url: str, *, timeout: int = 180, retries: int = 4) -> bytes:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, method=method)
            req.add_header("Accept", "application/json")
            if method == "POST":
                # SS-Hydro POST endpoints take their args as query params and
                # reject a chunked/absent body with HTTP 411; an explicit empty
                # body sets Content-Length: 0.
                req.data = b""
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as exc:  # noqa: PERF203
            last = exc
            if attempt < retries - 1:
                time.sleep(2**attempt)
    raise RuntimeError(f"{method} {url} failed after {retries} tries: {last}")


def get_json(url: str, **kw) -> object:
    return json.loads(_request("GET", url, **kw))


def post_json(url: str, **kw) -> object:
    return json.loads(_request("POST", url, **kw))


def _q(params: dict) -> str:
    return urllib.parse.urlencode(params)


# --------------------------------------------------------------------------- #
# Service calls.
# --------------------------------------------------------------------------- #
def snap(region: str, lat: float, lon: float) -> dict:
    """Snap a pour point to the str900 stream grid (StreamStats requires this)."""
    url = f"{SS}/pourpoint/v1/snap/str900?{_q({'region': region, 'lat': lat, 'lon': lon})}"
    return get_json(url, timeout=60)


def find_snappable(region: str, lat: float, lon: float, *, step: float = 0.002, rings: int = 2):
    """Return a (lat, lon) that snaps, searching a small grid around the point.

    Activity points sit *in* a canyon; the precise coordinate frequently misses
    the coarse str900 stream raster (Zion's raw point does). We nudge onto the
    mapped channel — the same thing the StreamStats web UI's snap does for a
    click. Returns (lat, lon, snapped_output, nudged: bool) or raises.
    """
    first = snap(region, lat, lon)
    if first.get("couldSnap"):
        return lat, lon, first["output"]["coordinates"], False
    for r in range(1, rings + 1):
        for i in range(-r, r + 1):
            for j in range(-r, r + 1):
                if max(abs(i), abs(j)) != r:
                    continue
                t_lat, t_lon = round(lat + i * step, 5), round(lon + j * step, 5)
                res = snap(region, t_lat, t_lon)
                if res.get("couldSnap"):
                    return t_lat, t_lon, res["output"]["coordinates"], True
    raise RuntimeError(f"no snappable point within {rings} rings of {lat},{lon}")


def delineate(region: str, lat: float, lon: float) -> dict:
    """SS-Delineate (sshydro format): returns globalwatershed polygon + workspace_id."""
    url = f"{SS}/ss-delineate/v1/delineate/sshydro/{region}?{_q({'lat': lat, 'lon': lon})}"
    return get_json(url)


def basin_characteristics(region: str, lat: float, lon: float) -> list[dict]:
    """SS-Hydro: delineate + compute every available basin characteristic."""
    url = (
        f"{SS}/ss-hydro/v1/basin-characteristics/calculate-using-ssdelineate/"
        f"?{_q({'region': region, 'lat': lat, 'lon': lon})}"
    )
    return post_json(url, timeout=300)


def tr55_runoff(precip_in: float, cn: float, pdur: str = "I24H2Y") -> dict:
    """Runoff Modeling Service TR-55: SCS runoff Q (inches) from precip + CN.

    Note: the service does NOT derive CN; CN is a required *input*. It is the
    Q() in the Effective-QPF formula and nothing more.
    """
    query = _q({"precip": precip_in, "crvnum": cn, "pdur": pdur})
    return get_json(f"{SS}/runoffmodelingservices/tr55?{query}", timeout=60)


def flowtrace(lat: float, lon: float, direction: str = "down") -> dict:
    """NLDI flowtrace: snap to the NHD network, return the flowline/raindrop path."""
    body = {
        "inputs": [
            {"id": "lat", "value": str(lat), "type": "text/plain"},
            {"id": "lon", "value": str(lon), "type": "text/plain"},
            {"id": "direction", "value": direction, "type": "text/plain"},
        ]
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{NLDI}/nldi-flowtrace/execution", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def split_catchment(lat: float, lon: float, *, upstream: bool = True) -> dict:
    """NLDI split-catchment: stateless pour-point delineation (NHDPlus).

    The stateless alternative to SS-Delineate. With ``upstream=True`` the
    ``drainageBasin`` feature is the full upstream contributing area. A missing
    ``drainageBasin`` means the point did not snap (same fragility as
    SS-Delineate, but signalled by omission rather than a warning).
    """
    body = {
        "inputs": [
            {"id": "lat", "value": str(lat), "type": "text/plain"},
            {"id": "lon", "value": str(lon), "type": "text/plain"},
            {"id": "upstream", "value": str(upstream), "type": "text/plain"},
        ]
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{NLDI}/nldi-splitcatchment/execution", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read())


# --------------------------------------------------------------------------- #
# Derived quantities.
# --------------------------------------------------------------------------- #
def _ring_area_km2(ring: list) -> float:
    """Shoelace area (km²) of a lon/lat ring, with a cos(lat) longitude scale."""
    lat0 = sum(p[1] for p in ring) / len(ring)
    k = math.cos(math.radians(lat0))
    xs = [p[0] * 111.320 * k for p in ring]
    ys = [p[1] * 110.574 for p in ring]
    a = sum(xs[i] * ys[i + 1] - xs[i + 1] * ys[i] for i in range(len(ring) - 1))
    return abs(a) / 2


def _geom_area_km2(geom: dict) -> float | None:
    t = geom.get("type")
    if t == "Polygon":
        return _ring_area_km2(geom["coordinates"][0])
    if t == "MultiPolygon":
        return sum(_ring_area_km2(poly[0]) for poly in geom["coordinates"])
    return None


def nldi_raindrop_snap(lat: float, lon: float) -> tuple[float, float, int | None, str | None]:
    """Snap a raw point onto the NHD network via the flowtrace raindrop path.

    This is the principled fix for the snapping problem (replacing a blind
    lat/lon grid nudge): flowtrace follows the 30 m NHDPlus V2 flow-direction
    grid downhill until it hits a stream line. The end of the ``raindropPath``
    is the on-network point; ``nhdFlowline`` carries the comid/name it hit.
    Verified to snap Zion's raw point onto the North Fork Virgin River main stem
    where the str900 grid snap fails. Returns (lat, lon, comid, gnis_name).
    """
    fc = flowtrace(lat, lon, "none")
    feats = {f.get("id"): f for f in fc.get("features", [])}
    rp = feats.get("raindropPath")
    snapped_lon, snapped_lat = (
        rp["geometry"]["coordinates"][-1] if rp else (lon, lat)
    )
    line = (feats.get("nhdFlowline") or {}).get("properties", {})
    return snapped_lat, snapped_lon, line.get("comid"), line.get("gnis_name")


def nldi_basin_area_sqkm(lat: float, lon: float) -> tuple[float | None, dict]:
    """Upstream basin area (km²) via raindrop-snap → split-catchment from a RAW point.

    Two-step pour-point delineation that needs no pre-snapping and no state code:
    raindrop-snap the raw point onto the network, then split the catchment there.
    Returns (area_km2 or None, snap_metadata).
    """
    meta: dict = {}
    try:
        s_lat, s_lon, comid, gnis = nldi_raindrop_snap(lat, lon)
        meta = {"snapped_lat": round(s_lat, 6), "snapped_lon": round(s_lon, 6),
                "comid": comid, "flowline": gnis}
        fc = split_catchment(s_lat, s_lon, upstream=True)
        feat = next((f for f in fc.get("features", []) if f.get("id") == "drainageBasin"), None)
        if feat is None:
            return None, meta  # no drainageBasin == split failed at snapped point
        return round(_geom_area_km2(feat["geometry"]), 1), meta
    except Exception as exc:  # noqa: BLE001
        meta["error"] = f"{type(exc).__name__}: {exc}"
        return None, meta


def chars_to_dict(chars: list[dict]) -> dict[str, float]:
    return {c["code"]: c["value"] for c in chars if isinstance(c.get("value"), (int, float))}


# TR-55 (NRCS 1986) curve numbers for the dominant CONUS covers, by hydrologic
# soil group, used only to *estimate* a composite CN from StreamStats land-cover
# + SSURGO fractions where the region exposes them. Authoritative CN derivation
# is a self-hosted NLCD x SSURGO join; this is a defensible first-order proxy.
_CN_FOREST = {"A": 30, "B": 55, "C": 70, "D": 77}  # woods, good condition
_CN_IMPERV = 98


def composite_cn(c: dict[str, float]) -> tuple[float | None, str]:
    """Estimate an area-composite CN, or explain why it cannot be derived.

    Requires SSURGO hydrologic-soil-group fractions (SSURGOA..D), which
    StreamStats exposes per region — present for NC, ABSENT for UT. When absent
    we cannot derive CN from the returned characteristics alone.
    """
    hsg = {g: c.get(f"SSURGO{g}") for g in ("A", "B", "C", "D")}
    if any(v is None for v in hsg.values()):
        return None, "SSURGO HSG fractions not exposed for region; CN not derivable from SS chars"
    total = sum(hsg.values())
    if total <= 0:
        return None, "SSURGO fractions sum to zero"
    imp = c.get("LC11IMP", 0.0) / 100.0  # impervious fraction
    # Pervious area weighted as forest (dominant cover for both gorges) by HSG;
    # impervious area at CN 98. A full implementation would weight every NLCD
    # class, not just forest.
    cn_pervious = sum(_CN_FOREST[g] * (hsg[g] / total) for g in hsg)
    cn = (1 - imp) * cn_pervious + imp * _CN_IMPERV
    return round(cn, 1), "forest-by-HSG proxy from SSURGOA-D + LC11IMP (first-order)"


# Representative flood-wave celerities (m/s) for a first-order travel-time
# bound. The hosted Time-of-Travel (Jobson) executable endpoint is gated (see
# report); these stand in until it is wired, and deliberately encode the
# East/West asymmetry the architecture rests on rather than hiding it.
_CELERITY_MS = {"steep_west": 3.0, "gentle_east": 1.2}


def travel_time_estimate(c: dict[str, float], regime: str, window_h: float = 6.0) -> dict:
    """First-order in-channel travel distance reachable in `window_h` hours.

    distance = celerity * window. If the basin's longest flow path (LFPLENGTH,
    miles) is shorter than that distance the *entire* basin is within the
    window; otherwise only the downstream fraction contributes.
    """
    celerity = _CELERITY_MS[regime]
    reach_km = celerity * window_h * 3600 / 1000.0
    lfp_mi = c.get("LFPLENGTH")
    lfp_km = lfp_mi * 1.609344 if lfp_mi else None
    if lfp_km is None:
        frac = None
        note = "LFPLENGTH not returned for region; cannot bound fraction"
    elif reach_km >= lfp_km:
        frac = 1.0
        note = "full basin within window"
    else:
        frac = round(reach_km / lfp_km, 3)
        note = "headwaters beyond window excluded"
    return {
        "regime": regime,
        "celerity_ms": celerity,
        "window_h": window_h,
        "reach_km": round(reach_km, 1),
        "longest_flow_path_km": round(lfp_km, 1) if lfp_km else None,
        "contributing_fraction": frac,
        "note": note,
    }


@dataclass
class PointResult:
    name: str
    region: str
    raw_lat: float
    raw_lon: float
    snapped_lat: float
    snapped_lon: float
    nudged: bool
    workspace_id: str
    area_sqkm: float | None
    nldi_area_sqkm: float | None
    nldi_snap: dict
    chars: dict[str, float]
    composite_cn: float | None
    cn_method: str
    impervious_frac: float | None
    carbonate_rock_frac: float | None
    travel_time: dict
    flowline_comid: int | None
    flowline_name: str | None
    nlcd_vintages: list[str] = field(default_factory=list)


def probe_point(p: dict, regime: str) -> PointResult:
    print(f"\n=== {p['name']} ({p['region']}) {p['lat']},{p['lon']} ===")
    s_lat, s_lon, snapped_out, nudged = find_snappable(p["region"], p["lat"], p["lon"])
    print(f"  snap: nudged={nudged} -> query {s_lat},{s_lon}")

    delin = delineate(p["region"], s_lat, s_lon)
    ws = delin.get("bcrequest", {}).get("wsresp", {}).get("workspace_id", "")

    chars_list = basin_characteristics(p["region"], s_lat, s_lon)
    c = chars_to_dict(chars_list)
    area = c.get("DRNAREA")  # square miles (authoritative)
    area_sqkm = round(area * SQMI_TO_SQKM, 1) if area else None
    print(f"  basin: DRNAREA={area} mi^2 (~{area_sqkm} km^2), {len(chars_list)} characteristics")

    cn, cn_method = composite_cn(c)
    imp = c.get("LC11IMP")
    carb = c.get("CARBROCK")  # carbonate rock fraction — region-specific, usually absent
    tt = travel_time_estimate(c, regime)

    try:
        ft = flowtrace(s_lat, s_lon, "down")
        feat = next(f for f in ft["features"] if f["id"] == "downstreamFlowline")
        comid = feat["properties"].get("comid")
        fname = feat["properties"].get("gnis_name")
    except Exception as exc:  # noqa: BLE001
        comid, fname = None, None
        print(f"  flowtrace failed: {exc}")
    print(f"  CN={cn} ({cn_method}); impervious={imp}%; flowline={fname} comid={comid}")

    # Stateless delineation cross-check: NLDI raindrop-snap + split-catchment,
    # run from the *raw* point (no pre-snapping) to show it handles unsnappable
    # points the str900 grid rejects.
    nldi_area, nldi_meta = nldi_basin_area_sqkm(p["lat"], p["lon"])
    delta = (
        f"{abs(nldi_area - area_sqkm) / area_sqkm * 100:.1f}%"
        if (nldi_area and area_sqkm)
        else "n/a"
    )
    print(
        f"  NLDI raindrop+split basin: {nldi_area} km^2 (vs SS {area_sqkm}; Δ {delta}); "
        f"snapped to {nldi_meta.get('flowline')} comid={nldi_meta.get('comid')}"
    )

    vintages = sorted({_nlcd_year(code) for code in c if _nlcd_year(code)})
    return PointResult(
        name=p["name"], region=p["region"], raw_lat=p["lat"], raw_lon=p["lon"],
        snapped_lat=s_lat, snapped_lon=s_lon, nudged=nudged, workspace_id=ws,
        area_sqkm=area_sqkm, nldi_area_sqkm=nldi_area, nldi_snap=nldi_meta, chars=c,
        composite_cn=cn, cn_method=cn_method, impervious_frac=imp, carbonate_rock_frac=carb,
        travel_time=tt, flowline_comid=comid, flowline_name=fname, nlcd_vintages=vintages,
    )


def _nlcd_year(code: str) -> str | None:
    # LC11* / LU92* / LC06* / LC01* embed the source NLCD vintage.
    for prefix, year in (("LC11", "NLCD2011"), ("LC06", "NLCD2006"),
                         ("LC01", "NLCD2001"), ("LU92", "NLCD1992"), ("LC92", "NLCD1992")):
        if code.startswith(prefix):
            return year
    return None


def cache_record(r: PointResult, *, ttl_days: int = 365) -> dict:
    now = datetime.now(UTC)
    return {
        "cache_key": f"{r.region}:{r.snapped_lat}:{r.snapped_lon}:tt6h",
        "lat": r.raw_lat,
        "lon": r.raw_lon,
        "snapped_lat": r.snapped_lat,
        "snapped_lon": r.snapped_lon,
        "snap_nudged": r.nudged,
        "region": r.region,
        "basin_area_sqkm": r.area_sqkm,
        "nldi_splitcatchment_area_sqkm": r.nldi_area_sqkm,
        "nldi_raindrop_snap": r.nldi_snap,
        "basin_characteristics": r.chars,
        "composite_cn": r.composite_cn,
        "composite_cn_method": r.cn_method,
        "impervious_frac": r.impervious_frac,
        "carbonate_rock_frac": r.carbonate_rock_frac,
        "travel_time_domain": r.travel_time,
        "flowline_comid": r.flowline_comid,
        "flowline_name": r.flowline_name,
        # The v2 SS-Delineate/SS-Hydro stack is STATELESS: workspace_id comes
        # back empty. Re-query by re-POSTing region+snapped lat/lon, not by id.
        "streamstats_workspace": r.workspace_id or None,
        "streamstats_data_vintage": r.nlcd_vintages,
        "computed_at": now.isoformat(),
        "expires_at": (now + timedelta(days=ttl_days)).isoformat(),
    }


def run() -> dict:
    regimes = {"UT": "steep_west", "NC": "gentle_east"}
    results = [probe_point(p, regimes[p["region"]]) for p in TEST_POINTS]

    print("\n=== TR-55 runoff demo (P=2.0 in, I24H2Y) ===")
    runoff_demo = {}
    for cn in (95, 85, 70, 55):
        q = tr55_runoff(2.0, cn).get("q")
        runoff_demo[cn] = round(q, 4) if q is not None else None
        print(f"  CN={cn}: Q={runoff_demo[cn]} in")

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "test_points": [cache_record(r) for r in results],
        "tr55_runoff_demo_P2.0in": runoff_demo,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Probe USGS StreamStats v2 API (Spike D).")
    ap.add_argument("--out", help="write the full JSON result here")
    args = ap.parse_args()

    result = run()
    text = json.dumps(result, indent=2)
    if args.out:
        from pathlib import Path

        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
        print(f"\nwrote {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
