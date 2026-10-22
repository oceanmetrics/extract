"""The engine over any grid reader: one place, one variable, one time window, summarised by the one rule.

`extract_reader()` is `engine.extract()` for a source that is not ERDDAP (today a COG): the same lobes
(`place_lobes`), the mask on the reader's native axes (`grid_mask`: cells by centre, partial-area weights), the same
`stats_daily` (or `stats_categorical`) SQL over the reader's long table, and the same `Extraction` with erddap-places'
stats columns. `engine.extract()` hands it a STAC Item (a `Feature`, dispatched by `readers.for_collection`) or a
reader object; the ERDDAP path, with its 0..360 frames and chunked requests, stays in `engine.extract()`.
"""
from __future__ import annotations

from typing import Any

import pyarrow as pa

from om_extract.engine import _RENAME, Extraction, _sql_slab, value_expr
from om_extract.geom import place_lobes
from om_extract.mask import grid_mask
from om_extract.readers import for_collection
from om_extract.stats import render
from om_extract.stats import stats as run_stats

MASK_SCHEMA = pa.schema([("latitude", pa.float64()), ("longitude", pa.float64()), ("weight", pa.float64())])


def _overlap(t0: str, t1: str, s: str | None, e: str | None) -> tuple[str, str]:
  """The window by UTC day, clamped to `[s, e]` when it overlaps it; as asked otherwise (a window outside a COG's
  span gives no rows, not an error)."""
  a, b = t0[:10], t1[:10]
  if s is None or e is None or b < s[:10] or a > e[:10]:
    return a, b
  return max(a, s[:10]), min(b, e[:10])


def extract_reader(source: Any, place_id: str, geometry_wkb: Any, var: str, t0: str, t1: str, *,
                   fetch: Any = None, coll: str | None = None, categorical: bool | None = None) -> Extraction:
  """Summarise `var` of a non-ERDDAP layer over one place and `[t0, t1]` by the one rule.

  `source` is a reader (anything with `kind`, `axes`, `slab`, `extent`, `source_block`, `dataset`) or a STAC document
  `readers.for_collection` dispatches (a COG Item), built with `fetch`. `var` is the reader's variable (a COG band
  name). Returns an `Extraction` whose `stats` has `STATS_COLUMNS` (`dataset_id` the item id), `urls` the asset href,
  `mask` the mask of all lobes on the native grid, `source` the reader's `source_block()`; a window or a place that
  misses the raster gives a stats table with no rows.
  """
  reader = for_collection(source, fetch=fetch) if isinstance(source, (dict, str, bytes)) else source
  if reader.kind == "erddap":
    raise ValueError("an ERDDAP layer goes through engine.extract(collection, ...): pass its collection")
  lobes = place_lobes(geometry_wkb, place_id)
  if not lobes:
    raise ValueError(f"{place_id}: no polygon lobes in the geometry")

  masks, slabs, axes = [], [], []
  for lobe in lobes:
    ax = reader.axes(lobe.bbox)
    if ax.n_cells:
      masks.append(grid_mask(lobe, list(ax.lat), list(ax.lon)).to_arrow())
      slabs.append(_sql_slab(reader.slab(lobe.bbox, t0, t1, var), var))
    axes.append({"bbox": list(lobe.bbox), "lon": [float(x) for x in ax.lon], "lat": [float(x) for x in ax.lat]})
  mask = pa.concat_tables(masks) if masks else MASK_SCHEMA.empty_table()
  empty = pa.table({"time": pa.array([], pa.timestamp("us", tz="UTC")), "latitude": pa.array([], pa.float64()),
                    "longitude": pa.array([], pa.float64()), var: pa.array([], pa.float64())})
  slab = pa.concat_tables(slabs) if slabs else empty

  cat = bool(categorical)
  expr = value_expr(var, None)
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
  ds = reader.dataset
  n = out.num_rows
  for name, v in (("place_id", place_id), ("dataset_id", ds.dataset_id), ("variable", var)):
    out = out.append_column(name, pa.array([v] * n, pa.string()))

  ext = reader.extent()
  s, e = _overlap(t0, t1, ext.time_start, ext.time_end)
  src = reader.source_block(var) if reader.kind == "cog" else reader.source_block()
  doc = getattr(reader, "item", None) or getattr(reader, "collection", None) or {}
  return Extraction(
    place_id=place_id, variable=var, categorical=cat, coll=coll, stats=out, urls=[getattr(reader, "href", ds.base_url)],
    mask=mask, mask_cells=mask.num_rows, lobes=len(lobes), sql=sql, source=src, dataset=ds, collection=doc,
    collection_href=None, t0=t0, t1=t1, start_date=s, end_date=e, start_datetime=f"{s}T00:00:00Z",
    end_datetime=f"{e}T00:00:00Z", record_start=ext.time_start[:10] if ext.time_start else None, axes=axes,
    step_days=ext.step_days, live_extent=False)
