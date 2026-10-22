"""Layers: a collection loaded from a file or URL, and the dataset record the apps read from it (erddap-places' `toDataset()`)."""
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

from om_extract.readers.erddap import Fetch, make_fetch, pick_format

COLLECTION_TIMEOUT_S = 30


def load_collection(href_or_path: str | Path, *, fetch: Fetch | None = None) -> dict:
  """The STAC collection at a URL (`http(s)://`, fetched with `fetch(url) -> bytes`, default `make_fetch`) or at a file path."""
  s = str(href_or_path)
  if s.startswith(("http://", "https://")):
    return json.loads((fetch or make_fetch(timeout_s=COLLECTION_TIMEOUT_S))(s))
  return json.loads(Path(s.removeprefix("file://")).read_text())


@dataclass(frozen=True)
class CubeVariable:
  """A data variable of the cube: its unit, whether it is class-valued, and the label of each class."""
  name: str
  unit: str | None
  description: str
  categorical: bool
  classes: dict[str, str] | None = None


@dataclass(frozen=True)
class Dataset:
  """The fields of an `erddap-grid` collection the apps and the engine use, with erddap-places' defaults applied."""
  id: str
  title: str
  base_url: str
  dataset_id: str
  protocol: str
  long_format: dict[str, str] | None
  version: str | None
  cors: bool
  formats: list[str]
  lon_range: tuple[float, float]
  lat_descending: bool
  variables: list[CubeVariable]
  time_step: str | None
  time_extent: list | None
  depth: float | None
  status: str | None
  format: str


def is_categorical(v: dict | None) -> bool:
  """The `erddap-places:categorical` flag as published (boolean, or the string "true")."""
  f = (v or {}).get("erddap-places:categorical")
  return f is True or f == "true"


def to_long_format(f: dict | None) -> dict[str, str] | None:
  """`erddap-places:long_format` to `{type_column, value_column}`, or None when either is missing."""
  t, v = (f or {}).get("type_column"), (f or {}).get("value_column")
  return {"type_column": str(t), "value_column": str(v)} if t and v else None


def _depth(c: dict) -> float | None:
  d = c.get("erddap-places:depth")
  if d is None or isinstance(d, bool):
    return None
  try:
    x = float(d)
  except (TypeError, ValueError):
    return None
  return x if math.isfinite(x) else None


def dataset_of(c: dict) -> Dataset:
  """One collection to the dataset record (`toDataset()`). Defaults: the title is the id; the protocol is griddap
  unless it is exactly tabledap; CORS is on unless it is false; no formats; longitude [-180, 180]; latitude ascends
  unless flagged; only `data` variables (a missing type counts as data); a variable's unit is None and its
  description its name when missing; the depth is a finite number or None; the format is the best the server serves."""
  variables = [CubeVariable(name=k, unit=(v or {}).get("unit"), description=(v or {}).get("description") or k,
                            categorical=is_categorical(v), classes=(v or {}).get("erddap-places:classes") or None)
               for k, v in (c.get("cube:variables") or {}).items() if ((v or {}).get("type") or "data") == "data"]
  time = (c.get("cube:dimensions") or {}).get("time") or {}
  return Dataset(
    id=c["id"],
    title=c.get("title") or c["id"],
    base_url=c["erddap:base_url"],
    dataset_id=c["erddap:dataset_id"],
    protocol="tabledap" if c.get("erddap:protocol") == "tabledap" else "griddap",
    long_format=to_long_format(c.get("erddap-places:long_format")),
    version=c.get("erddap:version"),
    cors=c.get("erddap:cors") is not False,
    formats=list(c.get("erddap:formats") or []),
    lon_range=tuple(c.get("erddap:lon_range") or (-180, 180)),
    lat_descending=c.get("erddap:lat_descending") is True,
    variables=variables,
    time_step=time.get("step"),
    time_extent=time.get("extent"),
    depth=_depth(c),
    status=c.get("erddap-places:status"),
    format=pick_format({"erddap": {"cors": c.get("erddap:cors"), "formats": c.get("erddap:formats")}}),
  )


def layer_info(href_or_path: str | Path, *, info_path: str | Path | None = None, fetch: Fetch | None = None) -> dict:
  """The dataset record and the time extent of a collection: `{"dataset": {...}, "extent": {...}}`. The extent comes
  from `info_path` (a saved `info/<id>/index.json`) when given, else from the server; the catalog's own range when
  the server does not answer (`live` says which)."""
  from om_extract.readers import for_collection
  doc = load_collection(href_or_path, fetch=fetch)
  info = json.loads(Path(info_path).read_text()) if info_path else None
  reader = for_collection(doc, info=info, fetch=fetch)
  return {"dataset": asdict(reader.dataset), "extent": asdict(reader.extent())}


def format_layer_info(rec: dict) -> str:
  """The record of `layer_info` as text, one fact a line."""
  d, e = rec["dataset"], rec["extent"]
  vars_ = ", ".join(f"{v['name']}{' (categorical)' if v['categorical'] else ''}" for v in d["variables"])
  src = "live from the server" if e["live"] else "from the catalog (the server was not asked, or did not answer)"
  lines = [
    f"id            {d['id']}",
    f"title         {d['title']}",
    f"server        {d['base_url']} ({d['protocol']} {d['dataset_id']}, ERDDAP {d['version']})",
    f"cors          {str(d['cors']).lower()}",
    f"formats       {', '.join(d['formats'])} (reads {d['format']})",
    f"lon_range     {d['lon_range'][0]} to {d['lon_range'][1]}",
    f"lat_descending {str(d['lat_descending']).lower()}",
    f"variables     {vars_}",
    f"time_step     {d['time_step']}",
    f"depth         {d['depth']}",
    f"status        {d['status'] or 'live'}",
    f"time extent   {e['time_start']} to {e['time_end']} ({src})",
  ]
  if e["step_days"] is not None:
    lines.append(f"time step     {e['step_label']} ({e['step_days']:.4g} days, {e['n_values']} values)")
  return "\n".join(lines)
