# Changelog — 2026-07-02: GEFS corrupt-subset resilience

A staging briefing showed `gefs: unavailable (EOFError)` that persisted across every mission
edit. Root cause: a GEFS byte-range subset fetched from a `.grib2` file that was **still
publishing** was truncated — eccodes raised `EOFError` decoding it — and two defects turned a
one-member hiccup into a total, sticky GEFS outage. This change fixes both and prevents the bad
bytes from ever being cached.

Suite: **415 passed** (was 406), hermetic, ruff clean. No threshold-config or engine-output
changes (NFR-4 intact).

## Defects

1. **One corrupt member sank the whole ensemble.** `gefs_provider._member_sample` caught
   `(LookupError, ValueError, TimeoutError, RequestException, OSError)` — **not `EOFError`** —
   so a decode failure propagated out of the member fan-out and marked the entire GEFS source
   unavailable, instead of degrading that one member behind the member quorum.
2. **The corrupt artifact self-perpetuated.** Nothing detected or removed a bad subset, so every
   subsequent briefing re-read the same truncated file and re-threw — for the life of the cached
   cycle, regardless of the mission (raw subsets are cached per cycle/member/fhour, shared across
   all points). That's why editing the mission never cleared it.
3. **Truncated bytes could land in the cache atomically.** The subset write is atomic
   (temp + `os.replace`), but an open-ended byte range fetched mid-publish returns *fewer bytes
   than the message declares* and still writes "successfully" — so a structurally-broken GRIB
   was moved into place intact.

## Fixes

### `src/upstreamwx/ingest/gefs_provider.py`
- `_member_sample` now includes `EOFError` in its caught set, so a corrupt-subset decode
  degrades that member to `None` and the quorum carries GEFS (NFR-6).

### `src/upstreamwx/gefs/cache.py`
- `load_member_field_cached` **self-heals**: on a decode failure (`_CORRUPT_SUBSET_ERRORS` =
  `EOFError | ValueError | OSError`) it discards the bad artifact (`_discard_subset` unlinks the
  subset and any `.idx` sidecar) and re-fetches once; a second failure propagates so the member
  (not the source) degrades.

### `src/upstreamwx/grib/idx.py` (shared GEFS + REFS download path)
- New `validate_grib2_bytes(data, *, expected_messages, what)` walks the concatenated messages by
  their self-declared Section-0 length and verifies each opens with `GRIB` (edition 2) and closes
  with `7777`; raises `TruncatedGribError` (a `ValueError` subclass) on any framing gap —
  truncation (declared length > bytes present), missing end marker, bad magic, empty, or a
  message-count mismatch.
- `download_subset` validates the concatenated bytes **before returning**, so `cached_subset`'s
  `finally` unlinks the temp and the truncated file never reaches the cache. `TruncatedGribError`
  being a `ValueError` flows straight through the per-member degradation path (quorum carries).

## Net behavior

A GEFS/REFS subset fetched during a model's publish window can no longer poison the cache or sink
the source: the truncated download is rejected at fetch time (member degrades, quorum carries),
any bad file already on disk self-heals on next read, and the ensemble recovers on its own by the
next forecast hour / cycle instead of requiring a manual `rm -rf data/gefs`. Validation applies to
REFS too (same shared download path).

## Tests
- `tests/test_grib_idx.py`: valid single/multi-message accepted; truncated / missing-`7777` /
  bad-magic / empty / edition / count-mismatch rejected; `download_subset` raises on a truncated
  range; `TruncatedGribError` is a `ValueError`. (Existing download tests updated to real GRIB2
  framing.)
- `tests/test_gefs_cache.py`: subset self-heal refetches then decodes; persistent corruption
  raises; `_discard_subset` removes file+idx idempotently.
- `tests/test_ensemble_providers.py`: `_member_sample` degrades on `EOFError`; `fetch` survives a
  corrupt member via quorum and notes the partial ensemble.
