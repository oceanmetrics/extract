"""Per place and time step statistics: erddap-places' SQL templates, run by DuckDB over a slab and a mask.

The templates in `om_extract/sql/` are byte-for-byte copies of erddap-places `sql/` (a test asserts it when that
checkout sits beside this repo), and `render()` / `lit()` are character-for-character `app/src/lib/sql.ts` and
`precompute/src/sql.ts`: `{{name}}` placeholders, where `var`, `expr`, `slab`, `mask` and `src` are spliced as
they are (identifiers and expressions) and every other parameter is quoted as a literal. So the runner, the
browser (DuckDB-WASM) and the DuckDB CLI run the same text.

- `stats_daily.sql`: continuous variables; per UTC day `date, n, mean, mean_wt, sd, min, max, p10, p90,
  weight_sum` (`mean_wt = sum(value * weight) / sum(weight)`, `weight_sum = sum(weight)` over the cells with
  data; `n`, `mean`, `sd`, `min`, `max`, `p10`, `p90` are unweighted over those cells).
- `stats_categorical.sql`: per date and class `date, class, n, weight, fraction, percent_cells` (`fraction`
  area-weighted, summing to 1 per date).
- `last_step.sql`: the masked cells of the last time step, `date, latitude, longitude, weight, value`.

The slab carries `time`, `latitude`, `longitude` and the variable; the mask carries `latitude`, `longitude`,
`weight` (`MaskTable.to_arrow()`). They are joined on `ROUND(x, 3)` equality, as the templates say.
"""
from __future__ import annotations

import re
from decimal import Decimal
from importlib.resources import files
from typing import Any

import pyarrow as pa

# params spliced literally (identifiers and expressions: table/column names); everything else quoted (sql.ts RAW)
RAW = frozenset({"var", "expr", "slab", "mask", "src"})
_AXES = ("time", "latitude", "longitude", "depth", "altitude", "zlev")


def template_names() -> list[str]:
  """the templates shipped in om_extract/sql (sql.ts `templateNames()`)."""
  return sorted(p.name[:-4] for p in files("om_extract").joinpath("sql").iterdir() if p.name.endswith(".sql"))


def template(name: str) -> str:
  """the text of one template (sql.ts `template()`)."""
  p = files("om_extract").joinpath("sql", f"{name}.sql")
  if not p.is_file():
    raise KeyError(f"no SQL template {name} (have: {', '.join(template_names())})")
  return p.read_text(encoding="utf-8")


def _js_number(v: float) -> str:
  """`String(v)` of a finite JS number (ECMAScript Number::toString): the shortest round-trip digits (python's
  `repr` finds the same ones), in exponent form only when the decimal exponent is >= 21 or <= -7."""
  if v == 0:
    return "0"
  t = Decimal(repr(abs(v))).normalize().as_tuple()
  digits = "".join(map(str, t.digits))
  k = len(digits)
  n = k + t.exponent  # value = 0.<digits> * 10^n
  if k <= n <= 21:
    s = digits + "0" * (n - k)
  elif 0 < n <= 21:
    s = digits[:n] + "." + digits[n:]
  elif -6 < n <= 0:
    s = "0." + "0" * (-n) + digits
  else:
    e = n - 1
    s = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + ("+" if e >= 0 else "-") + str(abs(e))
  return ("-" if v < 0 else "") + s


def lit(v: Any) -> str:
  """a parameter as a SQL literal (sql.ts `lit()`): NULL, a JS-formatted number, TRUE/FALSE, or a quoted string."""
  if v is None:
    return "NULL"
  if isinstance(v, bool):
    return "TRUE" if v else "FALSE"
  if isinstance(v, (int, float)):
    f = float(v)
    return _js_number(f) if f == f and f not in (float("inf"), float("-inf")) else "NULL"
  return "'" + str(v).replace("'", "''") + "'"


def render(name: str, params: dict[str, Any], *, text: str | None = None) -> str:
  """a template with its `{{name}}` placeholders filled (sql.ts `render()`); a missing parameter raises."""
  body = template(name) if text is None else text

  def sub(m: re.Match) -> str:
    k = m.group(1)
    if k not in params:
      raise KeyError(f"template {name}: missing param {k}")
    return str(params[k]) if k in RAW else lit(params[k])
  return re.sub(r"\{\{(\w+)\}\}", sub, body).strip()


def _as_arrow(t: Any) -> pa.Table:
  if isinstance(t, pa.Table):
    return t
  if hasattr(t, "to_arrow"):
    return t.to_arrow()
  return pa.table(t)


def value_column(slab: pa.Table) -> str:
  """the one variable column of a slab (every column that is not an axis); more or fewer raises."""
  vs = [c for c in slab.column_names if c not in _AXES]
  if len(vs) != 1:
    raise ValueError(f"slab has {len(vs)} variable columns {vs}: pass expr=")
  return vs[0]


def stats(slab_table: Any, mask_table: Any, template: str | None = None, *, categorical: bool = False,
          expr: str | None = None, con: Any = None) -> pa.Table:
  """Run one template over a slab and a mask in DuckDB and return its rows as an Arrow table.

  `slab_table`: an Arrow table (or a dict of columns) with `time`, `latitude`, `longitude` and the variable,
  registered as `slab`. `mask_table`: a `MaskTable`, an Arrow table or a dict with `latitude`, `longitude`,
  `weight`, registered as `mask`. `template`: a name from `template_names()`; by default `stats_categorical`
  when `categorical`, else `stats_daily`. `expr`: the value expression over the slab alias `s` (e.g.
  `(s."analysed_sst" - 273.15)`); by default `s."<the slab's one variable column>"`. The parameters are the
  ones engine.ts and precompute pass: `{expr, slab: 'slab', mask: 'mask'}`."""
  import duckdb

  slab = _as_arrow(slab_table)
  mask = _as_arrow(mask_table)
  name = template or ("stats_categorical" if categorical else "stats_daily")
  if expr is None:
    expr = '"' + value_column(slab).replace('"', '""') + '"'
    expr = f"s.{expr}"
  sql = render(name, {"expr": expr, "slab": "slab", "mask": "mask"})
  own = con is None
  c = duckdb.connect(":memory:") if own else con
  try:
    c.register("slab", slab)
    c.register("mask", mask)
    rel = c.sql(sql)
    # duckdb 2 names it to_arrow_table(); 1.x fetch_arrow_table()
    return rel.to_arrow_table() if hasattr(rel, "to_arrow_table") else rel.fetch_arrow_table()
  finally:
    c.unregister("slab")
    c.unregister("mask")
    if own:
      c.close()
