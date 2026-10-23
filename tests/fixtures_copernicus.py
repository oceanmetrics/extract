"""Synthetic Copernicus Marine data for the reader's tests, ported from oceanmetrics/indicators' tests/cmems_fixture.py
(the place producer's fixture, indicators 7bcbd7d): small grids whose values name their cell, made-up polygons, a
stand-in catalogue entry. Nothing here touches the network, the Toolbox or a Copernicus login."""
import datetime as dt

import numpy as np
import pytest
import shapely

xr = pytest.importorskip("xarray")

DATASET = "cmems_mod_glo_bgc_my_0.25deg_P1M-m"
NO3 = f"cmems:{DATASET}:no3:mean"
INFO = {"product_id": "GLOBAL_MULTIYEAR_BGC_001_029", "product_title": "Global Ocean Biogeochemistry Hindcast",
        "doi": "https://doi.org/10.48670/moi-00019", "units": "mmol m-3", "version": "202406",
        "lon_step": 1.0, "lat_step": 1.0, "lon_range": (-180.0, 179.0), "lat_range": (-80.0, 90.0), "n_times": 3}


def months(n, year=2020):
  return [np.datetime64(f"{year + i // 12}-{i % 12 + 1:02d}-01T00:00:00") for i in range(n)]


def grid(n_months=3, lon=(0.0, 1.0, 2.0, 3.0), lat=(0.0, 1.0, 2.0, 3.0), depth=None, units="mmol m-3"):
  """A (time, latitude, longitude) DataArray whose value names its cell: 100 * month + 10 * lat index + lon index."""
  t = np.array(months(n_months), dtype="datetime64[ns]")
  v = np.array([[[100.0 * k + 10 * j + i for i in range(len(lon))] for j in range(len(lat))] for k in range(n_months)])
  da = xr.DataArray(v, dims=("time", "latitude", "longitude"), coords={"time": t, "latitude": list(lat), "longitude": list(lon)},
                    name="no3", attrs={"units": units})
  return da if depth is None else da.assign_coords(depth=depth)


def box(x0, y0, x1, y1):
  return shapely.box(x0, y0, x1, y1)


def date(y, m, d=1):
  return dt.date(y, m, d)
