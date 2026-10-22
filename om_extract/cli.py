"""Command line entry point (`om-extract`). It parses arguments and calls the functions in `om_extract/`; it holds no logic."""
import argparse
import json
import sys

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
  return p


def main(argv: list[str] | None = None) -> int:
  """Run the command line; return the exit code (0 ok, 1 errors in the notebooks)."""
  args = _parser().parse_args(argv)
  if args.version or args.command is None:
    print(f"om-extract {__version__}")
    return 0
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
