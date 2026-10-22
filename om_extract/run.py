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


def run(collection: str | Path, place_id: str, geometry: str | Path, var: str, t0: str, t1: str, out: str | Path, *,
        stat: str = "mean_wt", coll: str | None = None, slab_dir: str | Path | None = None,
        fetch: Fetch | None = None) -> dict:
  """Extract and write `<out>/stats.parquet`, `<out>/provenance.json` and the values file
  `<out>/values/indicator=<id>/coll=<slug>/data.parquet`. Raises `contract.ContractError` when the rows or the written
  file break the contract. Returns `{"stats": path, "provenance": path, "values": path, "rows": n, "indicator": id,
  "coll": slug}`."""
  if slab_dir is not None:
    fetch = slab_dir_fetch(slab_dir)
  # the collection itself is read from its path or URL (never from the slab directory)
  x = replace(extract(load_collection(collection), place_id, read_geometry(geometry), var, t0, t1, fetch=fetch,
                      coll=coll), collection_href=str(collection))
  out = Path(out)
  out.mkdir(parents=True, exist_ok=True)
  stats_path = out / "stats.parquet"
  pq.write_table(x.stats, stats_path, compression="zstd")
  prov_path = out / "provenance.json"
  prov_path.write_text(json.dumps(stats_provenance(x, bytes_=stats_path.stat().st_size), indent=2) + "\n")
  rows = values_rows(x, stat=stat)
  path = contract.write_values(rows, out, provenance_for(x, __version__))
  errors = contract.validate_file(path)
  if errors:
    raise contract.ContractError(errors)
  return {"stats": stats_path, "provenance": prov_path, "values": path, "rows": len(rows),
          "indicator": rows[0]["indicator"], "coll": rows[0]["coll"]}
