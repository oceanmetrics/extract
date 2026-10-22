"""The extraction engine: one place, one variable, one time window of a layer, summarised by the one rule.

`extract()` is erddap-places' precompute `computeOne()` (`precompute/src/stats.ts`) in one function, built from the
ported parts: `layers.dataset_of()`, `readers.for_collection()`, `geom.place_lobes()`, `mask.grid_mask()` and
`stats.stats()` over the templates in `om_extract/sql/`. The steps, in order:

1. the dataset record (`dataset_of`) and the reader (`readers.for_collection`, `fetch` injected);
2. the place's lobes (`place_lobes`: one per side of +-180);
3. per lobe: the axes over its bbox (`reader.axes`), its mask (`grid_mask`; on a 0..360 grid the longitudes are
   shifted to -180..180 for the polygon and the mask cells shifted back, as `maskLobe()` does), the request URLs
   (`reader.slab_urls`) and the slab (`reader.slab`);
4. the masks and slabs of all lobes concatenated, the slab rows made DISTINCT (a coarse axis can snap two chunks'
   endpoints onto one step, as precompute's `SELECT DISTINCT * FROM read_parquet(...)`), and `stats()` run with
   `stats_daily` or, for a categorical variable, `stats_categorical`;
5. the stats table with erddap-places' published column names (`selectList()`: `fraction` is `frac_area` and
   `percent_cells` is `pct_cells`) and its three identity columns `place_id`, `dataset_id`, `variable`.

The value expression is erddap-places' `valueExpr()`: `s."<var>"`, or `(s."<var>" - 273.15)` for a Kelvin unit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa

from om_extract.geom import place_lobes
from om_extract.layers import Dataset, dataset_of, is_categorical, load_collection
from om_extract.mask import grid_mask
from om_extract.readers import for_collection
from om_extract.readers.erddap import Fetch, TimeExtent, clamp_window, day, time_instant, to_dataset_lon
from om_extract.stats import render
from om_extract.stats import stats as run_stats

# erddap-places' published stats columns (precompute `selectList()` plus the identity columns), in order
STATS_COLUMNS = ("date", "n", "mean", "mean_wt", "sd", "min", "max", "p10", "p90", "weight_sum",
                 "place_id", "dataset_id", "variable")
CATEGORICAL_COLUMNS = ("date", "class", "n", "weight", "frac_area", "pct_cells", "place_id", "dataset_id", "variable")
# the app's SQL names to the published ones (precompute `selectList()`)
_RENAME = {"fraction": "frac_area", "percent_cells": "pct_cells"}
# a provenance lists every request verbatim up to this many (precompute GRIDDAP_URLS_MAX)
GRIDDAP_URLS_MAX = 24
KELVIN = re.compile(r"^(k|kelvin|degree_?k(elvin)?)$", re.I)
# the first constraint after the variable is time: `?var%5B(t0):1:(t1)%5D`
_TIME_RE = re.compile(r"\?([^%&]+)%5B\(([^)]*)\):1:\(([^)]*)\)%5D")


def value_expr(var: str, unit: str | None, alias: str = "s") -> str:
  """The SQL value expression of a variable (catalog.ts `valueExpr()`): `s."<var>"`, or `(s."<var>" - 273.15)` when
  the unit is Kelvin (`K`, `kelvin`, `degree_K`, `degreeK`, `degree_kelvin`; any case), so values are in deg C."""
  col = f'{alias}."{var}"'
  return f"({col} - 273.15)" if KELVIN.match(unit or "") else col


def value_unit(unit: str | None) -> str | None:
  """The unit of the values `value_expr()` yields: `degree_C` for a Kelvin unit, else the variable's own unit."""
  return "degree_C" if KELVIN.match(unit or "") else unit


@dataclass(frozen=True, eq=False)
class Extraction:
  """What `extract()` computed and everything its provenance needs.

  `stats`: erddap-places' stats table (`STATS_COLUMNS`, or `CATEGORICAL_COLUMNS` when `categorical`), one row per UTC
  day (and class), oldest first. `urls`: every request, per lobe in lobe order, chunks in time order. `mask`: the
  SQL's mask table of all lobes (`latitude`, `longitude` in the dataset's frame, `weight`); `mask_cells` its rows;
  `lobes`: how many lobes. `sql`: the text DuckDB ran. `source`: the reader's `source_block()`.
  `dataset`: the `dataset_of()` record; `collection` the document and `collection_href` where it was read (None for a
  dict). `t0`/`t1` as asked; `start_date`/`end_date` the window clamped to the extent; `start_datetime`/`end_datetime`
  their griddap instants; `record_start` the dataset's first day. `axes`: per lobe its bbox and the axis values
  (dataset frame) the mask was computed on. `step_days`: the time step in days when the extent says, else None.
  """
  place_id: str
  variable: str
  categorical: bool
  coll: str | None
  stats: pa.Table
  urls: list[str]
  mask: pa.Table
  mask_cells: int
  lobes: int
  sql: str
  source: dict
  dataset: Dataset
  collection: dict
  collection_href: str | None
  t0: str
  t1: str
  start_date: str
  end_date: str
  start_datetime: str
  end_datetime: str
  record_start: str | None
  axes: list[dict] = field(default_factory=list)
  step_days: float | None = None
  live_extent: bool = False


def _mask_lobe(lobe, ax, lon_range) -> pa.Table:
  """One lobe's mask on its axes as the SQL's `mask` table (precompute `maskLobe()`): on a 0..360 grid the polygon is
  in -180..180, so the axis is shifted for the mask and the cells shifted back to the dataset's frame."""
  shifted = lon_range[1] > 180
  lon_poly = [x - 360 if shifted and x > 180 else float(x) for x in ax.lon]
  m = grid_mask(lobe, list(ax.lat), lon_poly)
  t = m.to_arrow()
  if shifted:
    lons = [to_dataset_lon(x, lon_range) for x in t["longitude"].to_pylist()]
    t = t.set_column(1, "longitude", pa.array(lons, pa.float64()))
  return t


def _sql_slab(slab: pa.Table, var: str) -> pa.Table:
  """The reader's long table (`time, lat, lon, value`) as the SQL templates read ERDDAP's Parquet:
  `time, latitude, longitude, <var>`."""
  return pa.table({"time": slab["time"], "latitude": slab["lat"], "longitude": slab["lon"], var: slab["value"]})


def time_window(url: str) -> tuple[str, str] | None:
  """The `(t0, t1)` of a griddap URL's time constraint (the first one after the variable), or None."""
  m = _TIME_RE.search(url)
  return (m.group(2), m.group(3)) if m else None


def url_template(url: str) -> str:
  """A griddap URL with its time constraint as `({t0}):1:({t1})` (precompute `griddap_url_templates`)."""
  return _TIME_RE.sub(lambda m: f"?{m.group(1)}%5B({{t0}}):1:({{t1}})%5D", url, count=1)


def default_coll(place_id: str) -> str:
  """The gazetteer collection of a place id by its authority: `NMS:` is `places`, any other the authority lowercased
  (`MRGID:8371` is `mrgid`)."""
  auth = place_id.split(":", 1)[0]
  return "places" if auth == "NMS" else auth.lower()


def extract(collection: dict | str | Path, place_id: str, geometry_wkb: Any, var: str, t0: str, t1: str, *,
            fetch: Fetch | None = None, coll: str | None = None, categorical: bool | None = None) -> Extraction:
  """Summarise `var` of a layer over one place and `[t0, t1]` (days, `yyyy-mm-dd`) by the one rule.

  `collection` is the layer's STAC collection (a dict, or a path or URL `load_collection()` reads); `geometry_wkb`
  the place's geometry (WKB bytes, or a GeoJSON-like dict, a shapely geometry), already cut at +-180 by the
  gazetteer. `fetch(url) -> bytes` serves every request (default: the reader's own HTTP fetch). `coll` is recorded for
  the values rows. `categorical` overrides the variable's `erddap-places:categorical` flag. Returns an `Extraction`
  (see its docstring). Raises ValueError when the place has no polygon or the variable is not in the collection.
  """
  href = None
  if not isinstance(collection, dict):
    href = str(collection)
    collection = load_collection(collection, fetch=fetch)
  ds = dataset_of(collection)
  meta = (collection.get("cube:variables") or {}).get(var)
  if meta is None:
    raise ValueError(f"{var!r} is not a variable of {ds.id}; it has {[v.name for v in ds.variables]}")
  cat = is_categorical(meta) if categorical is None else bool(categorical)
  reader = for_collection(collection, fetch=fetch)
  lobes = place_lobes(geometry_wkb, place_id)
  if not lobes:
    raise ValueError(f"{place_id}: no polygon lobes in the geometry")

  masks, slabs, urls, axes = [], [], [], []
  for lobe in lobes:
    ax = reader.axes(lobe.bbox)
    masks.append(_mask_lobe(lobe, ax, ds.lon_range))
    urls += reader.slab_urls(lobe.bbox, t0, t1, var)
    slabs.append(_sql_slab(reader.slab(lobe.bbox, t0, t1, var), var))
    axes.append({"bbox": list(lobe.bbox), "lon": [float(x) for x in ax.lon], "lat": [float(x) for x in ax.lat]})
  mask = pa.concat_tables(masks)
  slab = pa.concat_tables(slabs)

  expr = value_expr(var, meta.get("unit"))
  template = "stats_categorical" if cat else "stats_daily"
  sql = render(template, {"expr": expr, "slab": "slab", "mask": "mask"})
  import duckdb
  con = duckdb.connect(":memory:")
  try:
    con.register("raw_slab", slab)
    rel = con.sql("SELECT DISTINCT * FROM raw_slab")
    distinct = rel.to_arrow_table() if hasattr(rel, "to_arrow_table") else rel.fetch_arrow_table()
    con.unregister("raw_slab")
    out = run_stats(distinct, mask, template, expr=expr, con=con)
  finally:
    con.close()
  out = out.rename_columns([_RENAME.get(c, c) for c in out.column_names])
  n = out.num_rows
  for name, v in (("place_id", place_id), ("dataset_id", ds.dataset_id), ("variable", var)):
    out = out.append_column(name, pa.array([v] * n, pa.string()))

  ext = reader.extent()
  tx = TimeExtent(start=ext.time_start, end=ext.time_end, step_days=ext.step_days) if ext.time_start else None
  s, e, _, _ = clamp_window(day(t0), day(t1), tx)
  return Extraction(
    place_id=place_id, variable=var, categorical=cat, coll=coll, stats=out, urls=urls, mask=mask,
    mask_cells=mask.num_rows, lobes=len(lobes), sql=sql, source=reader.source_block(), dataset=ds,
    collection=collection, collection_href=href,
    t0=t0, t1=t1, start_date=s, end_date=e, start_datetime=time_instant(s, tx), end_datetime=time_instant(e, tx),
    record_start=day(ext.time_start) if ext.time_start else None, axes=axes, step_days=ext.step_days,
    live_extent=ext.live)


def stats_provenance(x: Extraction, *, rows: int | None = None, bytes_: int | None = None,
                     generated: str | None = None) -> dict:
  """The stats file's provenance JSON with erddap-places' keys (precompute `Provenance`): place, dataset, variable,
  window, lobes, mask cells, axes, the requests (verbatim up to `GRIDDAP_URLS_MAX`, always as one template per lobe
  plus the chunk instants), rows, bytes, generated, the ERDDAP base and version, and the SQL."""
  from om_extract.contract import utc_now
  windows = sorted({w for w in (time_window(u) for u in x.urls) if w})
  templates = list(dict.fromkeys(url_template(u) for u in x.urls))
  return {
    "place_id": x.place_id, "dataset_id": x.dataset.dataset_id, "variable": x.variable, "categorical": x.categorical,
    "start_date": x.start_date, "end_date": x.end_date,
    "start_datetime": x.start_datetime, "end_datetime": x.end_datetime, "record_start": x.record_start,
    "lobes": x.lobes, "mask_cells": x.mask_cells, "axes": x.axes,
    **({"griddap_urls": list(x.urls)} if len(x.urls) <= GRIDDAP_URLS_MAX else {}),
    "griddap_url_templates": templates, "griddap_chunks": [list(w) for w in windows],
    "griddap_requests": len(x.urls),
    "rows": x.stats.num_rows if rows is None else rows, "bytes": bytes_,
    "generated": generated or utc_now(), "erddap_base": x.dataset.base_url, "erddap_version": x.dataset.version,
    "sql": x.sql,
  }
