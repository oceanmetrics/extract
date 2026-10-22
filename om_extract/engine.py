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

import os
import re
import threading
import time as _time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa

from om_extract.geom import place_lobes
from om_extract.layers import Dataset, dataset_of, is_categorical, load_collection
from om_extract.mask import grid_mask
from om_extract.readers import for_collection
from om_extract.readers.erddap import (ErddapError, ErddapUnreachable, Fetch, TimeExtent, chunk_days_for, clamp_window,
                                      day, days_between, fetch_axis, griddap_url, info_url, lobe_lon_span, make_fetch,
                                      noon_z, parse_info_extent, time_instant, to_dataset_lon)
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
  # the chunked run (`chunks=`): the chunk instants, the chunk size, the store key and directory, the slices fetched
  # and kept, the stats file written and its rows and bytes; None / empty otherwise
  template: str | None = None
  expr: str | None = None
  chunks_at: list[tuple[str, str]] | None = None
  chunk_days: int | None = None
  mask_key: str | None = None
  chunk_dir: str | None = None
  fetched: list[tuple[str, str]] = field(default_factory=list)
  kept: list[tuple[str, str]] = field(default_factory=list)
  stats_path: str | None = None
  rows: int | None = None
  bytes: int | None = None
  extent_end: str | None = None


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


def extract(collection: dict | str | Path, place_id: str, geometry_wkb: Any, var: str, t0: str | None = None,
            t1: str | None = None, *, fetch: Fetch | None = None, coll: str | None = None,
            categorical: bool | None = None, chunks: str | Path | None = None, stats_path: str | Path | None = None,
            existing: list[str] | None = None, sleep: Callable[[float], None] = _time.sleep,
            log: Callable[[str], None] | None = None, threads: int | None = 1,
            should_stop: Callable[[], bool] | None = None) -> Extraction:
  """Summarise `var` of a layer over one place and `[t0, t1]` (days, `yyyy-mm-dd`) by the one rule.

  `collection` is the layer's STAC collection (a dict, or a path or URL `load_collection()` reads); `geometry_wkb`
  the place's geometry (WKB bytes, or a GeoJSON-like dict, a shapely geometry), already cut at +-180 by the
  gazetteer. `fetch(url) -> bytes` serves every request (default: the reader's own HTTP fetch). `coll` is recorded for
  the values rows. `categorical` overrides the variable's `erddap-places:categorical` flag. Returns an `Extraction`
  (see its docstring). Raises ValueError when the place has no polygon or the variable is not in the collection.

  `chunks` (a directory) runs it as erddap-places' precompute `computeOne()` does, through the chunk store
  (`om_extract.chunks`, `extract_chunked()`): the live extent (raising when the server cannot say), the window
  `window(ext, 'all')` unless `t0`/`t1` pin it, axes from the server, the chunk size of the largest lobe, only the
  chunks the store lacks fetched, and the stats file written by DuckDB `COPY` to `stats_path`.
  """
  if chunks is not None:
    return extract_chunked(collection, place_id, geometry_wkb, var, chunks, t0=t0, t1=t1, fetch=fetch, coll=coll,
                           categorical=categorical, stats_path=stats_path, existing=existing, sleep=sleep, log=log,
                           threads=threads, should_stop=should_stop)
  if t0 is None or t1 is None:
    raise ValueError("t0 and t1 are required without a chunk store (chunks=)")
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
    live_extent=ext.live, template=template, expr=expr)


def ts_sql(template: str, expr: str) -> str:
  """The provenance's `sql` as precompute renders it (stats.ts:425): the template with the slab shown as
  `(SELECT DISTINCT * FROM read_parquet([<one griddap slab per lobe for the chunk>]))`."""
  return render(template, {"expr": expr, "slab": TS_SQL_SLAB, "mask": "mask"})


def stats_provenance(x: Extraction, *, rows: int | None = None, bytes_: int | None = None,
                     generated: str | None = None) -> dict:
  """The stats file's provenance JSON with erddap-places' keys in its order (precompute `Provenance`, stats.ts:301-333,
  :413-426): place, dataset, variable, window, lobes, mask cells, axes, the requests (verbatim up to
  `GRIDDAP_URLS_MAX`, always as one template per lobe plus the chunk instants), rows, bytes, generated, the ERDDAP
  base and version (left out when the collection has none, as `JSON.stringify` drops an undefined key), and the SQL
  rendered the TS way (`ts_sql()`). `griddap_chunks` are the chunk instants of a chunked run, else parsed from the URLs."""
  from om_extract.contract import utc_now
  if x.chunks_at is not None:
    windows = [list(w) for w in x.chunks_at]
  else:
    windows = [list(w) for w in sorted({w for w in (time_window(u) for u in x.urls) if w})]
  templates = list(dict.fromkeys(url_template(u) for u in x.urls))
  if x.template and x.expr:
    sql = ts_sql(x.template, x.expr)
  else:
    sql = x.sql
  out = {
    "place_id": x.place_id, "dataset_id": x.dataset.dataset_id, "variable": x.variable, "categorical": x.categorical,
    "start_date": x.start_date, "end_date": x.end_date,
    "start_datetime": x.start_datetime, "end_datetime": x.end_datetime, "record_start": x.record_start,
    "lobes": x.lobes, "mask_cells": x.mask_cells, "axes": x.axes,
    **({"griddap_urls": list(x.urls)} if len(x.urls) <= GRIDDAP_URLS_MAX else {}),
    "griddap_url_templates": templates, "griddap_chunks": windows,
    "griddap_requests": len(x.urls),
    "rows": (x.rows if x.rows is not None else x.stats.num_rows) if rows is None else rows,
    "bytes": (x.bytes if bytes_ is None else bytes_),
    "generated": generated or utc_now(), "erddap_base": x.dataset.base_url,
  }
  if x.dataset.version is not None:
    out["erddap_version"] = x.dataset.version
  out["sql"] = sql
  return out


def provenance_text(prov: dict) -> str:
  """The provenance file's text as precompute writes it: `JSON.stringify(prov, null, 2) + '\\n'` (`_js.js_json`)."""
  from om_extract._js import js_json
  return js_json(prov, indent=2) + "\n"


# -- the chunked run: precompute computeOne() ------------------------------------------------------------------------
# the provenance's slab placeholder (stats.ts:425)
TS_SQL_SLAB = "(SELECT DISTINCT * FROM read_parquet([<one griddap slab per lobe for the chunk>]))"
# requests in flight per place and the pause before each (targets.ts CONCURRENCY, SLEEP_MS)
CONCURRENCY = 2
SLEEP_S = 0.75
# the extent probe: attempts and the pause before each retry (stats.ts EXTENT_ATTEMPTS, EXTENT_BACKOFF_MS), 120 s each
EXTENT_ATTEMPTS = 5
EXTENT_BACKOFF_S = (5, 20, 60, 120)
EXTENT_TIMEOUT_S = 120
# the axis requests of a headless run (stats.ts AXIS_RETRY: 2 min per try, 3 tries, 10 s apart)
AXIS_RETRY = {"timeout_s": 120, "attempts": 3, "backoff_s": 10}
# a slab download (stats.ts download(): 900 s, one retry after 5 s on any failure)
DOWNLOAD_TIMEOUT_S = 900
DOWNLOAD_PAUSE_S = 5


class StopRequested(Exception):
  """`should_stop()` turned true between chunks: the place is unfinished, not failed (its done chunks stay)."""


def live_extent(ds: Dataset, *, fetch: Fetch | None = None, sleep: Callable[[float], None] = _time.sleep,
                attempts: int = EXTENT_ATTEMPTS, log: Callable[[str], None] | None = None) -> TimeExtent:
  """The live time extent from `<base>/info/<id>/index.json` (`liveExtent()`, stats.ts:70-93), never the catalog's: a
  network error or a 5xx is retried `attempts` times, backing off `EXTENT_BACKOFF_S`; a 4xx raises at once; an info
  table with no time extent raises; and when every attempt fails it raises `ErddapUnreachable` (no silent fallback)."""
  url = info_url(ds.base_url, ds.dataset_id)
  one = fetch or make_fetch(timeout_s=EXTENT_TIMEOUT_S, attempts=1)
  last: Exception | None = None
  for i in range(attempts):
    if i:
      wait = EXTENT_BACKOFF_S[min(i - 1, len(EXTENT_BACKOFF_S) - 1)]
      (log or (lambda _: None))(f"   extent probe failed ({last}); retry {i}/{attempts - 1} in {wait} s")
      sleep(wait)
    try:
      body = one(url)
    except ErddapUnreachable as e:
      last = e
      continue
    import json as _json
    ext = parse_info_extent(_json.loads(body))
    if ext is None:
      raise ErddapError(f"{url}: no time extent in the info table")
    return ext
  raise ErddapUnreachable(last.host if isinstance(last, ErddapUnreachable) else url,
                          f"extent probe failed {attempts} times: {last}", 1, url)


def download(url: str, file: Path, fetch: Fetch, *, sleep: Callable[[float], None] = _time.sleep,
             log: Callable[[str], None] | None = None) -> int:
  """GET a griddap URL to `file` once and return its size; a cached file (size > 0) is reused. Any failure is retried
  once after `DOWNLOAD_PAUSE_S` (stats.ts `download()`: two tries, a 4xx too); the second failure raises."""
  if file.is_file() and file.stat().st_size > 0:
    return file.stat().st_size
  file.parent.mkdir(parents=True, exist_ok=True)
  last: Exception | None = None
  for attempt in range(2):
    try:
      t = _time.monotonic()
      buf = fetch(url)
      from om_extract.chunks import write_atomic
      write_atomic(file, buf)
      if log:
        log(f"      {len(buf) / 1e6:.1f} MB in {_time.monotonic() - t:.1f} s")
      return len(buf)
    except Exception as e:  # noqa: BLE001 - the TS retries any failure once
      last = e
      if attempt == 0:
        if log:
          log(f"      retrying after: {e}")
        sleep(DOWNLOAD_PAUSE_S)
  raise ErddapError(f"griddap failed twice: {url}\n  {last}")


def select_list(categorical: bool) -> str:
  """The published column list over the template's output (`selectList()`, stats.ts:273-278)."""
  return ("date, class, n, weight, fraction AS frac_area, percent_cells AS pct_cells" if categorical
          else 'date, n, mean, mean_wt, sd, "min", "max", p10, p90, weight_sum')


def _sq(s: str | Path) -> str:
  return "'" + str(s).replace("'", "''") + "'"


def _files_list(files: list[Path]) -> str:
  return "[" + ", ".join(_sq(f) for f in files) + "]"


def extract_chunked(collection: dict | str | Path, place_id: str, geometry_wkb: Any, var: str, chunks: str | Path, *,
                    t0: str | None = None, t1: str | None = None, fetch: Fetch | None = None, coll: str | None = None,
                    categorical: bool | None = None, stats_path: str | Path | None = None,
                    existing: list[str] | None = None, sleep: Callable[[float], None] = _time.sleep,
                    log: Callable[[str], None] | None = None, threads: int | None = 1,
                    should_stop: Callable[[], bool] | None = None) -> Extraction:
  """precompute `computeOne()` (stats.ts:335-430) over the chunk store at `chunks`. The steps, each the TS's:

  1. the live extent (`live_extent()`, raising) and the window: `window(ext, 'all')`, or `[t0, t1]` when given;
  2. per lobe, the axes from the server (`fetch_axis()`, `maskLobe()`: longitude over `lobe_lon_span()`, latitude
     high to low on a descending grid), the mask, the lobe's axis cells; a pause of `SLEEP_S` after each lobe;
  3. the chunk size from the largest lobe's axis cells for all lobes (`chunk_days_for(max cells, step_days)`);
  4. the mask key, the store directory, `plan_chunks()` over its files (or over `existing`, a listing to plan from:
     the parity harness passes the runner's), the dropped files removed;
  5. every slice without a chunk file is fetched (per lobe, `download()`; `CONCURRENCY` at a time, `SLEEP_S` before
     each) and aggregated in DuckDB with `COPY (SELECT <select_list> FROM (<template over the DISTINCT slab files>))`
     to `<chunk>.tmp`, renamed into place, its slabs deleted, a superseded short chunk deleted;
  6. the stats file: `COPY (SELECT *, place_id, dataset_id, variable FROM (SELECT DISTINCT * FROM read_parquet([every
     chunk])) ORDER BY date[, class]) TO stats_path (FORMAT PARQUET, COMPRESSION ZSTD)`, then its rows and bytes.

  `threads` sets DuckDB's threads (default 1: the summation order of `sum`/`avg`/`stddev` is then reproducible run to
  run; the runner uses every core, and its sums differ from run to run in the last bits). `stats_path` defaults to
  `<chunks>/.out/<dataset>/<variable>/<place>.parquet`. Raises `StopRequested` when `should_stop()` turns true before
  every chunk is done.
  """
  import duckdb
  import pyarrow.parquet as pq

  from om_extract.chunks import ChunkStore, chunk_name, mask_key, plan_chunks, window

  say = log or (lambda _: None)
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
  expr = value_expr(var, meta.get("unit"))
  template = "stats_categorical" if cat else "stats_daily"

  # 1. the live extent and the window
  ext = live_extent(ds, fetch=fetch, sleep=sleep, log=say)
  if t0 is None and t1 is None:
    win = window(ext.start, ext.end, "all")
  else:
    win = (day(t0 or ext.start), day(t1 or ext.end))
    if days_between(win[0], win[1]) < 0:
      raise ValueError(f"the window starts after it ends: {win[0]} to {win[1]}")

  def t(d: str) -> str:
    return time_instant(d, ext, noon_z)

  lobes = place_lobes(geometry_wkb, place_id)
  if not lobes:
    raise ValueError(f"{place_id}: no polygon lobes in the geometry")

  # 2. the mask of every lobe, on the server's axes
  axis_fetch = fetch or make_fetch(**AXIS_RETRY)
  masks, axes, lob_axes = [], [], []
  for lobe in lobes:
    lo, hi = lobe_lon_span(lobe.bbox, ds.lon_range)
    lon = fetch_axis(ds.base_url, ds.dataset_id, "longitude", lo, hi, False, fetch=axis_fetch)
    lat = fetch_axis(ds.base_url, ds.dataset_id, "latitude", lobe.bbox[1], lobe.bbox[3], ds.lat_descending,
                     fetch=axis_fetch)
    from om_extract.readers.base import Axes
    import numpy as np
    ax = Axes(lat=np.array(lat), lon=np.array(lon), step=(None, None), lat_descending=ds.lat_descending,
              lon_range=tuple(ds.lon_range), depth=ds.depth, source="live")
    masks.append(_mask_lobe(lobe, ax, ds.lon_range))
    lob_axes.append((lon, lat))
    axes.append({"bbox": list(lobe.bbox), "lon": lon, "lat": lat})
    sleep(SLEEP_S)
  mask = pa.concat_tables(masks)
  per_step = max(len(lon) * len(lat) for lon, lat in lob_axes)
  cd = chunk_days_for(per_step, ext.step_days if ext.step_days is not None else 1)

  # 3-4. the store directory and the plan
  cells = zip(mask["longitude"].to_pylist(), mask["latitude"].to_pylist(), mask["weight"].to_pylist())
  key = mask_key(ds.base_url, ds.dataset_id, ds.depth, var, expr, template, cells)
  store = ChunkStore(chunks)
  d = store.dir_for(ds.dataset_id, var, place_id, key)
  d.mkdir(parents=True, exist_ok=True)
  plan = plan_chunks(store.listing(d) if existing is None else existing, win, cd)
  for f in plan.drop:
    (d / f).unlink(missing_ok=True)
  slices = plan.slices
  chunk_files = [d / chunk_name(s, e) for s, e in slices]
  todo = [(sl, j) for j, sl in enumerate(slices) if not chunk_files[j].exists()]
  say(f"    {len(lobes)} lobe(s), {mask.num_rows} masked cells, {per_step} axis cells/step (largest lobe), "
      f"{len(slices) - len(todo)} chunk(s) done, {len(todo)} to fetch at <={cd} d ({len(todo) * len(lobes)} request(s))")

  def url_for(i: int, a: str, b: str) -> str:
    lon, lat = lob_axes[i]
    return griddap_url(base=ds.base_url, dataset_id=ds.dataset_id, variable=var, time=[a, b],
                       lat=[lat[-1], lat[0]], lon=[lon[0], lon[-1]], lat_descending=ds.lat_descending,
                       depth=ds.depth, format="parquet")

  con = duckdb.connect(":memory:", config={"threads": threads} if threads else {})
  con.execute("SET enable_progress_bar = false")
  con.register("mask_arrow", mask)
  con.execute("CREATE OR REPLACE TEMP TABLE mask AS SELECT latitude::DOUBLE AS latitude, longitude::DOUBLE AS longitude,"
              " weight::DOUBLE AS weight FROM mask_arrow")
  con.unregister("mask_arrow")
  lock = threading.Lock()
  slab_fetch = fetch or make_fetch(timeout_s=DOWNLOAD_TIMEOUT_S, attempts=1)
  slab_dir = store.slab_dir(ds.dataset_id, var, place_id)
  done = [0]
  stop = should_stop or (lambda: False)

  def job(sl: tuple[str, str], j: int) -> None:
    s, e = sl
    files = [slab_dir / f"lobe{i}_{s}_{e}.parquet" for i in range(len(lobes))]
    for i in range(len(lobes)):
      download(url_for(i, t(s), t(e)), files[i], slab_fetch, sleep=sleep, log=say)
    with lock:
      # DISTINCT: a coarse axis (the 8-day grid) can snap a chunk's two endpoints onto one step
      slab = f"(SELECT DISTINCT * FROM read_parquet({_files_list(files)}))"
      body = render(template, {"expr": expr, "slab": slab, "mask": "mask"})
      tmp = chunk_files[j].with_name(chunk_files[j].name + ".tmp")
      con.execute(f"COPY (SELECT {select_list(cat)} FROM ({body})) TO {_sq(tmp)} (FORMAT PARQUET, COMPRESSION ZSTD)")
      os.replace(tmp, chunk_files[j])
      for f in files:
        f.unlink(missing_ok=True)
      if plan.supersede and s == plan.supersede[:10]:
        (d / plan.supersede).unlink(missing_ok=True)
      done[0] += 1
      if done[0] % 25 == 0 or done[0] == len(todo):
        say(f"      {done[0]}/{len(todo)} chunk(s) aggregated")

  try:
    if todo:
      queue = list(todo)
      qlock = threading.Lock()
      errors: list[BaseException] = []

      def worker() -> None:
        while True:
          with qlock:
            if not queue or errors or stop():
              return
            sl, j = queue.pop(0)
          sleep(SLEEP_S)
          try:
            job(sl, j)
          except BaseException as e:  # noqa: BLE001 - re-raised below, after the other worker stops
            with qlock:
              errors.append(e)
            return
      with ThreadPoolExecutor(max_workers=min(CONCURRENCY, len(todo))) as pool:
        for f in [pool.submit(worker) for _ in range(min(CONCURRENCY, len(todo)))]:
          f.result()
      if errors:
        raise errors[0]
    if any(not chunk_files[j].exists() for _, j in todo):
      raise StopRequested("stopped at the run deadline")

    # 6. the file: every chunk's rows, oldest first. DISTINCT again: two chunks snapped onto the same step of a coarse
    # axis give that step's row twice, identical
    order = "date, class" if cat else "date"
    out = Path(stats_path) if stats_path else store.root / ".out" / ds.dataset_id / var / f"{place_id}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = out.with_name(out.name + ".tmp")
    con.execute(f"""COPY (
  SELECT *, {_sq(place_id)} AS place_id, {_sq(ds.dataset_id)} AS dataset_id, {_sq(var)} AS variable
  FROM (SELECT DISTINCT * FROM read_parquet({_files_list(chunk_files)})) ORDER BY {order}
) TO {_sq(tmp_out)} (FORMAT PARQUET, COMPRESSION ZSTD)""")
    os.replace(tmp_out, out)
    n = int(con.execute(f"SELECT count(*) AS n FROM read_parquet({_sq(out)})").fetchone()[0])
  finally:
    con.close()
  nbytes = out.stat().st_size
  stats_tab = pq.read_table(out)
  chunks_at = [(t(s), t(e)) for s, e in slices]
  urls = [url_for(i, a, b) for i in range(len(lobes)) for a, b in chunks_at]
  ran = render(template, {"expr": expr, "slab": TS_SQL_SLAB, "mask": "mask"})
  return Extraction(
    place_id=place_id, variable=var, categorical=cat, coll=coll, stats=stats_tab, urls=urls, mask=mask,
    mask_cells=mask.num_rows, lobes=len(lobes), sql=ran, source=reader.source_block(), dataset=ds,
    collection=collection, collection_href=href, t0=win[0], t1=win[1], start_date=win[0], end_date=win[1],
    start_datetime=t(win[0]), end_datetime=t(win[1]), record_start=day(ext.start), axes=axes,
    step_days=ext.step_days, live_extent=True, template=template, expr=expr, chunks_at=chunks_at, chunk_days=cd,
    mask_key=key, chunk_dir=str(d), fetched=[sl for sl, _ in todo], kept=list(plan.keep), stats_path=str(out),
    rows=n, bytes=nbytes, extent_end=ext.end)
