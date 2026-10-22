"""JSON as JavaScript's `JSON.stringify` writes it, so a hash over it or a file of it matches erddap-places' bytes.

`JSON.stringify` prints a number as `Number.prototype.toString` does (`1` not `1.0`, `-180` not `-180.0`, `1e-7`),
leaves non-ASCII text unescaped, writes `null` for a non-finite number and drops a key whose value is `undefined`
(here: a dict value that is the `UNDEFINED` sentinel). With `indent`, every array element and object member sits on
its own line and an empty array or object stays `[]` / `{}`, as V8 does.
"""
from __future__ import annotations

import json
import math
from typing import Any

from om_extract.readers.erddap import js_num


class _Undefined:
  def __repr__(self) -> str:
    return "UNDEFINED"


# a dict value that JSON.stringify would drop (a TS optional key left undefined)
UNDEFINED = _Undefined()


def _scalar(v: Any) -> str:
  if v is None:
    return "null"
  if isinstance(v, bool):
    return "true" if v else "false"
  if isinstance(v, (int, float)):
    if isinstance(v, float) and not math.isfinite(v):
      return "null"
    return js_num(v)
  if isinstance(v, str):
    return json.dumps(v, ensure_ascii=False)
  # numpy scalars and the like
  if hasattr(v, "item"):
    return _scalar(v.item())
  raise TypeError(f"not JSON: {type(v).__name__}")


def js_json(v: Any, indent: int | None = None, _level: int = 0) -> str:
  """`JSON.stringify(v, null, indent)`: compact without `indent` (`[1,"a",null]`), else one item per line."""
  if isinstance(v, dict):
    items = [(k, x) for k, x in v.items() if x is not UNDEFINED]
    if not items:
      return "{}"
    if indent is None:
      return "{" + ",".join(f"{json.dumps(str(k), ensure_ascii=False)}:{js_json(x)}" for k, x in items) + "}"
    pad, inner = " " * (indent * _level), " " * (indent * (_level + 1))
    body = ",\n".join(f"{inner}{json.dumps(str(k), ensure_ascii=False)}: {js_json(x, indent, _level + 1)}"
                      for k, x in items)
    return "{\n" + body + "\n" + pad + "}"
  if isinstance(v, (list, tuple)):
    if not v:
      return "[]"
    # an undefined array element is written as null
    vals = [None if x is UNDEFINED else x for x in v]
    if indent is None:
      return "[" + ",".join(js_json(x) for x in vals) + "]"
    pad, inner = " " * (indent * _level), " " * (indent * (_level + 1))
    return "[\n" + ",\n".join(inner + js_json(x, indent, _level + 1) for x in vals) + "\n" + pad + "]"
  return _scalar(v)
