"""A synthetic ERDDAP for the chunked engine: the info table, axis JSON and Parquet slabs built from each URL.

The grid is fixtures_engine's 1 deg `sst_deg1` (centres at x.5, latitude descending). A slab request is answered with
every day of its time constraint at 12:00Z over the cells of its lat/lon constraints, `value(day, lat, lon)` per cell
(default: the day of the year, so a day's mean is its day of the year). Every URL is logged. No network.
"""
import json
import math
import re
from datetime import date, datetime, timedelta, timezone

import pyarrow as pa
import pyarrow.parquet as pq

from fixtures_engine import BASE, INFO_URL
from fixtures_erddap import info_table
from om_extract.readers.erddap import ErddapError

_AXIS = re.compile(r"/griddap/sst_deg1\.json\?(longitude|latitude)%5B\(([^)]+)\):1:\(([^)]+)\)%5D$")
_SLAB = re.compile(r"/griddap/sst_deg1\.parquet\?(\w+)%5B\(([^)]+)\):1:\(([^)]+)\)%5D%5B\(([^)]+)\):1:\(([^)]+)\)%5D"
                   r"%5B\(([^)]+)\):1:\(([^)]+)\)%5D$")


def centres(a: float, b: float, lo: float, hi: float) -> list[float]:
  """The 1 deg centres from the one nearest `min(a, b)` to the one nearest `max(a, b)`, within [lo, hi]."""
  x0, x1 = sorted((a, b))
  c0 = min(max(math.floor(x0) + 0.5, lo), hi)
  c1 = min(max(math.floor(x1) + 0.5, lo), hi)
  return [c0 + i for i in range(int(round(c1 - c0)) + 1)]


def doy(d: str, lat: float, lon: float) -> float:
  return float(date.fromisoformat(d).timetuple().tm_yday)


class FakeErddap:
  """`fetch(url) -> bytes` for the synthetic grid; `slabs` lists each slab request as `t0..t1` (days) per lobe."""

  def __init__(self, start: str = "2026-01-01T12:00:00Z", end: str = "2026-04-10T12:00:00Z", value=doy,
               info_error: Exception | None = None):
    self.start, self.end, self.value, self.info_error = start, end, value, info_error
    self.log: list[str] = []
    self.slabs: list[str] = []

  def __call__(self, url: str) -> bytes:
    self.log.append(url)
    if url == INFO_URL:
      if self.info_error is not None:
        raise self.info_error
      return json.dumps(info_table(self.start, self.end)).encode()
    m = _AXIS.search(url)
    if m:
      axis, a, b = m.group(1), float(m.group(2)), float(m.group(3))
      if axis == "longitude":
        vals = centres(a, b, -179.5, 179.5)
      else:
        vals = centres(a, b, -89.5, 89.5)[::-1]       # latitude descends
      return json.dumps({"table": {"columnNames": [axis], "rows": [[v] for v in vals]}}).encode()
    m = _SLAB.search(url)
    if m:
      var, t0, t1, la1, la0, lo0, lo1 = m.groups()
      self.slabs.append(f"{t0[:10]}..{t1[:10]}")
      return self.slab(var, t0[:10], t1[:10], float(la1), float(la0), float(lo0), float(lo1))
    raise ErddapError(f"404 Not Found from ERDDAP: unexpected request {url}")

  def slab(self, var, d0, d1, la1, la0, lo0, lo1) -> bytes:
    days = []
    d = date.fromisoformat(d0)
    while d <= date.fromisoformat(d1):
      days.append(d.isoformat())
      d += timedelta(days=1)
    lats = centres(la0, la1, -89.5, 89.5)[::-1]
    lons = centres(lo0, lo1, -179.5, 179.5)
    rows = [(t, la, lo) for t in days for la in lats for lo in lons]
    tab = pa.table({
      "time": pa.array([datetime.fromisoformat(t + "T12:00:00+00:00") for t, _, _ in rows], pa.timestamp("ms", tz="UTC")),
      "latitude": pa.array([r[1] for r in rows], pa.float32()),
      "longitude": pa.array([r[2] for r in rows], pa.float32()),
      var: pa.array([self.value(t, la, lo) for t, la, lo in rows], pa.float64()),
    })
    sink = pa.BufferOutputStream()
    pq.write_table(tab, sink)
    return sink.getvalue().to_pybytes()


__all__ = ["BASE", "FakeErddap", "centres", "doy", "timezone"]
