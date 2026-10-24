"""The stats STAC Collection `gazetteer/stats/collection.json` and its thumbnail, rebuilt from the published Items.

erddap-places' `buildCollection()`, `writeThumbnail()` and `listedItems()` (`precompute/src/stac.ts`, erddap-places
ec67849, frozen) in Python. Since 2026-10-24 the engine computes and publishes every stats file and its Item
(`om_extract.stac`, through oceanmetrics/indicators `scripts/stats_unit.sh`), and erddap-places' precompute is off, so
the collection that links the Items is rebuilt here, after the Items, by indicators' daily job.

What is listed (`listed_refs()`), each rule a test:

- the targets are the units (place, gazetteer collection, dataset, variable) of the engine's units list (indicators
  `scripts/parity_units.tsv`); only the units of the `places` collection are listed (`listedItems()`: the daily run's
  places; a requested place of another collection has an Item and a file, but no entry here).
- a unit's Item is the run's own (a unit published in this run) or else the storage host's; a unit with no Item is
  `pending`: no Item link, an entry of `erddap-places:pending` (sorted by id, once each), counted in `item_counts`.
- the order of the item links (`order_refs()`): the previous collection's order, then the Items it did not link, by
  id. (stac.ts put the run's fresh Items first, then the earlier ones by id; the engine publishes unit by unit, so the
  published order is kept and a new Item goes at the end.)

What the collection says (`build_collection()`), each the TS line:

- `status` (`itemStatus()`): `complete` when the Item starts on or before the dataset's first time step (the Item's
  `erddap-places:record_start`, else the dataset collection's temporal start), `partial` when later; unknown: no status
  on the link, counted in neither. `summaries['erddap-places:status']` lists complete, partial when counted.
- the extent: the Items' bboxes unioned round the globe (`unionBbox()`: west > east across +-180), the earliest
  `start_datetime` to the latest `end_datetime` (string order, as the TS sort).
- summaries: the datasets and the variables in the order they first appear, the place ids sorted.
- providers (`datasetProviders()`): every producer / licensor of the datasets' own collections, in dataset order, once
  by name, Ocean Metrics left out; then Ocean Metrics LLC as processor and host, last (PTL-PRV-002).
- links: root, parent, agents, describedby, a `related` per dataset and one to the places collection, the datasets'
  `via` pages (`datasetVia()`), then one `item` link per listed Item with its status.
- the thumbnail asset (`write_thumbnail()`): the 480x240 PNG of the listed places' lobe boxes (the Items' geometry) on
  a graticule, with its `file:size` and `file:checksum`; `updated` the build time (UTC, whole seconds).

`rebuild()` does the I/O (the units, the run's Items, the host's Items, the previous collection, the datasets'
collections) and writes `stats.thumb.png` then `collection.json`; it refuses (raises) rather than write a collection
from a partial read: any host answer but 200 or 404, an Item that is not its unit's, or no Item at all.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import struct
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable

from om_extract._js import js_json
from om_extract.stac import (DAILY_COLUMNS, EXT_FILE, EXT_TABLE, PLACES_COLLECTION, PLACES_COLLECTION_TITLE,
                             STAC_VERSION, file_stats, item_id, item_status, union_bbox)
from om_extract.storage import STORAGE_HOST

EXT_PORTOLAN = "https://schemas.portolan-sdi.org/portolan/v0.2.0/schema.json"
THUMB = "stats.thumb.png"
STATUSES = ("complete", "partial")
OM_PROVIDER = {"name": "Ocean Metrics LLC", "roles": ["processor", "host"], "url": "https://oceanmetrics.io"}
TITLE = "Precomputed place statistics"
DESCRIPTION = (
  "Precomputed statistics of ERDDAP grid variables over the gazetteer places: one Parquet file "
  "per (dataset, variable, place), covering the dataset's full record. A long backfill fills in "
  "over several daily runs, so each item link and Item says how far it has got in "
  "erddap-places:status (complete: from the first time step; partial: a later start, still being "
  "backfilled), and erddap-places:pending lists the targets with no file yet. Each file is "
  "produced by exactly the code the browser app runs live — the place "
  "polygon masks the dataset's own grid (cell centre inside, partial-area weight on the "
  "boundary) and sql/stats_daily.sql or sql/stats_categorical.sql aggregates per day — so a "
  "precomputed row and a live in-browser row agree. Use these when you want the answer now; run "
  "the live query when you want today. An Item's gazetteer:collection names the gazetteer "
  "collection its place came from (places for the 18 sanctuaries listed here); places of other collections "
  "are precomputed on request and have an Item and a file, but no entry in this collection.")
KEYWORDS = ["statistics", "time series", "marine protected areas", "ERDDAP", "griddap", "zonal statistics"]

#: a fetch: the body of a URL, None for a 404; any other answer raises
Fetch = Callable[[str], "bytes | None"]


# -- the units ----------------------------------------------------------------------------------------------------------
def read_units(path: str | Path) -> list[dict]:
  """The units of a TSV (place, coll, dataset, variable[, cadence ...]) as dicts; comments, blank lines and the header
  line (`place ...`) are left out, as `stats_unit.sh daily` reads it."""
  out = []
  for line in Path(path).read_text().splitlines():
    f = line.split("\t")
    if not f[0] or f[0].startswith("#") or f[0] == "place":
      continue
    if len(f) < 4:
      raise ValueError(f"{path}: a unit is place, coll, dataset, variable: {line!r}")
    out.append({"place_id": f[0], "coll": f[1], "dataset_id": f[2], "variable": f[3]})
  return out


def unit_id(u: dict) -> str:
  return item_id(u["dataset_id"], u["variable"], u["place_id"])


# -- an Item as the collection reads it ---------------------------------------------------------------------------------
def lobe_boxes(item: dict) -> list[tuple[float, float, float, float]]:
  """The place's lobe boxes from an Item's geometry (`lobeBoxGeometry()`: a MultiPolygon of boxes, ring
  x0 y0, x1 y0, x1 y1, x0 y1, x0 y0)."""
  g = item.get("geometry") or {}
  if g.get("type") != "MultiPolygon":
    raise ValueError(f"item {item.get('id')}: the geometry is not the lobe boxes' MultiPolygon")
  return [(r[0][0][0], r[0][0][1], r[0][2][0], r[0][2][1]) for r in g["coordinates"]]


def item_ref(item: dict) -> dict:
  """What the collection needs of an Item (stac.ts `ItemRef`, `existingItems()`), plus its bbox and lobe boxes."""
  p = item.get("properties") or {}
  ref = {"id": item.get("id"), "place_id": p.get("erddap-places:place_id"), "dataset_id": p.get("erddap-places:dataset_id"),
         "variable": p.get("erddap-places:variable"), "start_datetime": p.get("start_datetime"),
         "end_datetime": p.get("end_datetime"), "record_start": p.get("erddap-places:record_start"),
         "bbox": item.get("bbox"), "lobes": lobe_boxes(item)}
  missing = [k for k in ("id", "place_id", "dataset_id", "variable", "start_datetime", "end_datetime", "bbox") if not ref[k]]
  if missing:
    raise ValueError(f"item {item.get('id')}: no {', '.join(missing)}")
  if ref["id"] != item_id(ref["dataset_id"], ref["variable"], ref["place_id"]):
    raise ValueError(f"item {ref['id']}: the id is not its dataset, variable and place's")
  return ref


def ref_status(ref: dict, dataset_collection: dict | None) -> str | None:
  """`itemStatus()` of an Item: its record start, else the dataset collection's temporal start."""
  return item_status({"record_start": ref.get("record_start"), "start_datetime": ref["start_datetime"]},
                     dataset_collection)


# -- what is listed -----------------------------------------------------------------------------------------------------
def listed_refs(units: Iterable[dict], items: dict[str, dict]) -> tuple[list[dict], list[dict]]:
  """(the refs of the units' Items, the pending units): the units of the `places` collection only, each Item once;
  `items` maps an Item id to its ref. Pending: a places unit with no Item, once each, sorted by id."""
  listed, pending, seen = [], {}, set()
  for u in units:
    if u["coll"] != PLACES_COLLECTION:
      continue
    k = unit_id(u)
    if k in items:
      if k not in seen:
        seen.add(k)
        listed.append(items[k])
    else:
      pending[k] = {"id": k, "dataset_id": u["dataset_id"], "variable": u["variable"], "place_id": u["place_id"]}
  return listed, [pending[k] for k in sorted(pending)]


def previous_order(collection: dict | None) -> list[str]:
  """The Item ids the previous collection links, in its order."""
  out = []
  for lk in (collection or {}).get("links") or []:
    m = re.fullmatch(r"\./items/(.+)\.json", lk.get("href") or "") if lk.get("rel") == "item" else None
    if m:
      out.append(m.group(1))
  return out


def order_refs(refs: list[dict], previous: list[str]) -> list[dict]:
  """The previous collection's order first, then the Items it did not link, by id."""
  pos = {k: i for i, k in enumerate(previous)}
  old = sorted((r for r in refs if r["id"] in pos), key=lambda r: pos[r["id"]])
  new = sorted((r for r in refs if r["id"] not in pos), key=lambda r: r["id"])
  return old + new


# -- the datasets' collections ------------------------------------------------------------------------------------------
def dataset_via(dataset_id: str, collection: dict | None) -> dict | None:
  """`datasetVia()`: the collection's `rel: via` text/html link as {href, title} (title the dataset id when absent)."""
  for lk in (collection or {}).get("links") or []:
    if lk.get("rel") == "via" and lk.get("type") == "text/html":
      return {"href": lk["href"], "title": lk.get("title") if lk.get("title") is not None else dataset_id}
  return None


def dataset_providers(dataset_ids: list[str], collections: dict[str, dict]) -> list[dict]:
  """`datasetProviders()`: the producers / licensors of the datasets' collections in dataset order, once by name, Ocean
  Metrics left out; then Ocean Metrics LLC as processor and host, last."""
  out: dict[str, dict] = {}
  for d in dataset_ids:
    for pr in (collections.get(d) or {}).get("providers") or []:
      roles = [r for r in pr.get("roles") or [] if r in ("producer", "licensor")]
      if not roles or re.search(r"ocean metrics", pr["name"], re.I) or pr["name"] in out:
        continue
      out[pr["name"]] = {"name": pr["name"], "roles": roles, **({"url": pr["url"]} if pr.get("url") else {})}
  return [*out.values(), dict(OM_PROVIDER)]


def _first_seen(xs: Iterable[str]) -> list[str]:
  return list(dict.fromkeys(xs))


# -- the collection -----------------------------------------------------------------------------------------------------
def build_collection(refs: list[dict], collections: dict[str, dict], pending: list[dict] | None = None, *,
                     thumbnail: dict | None = None, updated: str | None = None) -> dict:
  """`buildCollection()`: the stats Collection over the listed Items `refs` (in link order), the datasets'
  `collections` (by dataset id), the `pending` units, the thumbnail's `file:size` / `file:checksum` and `updated`."""
  if not refs:
    raise ValueError("build_collection: no Items to list")
  pending = pending or []
  bbox = union_bbox([tuple(r["bbox"]) for r in refs])
  start = sorted(r["start_datetime"] for r in refs)[0]
  end = sorted(r["end_datetime"] for r in refs)[-1]
  datasets = _first_seen(r["dataset_id"] for r in refs)
  status = {r["id"]: ref_status(r, collections.get(r["dataset_id"])) for r in refs}
  count = {s: sum(1 for x in status.values() if x == s) for s in STATUSES}
  via = [(d, dataset_via(d, collections.get(d))) for d in datasets]
  return {
    "type": "Collection", "id": "stats", "stac_version": STAC_VERSION,
    "stac_extensions": [EXT_TABLE, EXT_FILE, EXT_PORTOLAN],
    "title": TITLE, "description": DESCRIPTION, "license": "CC-BY-4.0", "keywords": list(KEYWORDS),
    "extent": {"spatial": {"bbox": [bbox]}, "temporal": {"interval": [[start, end]]}},
    "providers": dataset_providers(datasets, collections),
    "summaries": {
      "erddap-places:dataset_id": datasets,
      "erddap-places:variable": _first_seen(r["variable"] for r in refs),
      "erddap-places:place_id": sorted(set(r["place_id"] for r in refs)),
      "erddap-places:status": [s for s in STATUSES if count[s] > 0],
    },
    "erddap-places:item_counts": {"complete": count["complete"], "partial": count["partial"], "pending": len(pending)},
    "erddap-places:pending": [{"id": p["id"], "dataset_id": p["dataset_id"], "variable": p["variable"],
                               "place_id": p["place_id"]} for p in pending],
    "table:columns": DAILY_COLUMNS,
    "assets": {"thumbnail": {"href": f"./{THUMB}", "type": "image/png", "title": "the precomputed places, equirectangular",
                             "roles": ["thumbnail"], **(thumbnail or {})}},
    "updated": updated or utc_now(),
    "links": [
      {"rel": "root", "href": "../catalog.json", "type": "application/json", "title": "Ocean Metrics gazetteer"},
      {"rel": "parent", "href": "../catalog.json", "type": "application/json"},
      {"rel": "agents", "href": "./AGENTS.md", "type": "text/markdown", "title": "Agent/LLM usage guide"},
      {"rel": "describedby", "href": "./README.md", "type": "text/markdown", "title": "Human-readable documentation"},
      *({"rel": "related", "href": f"../erddap/{d}/collection.json", "type": "application/json",
         "title": f"source dataset: {d}"} for d in datasets),
      {"rel": "related", "href": "../places/collection.json", "type": "application/json", "title": PLACES_COLLECTION_TITLE},
      *({**v, "rel": "via", "type": "text/html"} for _, v in via if v),
      *({"rel": "item", "href": f"./items/{r['id']}.json", "type": "application/geo+json",
         "title": f"{r['place_id']} — {r['variable']}",
         **({"erddap-places:status": status[r["id"]]} if status[r["id"]] else {})} for r in refs),
    ],
  }


def collection_text(collection: dict) -> str:
  """The file's text as erddap-places wrote it: `JSON.stringify(collection, null, 2) + '\\n'`."""
  return js_json(collection, indent=2) + "\n"


def utc_now() -> str:
  """`new Date().toISOString()` without the milliseconds: `2026-10-24T09:30:00Z`."""
  return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- the thumbnail ------------------------------------------------------------------------------------------------------
def _round(x: float) -> int:
  """`Math.round()`: half up (toward +infinity), not Python's half to even."""
  return math.floor(x + 0.5)


def thumbnail_pixels(places: list[list[tuple[float, float, float, float]]], w: int = 480, h: int = 240) -> bytes:
  """`writeThumbnail()`'s RGB pixels: the ocean, the equator and five meridians, then each place's lobe boxes filled and
  outlined (a tiny place still at least one pixel), in the order given."""
  px = bytearray(w * h * 3)

  def put(x: int, y: int, rgb: tuple[int, int, int]) -> None:
    if 0 <= x < w and 0 <= y < h:
      i = (y * w + x) * 3
      px[i:i + 3] = bytes(rgb)

  to_x = lambda lon: _round(((lon + 180) / 360) * (w - 1))
  to_y = lambda lat: _round(((90 - lat) / 180) * (h - 1))
  px[:] = bytes((10, 26, 47)) * (w * h)
  for x in range(w):
    put(x, to_y(0), (24, 48, 80))
  for lon in (-120, -60, 0, 60, 120):
    for y in range(h):
      put(to_x(lon), y, (24, 48, 80))
  for boxes in places:
    for x0, y0, x1, y1 in boxes:
      a, b, c, d = to_x(x0), to_y(y1), to_x(x1), to_y(y0)
      for y in range(b, d + 1):
        for x in range(a, c + 1):
          put(x, y, (56, 142, 168))
      for x in range(a - 1, c + 2):
        put(x, b - 1, (240, 196, 92))
        put(x, d + 1, (240, 196, 92))
      for y in range(b - 1, d + 2):
        put(a - 1, y, (240, 196, 92))
        put(c + 1, y, (240, 196, 92))
  return bytes(px)


def png_bytes(rgb: bytes, w: int, h: int) -> bytes:
  """An 8-bit RGB PNG as `writeThumbnail()` writes it: filter byte 0 per scanline, deflate level 9, IHDR IDAT IEND."""
  raw = b"".join(b"\x00" + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))

  def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

  ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
  return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def png_pixels(png: bytes) -> tuple[int, int, bytes]:
  """(width, height, RGB) of an 8-bit RGB PNG with filter 0 scanlines (what `png_bytes()` writes), for the tests."""
  assert png[:8] == b"\x89PNG\r\n\x1a\n"
  i, idat, w, h = 8, b"", 0, 0
  while i < len(png):
    n = struct.unpack(">I", png[i:i + 4])[0]
    kind, data = png[i + 4:i + 8], png[i + 8:i + 8 + n]
    if kind == b"IHDR":
      w, h = struct.unpack(">II", data[:8])
    elif kind == b"IDAT":
      idat += data
    i += 12 + n
  raw = zlib.decompress(idat)
  rows = [raw[y * (w * 3 + 1):(y + 1) * (w * 3 + 1)] for y in range(h)]
  if any(r[0] != 0 for r in rows):
    raise ValueError("png_pixels: a scanline filter other than 0")
  return w, h, b"".join(r[1:] for r in rows)


def write_thumbnail(path: str | Path, refs: list[dict]) -> int:
  """The thumbnail of the listed places (their lobe boxes, in the order the places first appear); returns its size."""
  by_place: dict[str, list] = {}
  for r in refs:
    by_place.setdefault(r["place_id"], r["lobes"])
  png = png_bytes(thumbnail_pixels(list(by_place.values())), 480, 240)
  Path(path).parent.mkdir(parents=True, exist_ok=True)
  Path(path).write_bytes(png)
  return len(png)


# -- the I/O ------------------------------------------------------------------------------------------------------------
def http_fetch(url: str) -> bytes | None:
  """GET `url`: the body on 200, None on 404, raises on any other answer (an outage never yields a partial list)."""
  import httpx
  for attempt in range(3):
    try:
      r = httpx.get(url, follow_redirects=True, timeout=60, headers={"cache-control": "no-cache"})
    except httpx.TransportError:
      if attempt == 2:
        raise
      continue
    if r.status_code == 404:
      return None
    if r.status_code == 200:
      return r.content
    if r.status_code < 500 or attempt == 2:
      raise RuntimeError(f"HTTP {r.status_code} from {url}")
  raise RuntimeError(f"no answer from {url}")


def rebuild(units: Iterable[dict], out_dir: str | Path, *, host: str = STORAGE_HOST,
            local_items: Iterable[str | Path] = (), fetch: Fetch = http_fetch, updated: str | None = None,
            workers: int = 16) -> dict:
  """Rebuild `collection.json` and `stats.thumb.png` into `out_dir` from the published Items of `units`.

  An Item is read from `local_items` (Item files or folders of them: the units this run published) when there, else
  from `<host>/gazetteer/stats/items/<id>.json`; the order comes from the host's current `collection.json`, the
  providers and via pages from `<host>/gazetteer/erddap/<dataset>/collection.json`. Returns
  {collection, thumbnail, items, pending, local, previous}; the thumbnail is written first, the collection last."""
  units = [u for u in units if u["coll"] == PLACES_COLLECTION]
  base = f"{host.rstrip('/')}/gazetteer"
  local: dict[str, dict] = {}
  for p in local_items:
    p = Path(p)
    for f in sorted(p.glob("*.json")) if p.is_dir() else [p]:
      it = json.loads(f.read_text())
      local[it.get("id")] = it
  wanted = sorted({unit_id(u) for u in units} - set(local))
  with ThreadPoolExecutor(max_workers=workers) as ex:
    bodies = dict(zip(wanted, ex.map(lambda k: fetch(f"{base}/stats/items/{k}.json"), wanted)))
  items = {k: item_ref(it) for k, it in local.items() if k in {unit_id(u) for u in units}}
  for k, body in bodies.items():
    if body is not None:
      items[k] = item_ref(json.loads(body))
  for k, r in items.items():
    if r["id"] != k:
      raise ValueError(f"the Item at {k}.json is {r['id']}")
  listed, pending = listed_refs(units, items)
  if not listed:
    raise ValueError("no unit has a published Item: no collection written")
  prev_body = fetch(f"{base}/stats/collection.json")
  previous = json.loads(prev_body) if prev_body is not None else None
  refs = order_refs(listed, previous_order(previous))
  datasets = _first_seen(r["dataset_id"] for r in refs)
  collections = {}
  for d in datasets:
    body = fetch(f"{base}/erddap/{d}/collection.json")
    if body is None:
      raise ValueError(f"no dataset collection for {d} at {base}/erddap/{d}/collection.json")
    collections[d] = json.loads(body)
  out = Path(out_dir)
  thumb = out / THUMB
  write_thumbnail(thumb, refs)
  coll = build_collection(refs, collections, pending, thumbnail=file_stats(thumb), updated=updated)
  path = out / "collection.json"
  path.write_text(collection_text(coll))
  return {"collection": path, "thumbnail": thumb, "items": len(refs), "pending": len(pending),
          "local": sum(1 for r in refs if r["id"] in local), "previous": previous is not None,
          "counts": coll["erddap-places:item_counts"]}
