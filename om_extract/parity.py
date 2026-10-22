"""The parity harness: the engine's stats for one (dataset, variable, place) against erddap-places' published file.

`run_parity()` runs the engine live through its chunk store (`engine.extract(..., chunks=)`) over the published window,
downloads the published parquet, provenance and STAC item, and compares:

- **rows** by key (`date`, or `date, class` for a categorical variable), every column: `==` for the integers, strings
  and dates; floats exact first, and where not exact the number of rows that differ, the largest absolute difference
  and the largest distance in units in the last place (`compare_rows()`); duplicate keys on either side are reported;
- **provenance** key by key: which differ and how (`compare_provenance()`);
- **bytes** of the parquet: equal or not, and why not (writer, row groups, schema, values; `compare_bytes()`);
- **item** properties against the engine's provenance (`compare_item()`);
- with a reference chunk store (the runner's `PRECOMPUTE_CHUNKS`, read only), the mask key and every chunk the engine
  fetched against the runner's chunk of the same name (`compare_chunks()`).

`verdict_table()` prints the verdict, `parity.json` holds all of it. The comparisons are pure functions of tables and
dicts, tested on synthetic tables.
"""
from __future__ import annotations

import json
import math
import shutil
import struct
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pyarrow as pa
import pyarrow.parquet as pq

from om_extract.chunks import ChunkStore, chunk_name, chunk_span, file_safe
from om_extract.places import GAZETTEER_BASE

# keys whose difference is expected and says nothing about parity
EXPECTED_DIFF = ("generated",)
EXAMPLES = 5


# -- rows ---------------------------------------------------------------------------------------------------------------
def ordered_bits(x: float) -> int:
  """A double as an integer whose order and differences are the doubles' order and ulp distance (-0.0 is 0)."""
  i = struct.unpack("<q", struct.pack("<d", float(x)))[0]
  return i if i >= 0 else -(i & 0x7FFFFFFFFFFFFFFF)


def ulps(a: float, b: float) -> int:
  """The number of representable doubles between `a` and `b` (0 when equal)."""
  return abs(ordered_bits(a) - ordered_bits(b))


def _is_nan(v: Any) -> bool:
  return isinstance(v, float) and math.isnan(v)


def _key_str(k: tuple) -> str:
  return ", ".join(str(x) for x in k)


def _group(rows: list[dict], keys: tuple[str, ...]) -> dict[tuple, list[dict]]:
  out: dict[tuple, list[dict]] = {}
  for r in rows:
    out.setdefault(tuple(r[k] for k in keys), []).append(r)
  return out


def compare_rows(engine: pa.Table, ref: pa.Table, keys: tuple[str, ...] | None = None) -> dict:
  """Compare two stats tables row by row, matched by `keys` (default `date`, plus `class` when the reference has it).

  Rows of one key are paired in file order (a duplicated key pairs its first rows with the other side's first rows,
  and the rest are missing or extra). Per column of the reference: `exact` (every paired value equal: `==`, both null,
  or both NaN), `n_diff`, and for a float column `max_abs` and `max_ulps` over the differing rows, plus the first
  difference. `exact` overall means the same keys, no duplicates unpaired and every column exact."""
  if keys is None:
    keys = ("date", "class") if "class" in ref.column_names else ("date",)
  er, rr = engine.to_pylist(), ref.to_pylist()
  ge, gr = _group(er, keys), _group(rr, keys)
  missing, extra, pairs = [], [], []
  for k in sorted(set(ge) | set(gr), key=lambda k: tuple(str(x) for x in k)):
    a, b = ge.get(k, []), gr.get(k, [])
    pairs += list(zip(a, b))
    missing += [k] * max(0, len(b) - len(a))
    extra += [k] * max(0, len(a) - len(b))
  cols: dict[str, dict] = {}
  for c in ref.column_names:
    if c not in engine.column_names:
      continue
    typ = ref.schema.field(c).type
    is_float = pa.types.is_floating(typ)
    n_diff, max_abs, max_u, max_rel, first = 0, 0.0, 0, 0.0, None
    for e, r in pairs:
      x, y = e[c], r[c]
      if x == y or (x is None and y is None) or (_is_nan(x) and _is_nan(y)):
        continue
      n_diff += 1
      if first is None:
        first = {"key": _key_str(tuple(r[k] for k in keys)), "engine": x, "ref": y}
      if is_float and x is not None and y is not None and not (_is_nan(x) or _is_nan(y)):
        max_abs = max(max_abs, abs(x - y))
        max_u = max(max_u, ulps(x, y))
        max_rel = max(max_rel, abs(x - y) / max(abs(x), abs(y)))
      elif is_float:
        max_abs = math.inf
    col = {"type": str(typ), "exact": n_diff == 0, "n_diff": n_diff}
    if is_float and n_diff:
      col.update({"max_abs": max_abs, "max_ulps": max_u, "max_rel": max_rel})
    if first is not None:
      col["first_diff"] = first
    cols[c] = col
  dup_e = {k: len(v) for k, v in ge.items() if len(v) > 1}
  dup_r = {k: len(v) for k, v in gr.items() if len(v) > 1}
  schema_diff = []
  for c in ref.column_names:
    if c not in engine.column_names:
      schema_diff.append(f"column {c} only in the reference")
    elif engine.schema.field(c).type != ref.schema.field(c).type:
      schema_diff.append(f"{c}: {engine.schema.field(c).type} (engine) vs {ref.schema.field(c).type} (reference)")
  schema_diff += [f"column {c} only in the engine" for c in engine.column_names if c not in ref.column_names]
  if engine.column_names != ref.column_names and not schema_diff:
    schema_diff.append(f"column order {engine.column_names} vs {ref.column_names}")
  exact = not missing and not extra and not schema_diff and all(v["exact"] for v in cols.values())
  return {
    "keys": list(keys), "rows_engine": engine.num_rows, "rows_ref": ref.num_rows, "paired": len(pairs),
    "missing_in_engine": len(missing), "missing_examples": [_key_str(k) for k in missing[:EXAMPLES]],
    "extra_in_engine": len(extra), "extra_examples": [_key_str(k) for k in extra[:EXAMPLES]],
    "duplicate_keys_engine": len(dup_e), "duplicate_keys_ref": len(dup_r),
    "duplicate_examples_ref": [_key_str(k) for k in list(dup_r)[:EXAMPLES]],
    "schema_equal": not schema_diff, "schema_diff": schema_diff, "columns": cols, "exact": exact,
  }


# the relative difference below which a float column is "the same sum in another order" (DuckDB's parallel aggregates)
SUM_ORDER_REL = 1e-12


def rows_verdict(cmp: dict) -> str:
  """`exact`; `float sums differ (max rel x)` when the keys and every integer, string and date column are equal and
  the float columns that differ do so by at most `SUM_ORDER_REL` relative (the summation order of DuckDB's parallel
  `sum`/`avg`/`stddev`, which differs from run to run on the runner too); else `differs`."""
  if cmp["exact"]:
    return "exact"
  if cmp["missing_in_engine"] or cmp["extra_in_engine"] or not cmp["schema_equal"]:
    return "differs"
  bad = [c for c in cmp["columns"].values() if not c["exact"]]
  if all("max_rel" in c and c["max_rel"] <= SUM_ORDER_REL for c in bad):
    return f"float sums differ (max rel {max(c['max_rel'] for c in bad):.1g})"
  return "differs"


def column_verdict(col: dict) -> str:
  """One column's verdict: `exact`, or the largest difference (`max 7.1e-15 (2 ulp), 43 rows`)."""
  if col["exact"]:
    return "exact"
  if "max_ulps" in col:
    return f"max {col['max_abs']:.2g} ({col['max_ulps']} ulp), {col['n_diff']} rows"
  return f"{col['n_diff']} rows differ"


# -- provenance and item ------------------------------------------------------------------------------------------------
def _describe(a: Any, b: Any) -> dict:
  if isinstance(a, list) and isinstance(b, list):
    i = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    d = {"len_engine": len(a), "len_ref": len(b), "first_diff_index": i}
    if i < len(a):
      d["engine"] = a[i]
    if i < len(b):
      d["ref"] = b[i]
    return d
  if isinstance(a, str) and isinstance(b, str) and (len(a) > 200 or len(b) > 200):
    i = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    return {"len_engine": len(a), "len_ref": len(b), "first_diff_index": i, "engine": a[max(0, i - 40):i + 40],
            "ref": b[max(0, i - 40):i + 40]}
  return {"engine": a, "ref": b}


def compare_provenance(engine: dict, ref: dict, expected: tuple[str, ...] = EXPECTED_DIFF) -> dict:
  """Key by key: `equal` (the keys whose values are equal), `differ` (key -> how: both values, or for a list its
  lengths and first differing element), `only_engine` / `only_ref`, and `key_order_equal`. `expected` keys (the
  `generated` time) are listed in `expected_diff` instead of `differ`."""
  equal, differ, exp = [], {}, {}
  for k in ref:
    if k not in engine:
      continue
    if engine[k] == ref[k]:
      equal.append(k)
    elif k in expected:
      exp[k] = _describe(engine[k], ref[k])
    else:
      differ[k] = _describe(engine[k], ref[k])
  return {"equal": equal, "differ": differ, "expected_diff": exp,
          "only_engine": [k for k in engine if k not in ref], "only_ref": [k for k in ref if k not in engine],
          "key_order_equal": [k for k in engine if k in ref] == [k for k in ref if k in engine]}


# item property -> provenance key (stac.ts buildItem): the item states these from the provenance
ITEM_KEYS = {
  "start_datetime": "start_datetime", "end_datetime": "end_datetime", "table:row_count": "rows",
  "erddap-places:place_id": "place_id", "erddap-places:dataset_id": "dataset_id",
  "erddap-places:variable": "variable", "erddap-places:record_start": "record_start",
  "erddap-places:categorical": "categorical", "erddap-places:mask_cells": "mask_cells",
  "erddap-places:lobes": "lobes", "erddap-places:griddap_url_templates": "griddap_url_templates",
  "erddap-places:griddap_requests": "griddap_requests", "created": "generated",
}


def compare_item(item: dict, prov: dict) -> dict:
  """The published item's properties against the engine's provenance (`ITEM_KEYS`): `equal`, `differ` (both values;
  `created` vs `generated` is expected to differ and is listed under `expected_diff`)."""
  props = item.get("properties") or {}
  equal, differ, exp = [], {}, {}
  for ik, pk in ITEM_KEYS.items():
    if ik not in props or pk not in prov:
      continue
    if props[ik] == prov[pk]:
      equal.append(ik)
    elif pk in EXPECTED_DIFF:
      exp[ik] = _describe(prov[pk], props[ik])
    else:
      differ[ik] = _describe(prov[pk], props[ik])
  return {"equal": equal, "differ": differ, "expected_diff": exp}


# -- bytes --------------------------------------------------------------------------------------------------------------
def compare_bytes(engine_path: str | Path, ref_path: str | Path, rows_exact: bool | None = None) -> dict:
  """Whether two parquet files are byte for byte equal, and if not, why: the writer (`created_by`), the row groups,
  the row count, the schema, the key-value metadata, the codecs, and (`rows_exact` False) the values."""
  a, b = Path(engine_path).read_bytes(), Path(ref_path).read_bytes()
  out = {"equal": a == b, "bytes_engine": len(a), "bytes_ref": len(b), "reasons": []}
  if a == b:
    return out
  ma, mb = pq.ParquetFile(engine_path).metadata, pq.ParquetFile(ref_path).metadata
  out.update({"created_by_engine": ma.created_by, "created_by_ref": mb.created_by,
              "row_groups_engine": ma.num_row_groups, "row_groups_ref": mb.num_row_groups})
  r = out["reasons"]
  if ma.created_by != mb.created_by:
    r.append(f"writer: {ma.created_by} vs {mb.created_by}")
  if ma.num_row_groups != mb.num_row_groups:
    r.append(f"row groups: {ma.num_row_groups} vs {mb.num_row_groups}")
  if ma.num_rows != mb.num_rows:
    r.append(f"rows: {ma.num_rows} vs {mb.num_rows}")
  if not ma.schema.equals(mb.schema):
    sa, sb = ma.schema, mb.schema
    how = []
    for i in range(min(len(sa), len(sb))):
      ca, cb = sa.column(i), sb.column(i)
      if ca.name != cb.name or ca.physical_type != cb.physical_type or str(ca.logical_type) != str(cb.logical_type):
        how.append(f"{ca.name}: {ca.physical_type} {ca.logical_type} vs {cb.name}: {cb.physical_type} {cb.logical_type}")
      elif ca.max_definition_level != cb.max_definition_level:
        rep = {0: "required", 1: "optional"}
        how.append(f"{ca.name} {rep.get(ca.max_definition_level, ca.max_definition_level)} vs "
                   f"{rep.get(cb.max_definition_level, cb.max_definition_level)}")
    if len(sa) != len(sb):
      how.append(f"{len(sa)} vs {len(sb)} columns")
    r.append("parquet schema: " + ("; ".join(how) or "differs"))
  if (ma.metadata or {}) != (mb.metadata or {}):
    r.append("key-value metadata differs")
  def codecs(m):
    return sorted({m.row_group(i).column(j).compression for i in range(m.num_row_groups) for j in range(m.num_columns)})
  if codecs(ma) != codecs(mb):
    r.append(f"codecs: {codecs(ma)} vs {codecs(mb)}")
  if rows_exact is False:
    r.append("values differ (see rows)")
  if not r:
    r.append("same writer, row groups, schema and values: the encoded pages differ")
  return out


# -- chunks -------------------------------------------------------------------------------------------------------------
def reference_key_dir(ref_root: str | Path, dataset_id: str, variable: str, place_id: str) -> Path | None:
  """The runner store's key directory of a place: the one under `<root>/<dataset>/<variable>/<place>/` (when stale
  keys remain, the most recently modified)."""
  d = Path(ref_root) / dataset_id / variable / file_safe(place_id)
  if not d.is_dir():
    return None
  keys = [p for p in d.iterdir() if p.is_dir()]
  return max(keys, key=lambda p: p.stat().st_mtime) if keys else None


def seed_chunks(ref_dir: Path, dest: Path, fetch_mode: str) -> tuple[list[str], list[str]]:
  """Prepare the engine's store from the runner's (read only): returns `(names, copied)`. `names` are the runner's
  chunk names, which the engine plans from so its slices are the runner's. `fetch_mode` says which chunks the engine
  computes itself: `all` (none copied), `none` (every chunk copied), `last:N` (all but the newest N copied)."""
  names = ChunkStore.listing(ref_dir)
  spans = sorted((chunk_span(n), n) for n in names if chunk_span(n))
  if fetch_mode == "all":
    keep: list[str] = []
  elif fetch_mode == "none":
    keep = [n for _, n in spans]
  elif fetch_mode.startswith("last:"):
    k = int(fetch_mode.split(":", 1)[1])
    keep = [n for _, n in spans[:max(0, len(spans) - k)]]
  else:
    raise ValueError(f"fetch mode {fetch_mode!r}: all, none or last:N")
  dest.mkdir(parents=True, exist_ok=True)
  copied = []
  for n in keep:
    if not (dest / n).exists():
      shutil.copyfile(ref_dir / n, dest / n)
    copied.append(n)
  return names, copied


def compare_chunks(fetched: list[tuple[str, str]], engine_dir: Path, ref_dir: Path | None) -> dict:
  """Every chunk the engine fetched against the runner's chunk of the same name: how many were compared, how many
  are exact, and per column the largest difference over them."""
  if ref_dir is None:
    return {"compared": 0}
  compared, exact, cols, rows_diff = 0, 0, {}, []
  for s, e in fetched:
    n = chunk_name(s, e)
    a, b = engine_dir / n, ref_dir / n
    if not (a.exists() and b.exists()):
      continue
    c = compare_rows(pq.read_table(a), pq.read_table(b))
    compared += 1
    exact += c["exact"]
    if c["missing_in_engine"] or c["extra_in_engine"]:
      rows_diff.append(n)
    for k, v in c["columns"].items():
      w = cols.setdefault(k, {"exact": True, "n_diff": 0, "max_abs": 0.0, "max_ulps": 0})
      w["exact"] &= v["exact"]
      w["n_diff"] += v["n_diff"]
      w["max_abs"] = max(w["max_abs"], v.get("max_abs", 0.0))
      w["max_ulps"] = max(w["max_ulps"], v.get("max_ulps", 0))
  return {"compared": compared, "exact": exact, "rows_differ": rows_diff, "columns": cols}


# -- the run ------------------------------------------------------------------------------------------------------------
def _is_url(s: str | Path) -> bool:
  return str(s).startswith(("http://", "https://"))


def _get(base: str | Path, rel: str, http: Callable[[str], bytes]) -> bytes:
  if _is_url(base):
    return http(f"{str(base).rstrip('/')}/{rel}")
  return (Path(base) / rel).read_bytes()


def item_id(dataset_id: str, variable: str, place_id: str) -> str:
  """The stats item id (stac.ts `itemId()`): never a colon."""
  return f"{dataset_id}_{variable}_{place_id.replace(':', '-')}"


def fetch_reference(dataset_id: str, variable: str, place_id: str, dest: str | Path, *,
                    base: str | Path = GAZETTEER_BASE, http: Callable[[str], bytes] | None = None) -> dict:
  """Download the published parquet, provenance and item of a place to `dest`; returns their paths."""
  from om_extract.readers.erddap import make_fetch
  http = http or make_fetch(timeout_s=120, attempts=3, backoff_s=5)
  dest = Path(dest)
  dest.mkdir(parents=True, exist_ok=True)
  stem = f"stats/{dataset_id}/{variable}/{file_safe(place_id)}"
  paths = {"parquet": dest / f"{file_safe(place_id)}.parquet",
           "provenance": dest / f"{file_safe(place_id)}.provenance.json",
           "item": dest / f"{item_id(dataset_id, variable, place_id)}.json"}
  paths["parquet"].write_bytes(_get(base, f"{stem}.parquet", http))
  paths["provenance"].write_bytes(_get(base, f"{stem}.provenance.json", http))
  paths["item"].write_bytes(_get(base, f"stats/items/{item_id(dataset_id, variable, place_id)}.json", http))
  return paths


def run_parity(dataset_id: str, variable: str, place_id: str, out: str | Path, *, chunks: str | Path | None = None,
               base: str | Path = GAZETTEER_BASE, coll: str = "places", reference_chunks: str | Path | None = None,
               fetch_mode: str = "all", window_mode: str = "published", fetch: Callable[[str], bytes] | None = None,
               http: Callable[[str], bytes] | None = None, sleep: Callable[[float], None] = _time.sleep,
               log: Callable[[str], None] | None = None, threads: int | None = 1,
               parity_name: str = "parity.json") -> dict:
  """Run the engine for one place and compare it with the published file; write `<out>/<parity_name>`, return it.

  `base` is the gazetteer (`https://storage.oceanmetrics.io/gazetteer`, or a local copy): the collection is
  `<base>/erddap/<dataset>/collection.json`, the place `<base>/<coll>/places.parquet`, the reference
  `<base>/stats/...`. `chunks` is the engine's store (default `<out>/chunks`). `reference_chunks` is the runner's
  store, read only: the engine plans from its chunk names, `fetch_mode` says which chunks it computes itself (`all`,
  `none`, `last:N`, `seed_chunks()`), and every fetched chunk is compared with the runner's. `window_mode`
  `published` pins the window to the published provenance's `start_date`..`end_date`; `live` takes
  `window(ext, 'all')`. `fetch` serves ERDDAP, `http` the bucket (both default to HTTP)."""
  from om_extract.engine import extract, provenance_text, stats_provenance
  from om_extract.places import place_geometry
  from om_extract.readers.erddap import make_fetch

  out = Path(out)
  say = log or (lambda _: None)
  http = http or make_fetch(timeout_s=120, attempts=3, backoff_s=5)
  started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
  t_start = _time.monotonic()
  ref = fetch_reference(dataset_id, variable, place_id, out / "ref", base=base, http=http)
  ref_prov = json.loads(ref["provenance"].read_text())
  ref_item = json.loads(ref["item"].read_text())
  collection = json.loads(_get(base, f"erddap/{dataset_id}/collection.json", http))
  wkb = place_geometry(place_id, coll, base)
  t0, t1 = (ref_prov["start_date"], ref_prov["end_date"]) if window_mode == "published" else (None, None)

  store = Path(chunks) if chunks else out / "chunks"
  ref_dir = reference_key_dir(reference_chunks, dataset_id, variable, place_id) if reference_chunks else None
  names, copied = None, []
  if ref_dir is not None:
    dest = ChunkStore(store).dir_for(dataset_id, variable, place_id, ref_dir.name)
    names, copied = seed_chunks(ref_dir, dest, fetch_mode)
  engine_dir = out / "engine"
  engine_dir.mkdir(parents=True, exist_ok=True)
  t_engine = _time.monotonic()
  x = extract(collection, place_id, wkb, variable, t0, t1, chunks=store,
              stats_path=engine_dir / f"{file_safe(place_id)}.parquet", existing=names, fetch=fetch, sleep=sleep,
              log=say, threads=threads)
  engine_s = _time.monotonic() - t_engine
  prov = stats_provenance(x)
  (engine_dir / f"{file_safe(place_id)}.provenance.json").write_text(provenance_text(prov))

  rows = compare_rows(pq.read_table(x.stats_path), pq.read_table(ref["parquet"]))
  res = {
    "dataset_id": dataset_id, "variable": variable, "place_id": place_id, "started": started,
    "window": [x.start_date, x.end_date], "window_mode": window_mode, "live_extent_end": x.extent_end,
    "published_window": [ref_prov.get("start_date"), ref_prov.get("end_date")],
    "published_generated": ref_prov.get("generated"),
    "mask_key": x.mask_key, "reference_key": ref_dir.name if ref_dir else None,
    "mask_key_equal": (x.mask_key == ref_dir.name) if ref_dir else None,
    "chunk_days": x.chunk_days, "slices": len(x.chunks_at or []), "fetched": len(x.fetched),
    "requests_fetched": len(x.fetched) * x.lobes, "copied_from_reference": len(copied), "fetch_mode": fetch_mode,
    "rows_verdict": rows_verdict(rows), "rows": rows,
    "provenance": compare_provenance(prov, ref_prov),
    "bytes": compare_bytes(x.stats_path, ref["parquet"], rows_exact=rows["exact"]),
    "item": compare_item(ref_item, prov),
    "chunks": compare_chunks(x.fetched, Path(x.chunk_dir), ref_dir),
    "engine_seconds": round(engine_s, 1), "total_seconds": round(_time.monotonic() - t_start, 1),
    "duckdb": _duckdb_version(),
  }
  (out / parity_name).write_text(json.dumps(res, indent=2, default=str) + "\n")
  return res


def _duckdb_version() -> str:
  import duckdb
  return duckdb.__version__


def verdict_table(res: dict) -> str:
  """The verdict as text: one line per column, then the provenance, bytes, item and chunk lines."""
  r = res["rows"]
  lines = [f"{res['place_id']}  {res['dataset_id']}/{res['variable']}  window {res['window'][0]}..{res['window'][1]}"
           f"  (live extent end {res.get('live_extent_end')})",
           f"rows: {r['rows_engine']} engine, {r['rows_ref']} published, {r['paired']} paired, "
           f"{r['missing_in_engine']} missing, {r['extra_in_engine']} extra, "
           f"{r['duplicate_keys_ref']} duplicated keys published, {r['duplicate_keys_engine']} in the engine"
           f"  -> {res['rows_verdict']}"]
  w = max([len(c) for c in r["columns"]] + [6])
  for c, v in r["columns"].items():
    lines.append(f"  {c:<{w}}  {column_verdict(v)}")
  p = res["provenance"]
  lines.append(f"provenance: {len(p['equal'])} keys equal; differ: {', '.join(p['differ']) or 'none'}"
               f"; expected: {', '.join(p['expected_diff']) or 'none'}"
               + (f"; only engine: {', '.join(p['only_engine'])}" if p["only_engine"] else "")
               + (f"; only published: {', '.join(p['only_ref'])}" if p["only_ref"] else ""))
  b = res["bytes"]
  lines.append(f"bytes: {'equal' if b['equal'] else 'differ'} ({b['bytes_engine']} vs {b['bytes_ref']})"
               + ("" if b["equal"] else f": {'; '.join(b['reasons'])}"))
  it = res["item"]
  lines.append(f"item: {len(it['equal'])} properties equal; differ: {', '.join(it['differ']) or 'none'}")
  ch = res["chunks"]
  if ch.get("compared"):
    bad = {k: v for k, v in ch["columns"].items() if not v["exact"]}
    lines.append(f"chunks: mask key {'equal' if res['mask_key_equal'] else 'DIFFERS'} ({res['mask_key']}); "
                 f"{ch['compared']} fetched chunk(s) compared, {ch['exact']} exact"
                 + (f"; differ: " + ", ".join(f"{k} max {v['max_abs']:.2g} ({v['max_ulps']} ulp)" for k, v in bad.items())
                    if bad else ""))
  elif res.get("reference_key"):
    lines.append(f"chunks: mask key {'equal' if res['mask_key_equal'] else 'DIFFERS'} ({res['mask_key']})")
  lines.append(f"fetched {res['fetched']} chunk(s) ({res['requests_fetched']} request(s)) of {res['slices']}, "
               f"{res['copied_from_reference']} copied from the reference; engine {res['engine_seconds']} s")
  return "\n".join(lines)


def summary_markdown(results: list[dict], title: str = "Parity: engine vs erddap-places") -> str:
  """One table row per (variable, place): rows, the verdict, the columns that differ, provenance keys that differ,
  bytes, the chunks compared, the time; a failed place shows its error."""
  out = [f"# {title}", "", "| variable | place | rows engine / published | verdict | columns not exact | "
         "provenance differs | bytes | fetched chunks exact | engine s |", "|---|---|---|---|---|---|---|---|---|"]
  for r in results:
    if "error" in r:
      out.append(f"| {r['variable']} | {r['place_id']} | | error: {r['error']} | | | | | |")
      continue
    rows = r["rows"]
    bad = ", ".join(f"{k} ({column_verdict(v)})" for k, v in rows["columns"].items() if not v["exact"]) or "none"
    ch = r["chunks"]
    chs = f"{ch['exact']}/{ch['compared']}" if ch.get("compared") else "-"
    out.append(f"| {r['variable']} | {r['place_id']} | {rows['rows_engine']} / {rows['rows_ref']} | {r['rows_verdict']} | "
               f"{bad} | {', '.join(r['provenance']['differ']) or 'none'} | "
               f"{'equal' if r['bytes']['equal'] else 'differ'} | {chs} | {r['engine_seconds']} |")
  return "\n".join(out) + "\n"


# rests between the tries of one place (schedule.ts REST_MS: 10, 30, 60 min) and tries without a new chunk (MAX_ATTEMPTS)
REST_S = (600, 1800, 3600)
MAX_ATTEMPTS = 3


def run_batch(dataset_id: str, variables: list[str], place_ids: list[str], out: str | Path, *,
              rest_s: tuple[float, ...] = REST_S, attempts: int = MAX_ATTEMPTS,
              sleep: Callable[[float], None] = _time.sleep, log: Callable[[str], None] | None = None,
              **kw) -> list[dict]:
  """`run_parity()` for every variable x place: work in `<out>/<variable>/<place>/`, the result in
  `<out>/<variable>/parity_<place>.json`, and `<out>/summary.md` rewritten after each place. A place that fails is
  tried again after a rest (`rest_s`), up to `attempts` tries in a row that finish no new chunk (the chunk store keeps
  what a failed try finished, as the runner's schedule does); then it is recorded with its error and the batch goes on."""
  say = log or (lambda _: None)
  out = Path(out)
  results: list[dict] = []
  for var in variables:
    for pid in place_ids:
      work = out / var / file_safe(pid)
      tries, fails = 0, 0
      while True:
        tries += 1
        t = _time.monotonic()
        before = _chunk_count(kw.get("chunks") or work / "chunks", dataset_id, var, pid)
        try:
          say(f"== {var} {pid} (try {tries})")
          res = run_parity(dataset_id, var, pid, work, sleep=sleep, log=say,
                           parity_name=f"../parity_{file_safe(pid)}.json", **kw)
          res["tries"] = tries
          (out / var / f"parity_{file_safe(pid)}.json").write_text(json.dumps(res, indent=2, default=str) + "\n")
          say(verdict_table(res))
          break
        except Exception as e:  # noqa: BLE001 - a failed place is recorded and the batch goes on
          after = _chunk_count(kw.get("chunks") or work / "chunks", dataset_id, var, pid)
          fails = 0 if after > before else fails + 1
          say(f"   !! {var} {pid} failed after {_time.monotonic() - t:.0f} s: {e}")
          if fails >= attempts:
            res = {"dataset_id": dataset_id, "variable": var, "place_id": pid, "error": str(e).splitlines()[0],
                   "tries": tries}
            (out / var / f"parity_{file_safe(pid)}.json").write_text(json.dumps(res, indent=2) + "\n")
            break
          wait = rest_s[min(fails - 1, len(rest_s) - 1)] if fails else rest_s[0]
          say(f"   resting {wait:.0f} s before try {tries + 1}")
          sleep(wait)
      results.append(res)
      (out / "summary.md").write_text(summary_markdown(results))
  return results


def run_with_tries(fn: Callable[[], dict], attempts: int = MAX_ATTEMPTS, *, rest_s: tuple[float, ...] = REST_S,
                   sleep: Callable[[float], None] = _time.sleep, log: Callable[[str], None] | None = None) -> dict:
  """`fn()` until it returns, resting `rest_s` between tries; the last failure is raised after `attempts` tries."""
  say = log or (lambda _: None)
  for i in range(attempts):
    try:
      return fn()
    except Exception as e:  # noqa: BLE001 - retried, then re-raised
      if i == attempts - 1:
        raise
      wait = rest_s[min(i, len(rest_s) - 1)]
      say(f"   !! try {i + 1} failed: {e}; resting {wait:.0f} s")
      sleep(wait)
  raise AssertionError("unreachable")


def _chunk_count(root: str | Path, dataset_id: str, variable: str, place_id: str) -> int:
  d = Path(root) / dataset_id / variable / file_safe(place_id)
  return sum(len(ChunkStore.listing(k)) for k in d.iterdir() if k.is_dir()) if d.is_dir() else 0
