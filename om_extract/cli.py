"""Command line entry point (`om-extract`). It parses arguments and calls the functions in `om_extract/`; it holds no logic."""
import argparse
import json
import sys
from pathlib import Path

from om_extract import __version__
from om_extract.layers import format_layer_info, layer_info
from om_extract.notebooks import check_dir, read_blocks, write_index
from om_extract.readers import UnknownProfile
from om_extract.readers.erddap import ErddapError, InvalidCollection


def _parser() -> argparse.ArgumentParser:
  p = argparse.ArgumentParser(prog="om-extract", description="Ocean Metrics extraction engine")
  p.add_argument("--version", action="store_true", help="print the version and exit")
  sub = p.add_subparsers(dest="command")
  nb = sub.add_parser("notebooks", help="the om: blocks of a workflows directory")
  nbs = nb.add_subparsers(dest="action", required=True)
  chk = nbs.add_parser("check", help="validate the om: blocks and the dependency graph; exit 1 on any error")
  chk.add_argument("dir")
  idx = nbs.add_parser("index", help="write index.qmd, _om/order.json and _om/dag.mmd")
  idx.add_argument("dir")
  idx.add_argument("--title", default="Workflows")
  ly = sub.add_parser("layer", help="a layer's STAC collection")
  lys = ly.add_subparsers(dest="action", required=True)
  inf = lys.add_parser("info", help="print the dataset record and the time extent of a collection")
  inf.add_argument("collection", help="path or URL of the collection.json")
  inf.add_argument("--info", help="a saved ERDDAP info/<id>/index.json; without it the server is asked")
  inf.add_argument("--json", action="store_true", help="print the record as JSON")
  rn = sub.add_parser("run", help="summarise one variable of a layer over one place and window into a values file",
                      description="Extract one place x variable x window and write <out>/stats.parquet, "
                                  "<out>/provenance.json and <out>/values/indicator=<id>/coll=<slug>/data.parquet "
                                  "(validated against the values contract). With --chunks, the chunked engine over "
                                  "the whole record; with --stats-dir, erddap-places' stats/ layout and its STAC Item. "
                                  "--reader copernicus: one Copernicus Marine variable by the cell-centre mean.")
  rn.add_argument("--reader", default="auto", choices=["auto", "copernicus"],
                  help="auto (default): the reader the collection's profile names; copernicus: the Copernicus Marine "
                       "Toolbox (--dataset, the maintainer's login in ~/.copernicusmarine)")
  rn.add_argument("--collection", help="path or URL of the layer's collection.json (not with --reader copernicus)")
  rn.add_argument("--dataset", help="--reader copernicus: the Copernicus Marine dataset id")
  rn.add_argument("--place", required=True, help="the gazetteer place id (NMS:CINMS)")
  rn.add_argument("--geometry", help="the place geometry: a WKB file, or a GeoJSON file (default: read from the "
                                     "gazetteer, --coll and --base)")
  rn.add_argument("--var", required=True, help="the variable (a cube:variables key)")
  rn.add_argument("--t0", help="first day, yyyy-mm-dd (default with --chunks or copernicus: the whole record)")
  rn.add_argument("--t1", help="last day, yyyy-mm-dd")
  rn.add_argument("--out", required=True, help="output directory")
  rn.add_argument("--stat", default="mean_wt",
                  help="the stats column that is the value: mean_wt (default), mean, sd, min, max, p10, p90")
  rn.add_argument("--coll", help="gazetteer collection of the place (default: NMS: is places, else the authority lowercased)")
  rn.add_argument("--base", help="the gazetteer base URL or directory for the place geometry "
                                 "(default https://storage.oceanmetrics.io/gazetteer)")
  rn.add_argument("--places-internal", help="the private copies of the reference collections "
                                            "(default $PLACES_INTERNAL, else ~/data/oceanmetrics/places-internal)")
  rn.add_argument("--chunks", help="the chunk store: run the chunked engine (live extent, only missing chunks fetched)")
  rn.add_argument("--stats-dir", help="write <stats-dir>/<dataset>/<variable>/<place>.parquet, .provenance.json and "
                                      "<stats-dir>/items/<id>.json (erddap-places' stats/ layout and STAC Item)")
  rn.add_argument("--max-values", type=int, help="--reader copernicus: refuse a job over this many grid values")
  rn.add_argument("--slab-dir", help="offline: serve each request URL from <slab-dir>/<sha1 of the URL>.parquet "
                                     "(hex sha1 of the URL's UTF-8 bytes, the URL as the provenance lists it)")
  pa = sub.add_parser("parity", help="run the engine live and compare it with erddap-places' published stats",
                      description="Run the engine for a place over the published window through its chunk store, "
                                  "download the published parquet, provenance and item, compare them and write "
                                  "parity.json (several variables or places: <out>/<variable>/parity_<place>.json and "
                                  "<out>/summary.md).")
  pa.add_argument("--dataset", required=True, help="the ERDDAP collection (dhw_5km)")
  pa.add_argument("--variable", required=True, action="append", help="the variable (repeat for several)")
  pa.add_argument("--place", required=True, action="append",
                  help="the place id (NMS:CINMS; repeat for several, or 'all' for every NMS: place of --coll)")
  pa.add_argument("--chunks", help="the engine's chunk store (default <out>/chunks, per place in a batch)")
  pa.add_argument("--out", default="parity", help="output directory (default ./parity)")
  pa.add_argument("--base", default=None, help="the gazetteer base URL or directory "
                                               "(default https://storage.oceanmetrics.io/gazetteer)")
  pa.add_argument("--coll", default="places", help="the places collection of the geometry (default places)")
  pa.add_argument("--reference-chunks", help="the runner's chunk store (PRECOMPUTE_CHUNKS), read only: plan from its "
                                             "chunk names and compare every fetched chunk with it")
  pa.add_argument("--fetch", default="all", help="with --reference-chunks, the chunks the engine computes itself: "
                                                 "all (default), none, last:N (the rest copied from the reference)")
  pa.add_argument("--window", default="published", choices=["published", "live"],
                  help="published (default): the published file's window; live: window(ext, 'all')")
  pa.add_argument("--tries", type=int, default=3, help="tries in a row without a new chunk before a place is given "
                                                       "up, resting 10, 30, 60, 120 min between (default 3)")
  cp = sub.add_parser("compare", help="compare two stats files by the parity criterion; exit 1 when it fails",
                      description="Compare an engine's stats parquet with a published one row by row: every column but "
                                  "the sums exact, the sums within 1e-9 relative, the same keys and one row per key in "
                                  "the engine's file, and say whether the bytes are equal and why not.")
  cp.add_argument("engine", help="the engine's stats parquet (a path)")
  cp.add_argument("ref", help="the published stats parquet (a path)")
  cp.add_argument("--json", help="also write the comparison to this JSON file")
  return p


def _parity(args) -> int:
  from om_extract.parity import run_batch, run_parity, run_with_tries, verdict_table
  from om_extract.places import GAZETTEER_BASE, place_ids
  base = args.base or GAZETTEER_BASE
  places = place_ids(args.coll, base, "NMS:") if args.place == ["all"] else args.place
  kw = {"base": base, "coll": args.coll, "reference_chunks": args.reference_chunks, "fetch_mode": args.fetch,
        "window_mode": args.window}
  if len(places) == 1 and len(args.variable) == 1:
    from pathlib import Path
    from om_extract.parity import chunk_count
    store = args.chunks or str(Path(args.out) / "chunks")
    res = run_with_tries(lambda: run_parity(args.dataset, args.variable[0], places[0], args.out, chunks=args.chunks,
                                            log=print, **kw), args.tries, log=print,
                         progress=lambda: chunk_count(store, args.dataset, args.variable[0], places[0]))
    print(verdict_table(res))
    print(f"wrote {args.out}/parity.json")
    return 0
  if args.chunks:
    kw["chunks"] = args.chunks
  res = run_batch(args.dataset, args.variable, places, args.out, log=print, attempts=args.tries, **kw)
  print(f"wrote {args.out}/summary.md ({len(res)} place(s), {sum('error' in r for r in res)} failed)")
  return 0


def _run_copernicus(args) -> dict:
  """`om-extract run --reader copernicus`: the place geometry (the file, else the gazetteer), then
  `readers.copernicus.run()`."""
  from om_extract.engine import default_coll
  from om_extract.readers import copernicus as cm
  from om_extract.run import read_geometry
  if not args.dataset:
    raise ValueError("--reader copernicus needs --dataset (the Copernicus Marine dataset id)")
  coll = args.coll or default_coll(args.place)
  if args.geometry:
    geom, polygon = read_geometry(args.geometry), {"kind": "file", "url": f"file:{Path(args.geometry).name}"}
  else:
    from om_extract.places import GAZETTEER_BASE, place_record
    rec = place_record(args.place, coll, args.base or GAZETTEER_BASE, args.places_internal)
    # a private copy is named by its collection, never by its path on this machine
    url = rec["href"] if rec["copy"] == "published" else f"places-internal:{coll}/places.parquet"
    geom, polygon = rec["wkb"], {"kind": rec["copy"], "url": url}
  kw = {"max_values": args.max_values} if args.max_values else {}
  res = cm.run(args.dataset, args.var, args.place, geom, args.out, coll=coll, polygon=polygon, start=args.t0,
               end=args.t1, **kw)
  print(f"{args.place} ({coll}) {res['indicator']}: windows {res['windows']}, {res['cells']} cell centres inside")
  return res


def main(argv: list[str] | None = None) -> int:
  """Run the command line; return the exit code (0 ok, 1 on any error)."""
  args = _parser().parse_args(argv)
  if args.version or args.command is None:
    print(f"om-extract {__version__}")
    return 0
  if args.command == "run":
    from om_extract.contract import ContractError
    try:
      if args.reader == "copernicus":
        res = _run_copernicus(args)
      else:
        if not args.collection:
          raise ValueError("--collection is required (or --reader copernicus with --dataset)")
        from om_extract.run import run
        res = run(args.collection, args.place, args.geometry, args.var, args.t0, args.t1, args.out, stat=args.stat,
                  coll=args.coll, slab_dir=args.slab_dir, chunks=args.chunks, stats_dir=args.stats_dir,
                  base=args.base, internal=args.places_internal, log=print if args.chunks else None)
    except ContractError as e:
      for line in e.errors:
        print(f"error: {line}", file=sys.stderr)
      return 1
    except (InvalidCollection, UnknownProfile, ErddapError, OSError, ValueError, LookupError, RuntimeError) as e:
      for line in str(e).splitlines():
        print(f"error: {line}", file=sys.stderr)
      return 1
    for k in ("stats", "provenance", "item"):
      if res.get(k):
        print(f"wrote {res[k]}")
    if res.get("values"):
      print(f"wrote {res['values']} ({res['rows']} rows, {res['indicator']}, coll {res['coll']})")
    else:
      print(f"no values file: {res['rows']} rows")
    return 0
  if args.command == "compare":
    from om_extract.parity import compare_files, compare_text
    res = compare_files(args.engine, args.ref)
    print(compare_text(res))
    if args.json:
      Path(args.json).write_text(json.dumps(res, indent=2, default=str) + "\n")
    return 0 if res["verdict"]["pass"] else 1
  if args.command == "parity":
    try:
      return _parity(args)
    except (InvalidCollection, UnknownProfile, ErddapError, OSError, ValueError, LookupError) as e:
      for line in str(e).splitlines():
        print(f"error: {line}", file=sys.stderr)
      return 1
  if args.command == "layer":
    try:
      rec = layer_info(args.collection, info_path=args.info)
    except (InvalidCollection, UnknownProfile, ErddapError, OSError, ValueError) as e:
      for line in str(e).splitlines():
        print(f"error: {line}", file=sys.stderr)
      return 1
    print(json.dumps(rec, indent=2) if args.json else format_layer_info(rec))
    return 0
  if args.action == "check":
    errors = check_dir(args.dir)
    for e in errors:
      print(f"error: {e}", file=sys.stderr)
    for b in read_blocks(args.dir):
      if b["note"]:
        print(f"note: {b['path']}: {b['note']}")
    if not errors:
      print("ok")
    return 1 if errors else 0
  try:
    paths = write_index(args.dir, args.title)
  except ValueError as e:
    for line in str(e).splitlines():
      print(f"error: {line}", file=sys.stderr)
    return 1
  for p in paths:
    print(f"wrote {p}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
