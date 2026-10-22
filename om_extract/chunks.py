"""The chunk store: per-chunk daily rows that make a full-record run incremental, as erddap-places' precompute keeps them.

A place's record is cut into chunks of days (`chunk_days_for()`: at most 90 steps or 100,000 values per lobe request,
sized by the largest lobe for all lobes). Each chunk's slab is downloaded per lobe, aggregated with the stats SQL into
that chunk's daily rows, written to the store and the slab deleted. The next run fetches only the chunks it lacks.
The layout is the runner's (`precompute/src/stats.ts:357-360`, `PRECOMPUTE_CHUNKS`):

    <root>/<dataset_id>/<variable>/<place_id>/<mask_key>/<start>_<end>.parquet

The place id keeps its colon (`fileSafe()`, `COLON_IN_KEYS = true`). `mask_key()` is the TS `maskKey()` (`:289-294`)
character for character, so a key computed here names the same directory the runner writes. `plan_chunks()` is
`planChunks()` (`:166-190`): which done chunks to keep, which slices to fetch, which files to drop and which short last
chunk a refetch supersedes.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from om_extract._js import js_json
from om_extract.readers.erddap import add_days, chunks, day, days_between, js_num

_SPAN = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.parquet$")
# place ids keep their colon in a path (stats.ts COLON_IN_KEYS)
COLON_IN_KEYS = True


def file_safe(place_id: str) -> str:
  """A place id as a path segment (`fileSafe()`): verbatim, colon included, while `COLON_IN_KEYS`."""
  return place_id if COLON_IN_KEYS else place_id.replace(":", "_")


def chunk_name(start: str, end: str) -> str:
  """`<start>_<end>.parquet`, the file of one chunk."""
  return f"{start}_{end}.parquet"


def chunk_span(name: str) -> tuple[str, str] | None:
  """A chunk file name back to its `(start, end)` days, or None for any other file (`chunkSpan()`)."""
  m = _SPAN.match(name)
  return (m.group(1), m.group(2)) if m else None


def window(start: str, end: str | None, days: int | str = "all") -> tuple[str, str]:
  """The window of a run (`window(ext, days)`, stats.ts:117-123): `(day(start), day(end))` for `'all'`, else the last
  `days` days ending at the extent's end, never before its start. `start`/`end` are the live extent's instants."""
  if end is None:
    raise ValueError("the extent has no end: the live extent is required for a window")
  e = day(end)
  if days == "all" or not isinstance(days, int):
    return (day(start), e)
  s = add_days(e, -(days - 1))
  if days_between(day(start), s) < 0:
    s = day(start)
  return (s, e)


@dataclass(frozen=True)
class ChunkPlan:
  """What `plan_chunks()` decided: `keep` the done chunks inside the window (oldest first), `todo` the slices to fetch,
  `drop` the files outside the window or not chunk files, `supersede` a short last chunk to delete once its refetch is
  written (or None)."""
  keep: list[tuple[str, str]] = field(default_factory=list)
  todo: list[tuple[str, str]] = field(default_factory=list)
  drop: list[str] = field(default_factory=list)
  supersede: str | None = None

  @property
  def slices(self) -> list[tuple[str, str]]:
    """keep + todo, oldest first: the chunks whose rows make the file (`slices`, stats.ts:362)."""
    return sorted(self.keep + self.todo, key=lambda x: x[0])


def plan_chunks(existing: Iterable[str], win: Sequence[str], chunk_days: int) -> ChunkPlan:
  """Which chunks to fetch for the window `(start, end)`, given the names of the chunk files already done
  (`planChunks()`, stats.ts:166-190).

  - every done chunk inside the window is kept, whatever size it was cut at (a change of chunk size never throws
    finished work away); overlapping chunks are both kept (the merge is DISTINCT);
  - a short last chunk (fewer than `chunk_days` days, ending before the window does, and not covered by another kept
    chunk) is fetched again from its own start to the new end and named in `supersede`;
  - the gaps between kept chunks are cut into `chunk_days` slices (`todo`);
  - files outside the window, or not chunk files, are in `drop`.

  `existing` is read in sorted name order (the runner's `readdirSync` on APFS lists names sorted), then stably sorted
  by start as the TS does."""
  start, end = win
  names = sorted(existing)

  def in_win(s: str, e: str) -> bool:
    return days_between(start, s) >= 0 and days_between(e, end) >= 0
  spans = [(f, chunk_span(f)) for f in names]
  drop = [f for f, sp in spans if not sp or not in_win(*sp)]
  keep = sorted((sp for _, sp in spans if sp and in_win(*sp)), key=lambda x: x[0])
  supersede = None
  if keep:
    ls, le = keep[-1]
    if (days_between(le, end) > 0 and days_between(ls, le) + 1 < chunk_days
        and not any(days_between(le, e) >= 0 for _, e in keep[:-1])):
      supersede = chunk_name(ls, le)
      keep = keep[:-1]
  todo: list[tuple[str, str]] = []
  frm = start
  stop = add_days(end, 1)
  for s, e in [*keep, (stop, stop)]:
    if days_between(frm, s) > 0:
      todo += chunks(frm, add_days(s, -1), chunk_days)
    if days_between(frm, add_days(e, 1)) > 0:
      frm = add_days(e, 1)
  return ChunkPlan(keep=keep, todo=todo, drop=drop, supersede=supersede)


def mask_key(base_url: str, dataset_id: str, depth: float | None, variable: str, value_expr: str, template: str,
             cells: Iterable[tuple[float, float, float]]) -> str:
  """The 12-hex key of what a chunk's rows depend on besides its dates (`maskKey()`, stats.ts:289-294): the sha1 over
  `JSON.stringify([baseUrl, datasetId, depth ?? null, name, valueExpr, statsTemplate])` and then, per mask cell in
  mask order, `${lon},${lat},${weight};` with JavaScript's number printing. `cells` are `(lon, lat, weight)`, the
  longitude in the dataset's frame."""
  h = hashlib.sha1(js_json([base_url, dataset_id, depth, variable, value_expr, template]).encode("utf-8"))
  for lon, lat, w in cells:
    h.update(f"{js_num(float(lon))},{js_num(float(lat))},{js_num(float(w))};".encode("utf-8"))
  return h.hexdigest()[:12]


class ChunkStore:
  """A chunk store rooted at `root` (the runner's `PRECOMPUTE_CHUNKS`), with the slab cache beside it in
  `<root>/.slabs` (the runner's `precompute/.cache`)."""

  def __init__(self, root: str | Path):
    self.root = Path(root)

  def dir_for(self, dataset_id: str, variable: str, place_id: str, key: str) -> Path:
    """`<root>/<dataset_id>/<variable>/<place_id>/<key>`."""
    return self.root / dataset_id / variable / file_safe(place_id) / key

  def slab_dir(self, dataset_id: str, variable: str, place_id: str) -> Path:
    """Where a chunk's slabs wait for their aggregation (`CACHE/<dataset>/<variable>/<place>`)."""
    return self.root / ".slabs" / dataset_id / variable / file_safe(place_id)

  @staticmethod
  def listing(d: Path) -> list[str]:
    """The `.parquet` names in a chunk directory, sorted (a missing directory is empty)."""
    return sorted(f for f in os.listdir(d) if f.endswith(".parquet")) if d.is_dir() else []


def write_atomic(path: Path, data: bytes) -> None:
  """Write `data` to `<path>.tmp` and rename it over `path`: only a whole file ever counts as done."""
  path.parent.mkdir(parents=True, exist_ok=True)
  tmp = path.with_name(path.name + ".tmp")
  tmp.write_bytes(data)
  os.replace(tmp, path)
