"""GEFS/REFS provider logic — member-exceedance reduction, fhour selection, agreement.

Hermetic: the GRIB byte-range + zonal machinery is covered by the grib/ and sref/href package
tests (REFS reuses them unchanged), so here we mock the per-member field load and pin the
provider-specific logic: the GEFS member-exceedance reducer + CAPE×precip lightning proxy, the
6-hourly forecast-hour selection, and the GEFS<->REFS cross-ensemble agreement.
"""

from __future__ import annotations

from datetime import UTC, datetime

from shapely.geometry import box

from upstreamwx.engine.models import ActivityType, Mission
from upstreamwx.gefs.sources import MEMBERS, GefsCycle
from upstreamwx.ingest import gefs_provider
from upstreamwx.ingest.base import IngestBundle
from upstreamwx.ingest.refs_provider import cross_ensemble_agreement

_POLY = box(-112.0, 37.0, -111.9, 37.1)


def _mission():
    return Mission(
        activity_type=ActivityType.CANYON,
        lat=37.05,
        lon=-111.95,
        window_start=datetime(2026, 6, 20, 0, tzinfo=UTC),
        window_end=datetime(2026, 6, 20, 12, tzinfo=UTC),
    )


# --- cross-ensemble agreement (FR-17, §16.5) -------------------------------------------------

def test_agreement_consistent_when_both_present_and_aligned():
    assert cross_ensemble_agreement(60.0, 50.0, 55.0, 45.0) == "consistent"


def test_agreement_partial_on_strong_vs_absent():
    # GEFS strong precip (70%), REFS near-absent (5%) -> material divergence.
    assert cross_ensemble_agreement(70.0, 10.0, 5.0, 8.0) == "partial"


def test_agreement_consistent_when_a_side_is_none():
    # Out-of-range / unavailable on one side resolves to consistent (no false conflict).
    assert cross_ensemble_agreement(90.0, None, None, 10.0) == "consistent"


# --- GEFS forecast-hour selection ------------------------------------------------------------

def test_select_fhours_covers_window_on_6h_grid():
    cycle = GefsCycle("20260620", 0)
    # Window 1-10 h from init -> the 6-hourly buckets ending at f06 [0,6] and f12 [6,12] overlap.
    hours = gefs_provider._select_fhours(cycle, datetime(2026, 6, 20, 1, tzinfo=UTC),
                                         datetime(2026, 6, 20, 10, tzinfo=UTC))
    assert hours == [6, 12]
    assert all(h % 6 == 0 for h in hours)


def test_select_fhours_capped_and_nonempty():
    cycle = GefsCycle("20260620", 0)
    # A long window would exceed the cap; selection subsamples to <= MAX_FHOURS.
    hours = gefs_provider._select_fhours(cycle, datetime(2026, 6, 20, 6, tzinfo=UTC),
                                         datetime(2026, 6, 23, 6, tzinfo=UTC))
    assert 0 < len(hours) <= gefs_provider.MAX_FHOURS


# --- GEFS member-exceedance reducer + lightning proxy ----------------------------------------

def test_gefs_fetch_computes_member_exceedance_and_proxy(monkeypatch):
    """Mock per-member samples so the reduction math is exercised without any GRIB I/O."""
    n = len(MEMBERS)  # 31

    def fake_select(cycle, ws, we):
        return [24]  # a single forecast hour keeps the bookkeeping simple

    # Build per-member (apcp_flood, apcp_ltng, cape_ltng) so a known fraction exceeds each
    # threshold: ~half the members get heavy precip; ~a third are convective (CAPE & precip).
    samples: dict[str, tuple[float, float, float]] = {}
    for i, m in enumerate(MEMBERS):
        apcp = 10.0 if i < n // 2 else 1.0          # > 6.35 for the first half
        cape = 1500.0 if i < n // 3 else 200.0      # > 1000 for the first third
        samples[m] = (apcp, apcp, cape)

    def fake_member_sample(
        cycle, member, fhour, polygon, ltng_polygon, *, settings, crop_bbox=None, use_pool=False
    ):
        return samples[member]

    monkeypatch.setattr(gefs_provider, "_select_fhours", fake_select)
    monkeypatch.setattr(gefs_provider, "_member_sample", fake_member_sample)
    monkeypatch.setattr(
        gefs_provider, "_resolve_cycle", lambda c, *, settings=None: GefsCycle("20260620", 0)
    )

    bundle = IngestBundle()
    gefs_provider.fetch(_mission(), bundle, _POLY)

    # P(precip > 6.35) = first-half fraction; proxy = convective (CAPE>1000 AND precip>2.5) frac.
    assert bundle.gefs_p_precip == round(100.0 * (n // 2) / n, 6) or abs(
        bundle.gefs_p_precip - 100.0 * (n // 2) / n
    ) < 1e-9
    # The lightning proxy needs BOTH instability and precip: the convective third all have
    # apcp=10 (>2.5) and cape=1500 (>1000).
    assert abs(bundle.gefs_p_tstm - 100.0 * (n // 3) / n) < 1e-9
    assert bundle.sources_ok["gefs"] is True
    # Exceedance fractions double as member support for the confidence qualifier.
    assert abs(bundle.member_support["flash_flood"] - bundle.gefs_p_precip / 100.0) < 1e-9


def test_gefs_fetch_degrades_when_no_member_fields(monkeypatch):
    monkeypatch.setattr(gefs_provider, "_select_fhours", lambda c, ws, we: [24])
    monkeypatch.setattr(
        gefs_provider, "_member_sample", lambda *a, **k: (None, None, None)
    )
    monkeypatch.setattr(
        gefs_provider, "_resolve_cycle", lambda c, *, settings=None: GefsCycle("20260620", 0)
    )
    bundle = IngestBundle()
    gefs_provider.fetch(_mission(), bundle, _POLY)
    assert bundle.sources_ok["gefs"] is False
    assert bundle.gefs_p_precip is None and bundle.gefs_p_tstm is None
