"""Synthetic fixtures for the engine: a 1 deg erddap-grid collection, two square places, a 3-day slab served by URL.

The squares are the mask parity fixtures' (`tests/fixtures/mask/square.wkb.gz`, `square_am.wkb.gz`, synthetic): one
at 10.3..12.6 E, 20.2..21.7 N, and one cut at +-180 into a west part (-180..-178.4) and an east part (178.3..180).
On the 1 deg lattice (centres at x.5) the reader's axes snap each lobe bbox to the nearest centres, so every axis cell
of a lobe has its centre inside the square: 6 cells for the square, 4 + 4 for the cut one. Their weights are the
TypeScript's (`square_deg1.json.gz`, `square_am_deg1.json.gz`): the cell edges are the same on the wider axis it used.
"""
import copy
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from fixtures_erddap import info_table

MASK_DIR = Path(__file__).resolve().parent / "fixtures" / "mask"
BASE = "https://erddap.example.org/erddap"
DAYS = ["2026-01-01T12:00:00Z", "2026-01-02T12:00:00Z", "2026-01-03T12:00:00Z"]
INFO_URL = f"{BASE}/info/sst_deg1/index.json"
INFO = info_table("2000-01-01T12:00:00Z", "2026-09-30T12:00:00Z")

COLLECTION = {
  "type": "Collection",
  "stac_version": "1.1.0",
  "id": "erddap/sst_deg1",
  "title": "Daily 1 deg SST (synthetic)",
  "description": "A synthetic 1 degree daily grid for the engine tests.",
  "license": "CC-BY-4.0",
  "updated": "2026-10-01T00:00:00Z",
  "sci:citation": "Synthetic grid, Ocean Metrics tests (2026).",
  "providers": [{"name": "Example Ocean Agency", "roles": ["producer", "licensor"], "url": "https://example.org"},
                {"name": "Example ERDDAP", "roles": ["host"], "url": BASE, "description": "dropped by the provenance"}],
  "links": [{"rel": "via", "href": "https://example.org/sst_deg1.html", "type": "text/html"},
            {"rel": "cite-as", "href": "https://doi.org/10.0000/example.sst", "type": "text/html"}],
  "stac_extensions": ["https://stac-extensions.github.io/datacube/v2.2.0/schema.json"],
  "erddap:base_url": BASE,
  "erddap:dataset_id": "sst_deg1",
  "erddap:protocol": "griddap",
  "erddap:version": "2.29",
  "erddap:cors": True,
  "erddap:formats": ["parquet", "csvp", "json"],
  "erddap:lon_range": [-180, 180],
  "erddap:lat_descending": True,
  "cube:dimensions": {
    "time": {"type": "temporal", "extent": ["2000-01-01T12:00:00Z", None], "step": "P1D"},
    "latitude": {"type": "spatial", "axis": "y", "extent": [-89.5, 89.5], "step": 1},
    "longitude": {"type": "spatial", "axis": "x", "extent": [-179.5, 179.5], "step": 1},
  },
  "cube:variables": {
    "sst": {"dimensions": ["time", "latitude", "longitude"], "type": "data", "unit": "degree_C"},
    "sst_k": {"dimensions": ["time", "latitude", "longitude"], "type": "data", "unit": "kelvin"},
    "cls": {"dimensions": ["time", "latitude", "longitude"], "type": "data", "unit": "1",
            "erddap-places:categorical": True, "erddap-places:classes": {"0": "cool", "1": "warm"}},
  },
  "assets": {"griddap": {"href": BASE + "/griddap/sst_deg1.parquet?{variable}[({t0}):1:({t1})]",
                         "roles": ["data", "external"], "type": "application/vnd.apache.parquet"}},
}


def collection(**over) -> dict:
  """A copy of COLLECTION with top-level keys replaced (`__` stands for `:`)."""
  doc = copy.deepcopy(COLLECTION)
  for k, v in over.items():
    doc[k.replace("__", ":")] = v
  return doc


def wkb(name: str) -> bytes:
  """A mask fixture's place geometry as WKB (`square`, `square_am`)."""
  return gzip.open(MASK_DIR / f"{name}.wkb.gz").read()


def ts_cells(case: str) -> list[list[tuple[float, float, float]]]:
  """The TypeScript's mask cells of a parity fixture, per lobe, as (lat, lon, weight) sorted by lat then lon."""
  d = json.load(gzip.open(MASK_DIR / f"{case}.json.gz"))
  return [list(zip(lb["cells"]["lat"], lb["cells"]["lon"], lb["cells"]["weight"])) for lb in d["lobes"]]


def url(var: str, lat: tuple[float, float], lon: tuple[float, float], t0: str = DAYS[0], t1: str = DAYS[-1]) -> str:
  """The griddap URL the reader builds for one lobe and the 3-day window (latitude descending)."""
  def n(x):
    return str(int(x)) if float(x).is_integer() else repr(float(x))
  return (f"{BASE}/griddap/sst_deg1.parquet?{var}%5B({t0}):1:({t1})%5D%5B({n(lat[1])}):1:({n(lat[0])})%5D"
          f"%5B({n(lon[0])}):1:({n(lon[1])})%5D")


# the requests of each place, one per lobe (one chunk: 3 days of 4 or 6 cells is far under 100,000 values)
SQUARE_URL = url("sst", (20.5, 21.5), (10.5, 12.5))
SQUARE_AM_URLS = [url("sst", (20.5, 21.5), (-179.5, -178.5)), url("sst", (20.5, 21.5), (178.5, 179.5))]


def slab_bytes(var: str, lats: list[float], lons: list[float], value) -> bytes:
  """ERDDAP-shaped Parquet over DAYS x lats x lons (time-major, lat as given, lon ascending) with float32 axes;
  `value(day_index, lat, lon)` gives each cell (None for a missing value)."""
  rows = [(i, t, la, lo) for i, t in enumerate(DAYS) for la in lats for lo in lons]
  tab = pa.table({
    "time": pa.array([datetime.fromisoformat(t.replace("Z", "+00:00")) for _, t, _, _ in rows],
                     pa.timestamp("ms", tz="UTC")),
    "latitude": pa.array([r[2] for r in rows], pa.float32()),
    "longitude": pa.array([r[3] for r in rows], pa.float32()),
    var: pa.array([value(i, la, lo) for i, _, la, lo in rows], pa.float64()),
  })
  sink = pa.BufferOutputStream()
  pq.write_table(tab, sink)
  return sink.getvalue().to_pybytes()


# square values: day 1 is 1..6 over (20.5, 10.5), (20.5, 11.5), (20.5, 12.5), (21.5, 10.5), (21.5, 11.5), (21.5, 12.5);
# day 2 is day 1 + 10 with (21.5, 12.5) missing; day 3 is 8 everywhere
SQUARE_CELLS = [(20.5, 10.5), (20.5, 11.5), (20.5, 12.5), (21.5, 10.5), (21.5, 11.5), (21.5, 12.5)]


def square_value(i: int, lat: float, lon: float):
  k = SQUARE_CELLS.index((lat, lon)) + 1
  if i == 0:
    return float(k)
  if i == 1:
    return None if k == 6 else float(k + 10)
  return 8.0


# the cut square: west cells 1..4, east cells 5..8 (sorted by lat then lon within each lobe), + 10 a day
AM_CELLS = [(20.5, -179.5), (20.5, -178.5), (21.5, -179.5), (21.5, -178.5),
            (20.5, 178.5), (20.5, 179.5), (21.5, 178.5), (21.5, 179.5)]


def am_value(i: int, lat: float, lon: float) -> float:
  return float(AM_CELLS.index((lat, lon)) + 1 + 10 * i)


# square classes: day 1 class 0 on the south row and 1 on the north; day 2 all 1; day 3 class 1 only at (20.5, 11.5)
def square_class(i: int, lat: float, lon: float) -> float:
  if i == 0:
    return 0.0 if lat == 20.5 else 1.0
  if i == 1:
    return 1.0
  return 1.0 if (lat, lon) == (20.5, 11.5) else 0.0


def served(var: str = "sst", value=square_value, place: str = "square", info: bool = True) -> dict[str, bytes]:
  """URL -> bytes for one place: its slab request(s) and, with `info`, the info table (a live extent)."""
  out: dict[str, bytes] = {}
  if place == "square":
    out[url(var, (20.5, 21.5), (10.5, 12.5))] = slab_bytes(var, [21.5, 20.5], [10.5, 11.5, 12.5], value)
  else:
    out[url(var, (20.5, 21.5), (-179.5, -178.5))] = slab_bytes(var, [21.5, 20.5], [-179.5, -178.5], value)
    out[url(var, (20.5, 21.5), (178.5, 179.5))] = slab_bytes(var, [21.5, 20.5], [178.5, 179.5], value)
  if info:
    out[INFO_URL] = json.dumps(INFO).encode()
  return out


def fetch_from(pages: dict[str, bytes], log: list[str] | None = None):
  """A `fetch(url) -> bytes` over `pages`; any other URL is a test failure."""
  def fetch(u: str) -> bytes:
    if log is not None:
      log.append(u)
    if u not in pages:
      raise AssertionError(f"unexpected request: {u}")
    return pages[u]
  return fetch
