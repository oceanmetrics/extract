"""The COG reader: a STAC Item (`cog` profile) whose data asset is a Cloud-Optimized GeoTIFF, summarised on its own grid.

A raster is summarised on its native grid by the same rule as a griddap slab: `axes(bbox)` gives the pixel centres
the mask is computed on (`grid_mask()`: cells by centre, partial-area weights) and `slab()` the long table
`time, lat, lon, value` the stats SQL joins it with. The rules, each a test:

- **CRS**: EPSG:4326 only, in the item (`proj:code`) and in the file; anything else raises `UnsupportedCrs`. The
  EPSG:3857 copies the map draws are for display and are never summarised. Longitudes are in [-180, 180].
- **Pixels**: the native grid from the GeoTIFF's geotransform (no rotation); a pixel's centre is
  `x0 + (col + 0.5) * dx`, `y0 + (row + 0.5) * dy`. `axes(bbox)` holds every pixel whose footprint overlaps the bbox
  (for a bbox edge inside a pixel, that pixel: the cell ERDDAP snaps to) and one more pixel on each side where the
  raster has one, so every axis carries its true spacing (the mask derives cell edges from neighbouring centres, and
  a lone centre would get +-0.5 deg); latitudes in the raster's own order (north to south for a north-up raster).
  A bbox that misses the raster gives no pixels.
- **Band**: `var` is a band name from the data asset's `bands` (STAC 1.1) or `raster:bands` (raster 1.1), or a
  1-based band number; None is the asset's `atlas:bidx`, else band 1.
- **Values**: nodata (the file's, else the band's `nodata`: a number, `nan`, `inf`, `-inf`) and NaN are null; a band's
  `raster:scale` / `raster:offset` (or `scale` / `offset`) are applied (`value * scale + offset`).
- **Time**: a COG is one time step. Its time is the item's `datetime`, else `start_datetime`; its span is
  `[datetime, datetime]` or `[start_datetime, end_datetime]` (by UTC day). `slab(bbox, t0, t1)` returns that one step
  when the window overlaps the span and no rows when it does not. An item with no time at all is timeless: every
  window covers it and `time` is null.
- **Reads**: `rasterio.open` on the asset href: a local path, or an `http(s)` URL that GDAL reads by range requests
  (`/vsicurl/`); `fetch` is a rasterio `opener` (a callable giving a file-like object for a path) when the bytes come
  from elsewhere. A relative href resolves against `item_href`, else the item's `rel: self` link.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import numpy as np
import pyarrow as pa

from om_extract.readers.base import Axes, BBox, Extent, SLAB_SCHEMA, SLICE_SCHEMA, TIME_TYPE
from om_extract.readers.profile import check_profile

SUPPORTED_CRS = "EPSG:4326"
COG_TYPE = re.compile(r"^image/tiff; ?application=geotiff; ?profile=cloud-optimized$")


class UnsupportedCrs(ValueError):
  """The raster is not in EPSG:4326 (this version summarises 4326 rasters only; 3857 copies are for display)."""


def is_data_cog(asset: dict) -> bool:
  """An asset with roles including `data` and the COG media type."""
  return bool(COG_TYPE.match((asset or {}).get("type") or "")) and "data" in ((asset or {}).get("roles") or [])


def has_cog_asset(doc: dict) -> bool:
  """A STAC Item (`type: Feature`) with a data COG asset: what `readers.for_collection` hands to `CogReader`."""
  return doc.get("type") == "Feature" and any(is_data_cog(a) for a in (doc.get("assets") or {}).values())


def data_asset(item: dict, name: str = "cog") -> tuple[str, dict]:
  """`(key, asset)`: the asset `name` when it is a data COG, else the first data COG asset; raises when there is none."""
  assets = item.get("assets") or {}
  if is_data_cog(assets.get(name)):
    return name, assets[name]
  for k, a in assets.items():
    if is_data_cog(a):
      return k, a
  raise ValueError(f"item {item.get('id')!r} has no data COG asset (assets: {sorted(assets)})")


def bands_of(asset: dict) -> list[dict]:
  """The bands of a data asset: `bands` (STAC 1.1 core), else `raster:bands` (raster extension 1.1)."""
  return list(asset.get("bands") or asset.get("raster:bands") or [])


def band_of(asset: dict, var: str | int | None) -> tuple[int, dict]:
  """`(bidx, band)`, the 1-based band number and its STAC entry, for a band name, a band number, or None (the asset's
  `atlas:bidx`, else 1). Raises ValueError naming the bands when `var` matches none."""
  bands = bands_of(asset)
  if var is None:
    i = int(asset.get("atlas:bidx") or 1)
  elif isinstance(var, int) or (isinstance(var, str) and var.isdigit()):
    i = int(var)
  else:
    names = [b.get("name") for b in bands]
    if var not in names:
      raise ValueError(f"{var!r} is not a band of the COG; bands: {names}")
    i = names.index(var) + 1
  if not 1 <= i <= max(len(bands), 1):
    raise ValueError(f"band {i} is out of range: the COG lists {len(bands)} band(s)")
  return i, (bands[i - 1] if bands else {})


def nodata_of(v: Any) -> float | None:
  """A STAC nodata (`nan`, `inf`, `-inf`, a number or null) as a float, or None."""
  if v is None:
    return None
  if isinstance(v, str):
    return {"nan": math.nan, "inf": math.inf, "-inf": -math.inf}[v]
  return float(v)


def _day(iso: str) -> str:
  return iso[:10]


def _ts(iso: str | None) -> datetime | None:
  return None if iso is None else datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


def item_times(item: dict) -> tuple[str | None, str | None, str | None]:
  """`(start, end, time)` of an item: `[datetime, datetime]` and `datetime`, else `[start_datetime, end_datetime]`
  and `start_datetime`; all None for a timeless item."""
  p = item.get("properties") or {}
  if p.get("datetime"):
    return p["datetime"], p["datetime"], p["datetime"]
  s, e = p.get("start_datetime"), p.get("end_datetime")
  return s, e or s, s


def resolve_href(href: str, item: dict, item_href: str | Path | None = None) -> str:
  """An asset href made absolute: a URL or absolute path as is; a relative one against `item_href` (where the item
  was read), else the item's `rel: self` link; else as given (relative to the working directory)."""
  if "://" in href or href.startswith("/"):
    return href
  base = str(item_href) if item_href else next((lk["href"] for lk in item.get("links") or []
                                                if lk.get("rel") == "self" and lk.get("href")), None)
  if not base:
    return href
  if "://" in base:
    return urljoin(base, href)
  return str((Path(base).parent / href).resolve())


def _span(lo: float, hi: float, origin: float, step: float, n: int) -> tuple[int, int] | None:
  """The index range `[i0, i1]` of the pixels whose footprints overlap `[lo, hi]` along one axis (`origin` is the
  outer edge of pixel 0, `step` signed), or None when they miss the raster."""
  a, b = sorted(((lo - origin) / step, (hi - origin) / step))
  i0, i1 = max(math.floor(a), 0), min(math.ceil(b) - 1, n - 1)
  if b == a:  # a degenerate bbox (a point or a line) is the pixel holding it
    i0 = i1 = math.floor(a)
  return (i0, i1) if 0 <= i0 <= i1 <= n - 1 else None


class CogReader:
  """The reader for a STAC Item with a data COG (the `cog` profile), validated first (`InvalidProfile`).

  `asset` names the data asset (default `cog`; the first data COG otherwise). `fetch` is a rasterio opener for bytes
  that are not a path or a URL GDAL reads itself. `item_href` is where the item was read, to resolve a relative href.
  """
  kind = "cog"

  def __init__(self, item: dict, *, asset: str = "cog", fetch: Any = None, item_href: str | Path | None = None,
               validate: bool = True):
    if validate:
      check_profile("cog", item)
    crs = (item.get("properties") or {}).get("proj:code")
    if crs != SUPPORTED_CRS:
      raise UnsupportedCrs(f"item {item.get('id')!r} is in {crs}: the COG reader summarises {SUPPORTED_CRS} rasters "
                           "only (EPSG:3857 copies are for display; publish or point at the 4326 original)")
    self.item = item
    self.asset_key, self.asset = data_asset(item, asset)
    self.href = resolve_href(self.asset["href"], item, item_href)
    self._fetch = fetch
    self._grid: dict | None = None
    self._bidx, _ = band_of(self.asset, None)

  # the file ----
  def _open(self):
    import rasterio
    return rasterio.open(self.href, opener=self._fetch) if self._fetch else rasterio.open(self.href)

  def grid(self) -> dict:
    """The native grid: `x0`, `y0` (outer corner of pixel 0), `dx`, `dy` (signed), `width`, `height`, `count`,
    `nodata` per band (the file's). Opened once; raises `UnsupportedCrs` for a file not in EPSG:4326."""
    if self._grid is None:
      with self._open() as src:
        epsg = src.crs.to_epsg() if src.crs else None
        if epsg != 4326:
          raise UnsupportedCrs(f"{self.href} is in {src.crs.to_string() if src.crs else 'no CRS'}: the COG reader "
                               f"summarises {SUPPORTED_CRS} rasters only (EPSG:3857 copies are for display)")
        t = src.transform
        if t.b != 0 or t.d != 0:
          raise ValueError(f"{self.href} has a rotated geotransform; only north-up grids are read")
        self._grid = {"x0": t.c, "y0": t.f, "dx": t.a, "dy": t.e, "width": src.width, "height": src.height,
                      "count": src.count, "nodata": list(src.nodatavals)}
      g = self._grid
      xs = (g["x0"], g["x0"] + g["width"] * g["dx"])
      if min(xs) < -180 - 1e-9 or max(xs) > 180 + 1e-9:
        raise ValueError(f"{self.href} spans longitudes {min(xs)} to {max(xs)}: only [-180, 180] rasters are read")
    return self._grid

  def _window(self, bbox: BBox) -> tuple[tuple[int, int], tuple[int, int]] | None:
    g = self.grid()
    cols = _span(bbox[0], bbox[2], g["x0"], g["dx"], g["width"])
    rows = _span(bbox[1], bbox[3], g["y0"], g["dy"], g["height"])
    if not (rows and cols):
      return None
    def pad(r, n):
      return (max(r[0] - 1, 0), min(r[1] + 1, n - 1))
    return pad(rows, g["height"]), pad(cols, g["width"])

  # the interface ----
  def extent(self) -> Extent:
    """The item's time span (`item_times`) and bbox (the item's, else the raster's bounds); no depth levels."""
    s, e, _ = item_times(self.item)
    bb = self.item.get("bbox")
    if bb and len(bb) >= 4:
      bbox = tuple(float(v) for v in bb[:4])
    else:
      g = self.grid()
      xs = sorted((g["x0"], g["x0"] + g["width"] * g["dx"]))
      ys = sorted((g["y0"], g["y0"] + g["height"] * g["dy"]))
      bbox = (xs[0], ys[0], xs[1], ys[1])
    return Extent(time_start=s, time_end=e, bbox=bbox)

  def axes(self, bbox: BBox, depth: float | None = None) -> Axes:
    """The pixel centres of the native grid over `bbox`, one pixel of margin on each side (empty arrays when the bbox
    misses the raster)."""
    g = self.grid()
    w = self._window(bbox)
    if w is None:
      lat, lon = np.zeros(0), np.zeros(0)
    else:
      (r0, r1), (c0, c1) = w
      lat = g["y0"] + (np.arange(r0, r1 + 1) + 0.5) * g["dy"]
      lon = g["x0"] + (np.arange(c0, c1 + 1) + 0.5) * g["dx"]
    return Axes(lat=lat, lon=lon, step=(abs(g["dy"]), abs(g["dx"])), lat_descending=g["dy"] < 0,
                lon_range=(-180, 180), source="cog")

  def covers(self, t0: str | None, t1: str | None) -> bool:
    """Whether the window `[t0, t1]` (by UTC day) overlaps the item's span; a timeless item is in every window."""
    s, e, _ = item_times(self.item)
    if s is None or t0 is None or t1 is None:
      return True
    return _day(t0) <= _day(e) and _day(s) <= _day(t1)

  def _read(self, bbox: BBox, var: str | int | None) -> pa.Table:
    """The pixels of one band over `bbox` as `lat, lon, value` (lat in raster order, then lon), nodata as null."""
    bidx, band = band_of(self.asset, var)
    self._bidx = bidx
    ax = self.axes(bbox)
    if ax.n_cells == 0:
      return SLICE_SCHEMA.empty_table()
    from rasterio.windows import Window
    (r0, r1), (c0, c1) = self._window(bbox)
    with self._open() as src:
      v = src.read(bidx, window=Window(c0, r0, c1 - c0 + 1, r1 - r0 + 1)).astype(np.float64)
    nod = self.grid()["nodata"][bidx - 1]
    if nod is None:
      nod = nodata_of(band.get("nodata"))
    bad = np.isnan(v)
    if nod is not None and not math.isnan(nod):
      bad |= v == nod
    scale = band.get("raster:scale", band.get("scale"))
    offset = band.get("raster:offset", band.get("offset"))
    if scale is not None:
      v = v * float(scale)
    if offset is not None:
      v = v + float(offset)
    lat = np.repeat(ax.lat, len(ax.lon))
    lon = np.tile(ax.lon, len(ax.lat))
    vals = pa.array(v.ravel(), pa.float64(), mask=bad.ravel())
    return pa.table({"lat": pa.array(lat, pa.float64()), "lon": pa.array(lon, pa.float64()), "value": vals},
                    schema=SLICE_SCHEMA)

  def slice(self, bbox: BBox, t: str | None, var: str | int | None) -> pa.Table:
    """The pixels over `bbox` as `lat, lon, value` when the item covers day `t` (or `t` is None), else no rows."""
    if not self.covers(t, t):
      return SLICE_SCHEMA.empty_table()
    return self._read(bbox, var)

  def slab(self, bbox: BBox, t0: str, t1: str, var: str | int | None) -> pa.Table:
    """The long table `time, lat, lon, value` (SLAB_SCHEMA): the item's one time step when `[t0, t1]` overlaps its
    span, no rows otherwise. `time` is the item's time (null for a timeless item)."""
    if not self.covers(t0, t1):
      return SLAB_SCHEMA.empty_table()
    tab = self._read(bbox, var)
    when = _ts(item_times(self.item)[2])
    time = pa.array([when] * tab.num_rows, TIME_TYPE)
    return pa.table({"time": time, "lat": tab["lat"], "lon": tab["lon"], "value": tab["value"]}, schema=SLAB_SCHEMA)

  def source_block(self, var: str | int | None = None) -> dict:
    """The asset href, band (number and name), nodata and `proj:code`, keyed `cog:*` (the band last read, or `var`)."""
    bidx, band = band_of(self.asset, var) if var is not None else (self._bidx, band_of(self.asset, self._bidx)[1])
    return {"cog:href": self.href, "cog:item": self.item.get("id"), "cog:asset": self.asset_key, "cog:bidx": bidx,
            "cog:band": band.get("name"), "cog:nodata": band.get("nodata"),
            "proj:code": (self.item.get("properties") or {}).get("proj:code")}

  @property
  def dataset(self):
    """The item as the engine's dataset record (`layers.Dataset`): id and dataset id the item id, the href as the
    base URL, protocol `cog`, one variable per band."""
    from om_extract.layers import CubeVariable, Dataset
    p = self.item.get("properties") or {}
    s, e, _ = item_times(self.item)
    variables = [CubeVariable(name=b.get("name") or str(i + 1), unit=b.get("unit"),
                              description=b.get("description") or b.get("name") or str(i + 1), categorical=False)
                 for i, b in enumerate(bands_of(self.asset))]
    return Dataset(id=self.item["id"], title=p.get("title") or self.item["id"], base_url=self.href,
                   dataset_id=self.item["id"], protocol="cog", long_format=None, version=None, cors=True,
                   formats=["cog"], lon_range=(-180, 180), lat_descending=self.grid()["dy"] < 0, variables=variables, time_step=None,
                   time_extent=[s, e], depth=None, status=None, format="cog")
