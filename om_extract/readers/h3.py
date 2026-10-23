"""The H3 reader: a layer whose native grid is H3 (obis-h3, the hex twins of gridded layers, BioTrack), read by cell.

An H3 layer is summarised by joining its cells with the place's H3 membership (`engine_h3.extract_h3()`); it has no
lat/lon axes, so `axes()`, `slab()` and `slice()` raise and `cells()` is the read. The rules, each a test:

- **Collection**: a STAC Collection that passes the platform `h3` profile (`h3:resolutions`, `h3:cell_column`,
  `h3:cell_type`, `h3:layout.template` with `{res}`, `table:columns`), validated first (`InvalidProfile`).
- **Paths**: a file path is the layout's `template` with `{res}` (and any `h3:layout.keys`, given as `keys=`) filled,
  relative to `root` (default: the directory of the collection's `rel: self` link). A resolution named in
  `parent_res` with a `parent_template` is split by parent cell: only the partitions `p=<parent>` of the cells asked
  are read, the parent being each cell's ancestor at `parent_res[res]` (`h3_parent()`, bit arithmetic on the index,
  no h3 library). Any other resolution is one whole file. A partition or file that does not exist holds no rows.
- **Periods**: with `h3:periods`, a layer has one time step per period value. When the template holds the periods'
  placeholder, one file per period is read and a `period` column added; otherwise the files carry a column named by
  the placeholder, read as `period`. `period_date()` turns a label (`1990s`, `2005`, `2005-03`) into the first day of
  the period for the grain (`decade`, `year`, `month`; `all` is null).
- **Cells**: `cells(res, h3_ids)` returns the files' rows for those cells (all rows when `h3_ids` is None and the
  resolution is not split); a `string` cell column is matched as lowercase hex, a `uint64` one as the integer index.
- **Fetch**: `fetch(path_or_url) -> bytes | None` is injectable (None means absent); the default reads a local path or
  GETs a URL with httpx (404 is absent).
"""
from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from om_extract.readers.base import Axes, BBox, Extent
from om_extract.readers.profile import check_profile

H3Fetch = Callable[[str], "bytes | None"]
FETCH_TIMEOUT_S = 120
_RES_SHIFT = 52
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def is_h3_collection(doc: dict) -> bool:
  """A collection declaring `h3:resolutions`: what `readers.for_collection` hands to `H3Reader`."""
  return "h3:resolutions" in doc


def h3_int(cell: str | int) -> int:
  """An H3 index as an integer (from its hex string, or as is)."""
  return cell if isinstance(cell, int) else int(str(cell), 16)


def h3_str(cell: int) -> str:
  """An H3 index as its lowercase hex string (15 digits for a cell)."""
  return format(int(cell), "x")


def h3_res(cell: str | int) -> int:
  """The resolution of an H3 index (bits 52 to 55)."""
  return (h3_int(cell) >> _RES_SHIFT) & 0xF


def h3_parent(cell: str | int, res: int) -> str:
  """The ancestor of a cell at a coarser `res`, as a hex string: the resolution field set to `res` and the digits
  below it set to 7 (what `h3.cell_to_parent` gives, and the gazetteer's `parent_u64`)."""
  x = h3_int(cell)
  own = (x >> _RES_SHIFT) & 0xF
  if not 0 <= res <= own:
    raise ValueError(f"parent resolution {res} is not coarser than the cell's ({own}): {h3_str(x)}")
  x = (x & ~(0xF << _RES_SHIFT)) | (res << _RES_SHIFT) | ((1 << (3 * (15 - res))) - 1)
  return h3_str(x)


def period_date(grain: str, label: str | None) -> dt.date | None:
  """The first day of a period label for its grain: `1990s` for a decade, `2005` for a year, `2005-03` for a month (a
  label `yyyy-mm-dd` is read as its date); None for `all` or no label. Raises ValueError for any other label."""
  if grain == "all" or label is None:
    return None
  s = str(label)
  m = re.fullmatch(r"(\d{4})s", s) or re.fullmatch(r"(\d{4})", s)
  if m and grain in ("decade", "year"):
    y = int(m.group(1))
    return dt.date(y - y % 10, 1, 1) if grain == "decade" else dt.date(y, 1, 1)
  m = re.fullmatch(r"(\d{4})-(\d{2})(?:-\d{2})?", s)
  if m and grain == "month":
    return dt.date(int(m.group(1)), int(m.group(2)), 1)
  raise ValueError(f"period {s!r} is not a {grain} label (1990s, 2005, 2005-03)")


def default_fetch(path_or_url: str) -> bytes | None:
  """The bytes of a local file or a URL; None when it does not exist (a missing file, an HTTP 404)."""
  if path_or_url.startswith(("http://", "https://")):
    import httpx
    r = httpx.get(path_or_url, follow_redirects=True, timeout=FETCH_TIMEOUT_S)
    if r.status_code == 404:
      return None
    r.raise_for_status()
    return r.content
  p = Path(path_or_url.removeprefix("file://"))
  return p.read_bytes() if p.is_file() else None


def _self_dir(doc: dict) -> str | None:
  href = next((lk["href"] for lk in doc.get("links") or [] if lk.get("rel") == "self" and lk.get("href")), None)
  if href is None:
    return None
  return href.rsplit("/", 1)[0] if "/" in href else "."


def join_path(root: str | Path, rel: str) -> str:
  """`rel` under `root` (a URL or a directory)."""
  r = str(root)
  if "://" in r:
    return r.rstrip("/") + "/" + rel.removeprefix("./")
  return str(Path(r) / rel)


class H3Reader:
  """The reader for an `h3` collection. `resolution` is the default resolution of `cells()` (default: the finest
  published); `root` where the templates resolve (default: the `rel: self` directory); `keys` the values of the
  layout's other placeholders (`{"eov": "sst"}`); `fetch` serves every file (see the module docstring)."""
  kind = "h3"

  def __init__(self, collection: dict, *, resolution: int | None = None, fetch: H3Fetch | None = None,
               root: str | Path | None = None, keys: dict[str, str] | None = None, validate: bool = True):
    if validate:
      check_profile("h3", collection)
    self.collection = collection
    self.resolutions = list(collection["h3:resolutions"])
    self.resolution = max(self.resolutions) if resolution is None else int(resolution)
    if self.resolution not in self.resolutions:
      raise ValueError(f"{collection['id']} has no resolution {self.resolution}; it has {self.resolutions}")
    self.cell_column = collection["h3:cell_column"]
    self.cell_type = collection["h3:cell_type"]
    self.layout = collection["h3:layout"]
    self.periods = collection.get("h3:periods")
    self.columns = {c["name"]: c for c in collection["table:columns"]}
    self.root = str(root) if root is not None else _self_dir(collection)
    if self.root is None:
      raise ValueError(f"{collection['id']}: no root for the layout (pass root=, or give the collection a self link)")
    self.keys = dict(keys or {})
    self._fetch = fetch or default_fetch
    self.files_read: list[str] = []

  # the layout ----
  @property
  def grain(self) -> str:
    """The time grain of a step: the periods' grain, else `all`."""
    return (self.periods or {}).get("grain") or "all"

  def parent_res(self, res: int) -> int | None:
    """The parent resolution `res` is split by, or None for a whole file."""
    if not self.layout.get("parent_template"):
      return None
    p = (self.layout.get("parent_res") or {}).get(str(res))
    if p is not None and p >= res:
      raise ValueError(f"parent_res {p} of res {res} is not coarser")
    return p

  def _fill(self, template: str, values: dict[str, Any]) -> str:
    def sub(m: re.Match) -> str:
      k = m.group(1)
      if k not in values:
        raise ValueError(f"template {template!r} needs {{{k}}}: pass keys={{{k!r}: ...}}")
      return str(values[k])
    return _PLACEHOLDER.sub(sub, template)

  def _period_in_path(self, template: str) -> bool:
    return bool(self.periods) and "{" + self.periods["placeholder"] + "}" in template

  def paths(self, res: int, h3_ids: Iterable[str | int] | None = None) -> list[tuple[str, str | None]]:
    """The files to read for `res` (and the cells, when the resolution is split by parent), each with its period
    label when the period is in the path (else None), in parent then period order."""
    if res not in self.resolutions:
      raise ValueError(f"{self.collection['id']} has no resolution {res}; it has {self.resolutions}")
    pres = self.parent_res(res)
    if pres is None:
      template, parents = self.layout["template"], [None]
    else:
      if h3_ids is None:
        raise ValueError(f"res {res} is split by parent (res {pres}): pass the cells to read")
      template = self.layout["parent_template"]
      parents = sorted({h3_parent(c, pres) for c in h3_ids})
    periods = self.periods["values"] if self._period_in_path(template) else [None]
    out = []
    for p in parents:
      for per in periods:
        vals = {**self.keys, "res": res}
        if p is not None:
          vals["parent"] = p
        if per is not None:
          vals[self.periods["placeholder"]] = per
        out.append((join_path(self.root, self._fill(template, vals)), per))
    return out

  def _match(self, ids: Iterable[str | int], typ: pa.DataType) -> pa.Array:
    if self.cell_type == "uint64":
      return pa.array([h3_int(c) for c in ids], pa.uint64()).cast(typ)
    return pa.array([h3_str(h3_int(c)) for c in ids], pa.string()).cast(typ)

  def cells(self, res: int | None = None, h3_ids: Iterable[str | int] | None = None) -> pa.Table:
    """The layer's rows for the cells `h3_ids` at `res` (default `self.resolution`): every column of the files, plus
    `period` (a string) when the layer has periods. All rows when `h3_ids` is None (a whole-file resolution only)."""
    res = self.resolution if res is None else int(res)
    ids = None if h3_ids is None else list(h3_ids)
    parts = []
    for path, per in self.paths(res, ids):
      data = self._fetch(path)
      if data is None:
        continue
      self.files_read.append(path)
      t = pq.read_table(pa.BufferReader(data))
      if self.cell_column not in t.column_names:
        raise ValueError(f"{path} has no cell column {self.cell_column!r}; it has {t.column_names}")
      if ids is not None:
        col = t[self.cell_column]
        if self.cell_type == "string":
          col = pc.utf8_lower(col)
        t = t.filter(pc.is_in(col, value_set=self._match(ids, col.type)))
      if self.periods:
        if per is not None:
          t = t.append_column("period", pa.array([per] * t.num_rows, pa.string()))
        else:
          ph = self.periods["placeholder"]
          if ph not in t.column_names:
            raise ValueError(f"{path}: the periods are neither in the path nor in a column {ph!r}")
          t = t.set_column(t.column_names.index(ph), "period", t[ph].cast(pa.string()))
      parts.append(t)
    if not parts:
      return pa.table({self.cell_column: pa.array([], pa.string() if self.cell_type == "string" else pa.uint64())})
    return pa.concat_tables(parts, promote_options="permissive")

  # the interface ----
  def extent(self) -> Extent:
    """The periods' first and last day as the time range (none without periods), the collection's spatial bbox."""
    start = end = None
    if self.periods and self.grain != "all":
      ds = [period_date(self.grain, v) for v in self.periods["values"]]
      start, end = min(ds).isoformat(), max(ds).isoformat()
    sb = (((self.collection.get("extent") or {}).get("spatial") or {}).get("bbox") or [None])[0]
    bbox = tuple(float(v) for v in sb[:4]) if sb and len(sb) >= 4 else None
    return Extent(time_start=start, time_end=end, bbox=bbox)

  def _no_axes(self, *_, **__):
    raise NotImplementedError("an H3 layer has cells, not lat/lon axes: read it with cells() and summarise it with "
                              "engine_h3.extract_h3() over the place's H3 membership")

  def axes(self, bbox: BBox, depth: float | None = None) -> Axes:
    """Not defined for an H3 layer (raises NotImplementedError)."""
    self._no_axes()

  def slab(self, bbox: BBox, t0: str, t1: str, var: str) -> pa.Table:
    """Not defined for an H3 layer (raises NotImplementedError)."""
    self._no_axes()

  def slice(self, bbox: BBox, t: str, var: str) -> pa.Table:
    """Not defined for an H3 layer (raises NotImplementedError)."""
    self._no_axes()

  def source_block(self) -> dict:
    """The `h3:*` keys of the collection, its `table:columns`, the root and the files read so far."""
    out = {k: v for k, v in self.collection.items() if k.startswith("h3:")}
    out["table:columns"] = self.collection["table:columns"]
    out["h3:root"] = self.root
    out["h3:files"] = list(self.files_read)
    return out
