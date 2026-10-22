"""The ERDDAP reader: griddap Parquet slabs, ported from erddap-places (`erddap.ts`, `extent.ts`, `fetchRetry.ts`).

URL rules (griddap): `{base}/griddap/{dataset}.{fmt}?{var}[(t0):1:(t1)][(lat0):1:(lat1)][(lon0):1:(lon1)]`. Each
constraint's square brackets are percent-encoded (`%5B`, `%5D`) and nothing else is; ERDDAP refuses a fully encoded
query. Constraint values go in the axis's own direction: a descending latitude axis is requested high to low. A grid
with a depth axis gets one more constraint, a single level, after time. Longitudes are clamped to `erddap:lon_range`.
A slab is cut into requests of at most 90 days or 100,000 values (`chunk_days_for`), each fetched as Parquet.
"""
import json
import math
import re
import time as _time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

import httpx
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from om_extract import schemas
from om_extract.readers.base import Axes, BBox, Extent, SLAB_SCHEMA, SLICE_SCHEMA, TIME_TYPE

Format = Literal["parquet", "parquetWMeta", "csvp", "jsonp"]
Fetch = Callable[[str], bytes]

MAX_CHUNK_DAYS = 90          # time steps a request spans at most (precompute/src/targets.ts)
MAX_REQUEST_VALUES = 100_000  # values a request returns at most (extractr's n_max_vals_per_req)
# per-attempt timeouts in seconds: a busy server is slow whatever the size (PacIOOS answered axis requests in 13-19 s
# and 45 days of one pixel in 27-31 s on 2026-10-20); a slab is a runner-sized download
INFO_TIMEOUT_S = 30
AXIS_TIMEOUT_S = 45
SLAB_TIMEOUT_S = 300

_EXT: dict[str, str] = {"parquet": ".parquet", "parquetWMeta": ".parquetWMeta", "csvp": ".csvp", "jsonp": ".json"}


# -- numbers and strings as javascript prints them -----------------------------------------------------------------
def js_num(x) -> str:
  """A number the way a JavaScript template literal prints it (`1` not `1.0`, `0.4940254`, `1e-7`); strings pass through."""
  if isinstance(x, str):
    return x
  if isinstance(x, bool):
    return "true" if x else "false"
  if isinstance(x, int):
    return str(x)
  x = float(x)
  if math.isnan(x):
    return "NaN"
  if math.isinf(x):
    return "Infinity" if x > 0 else "-Infinity"
  if x == 0:
    return "0"
  if x.is_integer() and abs(x) < 1e21:
    return str(int(x))
  d = Decimal(repr(x))
  exp = d.adjusted()
  if -6 <= exp < 21:
    return format(d, "f")
  sign, digits, _ = d.as_tuple()
  ds = "".join(map(str, digits)).rstrip("0") or "0"
  mant = ds[0] + ("." + ds[1:] if len(ds) > 1 else "")
  return f"{'-' if sign else ''}{mant}e{'+' if exp > 0 else '-'}{abs(exp)}"


def _enc(s: str) -> str:
  return s.replace("[", "%5B").replace("]", "%5D")


def _strip_slash(s: str) -> str:
  return re.sub(r"/+$", "", s)


def constraint(lo, hi, stride=1) -> str:
  """One axis constraint, `[(lo):1:(hi)]`, with the brackets encoded."""
  return _enc(f"[({js_num(lo)}):{js_num(stride)}:({js_num(hi)})]")


def noon_z(d: date | datetime | str) -> str:
  """An ISO instant at 12:00Z for a date, a datetime (UTC day) or a yyyy-mm-dd string (ERDDAP daily grids sit at noon)."""
  if isinstance(d, str):
    iso = d[:10]
  elif isinstance(d, datetime):
    iso = d.astimezone(timezone.utc).date().isoformat() if d.tzinfo else d.date().isoformat()
  else:
    iso = d.isoformat()
  return f"{iso}T12:00:00Z"


# -- griddap and tabledap URLs ---------------------------------------------------------------------------------------
def griddap_url(*, base: str, dataset_id: str, variable: str, time: Sequence[str], lat: Sequence[float],
                lon: Sequence[float], lat_descending: bool = True, depth: float | None = None,
                format: Format = "parquet", callback: str | None = None) -> str:
  """The griddap request URL for one lobe. `lat` and `lon` are `[min, max]` in any order; latitude is written
  high to low when `lat_descending`. `depth` adds a single-level constraint after time (a 4-D grid)."""
  y0, y1 = min(lat), max(lat)
  x0, x1 = min(lon), max(lon)
  q = (variable
       + constraint(time[0], time[1])
       + (constraint(depth, depth) if depth is not None and math.isfinite(depth) else "")
       + (constraint(y1, y0) if lat_descending else constraint(y0, y1))
       + constraint(x0, x1))
  tail = f"&.jsonp={callback or 'erddapCb'}" if format == "jsonp" else ""
  return f"{_strip_slash(base)}/griddap/{dataset_id}{_EXT[format]}?{q}{tail}"


def griddap_slice_url(*, at: str, **spec) -> str:
  """The griddap URL for one time step (`[(t):1:(t)]`): the same axis order, longitude range and depth as `griddap_url`."""
  return griddap_url(time=[at, at], **spec)


def griddap_urls(*, bboxes: Sequence[BBox], **spec) -> list[str]:
  """One URL per polygon lobe (bbox = `[lon_min, lat_min, lon_max, lat_max]`); `spec` is `griddap_url` without lat and lon."""
  return [griddap_url(lon=[b[0], b[2]], lat=[b[1], b[3]], **spec) for b in bboxes]


@dataclass(frozen=True)
class TableConstraint:
  """One tabledap constraint: `column op value`, op one of `>= <= > < = != =~`."""
  column: str
  op: str
  value: str | float


# only the comparison characters are encoded; `=` stays literal, as ERDDAP's own example URLs do
_OPS = {">=": "%3E=", "<=": "%3C=", ">": "%3E", "<": "%3C", "=": "=", "!=": "!=", "=~": "=~"}


def table_constraint(c: TableConstraint) -> str:
  """One tabledap constraint, `column%3E=value`."""
  return f"{c.column}{_OPS.get(c.op, c.op)}{js_num(c.value)}"


def tabledap_url(*, base: str, dataset_id: str, columns: Sequence[str], constraints: Sequence[TableConstraint] = (),
                 format: Format = "parquetWMeta", callback: str | None = None,
                 functions: Sequence[str] = ()) -> str:
  """The tabledap request URL: the column list with `%2C` between columns, then one `&constraint` each, then `functions`
  (server-side `distinct()`, `orderByMax("time")`, appended literally)."""
  q = ["%2C".join(columns), *(table_constraint(c) for c in constraints), *functions]
  tail = f"&.jsonp={callback or 'erddapCb'}" if format == "jsonp" else ""
  return f"{_strip_slash(base)}/tabledap/{dataset_id}{_EXT[format]}?{'&'.join(q)}{tail}"


def tabledap_place_constraints(*, bbox: BBox, from_: str, to: str, type_column: str | None = None,
                               type_value: str | None = None) -> list[TableConstraint]:
  """The constraints for one place lobe and one time window: a bbox and a time range, plus the long-format row filter
  when the dataset keeps its variables in a `measurement_type` column (`erddap-places:long_format`)."""
  def iso(d: str, end: bool = False) -> str:
    return d if len(d) > 10 else f"{d}T{'23:59:59' if end else '00:00:00'}Z"
  cs = [TableConstraint("time", ">=", iso(from_)), TableConstraint("time", "<=", iso(to, True)),
        TableConstraint("longitude", ">=", bbox[0]), TableConstraint("longitude", "<=", bbox[2]),
        TableConstraint("latitude", ">=", bbox[1]), TableConstraint("latitude", "<=", bbox[3])]
  # a string value is quoted, and the quotes percent-encoded (%22), as ERDDAP requires
  if type_column and type_value:
    cs.append(TableConstraint(type_column, "=", f"%22{type_value}%22"))
  return cs


def info_url(base: str, dataset_id: str) -> str:
  """`<base>/info/<datasetID>/index.json`, the metadata table of a dataset."""
  return f"{_strip_slash(base)}/info/{dataset_id}/index.json"


def pick_format(collection: dict, *, browser: bool = True) -> Format:
  """The best format this server can serve us: Parquet, then CSV (`csvp`), then JSONP. `collection` is
  `{"erddap": {"cors": bool, "formats": [...]}}`. CORS off means only JSONP (a script tag is not subject to CORS);
  `browser=False` ignores CORS, as the runner is not a browser."""
  e = (collection or {}).get("erddap") or {}
  fmts = [re.sub(r"^\.", "", f).lower() for f in (e.get("formats") or [])]
  if browser and e.get("cors") is False:
    return "jsonp"
  if not fmts:
    return "parquet"
  # parquetWMeta is plain Parquet with ERDDAP's metadata in the file footer: prefer it when offered
  if "parquetwmeta" in fmts:
    return "parquetWMeta"
  if "parquet" in fmts:
    return "parquet"
  if "csvp" in fmts or "csv" in fmts:
    return "csvp"
  return "jsonp"


def to_dataset_lon(lon: float, rng: Sequence[float]) -> float:
  """A longitude in the dataset's own frame, [-180, 180] or [0, 360]."""
  if rng[1] > 180 and lon < 0:
    return lon + 360
  if rng[0] < 0 and lon > 180:
    return lon - 360
  return lon


def lobe_lon_span(bbox: Sequence[float], rng: Sequence[float]) -> tuple[float, float]:
  """A lobe's longitude span in the dataset's own frame, `(min, max)`, clamped to `erddap:lon_range`: a lobe that runs
  to 180 on a grid ending at 179.75 (CMEMS 0.25 degrees) would otherwise ask past the axis maximum, which ERDDAP refuses (404)."""
  lo, hi = to_dataset_lon(bbox[0], rng), to_dataset_lon(bbox[2], rng)
  def clamp(x: float) -> float:
    return min(max(x, rng[0]), rng[1])
  return (clamp(min(lo, hi)), clamp(max(lo, hi)))


# -- chunking ------------------------------------------------------------------------------------------------------------
def day(iso: str) -> str:
  """The UTC calendar day (yyyy-mm-dd) of an ISO instant."""
  return iso[:10]


def add_days(d: str, n: int) -> str:
  """yyyy-mm-dd, n days after `d` (n may be negative)."""
  return (date.fromisoformat(day(d)) + timedelta(days=n)).isoformat()


def days_between(a: str, b: str) -> int:
  """Whole days from `a` to `b`, by UTC calendar day."""
  return (date.fromisoformat(day(b)) - date.fromisoformat(day(a))).days


def chunk_days_for(cells: float, step_days: float = 1, max_values: int = MAX_REQUEST_VALUES,
                   max_days: int = MAX_CHUNK_DAYS) -> int:
  """Days per request: `max_days` time steps (90 days of a daily grid; a coarser grid's steps are counted in its own
  days, so a monthly product asks for up to 90 months at once), shrunk so one request stays under `max_values` values.
  `cells` is the largest lobe's axis cells per step: each lobe is its own request. Never below one step."""
  sd = max(1, step_days)
  cap = math.floor(max_days * sd)
  if not cells > 0:
    return cap
  steps = max(1, math.floor(max_values / cells))
  return max(1, min(cap, math.floor(steps * sd)))


def chunks(start: str, end: str, chunk_days: int) -> list[tuple[str, str]]:
  """Split [start, end] into consecutive, non-overlapping slices of at most `chunk_days` days."""
  out: list[tuple[str, str]] = []
  s = start
  while days_between(s, end) >= 0:
    e = add_days(s, chunk_days - 1)
    out.append((s, end if days_between(e, end) < 0 else e))
    s = add_days(e, 1)
  return out


# -- the live time extent ----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class TimeExtent:
  """The time range of a dataset: ISO instants of the first and last step, the average spacing in days and its label."""
  start: str
  end: str | None
  step_days: float | None = None
  step_label: str | None = None
  n_values: int | None = None


def _iso_z(secs: float) -> str:
  return datetime.fromtimestamp(math.floor(secs), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_spacing(s: str) -> float | None:
  """`8 days 0h 7m 57s` / `1 day` / `12h` to days."""
  def num(m: re.Match | None) -> float:
    return float(m.group(1)) if m else 0.0
  m = re.search(r"([\d.]+)\s*days?", s, re.I | re.A)
  try:
    days = float(m.group(1)) if m else 0.0
  except ValueError:
    return None
  days += (num(re.search(r"(\d+)\s*h\b", s, re.A)) / 24 + num(re.search(r"(\d+)\s*m\b", s, re.A)) / 1440
           + num(re.search(r"(\d+)\s*s\b", s, re.A)) / 86400)
  return days if days > 0 else None


def step_label(days: float | None) -> str | None:
  """A human label for a time step in days."""
  if not days or not math.isfinite(days):
    return None
  d = math.floor(days + 0.5)
  if days < 1:
    return "sub-daily"
  if d == 1:
    return "daily"
  if d == 7:
    return "weekly"
  if 28 <= d <= 31:
    return "monthly"
  if 360 <= d <= 366:
    return "annual"
  return f"{d}-day"


def parse_info_extent(info: dict, axis: str = "time") -> TimeExtent | None:
  """The ERDDAP `info/<id>/index.json` table to the dataset's live time extent; None when it has neither an
  `actual_range` on the axis nor `time_coverage_start/end` globally."""
  rows = [["" if c is None else str(c) for c in r] for r in ((info or {}).get("table") or {}).get("rows") or []]

  def find(typ: str, var: str, attr: str | None) -> str | None:
    for r in rows:
      if len(r) > 4 and r[0] == typ and r[1] == var and (attr is None or r[2] == attr):
        return r[4]
    return None

  start = end = None
  rng = find("attribute", axis, "actual_range")
  if rng:
    try:
      a, b = (float(x.strip()) for x in rng.split(",")[:2])
      lo, hi = (a, b) if a <= b else (b, a)
      start, end = _iso_z(lo), _iso_z(hi)
    except ValueError:
      pass
  # fall back to the NC_GLOBAL coverage attributes (already ISO strings)
  if start is None:
    start = find("attribute", "NC_GLOBAL", "time_coverage_start")
  if end is None:
    end = find("attribute", "NC_GLOBAL", "time_coverage_end")
  if not start or not end:
    return None
  dim = ""
  for r in rows:
    if len(r) > 4 and r[0] == "dimension" and r[1] == axis:
      dim = r[4]
      break
  sm = re.search(r"averageSpacing=([^,]+)", dim)
  step = parse_spacing(sm.group(1).strip() if sm else "")
  nm = re.search(r"nValues=(\d+)", dim, re.A)
  return TimeExtent(start=start, end=end, step_days=step, step_label=step_label(step),
                    n_values=int(nm.group(1)) if nm else None)


def span_days(ext: TimeExtent | None, days: int = 30, min_steps: int = 8, max_days: int = 90) -> int:
  """How many days a default window spans for a dataset of this step."""
  step = ext.step_days if ext and ext.step_days is not None else 1
  return min(max_days, max(days, math.ceil(min_steps * step) if step > 1 else days))


def default_window(ext: TimeExtent | None, **opts) -> tuple[str, str]:
  """The last `days` (or `min_steps` steps) ending at the dataset's last time step, as `(start, end)` days."""
  end = day(ext.end) if ext and ext.end else add_days(datetime.now(timezone.utc).date().isoformat(), -2)
  start = add_days(end, -(span_days(ext, **opts) - 1))
  if ext and days_between(ext.start, start) < 0:
    start = day(ext.start)
  return (start, end)


def clamp_window(start: str, end: str, ext: TimeExtent | None, **opts) -> tuple[str, str, bool, str | None]:
  """Keep a window inside the dataset's extent; returns `(start, end, snapped, reason)`. A start after the last step
  (or an end before the first) snaps the whole window back to the default one, with a reason."""
  if ext is None or not ext.end:
    return (start, end, False, None)
  lo, hi = day(ext.start), day(ext.end)
  if days_between(start, hi) < 0 or days_between(lo, end) < 0:
    ds, de = default_window(ext, **opts)
    why = (f"the requested window starts after this dataset ends ({hi})" if days_between(start, hi) < 0
           else f"the requested window ends before this dataset starts ({lo})")
    return (ds, de, True, f"{why}; showing {ds} to {de} instead")
  s = lo if days_between(lo, start) < 0 else start
  e = hi if days_between(end, hi) < 0 else end
  snapped = s != start or e != end
  return (s, e, snapped, f"clamped to the dataset extent ({lo} to {hi})" if snapped else None)


def time_instant(date_: str, ext: TimeExtent | None, noon: Callable[[str], str] = noon_z) -> str:
  """The ISO instant for a griddap time constraint on a calendar day. The extent's own endpoints are used verbatim, so
  a request never overshoots an axis whose steps are not at 12:00Z (MUR sits at 09:00Z); an interior day falls back to noon."""
  if ext:
    if ext.end and day(date_) == day(ext.end):
      return ext.end
    if day(date_) == day(ext.start):
      return ext.start
  return noon(date_)


# -- fetching: a per-attempt timeout, one retry, an error that names the host --------------------------------------------
class ErddapError(Exception):
  """ERDDAP answered with an error (a 4xx is the server's answer to a bad constraint and is not retried)."""


class ErddapUnreachable(ErddapError):
  """The host did not answer, or answered with a 5xx, on every attempt."""

  def __init__(self, host: str, reason: str, attempts: int, url: str):
    super().__init__(f"{host} did not answer ({reason}{f', {attempts} tries' if attempts > 1 else ''})")
    self.host, self.reason, self.attempts, self.url = host, reason, attempts, url


class InvalidCollection(ValueError):
  """The collection does not validate against the `erddap-grid` schema; `errors` holds the messages."""

  def __init__(self, collection_id, errors: list[str]):
    super().__init__(f"collection {collection_id!r} is not a valid erddap-grid:\n" + "\n".join(f"  {e}" for e in errors))
    self.errors = errors


def host_of(url: str) -> str:
  """The host of a URL, or the string itself when it is not one."""
  return httpx.URL(url).netloc.decode() if "://" in url else url


def make_fetch(*, timeout_s: float = 20, attempts: int = 2, backoff_s: float = 1.5,
               client: httpx.Client | None = None, sleep: Callable[[float], None] = _time.sleep) -> Fetch:
  """A `fetch(url) -> bytes` for a server that is not ours. Each attempt gives up after `timeout_s` (the body download
  counts); a network error, a timeout or a 5xx is retried after `backoff_s`; a 4xx raises `ErddapError` with the
  server's message at once. The last failure is an `ErddapUnreachable` that names the host."""
  def fetch(url: str) -> bytes:
    reason = ""
    owned = client is None
    c = client or httpx.Client(follow_redirects=True)
    try:
      for i in range(attempts):
        if i:
          sleep(backoff_s)
        deadline = _time.monotonic() + timeout_s
        try:
          with c.stream("GET", url, timeout=timeout_s) as res:
            if res.status_code >= 500:
              reason = f"HTTP {res.status_code} {res.reason_phrase}".strip()
              continue
            body = bytearray()
            for part in res.iter_bytes():
              body += part
              if _time.monotonic() > deadline:
                raise httpx.ReadTimeout("body not complete in time")
            if res.status_code >= 400:
              msg = body[:400].decode("utf-8", "replace")
              raise ErddapError(f"{res.status_code} {res.reason_phrase} from ERDDAP: {msg}")
            return bytes(body)
        except httpx.TimeoutException:
          reason = f"no response within {round(timeout_s)} s"
        except httpx.TransportError as e:
          reason = f"network error: {e}"
      raise ErddapUnreachable(host_of(url), reason, attempts, url)
    finally:
      if owned:
        c.close()
  return fetch


def fetch_axis(base: str, dataset_id: str, axis: str, lo: float, hi: float, descending: bool = False, *,
               fetch: Fetch | None = None) -> list[float]:
  """`dataset.json?latitude[(min):1:(max)]` to the axis values, in the server's own order."""
  c = constraint(hi, lo) if descending else constraint(lo, hi)
  url = f"{_strip_slash(base)}/griddap/{dataset_id}.json?{axis}{c}"
  j = json.loads((fetch or make_fetch(timeout_s=AXIS_TIMEOUT_S))(url))
  return [float(r[0]) for r in j["table"]["rows"]]


# -- the reader ----------------------------------------------------------------------------------------------------------
def _is_finite_number(x) -> bool:
  if isinstance(x, bool):
    return False
  try:
    return x is not None and math.isfinite(float(x))
  except (TypeError, ValueError):
    return False


def _lattice(lo: float, hi: float, step: float) -> np.ndarray:
  """Evenly spaced centres from `lo` to `hi`; the count comes from the (rounded) `step`, the spacing from the exact
  ends, so the rounding of a step such as 0.083333 does not drift along the axis."""
  n = max(1, int(math.floor((hi - lo) / step + 0.5)) + 1)
  return np.round(np.linspace(lo, hi, n), 9)


def _cover(vals: np.ndarray, a: float, b: float) -> np.ndarray:
  """The centres from the one nearest `a` to the one nearest `b`: ERDDAP snaps both ends of a constraint to the nearest
  axis value. Raises when [a, b] lies wholly beyond the axis (ERDDAP answers 404)."""
  half = abs(vals[-1] - vals[0]) / (len(vals) - 1) / 2 if len(vals) > 1 else 0.0
  if b < vals[0] - half or a > vals[-1] + half:
    raise ValueError(f"[{a}, {b}] is outside the axis [{vals[0]}, {vals[-1]}]")
  i0, i1 = int(np.abs(vals - a).argmin()), int(np.abs(vals - b).argmin())
  return vals[i0:i1 + 1]


class ErddapGrid:
  """The reader for an `erddap-grid` collection (griddap; a tabledap collection builds and reports its extent but has no slab).

  `fetch(url) -> bytes` is injectable (tests feed bytes, nothing touches the network); without it each kind of request
  gets `make_fetch` with its own timeout. `info` is a preloaded `info/<id>/index.json` table (the CLI's `--info`);
  without it `extent()` asks the server.
  """
  kind = "erddap"

  def __init__(self, collection: dict, *, fetch: Fetch | None = None, info: dict | None = None, validate: bool = True):
    from om_extract.layers import dataset_of  # deferred: layers imports pick_format from this module
    if validate:
      errors = schemas.validate("erddap-grid", collection)
      if errors:
        raise InvalidCollection(collection.get("id"), errors)
    self.collection = collection
    self.dataset = dataset_of(collection)
    self._fetch = fetch
    self._info = info
    self._extent: Extent | None = None

  def _fetcher(self, timeout_s: float) -> Fetch:
    return self._fetch or make_fetch(timeout_s=timeout_s)

  def _cube(self, name: str) -> dict:
    return (self.collection.get("cube:dimensions") or {}).get(name) or {}

  # extent ----
  def time_extent(self) -> TimeExtent | None:
    """The live time extent from `info/<id>/index.json` (the given `info`, else asked of the server); None when the
    server cannot say. The catalog's `cube:dimensions.time.extent` end is null or stale for near-real-time grids."""
    info = self._info
    if info is None:
      try:
        info = json.loads(self._fetcher(INFO_TIMEOUT_S)(info_url(self.dataset.base_url, self.dataset.dataset_id)))
      except (ErddapError, ValueError):
        return None
    return parse_info_extent(info)

  def extent(self) -> Extent:
    """Time range (live from the server's info table; the catalog's when the server does not answer), the bbox and the depth levels."""
    if self._extent is None:
      live = self.time_extent()
      cube_t = self._cube("time").get("extent") or [None, None]
      t0 = live.start if live else cube_t[0]
      t1 = live.end if live else cube_t[1]
      lat, lon = self._cube("latitude").get("extent"), self._cube("longitude").get("extent")
      if lat and lon:
        bbox = (float(lon[0]), float(lat[0]), float(lon[1]), float(lat[1]))
      else:
        sb = ((self.collection.get("extent") or {}).get("spatial") or {}).get("bbox") or [None]
        bbox = tuple(float(v) for v in sb[0][:4]) if sb[0] and len(sb[0]) >= 4 else None
      dep = self._cube("depth")
      levels = tuple(float(v) for v in dep.get("values") or ())
      if not levels and self.dataset.depth is not None:
        levels = (self.dataset.depth,)
      self._extent = Extent(time_start=t0, time_end=t1, bbox=bbox, depth_levels=levels,
                            step_days=live.step_days if live else None, step_label=live.step_label if live else None,
                            n_values=live.n_values if live else None, live=live is not None)
    return self._extent

  # axes ----
  def axes(self, bbox: BBox, depth: float | None = None, *, live: bool = False) -> Axes:
    """The cell centres a request over `bbox` covers, in the server's order (high to low when latitude descends), with
    longitudes in the dataset's frame clamped to `erddap:lon_range`.

    By default they come from the catalog's `cube:dimensions` lattice (no request; within the rounding of the
    published step). `live=True`, or an axis the catalog gives no step for, asks the server for the axis vectors.
    """
    ds = self.dataset
    if bbox[0] > bbox[2] or bbox[1] > bbox[3]:
      raise ValueError(f"bbox {tuple(bbox)} is not [lon_min, lat_min, lon_max, lat_max] with min <= max; "
                       "split a lobe that crosses the antimeridian")
    lo, hi = lobe_lon_span(bbox, ds.lon_range)
    if to_dataset_lon(bbox[0], ds.lon_range) > to_dataset_lon(bbox[2], ds.lon_range):
      raise ValueError(f"bbox {tuple(bbox)} straddles the seam of a {list(ds.lon_range)} grid; split it into two lobes")
    cube_lat, cube_lon = self._cube("latitude"), self._cube("longitude")
    steps = (cube_lat.get("step"), cube_lon.get("step"))
    use_cube = (not live and all(c.get("extent") and s for c, s in zip((cube_lat, cube_lon), steps)))
    if use_cube:
      lat_all = _lattice(float(cube_lat["extent"][0]), float(cube_lat["extent"][1]), float(steps[0]))
      lon_all = _lattice(float(cube_lon["extent"][0]), float(cube_lon["extent"][1]), float(steps[1]))
      lat = _cover(lat_all, bbox[1], bbox[3])
      lat = lat[::-1] if ds.lat_descending else lat
      lon = _cover(lon_all, lo, hi)
    else:
      f = self._fetcher(AXIS_TIMEOUT_S)
      lon = np.array(fetch_axis(ds.base_url, ds.dataset_id, "longitude", lo, hi, False, fetch=f))
      lat = np.array(fetch_axis(ds.base_url, ds.dataset_id, "latitude", bbox[1], bbox[3], ds.lat_descending, fetch=f))
    def step_of(v: np.ndarray, published) -> float | None:
      return float(published) if published else (float(np.abs(np.diff(v)).mean()) if len(v) > 1 else None)
    return Axes(lat=lat, lon=lon, step=(step_of(lat, steps[0]), step_of(lon, steps[1])), lat_descending=ds.lat_descending,
                lon_range=tuple(ds.lon_range), depth=depth if depth is not None else ds.depth,
                source="cube" if use_cube else "live")

  # slab and slice ----
  def _parquet_format(self) -> Format:
    fmt = pick_format({"erddap": {"cors": self.dataset.cors, "formats": self.dataset.formats}}, browser=False)
    if fmt not in ("parquet", "parquetWMeta"):
      raise ValueError(f"{self.dataset.dataset_id} on {self.dataset.base_url} offers no Parquet "
                       f"(formats {self.dataset.formats}); the reader reads Parquet only")
    return fmt

  def slab_urls(self, bbox: BBox, t0: str, t1: str, var: str) -> list[str]:
    """The chunked griddap URLs of a slab: at most 90 days or 100,000 values each, in time order. The window is clamped
    to the dataset's extent; a window wholly outside it raises. `t0`/`t1` are days (yyyy-mm-dd) or ISO instants."""
    ds = self.dataset
    if ds.protocol != "griddap":
      raise NotImplementedError(f"{ds.dataset_id} is a tabledap dataset: slab() reads griddap grids "
                                "(tabledap point reads arrive with the point mask)")
    names = [v.name for v in ds.variables]
    if var not in names:
      raise ValueError(f"{var!r} is not a variable of {ds.dataset_id}; it has {names}")
    ext = self.extent()
    time_ext = TimeExtent(start=ext.time_start, end=ext.time_end, step_days=ext.step_days) if ext.time_start else None
    s, e = day(t0), day(t1)
    if days_between(s, e) < 0:
      raise ValueError(f"the window starts after it ends: {s} to {e}")
    if time_ext and time_ext.end and (days_between(s, time_ext.end) < 0 or days_between(time_ext.start, e) < 0):
      raise ValueError(f"the window {s} to {e} is outside {ds.dataset_id}'s time extent "
                       f"({day(time_ext.start)} to {day(time_ext.end)})")
    s, e, _, _ = clamp_window(s, e, time_ext)
    ax = self.axes(bbox)
    step = ext.step_days if ext.step_days is not None else 1
    days = chunk_days_for(ax.n_cells, step)
    fmt = self._parquet_format()
    lat = [float(ax.lat.min()), float(ax.lat.max())]
    lon = [float(ax.lon[0]), float(ax.lon[-1])]
    return [griddap_url(base=ds.base_url, dataset_id=ds.dataset_id, variable=var,
                        time=[time_instant(a, time_ext), time_instant(b, time_ext)], lat=lat, lon=lon,
                        lat_descending=ds.lat_descending, depth=ds.depth, format=fmt)
            for a, b in chunks(s, e, days)]

  def slab(self, bbox: BBox, t0: str, t1: str, var: str) -> pa.Table:
    """The long table `time, lat, lon, value` for `var` over `bbox` and `[t0, t1]`: one Parquet request per chunk,
    read with pyarrow, concatenated in time order and renamed from ERDDAP's `time, latitude, longitude, <var>`."""
    fetch = self._fetcher(SLAB_TIMEOUT_S)
    parts = [read_parquet_slab(fetch(u), var, u) for u in self.slab_urls(bbox, t0, t1, var)]
    return pa.concat_tables(parts)

  def slice(self, bbox: BBox, t: str, var: str) -> pa.Table:
    """One time step as `lat, lon, value`: a slab of `[t, t]` without its time column."""
    tab = self.slab(bbox, t, t, var)
    return pa.table({"lat": tab["lat"], "lon": tab["lon"], "value": tab["value"]}, schema=SLICE_SCHEMA)

  def source_block(self) -> dict:
    """The `erddap:*` and `erddap-places:*` keys of the collection: server, dataset, protocol, version, formats, axis
    conventions and the depth level, which together say what was asked of whom."""
    return {k: v for k, v in self.collection.items() if k.startswith(("erddap:", "erddap-places:"))}


def read_parquet_slab(data: bytes, var: str, url: str = "") -> pa.Table:
  """ERDDAP's griddap Parquet (`time, latitude, longitude, <var>`, plus `depth` on a 4-D grid) to the long table
  `time, lat, lon, value` (SLAB_SCHEMA), rows in file order, NaN kept."""
  t = pq.read_table(pa.BufferReader(data))
  need = ["time", "latitude", "longitude", var]
  missing = [c for c in need if c not in t.column_names]
  if missing:
    raise ValueError(f"the Parquet from {url or 'ERDDAP'} lacks column(s) {missing}; it has {t.column_names}")
  time = t["time"]
  if pa.types.is_timestamp(time.type):
    time = time.cast(TIME_TYPE)
  return pa.table({"time": time, "lat": t["latitude"].cast(pa.float64()), "lon": t["longitude"].cast(pa.float64()),
                   "value": t[var].cast(pa.float64())}, schema=SLAB_SCHEMA)
