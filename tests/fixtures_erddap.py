"""Synthetic ERDDAP fixtures: a collection, an info table and a Parquet slab. No network, no copied data."""
import copy
from datetime import datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq

BASE = "https://pae-paha.pacioos.hawaii.edu/erddap"

# a dhw_5km-like grid: latitude descends, 0.05 degree lattice on -179.975..179.975, one daily variable
GRID = {
  "type": "Collection",
  "stac_version": "1.1.0",
  "id": "erddap/dhw_5km",
  "title": "Daily 5km SST (synthetic)",
  "stac_extensions": ["https://stac-extensions.github.io/datacube/v2.2.0/schema.json"],
  "erddap:base_url": BASE,
  "erddap:dataset_id": "dhw_5km",
  "erddap:protocol": "griddap",
  "erddap:version": "2.29",
  "erddap:cors": True,
  "erddap:formats": ["parquet", "csvp", "json"],
  "erddap:lon_range": [-180, 180],
  "erddap:lat_descending": True,
  "cube:dimensions": {
    "time": {"type": "temporal", "extent": ["1985-04-01T12:00:00Z", None], "step": "P1D"},
    "latitude": {"type": "spatial", "axis": "y", "extent": [-89.975, 89.975], "step": 0.05},
    "longitude": {"type": "spatial", "axis": "x", "extent": [-179.975, 179.975], "step": 0.05},
  },
  "cube:variables": {
    "CRW_SST": {"dimensions": ["time", "latitude", "longitude"], "type": "data", "unit": "Celsius"},
    "CRW_BAA": {"dimensions": ["time", "latitude", "longitude"], "type": "data", "unit": "1",
                "erddap-places:categorical": True, "erddap-places:classes": {"0": "No Stress", "1": "Bleaching Watch"}},
  },
  "assets": {"griddap": {"href": BASE + "/griddap/dhw_5km.parquet?{variable}[({t0}):1:({t1})]", "roles": ["data", "external"],
                          "type": "application/vnd.apache.parquet"}},
}


def grid(**over) -> dict:
  """A copy of GRID with top-level keys replaced (`__` stands for `:` in a keyword); a value of None removes the key."""
  doc = copy.deepcopy(GRID)
  for k, v in over.items():
    k = k.replace("__", ":")
    if v is None:
      doc.pop(k, None)
    else:
      doc[k] = v
  return doc


def secs(iso: str) -> float:
  return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def info_table(start: str, end: str, spacing: str = "1 day", nvalues: int = 15000) -> dict:
  """An `info/<id>/index.json` table with a time axis only."""
  rows = [
    ["attribute", "NC_GLOBAL", "time_coverage_end", "String", end],
    ["attribute", "NC_GLOBAL", "time_coverage_start", "String", start],
    ["dimension", "time", "", "double", f"nValues={nvalues}, evenlySpaced=true, averageSpacing={spacing}"],
    ["attribute", "time", "actual_range", "double", f"{secs(start)}, {secs(end)}"],
  ]
  return {"table": {"columnNames": ["Row Type", "Variable Name", "Attribute Name", "Data Type", "Value"], "rows": rows}}


def slab_parquet(times: list[str], lats: list[float], lons: list[float], var: str, values: list[float] | None = None) -> bytes:
  """ERDDAP-shaped Parquet: columns time (timestamp), latitude, longitude, var; rows time-major, then lat, then lon."""
  rows = [(t, la, lo) for t in times for la in lats for lo in lons]
  vals = values if values is not None else [float(i) for i in range(len(rows))]
  tab = pa.table({
    "time": pa.array([datetime.fromisoformat(t.replace("Z", "+00:00")) for t, _, _ in rows], pa.timestamp("ms", tz="UTC")),
    "latitude": [r[1] for r in rows],
    "longitude": [r[2] for r in rows],
    var: vals,
  })
  sink = pa.BufferOutputStream()
  pq.write_table(tab, sink)
  return sink.getvalue().to_pybytes()
