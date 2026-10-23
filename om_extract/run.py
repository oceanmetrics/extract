"""`om-extract run`: one place x variable x window, end to end, written to a directory.

`run()` calls `engine.extract()`, writes the stats table and its provenance as erddap-places does, turns the stats into
contract rows (`values.values_rows()`, `values.provenance_for()`), writes them with `contract.write_values()` and
validates the file with `contract.validate_file()`. The CLI only parses arguments and calls it.

Offline runs (`slab_dir`): each request URL is served from `<slab_dir>/<sha1 of the URL>.parquet` (`slab_file()`: the
hex sha1 of the URL's UTF-8 bytes, exactly as the URL is printed in the provenance). A URL with no such file raises
`ErddapError`, so the time extent falls back to the catalog's and a missing slab names its URL.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from om_extract import __version__, contract
from om_extract.engine import extract, stats_provenance
from om_extract.layers import load_collection
from om_extract.readers.erddap import ErddapError, Fetch
from om_extract.values import values_rows, provenance_for


def slab_file(url: str) -> str:
  """The file name a request URL is served from in a slab directory: `<sha1(url)>.parquet`."""
  return hashlib.sha1(url.encode("utf-8")).hexdigest() + ".parquet"


def slab_dir_fetch(slab_dir: str | Path) -> Fetch:
  """A `fetch(url) -> bytes` that reads `<slab_dir>/<sha1(url)>.parquet` and raises `ErddapError` for a URL it lacks."""
  root = Path(slab_dir)

  def fetch(url: str) -> bytes:
    p = root / slab_file(url)
    if not p.is_file():
      raise ErddapError(f"no file {p.name} in {root} for {url}")
    return p.read_bytes()
  return fetch


def read_geometry(path: str | Path) -> Any:
  """A place geometry from a file: GeoJSON (a geometry, Feature or FeatureCollection) when the file is JSON text,
  else WKB bytes."""
  data = Path(path).read_bytes()
  if data.lstrip()[:1] in (b"{", b"["):
    return json.loads(data)
  return data


def run(collection: str | Path, place_id: str, geometry: str | Path | None, var: str, t0: str | None, t1: str | None,
        out: str | Path, *, stat: str = "mean_wt", coll: str | None = None, slab_dir: str | Path | None = None,
        fetch: Fetch | None = None, chunks: str | Path | None = None, stats_dir: str | Path | None = None,
        base: str | Path | None = None, internal: str | Path | None = None, place_name: str | None = None,
        log: Any = None) -> dict:
  """Extract and write the stats table, its provenance and the values file
  `<out>/values/indicator=<id>/coll=<slug>/data.parquet`. Raises `contract.ContractError` when the rows or the written
  file break the contract.

  `geometry` None reads the place from the gazetteer (`places.place_record()`: the private copy of a reference
  collection when this machine holds one, else `<base>/<coll>/places.parquet`), with its name for the Item.
  `chunks` runs the chunked engine (`engine.extract(..., chunks=)`: the live extent, the whole record unless `t0`/`t1`
  pin it, only missing chunks fetched). `stats_dir` writes erddap-places' `stats/` layout there (`stac.write_stats()`:
  `<dataset>/<variable>/<place>.parquet`, `.provenance.json`, `items/<id>.json`) instead of `<out>/stats.parquet` and
  `<out>/provenance.json`. Returns `{"stats", "provenance", "values", "rows", "indicator", "coll", "item"}`."""
  from om_extract.engine import default_coll
  if slab_dir is not None:
    fetch = slab_dir_fetch(slab_dir)
  coll = coll or default_coll(place_id)
  if geometry is None:
    from om_extract.places import GAZETTEER_BASE, place_record
    rec = place_record(place_id, coll, base or GAZETTEER_BASE, internal)
    geom, name = rec["wkb"], place_name or rec["name"]
  else:
    geom, name = read_geometry(geometry), place_name or place_id
  if chunks is None and (t0 is None or t1 is None):
    raise ValueError("--t0 and --t1 are required without --chunks")
  out = Path(out)
  out.mkdir(parents=True, exist_ok=True)
  stats_path = out / "stats.parquet"
  if stats_dir is not None:
    from om_extract.layers import dataset_of
    from om_extract.stac import stats_rel
    doc = load_collection(collection)
    stats_path = Path(stats_dir) / stats_rel(dataset_of(doc).dataset_id, var, place_id)
  else:
    doc = load_collection(collection)
  # the collection itself is read from its path or URL (never from the slab directory)
  x = replace(extract(doc, place_id, geom, var, t0, t1, fetch=fetch, coll=coll, chunks=chunks,
                      stats_path=stats_path if chunks is not None else None, log=log),
              collection_href=str(collection))
  item = None
  if stats_dir is not None:
    from om_extract.stac import write_stats
    files = write_stats(x, stats_dir, geom, name, coll=coll)
    stats_path, prov_path, item = files["parquet"], files["provenance"], files["item"]
  else:
    if chunks is None:
      pq.write_table(x.stats, stats_path, compression="zstd")
    prov_path = out / "provenance.json"
    prov_path.write_text(json.dumps(stats_provenance(x, bytes_=stats_path.stat().st_size), indent=2) + "\n")
  rows = values_rows(x, stat=stat)
  path = None
  if rows:            # a place with no cell on the grid (the Great Lakes on a sea grid) has stats but no values file
    path = contract.write_values(rows, out, provenance_for(x, __version__))
    errors = contract.validate_file(path)
    if errors:
      raise contract.ContractError(errors)
  return {"stats": stats_path, "provenance": prov_path, "values": path, "rows": len(rows),
          "indicator": rows[0]["indicator"] if rows else None, "coll": coll, "item": item}
