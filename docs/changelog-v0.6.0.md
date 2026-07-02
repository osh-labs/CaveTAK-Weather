# UpstreamWX v0.6.0 — Pre-launch hardening release

This release closes **every Critical and High finding** from the 2026-07-02 pre-launch code
review ([`docs/code-review-2026-07-02.md`](code-review-2026-07-02.md)), plus a production
incident found during staging. Its unifying theme is **data quality as a first-class value**:
a missing, stale, truncated, NaN, or partial input is never allowed to read as benign — it is
represented distinctly, floored/capped in confidence, surfaced to the user, and recovered from
automatically where possible.

- **Tests:** 285 → **418** passing (hermetic, offline by default).
- **Threshold config:** `confidence` 1.0.0 → **1.2.0**, `lightning` 1.4.0 → **1.5.0** (both with
  provenance); engine output is otherwise unchanged (NFR-4).
- **Detailed per-area changelogs:** data-quality criticals
  [`changelog-2026-07-02-data-quality.md`](changelog-2026-07-02-data-quality.md); highs
  [`changelog-2026-07-02-high-fixes.md`](changelog-2026-07-02-high-fixes.md); GEFS resilience
  [`changelog-2026-07-02-gefs-resilience.md`](changelog-2026-07-02-gefs-resilience.md).

---

## Critical fixes (launch blockers)

**C-1 — The slot-canyon conservative flood fallback was dead code live.** No provider populated
`convective_rate_in_per_hr` (or `cape_jkg`/`wind_mph`), so the engine's "force ≥ High on a slot
at > 0.5 in/hr" safeguard could never fire in production. Open-Meteo now derives all three; the
slot fallback is live, and a slot mission whose rate feed is down says so explicitly.

**C-2 — `/v1/briefing/pdf` allowed script injection into server-side Chromium.** The endpoint
rendered client-supplied JSON in headless Chromium (`--no-sandbox`, `file://`), enabling local
file read / SSRF via unescaped fields. Every interpolation is now escaped/coerced (verified by a
real-Chromium no-execution test), fields are typed pydantic sub-models that reject markup, a
Playwright request gate blocks `file://` and network fetches, and the endpoint gained a 2 MiB
payload cap, a render concurrency semaphore, and filename sanitization.

**C-3 — The refresh scheduler blocked the asyncio event loop.** The per-cycle warm + refresh pass
ran synchronously on the event loop, taking the API dark (health checks, briefings, 502s) at
every 00/06/12/18Z boundary. Both passes now run via `asyncio.to_thread`; lifespan shutdown is
bounded by a timeout so a stuck pass can't hang exit.

**C-4 — Missing / failed / NaN inputs read as "no hazard."** Systemic and the review's core
finding. Now: all-NaN zonal aggregates return `None` (never a NaN that compares False against
every threshold); off-grid polygons refuse the fabricated nearest-cell value; a failed
Open-Meteo leaves precip tri-state (`None` = unknown, applied conservatively) instead of a
"dry" `False` that gated the flood band off; a watershed failure no longer silences lightning;
the engine emits explicit **"DATA GAP … unassessed, not low"** drivers; and `confidence.yaml`
v1.1 floors confidence at **Low** whenever a hazard's primary driver was unavailable. A single
`bundle_data_gaps()` feeds the SITREP "DATA GAPS" section and the structured contract's
`data_quality` block.

**C-5 — Stale ensemble cycles were served as current, and the cache token lied about it.** A
stalled scheduler (or long-idle CLI) could serve days-old GEFS/REFS as "current," and the API
cache token used wall-clock cycle math while data lagged publication (a 06Z briefing labeled
12Z-fresh). Now: a `ensemble_max_age_h` (24 h) freshness bound gates both ensembles (GEFS
re-probes, REFS degrades loudly), and the cache token tracks the newest **available** cycle.

## High fixes

**H-1 — Silent upstream-watershed truncation.** The trace probed external inflow only at
headwater leaves, swallowed probe failures, and accepted the widest fetch unconditionally —
silently dropping tributary area and understating flash-flood risk. It now probes **every** node
(chunked `tohuc` set-difference), fails probe errors toward widening, checks at the HU4 ceiling,
and carries first-class completeness (`UpstreamTrace.complete`) → a DATA GAP + flash-flood
confidence capped at Moderate (`confidence.yaml` v1.2). A broken tohuc walk raises instead of
returning an origin-only "basin."

**H-2 — GEFS was all-or-nothing.** One failed member fetch out of ~250 discarded the whole
ensemble — routine during a cycle's publish window. Per-member failures now degrade to `None`
behind an 8-member quorum, with a partial-ensemble provenance note.

**H-3 — Open-Meteo fetched only 3 forecast days** while missions run to 5+. Day-4 windows read
as "dry"/no-data with no note. Now fetches 16 days with coverage-aware tri-state precip.

**H-4 — NWS alerts were checked at the point only**, while the driver text claimed upstream
coverage. A basin-wide alert check (sampled points, concurrent with the ensemble pulls,
OR-merged raise-only) now catches a warning polygon over the upper watershed.

**H-5 — The lightning AFD ceiling lowered postures on window-blind text.** For multi-day-out
missions, an "isolated/scattered" mention (possibly about a different day) could cap a strong
ensemble signal with no override available. The ceiling now applies only while REFS is in-window
(`lightning.yaml` v1.5); the raise-only storm-mode floor is unchanged.

**H-6 — Short same-day windows lost REFS entirely** and were mislabeled "outside range." REFS
selection now covers between-output hours by accumulation bucket — the product's core
short-window use case keeps the authoritative 3 km signal.

**H-7 — Offline review of the last briefing was broken in production.** The service worker
couldn't cache the `POST /v1/briefing` response and nothing else persisted it. The PWA now
persists the last briefing to `localStorage` and renders it offline with a non-dismissible,
age-labeled "Cached briefing" status (and a mismatch note for a different saved mission); the
offline PDF handoff is wired. Verified end-to-end in headless Chromium.

**H-8 — No request validation or throttling on a public API.** Added MissionSpec validation
(CONUS bounds, `end > start`, 7-day window cap, 322 km radius caps, live-window currency —
offline replays exempt), bounded `_active` and warm queues with eviction/backpressure, and
dependency-free per-IP rate limits on frame/pdf/warm (429 + Retry-After).

**H-9 — Watershed cache defects.** Now identical-point-only (6-decimal keys — two different
coordinates never share a basin), with TTL'd re-attempts of coarse fallback basins, self-healing
corrupt-file reads, resolve-waiters-before-write single-flight, fsync'd atomic writes, and waiter
timeouts.

**H-10 — REFS flood cut points had zero corpus coverage.** The "authoritative in-window" flood
signal is now pinned at its boundaries plus REFS-raises-over-GEFS/product cases.

**H-11 — `heat_index_f` was Open-Meteo apparent temperature**, evaluated against NWS Heat Index
category bands. It is now the real NWS Rothfusz heat index computed from temp + RH (chart-verified);
cold/wet keeps apparent temperature as its correct basis.

## Post-review production hardening — GEFS/REFS corrupt-subset resilience

Found on staging: a briefing stuck at `gefs: unavailable (EOFError)` across every mission edit.
A byte-range subset fetched while a `.grib2` was still publishing was truncated; eccodes raised
`EOFError`, and a one-member hiccup sank the whole source and stuck there. Closed with defense in
depth:

- **Prevention at fetch:** the shared download path validates GRIB2 framing (`GRIB`…`7777`,
  declared lengths, message count) via `validate_grib2_bytes` before a subset is accepted; a
  truncated download raises `TruncatedGribError` (a `ValueError`) and never reaches the cache.
- **Recovery at read:** GEFS **and** REFS decode paths self-heal — on a decode failure they
  discard the bad artifact (+ `.idx`) and re-fetch once.
- **Survivability:** `_member_sample` catches `EOFError` (GEFS quorum carries), and both warm
  loops skip a truncated subset instead of sinking the pass.

The upshot: a mid-publish truncation now self-corrects by the next forecast hour instead of
requiring a manual cache wipe. Applies to both ensembles (shared download path).

---

## Upgrade notes

- **No cache reset required.** The raw subset format is unchanged, so valid cached files are
  reused as-is; any latent truncated GEFS *or* REFS file self-heals on first read. (Clearing
  `data/gefs`/`data/refs` is safe but unnecessary — it only adds a cold-fetch latency spike.)
- **No config migration required.** New settings (`ensemble_max_age_h`, `api_active_missions_max`,
  `api_warm_pending_max`, `api_rate_limits_enabled`, …) ship with safe defaults.
- **Behavior changes users will see** (all in the conservative direction): during a source
  outage, briefings now show **Low** confidence + a DATA GAP note instead of Minimal/Moderate; a
  humid-day heat category may shift to match the official NWS chart; a possibly-truncated
  watershed shows a DATA GAP and at most Moderate flood confidence; the API now returns 422 for
  out-of-CONUS / malformed windows and 429/503 under abuse; the PWA shows the last briefing
  offline with a mandatory age label.
