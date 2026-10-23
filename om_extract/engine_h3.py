"""The engine for an H3 layer: one place, one variable, summarised by joining the layer's cells with the place's
H3 membership, share-weighted.

H3 membership serves only layers whose native grid is H3 (obis-h3, the hex twins, BioTrack); a raster is summarised on
its own grid (`engine.extract()`). The rule, `om_extract/sql/stats_h3.sql`, is the grid rule with the membership in
the mask's place: per time step (period), over the place's cells that have a value,

- `mean_wt = sum(value * share) / sum(share)`, `weight_sum = sum(share)` (`share` is the fraction of the cell's area
  inside the place, the gazetteer's float32 cast to double);
- `n` the cells with a value, `mean`, `sd` (sample, n - 1), `min`, `max`, `p10`, `p90` over them, unweighted.

The stats table has the grid stats' columns (`H3_STATS_COLUMNS`): `date` is the first day of the period (null when the
layer has no periods: one row), `period` the label as published, then `n` ... `weight_sum` and the identity columns
`place_id`, `dataset_id` (the collection id), `variable`. A cell of the layer outside the membership is not read; a
place with no cell holding a value gives no rows.

Values rows (`h3_values_rows()`, reached through `values.values_rows()`): indicator `h3:<collection id>:<var>:<label>`
(`mean_wt` is `mean`, as the grid ids; `mean` is `mean_unw`), `time_grain` the periods' grain or `all`, `n` the
cells with a value, `sd` the cell SD for `mean_wt` and `mean`, `unit` the column's `unit` in `table:columns`, else `1`
(the obis-h3 metrics are counts and indices).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from om_extract.layers import load_collection
from om_extract.readers.h3 import H3Reader, h3_int, h3_str, period_date
from om_extract.stats import render

H3_STATS_COLUMNS = ("date", "period", "n", "mean", "mean_wt", "sd", "min", "max", "p10", "p90", "weight_sum",
                    "place_id", "dataset_id", "variable")
ADAPTER = "om_extract.engine_h3"
METHOD = ("per period, the share-weighted mean of the layer's H3 cells that the place covers (place x H3 membership "
          "from the gazetteer, each cell weighted by the share of its area inside the place), with the unweighted "
          "mean and the sample SD across those cells.")


@dataclass(frozen=True, eq=False)
class H3Extraction:
  """What `extract_h3()` computed: the stats table (`H3_STATS_COLUMNS`, oldest period first), the membership used
  (`membership`: `cell`, `share`), how many of its cells the layer had rows for (`layer_cells`), the files read, the
  SQL, the reader's source block, the grain and the collection."""
  place_id: str
  variable: str
  res: int
  coll: str | None
  stats: pa.Table
  membership: pa.Table
  membership_cells: int
  layer_cells: int
  files: list[str]
  sql: str
  source: dict
  time_grain: str
  collection: dict
  collection_href: str | None = None
  keys: dict = field(default_factory=dict)
  kind: str = "h3"


def _membership_table(membership: Any, place_id: str) -> pa.Table:
  t = membership if isinstance(membership, pa.Table) else (
    pa.Table.from_pylist(membership) if isinstance(membership, list) else pa.table(membership))
  missing = [c for c in ("h3", "share") if c not in t.column_names]
  if missing:
    raise ValueError(f"the membership lacks column(s) {missing}; it has {t.column_names}")
  if "place_id" in t.column_names:
    t = t.filter(pc.equal(t["place_id"], place_id))
  return t


def extract_h3(collection: dict | str | Path, place_id: str, membership: Any, var: str, res: int, *,
               fetch: Any = None, root: str | Path | None = None, keys: dict | None = None, coll: str | None = None,
               reader: H3Reader | None = None) -> H3Extraction:
  """Summarise column `var` of an H3 layer over one place at resolution `res` (rules in the module docstring).

  `collection` is the layer's `h3` collection (a dict, or a path or URL); `membership` the place's rows at `res`
  (`place_id`, `h3`, `share`: a table, a list of dicts or a dict of columns, e.g. from `load_membership()`; rows of
  other places are dropped). `fetch`, `root` and `keys` go to the `H3Reader` (or pass `reader`). `coll` is recorded
  for the values rows. Raises ValueError when `var` is not a metric column of the layer.
  """
  import duckdb
  href = None
  if not isinstance(collection, dict):
    href = str(collection)
    collection = load_collection(collection, fetch=fetch)
  r = reader or H3Reader(collection, resolution=res, fetch=fetch, root=root, keys=keys)
  if var not in r.columns or var == r.cell_column:
    raise ValueError(f"{var!r} is not a metric column of {collection['id']}; it has "
                     f"{[c for c in r.columns if c != r.cell_column]}")
  mem = _membership_table(membership, place_id)
  cells = mem["h3"].to_pylist()
  key = pa.uint64() if r.cell_type == "uint64" else pa.string()
  mask = pa.table({
    "cell": pa.array([h3_int(c) if r.cell_type == "uint64" else h3_str(h3_int(c)) for c in cells], key),
    "share": mem["share"].cast(pa.float64())})
  layer = r.cells(res, cells) if cells else pa.table({r.cell_column: pa.array([], key)})
  n_layer = layer.num_rows
  if var not in layer.column_names:
    layer = layer.append_column(var, pa.nulls(n_layer, pa.float64()))
  col = layer[r.cell_column]
  if r.cell_type == "string":
    col = pc.utf8_lower(col.cast(pa.string()))
  else:
    col = col.cast(pa.uint64())
  period = layer["period"] if "period" in layer.column_names else pa.nulls(n_layer, pa.string())
  slab = pa.table({"cell": col, "period": period, var: layer[var].cast(pa.float64())})

  expr = 's."' + var.replace('"', '""') + '"'
  sql = render("stats_h3", {"expr": expr, "slab": "slab", "mask": "mask"})
  con = duckdb.connect(":memory:")
  try:
    con.register("slab", slab)
    con.register("mask", mask)
    rel = con.sql(sql)
    out = rel.to_arrow_table() if hasattr(rel, "to_arrow_table") else rel.fetch_arrow_table()
  finally:
    con.close()
  dates = [period_date(r.grain, p) for p in out["period"].to_pylist()]
  out = pa.table({"date": pa.array(dates, pa.date32()), **{c: out[c] for c in out.column_names}})
  n = out.num_rows
  for name, v in (("place_id", place_id), ("dataset_id", collection["id"]), ("variable", var)):
    out = out.append_column(name, pa.array([v] * n, pa.string()))
  out = out.select(list(H3_STATS_COLUMNS))
  if r.grain != "all":
    out = out.sort_by([("date", "ascending")])
  return H3Extraction(
    place_id=place_id, variable=var, res=int(res), coll=coll, stats=out, membership=mask,
    membership_cells=mask.num_rows, layer_cells=len(set(slab["cell"].to_pylist())), files=list(r.files_read),
    sql=sql, source=r.source_block(), time_grain=r.grain, collection=collection, collection_href=href,
    keys=dict(keys or {}))


def indicator_id_h3(collection_id: str, variable: str, stat: str) -> str:
  """`h3:<collection id>:<variable>:<label>`, `mean_wt` labelled `mean` and `mean` `mean_unw` (as the grid ids)."""
  from om_extract.values import STAT_LABEL
  return f"h3:{collection_id}:{variable}:{STAT_LABEL.get(stat, stat)}"


def h3_values_rows(x: H3Extraction, *, stat: str = "mean_wt", grain: str | None = None) -> list[dict]:
  """The contract rows of an H3 extraction, one per period (or one `all` row): see the module docstring. `grain` may
  only name the layer's own grain."""
  from om_extract.contract import GRAINS, period_start
  from om_extract.engine import default_coll
  from om_extract.values import COARSER, CONTINUOUS_STATS, SD_STATS, _finite
  if grain is not None and grain != x.time_grain:
    if grain not in GRAINS:
      raise ValueError(f"unknown grain {grain!r}; the contract has {list(GRAINS)}")
    raise ValueError(f"{COARSER} (native grain {x.time_grain!r}, asked {grain!r})")
  if stat not in CONTINUOUS_STATS:
    raise ValueError(f"stat {stat!r} is not one of {list(CONTINUOUS_STATS)}")
  col = next((c for c in x.collection["table:columns"] if c["name"] == x.variable), {})
  unit = col.get("unit") or "1"
  ind = indicator_id_h3(x.collection["id"], x.variable, stat)
  coll = x.coll or default_coll(x.place_id)
  out = []
  for r in x.stats.to_pylist():
    v = _finite(r[stat])
    sd = _finite(r["sd"]) if stat in SD_STATS and v is not None else None
    out.append({"place_id": x.place_id, "coll": coll, "indicator": ind, "time_grain": x.time_grain,
                "time": period_start(x.time_grain, r["date"]) if r["date"] is not None else None,
                "class": None, "value": v, "n": float(r["n"]), "sd": sd, "unit": unit})
  return out


def h3_provenance_for(x: H3Extraction, code_version: str) -> dict:
  """The values file's provenance for an H3 extraction: producer, dataset (the collection's id, title, providers,
  licence), one `data` source per layer file read, the collection as `product`, method, code and licence."""
  from om_extract.contract import utc_now
  from om_extract.values import PRODUCER_URL
  c = x.collection
  links = c.get("links") or []
  self_ = next((lk["href"] for lk in links if lk.get("rel") == "self"), None)
  via = next((lk["href"] for lk in links if lk.get("rel") in ("via", "about") and lk.get("href")), None)
  return {
    "producer": {"name": "oceanmetrics/extract", "url": PRODUCER_URL, "version": code_version},
    "dataset": {"id": c["id"], "title": c.get("title") or c["id"], "via": via,
                "providers": [{k: p[k] for k in ("name", "roles", "url") if k in p}
                              for p in c.get("providers") or [] if p.get("name")],
                "license": c.get("license") or "other", "citation": c.get("sci:citation"), "doi": None},
    "sources": [*({"href": f, "item": None, "role": "data"} for f in x.files),
                {"href": x.collection_href or self_ or c["id"], "item": None, "role": "product"}],
    "source_version": c.get("updated"),
    "method": METHOD,
    "code": {"name": "om-extract", "version": code_version, "adapter": ADAPTER},
    "license": c.get("license") or "other",
    "created": utc_now(),
  }
