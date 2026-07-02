# Changelog — 2026-07-02 (round 2): all remaining HIGH findings

Closes every remaining HIGH-severity finding from
[`docs/code-review-2026-07-02.md`](code-review-2026-07-02.md) (H-1, H-4, H-5, H-7, H-8,
H-9, H-11 — H-2/H-3/H-6/H-10 were closed in the criticals round, see
[`changelog-2026-07-02-data-quality.md`](changelog-2026-07-02-data-quality.md)). The
data-quality-first-class principle continues: incomplete traces, stale caches, and
unverifiable products are represented, floored/capped in confidence, and surfaced —
never silently benign.

Suite: **401 passed** (round-1 baseline 343), hermetic, ruff clean. Threshold configs
bumped with provenance: `lightning` 1.4.0 → **1.5.0**, `confidence` 1.1.0 → **1.2.0**.

---

## H-1 — Upstream trace silently truncated the basin (fixed)

`src/upstreamwx/watershed/upstream.py`, `pourpoint.py`, `__init__.py`

- External inflow is now probed for **every** node in the upstream set — not just
  headwater leaves — via chunked `tohuc IN (...)` set-difference WFS queries (per-node
  equality fallback when a server rejects the IN form). The Paria-into-Colorado
  mid-region-confluence topology that silently dropped an entire tributary watershed is
  covered by a hermetic test.
- Probe failures fail **toward widening** (a transient WFS error no longer reads as "no
  inflow"); the check also runs at the widest (HU4) fetch, where widening is impossible —
  inflow or a probe failure there marks the trace incomplete instead of being accepted
  unconditionally.
- **Trace completeness is a first-class output:** `UpstreamTrace`/`PourpointBasin` gain
  `complete: bool` + `completeness_notes: list[str]`, persisted through the disk cache
  (legacy files default complete). Downstream (wired in `ingest/orchestrator.py`,
  `ingest/base.py`, `engine/models.py`, `engine/confidence.py`): an incomplete trace is
  named in the DATA GAPS section (`bundle_data_gaps`) and **caps flash-flood confidence
  at Moderate** (`confidence.yaml` v1.2 `incomplete_domain_max` — config, not code). The
  tier itself never lowers; only its claimed support does.
- A zero-edge tohuc walk (HU10 layer without ToHUC, attribute renames) now raises
  `UpstreamGraphError` → NLDI fallback → error, instead of returning an origin-only
  polygon posing as the watershed.
- The "widened to HUX" note is recorded only after the wider fetch actually succeeds.

## H-9 — Watershed cache defects (fixed, per the maintainer's product decision)

`src/upstreamwx/watershed/cache.py`

Product decision: the cache exists **only for identical-point reuse** (planner warm →
briefing seconds later; scheduled 6 h refresh; reopening a saved mission). Two different
user-entered coordinates must never share a basin.

- Key precision 3 → **6 decimals** (~10 cm): effectively exact; the canyon-rim
  wrong-basin sharing case is impossible. Old 3-decimal cache files are inert (never
  matched), not misread.
- WBD-fallback basins (24–54 % over-inclusive per Spike D) get a **6 h TTL**: the exact
  NLDI path is re-attempted; success upgrades the entry, failure serves the stale
  fallback (best effort, NFR-6). Exact basins never expire.
- Corrupt/empty cache files **self-heal** (log, unlink, live delineation) instead of
  poisoning the key forever; `_atomic_write` fsyncs before replace.
- The single-flight owner resolves waiters **before** the best-effort cache write — a
  disk-full error no longer fails a briefing whose delineation succeeded. Waiters time
  out on a stuck owner (120 s), evict the entry, and delineate themselves.

## H-4 — NWS alerts checked over the upstream basin (fixed)

`src/upstreamwx/ingest/nws.py`, `ingest/orchestrator.py`

- New `basin_flood_flags(polygon)`: samples up to 5 interior points across the basin
  (representative point + quadrants, ~1 km dedupe) and queries the alerts API
  concurrently — a Flash Flood Warning polygon over the upper watershed that misses the
  canyon mouth now sets the product flags. Runs on the ensemble branch's thread pool
  (overlapped with the GEFS/REFS pulls, ~zero added latency).
- Flags merge by **OR** across the point provider and the basin check
  (`_MERGE_OR_FIELDS`) — raise-only and order-independent (NFR-4); a basin-check failure
  appends an explicit "verified at the mission point only" note and can only miss a
  raise, never lower a posture.
- Event-name classification extracted to a shared `flood_flags_from_events()` (one
  vocabulary for both checks; coastal flooding still excluded). The flash-flood driver's
  "over area or upstream domain" wording is now true.

## H-5 — Window-blind AFD ceiling scoped to REFS range (fixed)

`src/upstreamwx/engine/hazards/lightning.py`, `data/thresholds/lightning.yaml` (v1.5)

- The contextual ceiling (AFD "isolated"/"scattered" caps the lightning tier) now applies
  **only while REFS is in-window**. Rationale (in the YAML provenance): the AFD scan is
  window-blind — the coverage word may describe a different day entirely — and beyond
  REFS range the REFS override that keeps the cap honest can never fire; a posture-
  *lowering* text signal must not cap a live multi-day ensemble it cannot be checked
  against. The raise-only storm-mode floor is unchanged (over-warning at worst).
- Corpus: the two ceiling cases are now in-window (REFS present, below the override);
  two new cases pin that the ceiling is inert beyond REFS range.

## H-7 — Offline review of the last briefing (fixed; verified in real Chromium)

`frontend/js/app.js`, `frontend/sw.js`

- Every successful briefing is persisted to `localStorage["uwx.briefing.v1"]`
  (`{v, stored_at, spec, briefing}`, single most-recent, quota-safe with one
  clear-and-retry, corrupt records discarded). On boot/refresh network failure the
  persisted briefing renders fully with a **non-dismissible "Cached briefing — saved
  \<time> (\<age>)" status** (stale never masquerades as fresh) and a mismatch note when
  it belongs to a different saved mission; the retry banner stays. Demo mode and this
  path are mutually exclusive — production never touches sample data.
- The "Available offline" badge is keyed to the real on-device copy; the dead SW
  POST-cache branch is removed (Cache API cannot store POSTs).
- The documented offline PDF fallback is finally wired: offline/failed export writes the
  `uwx.pdf.briefing` handoff and opens the precached `pdf/briefing-pdf.html?print=1`
  (FR-27).
- Verified end-to-end in headless Chromium: online persist → offline reload renders the
  cached briefing with age label → mismatch note → corrupt-record discard → PDF handoff
  → fresh fetch clears the notice.

## H-8 — API request validation, bounded sinks, per-IP rate limiting (fixed)

`src/upstreamwx/api/models.py`, `app.py`, `service.py`, `config.py`

- **MissionSpec validation:** CONUS bounds (24–50 / −125–−66), `end > start`, 7-day max
  window, both radii capped at 322 km (200 mi, the PRD slider max). Wall-clock currency
  (`ensure_current`, service-injected `now`): live windows starting beyond the f240
  horizon (10 d) or fully ended >24 h ago are 422 — **skipped for offline `inputs`
  replays** so pinned-date fixtures stay deterministic (FR-25/NFR-4).
- **Bounded sinks:** `_active` capped at `api_active_missions_max` (256; evicts the
  soonest-ending mission — refresh cost scales linearly with the registry);
  the warm queue capped at `api_warm_pending_max` (32 → `WarmQueueFull` → 503 +
  Retry-After); warm requests CONUS-validated (no 3–15 s delineations outside coverage).
- **Per-IP token buckets** (dependency-free, thread-safe, LRU-bounded at 4096 IPs):
  frame 6/min (billable Anthropic), pdf 4/min (Chromium launch), warm 12/min → 429 +
  Retry-After. `X-Forwarded-For` trusted only from a loopback peer, rightmost entry
  (matches nginx `$proxy_add_x_forwarded_for`). `/v1/briefing` deliberately not
  rate-limited (cache hits stay cheap; cold generations keep the `_gen_sem` 503 path).
  Gate: `api_rate_limits_enabled`. `/v1/health` echoes the limits.

## H-11 — Real NWS heat index (fixed)

`src/upstreamwx/ingest/openmeteo.py`

- `heat_index_f` is now the actual **NWS Rothfusz heat index** computed in-house from
  temperature + relative humidity (both fetched; the simple Steadman blend below 80 °F,
  the full regression with low-RH/high-RH adjustments at/above) — verified against the
  NWS chart (90 °F/70 % → ~106; 96 °F/13 % → ~91). The FR-15 category bands are the NWS
  standard, so the value compared against them is now the NWS index rather than
  Open-Meteo's apparent-temperature formulation (wind + solar; several °F off across a
  category boundary). Cold/wet keeps apparent temperature (wind folded in) — correct
  basis there. Missing temp/RH falls back to apparent temperature with an explicit note.

---

## Client-visible behavior changes

1. Requests outside CONUS, with malformed/oversize windows, or oversize radii are 422;
   heavy endpoints can return 429/503 with Retry-After under abuse.
2. Humid-day heat categories may shift (up or down) vs. the old apparent-temperature
   proxy — they now match the official NWS heat-index chart.
3. Multi-day-out missions with an AFD "isolated/scattered" mention no longer show a
   capped lightning tier; same-day missions keep the ceiling (with the REFS override).
4. Briefings near an upstream warning polygon can now show active flood products the
   point check missed.
5. A possibly-truncated watershed shows a DATA GAP line and at most Moderate flood
   confidence.
6. The PWA shows the last real briefing offline (clearly aged), and PDF export works
   offline via the print template.

## Corpus/test deltas

New: `tests/test_watershed_completeness.py` (10), `tests/test_watershed_cache.py` (13),
`tests/test_api_limits.py` (25), heat-index/basin-alert/OR-merge cases in
`tests/test_data_quality.py`; corpus: lightning ceiling in-window + out-of-range inert
cases, confidence incomplete-domain cap cases. Updated: goldens (threshold version
strings only). Removed behavior: none — every posture-affecting change is corpus-pinned
with in-file rationale.
