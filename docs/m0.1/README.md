# M0.1 — Data Ingest, Decision Engine, Watershed Component: Findings

**Date:** 2026-06-18 · **Branch:** `claude/nifty-bohr-03zh0d`

M0.1 builds the product's spine: the deterministic decision engine, driven by
externalized threshold config and validated against a corpus, plus the watershed
promotion and the data-ingest abstraction that feeds the engine (roadmap §M0.1).

## Exit-criteria status

| Exit criterion (roadmap §M0.1) | Status |
| --- | --- |
| Engine produces expected postures/confidence/windows across the validation corpus | ✅ `tests/corpus/` — boundary cases per hazard (`test_engine_corpus.py`) **and** documented-event replays (`test_engine_replay.py`) |
| Threshold changes are config-only (FR-20a) | ✅ YAML in `upstreamwx/data/thresholds/`; `test_thresholds_config.py` proves config-only tuning + no hard-coded cut points |
| SREF job runs on schedule and caches | ⏸ **Moved to M0.1.1 (EC2):** scheduling/persistence are not testable in an ephemeral container. On-demand SREF processing is built + live-tested here. |
| Offline tests pass with no network; lint clean | ✅ `pytest` 84 passed / 7 network deselected; `ruff` clean |
| Live adapters work against real services | ✅ `pytest -m network` 5 passed (NWS, Open-Meteo, SPC, SREF, watershed cache) |

## What was built

### Decision engine — `upstreamwx.engine`
- **Domain model** (`models.py`): `Mission`, `HazardInputs` (the normalized feature
  vector the engine consumes — decoupled from providers per FR-13/§12),
  `HazardPosture`, `PhaseAssessment`, `BriefingResult` (FR-22 structure), plus
  ordered `Tier`/`HeatCategory`/`Confidence` enums.
- **Phases** (`phases.py`): inference (FR-9a), the phase × activity applicability
  matrix (FR-14a), thermal weighting (FR-14b), lightning/cave gating (FR-14c).
- **Hazard evaluators** (`hazards/`): one pure function per hazard
  (flash flood, lightning, heat, cold/wet) reading Appendix B config only.
- **Confidence** (`confidence.py`): SREF member support vs source agreement (FR-17, §16.5).
- **Orchestrator** (`assess.py`): per-phase evaluation, overall posture = max across
  applicable hazards (FR-19); deterministic (NFR-4).

### Threshold config — `upstreamwx/data/thresholds/*.yaml`
Appendix B §16.1–16.5 transcribed as versioned YAML, each with a `provenance`
block (effective date, rationale, source). Loaded at runtime by
`engine/thresholds.py`; the engine never hard-codes a number (FR-20a).

### Validation corpus — `tests/corpus/*.yaml`
The oracle for "passing internal validation," in the two halves the roadmap
defines:
- **Boundary cases** (`flash_flood`/`lightning`/`heat`/`cold_wet`/`confidence`
  `.yaml`, run by `test_engine_corpus.py`): hand-constructed inputs sitting just
  inside/outside each tier edge, per hazard — the backbone.
- **Historical replay** (`historical_replay.yaml`, run by `test_engine_replay.py`):
  documented events (Antelope Canyon 1997, Keyhole/Zion 2015, Grand Canyon heat,
  cold-water slots) plus a clear-day control, run whole-mission through
  `engine.assess` and asserted to flag the right overall + dominant-hazard tier.
  Each carries provenance naming the event and the conditions its inputs encode.

### Watershed promotion — `upstreamwx.watershed.resolve_and_trace_cached`
Wraps the M0.0 resolve + trace with an on-disk GeoJSON cache (keyed by rounded
lat/lon) under `Settings.data_dir`, plus retry/backoff on transient USGS failures.

### Data ingest — `upstreamwx.ingest`
Provider abstraction (`base.py`: `IngestBundle` + `to_hazard_inputs`) so sources
are swappable (§12). Live adapters: NWS alerts + AFD (`nws.py`, FR-5), Open-Meteo
derived fields (`openmeteo.py`, FR-6), SPC categorical outlook (`spc.py`), SREF over
the upstream domain (`sref_provider.py`, FR-7). `orchestrator.py` assembles the
bundle with graceful degradation (NFR-6): `mission → trace → bundle → HazardInputs
→ engine.assess`.

## Deferred to M0.1.1 (EC2)
Recurring SREF/AFD scheduling at cadence, persistent cross-restart cache, and the
NLDI fallback smoke test. See roadmap §M0.1.1.

## Reproduce

```sh
uv venv && uv pip install -e '.[dev]'
.venv/bin/pytest -q                 # 84 hermetic (engine + corpus + config + ingest)
.venv/bin/pytest -m network -q      # 5 live adapters (services reachable)
.venv/bin/ruff check src tests
```
