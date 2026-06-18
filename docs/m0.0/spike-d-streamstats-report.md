# Spike D — USGS StreamStats API Probe (Effective-QPF de-risk)

**Date:** 2026-06-18
**Branch:** `claude/streamstats-api-probe-ttx2rj`
**Goal:** characterise the post-decommission (v2) StreamStats API surface and
prove whether it can supply the inputs for the **travel-time-bounded +
Curve-Number-weighted Effective QPF** architecture — replacing today's raw
geometric HUC-12 QPF averaging (`upstreamwx.watershed`).
**Code:** `spikes/spike_d_streamstats/run_spike_d.py`
**Live artifact:** `docs/m0.0/spike-d-streamstats.json` (full probe output, 2026-06-18)

> Scope note: this is exploration / de-risk, not a roadmap milestone. The current
> roadmap (M0.1) uses geometric upstream aggregation. Effective QPF is a proposed
> enhancement; this spike establishes its data feasibility before it is scheduled.

-----

## TL;DR verdict

**PARTIAL YES, with a hard regional-coverage caveat.**

StreamStats v2 cleanly delivers **watershed delineation + basin characteristics**
on a cache-on-miss call pattern, and the pieces of the Effective-QPF formula are
all reachable. **But the inputs are not uniform across states**, and that
unevenness lands exactly where the architecture is most sensitive:

- The **Curve-Number asymmetry the architecture rests on is real and large** —
  but only directly computable where the state exposes SSURGO soil groups.
  **NC exposes SSURGO; UT does not.** So we can derive CN for the *East* (the
  case we want to *suppress* risk on) but **not** for the *desert West* (the case
  we want to *keep* high) from StreamStats characteristics alone.
- StreamStats **does not return a composite CN** anywhere, and the Runoff
  Modeling Service **takes CN as an input, not an output** — it is just the SCS
  `Q(P, CN)` equation. CN must be computed by us from NLCD land cover × SSURGO
  hydrologic soil group.
- The hosted **Time-of-Travel (Jobson) executable endpoint is gated**; only its
  `apiconfig` resolves on production. Travel-time bounding is therefore not a
  turnkey StreamStats call today.

The cache-on-miss integration pattern is sound. The Effective-QPF *physics* is
validated. The blocker is **CONUS-wide CN/travel-time coverage**, which needs a
fallback (self-hosted SSURGO + NLCD, or a national CN grid) for states that
don't expose the characteristics.

**Follow-up result (delineation):** for the *geometry* itself, the stateless
**NLDI split-catchment** process matches SS-Delineate to ~1 % (Linville 114.3 vs
114.7 km²; Zion 747.4 vs 756.3 km²) while being ~7× faster and needing no state
code — so StreamStats is **not required for delineation**. Its only residual
value is the SSURGO/NLCD characteristics bundle, which is the regionally-uneven
part. See "Follow-up — NLDI split-catchment vs SS-Delineate" below.

-----

## API status confirmed (June 2026)

The legacy StreamStats Services were decommissioned 2026-01-30. The replacements
are **FastAPI services that publish OpenAPI specs** (`/openapi.json`, `/docs`):

| Service | Base | Spec title | Key endpoints used |
| --- | --- | --- | --- |
| Pourpoint (snap) | `/pourpoint/` | Pourpoint Services 1.2.0 | `GET /v1/snap/str900` |
| SS-Delineate | `/ss-delineate/` | Delineate Services 1.2.0 | `GET /v1/delineate/sshydro/{region}` |
| SS-Hydro | `/ss-hydro/` | SS-Hydro 1.3.0 | `POST /v1/basin-characteristics/calculate-using-ssdelineate/` |
| Runoff Modeling | `/runoffmodelingservices/` | (apiconfig) | `GET /tr55` (TR-55 / SCS) |
| Time of Travel | `/timeoftravelservices/` | (apiconfig: "Jobsons") | `GET/POST /Execute` — **gated** |
| NLDI flowtrace | `api.water.usgs.gov/nldi/pygeoapi` | OGC API Processes | `POST /processes/nldi-flowtrace/execution` |

The doc pages at `/ss-delineate/docs`, `/docs/runoffmodelingservices/`, and
`/tot-beta/` are SPAs (as the brief warned), but every service exposes a
machine-readable spec or `apiconfig` — no headless-render needed.

-----

## Task 1 — SS-Delineate

**Endpoint:** `GET /ss-delineate/v1/delineate/sshydro/{region}?lat=&lon=`
(`{region}` is the 2-letter state code; the `features/{region}` variant returns
the same geometry without the ss-hydro wrapper.)

**Returns** a nested `featurecollection` with two named features:
`globalwatershedpoint` (the snapped pour point, with `HUCID` = containing HU8)
and `globalwatershed` (the basin polygon — `Polygon` for Zion, `MultiPolygon`
for Linville). Geometry is GeoJSON in WGS84. Polygon properties: `Shape_Area`
(m²), `Shape_Leng`, `GlobalWshd`, `WarningMsg`.

**Two findings that correct the brief / cache schema:**

1. **Snapping is mandatory and frequently fails on raw activity coordinates.**
   The delineation internally calls `/pourpoint/v1/snap/str900`. **Zion's raw
   point `37.2794,-112.9481` returns `couldSnap: false`** and yields a degenerate
   **1,700 m²** "basin" with `WarningMsg: "Point not snappable… results may be
   inaccurate."` Linville's raw point snaps fine. The probe nudges onto the
   mapped channel (a 2-ring, ~200 m grid search) before delineating — the same
   thing the web UI does for a click. **Snapped Zion (`37.2774,-112.9461`) then
   delineates to a real 756 km² basin.** *The cache must store the snapped
   coordinate and a `snap_nudged` flag.*

2. **`workspace_id` comes back empty — the v2 stack is stateless.** There is no
   persistent workspace to re-query later (unlike legacy StreamStats). Re-query =
   re-POST `region + snapped lat/lon`. **The proposed `streamstats_workspace`
   cache field has no value to store; drop it or set null.**

**No CN, no soil group, no carbonate fraction is returned by SS-Delineate** —
it is purely geometry + delineation metadata. Characteristics come from SS-Hydro.

-----

## Task 2 — Runoff Modeling Service (TR-55)

**Endpoint:** `GET /runoffmodelingservices/tr55?precip=&crvnum=&pdur=` (discovered
via `/runoffmodelingservices/apiconfig`; `pdur` is a code such as `I24H2Y`,
`I6H2Y`, `I24H100Y`).

**This is the single biggest correction to the brief.** The service implements
the SCS runoff equation and **requires the curve number `crvnum` as an input**:

```
Q = (P − Iₐ)² / ((P − Iₐ) + S),   Iₐ = 0.2S,   S = 1000/CN − 10
```

It **does not delineate, does not read soil/land cover, and does not derive CN.**
It returns `{p, rcn, s, ia, q, …}` — i.e. it is exactly the `Q()` term in the
Effective-QPF formula and nothing more. We can (and should) compute this in three
lines locally; it need not be a runtime dependency.

Verified runoff at P = 2.0 in, `I24H2Y` (shows the CN leverage directly):

| CN | Q (in) | per inch of rain |
| ---: | ---: | --- |
| 95 | 1.483 | slickrock / desert |
| 85 | 0.795 | |
| 70 | 0.241 | |
| 55 | 0.015 | forested Appalachian (≈**1%** of CN-95 runoff) |

**Caveat:** when `P < Iₐ` (low CN, light rain) the service returns a small
**non-zero** `q` instead of clamping to 0 — the SCS equation is undefined there.
Our local implementation must guard `Q = 0 for P ≤ Iₐ`.

-----

## Task 3 — Time of Travel (Jobson)

**`apiconfig` confirms a REST service** (`/timeoftravelservices/apiconfig`):
resource **"Jobsons"** (Jobson 1996, *Prediction of Traveltime and Longitudinal
Dispersion*), with `GET /Execute` and `POST /Execute` taking
`initialmassconcentration` + `starttime`. **But the executable routes return 404
on production** (`/timeoftravelservices/` and `/Execute` both 404; the `tot-beta`
web app points at `test.streamstats.usgs.gov/timeoftravelservices/`, which is
503). Only `apiconfig` resolves. **So travel time is not a turnkey StreamStats
REST call today.**

The `tot-beta` SPA's own workflow (read from its JS bundle): it traces the
flowpath with the **NLDI `nldi-flowtrace` pygeoapi process**, pulls discharge
from **NWIS**, then posts reach inputs to the Jobson service. The accessible,
documented piece is the flowtrace:

**`POST api.water.usgs.gov/nldi/pygeoapi/processes/nldi-flowtrace/execution`**
(inputs `lat`, `lon`, `direction` ∈ {up, down, none}, old-style array body).
Verified for both points — returns `downstreamFlowline` / `raindropPath` /
`nhdFlowline` with `comid`, `reachcode`, `gnis_name`:

- Zion snapped → **North Fork Virgin River**, comid 10025834
- Linville → **Linville River**, comid 9751596

**Implication:** travel-time bounding will require us to assemble it — navigate
the NHD network (NLDI upstream-tributaries) and apply Jobson's regression from
reach length + slope + discharge — rather than calling one StreamStats endpoint.

-----

## Task 4 — SS-Hydro

SS-Hydro is the **basin-characteristics computation layer** that complements
SS-Delineate's geometry layer. The one-shot endpoint
`POST /v1/basin-characteristics/calculate-using-ssdelineate/?region=&lat=&lon=`
delineates *and* computes every characteristic the state publishes (needs an
explicit empty body — `Content-Length: 0` — or it returns **HTTP 411**).

Drainage area cross-checks the delineation exactly (Zion 292 mi² = 756 km²;
Linville 44.3 mi² = 115 km²).

**The characteristic set is region-dependent — this is the central finding:**

| | Zion / **UT** (21 chars) | Linville / **NC** (54 chars) |
| --- | --- | --- |
| Drainage area | ✅ DRNAREA | ✅ DRNAREA |
| Impervious % | ✅ LC11IMP | ✅ LC11IMP / LC06IMP |
| Forest %, slope, elev, precip | ✅ | ✅ |
| **SSURGO HSG A–D %** | ❌ **absent** | ✅ SSURGOA-D |
| Longest flow path (LFPLENGTH) | ❌ absent | ✅ 24.18 mi |
| Channel slope (CSL10_85fm) | ❌ absent | ✅ |
| Composite CN | ❌ (nowhere) | ❌ (nowhere) |
| Carbonate rock fraction | ❌ | ❌ |

UT exposes neither SSURGO soil groups (needed for CN) nor the flow-path/slope
metrics (needed for in-channel travel time). **The very state where we most need
a high CN and a large fast-travel domain returns the least.** Neither state
returns a `CARBROCK`-style carbonate fraction — that characteristic exists only
in karst-region state configs, so the karst caveat trigger cannot rely on it
universally (today's `upstreamwx` carbonate caveat already comes from elsewhere).

-----

## Task 5 — Side-by-side comparison (the primary deliverable)

Live values, 2026-06-18 (`docs/m0.0/spike-d-streamstats.json`):

| Metric | **Zion Narrows, UT** (desert SW) | **Linville Gorge, NC** (Appalachian) |
| --- | --- | --- |
| Raw → snapped point | 37.2794,-112.9481 → **37.2774,-112.9461** (nudged) | 35.9499,-81.9271 (snapped as-is) |
| Snapped flowline | North Fork Virgin River | Linville River |
| **Delineated basin area** | **756.3 km²** (292 mi²) | **114.7 km²** (44.3 mi²) |
| Mean basin slope | 32.1 % | 24.3 % |
| Mean annual precip | 20.5 in | 56.5 in |
| Impervious frac | 0.078 % | 1.25 % |
| **SSURGO HSG** | **not exposed** | A 24.4 % · B 70.7 % · C 0.3 % · D 3.7 % |
| **Composite CN** | **not derivable from SS** | **≈ 50.3** (forest-on-HSG-A/B proxy) |
| Carbonate rock frac | not returned | not returned |
| Longest flow path | not returned | 24.18 mi (38.9 km) |
| **6-h travel-time domain** | celerity ~3 m/s → reach 64.8 km; **fraction unbounded** (no LFP) | celerity ~1.2 m/s → reach 25.9 km vs 38.9 km path → **~67 % of basin** |
| Data vintage (NLCD) | NLCD 1992, 2011 | NLCD 1992, 2001, 2006, 2011 |

**Does StreamStats produce the East/West asymmetry the architecture requires?**

- **Curve Number: emphatically yes, where derivable.** Linville's soils are
  **95 % HSG A+B** (high-infiltration forested Appalachian), giving a composite
  CN ≈ 50. Feeding that through TR-55 at P = 2 in yields **Q ≈ 0.015 in of
  runoff** versus **Q ≈ 1.48 in at the CN ≈ 95** a slickrock desert basin would
  carry — a **~50× difference in effective runoff per inch of rain** from CN
  alone. This is precisely the suppression the East-Coast over-warning problem
  needs, and StreamStats supplies the ingredients (NLCD + SSURGO) directly for
  NC. The gap is that **UT returns no SSURGO**, so the *desert* CN — the value we
  want high — can't be computed from StreamStats and needs a fallback source.

- **Travel time: directionally yes, but not from a StreamStats call.** The
  characteristics that bound it (LFPLENGTH, channel slope) are present for NC and
  absent for UT, and the Jobson endpoint is gated. The first-order celerity
  estimate in the probe encodes the intended asymmetry (steep West travels
  farther per hour) but is a placeholder, not a StreamStats result.

> **Caveat on the travel-time numbers:** the `contributing_fraction` and
> `celerity_ms` figures are first-order placeholders (representative flood-wave
> celerities × window), **not** Jobson outputs. They exist to exercise the cache
> shape and flag the asymmetry, and must be replaced by a real NLDI-navigation +
> Jobson computation before any briefing uses them.

-----

## Follow-up — NLDI split-catchment vs SS-Delineate (delineation head-to-head)

Since SS-Delineate's *unique* contribution over our existing tooling is the
delineation geometry, we tested the obvious stateless alternative:
**`POST nldi/pygeoapi/processes/nldi-splitcatchment/execution`** (`lat`, `lon`,
`upstream=True`). It returns `catchment`, `splitCatchment`, and the upstream
`drainageBasin` polygon — the same NHDPlus backing StreamStats' own tracing uses.

| | NLDI split-catchment | SS-Delineate |
| --- | --- | --- |
| Linville drainage basin | **114.3 km²** | 114.7 km² (Δ 0.3 %) |
| Zion (snapped) drainage basin | **747.4 km²** | 756.3 km² (Δ 1.2 %) |
| State region code | **not required** | required |
| Statefulness | stateless | stateless (empty workspace_id) |
| Latency | **~0.9 s** | ~6–8 s |
| Bundled characteristics | **none** | area/slope/NLCD/SSURGO (region-dependent) |
| Snap behaviour | **same fragility** | same fragility |
| Non-snap signal | **silent** (drops `drainageBasin`) | explicit `WarningMsg` |

**The geometries match to ~1 %** — unsurprising, since both trace NHDPlus. Two
findings worth recording:

1. **`splitcatchment` alone is snap-fragile on a raw point — but the raindrop
   trace fixes it (see next section).** Calling `splitcatchment` directly on
   Zion's *raw* point returns only a 1.9 km² local catchment and a 0.01 km²
   `splitCatchment`, **no `drainageBasin`**. The fix is *not* a blind lat/lon
   nudge: run `flowtrace` first to raindrop-snap the point onto the network,
   then `splitcatchment` from there.
2. **NLDI fails more quietly than StreamStats.** StreamStats flags an unsnappable
   point with an explicit `WarningMsg`; NLDI just omits the `drainageBasin`
   feature. Treat "no `drainageBasin` in the response" as the failure signal.

**Conclusion:** for the *geometry alone*, SS-Delineate buys us essentially
nothing over NLDI split-catchment — NLDI is faster, needs no state code, and is
already a dependency (the Spike B fallback + the flowtrace above). StreamStats'
only real residual value is the **SSURGO/NLCD characteristics bundle for CN**,
which is exactly the regionally-uneven part (absent for UT). This sharpens the
recommendation: lean on NLDI for delineation, and source soil/land-cover for CN
from a CONUS-complete dataset rather than from SS-Delineate's per-state payload.

-----

## Follow-up — current WBD HUC-12 trace vs NLDI split-catchment

The project already ships a delineator: the deterministic WBD `tohuc` HUC-12
graph walk (Spike B, `upstreamwx.watershed.trace_upstream`). Run live on the two
test points against NLDI split-catchment:

| Metric | Current WBD HUC-12 trace | NLDI split-catchment |
| --- | --- | --- |
| Zion area | **926.6 km²** (origin `150100080109`, 9 HUC-12) | 747.4 km² |
| Linville area | **175.6 km²** (origin `030501010302`, 2 HUC-12) | 114.3 km² |
| Over-inclusion vs NLDI | **+24 % (Zion), +54 % (Linville)** | baseline (pour-point exact) |
| Spatial unit | whole HUC-12 polygons | exact pour-point split |
| Snapping | **none needed** (HUC containment) | raindrop trace (handles raw Zion) |
| Raw Zion point | ✅ 926 km² | ✅ 747 km² (via raindrop snap) |
| Determinism | deterministic (WBD snapshot) | NHDPlus, stateless |
| Latency | 2.6–8.3 s | ~0.9 s |

The methods answer different questions: the WBD trace returns *everything
draining into the HUC-12 that contains the point* (the whole containing unit
counts, including area below/lateral to the activity point), while NLDI splits
the catchment at the point. The WBD answer is therefore **24–54 % larger** than
the true contributing area.

**This is not cosmetic for Effective QPF.** An inflated contributing area
inflates aggregated upstream QPF — the same over-warning bias the redesign
exists to remove — so the coarse HUC-12 method works *against* the goal and
should not be the primary domain. Its genuine virtue is being
**deterministic and snap-free** (reproducible from a WBD snapshot, no pour-point
snap to get wrong), which keeps it useful as a coarse cross-check / fallback —
but not the right pour-point domain.

-----

## Follow-up — solving the snap problem with the raindrop trace (`nldi-flowtools`)

The earlier sections treated snap fragility as an inherent cost of pixel-level
delineation. It is not. The fix is the **raindrop trace** — the terrain-following
snap inside `flowtrace` (and packaged as the `nldi-flowtools` library, the same
USGS code behind these pygeoapi processes; `flowtrace(lon, lat, dir)` /
`splitcatchment(lon, lat, upstream, simplified)`, deps `rasterio`/`pyproj`/
`shapely`/`numpy`, fetching a 30 m NHDPlus V2 flow-direction grid + NLDI).

The crude lat/lon grid nudge used earlier in this spike is the wrong tool: it
moves the point blindly and can land on the wrong channel. The raindrop trace
instead follows the flow-direction grid **downhill until it hits a stream line**,
which is a hydrologically correct snap. Verified live:

| | str900 grid snap | raindrop trace (`flowtrace`) |
| --- | --- | --- |
| Zion **raw** `37.2794,-112.9481` | ❌ `couldSnap:false` | ✅ snaps 300 m downhill to **North Fork Virgin River** (comid 10025834) |

**Recommended two-step (no pre-snap, no state code, handles raw activity
points):**

1. `flowtrace(lon, lat, "none")` → take the end of the `raindropPath` as the
   on-network point (and the `nhdFlowline` comid/name it hit, for a sanity check).
2. `splitcatchment(snapped_lon, snapped_lat, upstream=True, …)` → `drainageBasin`.

Run from the **raw** points this yields Zion **747.2 km²** (snapped to North Fork
Virgin River, comid 10025834) and Linville **114.3 km²** (Linville River, comid
9751596) — matching the manually-nudged and SS-Delineate areas to ~1 %, with
**no nudge step**. The probe script now uses this two-step for its NLDI
cross-check (`nldi_raindrop_snap` → `split_catchment`), and records the snapped
point + comid in `nldi_raindrop_snap`.

Residual caveat: the raindrop trace snaps to the *nearest downhill* flowline, so
a point near a confluence could still pick a minor tributary; the returned comid
+ a non-degenerate `drainageBasin` area are the sanity checks. On both test
points it picked the correct main stem. Whether to vendor `nldi-flowtools`
(adds `rasterio`; removes the hosted rate limit / availability dependency) or
just chain the two **hosted** calls (no new deps; subject to the 4-concurrent
limit) is an implementation choice — both run the identical algorithm. For
cache-on-miss volume, chaining the hosted calls is the lighter default.

-----

## Corrected cache schema

The probe writes one record per point (`docs/m0.0/spike-d-streamstats.json`).
Changes from the brief's proposed schema, each traceable to a finding above:

| Proposed field | Verdict |
| --- | --- |
| `cache_key` (hash of HUC-12 + threshold) | keep, but key on **snapped lat/lon + region + threshold** (delineation is stateless, not HUC-keyed) |
| `basin_geojson` | ✅ from SS-Delineate `globalwatershed` |
| `basin_area_sqkm` | ✅ from `DRNAREA` (mi² × 2.59) — authoritative, matches geometry |
| `basin_characteristics` | ✅ SS-Hydro dict — **but contents vary by state** |
| `composite_cn` | ⚠️ **computed by us**, not returned; null where SSURGO absent (e.g. UT) |
| `impervious_frac` | ✅ `LC11IMP` |
| `carbonate_rock_frac` | ⚠️ usually **null** (region-specific characteristic) |
| `travel_time_domain` | ⚠️ **not a StreamStats call** — assemble from NLDI + Jobson |
| `streamstats_workspace` | ❌ **always empty** — stateless API; drop or null |
| `streamstats_vintage` | ✅ inferred from NLCD-year characteristic codes (no single field) |
| `computed_at` / `expires_at` | ✅ |
| **add** `snapped_lat/lon`, `snap_nudged` | **new — required** (raw activity points often don't snap) |
| **add** `flowline_comid` | **new — useful** for NLDI navigation / travel time |

-----

## Operational notes

- **Rate limit** 4 simultaneous requests; cache-on-miss makes this a non-issue.
  Probe latencies: snap ~0.3 s; delineate ~6–8 s; basin characteristics ~7–17 s;
  flowtrace ~7–33 s.
- **POST quirk:** SS-Hydro POSTs need an explicit empty body (`Content-Length: 0`)
  or return HTTP 411.
- **Snap with the raindrop trace, not a grid nudge.** Run `flowtrace` first to
  snap the raw point onto the network (terrain-following, hydrologically
  correct), then `splitcatchment` from the snapped point — otherwise a raw
  activity point can yield a degenerate single-cell basin. The str900 grid snap
  is only needed for the SS-Delineate path (it snaps internally).
- **Data currency** is uneven and only loosely datable: the practical vintage
  signal is the NLCD year embedded in the land-cover characteristic codes
  (UT: 1992/2011; NC: 1992/2001/2006/2011). SSURGO carries no date in the
  response. Surface the NLCD vintage in the briefing's karst/uncertainty notes.

## Recommendation

1. **Delineate with NLDI, not SS-Delineate**, using the **raindrop two-step**:
   `flowtrace` to snap the raw point onto the network, then `splitcatchment` for
   the `drainageBasin`. The head-to-head shows matching geometry (~1 %), but NLDI
   is faster (~0.9 s), needs no state code, is stateless, already a dependency,
   and the raindrop snap handles raw activity points (Zion 747 km² with no
   nudge). Use `nldi-flowtools` if we want to drop the hosted rate limit (adds
   `rasterio`); otherwise chain the two hosted calls. This
   upgrades pour-point accuracy over the current WBD HUC-12 trace, which
   over-includes the contributing area by **24–54 %** on the test points. Keep
   the WBD trace as the **snap-free deterministic fallback** for points that
   won't snap (it handled Zion's raw point where both pixel methods degenerated).
2. Compute CN **ourselves** (TR-55 lookup over NLCD × SSURGO). Because the
   SSURGO/NLCD bundle is SS-Delineate's only real residual value yet is
   regionally uneven (absent for UT), source soil/land cover from a
   **CONUS-complete dataset** (national gridded SSURGO/gSSURGO + NLCD) rather than
   per-state SS-Hydro payloads. This is the real remaining work item.
3. Treat **travel time** as a separate build: NLDI flowtrace + upstream
   navigation + Jobson regression, not a StreamStats endpoint (executable ToT is
   gated).
4. **Net:** StreamStats is not required for the Effective-QPF architecture.
   Keep SS-Hydro only as an opportunistic *characteristics* source where a state
   exposes SSURGO and a national join isn't yet wired; do not couple delineation
   to it.
