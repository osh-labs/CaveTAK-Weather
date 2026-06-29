"""Open REFS GRIB2 subsets and pull labeled neighborhood-probability fields (HREF replacement).

The ``enspost`` ``prob`` product encodes Neighborhood Ensemble Probability as percent (0-100)
over the 15-member ensemble (idx tag ``0/14``). cfgrib decodes each message onto the native
~3 km CONUS Lambert grid with 2D ``latitude``/``longitude`` coords — the same shape HREF
produces, so the downstream polygon aggregation (:mod:`upstreamwx.grib.zonal`) is unchanged.

Like HREF (one file per forecast hour), a field is identified by ``(cycle, fhour, var, prob,
fcst-window)``. The accumulation-window descriptors are byte-compatible with HREF: at f12 the
1-hour bucket is ``"11-12 hour acc"``, the 3-hour is ``"9-12 hour acc"``, the 6-hour is
``"6-12 hour acc"`` and the run-total is ``"0-12 hour acc"``.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import xarray as xr

from ..grib.idx import download_subset, fetch_idx, select_messages
from .sources import DEFAULT_DOMAIN, DEFAULT_PRODUCT, RefsCycle


@dataclass
class RefsField:
    """A decoded REFS field over the native grid."""

    name: str
    threshold: str
    fhour: int
    data: xr.DataArray  # dims y/x (2D lat/lon coords); single forecast hour
    grib_path: Path
    descriptor_count: int
    extras: dict = field(default_factory=dict)


def accum_window(fhour: int, hours: int = 1) -> str:
    """``.idx`` fcst-window substring for an ``hours``-long accumulation ending at ``fhour``.

    Identical convention to HREF: ``accum_window(12, 1) -> "11-12 hour acc"`` (1-hourly QPF),
    ``accum_window(12, 3) -> "9-12 hour acc"``, ``accum_window(12, 6) -> "6-12 hour acc"``.
    """
    start = max(fhour - hours, 0)
    return f"{start}-{fhour} hour acc"


def open_subset(grib_path: str | Path) -> xr.Dataset:
    """Open a GRIB2 subset with cfgrib (no on-disk index written)."""
    return xr.open_dataset(
        grib_path,
        engine="cfgrib",
        backend_kwargs={"indexpath": ""},
    )


def _primary_dataarray(ds: xr.Dataset) -> xr.DataArray:
    """Return the single (largest) data variable from a homogeneous subset dataset."""
    names = list(ds.data_vars)
    if not names:
        raise ValueError("GRIB subset contains no data variables")
    names.sort(key=lambda n: ds[n].size, reverse=True)
    return ds[names[0]]


def load_probability_field(
    cycle: RefsCycle,
    fhour: int,
    var: str,
    prob: str,
    fcst: str | None = None,
    domain: str = DEFAULT_DOMAIN,
    product: str = DEFAULT_PRODUCT,
    cache_dir: str | Path | None = None,
) -> RefsField:
    """Fetch (via idx subset) and decode one neighborhood-probability field.

    Parameters mirror the ``.idx`` descriptor fields. ``prob`` selects the threshold (e.g.
    ``">12.7"`` for APCP mm, ``">40"`` for REFC dBZ, ``">0.08"`` for LTNG). ``fcst`` narrows to
    one accumulation window (see :func:`accum_window`); for instantaneous fields (REFC, LTNG,
    CAPE) it can be left ``None`` since an ``fHH`` file holds a single valid time.
    """
    idx = fetch_idx(cycle.idx_url(fhour, product=product, domain=domain))
    selected = select_messages(idx, var=var, prob=prob, fcst=fcst)
    if not selected:
        raise LookupError(
            f"No REFS messages match f{fhour:02d} var={var!r} prob={prob!r} fcst={fcst!r}"
        )

    out_dir = Path(cache_dir) if cache_dir else Path(tempfile.mkdtemp(prefix="refs_"))
    out_path = out_dir / (
        f"{cycle.date}_{cycle.hh}_f{fhour:02d}_{var}_{prob}.grib2".replace(" ", "")
        .replace(">", "gt")
        .replace("<", "lt")
    )
    download_subset(
        cycle.product_url(fhour, product=product, domain=domain), selected, out_path
    )

    ds = open_subset(out_path)
    da = _primary_dataarray(ds)
    return RefsField(
        name=var,
        threshold=prob,
        fhour=fhour,
        data=da,
        grib_path=out_path,
        descriptor_count=len(selected),
        extras={"domain": domain, "product": product, "fcst": fcst},
    )
