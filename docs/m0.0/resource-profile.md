# SREF Job — Resource Profile (M0.0)

**Date:** 2026-06-18 · **Purpose:** size EC2 headroom for the recurring SREF
processor, the heaviest backend component (PRD §11.2–§11.4). Measured on the dev
container (Python 3.11), against live NOMADS cycle `20260617/15Z`.

## Measured (single cycle, Buckskin upstream domain)

| Operation | Download | Messages | Wall time | Notes |
| --- | ---: | ---: | ---: | --- |
| Find latest available cycle | tiny | — | ~1.4 s | newest-first probe of recent cycles |
| P(precip > 0.25 mm/3h), 1 window | 248 KB | 1 | ~1.9 s | idx byte-range subset |
| P(precip > 12.7 mm/3h slot), 1 window | 28 KB | 1 | ~0.8 s | |
| P(CAPE > 1000 J/kg), all fcst hours | 3.7 MB | 30 | ~6.7 s | no accumulation subsetting → all hours |
| Polygon aggregation (regionmask) | — | — | sub-second | per domain, after grid is in memory |
| **Peak RSS (whole process)** | — | — | — | **~514 MB** |

Full uncompressed product files for reference: `prob` ~660 MB, `spread`/`mean`
~370 MB each. **Byte-range subsetting reduces a field to 0.03–4 MB** — a 100–1000×
reduction. It is mandatory, not an optimization.

## Architecture implication: cost is per-cycle, not per-domain

The SREF products are CONUS-wide grids. The efficient pattern (for M0.1) is:

1. **Once per cycle:** download the selected messages (the variables/thresholds/
   forecast hours the rule engine needs) for the whole CONUS grid — a few MB.
2. **Per active domain:** rasterize that domain's polygon onto the cached grid and
   reduce (sub-second, ~tens of MB transient).

So **download and decode scale with cycles/day, while only the cheap aggregation
scales with active-domain count.**

## Projection

Assume a realistic engine field set ≈ 10–20 messages/cycle (a few precip thresholds
× relevant forecast windows + a few CAPE/PLI thresholds at relevant hours):

- **Download/decode per cycle:** ~3–8 MB, ~10–20 s wall, peak RSS well under ~1 GB.
- **Cycles/day:** 4 (03/09/15/21Z).
- **Per-domain aggregation:** sub-second + ~tens of MB transient each.

| Active domains | Per-cycle work | Daily download | Notes |
| ---: | --- | ---: | --- |
| 10 | 1 download + 10 aggregations | ~12–32 MB | trivial |
| 100 | 1 download + 100 aggregations | ~12–32 MB | aggregation a few min total |
| 500 | 1 download + 500 aggregations | ~12–32 MB | consider batching / parallel aggregation |

(Download is per-cycle, so daily download is ~4× the per-cycle figure regardless of
domain count, provided domains are aggregated from the shared cached grid.)

## Headroom verdict

**Fits comfortably on the existing UpstreamWX EC2** at the PRD's "hundreds of users"
scale (PRD §11.3). The recurring SREF job's real constraints are:

- **Peak memory** during decode (~0.5 GB observed for a modest field set; keep an eye
  on it if many forecast hours/thresholds are loaded at once) — bound it by selecting
  only needed messages and processing fields sequentially.
- **NOMADS retention (~2 days):** the scheduler must run on the SREF cadence and pull
  promptly; a missed window means re-fetch is impossible.

No separate instance or bill is needed for v1.

## Levers (apply in M0.1)

- idx byte-range subsetting (already implemented) — the dominant saving.
- Use `ensprod` `prob`/`spread` products instead of summing raw members.
- Download the CONUS field set **once per cycle**, aggregate all domains from it.
- Select specific forecast hours for CAPE (avoid pulling all 30).
- Cache the decoded grid for the cycle; parallelize per-domain aggregation if domain
  count grows.
