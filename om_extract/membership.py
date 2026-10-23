"""Place x H3 membership from the gazetteer: the cells of a place at one resolution, each with its share.

The gazetteer (`oceanmetrics/places`, `gazetteer/membership.py`) publishes, per place collection `<slug>`:

- `<base>/<slug>/h3/membership.json`: the manifest (`res`, `parent_res`, `files` with `res`, `parent`, `path`,
  `parents`, `min_share`); paths are relative to `<base>/<slug>/`;
- `h3/res=<r>/membership.parquet` for an unsplit resolution (1 to 5), `h3/res=<r>/p=<parent>/membership.parquet`
  for a resolution split by parent (6 and 7, by the parents at res 2 and 3), columns `place_id, h3 (string),
  share (float32)`, sorted by place_id then h3;
- `h3/parents.parquet` (`place_id, res, parent`): which split files hold each place, so a reader opens only those.

A cell is kept when its share is at least the manifest's `min_share` (0.01) or it holds that share of the place's own
area. `load_membership()` reads the manifest, then the one file (or the place's parent partitions) with a
`place_id =` filter, in DuckDB, which reads local paths and `https` URLs alike (by range requests over HTTP).
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa

from om_extract.readers.h3 import default_fetch, join_path
from om_extract.stats import lit

GAZETTEER = "https://storage.oceanmetrics.io/gazetteer"
SCHEMA = pa.schema([("place_id", pa.string()), ("h3", pa.string()), ("share", pa.float32())])


def membership_root(slug: str, base: str | Path = GAZETTEER) -> str:
  """`<base>/<slug>`: where the manifest's paths resolve."""
  return join_path(base, slug)


def membership_manifest(slug: str, base: str | Path = GAZETTEER, *,
                        fetch: Callable[[str], bytes | None] | None = None) -> dict:
  """The `h3/membership.json` of a place collection; raises FileNotFoundError naming it when absent."""
  url = join_path(membership_root(slug, base), "h3/membership.json")
  data = (fetch or default_fetch)(url)
  if data is None:
    raise FileNotFoundError(f"no H3 membership manifest at {url}")
  return json.loads(data)


def _rows(con: Any, sql: str, params: list) -> pa.Table:
  rel = con.sql(sql, params=params)
  return rel.to_arrow_table() if hasattr(rel, "to_arrow_table") else rel.fetch_arrow_table()


def membership_files(manifest: dict, place_id: str, res: int, root: str, *, con: Any = None) -> list[str]:
  """The files that hold `place_id` at `res`: the one file of an unsplit resolution; for a split one, the partitions
  of the place's parents from `parents.parquet` (no file when the place has no cells there)."""
  if res not in manifest.get("res", []):
    raise ValueError(f"no H3 membership at res {res}; the manifest has {manifest.get('res')}")
  files = [f for f in manifest.get("files", []) if f.get("res") == res]
  if str(res) not in (manifest.get("parent_res") or {}):
    whole = [f["path"] for f in files if f.get("parent") is None] or [f"h3/res={res}/membership.parquet"]
    return [join_path(root, p) for p in whole]
  import duckdb
  own = con is None
  c = duckdb.connect(":memory:") if own else con
  try:
    src = join_path(root, manifest.get("parents") or "h3/parents.parquet")
    t = _rows(c, f"SELECT DISTINCT parent FROM read_parquet({lit(src)}) WHERE place_id = ? AND res = ? "
                 "ORDER BY parent", [place_id, res])
  finally:
    if own:
      c.close()
  by_parent = {f.get("parent"): f["path"] for f in files}
  return [join_path(root, by_parent.get(p, f"h3/res={res}/p={p}/membership.parquet"))
          for p in t["parent"].to_pylist()]


def load_membership(slug: str, place_id: str, res: int, base: str | Path = GAZETTEER, *,
                    fetch: Callable[[str], bytes | None] | None = None, con: Any = None) -> pa.Table:
  """The membership rows of one place at one resolution, `place_id, h3, share` (SCHEMA), sorted by `h3`.

  `base` is the gazetteer root (a URL or a directory); `slug` the place collection (`places`, `mr_eez` ...). The
  manifest is read with `fetch` (default: a local read or an HTTP GET); the Parquet files with DuckDB (`con`, or a
  new in-memory connection). A place with no cells at `res` gives an empty table.
  """
  root = membership_root(slug, base)
  manifest = membership_manifest(slug, base, fetch=fetch)
  import duckdb
  own = con is None
  c = duckdb.connect(":memory:") if own else con
  try:
    files = membership_files(manifest, place_id, int(res), root, con=c)
    if not files:
      return SCHEMA.empty_table()
    srcs = "[" + ", ".join(lit(f) for f in files) + "]"
    t = _rows(c, f"SELECT place_id, h3, share FROM read_parquet({srcs}) WHERE place_id = ? ORDER BY h3", [place_id])
  finally:
    if own:
      c.close()
  return t.cast(SCHEMA)
