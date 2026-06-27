"""Ingest tests: hermetic mapping + graceful degradation, and live smoke tests."""

from __future__ import annotations

from datetime import datetime

import pytest
from shapely.geometry import box

from upstreamwx.engine.models import ActivityType, Mission
from upstreamwx.ingest import gather, to_hazard_inputs
from upstreamwx.ingest.base import IngestBundle


def _mission(activity=ActivityType.CANYON) -> Mission:
    return Mission(
        activity_type=activity,
        lat=37.0192,
        lon=-111.9889,
        window_start=datetime(2026, 6, 20, 8),
        window_end=datetime(2026, 6, 20, 18),
        name="Buckskin",
    )


def test_to_hazard_inputs_maps_fields():
    bundle = IngestBundle(
        flash_flood_warning=True,
        sref_p_precip=55.0,
        sref_p_tstm=30.0,
        heat_index_f=98.0,
        apparent_temp_f=44.0,
        spc_category="slight",
        afd_convective_mention=True,
        member_support={"flash_flood": 0.55},
    )
    inputs = to_hazard_inputs(bundle, dry_party=True)
    assert inputs.flash_flood_warning is True
    assert inputs.sref_p_precip == 55.0
    assert inputs.heat_index_f == 98.0
    assert inputs.spc_category == "slight"
    assert inputs.dry_party is True
    assert inputs.member_support == {"flash_flood": 0.55}


def test_to_hazard_inputs_maps_flood_products_and_afd():
    bundle = IngestBundle(
        flood_warning=True,
        flood_advisory=True,
        flood_watch=True,
        afd_flood_mention=True,
    )
    inputs = to_hazard_inputs(bundle)
    assert inputs.flood_warning is True
    assert inputs.flood_advisory is True
    assert inputs.flood_watch is True
    assert inputs.afd_flood_mention is True


def test_nws_fetch_parses_flood_products_and_afd(monkeypatch):
    # Areal Flood Warning + Flash Flood Watch + an irrelevant Coastal Flood
    # Advisory; the AFD discusses excessive rainfall. The flash family must not
    # double-count as an areal Flood Warning, and coastal flooding must not fire.
    from upstreamwx.ingest import nws

    events = ["Flash Flood Watch", "Flood Warning", "Coastal Flood Advisory"]
    afd = "...HEAVY RAIN... Excessive rainfall and training thunderstorms likely."
    monkeypatch.setattr(nws, "active_alerts", lambda *a, **k: events)
    monkeypatch.setattr(nws, "latest_afd", lambda *a, **k: afd)

    bundle = IngestBundle()
    nws.fetch(_mission(), bundle)

    assert bundle.flash_flood_watch is True
    assert bundle.flash_flood_warning is False
    assert bundle.flood_warning is True       # areal Flood Warning matched
    assert bundle.flood_advisory is False     # Coastal Flood Advisory excluded
    assert bundle.flood_watch is False
    assert bundle.afd_flood_mention is True


def test_gather_merges_all_sources_deterministically(monkeypatch):
    # The point providers and the ensemble branch run concurrently on private bundles;
    # gather must merge every branch's disjoint contributions in a fixed, timing-independent
    # order (NFR-4). Stub each source to write distinct fields/notes.
    from upstreamwx.ingest import href_provider, nws, openmeteo, spc, sref_provider

    def nws_fetch(m, b):
        b.flash_flood_warning = True
        b.sources_ok["nws"] = True
        b.notes.append("nws ok")

    def om_fetch(m, b):
        b.heat_index_f = 95.0
        b.sources_ok["open_meteo"] = True
        b.notes.append("om ok")

    def spc_fetch(m, b):
        b.spc_category = "slight"
        b.sources_ok["spc"] = True
        b.notes.append("spc ok")

    def sref_fetch(m, b, poly, *, cycle=None):
        b.sref_p_precip = 40.0
        b.member_support["flash_flood"] = 0.4
        b.sources_ok["sref"] = True
        b.notes.append("sref ok")

    def href_fetch(m, b, poly, **k):
        b.href_p_precip = 60.0
        b.sources_ok["href"] = True
        b.notes.append("href ok")

    monkeypatch.setattr(nws, "fetch", nws_fetch)
    monkeypatch.setattr(openmeteo, "fetch", om_fetch)
    monkeypatch.setattr(spc, "fetch", spc_fetch)
    monkeypatch.setattr(sref_provider, "fetch", sref_fetch)
    monkeypatch.setattr(href_provider, "fetch", href_fetch)

    bundle = gather(_mission(), polygon=box(-112.0, 37.0, -111.9, 37.1))

    # Every branch's disjoint fields survived the concurrent merge.
    assert bundle.flash_flood_warning is True
    assert bundle.heat_index_f == 95.0
    assert bundle.spc_category == "slight"
    assert bundle.sref_p_precip == 40.0
    assert bundle.href_p_precip == 60.0
    assert bundle.member_support == {"flash_flood": 0.4}
    assert all(bundle.sources_ok[s] for s in ("nws", "open_meteo", "spc", "sref", "href"))
    # Notes from every source are present, point providers ahead of the ensemble branch.
    assert {"nws ok", "om ok", "spc ok", "sref ok", "href ok"} <= set(bundle.notes)
    assert bundle.notes.index("nws ok") < bundle.notes.index("sref ok")

    # Same inputs -> identical merged notes order regardless of thread timing (NFR-4).
    again = gather(_mission(), polygon=box(-112.0, 37.0, -111.9, 37.1))
    assert again.notes == bundle.notes


def test_nws_office_lookup_is_cached(monkeypatch):
    # The /points -> office resolution is static per point, so it is fetched once and reused;
    # this drops one of the AFD chain's three serial round-trips on every later call.
    from upstreamwx.ingest import nws

    nws._office_cache.clear()
    calls: list[str] = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "/points/" in url:
            return {"cwa": "SLC"}
        if "/products/types/AFD/locations/" in url:
            return {"@graph": [{"@id": "http://example/afd/latest"}]}
        return {"productText": "AFD discussion text"}

    monkeypatch.setattr(nws, "_get", fake_get)

    first = nws.latest_afd(37.0192, -111.9889)
    second = nws.latest_afd(37.0192, -111.9889)

    assert first == second == "AFD discussion text"
    # The point/office endpoint was hit once across both calls (cached); the listing/product
    # endpoints were not cached and ran each time.
    assert len([u for u in calls if "/points/" in u]) == 1


def test_gather_combines_concurrent_ensembles(monkeypatch):
    # SREF and HREF run concurrently on private bundles; gather merges member_support per-key
    # by max (the stronger ensemble wins, §16.5) and computes the SREF<->HREF agreement after
    # both complete (FR-17) — HREF no longer has to run after SREF to see its signal.
    from upstreamwx.ingest import href_provider, nws, openmeteo, spc, sref_provider

    monkeypatch.setattr(nws, "fetch", lambda m, b: None)
    monkeypatch.setattr(openmeteo, "fetch", lambda m, b: None)
    monkeypatch.setattr(spc, "fetch", lambda m, b: None)

    def sref_fetch(m, b, poly, *, cycle=None):
        b.sref_p_precip, b.sref_p_tstm = 70.0, 10.0
        b.member_support.update({"flash_flood": 0.70, "lightning": 0.10})
        b.sources_ok["sref"] = True

    def href_fetch(m, b, poly, **k):
        # HREF strongly diverges on precip (SREF strong, HREF near-absent) -> "partial".
        b.href_p_precip, b.href_p_lightning = 5.0, 8.0
        b.member_support.update({"flash_flood": 0.05, "lightning": 0.08})
        b.sources_ok["href"] = True

    monkeypatch.setattr(sref_provider, "fetch", sref_fetch)
    monkeypatch.setattr(href_provider, "fetch", href_fetch)

    bundle = gather(_mission(), polygon=box(-112.0, 37.0, -111.9, 37.1))

    # Per-key max across the two ensembles, independent of which finished first.
    assert bundle.member_support == {"flash_flood": 0.70, "lightning": 0.10}
    # Agreement computed from both signals once joined: SREF strong vs HREF absent on precip.
    assert bundle.source_agreement == "partial"


def test_gather_degrades_gracefully(monkeypatch):
    # All sources raise; gather must not raise and must flag the failures (NFR-6).
    from upstreamwx.ingest import nws, openmeteo, spc, sref_provider

    def boom(*a, **k):
        raise RuntimeError("down")

    for mod in (nws, openmeteo, spc, sref_provider):
        monkeypatch.setattr(mod, "fetch", boom)

    bundle = gather(_mission(), polygon=box(-112.0, 37.0, -111.9, 37.1))
    assert bundle.sources_ok.get("nws") is False
    assert bundle.sources_ok.get("sref") is False
    # NWS is mandatory: its failure is surfaced as a warning.
    assert any("mandatory source" in n for n in bundle.notes)
    # Engine still gets usable (empty) inputs rather than crashing.
    inputs = to_hazard_inputs(bundle)
    assert inputs.sref_p_precip is None


# --- Live smoke tests (services reachable in dev env; opt-in) -------------------


@pytest.mark.network
def test_nws_live():
    from upstreamwx.ingest import nws

    bundle = IngestBundle()
    nws.fetch(_mission(), bundle)
    assert bundle.sources_ok["nws"] is True
    assert isinstance(bundle.flash_flood_warning, bool)


@pytest.mark.network
def test_openmeteo_live():
    from upstreamwx.ingest import openmeteo

    bundle = IngestBundle()
    openmeteo.fetch(_mission(), bundle)
    assert bundle.sources_ok["open_meteo"] is True
    assert bundle.heat_index_f is not None


@pytest.mark.network
def test_spc_live():
    from upstreamwx.ingest import spc

    bundle = IngestBundle()
    spc.fetch(_mission(), bundle)
    assert bundle.sources_ok["spc"] is True
    # Category may legitimately be None (point outside any outlook polygon).


@pytest.mark.network
def test_sref_provider_live(fixtures_dir):
    import json

    from shapely.geometry import shape

    from upstreamwx.ingest import sref_provider

    geojson = json.loads((fixtures_dir / "buckskin_huc12.geojson").read_text())
    polygon = shape(geojson["features"][0]["geometry"])
    bundle = IngestBundle()
    sref_provider.fetch(_mission(), bundle, polygon)
    if bundle.sources_ok.get("sref"):
        assert 0.0 <= bundle.sref_p_precip <= 100.0


@pytest.mark.network
def test_watershed_cache_live(tmp_path):
    from upstreamwx.config import Settings
    from upstreamwx.watershed import resolve_and_trace_cached

    settings = Settings(data_dir=tmp_path)
    m = _mission()
    first = resolve_and_trace_cached(m.lat, m.lon, settings=settings)
    assert first.origin_huc12 == "140700070505"
    # Second call is served from the on-disk cache.
    cache_file = next((tmp_path / "watershed").glob("*.geojson"))
    assert cache_file.is_file()
    second = resolve_and_trace_cached(m.lat, m.lon, settings=settings)
    assert second.upstream_huc_ids == first.upstream_huc_ids
