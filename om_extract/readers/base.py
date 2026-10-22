"""The reader interface: one per source, the same four reads and one provenance block.

A reader turns a collection (a STAC document with a source profile such as `erddap:*`) into the long tables the
engine summarises. The engine never sees the source: it asks `extent()`, `axes(bbox, depth)`, `slab(bbox, t0, t1, var)`
and `slice(bbox, t, var)`, and records `source_block()` as the provenance of the values.
"""
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np
import pyarrow as pa

# [lon_min, lat_min, lon_max, lat_max] in degrees, longitudes in [-180, 180]; a lobe never crosses the antimeridian
BBox = tuple[float, float, float, float]

TIME_TYPE = pa.timestamp("us", tz="UTC")
# the long table erddap-places shapes: one row per cell and time step
SLAB_SCHEMA = pa.schema([("time", TIME_TYPE), ("lat", pa.float64()), ("lon", pa.float64()), ("value", pa.float64())])
# one time step: the same without the time column
SLICE_SCHEMA = pa.schema([("lat", pa.float64()), ("lon", pa.float64()), ("value", pa.float64())])


@dataclass(frozen=True)
class Extent:
  """What a source covers.

  `time_start`/`time_end` are ISO instants (`time_end` is None for an open-ended source whose end is unknown);
  `bbox` is `(lon_min, lat_min, lon_max, lat_max)` or None; `depth_levels` the levels a depth axis offers (empty when
  there is none). `step_days` is the average time step in days (None when irregular or unknown). `live` is True
  when the end was asked of the server rather than read from the (possibly stale) catalog.
  """
  time_start: str | None
  time_end: str | None
  bbox: BBox | None = None
  depth_levels: tuple[float, ...] = ()
  step_days: float | None = None
  step_label: str | None = None
  n_values: int | None = None
  live: bool = False


@dataclass(frozen=True, eq=False)
class Axes:
  """The cell centres a request over `bbox` covers, in the server's own order.

  `lat` runs high to low when `lat_descending`; `lon` is in the source's own frame (`lon_range` says whether that is
  [-180, 180] or [0, 360]). `step` is `(lat_step, lon_step)` in degrees, None where the axis is irregular. `depth` is
  the single level a grid with a depth axis is sliced at. `source` says where the centres came from: `cube`
  (the catalog's datacube lattice, within the rounding of its step) or `live` (the server's own axis vectors).
  """
  lat: np.ndarray
  lon: np.ndarray
  step: tuple[float | None, float | None]
  lat_descending: bool
  lon_range: tuple[float, float]
  depth: float | None = None
  source: str = "cube"

  @property
  def n_cells(self) -> int:
    """Cells per time step."""
    return len(self.lat) * len(self.lon)


@runtime_checkable
class Reader(Protocol):
  """The interface every source implements. `kind` names the source (`erddap`, later `cog`, `h3`, `zarr`)."""

  kind: str

  def extent(self) -> Extent:
    """Time range (the live end where the source can say), bbox and depth levels."""
    ...

  def axes(self, bbox: BBox, depth: float | None = None) -> Axes:
    """Cell centres over `bbox` (and the depth level), with step, latitude direction and longitude range."""
    ...

  def slab(self, bbox: BBox, t0: str, t1: str, var: str) -> pa.Table:
    """The long table `time, lat, lon, value` for `var` over `bbox` and `[t0, t1]` (SLAB_SCHEMA)."""
    ...

  def slice(self, bbox: BBox, t: str, var: str) -> pa.Table:
    """One time step as `lat, lon, value` (SLICE_SCHEMA)."""
    ...

  def source_block(self) -> dict:
    """The profile keys (`erddap:*`, `cog:*`, ...) that identify the source, for the provenance of the values."""
    ...
