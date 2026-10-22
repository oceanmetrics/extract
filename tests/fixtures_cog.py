"""Synthetic fixtures for the COG reader: a 4 x 4 deg GeoTIFF of 1 deg pixels (lon 0..4, lat 0..4) and its STAC Item.

Pixel values, north row first (lat 3.5, 2.5, 1.5, 0.5), west to east (lon 0.5 .. 3.5); NODATA at lat 1.5, lon 2.5:

   1  2  3  4
   5  6  7  8
   9 10  .  12
  13 14 15 16
"""
import copy

import numpy as np

NODATA = -9999.0
VALUES = np.array([[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, NODATA, 12], [13, 14, 15, 16]], dtype=np.float32)
COG_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"


def write_tif(path, values=VALUES, *, crs="EPSG:4326", x0=0.0, y0=4.0, res=1.0, nodata=NODATA, bands=1):
  """A north-up GeoTIFF with its upper-left corner at (x0, y0); `bands` copies of `values` (band k adds 100 * (k-1))."""
  import rasterio
  from rasterio.transform import from_origin
  h, w = values.shape
  with rasterio.open(path, "w", driver="GTiff", height=h, width=w, count=bands, dtype=values.dtype, crs=crs,
                     transform=from_origin(x0, y0, res, res), nodata=nodata) as dst:
    for k in range(bands):
      v = values.copy()
      if k:
        v = np.where(values == nodata, nodata, values + 100 * k).astype(values.dtype)
      dst.write(v, k + 1)
  return str(path)


def item(href, *, crs="EPSG:4326", datetime="2020-06-01T00:00:00Z", start=None, end=None, bands=None, bidx=None,
         **props):
  """A STAC Item that passes the `cog` profile, its data asset `cog` at `href`."""
  bands = bands if bands is not None else [
    {"name": "sst", "data_type": "float32", "nodata": NODATA, "unit": "degree_C",
     "statistics": {"minimum": 1, "maximum": 16}}]
  asset = {"href": str(href), "type": COG_TYPE, "roles": ["data"], "raster:bands": copy.deepcopy(bands)}
  if bidx is not None:
    asset["atlas:bidx"] = bidx
  p = {"datetime": datetime, "proj:code": crs, "title": "synthetic 1 deg raster", **props}
  if start or end:
    p.update({"datetime": None, "start_datetime": start, "end_datetime": end})
  return {"type": "Feature", "stac_version": "1.1.0", "id": "synthetic_cog", "bbox": [0, 0, 4, 4],
          "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]]},
          "properties": p, "assets": {"cog": asset}, "links": []}
