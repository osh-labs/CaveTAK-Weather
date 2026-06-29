"""REFS/GEFS source-feed resolution and URL construction (NWS SCN 26-48).

The REFS feed (AWS prototype vs NOMADS para/prod) is configuration, not code: these tests pin
that ``refs_source`` and the raw overrides build the exact SCN 26-48 paths — production REFS is
``com/refs/prod/refs.YYYYMMDD/CC/ensprod/refs.tCCz.<type>.fFF.<dom>.grib2`` (note ``ensprod``,
not the AWS prototype's ``enspost``) — and that GEFS honors its base-URL override. Pure string
construction; no network.
"""

from __future__ import annotations

from upstreamwx.config import Settings
from upstreamwx.gefs.sources import GefsCycle, gefs_base
from upstreamwx.refs.sources import RefsCycle, refs_feed


def _refs_url(settings: Settings, fhour: int = 12) -> str:
    base, subdir = refs_feed(settings)
    return RefsCycle("20260901", 12).product_url(fhour, base=base, subdir=subdir)


def test_refs_default_is_aws_prototype():
    base, subdir = refs_feed(Settings())
    assert base == "https://noaa-rrfs-pds.s3.amazonaws.com/rrfs_a"
    assert subdir == "enspost"
    assert _refs_url(Settings()) == (
        "https://noaa-rrfs-pds.s3.amazonaws.com/rrfs_a/refs.20260901/12/enspost/"
        "refs.t12z.prob.f12.conus.grib2"
    )


def test_refs_nomads_prod_uses_ensprod():
    """The cutover target: NOMADS production path with the ensprod subdir (SCN 26-48)."""
    assert _refs_url(Settings(refs_source="nomads_prod")) == (
        "https://nomads.ncep.noaa.gov/pub/data/nccf/com/refs/prod/refs.20260901/12/ensprod/"
        "refs.t12z.prob.f12.conus.grib2"
    )


def test_refs_nomads_para_path():
    base, subdir = refs_feed(Settings(refs_source="nomads_para"))
    assert base == "https://nomads.ncep.noaa.gov/pub/data/nccf/com/refs/para"
    assert subdir == "ensprod"


def test_refs_raw_overrides_take_precedence():
    s = Settings(
        refs_source="nomads_prod", refs_base_url="https://example.test/refs", refs_subdir="x"
    )
    assert refs_feed(s) == ("https://example.test/refs", "x")
    assert _refs_url(s).startswith("https://example.test/refs/refs.20260901/12/x/")


def test_refs_idx_url_appends_idx():
    base, subdir = refs_feed(Settings(refs_source="nomads_prod"))
    url = RefsCycle("20260901", 0).idx_url(3, base=base, subdir=subdir)
    assert url.endswith("/ensprod/refs.t00z.prob.f03.conus.grib2.idx")


def test_gefs_default_is_operational_nomads():
    assert gefs_base(Settings()) == "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gens/prod"
    url = GefsCycle("20260901", 0).member_url("gec00", 24, base=gefs_base(Settings()))
    assert url == (
        "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gens/prod/gefs.20260901/00/atmos/"
        "pgrb2sp25/gec00.t00z.pgrb2s.0p25.f024"
    )


def test_gefs_base_url_override():
    s = Settings(gefs_base_url="https://example.test/gefs")
    assert gefs_base(s) == "https://example.test/gefs"
    assert GefsCycle("20260901", 0).atmos_dir(base=gefs_base(s)).startswith("https://example.test/gefs/")
