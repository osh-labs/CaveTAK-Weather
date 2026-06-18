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
- **Snap-first** always, and nudge onto the channel if `couldSnap == false`,
  before delineating — otherwise you get a degenerate single-cell basin.
- **Data currency** is uneven and only loosely datable: the practical vintage
  signal is the NLCD year embedded in the land-cover characteristic codes
  (UT: 1992/2011; NC: 1992/2001/2006/2011). SSURGO carries no date in the
  response. Surface the NLCD vintage in the briefing's karst/uncertainty notes.

## Recommendation

1. Adopt StreamStats (SS-Delineate + SS-Hydro) for **delineation and basin
   characteristics** on cache-on-miss — it works well and matches the pattern.
2. Compute CN **ourselves** (TR-55 lookup over NLCD × SSURGO); use StreamStats
   SSURGO where exposed and **a national SSURGO/NLCD fallback where not** (UT and
   other western states). This fallback is the real remaining work item.
3. Treat **travel time** as a separate build: NLDI flowtrace + upstream
   navigation + Jobson regression, not a StreamStats endpoint.
4. Validate CN/SSURGO exposure across the **full set of target states** before
   committing — the UT/NC split shows coverage cannot be assumed CONUS-wide.
