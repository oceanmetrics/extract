"""The Copernicus Marine reader: the Toolbox `subset` of a dataset's surface layer for a place, summarised by the mean of
the cells whose centres fall inside the place polygon.

Ported from oceanmetrics/indicators' place producer (`om_indicators/producers/cmems_place.py` at indicators 7bcbd7d),
which is now a thin call into this module; its test fixture is ported with it (tests/fixtures_copernicus.py), and
indicators keeps a test that its output folder is byte for byte what it wrote before. The rules, each a test:

- **Windows** (`windows()`): the polygon's bbox grown by one grid cell and clamped to the dataset's range; a place
  across the antimeridian (stored split at +-180, parts touching both) gets one window per side, each from its own
  parts (parts by the sign of their centroid), because its plain bbox would be the whole globe.
- **Subset** (`toolbox_subset()`): the Copernicus Marine Toolbox, one window, one variable, the surface layer only
  (depth 0 to 0.51 m: the BGC multiyear dataset's first level is 0.506 m, the physics one 0.494 m), the whole record
  or `[start, end]`; `surface()` keeps the shallowest level of what comes back. The Toolbox finds the maintainer's
  login itself (`~/.copernicusmarine`); nothing here reads, prints or copies it.
- **Mean** (`cell_means()`): per time step the mean of the cells whose CENTRES fall inside the polygon (boundary
  included: `shapely.intersects_xy`), NaN skipped, and the sample SD (n - 1) of those cells; a cell seen in two windows
  counts once (the first window's). This is R `terra::extract(r, p, fun = mean, na.rm = TRUE)`, the method of
  noaa-onms/eco-indicators, so sanctuary and EEZ values share one method. It is NOT the grid rule of the ERDDAP and
  COG readers (partial-area weights): the Copernicus record is eco-indicators' and keeps its method. No centre inside,
  or every inside cell NaN at every step, raises `NoCellsError`.
- **Rows** (`place_rows()`): contract rows as eco-indicators publishes them: a month row is the monthly mean with
  `n = 1` and the cell SD in `sd`, none without a mean; a year row is the mean of its 12 monthly means (summed in month
  order), `n = 12`, `sd` null, written only when all 12 months have a row.
- **Provenance** (`build_provenance()`): `provenance.dataset` the Copernicus Marine product (id, title, DOI, citation
  "E.U. Copernicus Marine Service Information; <title>; <doi>"), `sources[]` the dataset's subset (`href`
  `copernicusmarine:<dataset id>`, its version in the title), the product page and the polygon's source.

The scientific stack (copernicusmarine, xarray, a NetCDF backend) is the `cmems` extra (`uv sync --extra cmems`), imported
inside the functions that need it; numpy and shapely are core dependencies.

`CopernicusReader` puts the Toolbox behind the `Reader` interface (`extent`, `axes`, `slab`, `slice`, `source_block`);
`extract_place()` is the one call a producer makes: windows, size check, subsets, the mean, the rows.
"""
from __future__ import annotations

import datetime as dt
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np
import pyarrow as pa

from om_extract.readers.base import Axes, BBox, Extent, SLAB_SCHEMA, SLICE_SCHEMA, TIME_TYPE

DEPTH_RANGE = (0.0, 0.51)           # the surface level only: 0.506 m in the BGC dataset, 0.494 m in the physics one
MAX_VALUES = 50_000_000             # grid values of one job (cells of the windows x time steps) before max_values=
EXTRACT_URL = "https://github.com/oceanmetrics/extract"
PRODUCER_NAME = "oceanmetrics/extract"
ADAPTER = "om_extract.readers.copernicus"
PRODUCT_URL = "https://data.marine.copernicus.eu/product/{product_id}/description"
LICENSE = "CC-BY-4.0"
METHOD = (
  "Monthly mean of the cells of a Copernicus Marine Service product whose centres fall inside the place polygon, at the "
  "surface level (0 to 0.51 m), computed with the Copernicus Marine Toolbox by om_extract.readers.copernicus (the "
  "method of noaa-onms/eco-indicators: terra::extract mean over the polygon, with the cell SD in sd). A year is the "
  "mean of its 12 monthly means, written only when all 12 months have a value.")

Window = tuple[float, float, float, float]
# a subset function: (dataset id, variable, window, start, end) -> DataArray (time, latitude, longitude) at the surface
Subset = Callable[[str, str, Window, "str | None", "str | None"], Any]


class NoCellsError(RuntimeError):
  """No grid cell centre inside the polygon, or none of them has a value: no rows are written."""


# ---- windows: where to ask the Toolbox ----

def windows(geom: Any, lon_step: float, lat_step: float, lon_range: tuple[float, float] = (-180.0, 180.0),
            lat_range: tuple[float, float] = (-90.0, 90.0)) -> list[Window]:
  """The `(xmin, ymin, xmax, ymax)` windows to subset: the polygon's bbox buffered by one grid cell and clamped to the
  dataset's own range. A place across the antimeridian is stored split at +-180 (parts touching both), whose plain
  bbox would be the whole globe: it gets one window per side (parts by the sign of their centroid), each from its own
  parts."""
  parts = list(geom.geoms) if hasattr(geom, "geoms") else [geom]
  edge = 1e-6
  across = any(p.bounds[2] >= 180 - edge for p in parts) and any(p.bounds[0] <= -180 + edge for p in parts)
  groups = [[p for p in parts if p.centroid.x >= 0], [p for p in parts if p.centroid.x < 0]] if across else [parts]
  out = []
  for g in groups:
    if not g:
      continue
    x0 = min(p.bounds[0] for p in g); y0 = min(p.bounds[1] for p in g)
    x1 = max(p.bounds[2] for p in g); y1 = max(p.bounds[3] for p in g)
    out.append((max(x0 - lon_step, lon_range[0]), max(y0 - lat_step, lat_range[0]),
                min(x1 + lon_step, lon_range[1]), min(y1 + lat_step, lat_range[1])))
  return out


def window_cells(window: Window, lon_step: float, lat_step: float) -> int:
  """Grid cells of a window (what the Toolbox returns), at least one per axis."""
  x0, y0, x1, y1 = window
  return max(1, int((x1 - x0) / lon_step + 1 + 1e-9)) * max(1, int((y1 - y0) / lat_step + 1 + 1e-9))


# ---- the mean over the cells under the polygon ----

@dataclass
class CellSeries:
  """Per time step of the subset: the cell-centre mean and sample SD, the cells with a value; `cells` is the cell
  centres inside the polygon (land cells included)."""
  times: list[dt.date]
  mean: list[float | None]
  sd: list[float | None]
  n_cells: list[int]
  cells: int


def cell_means(arrays: Sequence[Any], geom: Any) -> CellSeries:
  """Per time step the mean (and the sample SD) of the cells whose centres are inside `geom`, NaN skipped.

  `arrays` are xarray DataArrays with dims (time, latitude, longitude), one per window; a cell seen in two windows
  counts once. Boundary centres count as inside (a split place has cells centred exactly on -180). Raises
  `NoCellsError` when no centre is inside, or when every cell inside is NaN at every step."""
  import shapely
  shapely.prepare(geom)
  columns: dict[tuple[float, float], Any] = {}
  times = None
  for a in arrays:
    a = a.transpose("time", "latitude", "longitude")
    t = a["time"].values
    if times is None:
      times = t
    elif not np.array_equal(times, t):
      raise ValueError("the windows have different time steps")
    lat, lon = a["latitude"].values, a["longitude"].values
    lons, lats = np.meshgrid(lon, lat)
    inside = shapely.intersects_xy(geom, lons, lats)
    vals = np.asarray(a.values, dtype=float)
    for j, i in zip(*np.nonzero(inside)):
      columns.setdefault((round(float(lat[j]), 6), round(float(lon[i]), 6)), vals[:, j, i])
  if not columns:
    raise NoCellsError("no grid cell centre falls inside the place polygon: the place is too small for this grid")
  m = np.stack(list(columns.values()), axis=1)               # (time, cells)
  ok = np.isfinite(m)
  n = ok.sum(axis=1)
  if not n.any():
    raise NoCellsError(f"the {len(columns)} cell centres inside the place polygon have no value at any time step "
                       "(land or ice mask)")
  total = np.where(ok, m, 0.0).sum(axis=1)
  mean = np.where(n > 0, total / np.maximum(n, 1), np.nan)
  dev = np.where(ok, (m - mean[:, None]) ** 2, 0.0).sum(axis=1)
  sd = np.where(n > 1, np.sqrt(dev / np.maximum(n - 1, 1)), np.nan)
  days = times.astype("datetime64[D]")
  fin = lambda v: float(v) if np.isfinite(v) else None  # noqa: E731
  return CellSeries([d.item() for d in days], [fin(v) for v in mean], [fin(v) for v in sd], [int(k) for k in n],
                    len(columns))


# ---- rows ----

def _month_start(d: dt.date) -> dt.date:
  return dt.date(d.year, d.month, 1)


def month_rows(place_id: str, coll: str, indicator: str, unit: str, mean: dict[dt.date, float | None],
               sd: dict[dt.date, float | None]) -> list[dict]:
  """Month rows of one place: value = the monthly mean, n = 1, sd = the cell SD; no row without a mean."""
  return [{"place_id": place_id, "coll": coll, "indicator": indicator, "time_grain": "month", "time": m, "class": None,
           "value": v, "n": 1.0, "unit": unit, "sd": sd.get(m)}
          for m, v in sorted(mean.items()) if v is not None]


def year_rows(place_id: str, coll: str, indicator: str, unit: str, months: list[dict]) -> list[dict]:
  """Year rows: only years with all 12 month rows; value = the mean of the 12 monthly means (summed in month order),
  n = the sum of the months' n (12), sd null (an SD of monthly means is not the cell SD)."""
  by_year: dict[int, list[dict]] = {}
  for m in months:
    by_year.setdefault(m["time"].year, []).append(m)
  return [{"place_id": place_id, "coll": coll, "indicator": indicator, "time_grain": "year", "time": dt.date(y, 1, 1),
           "class": None, "value": sum(m["value"] for m in ms) / 12, "n": sum(m["n"] for m in ms), "unit": unit,
           "sd": None}
          for y, ms in sorted(by_year.items()) if len({m["time"].month for m in ms}) == 12]


def place_rows(place_id: str, coll: str, indicator: str, unit: str, series: CellSeries) -> list[dict]:
  """Month and year contract rows of one place from its cell series (the rules of eco-indicators: n = 1 and sd per
  month; a year is the mean of 12 months, only when complete)."""
  mean, sd = {}, {}
  for d, v, s in zip(series.times, series.mean, series.sd):
    m = _month_start(d)
    mean[m], sd[m] = v, s
  months = month_rows(place_id, coll, indicator, unit, mean, sd)
  return months + year_rows(place_id, coll, indicator, unit, months)


# ---- the Copernicus Marine Toolbox (network, injectable) ----

def info_from_catalogue(cat: Any, dataset: str, var: str, product_dois: dict[str, str] | None = None) -> dict:
  """What a run needs from `copernicusmarine.describe()`: product, DOI (the dataset's, else `product_dois` by product
  id), units, version, grid steps and ranges, and the number of time steps."""
  product = next((p for p in cat.products if any(d.dataset_id == dataset for d in p.datasets)), None)
  if product is None:
    raise ValueError(f"{dataset} is not in the Copernicus Marine catalogue")
  ds = next(d for d in product.datasets if d.dataset_id == dataset)
  for version in sorted(ds.versions, key=lambda v: v.label, reverse=True):
    for part in version.parts:
      for service in part.services:
        for v in service.variables:
          if v.short_name != var:
            continue
          c = {x.coordinate_id: x for x in v.coordinates}
          if "longitude" in c and "latitude" in c and c["longitude"].step and c["latitude"].step:
            t = c.get("time")
            doi = getattr(ds, "digital_object_identifier", None)
            return {"product_id": product.product_id, "product_title": product.title,
                    "doi": f"https://doi.org/{doi}" if doi else (product_dois or {}).get(product.product_id),
                    "units": v.units, "version": version.label,
                    "lon_step": float(c["longitude"].step), "lat_step": float(c["latitude"].step),
                    "lon_range": (float(c["longitude"].minimum_value), float(c["longitude"].maximum_value)),
                    "lat_range": (float(c["latitude"].minimum_value), float(c["latitude"].maximum_value)),
                    "n_times": len(t.values) if t is not None and t.values else None}
  raise ValueError(f"{var} of {dataset} has no gridded longitude/latitude description in the catalogue")


def dataset_info(dataset: str, var: str, product_dois: dict[str, str] | None = None) -> dict:
  """`info_from_catalogue()` of the live catalogue (no login needed)."""
  import copernicusmarine
  return info_from_catalogue(copernicusmarine.describe(dataset_id=dataset, disable_progress_bar=True), dataset, var,
                             product_dois)


def toolbox_version() -> str:
  import copernicusmarine
  return copernicusmarine.__version__


def surface(da: Any) -> Any:
  """The shallowest depth level of a variable (the surface layer); a variable without a depth axis is returned as is."""
  if "depth" in da.dims:
    da = da.isel(depth=int(np.argmin(da["depth"].values)))
  return da


def toolbox_subset(dataset: str, var: str, window: Window, start: str | None, end: str | None, workdir: Path,
                   name: str = "w0") -> Any:
  """One window of one variable through the Copernicus Marine Toolbox -> a loaded DataArray (time, latitude, longitude)
  at the surface level. The Toolbox finds the login itself; a failure is re-raised with the host's setup in the
  message."""
  import copernicusmarine
  import xarray as xr
  x0, y0, x1, y1 = window
  try:
    r = copernicusmarine.subset(
      dataset_id=dataset, variables=[var], minimum_longitude=x0, maximum_longitude=x1, minimum_latitude=y0,
      maximum_latitude=y1, minimum_depth=DEPTH_RANGE[0], maximum_depth=DEPTH_RANGE[1], start_datetime=start,
      end_datetime=end, output_directory=str(workdir), output_filename=f"{name}.nc", overwrite=True,
      disable_progress_bar=True)
  except Exception as e:  # noqa: BLE001 -- say where the login lives; the cause stays chained
    raise RuntimeError(f"Copernicus Marine subset of {dataset} failed ({type(e).__name__}: {e}); the Toolbox reads the "
                       "maintainer's login from ~/.copernicusmarine") from e
  with xr.open_dataset(r.file_path) as ds:
    return surface(ds[var]).load()


# ---- the reader ----

def _lattice(lo: float, hi: float, origin: float, step: float, top: float) -> np.ndarray:
  """The centres `origin + k * step` in `[lo, hi]` (and within the dataset's `[origin, top]`)."""
  k0 = max(int(np.ceil((lo - origin) / step - 1e-9)), 0)
  k1 = min(int(np.floor((hi - origin) / step + 1e-9)), int(np.floor((top - origin) / step + 1e-9)))
  return origin + np.arange(k0, k1 + 1) * step if k1 >= k0 else np.zeros(0)


class CopernicusReader:
  """One variable of a Copernicus Marine dataset behind the `Reader` interface.

  `info` is the catalogue record (`info_from_catalogue()`; default: asked of the live catalogue, no login needed);
  `subset(dataset, var, window, start, end)` serves the data (default: `toolbox_subset()` into a temporary directory,
  with the maintainer's login); `version` is the Toolbox version for the provenance (default: the installed one).
  Subsets are kept per (window, start, end), so `slab()` and `extract_place()` ask the Toolbox once per window.
  """
  kind = "copernicus"

  def __init__(self, dataset_id: str, variable: str, *, info: dict | Callable[[str, str], dict] | None = None,
               subset: Subset | None = None, version: str | None = None,
               product_dois: dict[str, str] | None = None):
    self.dataset_id, self.variable = dataset_id, variable
    self._info = info
    self._subset = subset
    self._version = version
    self._dois = product_dois
    self._cache: dict[tuple, Any] = {}
    self._tmp: tempfile.TemporaryDirectory | None = None

  @property
  def info(self) -> dict:
    if not isinstance(self._info, dict):
      self._info = (self._info or (lambda d, v: dataset_info(d, v, self._dois)))(self.dataset_id, self.variable)
    return self._info

  @property
  def version(self) -> str:
    if self._version is None:
      self._version = toolbox_version()
    return self._version

  def windows(self, geom: Any) -> list[Window]:
    """`windows()` on this dataset's grid."""
    i = self.info
    return windows(geom, i["lon_step"], i["lat_step"], i["lon_range"], i["lat_range"])

  def subset(self, window: Window, start: str | None = None, end: str | None = None, name: str = "w0") -> Any:
    """The surface DataArray of one window (asked once per window and time range)."""
    key = (tuple(window), start, end)
    if key not in self._cache:
      if self._subset is not None:
        self._cache[key] = self._subset(self.dataset_id, self.variable, window, start, end)
      else:
        if self._tmp is None:
          self._tmp = tempfile.TemporaryDirectory()
        self._cache[key] = toolbox_subset(self.dataset_id, self.variable, window, start, end, Path(self._tmp.name),
                                          name)
    return self._cache[key]

  def close(self) -> None:
    """Remove the Toolbox's files (the subsets are loaded into memory, so they stay readable)."""
    if self._tmp is not None:
      self._tmp.cleanup()
      self._tmp = None

  # the interface ----
  def extent(self) -> Extent:
    """The dataset's grid range as a bbox; the time range is the subset's (the catalogue gives only the number of
    steps), so `time_start`/`time_end` are None until a subset is read."""
    i = self.info
    return Extent(time_start=None, time_end=None, bbox=(i["lon_range"][0], i["lat_range"][0], i["lon_range"][1],
                                                        i["lat_range"][1]), depth_levels=(DEPTH_RANGE[0],),
                  n_values=None)

  def axes(self, bbox: BBox, depth: float | None = None) -> Axes:
    """The catalogue's lattice centres inside `bbox` (south to north, as the Toolbox returns them)."""
    i = self.info
    lat = _lattice(bbox[1], bbox[3], i["lat_range"][0], i["lat_step"], i["lat_range"][1])
    lon = _lattice(bbox[0], bbox[2], i["lon_range"][0], i["lon_step"], i["lon_range"][1])
    return Axes(lat=lat, lon=lon, step=(i["lat_step"], i["lon_step"]), lat_descending=False,
                lon_range=tuple(i["lon_range"]), depth=DEPTH_RANGE[0], source="copernicus")

  def slab(self, bbox: BBox, t0: str | None, t1: str | None, var: str | None = None) -> pa.Table:
    """The long table `time, lat, lon, value` of the subset over `bbox` (SLAB_SCHEMA), NaN as null."""
    a = self.subset(tuple(bbox), t0, t1).transpose("time", "latitude", "longitude")
    t = a["time"].values.astype("datetime64[us]")
    lat, lon = a["latitude"].values.astype(float), a["longitude"].values.astype(float)
    v = np.asarray(a.values, dtype=float)
    nt, ny, nx = v.shape
    time = pa.array(np.repeat(t, ny * nx), pa.timestamp("us")).cast(TIME_TYPE)
    return pa.table({"time": time, "lat": pa.array(np.tile(np.repeat(lat, nx), nt), pa.float64()),
                     "lon": pa.array(np.tile(lon, nt * ny), pa.float64()),
                     "value": pa.array(v.ravel(), pa.float64(), mask=~np.isfinite(v.ravel()))}, schema=SLAB_SCHEMA)

  def slice(self, bbox: BBox, t: str, var: str | None = None) -> pa.Table:
    """One time step of the subset as `lat, lon, value` (SLICE_SCHEMA)."""
    tab = self.slab(bbox, t, t, var)
    return pa.table({"lat": tab["lat"], "lon": tab["lon"], "value": tab["value"]}, schema=SLICE_SCHEMA)

  def source_block(self) -> dict:
    """The dataset, its version and product, the variable, the depth range and the Toolbox version, keyed
    `copernicus:*`."""
    i = self.info
    return {"copernicus:dataset_id": self.dataset_id, "copernicus:dataset_version": i.get("version"),
            "copernicus:product_id": i.get("product_id"), "copernicus:variable": self.variable,
            "copernicus:depth": list(DEPTH_RANGE), "copernicus:toolbox": self.version}


# ---- one place ----

@dataclass
class PlaceExtraction:
  """What `extract_place()` computed for one place: the windows and the job size, and (unless `check`) the cell
  series, the contract rows, the unit, the depth used and the time range."""
  place_id: str
  coll: str
  indicator: str
  windows: list[Window]
  grid_cells: int
  time_steps: int | None
  grid_values: int
  series: CellSeries | None = None
  rows: list[dict] = field(default_factory=list)
  unit: str | None = None
  depth: float | None = None


def indicator_id(dataset_id: str, variable: str) -> str:
  """The catalog's indicator id of a Copernicus Marine mean: `cmems:<dataset id>:<variable>:mean`."""
  return f"cmems:{dataset_id}:{variable}:mean"


def extract_place(reader: CopernicusReader, place_id: str, geom: Any, coll: str, *, indicator: str | None = None,
                  start: str | None = None, end: str | None = None, max_values: int = MAX_VALUES,
                  check: bool = False) -> PlaceExtraction:
  """One place: the windows on the reader's grid and the job size (refused over `max_values` before any download);
  with `check`, nothing more. Else each window's subset, `cell_means()`, the unit (the data's, else the catalogue's)
  and `place_rows()`. Raises `NoCellsError` when no month has a value, ValueError without a unit."""
  ind = indicator or indicator_id(reader.dataset_id, reader.variable)
  i = reader.info
  wins = reader.windows(geom)
  cells = sum(window_cells(w, i["lon_step"], i["lat_step"]) for w in wins)
  values = cells * (i.get("n_times") or 1)
  x = PlaceExtraction(place_id=place_id, coll=coll, indicator=ind, windows=wins, grid_cells=cells,
                      time_steps=i.get("n_times"), grid_values=values)
  if values > max_values:
    raise ValueError(f"about {values:,} grid values ({cells:,} cells x {i.get('n_times')} time steps) is over the "
                     f"{max_values:,} limit; raise --max-values if a maintainer really means it")
  if check:
    return x
  arrays = [reader.subset(w, start, end, f"w{k}") for k, w in enumerate(wins)]
  x.series = cell_means(arrays, geom)
  unit = (arrays[0].attrs.get("units") or i.get("units") or "").strip()
  if not unit:
    raise ValueError(f"{ind} has no units in the data or the catalogue")
  x.unit = unit
  x.rows = place_rows(place_id, coll, ind, unit, x.series)
  if not x.rows:
    raise NoCellsError("no month has a value inside the place polygon: no rows written")
  x.depth = next((float(a["depth"]) for a in arrays if "depth" in a.coords and a["depth"].ndim == 0), None)
  return x


# ---- provenance ----

def dataset_meta(info: dict, processor: dict | None = None) -> dict:
  """The Copernicus Marine product as `provenance.dataset`: the statement eco-indicators makes, so one indicator reads
  the same wherever its places were computed. `processor` is the provider that computed it (default oceanmetrics/extract)."""
  title, doi = info["product_title"], info.get("doi")
  return {
    "id": info["product_id"], "title": title, "via": PRODUCT_URL.format(product_id=info["product_id"]),
    "providers": [
      {"name": "Copernicus Marine Service (Mercator Ocean International)", "roles": ["producer", "licensor"],
       "url": "https://marine.copernicus.eu"},
      processor or {"name": PRODUCER_NAME, "roles": ["processor"], "url": EXTRACT_URL},
    ],
    "license": "other",
    "citation": f"E.U. Copernicus Marine Service Information; {title}; {doi}" if doi else f"E.U. Copernicus Marine Service Information; {title}",
    "doi": doi,
  }


def data_title(dataset: str, var: str, version: str | None, toolbox: str) -> str:
  """The title of the data source: the dataset id, its version, the variable and the Toolbox version."""
  return f"{dataset} {version} {var}, Copernicus Marine Toolbox {toolbox}" if version else \
    f"{dataset} {var}, Copernicus Marine Toolbox {toolbox}"


def build_provenance(x: PlaceExtraction, reader: CopernicusReader, polygon: dict, *, created: str,
                     producer: dict | None = None, code: dict | None = None, processor: dict | None = None,
                     method: str = METHOD, source_title: str | None = None, contract_version: str | None = None,
                     ) -> tuple[dict, dict]:
  """(the provenance embedded in the values Parquet, restricted to the platform schema's keys, and the richer
  `provenance.json` written beside it). `polygon` is `{"kind", "url"}` of the place geometry; `producer`, `code` and
  `processor` name who ran it (default om-extract); `source_title` replaces `data_title()` (the place producer keeps
  the title it published)."""
  from om_extract import __version__
  from om_extract.contract import CONTRACT_VERSION
  info, dataset, var = reader.info, reader.dataset_id, reader.variable
  first, last = min(x.series.times), max(x.series.times)
  embedded = {
    "producer": producer or {"name": PRODUCER_NAME, "url": EXTRACT_URL, "version": __version__},
    "sources": [
      # the data read is the dataset's subset through the Toolbox (it has no file or STAC item of its own), the product
      # page is where the dataset is described
      {"href": f"copernicusmarine:{dataset}",
       "title": source_title or data_title(dataset, var, info.get("version"), reader.version),
       "updated": None, "end_datetime": last.isoformat(), "checksum": None, "item": None, "role": "data"},
      {"href": PRODUCT_URL.format(product_id=info["product_id"]), "title": f"product {info['product_id']}",
       "updated": None, "end_datetime": None, "checksum": None, "item": None, "role": "product"},
      {"href": polygon["url"], "title": f"polygon of {x.place_id} ({polygon['kind']})", "updated": None,
       "end_datetime": None, "checksum": None, "item": None, "role": "geometry"},
    ],
    "source_version": f"{dataset} {info.get('version') or ''} {first.isoformat()} to {last.isoformat()}".replace("  ", " "),
    "method": method, "dataset": dataset_meta(info, processor),
    "code": code or {"name": "om-extract", "version": __version__, "adapter": ADAPTER},
    "license": LICENSE, "created": created,
  }
  sidecar = {
    **embedded, "contract": contract_version or CONTRACT_VERSION, "indicator": x.indicator, "coll": x.coll,
    "rows": len(x.rows), "place_id": x.place_id, "dataset_id": dataset, "dataset_version": info.get("version"),
    "variable": var, "unit": x.rows[0]["unit"], "product_doi": info.get("doi"),
    "depth": {"requested": list(DEPTH_RANGE), "used_m": x.depth},
    "windows": [list(w) for w in x.windows], "grid": {"lon_step": info["lon_step"], "lat_step": info["lat_step"]},
    "cells": x.series.cells, "time_range": [first.isoformat(), last.isoformat()],
    "polygon": polygon, "copernicusmarine_version": reader.version, "run_time": created,
  }
  return embedded, sidecar


# ---- `om-extract run --reader copernicus` ----

def to_shapely(geometry: Any) -> Any:
  """A place geometry as shapely: WKB bytes, a GeoJSON geometry, Feature or FeatureCollection (its features unioned),
  or a shapely geometry."""
  import shapely
  import shapely.geometry
  if isinstance(geometry, (bytes, bytearray, memoryview)):
    return shapely.from_wkb(bytes(geometry))
  if isinstance(geometry, dict):
    if geometry.get("type") == "FeatureCollection":
      parts = [shapely.geometry.shape(f["geometry"]) for f in geometry.get("features") or [] if f.get("geometry")]
      return parts[0] if len(parts) == 1 else shapely.union_all(parts)
    if geometry.get("type") == "Feature":
      return shapely.geometry.shape(geometry["geometry"])
    return shapely.geometry.shape(geometry)
  return geometry


def run(dataset_id: str, variable: str, place_id: str, geometry: Any, out: str | Path, *, coll: str,
        polygon: dict | None = None, start: str | None = None, end: str | None = None,
        reader: CopernicusReader | None = None, max_values: int = MAX_VALUES, created: str | None = None) -> dict:
  """One place x one Copernicus Marine variable to `<out>/values/indicator=<id>/coll=<slug>/data.parquet` (through
  `contract.write_values()`, then `contract.validate_file()`) and `<out>/provenance.json` (the sidecar). Raises
  `contract.ContractError` on a contract error. Returns `{"values", "provenance", "rows", "indicator", "coll",
  "cells", "windows"}`."""
  import json
  from om_extract import contract
  r = reader or CopernicusReader(dataset_id, variable)
  geom = to_shapely(geometry)
  x = extract_place(r, place_id, geom, coll, start=start, end=end, max_values=max_values)
  embedded, sidecar = build_provenance(x, r, polygon or {"kind": "file", "url": f"gazetteer:{coll}/{place_id}"},
                                       created=created or contract.utc_now())
  out = Path(out)
  path = contract.write_values(x.rows, out, embedded)
  errors = contract.validate_file(path)
  if errors:
    raise contract.ContractError(errors)
  prov = out / "provenance.json"
  prov.write_text(json.dumps(sidecar, indent=1, sort_keys=True) + "\n")
  return {"values": path, "provenance": prov, "rows": len(x.rows), "indicator": x.indicator, "coll": coll,
          "cells": x.series.cells, "windows": x.windows}
