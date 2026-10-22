"""An `Extraction` as values-contract rows and their provenance (`om_extract.contract`, contract 1.2.0).

The rows are at the grid's native grain, one per time step of the stats table (and class): no aggregation over time
happens here yet (month and year rows from daily ones arrive in build week 1). The rules, each a test:

- **grain** (`native_grain()`): `day` for a daily step (`P1D`), `month` for `P1M`, `year` for `P1Y`; otherwise from the
  step in days (the extent's `step_days`, else the `P<n>D|M|Y` step of the catalog): under 28 days is `day` (an 8-day
  composite is one row a step, at the step's date), 28 to 359 is `month`, 360 or more `year`; unknown is `day`. `time`
  is the first day of the grain period holding the stats `date`.
- **indicator**: `erddap:<dataset_id>:<variable>:<label>`, where `mean_wt` is labelled `mean` (the id the catalog
  publishes) and the unweighted `mean` is `mean_unw`; a categorical variable's is `erddap:<dataset_id>:<variable>:share`.
- **value**: the `stat` column of the stats table (`mean_wt` by default; `mean`, `sd`, `min`, `max`, `p10`, `p90`);
  for a categorical variable `frac_area`, the area-weighted share of the place in the class. NaN or infinite is null.
- **n**: the stats `n`, the cells with data at that step (for a class: its cells).
- **sd**: the stats `sd` (the SD across the cells at that step) for `mean_wt` and `mean`; null for any other stat, for
  a categorical variable, and wherever `value` is null.
- **unit**: the collection's unit of the variable (`degree_C` for a Kelvin unit, which the SQL converts); `1` for a
  categorical variable.
- **coll**: as given to `extract()`, else by the place id's authority (`NMS:` is `places`, else the authority
  lowercased).
- **class**: null for continuous; the class code as a string for categorical, one row per class.
"""
from __future__ import annotations

import math
import re
from typing import Any

from om_extract.contract import GRAINS, period_start, utc_now
from om_extract.engine import Extraction, default_coll, value_unit

CONTINUOUS_STATS = ("mean_wt", "mean", "sd", "min", "max", "p10", "p90")
SD_STATS = ("mean_wt", "mean")
CATEGORICAL_STATS = ("mean_wt", "share")   # the share of a class is the area-weighted mean of membership
PRODUCER_URL = "https://github.com/oceanmetrics/extract"
ADAPTER = "om_extract.engine"
METHOD = ("per time step, the area-weighted mean of the cells whose centres fall inside the place's lobes, each cell "
          "weighted by the share of its area inside the place (partial-area weights), with the sample SD across those "
          "cells; a categorical variable gives the area-weighted share of each class.")
COARSER = "aggregation to a coarser grain is build week 1"
_ISO_STEP = re.compile(r"^P(\d+(?:\.\d+)?)([DMY])$")


def step_days_of(time_step: str | None) -> float | None:
  """An ISO 8601 step of the catalog (`P1D`, `P8D`, `P1M`, `P1Y`) in days; None for anything else."""
  m = _ISO_STEP.match(time_step or "")
  if not m:
    return None
  return float(m.group(1)) * {"D": 1, "M": 30.4375, "Y": 365.25}[m.group(2)]


def native_grain(time_step: str | None, step_days: float | None = None) -> str:
  """The contract grain of one time step of a grid (see the module docstring)."""
  exact = {"P1D": "day", "P1M": "month", "P1Y": "year"}
  if time_step in exact:
    return exact[time_step]
  d = step_days if step_days is not None else step_days_of(time_step)
  if d is None or d < 28:
    return "day"
  return "month" if d < 360 else "year"


# the published id of the area-weighted mean is `...:mean` (oceanmetrics/indicators' erddap adapter names the
# `mean_wt` column that way), so the engine's ids match the catalog's; the unweighted mean is published as `mean_unw`
STAT_LABEL = {"mean_wt": "mean", "mean": "mean_unw"}


def indicator_id(dataset_id: str, variable: str, stat: str, categorical: bool = False) -> str:
  """`erddap:<dataset_id>:<variable>:<label>` with `mean_wt` labelled `mean` (the published id) and `mean` as
  `mean_unw`; `...:share` for a categorical variable."""
  label = "share" if categorical else STAT_LABEL.get(stat, stat)
  return f"erddap:{dataset_id}:{variable}:{label}"


def _finite(v: Any) -> float | None:
  if v is None:
    return None
  v = float(v)
  return v if math.isfinite(v) else None


def values_rows(extraction: Extraction, *, stat: str = "mean_wt", grain: str | None = None) -> list[dict]:
  """The contract rows of an extraction at its native grain (rules in the module docstring). `grain` may only name the
  native grain today: any other raises ValueError("aggregation to a coarser grain is build week 1"). An H3 extraction
  (`engine_h3.extract_h3()`) gives `engine_h3.h3_values_rows()`."""
  if getattr(extraction, "kind", None) == "h3":
    from om_extract.engine_h3 import h3_values_rows
    return h3_values_rows(extraction, stat=stat, grain=grain)
  x = extraction
  if x.dataset.protocol == "cog":
    raise NotImplementedError("values rows of a COG extraction wait on the atlas indicators' ids and grains (vision "
                              "plan 'From SDMs to indicators', step 2); use extraction.stats")
  native = native_grain(x.dataset.time_step, x.step_days)
  if grain is not None and grain != native:
    if grain not in GRAINS:
      raise ValueError(f"unknown grain {grain!r}; the contract has {list(GRAINS)}")
    raise ValueError(f"{COARSER} (native grain {native!r}, asked {grain!r})")
  if x.categorical and stat not in CATEGORICAL_STATS:
    raise ValueError(f"a categorical variable has one stat, the area share of each class ('share'), not {stat!r}")
  if not x.categorical and stat not in CONTINUOUS_STATS:
    raise ValueError(f"stat {stat!r} is not one of {list(CONTINUOUS_STATS)}")
  coll = x.coll or default_coll(x.place_id)
  ind = indicator_id(x.dataset.dataset_id, x.variable, stat, x.categorical)
  if x.categorical:
    unit = "1"
  else:
    unit = value_unit(((x.collection.get("cube:variables") or {}).get(x.variable) or {}).get("unit"))
    if unit is None:
      raise ValueError(f"variable {x.variable!r} of {x.dataset.id} has no unit in the collection")
  out = []
  for r in x.stats.to_pylist():
    base = {"place_id": x.place_id, "coll": coll, "indicator": ind, "time_grain": native,
            "time": period_start(native, r["date"]), "unit": unit, "n": float(r["n"])}
    if x.categorical:
      v = _finite(r["frac_area"])
      out.append({**base, "class": str(int(r["class"])), "value": v, "sd": None})
    else:
      v = _finite(r[stat])
      sd = _finite(r["sd"]) if stat in SD_STATS and v is not None else None
      out.append({**base, "class": None, "value": v, "sd": sd})
  return out


def _doi(c: dict) -> str | None:
  links = c.get("links") or []
  doi = next((lk["href"] for lk in links if "doi.org/" in (lk.get("href") or "")), None)
  if doi is None and c.get("sci:doi"):
    doi = f"https://doi.org/{c['sci:doi']}"
  return doi


def provenance_dataset(c: dict) -> dict:
  """The original dataset as `provenance.dataset` (contract 1.2), as oceanmetrics/indicators' manifest states it: `id`
  the ERDDAP dataset id, `title`, `providers` (name, roles, url), `license` (`other` when absent), `via` (the link
  `rel: via`, else `about`, else `license`), `citation` (`sci:citation`) and `doi` (a doi.org link, else `sci:doi`)."""
  links = c.get("links") or []
  def rel(name: str) -> str | None:
    return next((lk["href"] for lk in links if lk.get("rel") == name and lk.get("href")), None)
  return {
    "id": c["erddap:dataset_id"], "title": c.get("title") or c["id"],
    "via": rel("via") or rel("about") or rel("license"),
    "providers": [{k: p[k] for k in ("name", "roles", "url") if k in p}
                  for p in c.get("providers") or [] if p.get("name")],
    "license": c.get("license") or "other", "citation": c.get("sci:citation"), "doi": _doi(c),
  }


def collection_href(x: Extraction) -> str:
  """Where the collection was read: the URL given, else its `rel: self` link, else the path given, else its id."""
  self_ = next((lk["href"] for lk in x.collection.get("links") or [] if lk.get("rel") == "self"), None)
  given = x.collection_href
  if given and given.startswith(("http://", "https://")):
    return given
  return self_ or given or x.collection["id"]


def source_version(c: dict) -> str | None:
  """One line naming the upstream snapshot: the ERDDAP version and the collection's `updated`."""
  parts = []
  if c.get("erddap:version"):
    parts.append(f"ERDDAP {c['erddap:version']}")
  if c.get("updated"):
    parts.append(f"{c['id']} updated {c['updated']}")
  return ", ".join(parts) or None


def provenance_for(extraction: Extraction, code_version: str) -> dict:
  """The values file's provenance: producer, dataset, sources (one `data` entry per request URL and the collection
  JSON as `product`), method, code, licence, created and source_version. `write_values()` adds indicator, coll,
  contract and rows."""
  x = extraction
  c = x.collection
  return {
    "producer": {"name": "oceanmetrics/extract", "url": PRODUCER_URL, "version": code_version},
    "dataset": provenance_dataset(c),
    "sources": [*({"href": u, "item": None, "role": "data"} for u in x.urls),
                {"href": collection_href(x), "item": None, "role": "product"}],
    "source_version": source_version(c),
    "method": METHOD,
    "code": {"name": "om-extract", "version": code_version, "adapter": ADAPTER},
    "license": c.get("license") or "other",
    "created": utc_now(),
  }
