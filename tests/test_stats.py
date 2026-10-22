"""om_extract.stats: erddap-places' SQL templates over a slab and a mask, in DuckDB (python) and the DuckDB CLI.

The slab is a 3 x 3 grid over two days with float32 axes (as ERDDAP's parquet has them), the mask three cells
on its diagonal with weights 1, 0.5, 0.5, plus six slab cells outside the mask that must reach no statistic.
Values are chosen so every expected number is exact on every platform: dyadic sums, and quantiles whose
interpolation has a zero end (DuckDB interpolates lo * (1 - d) + hi * d, fused on arm64, so a zero end keeps
the result one rounding either way).

  day 1  values -2, 0, 2   weights 1, .5, .5   -> n 3, mean 0, mean_wt (-2 + 0 + 1) / 2 = -0.5, sd 2
  day 2  NULL, 0, 2        weights (1), .5, .5 -> n 2, mean 1, mean_wt (0 + 1) / 1 = 1, sd sqrt(2)
"""
import datetime as dt
import json
import math
import os
import shutil
import subprocess
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from om_extract.mask import MaskTable
from om_extract.stats import lit, render, stats, template, template_names

SQL_DIR = Path(__file__).resolve().parents[1] / "om_extract" / "sql"
SIBLING = Path(os.environ.get("OM_ERDDAP_PLACES_DIR", Path(__file__).resolve().parents[2] / "erddap-places"))
COPIED = ("stats_daily.sql", "stats_categorical.sql", "last_step.sql")

LATS = [20.0, 20.05, 20.1]
LONS = [-160.0, -159.95, -159.9]
DAYS = [dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone.utc), dt.datetime(2026, 1, 2, 12, tzinfo=dt.timezone.utc)]
VALUES = [{(0, 0): -2.0, (1, 1): 0.0, (2, 2): 2.0}, {(0, 0): None, (1, 1): 0.0, (2, 2): 2.0}]
CLASSES = [{(0, 0): 1, (1, 1): 1, (2, 2): 2}, {(0, 0): None, (1, 1): 3, (2, 2): 3}]


def slab(var: str = "SST", values=VALUES) -> pa.Table:
  t, la, lo, v = [], [], [], []
  for d, vals in zip(DAYS, values):
    for i, lat in enumerate(LATS):
      for j, lon in enumerate(LONS):
        t.append(d)
        la.append(lat)
        lo.append(lon)
        v.append(vals[(i, j)] if (i, j) in vals else 99.0)
  vtype = pa.float64() if var != "CLASS" else pa.float32()
  return pa.table({"time": pa.array(t, pa.timestamp("us", tz="UTC")), "latitude": pa.array(la, pa.float32()),
                   "longitude": pa.array(lo, pa.float32()), var: pa.array(v, vtype)})


def mask() -> MaskTable:
  import numpy as np
  return MaskTable(lon=np.array(LONS), lat=np.array(LATS), weight=np.array([1.0, 0.5, 0.5]), n_inside=3)


# ── the templates ─────────────────────────────────────────────────────────────
def test_templates_shipped():
  assert set(template_names()) >= {"stats_daily", "stats_categorical", "last_step"}


@pytest.mark.skipif(not (SIBLING / "sql").is_dir(), reason=f"no erddap-places checkout at {SIBLING}")
@pytest.mark.parametrize("name", COPIED)
def test_templates_are_byte_identical_to_erddap_places(name: str):
  assert (SQL_DIR / name).read_bytes() == (SIBLING / "sql" / name).read_bytes()


def test_render_splices_raw_params_and_quotes_the_rest():
  assert lit("O'ahu") == "'O''ahu'"
  assert [lit(None), lit(True), lit(False), lit(5), lit(5.0), lit(0.1), lit(1e-7), lit(1e21), lit(float("nan"))] == \
         ["NULL", "TRUE", "FALSE", "5", "5", "0.1", "1e-7", "1e+21", "NULL"]
  sql = render("stats_categorical", {"expr": 's."CLASS"', "slab": "slab", "mask": "mask"})
  assert "FROM slab s" in sql and "CAST(s.\"CLASS\" AS BIGINT)" in sql
  assert sql == sql.strip() and "{{" not in sql
  assert render("x", {"a": "it's", "expr": "s.v"}, text="{{expr}} = {{a}}") == "s.v = 'it''s'"


def test_render_missing_param_raises():
  with pytest.raises(KeyError, match="missing param mask"):
    render("stats_daily", {"expr": "s.v", "slab": "slab"})
  with pytest.raises(KeyError, match="no SQL template"):
    template("nope")


# ── stats_daily ───────────────────────────────────────────────────────────────
DAILY = [
  {"date": dt.date(2026, 1, 1), "n": 3, "mean": 0.0, "mean_wt": -0.5, "sd": 2.0, "min": -2.0, "max": 2.0,
   "p10": -1.6, "p90": 1.6, "weight_sum": 2.0},
  {"date": dt.date(2026, 1, 2), "n": 2, "mean": 1.0, "mean_wt": 1.0, "sd": math.sqrt(2), "min": 0.0, "max": 2.0,
   "p10": 0.2, "p90": 1.8, "weight_sum": 1.0},
]


def test_stats_daily_exact():
  t = stats(slab(), mask())
  assert t.column_names == ["date", "n", "mean", "mean_wt", "sd", "min", "max", "p10", "p90", "weight_sum"]
  assert t.to_pylist() == DAILY


def test_stats_daily_expr_converts_units():
  # the value expression is spliced as is: Kelvin to Celsius shifts every statistic but n, sd and weight_sum
  t = stats(slab("analysed_sst"), mask(), expr='(s."analysed_sst" - 273.15)').to_pylist()
  assert [r["n"] for r in t] == [3, 2] and t[0]["mean"] == 0.0 - 273.15 and t[0]["weight_sum"] == 2.0


def _duckdb_cli(tmp_path: Path, slab_t: pa.Table, name: str) -> list[dict]:
  pq.write_table(slab_t, tmp_path / "slab.parquet")
  pq.write_table(mask().to_arrow(), tmp_path / "mask.parquet")
  var = [c for c in slab_t.column_names if c not in ("time", "latitude", "longitude")][0]
  sql = render(name, {"expr": f's."{var}"', "slab": f"read_parquet('{tmp_path / 'slab.parquet'}')",
                      "mask": f"read_parquet('{tmp_path / 'mask.parquet'}')"})
  out = subprocess.run(["duckdb", "-json", "-c", sql], capture_output=True, text=True, check=True).stdout
  return json.loads(out or "[]")


def _jsonable(rows: list[dict]) -> list[dict]:
  return [{k: (v.isoformat() if isinstance(v, dt.date) else v) for k, v in r.items()} for r in rows]


@pytest.mark.skipif(shutil.which("duckdb") is None, reason="no duckdb CLI on PATH")
def test_stats_daily_same_in_the_duckdb_cli(tmp_path: Path):
  # the same template text in another engine (the native CLI, as erddap-places' sql.test.ts runs it)
  assert _duckdb_cli(tmp_path, slab(), "stats_daily") == _jsonable(DAILY)


# ── stats_categorical ─────────────────────────────────────────────────────────
CATEGORICAL = [
  {"date": dt.date(2026, 1, 1), "class": 1, "n": 2, "weight": 1.5, "fraction": 0.75, "percent_cells": 200 / 3},
  {"date": dt.date(2026, 1, 1), "class": 2, "n": 1, "weight": 0.5, "fraction": 0.25, "percent_cells": 100 / 3},
  {"date": dt.date(2026, 1, 2), "class": 3, "n": 2, "weight": 1.0, "fraction": 1.0, "percent_cells": 100.0},
]


def test_stats_categorical_fractions_by_area_percent_by_cells():
  t = stats(slab("CLASS", CLASSES), mask(), categorical=True)
  assert t.column_names == ["date", "class", "n", "weight", "fraction", "percent_cells"]
  assert t.to_pylist() == CATEGORICAL


@pytest.mark.skipif(shutil.which("duckdb") is None, reason="no duckdb CLI on PATH")
def test_stats_categorical_same_in_the_duckdb_cli(tmp_path: Path):
  assert _duckdb_cli(tmp_path, slab("CLASS", CLASSES), "stats_categorical") == _jsonable(CATEGORICAL)


# ── last_step ─────────────────────────────────────────────────────────────────
def test_last_step_keeps_the_masked_cells_of_the_last_day_with_nulls():
  t = stats(slab(), mask(), "last_step")
  assert t.column_names == ["date", "latitude", "longitude", "weight", "value"]
  assert [(r["date"], r["weight"], r["value"]) for r in t.to_pylist()] == [
    (dt.date(2026, 1, 2), 1.0, None), (dt.date(2026, 1, 2), 0.5, 0.0), (dt.date(2026, 1, 2), 0.5, 2.0)]
  assert t.column("latitude").to_pylist() == LATS  # the mask's own float64 positions, not the slab's float32


def test_stats_needs_one_variable_or_an_expr():
  two = slab().append_column("other", pa.array([0.0] * 18))
  with pytest.raises(ValueError, match="pass expr="):
    stats(two, mask())
  assert stats(two, mask(), expr='s."SST"').to_pylist() == DAILY
