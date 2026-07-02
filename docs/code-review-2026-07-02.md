# UpstreamWX Pre-Launch Code Review — 2026-07-02

Full-repo review ahead of launch, with deep focus on the deterministic engine and the
end-to-end risk-assessment path (ingest → watershed → ensemble aggregation → engine →
SITREP/API/PWA). Every finding below was verified against the code; references are
`file:line` on branch `claude/code-review-launch-d0bf6c` (from `main` @ 4c1d86d).

**Bottom line:** the engine core is clean, well-factored, and true to its spec (the FR-19
max rule, cave gating, thermal weighting, determinism, and the thresholds-as-data
constraint are all correctly implemented and mostly well-tested). The serious problems
are concentrated in two places: (1) a systemic **fail-toward-"safe"** pattern — missing,
stale, or failed *inputs* are indistinguishable from genuinely benign conditions, so
outages and edge cases surface as `Minimal` postures at `Moderate` confidence; and
(2) the **public API surface** (PDF render, warm endpoint, scheduler) has exploitable
and availability-killing defects. Several individually-critical bugs (the dead slot
fallback, the silently truncated watershed, the stale-cycle serving) all feed that first
pattern. I'd treat the Critical and High sections as launch blockers.

---

## 1. Issues

### 1.1 Critical

#### C-1. The slot-canyon conservative fallback is dead code on every live briefing
`src/upstreamwx/ingest/base.py:73` · `src/upstreamwx/engine/hazards/flash_flood.py:123-132`

No provider ever populates `bundle.convective_rate_in_per_hr` — the only writes in the
codebase are the dataclass default and the `to_hazard_inputs()` pass-through (verified by
grep over `src/`). The engine's slot fallback ("forecast convective rate > 0.5 in/hr on a
slot mission forces at least High, intentionally conservative") therefore **can never
fire in production**. It works only in the validation corpus, where the field is set by
hand — including the historical replay that models the Antelope-Canyon-class event
(`tests/corpus/historical_replay.yaml:45`). A user briefing Buckskin Gulch with `--slot`
on a 0.9 in/hr convective day with GEFS P(precip) 15% and no active products gets
**Minimal**, where the corpus says the product must say High.

`cape_jkg` is likewise never populated (Open-Meteo requests `cape` in `_HOURLY` but never
reads it out; the GEFS provider fetches per-member CAPE for its proxy but never writes an
aggregate), so the lightning CAPE-context note is also dead live. `wind_mph` too
(cosmetic: permanent "Wind: n/a" in every live SITREP).

*Fix:* derive `convective_rate_in_per_hr` from Open-Meteo's hourly `precipitation` max
(already in the response) or REFS 1-h QPF, and `cape_jkg` from the already-requested
Open-Meteo `cape` column. Add an ingest-level end-to-end test that a slot mission can
actually trigger the fallback — that test would have caught this immediately.

#### C-2. `/v1/briefing/pdf`: script injection into server-side Chromium → local file read / SSRF, unauthenticated
`src/upstreamwx/api/app.py:194` · `frontend/pdf/briefing-pdf.html:537,551,604-609` · `src/upstreamwx/sitrep/pdf.py:113,150`

The endpoint renders **client-supplied JSON** (`bluf`, `phases`, `risk_inputs` are bare
`dict`s in `api/models.py:130-135`) in headless Chromium. The template escapes most
fields via `esc()`, but not all: `fmtWindow(h.window)` / `fmtWindow(p.window)` pass
non-digit content verbatim into `innerHTML`, and `risk_inputs` values are interpolated
assuming they're numbers. Chromium is launched with `--no-sandbox` and the page is loaded
from a `file://` URI, so injected script has same-origin `file://` access: a crafted
`window` string like `<img src=x onerror=fetch('file:///etc/passwd')…>` yields arbitrary
local file read and SSRF exfiltration from the prod host. The endpoint is public, has no
auth, no rate limit, no payload size bound, and no concurrency cap (the `_gen_sem`
semaphore guards only `/v1/briefing`) — each request also spins up a ~300 MB browser on
the ≤2 GB host, so it's a trivial OOM/DoS vector independent of the injection.

*Fix:* escape every interpolated field in the template; validate the incoming
`BriefingResponse` against typed sub-models instead of bare dicts; cap request size and
PDF concurrency; don't serve the template via `file://` (or hard-block `file://`
subresource fetches); rate-limit or gate the endpoint.

#### C-3. The refresh scheduler blocks the asyncio event loop for the entire warm + refresh pass
`src/upstreamwx/api/scheduler.py:74-79` · `src/upstreamwx/api/app.py:56` · `src/upstreamwx/api/service.py:132-177`

`service.warm_and_prune()` (downloads the full GEFS warm band — hundreds of subsets) and
`service.refresh_active()` (serial full live ingest per active mission) are synchronous
calls made directly inside the scheduler coroutine on the uvicorn event loop. At every
00/06/12/18Z boundary the loop is blocked for minutes (and growing — see H-8): no
`/v1/health`, no `/v1/briefing`, nginx 502/504s. The product goes dark four times a day,
exactly when it should be freshest. Related: the lifespan shutdown awaits the cancelled
task with no timeout (`app.py:79-82`), and cancellation can't interrupt a blocked sync
read, so shutdown can hang indefinitely.

*Fix:* `await asyncio.to_thread(...)` around both calls (or run the scheduler in a
dedicated thread), and `asyncio.wait_for` on the shutdown await.

#### C-4. Systemic: missing/failed/NaN inputs are indistinguishable from "no hazard"
Engine: `engine/hazards/flash_flood.py:34-35`, `heat.py:22-24`, `cold_wet.py:22-24`, `lightning.py:75-80` · Ingest: `ingest/base.py:63-64,136-162` · Zonal: `grib/zonal.py:159-164` · Confidence: `engine/confidence.py:27-28`

This is the review's core structural finding, with several concrete instances:

- Every evaluator maps `None` to the lowest tier. `BriefingResult`/`HazardPosture` have
  **no representation of input availability** — an assessed-low and an unknown are the
  same posture. Confidence defaults to **Moderate** when member support is missing, so a
  total ensemble outage renders "flash flood: Minimal, Moderate confidence".
- `sources_ok` dies at the bundle boundary: `to_hazard_inputs()` drops it, so a failed
  Open-Meteo leaves `measurable_precip=False` — indistinguishable from a dry forecast —
  which **gates the GEFS Elevated flood band off** (`flash_flood.py:40`). A degraded
  display feed silently lowers an ensemble-driven posture.
- NaN is never guarded anywhere (repo-wide grep): an all-NaN zonal aggregation returns
  `max=NaN`; NaN members count as "did not exceed" in the GEFS exceedance denominator;
  one NaN hour poisons the REFS window max; `nan >= high_min` is False so the engine
  yields Minimal with a driver reading "GEFS P(precip/thunder) nan%"; and
  `cross_ensemble_agreement` classifies NaN as "consistent" — the highest-trust label.
  Every NaN path points anti-conservative. (The test suite itself emits the all-NaN
  regionmask warning 3×, so this path is real.)
- A watershed delineation failure also silences the **lightning** ensemble
  (`ingest/orchestrator.py:153-171` gates the LAoC on the basin polygon), though the
  LAoC is just a disk around the point and needs no basin.

*Fix (one coherent change):* make availability first-class. Treat NaN as missing at the
aggregation layer (all-NaN → None + flag); carry per-input availability into
`HazardInputs`; when a hazard's primary driver is unavailable, force confidence **Low**
and emit an explicit "data unavailable" driver on the hazard card (not just a distant
orchestrator note). NFR-6 says "mark unavailable and continue" — today the marking never
reaches the user next to the number it affects.

#### C-5. Stale ensemble cycles are served as current, and the cache token lies about it
`src/upstreamwx/ingest/gefs_provider.py:158-165` · `src/upstreamwx/gefs/cache.py:242-275` · `src/upstreamwx/ingest/refs_selection.py:52-83` · `src/upstreamwx/api/cycles.py:28-37` · `src/upstreamwx/api/service.py:88,146-167`

Two compounding bugs:

- `_resolve_cycle` unconditionally prefers any non-empty on-disk cycle over a live
  probe, and `cached_cycles` has **no maximum age** (pruning only runs inside the API
  scheduler). If the scheduler is disabled, stalled, or dies — or in CLI use with a
  persistent `data_dir` — a days-old GEFS run silently masquerades as the current
  ensemble forever; the fhour math is self-consistent against the stale init, so nothing
  errors. REFS accepts runs up to ~45 h old as the *authoritative in-window* signal.
- The API's cache-validity token is pure wall-clock arithmetic (`current_cycle` floors
  now to the last 00/06/12/18Z boundary), ignoring publication lag. At 12:10Z the token
  says `T12Z` but `latest_available_cycle` still resolves 06Z — so a briefing built from
  06Z data is keyed, labeled, and cached as 12Z-fresh, and `refresh_active` (naive
  boundary) disagrees with `warm_and_prune` (lag-aware) about what "current" means.

Failure scenario: scheduler thread dies Friday night; Saturday-morning canyoneers get a
briefing advertising the current cycle whose flood probabilities predate the mesoscale
setup by 36 h. *Fix:* max-age on `cached_cycles` (e.g. 2× cadence) falling through to
the live probe; derive the cache token from `latest_available_cycle()`; stamp the actual
model cycle into structured provenance so the UI can show data age.

### 1.2 High

#### H-1. Upstream watershed trace can silently truncate the basin (understates flash flood)
`src/upstreamwx/watershed/upstream.py:107-174`

Three related defects in the WBD trace path, all failing toward a smaller basin with no
note: (a) the external-inflow check probes only headwater *leaves*, so a tributary
joining mid-region from an adjacent HU8 (Paria-into-Colorado topology) is never detected
and its entire watershed is dropped; (b) per-leaf probe failures are swallowed
(`except Exception: continue`) — a transient WFS error means "no inflow", accepting the
truncated basin; (c) there is no inflow check or note at the widest HU4 fetch, and
`tohuc` links legitimately cross HU4/HU2 boundaries. A storm over the dropped tributary
is invisible to the Effective-QPF aggregation. Since M0.1 these traces are also
**cached permanently** (see H-9). *Fix:* probe all boundary-adjacent nodes, fail probe
errors toward widening (or at least annotate), and emit a truncation-risk flag at the
HU4 ceiling that flows into flash-flood confidence.

#### H-2. GEFS member fan-out is all-or-nothing — and the readiness probe makes failure routine
`src/upstreamwx/ingest/gefs_provider.py:137-155,209-222` · `src/upstreamwx/gefs/sources.py:109-124`

The first `TimeoutError/RequestException/OSError` from any of the ~250–500 member×fhour
tasks discards the **entire ensemble** (no retry, no fallback to the previous complete
cycle in cache). `latest_available_cycle` declares a cycle ready from `gec00 f006`
alone, but GEFS publishes progressively over ~1.5 h — so a cold briefing requesting
f096–f120 mid-publish 404s and loses all of GEFS, rendering flash flood "Minimal / No
GEFS precip signal" during exactly the windows users refresh. Separately, when members
are partially decodable the exceedance denominator silently shrinks (1 of 2 members =
50%) with no quorum. *Fix:* catch the network-error family per member (mirroring the
existing `LookupError` handling), require a minimum member count, and fall back to the
previous complete cached cycle when the freshest is mid-publish.

#### H-3. Open-Meteo fetches 3 forecast days while the product supports 5-day missions
`src/upstreamwx/ingest/openmeteo.py:55,155-173` · `engine/hazards/flash_flood.py:40`

`_query` pins `forecast_days: 3`; nothing validates mission lead time. For a day-4/5
mission the window filter comes back empty, `heat_index_f`/`apparent_temp_f` stay None
(heat/cold read "no data" → C-4), and — the dangerous part — `measurable_precip` stays
`False` with `sources_ok["open_meteo"]=True` and **no note**. Because the GEFS Elevated
band requires `measurable_precip`, a day-4 mission with GEFS P(precip)=45% renders flash
flood **Minimal ("dry upstream")**. Partial window coverage (straddling day 3) is
equally silent, and "antecedent precip" quietly measures a period ending days before the
window. *Fix:* raise `forecast_days` to cover the supported horizon (16 is available),
record per-provider covered spans, and treat window-not-covered as unavailable, not dry.

#### H-4. NWS alerts are checked at the mission point only — not over the upstream basin the driver text claims
`src/upstreamwx/ingest/nws.py:85` · `engine/hazards/flash_flood.py:20-21`

The product's centerpiece is upstream aggregation, but the authoritative near-term
anchor (`/alerts/active?point=lat,lon`) covers only the trip point. A Flash Flood
Warning polygon over the upper basin — where the storm is — sets nothing, while the
driver copy says "over area or upstream domain". Also all-or-nothing within NWS: if the
AFD chain 500s after alerts succeeded, the already-fetched alert flags are discarded
(`nws.py:130-138` resolves both futures before writing). *Fix:* query alerts over the
basin polygon (the orchestrator has it; NWS supports area queries), or at minimum fix
the driver copy; write alert flags independently of AFD success.

#### H-5. The lightning AFD ceiling lowers postures based on window-blind text, with no override beyond REFS range
`src/upstreamwx/ingest/nws.py:32-53` · `src/upstreamwx/engine/hazards/lightning.py:85-102` · `data/thresholds/lightning.yaml` (afd_ceiling)

`_afd_storm_mode` regex-scans the **entire** AFD — long-term, aviation, fire sections —
with no mission-window awareness, and the result activates a posture-**lowering** cap.
For a mission at +40 h, REFS is out of range (`refs_p_lightning is None`) so the
override can never fire: an "isolated" in a section about a different day caps a GEFS
50% → High signal at **Elevated**. This is the one place a stale, mis-scoped text signal
can pull a live ensemble signal down — the inverse of the "signals only raise" rule.
*Fix:* parse AFD sections (`.SHORT TERM...`/`.LONG TERM...` headers are machine-splittable)
and apply the mode only when it covers the mission day; or apply the ceiling only when
REFS is in-range so the override is always live. (Also: the override comparison is
strict `>` while the config comment and the user-facing note both say `≥` —
`lightning.py:91`; REFS at exactly 60.0% doesn't override.)

#### H-6. Short same-day windows lose REFS entirely and are mislabeled "outside range"
`src/upstreamwx/ingest/refs_selection.py:104-119` · `src/upstreamwx/ingest/refs_provider.py:133-143`

REFS selection matches only instantaneous 3-hourly valid times with `vt <= end`, unlike
GEFS's correct accumulation-*bucket-overlap* logic. A 12:10–14:50Z window matches zero
sources → `refs_in_range=False` plus a note claiming the window is "outside the same-day
supplement range" — factually wrong. The authoritative 3 km convection-allowing signal is
silently replaced by coarse GEFS for exactly the product's core use case (short
same-day slot windows). The 1-h QPF NEP also samples only every third hour. *Fix:* select
by accumulation-window overlap (reuse the GEFS helper) and distinguish "out of lead-time
range" from "in range, no aligned output".

#### H-7. Offline review of the last briefing is broken on the production path
`frontend/sw.js:71,79` · `frontend/js/app.js`

The service worker bails on all non-GET requests, and briefings are `POST /v1/briefing`
— so the "briefing: network-first, cache fallback" branch is dead code in production
(Cache API can't store POSTs anyway), and nothing else persists the result. A caver who
loads a briefing at home and reopens the PWA at a no-signal trailhead gets a retry
banner, not the briefing — FR-26/FR-28's core offline promise only works in demo mode.
The documented offline-PDF fallback is similarly unwired: the reader side honors
`uwx.pdf.briefing` + `?print=1`, but nothing in `app.js` ever writes that key or opens
the template. *Fix:* persist the last successful briefing JSON (localStorage/IndexedDB)
and render it when the POST fails offline; wire the PDF handoff on export failure.

#### H-8. No auth or rate limiting on any endpoint; several unbounded resource sinks
`src/upstreamwx/api/app.py` · `src/upstreamwx/api/service.py:116-117,184-212`

Going live public: `/v1/briefing/frame` makes billable Anthropic calls with no throttle;
`/v1/watershed/warm` runs 3–15 s USGS delineations per distinct coordinate with an
unbounded `_warm_pending` set and unbounded executor queue (202 returned instantly — no
backpressure); `service._active` grows without bound (missions only drop when their
window *ends*), and `refresh_active` re-ingests every entry each cycle, compounding C-3.
nginx `limit_req` exists but the app itself assumes a trusted caller. *Fix:* bound
`_active` and the warm queue (reject when saturated), validate points are in CONUS, cap
window length/lead and both radii (`models.py:33,38` have `ge=1` but no `le`) in
`MissionSpec` — which also has no `end > start` check — and rate-limit frame/pdf.

#### H-9. Watershed cache: coarse keys, permanently pinned fallbacks, and poisonable entries
`src/upstreamwx/watershed/cache.py:29-55,191-222` · `src/upstreamwx/watershed/pourpoint.py:234-238`

Four related defects: (a) the pour-point cache key rounds to 3 decimals (~110 m) — two
pins on opposite sides of a drainage divide (realistic at canyon rims) share a key, and
the second user silently gets the first user's basin; the briefing cache rounds to 4
decimals, so the two layers disagree about which points are "the same". (b) A transient
NLDI outage permanently pins the coarse WBD fallback basin (24–54% over-inclusive per
Spike D, possibly under-inclusive via H-1) — `basin.method` records the fallback but the
read path never inspects it, and nothing ever retries the exact path. (c) A corrupt/empty
cache file (possible: `_atomic_write` renames without fsync) raises out of
`delineate_cached` on every subsequent briefing for that point — no self-heal. (d) A
cache-*write* failure (disk full) fails the briefing and all single-flight waiters even
though a valid basin exists in memory. *Fix:* key at 4 decimals; treat fallback entries
as soft (TTL + background upgrade); on read error delete the file and fall through to
live delineation; set the single-flight result before writing the cache.

#### H-10. REFS flood cut points have zero corpus coverage
`tests/corpus/flash_flood.yaml` vs `data/thresholds/flash_flood.yaml` (refs_probability)

No corpus case sets `refs_p_precip` at all — no test at 40/39/10/9, and the REFS
*raise* path (REFS lifting a lower GEFS/product tier) is asserted nowhere. The signal
CLAUDE.md calls "authoritative in-window" for the flood hazard is unpinned: flipping
`>=` to `>`, misreading a key, or deleting the branch passes the entire suite. (The
lightning REFS bands, by contrast, are textbook-pinned.) *Fix:* add boundary cases plus
REFS-raises-over-GEFS and REFS-raises-over-product cases. Cheapest high-value fix in
this review.

#### H-11. `heat_index_f` is actually Open-Meteo apparent temperature, evaluated against NWS Heat Index cut points
`src/upstreamwx/ingest/openmeteo.py:157-161` · `data/thresholds/heat.yaml`

The bundle field named `heat_index_f` is filled with the window max of
`apparent_temperature` — a different formulation (includes wind and solar radiation)
from the NWS Rothfusz heat index the FR-15 categories encode. Divergence of several °F
either direction is normal outdoors; wind can push a genuine Danger-category day below
a category boundary. A definitional unit mismatch sitting directly on a life-safety
category edge. *Fix:* compute the actual NWS HI from temp/RH (both fetchable), or at
minimum document the proxy in `heat.yaml` provenance and calibrate the bands to it.

### 1.3 Medium

- **M-1. Dead heat config / hard-coded equivalents.** `heat.yaml`
  `modifiers.approach_strain_categories` and `approach_surface_min_category` are read by
  no code; the "+1 category on approach" is a hard-coded advisory string
  (`heat.py:38-42`) and `assess.py:44-45` hard-codes `_WINDOW_MIN_TIER`/`_WINDOW_MIN_HEAT`.
  Tuning the YAML does nothing — an FR-20a violation the guard test misses because it
  only tokenizes `engine/hazards/*.py`. Wire or delete; extend the guard to `assess.py`;
  add an "every threshold key is read" test.
- **M-2. No mission-window/phase-marker validation.** `phases.py:50-56` accepts
  `egress_start < approach_end` and markers outside the window (inverted spans);
  providing only one marker silently ignores it; `Mission`/`MissionSpec` never check
  `end > start`. Combined with H-8 this is also a resource amplifier (10-year windows).
- **M-3. Polygon that misses the grid entirely returns the nearest edge cell.**
  `grib/zonal.py:132-160` — the sub-cell-headwater fallback fires identically for a
  fully off-grid polygon (verified empirically); both consumers ignore the
  `fallback_nearest_cell` flag, so a mission just outside the REFS domain edge reports
  an unrelated cell's probability. Distinguish sub-cell from off-grid; surface the flag.
- **M-4. REFS member-support override mis-scales confidence on partially covered
  windows.** `orchestrator.py:186-189` — a window spanning +30→+50 h gets REFS
  confidence for a tier whose driving signal may be GEFS at +45 h. Track confidence to
  the ensemble that produced the assigned tier, or apply the override only when the
  window is fully in-range.
- **M-5. GEFS long-window subsampling and off-window fallback are silent.**
  `gefs_provider.py:71-78,158-165` — >48 h windows are subsampled to 8 fhours (the
  window max can miss the peak bucket, no note); when nothing overlaps, a single nearest
  clamped fhour is presented as the window's signal with `sources_ok=True`. Also
  `_select_fhours` will request beyond the 0.25° product's f240 horizon → 404 → H-2.
- **M-6. Member support is exceedance fraction, not "support for the assigned tier".**
  A confidently-dry 5% exceedance renders "Minimal, **Low** confidence" — reads as "we
  don't know" when the ensemble strongly agrees it's dry. Misrepresents FR-17; cautious
  direction but misleading. Consider support-for-assigned-tier (e.g. 1−p for Minimal).
- **M-7. SPC Day-1 outlook applied to missions on any day.** `spc.py:21,105-108` — a
  Day-2 Moderate contributes nothing (missed raise); today's Day-1 category can attach
  to a day-3 mission (spurious raise). SPC publishes day-2/3 GeoJSONs; select by mission
  date. Use `covers()` not `contains()` for boundary points.
- **M-8. No Haiku output guard for verdict language.** `frame.py` structurally cannot
  change a posture (good, well-tested), but AFD free text flows into the model input and
  nothing scans the narrative for "go/no-go/all clear/safe to" before splicing it into a
  life-safety briefing. Non-negotiable #1 is prompt-enforced only; add a cheap output
  filter. Also `frame.py:38-48` still instructs the model to translate "SREF"/"HREF"
  driver strings the engine no longer emits.
- **M-9. Deploy stamps `version.json` before the health check, with no rollback.**
  `deploy/deploy.sh:48→383→388-400` — a failed deploy leaves the service restarted onto
  broken code while every open PWA tab is nudged to reload into it. Stamp after health
  check passes; keep a previous-ref rollback path.
- **M-10. No Content-Security-Policy**, despite a landing.conf comment implying one.
  The markdown renderer is currently XSS-safe (verified carefully — escaping happens
  before formatting, URLs are scheme-constrained), but CSP is the backstop for the one
  place LLM/API content becomes HTML. Add one (report-only first); the app already pulls
  scripts from jsdelivr, so the allowlist is small.
- **M-11. Forecast tab lacks the reference-only disclaimer.** Every other primary view
  renders one; Forecast doesn't (`app.js:727`), and per-view footers arguably don't meet
  FR-39/40's "persistent, non-dismissible" bar. A single fixed element rendered once in
  `index.html` fixes both.
- **M-12. Single-flight registry edge cases.** Waiters call `fut.result()` with no
  timeout (a dying owner between register and try leaves all future callers hanging
  forever); `warm_watershed` can leak a pending key if `submit` races `stop_warming`;
  the registry ignores `data_dir`, so different-Settings callers coalesce.
- **M-13. HUC-10 fallback trace likely returns an origin-only "basin".** The wbd10
  layer generally lacks `ToHUC`; an empty graph walk is indistinguishable from a genuine
  headwater (`upstream.py:48-50`), returning a single-HUC polygon without trying the
  NLDI fallback. Fail loudly when the tohuc attribute is absent.
- **M-14. Corpus/test gaps beyond H-10** (full inventory in the review notes): heat
  equivalence `caution→elevated`/`extreme_caution→high` unpinned; antecedent bump
  High→Extreme + Extreme cap unpinned; SPC high/moderate/general_thunder mappings
  unpinned or confounded; unknown `source_agreement` string silently reads as
  "consistent" (overclaims confidence — fail-dangerous) and is untested; NaN/garbage
  inputs untested; per-hazard None-input postures mostly unpinned; all six replay cases
  are canyon (cave path never replay-exercised); hermeticity is discipline-only (no
  socket-blocking fixture); `nws.active_alerts` JSON parsing is always mocked away; CI
  has zero frontend coverage (the XSS-critical escaping has no test).

### 1.4 Low

- Strict-`>` vs documented-`≥` on `slot_rate_in_per_hr` (boundary unpinned) — same
  pattern as the resolved-in-H-5 override mismatch.
- GEFS lightning proxy counts CAPE and precip maxima at disjoint cells and mixes a 6-h
  accumulation with end-of-bucket instantaneous CAPE (over-counts only; document it).
- `gefs_probability.high_min` YAML comment claims a convective-rate condition the code
  doesn't apply.
- Stale SREF/HREF language throughout: `cycles.py:5-8` (actively wrong cadence),
  `api/cache.py:5`, `scheduler.py:68`, `confidence.yaml` provenance, corpus case ids.
- `HazardThresholds.__getitem__` raises bare `KeyError` deep in evaluators on a
  malformed YAML edit — validate schema at load time.
- Point exactly on a shared HUC boundary picks `gdf.iloc[0]` — service-order-dependent
  (NFR-4 seam).
- `roc.py:75-79` degenerate-kept fallback leaves a stale overlapping `excluded`
  (display-only); intersection can yield a GeometryCollection downstream masking may
  reject.
- `.idx` submessage lines (`596.1`) are silently dropped — landmine for the next field
  added (`grib/idx.py:78-82`).
- PDF `Content-Disposition` filename allows control chars through ASCII-encode;
  whitelist `[A-Za-z0-9._-]`.
- `get_settings()` re-reads `.env` on every call on the hot path; `?demo` works on the
  production origin (labeled, but restrict pre-launch); Nominatim geocode lacks
  identifying UA; localStorage spec isn't shape-validated (valid-JSON-wrong-shape flows
  through); `crop_bbox_normalize` breaks silently on dateline-crossing bboxes (assert
  CONUS assumptions loudly); GEFS availability probe ignores caller-supplied
  `gefs_base_url`; `thermal_primary()` fallback loop is unreachable dead code;
  `nws._office_cache` leaks between tests.

---

## 2. Architecture improvements

1. **Make data quality a first-class value through the whole pipeline.** The single
   highest-leverage change, resolving C-4/C-5/H-3 and half the mediums structurally:
   aggregations return `(value, quality)` instead of bare floats (`n_cells`, NaN counts,
   `fallback_nearest_cell`, members_used/total); providers record the time span they
   actually covered; `HazardInputs` carries per-input availability; the confidence layer
   consumes it (primary driver missing → Low, never default-Moderate); the SITREP/PWA
   render data age and gaps next to the numbers they affect. The engine stays pure — it
   just gains an honest input vocabulary.

2. **Freshness as data, not ops.** Stamp `(model, cycle_init, fhours, age)` into the
   bundle and structured contract; max-age gates in `cached_cycles`; cache tokens derived
   from `latest_available_cycle()`; `/v1/health` reports data age (the
   Healthchecks dead-man's switch catches a dead scheduler, but nothing today catches a
   live scheduler serving dead data).

3. **Trace completeness as a first-class trace output.** `UpstreamTrace.complete /
   truncation_risk` set on probe failure, HU4 ceiling, or an empty tohuc walk, flowing
   into flash-flood confidence and the SITREP. Silent truncation is the worst failure
   shape this subsystem has (H-1). A cheap NLDI-vs-WBD area-ratio sanity note would
   catch most cases.

4. **Move provider-level cut points into the threshold YAML.** `PRECIP_THRESH_MM=6.35`,
   `PROXY_CAPE_JKG=1000`, `PROXY_PRECIP_MM=2.5` (gefs_provider.py:44-49) and the REFS
   equivalents *define* the probabilities the engine compares against YAML bands — the
   FR-20a guarantee is bypassed one layer down, and these are exactly the numbers field
   calibration will tune. Give them a provenance block like everything else.

5. **A shared partial-failure combinator for fan-outs.** GEFS fetch (all-or-nothing),
   REFS (per-field), warm paths (per-task), and NWS (both-or-nothing) each hand-roll
   divergent error containment. One `gather_partial(tasks, min_ok=...)` helper makes
   H-2/H-4-class bugs unreproducible, and one shared `get_with_retry` unifies the HTTP
   policy (today only Open-Meteo retries).

6. **Unify forecast-hour selection** (GEFS bucket-overlap logic extracted and reused by
   REFS), with an explicit tri-state: out-of-lead-range / in-range-no-aligned-output /
   covered.

7. **API hardening layer before launch:** typed sub-models for `BriefingResponse`
   (kills the PDF injection class), MissionSpec validators (window ordering/length/lead,
   radius caps, CONUS bounds), bounded `_active` and warm queues, rate limits on
   frame/pdf/warm, and the scheduler off the event loop.

8. **Deploy safety:** health-check-then-stamp ordering, previous-ref rollback, and a CI
   job that runs the frontend escaping tests (the renderer is pure and trivially
   testable in Node).

9. **Engine niceties:** cache `load_thresholds()` (currently re-parsed per `assess()`
   call); schema-validate threshold YAML at load; `Mission.__post_init__` window
   validation; move `_WINDOW_MIN_*` into config.

## 3. Ideas

- **Areal exceedance instead of domain max.** "Any cell in the basin exceeds the
  threshold" saturates toward 100% as the domain grows — a user widening their Radius of
  Concern mechanically raises their flood tier, which will fight field calibration.
  Reporting the areal exceedance *fraction* alongside the max (or area-normalizing cut
  points) makes the seeded thresholds calibratable and the RoC slider honest.
- **Per-phase (hourly) feature vectors.** Today one mission-wide `HazardInputs` is
  evaluated for every phase; postures differ only via flags, and `window_of_concern` is
  just the phase span. The ingest already has hourly series — slicing per phase would
  make the phase table genuinely phase-aware and let the window of concern reflect when
  the hazard actually peaks.
- **Section-aware AFD parsing** (`.SHORT TERM...` / `.LONG TERM...`) so the storm-mode
  floor and ceiling are scoped to the mission day; SPC day-2/3 outlooks selected by
  mission date.
- **Compute the real NWS heat index** from temp/RH (Rothfusz + adjustments) rather than
  proxying with apparent temperature; keep apparent temp for cold/wet where it is the
  right basis.
- **Corpus depth:** REFS flood boundaries (H-10), a cave replay case, a
  warning-driven-Extreme replay, NaN/garbage-input contract cases, the unknown
  `source_agreement` string, and single-determined replay expectations (assert driver
  strings so multi-signal cases don't mask per-signal regressions).
- **Contract tests against recorded `.idx` fixtures** for the NOMADS prod REFS ensprod
  grammar before the 2026-08-31 cutover — the prod feed is configured but not yet
  validated end-to-end.
- **Verdict-language linter as a shared guard:** one function used by frame.py output
  filtering *and* a repo test over `hazard_copy.py`/templates, enforcing non-negotiable
  #1 in code rather than convention.
- **Surface basin provenance in the PWA:** delineation method (exact vs fallback), trace
  completeness, data age — the map already renders the basin; a small badge would make
  degradation visible where users look.

---

## What's genuinely solid

Worth recording so the fixes above don't read as a condemnation: the engine core is
pure, deterministic, and matches its spec; the FR-19 max rule, min-confidence
aggregation, cave gating, and thermal weighting are correctly implemented and directly
tested; GEFS/lightning-REFS/cold-wet threshold bands are textbook-pinned at their edges;
the goldens are byte-exact with disclaimer assertions; the frame layer's prepend-only
design genuinely cannot mutate a posture (and is tested for it); the markdown renderer's
escaping is rigorous (escape-then-format, scheme-constrained URLs — no XSS found); unit
handling across providers is consistent end-to-end (°F/mph/inches at the source, mm
GRIB-native with correct conversions, probabilities percent throughout);
coordinate-order handling in the watershed code is correct everywhere, the RoC disk is a
true geodesic radius, and the decode-time union-crop is verifiably bit-identical to the
old path; timezone handling (FR-9) localizes correctly at the boundaries; the service
worker's cache-busting/version-poll design is sound; and the historical-replay corpus
with sourced provenance is a model other projects should copy.
