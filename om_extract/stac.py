"""The stats STAC Item: erddap-places' `buildItem()` (`precompute/src/stac.ts`, erddap-places 0d57ad3) in Python.

A stats file is published as three files under the gazetteer's `stats/` folder, the layout erddap-places writes and the
app, the indicators adapter and the requests list read:

    stats/<dataset_id>/<variable>/<place_id>.parquet            the stats table (engine.extract(..., chunks=))
    stats/<dataset_id>/<variable>/<place_id>.provenance.json    engine.stats_provenance(), as JSON.stringify(p, null, 2)
    stats/items/<dataset_id>_<variable>_<place with - for :>.json   this Item

The rules, each the TS line (each a test):

- `id` `itemId()`; `collection` `stats`; `geometry` a MultiPolygon of the place's LOBE boxes (`lobeBoxGeometry()`),
  not its outline; `bbox` the lobes' union round the globe (`placeBbox()` / `unionBbox()`: west > east for a place
  across the antimeridian).
- `properties`: title `<place name> — <variable> (<dataset_id>)`, the description sentence, `datetime` null, the
  provenance's `start_datetime`/`end_datetime`, `created` its `generated`, `table:row_count` its `rows`,
  `gazetteer:collection`, then `erddap-places:*` (place, dataset, variable, `status` and `record_start` when the record
  start is known, categorical, mask cells, lobes, `griddap_urls` when the provenance lists them, the templates, the
  request count).
- `status` (`itemStatus()`): `complete` when the file starts on or before the dataset's first time step (the
  provenance's `record_start`, else the dataset collection's temporal start), else `partial`.
- `assets`: `data` (the parquet, its `table:columns`) and `provenance`, with hrefs relative to the Item file
  (`../<dataset>/<variable>/<place>.parquet`: items/ is one level under stats/; erddap-places fixed `../../` on
  2026-10-22, 33a85d8) and `file:size` + `file:checksum` (multihash sha2-256, `1220` + hex) when the file exists.
- `links`: root, parent, collection, the dataset's collection and the place's collection as `related`, and the
  dataset's own `rel: via` (text/html) when its collection has one.

`write_stats()` writes the three files from a chunked extraction; `item_text()` is `JSON.stringify(item, null, 2)`.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import quote

from om_extract._js import js_json
from om_extract.geom import place_lobes

STAC_VERSION = "1.1.0"
EXT_TABLE = "https://stac-extensions.github.io/table/v1.2.0/schema.json"
EXT_FILE = "https://stac-extensions.github.io/file/v2.1.0/schema.json"
PLACES_COLLECTION = "places"
# what encodeURIComponent leaves as is, beyond the letters, digits and `-_.~` that quote() keeps
_URI_SAFE = "!*'()"
PLACES_COLLECTION_TITLE = "National Marine Sanctuaries (NMS codes)"

DAILY_COLUMNS = [
  {"name": "date", "type": "date", "description": "UTC day of the time step"},
  {"name": "n", "type": "int64", "description": "grid cells with a non-null value on that day"},
  {"name": "mean", "type": "double", "description": "unweighted mean over the masked cells"},
  {"name": "mean_wt", "type": "double", "description": "area-weighted mean (partial boundary cells count for their overlap)"},
  {"name": "sd", "type": "double", "description": "sample standard deviation"},
  {"name": "min", "type": "double", "description": "minimum"},
  {"name": "max", "type": "double", "description": "maximum"},
  {"name": "p10", "type": "double", "description": "10th percentile (continuous)"},
  {"name": "p90", "type": "double", "description": "90th percentile (continuous)"},
  {"name": "weight_sum", "type": "double", "description": "sum of the cell area weights contributing to the day"},
  {"name": "place_id", "type": "string", "description": "gazetteer place id, e.g. NMS:HIHWNMS"},
  {"name": "dataset_id", "type": "string", "description": "ERDDAP datasetID, e.g. dhw_5km"},
  {"name": "variable", "type": "string", "description": "ERDDAP variable, e.g. CRW_SST"},
]
CATEGORICAL_COLUMNS = [
  {"name": "date", "type": "date", "description": "UTC day of the time step"},
  {"name": "class", "type": "int64", "description": "the class value (see the dataset collection's erddap-places:classes)"},
  {"name": "n", "type": "int64", "description": "grid cells in that class on that day"},
  {"name": "weight", "type": "double", "description": "sum of the class cells' area weights"},
  {"name": "frac_area", "type": "double", "description": "area-weighted share of the place in that class; sums to 1 over a date (the SQL template calls this `fraction`)"},
  {"name": "pct_cells", "type": "double", "description": "unweighted percent of masked cells in that class (the SQL template calls this `percent_cells`)"},
  {"name": "place_id", "type": "string", "description": "gazetteer place id, e.g. NMS:HIHWNMS"},
  {"name": "dataset_id", "type": "string", "description": "ERDDAP datasetID, e.g. dhw_5km"},
  {"name": "variable", "type": "string", "description": "ERDDAP variable, e.g. CLASS"},
]


# -- names --------------------------------------------------------------------------------------------------------------
def item_id(dataset_id: str, variable: str, place_id: str) -> str:
  """`itemId()`: `<dataset>_<variable>_<place with every ':' as '-'>`."""
  return f"{dataset_id}_{variable}_{place_id.replace(':', '-')}"


def stats_rel(dataset_id: str, variable: str, place_id: str) -> str:
  """The stats file under `stats/`: `<dataset>/<variable>/<place_id>.parquet` (the colon kept, `fileSafe()`)."""
  return f"{dataset_id}/{variable}/{place_id}.parquet"


def stats_href(dataset_id: str, variable: str, place_id: str) -> str:
  """`statsHref()`: `./<dataset>/<variable>/<place>.parquet`, the place URI-encoded but its colon kept."""
  return f"./{dataset_id}/{variable}/{quote(place_id, safe=_URI_SAFE)}.parquet".replace("%3A", ":").replace("%3a", ":")


def multihash(path: str | Path) -> str:
  """The multihash (sha2-256) of a file, as portolan writes `file:checksum`: `1220` + the hex digest."""
  return "1220" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def file_stats(path: str | Path | None) -> dict:
  """`file:size` and `file:checksum` of a file that exists; `{}` otherwise."""
  if path is None or not Path(path).is_file():
    return {}
  return {"file:size": Path(path).stat().st_size, "file:checksum": multihash(path)}


# -- geometry -----------------------------------------------------------------------------------------------------------
def union_bbox(boxes: list[tuple[float, float, float, float]]) -> list[float]:
  """`unionBbox()`: boxes unioned round the globe; crosses +-180 (west > east) when the widest gap is elsewhere."""
  if not boxes:
    raise ValueError("union_bbox: no boxes")
  s, n = min(b[1] for b in boxes), max(b[3] for b in boxes)
  spans = sorted([sp for w, _, e, _ in boxes for sp in ([[w, e]] if w <= e else [[w, 180], [-180, e]])],
                 key=lambda sp: sp[0])
  merged: list[list[float]] = []
  for a, b in spans:
    if merged and a <= merged[-1][1]:
      merged[-1][1] = max(merged[-1][1], b)
    else:
      merged.append([a, b])
  w, e = merged[0][0], merged[-1][1]
  gap = w + 360 - e
  for i in range(len(merged) - 1):
    g = merged[i + 1][0] - merged[i][1]
    if g > gap:
      gap, w, e = g, merged[i + 1][0], merged[i][1]
  return [w, s, e, n]


def lobe_box_geometry(geometry: Any, place_id: str = "") -> dict:
  """`lobeBoxGeometry()`: a MultiPolygon of the place's lobe bboxes (one box per side of +-180)."""
  boxes = [lb.bbox for lb in place_lobes(geometry, place_id)]
  return {"type": "MultiPolygon",
          "coordinates": [[[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]] for x0, y0, x1, y1 in boxes]}


def place_bbox(geometry: Any, place_id: str = "") -> list[float]:
  """`placeBbox()`: `union_bbox()` of the place's lobes."""
  return union_bbox([lb.bbox for lb in place_lobes(geometry, place_id)])


# -- the dataset's collection -------------------------------------------------------------------------------------------
def dataset_via(collection: dict | None) -> dict | None:
  """`datasetVia()`: the collection's `rel: via` text/html link as `{href, title}` (title the dataset id when absent)."""
  for lk in (collection or {}).get("links") or []:
    if lk.get("rel") == "via" and lk.get("type") == "text/html":
      ds = (collection or {}).get("erddap:dataset_id") or (collection or {}).get("id")
      return {"href": lk["href"], "title": lk.get("title") or ds}
  return None


def record_start(prov: dict, collection: dict | None = None) -> str | None:
  """`recordStart()`: the provenance's `record_start` (its day), else the collection's temporal start."""
  if prov.get("record_start"):
    return str(prov["record_start"])[:10]
  try:
    start = (collection or {})["extent"]["temporal"]["interval"][0][0]
  except (KeyError, IndexError, TypeError):
    return None
  return str(start)[:10] if start else None


def item_status(prov: dict, collection: dict | None = None) -> str | None:
  """`itemStatus()`: `complete` when the file starts at the record start, `partial` when later, None when unknown."""
  first = record_start(prov, collection)
  if not first:
    return None
  return "complete" if prov["start_datetime"][:10] <= first else "partial"


# -- the item -----------------------------------------------------------------------------------------------------------
def build_item(prov: dict, geometry: Any, place_name: str, dataset_title: str, *, coll: str | None = None,
               collection: dict | None = None, stats_root: str | Path | None = None) -> dict:
  """The stats Item of one provenance (`buildItem()`). `geometry` is the place's (WKB, GeoJSON or shapely), `coll`
  its gazetteer collection (default `places`), `collection` the dataset's STAC collection (its `via` link and temporal
  start), `stats_root` the local `stats/` folder holding the files (for their `file:size` and `file:checksum`)."""
  dsid, var, pid = prov["dataset_id"], prov["variable"], prov["place_id"]
  href = stats_href(dsid, var, pid)
  rel = "../" + href[2:]
  parquet = Path(stats_root) / stats_rel(dsid, var, pid) if stats_root is not None else None
  status = item_status(prov, collection)
  first = record_start(prov, collection)
  coll = coll or PLACES_COLLECTION
  assets = {
    "data": {"href": rel, "type": "application/vnd.apache.parquet", "title": f"{var} statistics for {place_name}",
             "roles": ["data"], "table:columns": CATEGORICAL_COLUMNS if prov["categorical"] else DAILY_COLUMNS,
             **file_stats(parquet)},
    "provenance": {"href": rel[: -len(".parquet")] + ".provenance.json", "type": "application/json",
                   "title": "the griddap URLs, mask size and SQL this file was built from", "roles": ["metadata"],
                   **file_stats(parquet.with_name(parquet.name[: -len(".parquet")] + ".provenance.json")
                                if parquet is not None else None)},
  }
  kind = "per-class composition" if prov["categorical"] else "daily statistics"
  props = {
    "title": f"{place_name} — {var} ({dsid})",
    "description": f"Precomputed {kind} of {var} from {dataset_title} over {place_name} ({pid}), "
                   f"{prov['start_date']} to {prov['end_date']}.",
    "datetime": None, "start_datetime": prov["start_datetime"], "end_datetime": prov["end_datetime"],
    "created": prov["generated"], "table:row_count": prov["rows"], "gazetteer:collection": coll,
    "erddap-places:place_id": pid, "erddap-places:dataset_id": dsid, "erddap-places:variable": var,
    **({"erddap-places:status": status} if status else {}),
    **({"erddap-places:record_start": first} if first else {}),
    "erddap-places:categorical": prov["categorical"], "erddap-places:mask_cells": prov["mask_cells"],
    "erddap-places:lobes": prov["lobes"],
    **({"erddap-places:griddap_urls": prov["griddap_urls"]} if prov.get("griddap_urls") else {}),
    "erddap-places:griddap_url_templates": prov["griddap_url_templates"],
    "erddap-places:griddap_requests": prov["griddap_requests"],
  }
  via = dataset_via(collection)
  links = [
    {"rel": "root", "href": "../../catalog.json", "type": "application/json"},
    {"rel": "parent", "href": "../collection.json", "type": "application/json"},
    {"rel": "collection", "href": "../collection.json", "type": "application/json"},
    {"rel": "related", "href": f"../../erddap/{dsid}/collection.json", "type": "application/json", "title": dataset_title},
    {"rel": "related", "href": f"../../{coll}/collection.json", "type": "application/json",
     "title": PLACES_COLLECTION_TITLE if coll == PLACES_COLLECTION else coll},
    *([{**via, "rel": "via", "type": "text/html"}] if via else []),
  ]
  return {"type": "Feature", "stac_version": STAC_VERSION, "stac_extensions": [EXT_TABLE, EXT_FILE],
          "id": item_id(dsid, var, pid), "collection": "stats", "geometry": lobe_box_geometry(geometry, pid),
          "bbox": place_bbox(geometry, pid), "properties": props, "assets": assets, "links": links}


def item_text(item: dict) -> str:
  """The Item file's text as erddap-places writes it: `JSON.stringify(item, null, 2) + '\\n'`."""
  return js_json(item, indent=2) + "\n"


def write_stats(x: Any, stats_root: str | Path, geometry: Any, place_name: str, *, coll: str | None = None,
                generated: str | None = None) -> dict:
  """Write a chunked extraction's three `stats/` files under `stats_root`: the parquet (copied from `x.stats_path`
  unless it is already there), its provenance and the Item, in that order (the Item records the others' sizes and
  checksums). Returns `{"parquet", "provenance", "item"}` paths (the upload order)."""
  import shutil
  from om_extract.engine import provenance_text, stats_provenance
  root = Path(stats_root)
  parquet = root / stats_rel(x.dataset.dataset_id, x.variable, x.place_id)
  parquet.parent.mkdir(parents=True, exist_ok=True)
  if x.stats_path is None:
    import pyarrow.parquet as pq
    pq.write_table(x.stats, parquet, compression="zstd")
  elif Path(x.stats_path).resolve() != parquet.resolve():
    shutil.copyfile(x.stats_path, parquet)
  prov = stats_provenance(x, bytes_=parquet.stat().st_size, generated=generated)
  prov_path = parquet.with_name(parquet.name[: -len(".parquet")] + ".provenance.json")
  prov_path.write_text(provenance_text(prov))
  item = build_item(prov, geometry, place_name, x.dataset.title, coll=coll, collection=x.collection, stats_root=root)
  item_path = root / "items" / f"{item['id']}.json"
  item_path.parent.mkdir(parents=True, exist_ok=True)
  item_path.write_text(item_text(item))
  return {"parquet": parquet, "provenance": prov_path, "item": item_path}
