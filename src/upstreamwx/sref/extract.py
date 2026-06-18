"""Open SREF GRIB2 subsets and pull labeled probability/spread fields (Spike A).

The ensprod ``prob`` product encodes probabilities as percent (0-100) over the
ensemble (27 members, idx tag ``0/26``). cfgrib decodes each message onto the
native pgrb132 grid (697x553 Lambert) with 2D ``latitude``/``longitude`` coords.

A field is identified by a variable + threshold (e.g. ``APCP`` ``prob >12.7`` =
P(3-h precip > 0.5 in)). To keep cfgrib happy we select messages for a *single*
threshold (optionally across several forecast windows, which stack on ``step``).
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import xarray as xr

from .fetch import download_subset, fetch_idx, select_messages
from .sources import SrefCycle


@dataclass
class SrefField:
    """A decoded SREF field over the native grid."""

    name: str
    threshold: str
    data: xr.DataArray  # dims include y/x (and step if multi-window); 2D lat/lon coords
    grib_path: Path
    descriptor_count: int
    extras: dict = field(default_factory=dict)


def open_subset(grib_path: str | Path) -> xr.Dataset:
    """Open a GRIB2 subset with cfgrib (no on-disk index written)."""
    return xr.open_dataset(
        grib_path,
        engine="cfgrib",
        backend_kwargs={"indexpath": ""},
    )


def _primary_dataarray(ds: xr.Dataset) -> xr.DataArray:
    """Return the single data variable from a homogeneous subset dataset."""
    names = list(ds.data_vars)
    if not names:
        raise ValueError("GRIB subset contains no data variables")
    # Prefer the largest (real field) over scalar coords mistakenly promoted.
    names.sort(key=lambda n: ds[n].size, reverse=True)
    return ds[names[0]]


def load_probability_field(
    cycle: SrefCycle,
    var: str,
    prob: str,
    freq: str = "3hrly",
    grid: str = "pgrb132",
    fcst: str | None = None,
    cache_dir: str | Path | None = None,
) -> SrefField:
    """Fetch (via idx subset) and decode one probability field from a cycle.

    Parameters mirror the ``.idx`` descriptor fields. ``prob`` selects the
    threshold (e.g. ``">12.7"`` for APCP, ``">1000"`` for CAPE). ``fcst`` may
    narrow to one accumulation window; if ``None``, all matching windows are
    pulled and stacked on ``step``.
    """
    idx = fetch_idx(cycle.idx_url(product="prob", grid=grid, freq=freq))
    selected = select_messages(idx, var=var, prob=prob, fcst=fcst)
    if not selected:
        raise LookupError(f"No SREF messages match var={var!r} prob={prob!r} fcst={fcst!r}")

    out_dir = Path(cache_dir) if cache_dir else Path(tempfile.mkdtemp(prefix="sref_"))
    out_path = out_dir / f"{cycle.date}_{cycle.hh}_{var}_{prob}.grib2".replace(
        " ", ""
    ).replace(">", "gt").replace("<", "lt")
    download_subset(cycle.product_url(product="prob", grid=grid, freq=freq), selected, out_path)

    ds = open_subset(out_path)
    da = _primary_dataarray(ds)
    return SrefField(
        name=var,
        threshold=prob,
        data=da,
        grib_path=out_path,
        descriptor_count=len(selected),
        extras={"freq": freq, "grid": grid, "fcst": fcst},
    )
