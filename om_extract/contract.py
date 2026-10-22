"""the values contract: schema, validation, and the one writer every producer uses.

a contract row is (place_id, coll, indicator, time_grain, time, class, value, n, sd, unit) plus optional x_* producer
columns (the platform schema `values-row`). a values file holds one (indicator, coll) at
values/indicator=<indicator>/coll=<coll>/data.parquet, with its provenance (the platform schema `values-provenance`) as
JSON in the parquet key-value metadata under `om:provenance`. the schemas live in oceanmetrics/platform
(`packages/catalog/schemas/`) and are read through `om_extract.schemas`, never copied here. json schema checks one row
at a time; the rules that span rows or need arithmetic (time aligned to its grain, unique keys, one indicator and coll
per file, finite values) are here. moved from oceanmetrics/indicators `om_indicators/contract.py` (contract 1.2.0).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
from functools import cache
from pathlib import Path
from typing import Any, Iterable

import jsonschema
import pyarrow as pa
import pyarrow.parquet as pq

from om_extract import schemas

CONTRACT_VERSION = "1.2.0"
PROVENANCE_KEY = b"om:provenance"
# the platform schemas of a row and of a file's provenance (om_extract.schemas.load_schema names)
VALUES_SCHEMA = "values-row"
PROVENANCE_SCHEMA = "values-provenance"
GRAINS = ("all", "decade", "year", "month", "day")
KEY = ("place_id", "time_grain", "time", "class")
ROLES = ("data", "geometry", "product", "manifest")
# contract 1.2 added the sd column, provenance.dataset as required and sources[].item / role; a 1.1 file stays valid
# without them (validate_table / validate_provenance read the file's own `contract`)
SD_SINCE = (1, 2)
# sd is undefined without a time step behind the value: n = 0 means none. (the spec text said n < 2, but a monthly or
# daily row has n = 1 and carries the SD across the cells of that one step, so n < 2 would null every cell SD)
SD_MIN_N = 1

# the core columns, in order, with their parquet types ----
ARROW_SCHEMA = pa.schema([
  ("place_id", pa.string()),
  ("coll", pa.string()),
  ("indicator", pa.string()),
  ("time_grain", pa.string()),
  ("time", pa.date32()),
  ("class", pa.string()),
  ("value", pa.float64()),
  ("n", pa.float64()),
  ("sd", pa.float64()),
  ("unit", pa.string()),
])
CORE = tuple(ARROW_SCHEMA.names)
SD_COLUMN = "sd"


class ContractError(ValueError):
  """rows or a file that break the contract; `errors` lists every problem found."""

  def __init__(self, errors: list[str]):
    self.errors = errors
    shown = "\n  ".join(errors[:20]) + (f"\n  ... and {len(errors) - 20} more" if len(errors) > 20 else "")
    super().__init__(f"{len(errors)} contract error(s):\n  {shown}")


@cache
def load_schema(name: str) -> dict:
  """a platform schema by name (`values-row`, `values-provenance`), through om_extract.schemas.load_schema."""
  return schemas.load_schema(name)


@cache
def _validator(name: str) -> jsonschema.protocols.Validator:
  schema = load_schema(name)
  cls = jsonschema.validators.validator_for(schema)
  cls.check_schema(schema)
  return cls(schema)


# periods ----
def period_start(grain: str, d: dt.date) -> dt.date | None:
  """the first day of the `grain` period holding date `d` (None for `all`)."""
  if grain == "all":
    return None
  if grain == "decade":
    return dt.date(d.year - d.year % 10, 1, 1)
  if grain == "year":
    return dt.date(d.year, 1, 1)
  if grain == "month":
    return dt.date(d.year, d.month, 1)
  if grain == "day":
    return d
  raise ValueError(f"unknown time_grain {grain!r}")


def _version(contract: Any) -> tuple[int, int] | None:
  """(major, minor) of a contract version string, None when it is not one."""
  m = re.fullmatch(r"(\d+)\.(\d+)\.\d+", contract) if isinstance(contract, str) else None
  return (int(m[1]), int(m[2])) if m else None


def _since(contract: Any, since: tuple[int, int]) -> bool:
  """whether a file of contract version `contract` follows the rules introduced in `since`; an unreadable or absent
  version counts as the current one."""
  v = _version(contract) or _version(CONTRACT_VERSION)
  return v >= since


def _as_date(t: Any) -> dt.date | None:
  if t is None or isinstance(t, dt.date) and not isinstance(t, dt.datetime):
    return t
  if isinstance(t, dt.datetime):
    return t.date()
  return dt.date.fromisoformat(str(t))


# validation ----
def validate_rows(rows: Iterable[dict], *, one_file: bool = True) -> list[str]:
  """every contract problem in `rows` (dicts keyed by column; `time` a date, an ISO string or None). with one_file,
  the rows must also share one indicator and one coll and, when categorical, all carry a class."""
  errors: list[str] = []
  seen: dict[tuple, int] = {}
  indicators, colls, with_class, without_class = set(), set(), 0, 0
  v = _validator(VALUES_SCHEMA)
  for i, r in enumerate(rows):
    t = r.get("time")
    try:
      d = _as_date(t)
    except (TypeError, ValueError):
      errors.append(f"row {i}: time {t!r} is not an ISO date")
      d = None
    as_json = {**r, "time": d.isoformat() if d else None}
    for e in sorted(v.iter_errors(as_json), key=lambda e: list(e.path)):
      if e.validator == "minimum" and list(e.path) == ["sd"]:
        continue    # reported below as the sd rule
      if list(e.path) == ["time"] and e.relative_schema_path and e.relative_schema_path[0] in ("then", "else"):
        continue    # the schema's time-null-for-all if/then/else, reported below as the time rule
      where = ".".join(str(p) for p in e.path) or "row"
      errors.append(f"row {i}: {where}: {e.message}")
    g = r.get("time_grain")
    if g == "all" and t is not None:
      errors.append(f"row {i}: time must be null for time_grain 'all', got {t!r}")
    elif g in GRAINS and g != "all":
      if d is None:
        errors.append(f"row {i}: time is required for time_grain {g!r}")
      elif period_start(g, d) != d:
        errors.append(f"row {i}: time {d.isoformat()} is not the start of a {g} (expected {period_start(g, d).isoformat()})")
    val = r.get("value")
    if isinstance(val, float) and not math.isfinite(val):
      errors.append(f"row {i}: value must be finite or null, got {val!r}")
    n = r.get("n")
    if isinstance(n, float) and not math.isfinite(n):
      errors.append(f"row {i}: n must be finite or null, got {n!r}")
    sd = r.get("sd")
    if isinstance(sd, (int, float)) and not (math.isfinite(sd) and sd >= 0):
      errors.append(f"row {i}: sd must be finite and >= 0 or null, got {sd!r}")
    if sd is not None and val is None:
      errors.append(f"row {i}: sd must be null where value is null, got {sd!r}")
    if sd is not None and n is not None and n < SD_MIN_N:
      errors.append(f"row {i}: sd must be null where n < {SD_MIN_N} (no time step behind it), got sd {sd!r} with n {n!r}")
    key = (r.get("place_id"), r.get("indicator"), g, d, r.get("class"))
    if key in seen:
      errors.append(f"row {i}: duplicate of row {seen[key]} on (place_id, indicator, time_grain, time, class) = {key[0]}, {key[1]}, {g}, {d}, {key[4]}")
    else:
      seen[key] = i
    indicators.add(r.get("indicator"))
    colls.add(r.get("coll"))
    if r.get("class") is None:
      without_class += 1
    else:
      with_class += 1
  if one_file:
    if len(indicators) > 1:
      errors.append(f"a values file holds one indicator, found {len(indicators)}: {sorted(map(str, indicators))}")
    if len(colls) > 1:
      errors.append(f"a values file holds one coll, found {len(colls)}: {sorted(map(str, colls))}")
    if with_class and without_class:
      errors.append(f"class must be set on every row (categorical) or none (continuous): {with_class} set, {without_class} null")
  return errors


def validate_provenance(prov: Any) -> list[str]:
  """every problem with a provenance object. from contract 1.2 `dataset` is required (the schema's if/then) and at least
  one source has the role `data` (explicit, or by default when `role` is absent)."""
  errors = [f"provenance: {'.'.join(str(p) for p in e.path) or 'root'}: {e.message}"
            for e in sorted(_validator(PROVENANCE_SCHEMA).iter_errors(prov), key=lambda e: list(e.path))]
  if isinstance(prov, dict) and _since(prov.get("contract"), SD_SINCE) and isinstance(prov.get("sources"), list):
    srcs = [s for s in prov["sources"] if isinstance(s, dict)]
    if srcs and not any(s.get("role", "data") == "data" for s in srcs):
      errors.append("provenance: sources: at least one source must have role 'data' (contract 1.2)")
  return errors


def validate_table(table: pa.Table, contract: str | None = None) -> list[str]:
  """column names and parquet types, then the rows. `contract` is the version the file declares (its provenance's
  `contract`; default the current one): from 1.2 the `sd` column is required, a 1.1 file may lack it."""
  errors: list[str] = []
  names = table.schema.names
  sd_required = _since(contract, SD_SINCE)
  for f in ARROW_SCHEMA:
    if f.name == SD_COLUMN and not sd_required and f.name not in names:
      continue
    if f.name not in names:
      errors.append(f"missing column {f.name!r}")
    elif not table.schema.field(f.name).type.equals(f.type):
      errors.append(f"column {f.name!r} is {table.schema.field(f.name).type}, expected {f.type}")
  for n in names:
    if n not in CORE and not n.startswith("x_"):
      errors.append(f"unexpected column {n!r} (extra columns must be named x_*)")
  if errors:
    return errors
  return validate_rows(table.to_pylist())


def values_path(root: Path | str, indicator: str, coll: str) -> Path:
  """where the values of one (indicator, coll) live under a catalog root."""
  return Path(root) / "values" / f"indicator={indicator}" / f"coll={coll}" / "data.parquet"


def validate_file(path: Path | str) -> list[str]:
  """a values file: its rows, its provenance, and that both agree with its path."""
  path = Path(path)
  errors: list[str] = []
  try:
    pf = pq.ParquetFile(path)
  except Exception as e:  # noqa: BLE001 -- any unreadable file is one error
    return [f"{path}: unreadable parquet: {e}"]
  table = pf.read()
  meta = pf.schema_arrow.metadata or {}
  if PROVENANCE_KEY not in meta:
    errors.append(f"missing key-value metadata {PROVENANCE_KEY.decode()!r}")
    prov = None
  else:
    try:
      prov = json.loads(meta[PROVENANCE_KEY])
    except json.JSONDecodeError as e:
      errors.append(f"provenance is not JSON: {e}")
      prov = None
  errors += validate_table(table, prov.get("contract") if isinstance(prov, dict) else None)
  if prov is not None:
    errors += validate_provenance(prov)
  ind_dir, coll_dir = path.parent.parent.name, path.parent.name
  ind = ind_dir.removeprefix("indicator=") if ind_dir.startswith("indicator=") else None
  coll = coll_dir.removeprefix("coll=") if coll_dir.startswith("coll=") else None
  if path.name != "data.parquet" or ind is None or coll is None:
    errors.append(f"path must end values/indicator=<id>/coll=<slug>/data.parquet, got {path}")
  if table.num_rows and "indicator" in table.schema.names:
    if ind is not None and set(table.column("indicator").to_pylist()) != {ind}:
      errors.append(f"rows' indicator differs from the path's {ind!r}")
    if coll is not None and set(table.column("coll").to_pylist()) != {coll}:
      errors.append(f"rows' coll differs from the path's {coll!r}")
  if isinstance(prov, dict):
    if ind is not None and prov.get("indicator") != ind:
      errors.append(f"provenance indicator {prov.get('indicator')!r} differs from the path's {ind!r}")
    if coll is not None and prov.get("coll") != coll:
      errors.append(f"provenance coll {prov.get('coll')!r} differs from the path's {coll!r}")
    if isinstance(prov.get("rows"), int) and prov["rows"] != table.num_rows:
      errors.append(f"provenance says {prov['rows']} rows, the file has {table.num_rows}")
  return [f"{path}: {e}" for e in errors]


def validate_tree(root: Path | str) -> dict[str, list[str]]:
  """validate_file() for every values file under a catalog root; only files with problems are returned."""
  out = {}
  for f in sorted(Path(root).glob("values/indicator=*/coll=*/data.parquet")):
    errs = validate_file(f)
    if errs:
      out[str(f)] = errs
  return out


# writing ----
def _sort_key(r: dict) -> tuple:
  d = _as_date(r.get("time"))
  return (r["place_id"], GRAINS.index(r["time_grain"]) if r["time_grain"] in GRAINS else 99,
          d or dt.date.min, r.get("class") or "")


def to_table(rows: list[dict]) -> pa.Table:
  """rows -> an arrow table in contract column order and types, x_* columns after, rows in a stable order
  (place_id, grain, time, class)."""
  rows = sorted(rows, key=_sort_key)
  extras = sorted({k for r in rows for k in r if k.startswith("x_")})
  cols = {f.name: pa.array([_as_date(r.get(f.name)) if f.name == "time" else r.get(f.name) for r in rows], type=f.type)
          for f in ARROW_SCHEMA}
  for x in extras:
    cols[x] = pa.array([r.get(x) for r in rows])
  return pa.table(cols)


def write_values(rows: list[dict], root: Path | str, provenance: dict) -> Path:
  """validate and write one values file (one indicator, one coll) under `root`; raises ContractError on any problem.
  `provenance` gets `indicator` and `coll` filled in when absent, and `contract` (always the current version) and `rows` set."""
  if not rows:
    raise ContractError(["no rows to write"])
  table = to_table(rows)
  ind, coll = rows[0]["indicator"], rows[0]["coll"]
  # a writer always writes the current contract: `sd` is all null when the rows carry none (to_table)
  prov = {"indicator": ind, "coll": coll, **provenance, "contract": CONTRACT_VERSION, "rows": table.num_rows}
  errors = validate_table(table, CONTRACT_VERSION) + validate_provenance(prov)
  if errors:
    raise ContractError(errors)
  path = values_path(root, ind, coll)
  path.parent.mkdir(parents=True, exist_ok=True)
  meta = {**(table.schema.metadata or {}), PROVENANCE_KEY: json.dumps(prov, sort_keys=True).encode()}
  pq.write_table(table.replace_schema_metadata(meta), path, compression="snappy")
  return path


def read_values(path: Path | str) -> tuple[pa.Table, dict | None]:
  """a values file's rows and its provenance."""
  pf = pq.ParquetFile(path)
  meta = pf.schema_arrow.metadata or {}
  prov = json.loads(meta[PROVENANCE_KEY]) if PROVENANCE_KEY in meta else None
  return pf.read().replace_schema_metadata(None), prov


def utc_now() -> str:
  """the created stamp, to the second."""
  return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

