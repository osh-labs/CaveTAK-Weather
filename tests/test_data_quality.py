"""Data quality as a first-class value — the missing/NaN/stale-input contract (NFR-6).

Pins the anti-"silent benign" behaviors added after the 2026-07-02 pre-launch review:
an all-NaN aggregation reports None (never a NaN that reads as "no hazard"), an
off-grid domain refuses the nearest-cell fallback, unknown surface precip is tri-state
(not "dry"), the Open-Meteo adapter populates the slot-fallback/CAPE fields and marks
window coverage, stale ensemble cycles are never served as current, a failed NWS chain
marks products *unchecked*, and the API cache token tracks the data rather than the
wall clock. Hermetic — every network call is faked.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
import xarray as xr
from shapely.geometry import box

from upstreamwx.config import Settings
from upstreamwx.engine.models import Mission
from upstreamwx.grib.zonal import aggregate_over_polygon
from upstreamwx.ingest import nws, openmeteo, refs_provider
from upstreamwx.ingest.base import IngestBundle, bundle_data_gaps, to_hazard_inputs
from upstreamwx.ingest.gefs_provider import MAX_FHOUR_0P25, _select_fhours
from upstreamwx.refs.sources import RefsCycle


def _grid(fill: float | np.ndarray = 0.0) -> xr.DataArray:
    lats = np.arange(40.0, 36.0, -0.25)
    lons = np.arange(-112.0, -108.0, 0.25)
    data = np.full((lats.size, lons.size), fill, dtype=float)
    return xr.DataArray(
        data,
        dims=("latitude", "longitude"),
        coords={"latitude": lats, "longitude": lons},
        name="APCP",
    )


def _mission(start: str, end: str, **kw) -> Mission:
    return Mission(
        activity_type="canyon",
        lat=37.0,
        lon=-111.0,
        window_start=datetime.fromisoformat(start),
        window_end=datetime.fromisoformat(end),
        **kw,
    )


# -- zonal aggregation ---------------------------------------------------------------


def test_all_nan_cells_report_none_not_nan() -> None:
    """An all-NaN masked region must yield None — NaN compares False against every
    threshold and would silently read as 'no hazard' (the anti-conservative direction)."""
    da = _grid(np.nan)
    agg = aggregate_over_polygon(da, box(-111.5, 37.0, -109.0, 39.0))
    assert agg.n_cells > 0
    assert agg.max_value is None
    assert agg.mean_value is None


def test_partial_nan_cells_ignored() -> None:
    da = _grid(2.0)
    da.values[0, 0] = np.nan
    agg = aggregate_over_polygon(da, box(-112.5, 35.0, -107.0, 41.0))
    assert agg.max_value == 2.0


def test_off_grid_polygon_refuses_nearest_cell_fallback() -> None:
    """A polygon nowhere near the grid must raise, not answer with an edge cell's value."""
    da = _grid(5.0)
    europe = box(10.0, 47.0, 10.1, 47.1)
    with pytest.raises(ValueError, match="outside the field grid"):
        aggregate_over_polygon(da, europe)


def test_subcell_on_grid_polygon_still_falls_back() -> None:
    """The honest sub-cell fallback (headwater smaller than a cell) is preserved."""
    da = _grid(5.0)
    tiny = box(-110.06, 38.06, -110.04, 38.08)
    agg = aggregate_over_polygon(da, tiny)
    assert agg.fallback_nearest_cell is True
    assert agg.max_value == 5.0


# -- Open-Meteo adapter (C-1 fields + coverage tri-state) ------------------------------


def _om_payload(
    hours: list[str],
    precip: list[float],
    cape: list[float],
    temp_f: float = 70.0,
    rh: float = 50.0,
) -> dict:
    n = len(hours)
    return {
        "hourly": {
            "time": hours,
            "temperature_2m": [temp_f] * n,
            "apparent_temperature": [75.0] * n,
            "relative_humidity_2m": [rh] * n,
            "precipitation": precip,
            "precipitation_probability": [10.0] * n,
            "cape": cape,
            "wind_speed_10m": [12.0] * n,
            "wind_gusts_10m": [18.0] * n,
            "weather_code": [1] * n,
        }
    }


def test_openmeteo_populates_slot_rate_cape_and_wind(monkeypatch) -> None:
    """The slot-canyon fallback's convective rate (and CAPE/wind) come from data the
    adapter already fetched — previously never written, leaving the safeguard dead live."""
    hours = [f"2026-06-20T{h:02d}:00" for h in range(0, 24)]
    precip = [0.0] * 24
    precip[10] = 0.9  # 0.9 in over one hour = 0.9 in/hr convective rate
    cape = [100.0] * 24
    cape[11] = 1800.0
    monkeypatch.setattr(openmeteo, "_query", lambda lat, lon: _om_payload(hours, precip, cape))

    bundle = IngestBundle()
    openmeteo.fetch(_mission("2026-06-20T08:00", "2026-06-20T18:00"), bundle)

    assert bundle.convective_rate_in_per_hr == pytest.approx(0.9)
    assert bundle.cape_jkg == pytest.approx(1800.0)
    assert bundle.wind_mph == pytest.approx(12.0)
    assert bundle.measurable_precip is True


def test_openmeteo_covered_dry_window_is_false_not_none(monkeypatch) -> None:
    hours = [f"2026-06-20T{h:02d}:00" for h in range(0, 24)]
    monkeypatch.setattr(
        openmeteo, "_query", lambda lat, lon: _om_payload(hours, [0.0] * 24, [50.0] * 24)
    )
    bundle = IngestBundle()
    openmeteo.fetch(_mission("2026-06-20T08:00", "2026-06-20T18:00"), bundle)
    assert bundle.measurable_precip is False


def test_openmeteo_uncovered_window_is_unknown_not_dry(monkeypatch) -> None:
    """A window past the fetched horizon must read 'unknown', never 'dry' — a False here
    gated the GEFS Elevated flood band off for day-4+ missions."""
    hours = [f"2026-06-20T{h:02d}:00" for h in range(0, 24)]
    monkeypatch.setattr(
        openmeteo, "_query", lambda lat, lon: _om_payload(hours, [0.0] * 24, [50.0] * 24)
    )
    bundle = IngestBundle()
    openmeteo.fetch(_mission("2026-06-25T08:00", "2026-06-25T18:00"), bundle)
    assert bundle.measurable_precip is None
    assert bundle.heat_index_f is None
    assert any("outside the fetched forecast range" in n for n in bundle.notes)
    inputs = to_hazard_inputs(bundle)
    assert inputs.measurable_precip is None


def test_openmeteo_partial_coverage_dry_is_unknown(monkeypatch) -> None:
    hours = [f"2026-06-20T{h:02d}:00" for h in range(0, 12)]  # series ends 11Z
    monkeypatch.setattr(
        openmeteo, "_query", lambda lat, lon: _om_payload(hours, [0.0] * 12, [50.0] * 12)
    )
    bundle = IngestBundle()
    openmeteo.fetch(_mission("2026-06-20T08:00", "2026-06-20T18:00"), bundle)
    assert bundle.measurable_precip is None  # the uncovered tail could hold the precip
    assert any("partially covers" in n for n in bundle.notes)


# -- NWS heat index (FR-15: the value must match the official category bands) -------------


def test_nws_heat_index_matches_nws_chart() -> None:
    from upstreamwx.ingest.openmeteo import _nws_heat_index as hi

    assert hi(90.0, 70.0) == pytest.approx(106.0, abs=1.5)  # NWS chart: ~105 °F
    assert hi(96.0, 13.0) == pytest.approx(91.0, abs=1.5)  # low-RH adjustment applies
    assert hi(70.0, 50.0) == pytest.approx(70.0, abs=1.5)  # below 80 °F ≈ air temp
    assert hi(85.0, 90.0) == pytest.approx(102.0, abs=1.5)  # high-RH adjustment applies


def test_openmeteo_heat_index_computed_from_temp_rh(monkeypatch) -> None:
    """heat_index_f is the real NWS heat index, not the apparent-temperature proxy."""
    hours = [f"2026-06-20T{h:02d}:00" for h in range(0, 24)]
    monkeypatch.setattr(
        openmeteo,
        "_query",
        lambda lat, lon: _om_payload(hours, [0.0] * 24, [50.0] * 24, temp_f=90.0, rh=70.0),
    )
    bundle = IngestBundle()
    openmeteo.fetch(_mission("2026-06-20T08:00", "2026-06-20T18:00"), bundle)
    assert bundle.heat_index_f == pytest.approx(106.0, abs=1.5)
    assert bundle.apparent_temp_f == pytest.approx(75.0)  # cold/wet basis unchanged


# -- basin-wide alert check (FR-5 + FR-3) --------------------------------------------------


def test_flood_flags_classification() -> None:
    flags = nws.flood_flags_from_events(
        ["Flash Flood Warning", "Flood Advisory", "Coastal Flood Watch"]
    )
    assert flags["flash_flood_warning"] is True
    assert flags["flood_advisory"] is True
    assert flags["flood_watch"] is False  # coastal flooding is not an upstream hazard
    assert flags["flood_warning"] is False


def test_basin_sample_points_stay_inside_and_bounded() -> None:
    from shapely.geometry import Point

    poly = box(-111.5, 37.0, -109.0, 39.0)
    pts = nws._basin_sample_points(poly)
    assert 1 <= len(pts) <= 5
    assert all(poly.contains(Point(lon, lat)) for lat, lon in pts)


def test_basin_alert_flags_or_into_point_flags() -> None:
    """The ensemble branch's basin flags merge by OR — a warning over the upper basin
    must survive the point provider's False, and vice versa (raise-only)."""
    from upstreamwx.ingest.orchestrator import _merge_into

    dest = IngestBundle(flash_flood_warning=True)  # point check fired
    src = IngestBundle(flood_watch=True)  # basin check fired a different product
    _merge_into(dest, src)
    assert dest.flash_flood_warning is True  # not erased by src's False
    assert dest.flood_watch is True


# -- GEFS provider guards ---------------------------------------------------------------


def test_gefs_fhours_empty_beyond_product_horizon() -> None:
    """A window wholly beyond f240 returns no forecast hours — off-horizon data must not
    masquerade as the window's signal (previously sampled a clamped nearest hour)."""
    from upstreamwx.gefs.sources import GefsCycle

    cycle = GefsCycle(date="20260620", hour=0)
    start = datetime(2026, 7, 2, 8, tzinfo=UTC)  # ~f290
    end = datetime(2026, 7, 2, 18, tzinfo=UTC)
    assert _select_fhours(cycle, start, end) == []
    # Sanity: an in-horizon window still resolves.
    assert _select_fhours(
        cycle, datetime(2026, 6, 21, 8, tzinfo=UTC), datetime(2026, 6, 21, 18, tzinfo=UTC)
    )
    assert MAX_FHOUR_0P25 == 240


# -- REFS freshness gate ------------------------------------------------------------------


def test_refs_stale_cache_degrades_loudly(monkeypatch, tmp_path) -> None:
    """A newest warmed run older than the freshness bound must not serve as the
    authoritative same-day signal — stale REFS degrades to 'unavailable' with a note."""
    old = RefsCycle(date="20260618", hour=0)  # ~60 h before `now`
    monkeypatch.setattr(refs_provider, "cached_cycles", lambda now, settings: [old])
    bundle = IngestBundle()
    refs_provider.fetch(
        _mission("2026-06-20T14:00", "2026-06-20T18:00"),
        bundle,
        box(-111.5, 37.0, -109.0, 39.0),
        now=datetime(2026, 6, 20, 12, tzinfo=UTC),
        settings=Settings(data_dir=tmp_path),
    )
    assert bundle.sources_ok["refs"] is False
    assert bundle.refs_p_precip is None
    assert any("freshness bound" in n for n in bundle.notes)


# -- NWS split-chain degradation ---------------------------------------------------------


def test_nws_alert_flags_survive_afd_failure(monkeypatch) -> None:
    """A failed AFD chain must not discard successfully fetched alert flags (the
    authoritative flood/thunderstorm anchor)."""
    monkeypatch.setattr(nws, "active_alerts", lambda lat, lon: ["Flash Flood Warning"])

    def _boom(lat, lon):
        raise RuntimeError("AFD listing 500")

    monkeypatch.setattr(nws, "latest_afd", _boom)
    bundle = IngestBundle()
    nws.fetch(_mission("2026-06-20T08:00", "2026-06-20T18:00"), bundle)
    assert bundle.flash_flood_warning is True
    assert bundle.sources_ok["nws"] is True
    assert bundle.sources_ok["nws_afd"] is False
    assert to_hazard_inputs(bundle).nws_products_available is True


def test_nws_failed_alert_check_marks_products_unchecked(monkeypatch) -> None:
    def _boom(lat, lon):
        raise RuntimeError("alerts down")

    monkeypatch.setattr(nws, "active_alerts", _boom)
    monkeypatch.setattr(nws, "latest_afd", lambda lat, lon: "ISOLATED storms.")
    bundle = IngestBundle()
    nws.fetch(_mission("2026-06-20T08:00", "2026-06-20T18:00"), bundle)
    assert bundle.sources_ok["nws"] is False
    assert bundle.afd_storm_mode == "isolated"  # the surviving chain still contributes
    inputs = to_hazard_inputs(bundle)
    assert inputs.nws_products_available is False


# -- gap derivation (single source of truth for render + structured) ----------------------


def test_bundle_data_gaps_names_the_gaps() -> None:
    bundle = IngestBundle()
    bundle.sources_ok["nws"] = False
    gaps = bundle_data_gaps(bundle)
    joined = " | ".join(gaps)
    assert "flood ensemble signal unavailable" in joined
    assert "lightning ensemble signal unavailable" in joined
    assert "thermal forecast series unavailable" in joined
    assert "surface precip signal unavailable" in joined
    assert "NWS active-alert check unavailable" in joined


def test_healthy_bundle_has_no_gaps() -> None:
    bundle = IngestBundle(
        gefs_p_precip=30.0,
        gefs_p_tstm=20.0,
        heat_index_f=90.0,
        apparent_temp_f=70.0,
        measurable_precip=True,
        antecedent_precip_24_72h=False,
    )
    bundle.sources_ok.update({"nws": True, "nws_afd": True, "watershed": True})
    assert bundle_data_gaps(bundle) == []


# -- API cache token tracks data availability, not the wall clock -------------------------


def test_cycle_token_uses_fresh_cached_cycle(monkeypatch, tmp_path) -> None:
    from upstreamwx import gefs
    from upstreamwx.api.service import BriefingService
    from upstreamwx.gefs.sources import GefsCycle

    now = datetime(2026, 6, 20, 12, 10, tzinfo=UTC)
    monkeypatch.setenv("UPSTREAMWX_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        gefs, "cached_cycles", lambda **kw: [GefsCycle(date="20260620", hour=6)]
    )
    svc = BriefingService()
    # At 12:10Z the wall-clock boundary says T12Z, but the freshest available run is 06Z:
    # the token must track the data (a 06Z briefing must not be labeled 12Z-fresh).
    assert svc._cycle_token(now) == "2026-06-20T06Z"


def test_cycle_token_falls_back_to_live_probe_then_clock(monkeypatch, tmp_path) -> None:
    from upstreamwx import gefs
    from upstreamwx.api.service import BriefingService
    from upstreamwx.gefs.sources import GefsCycle

    now = datetime(2026, 6, 20, 12, 10, tzinfo=UTC)
    monkeypatch.setenv("UPSTREAMWX_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(gefs, "cached_cycles", lambda **kw: [])
    monkeypatch.setattr(
        gefs, "latest_available_cycle", lambda now=None: GefsCycle(date="20260620", hour=6)
    )
    svc = BriefingService()
    assert svc._cycle_token(now) == "2026-06-20T06Z"

    svc2 = BriefingService()
    monkeypatch.setattr(gefs, "latest_available_cycle", lambda now=None: None)
    # Feed dark: last-resort wall-clock fallback keeps caching functional (NFR-6).
    assert svc2._cycle_token(now) == "2026-06-20T12Z"
