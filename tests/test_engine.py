"""om_extract.engine.extract(): one place x variable x window, end to end, on a synthetic 1 deg grid. No network.

The expected statistics are computed by hand from the slab values and the TypeScript's mask weights
(`fixtures_engine`): counts, unweighted means, minima and maxima are exact; the weighted mean, the SD, the weight sum
and the interpolated quantiles are compared within 1e-12 (DuckDB's summation order is its own).
"""
import datetime as dt
import math
import statistics

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from om_extract.engine import (CATEGORICAL_COLUMNS, STATS_COLUMNS, default_coll, extract, stats_provenance,
                               time_window, url_template, value_expr, value_unit)
from fixtures_engine import (DAYS, INFO_URL, SQUARE_AM_URLS, SQUARE_URL, am_value, collection, fetch_from, served,
                             slab_bytes, square_class, square_value, ts_cells, url, wkb)

T0, T1 = "2026-01-01", "2026-01-03"
REL = 1e-12


def run(var="sst", value=square_value, place="square", **kw):
  log: list[str] = []
  pid = "TEST:square" if place == "square" else "TEST:square_am"
  x = extract(kw.pop("doc", None) or collection(), pid, wkb(place), var, T0, T1,
              fetch=fetch_from(served(var, value, place), log), **kw)
  return x, log


def expected_daily(cells_w: list[tuple[float, float, float]], value) -> list[dict]:
  """the stats_daily rows by hand: per day the cells with data, their weights, and the statistics of the rule."""
  out = []
  for i, t in enumerate(DAYS):
    vw = [(value(i, la, lo), w) for la, lo, w in cells_w if value(i, la, lo) is not None]
    vs = sorted(v for v, _ in vw)
    def q(p):
      pos = (len(vs) - 1) * p
      lo = math.floor(pos)
      return vs[lo] + (vs[min(lo + 1, len(vs) - 1)] - vs[lo]) * (pos - lo)
    out.append({"date": dt.date.fromisoformat(t[:10]), "n": len(vs), "mean": sum(vs) / len(vs),
                "mean_wt": math.fsum(v * w for v, w in vw) / math.fsum(w for _, w in vw),
                "sd": statistics.stdev(vs), "min": min(vs), "max": max(vs), "p10": q(0.1), "p90": q(0.9),
                "weight_sum": math.fsum(w for _, w in vw)})
  return out


def assert_rows(got: list[dict], want: list[dict]):
  assert len(got) == len(want)
  for g, w in zip(got, want):
    for k in ("date", "n", "mean", "min", "max"):
      assert g[k] == w[k], (k, g, w)
    for k in ("mean_wt", "sd", "weight_sum", "p10", "p90"):
      assert g[k] == pytest.approx(w[k], rel=REL, abs=1e-12), (k, g, w)


# -- the square: one lobe ------------------------------------------------------------------------------------------------
def test_a_square_crossing_no_seam_is_one_lobe_of_six_cells_with_the_typescript_weights():
  x, _ = run()
  assert (x.lobes, x.mask_cells) == (1, 6)
  assert [(r["latitude"], r["longitude"], r["weight"]) for r in x.mask.to_pylist()] == ts_cells("square_deg1")[0]


def test_the_square_asks_one_request_with_the_exact_griddap_url_after_the_info_table():
  x, log = run()
  want = ("https://erddap.example.org/erddap/griddap/sst_deg1.parquet?sst"
          "%5B(2026-01-01T12:00:00Z):1:(2026-01-03T12:00:00Z)%5D%5B(21.5):1:(20.5)%5D%5B(10.5):1:(12.5)%5D")
  assert x.urls == [want] == [SQUARE_URL]
  assert log == [INFO_URL, want]


def test_the_square_stats_rows_equal_the_hand_computation_one_per_day():
  x, _ = run()
  cells = ts_cells("square_deg1")[0]
  assert_rows(x.stats.to_pylist(), expected_daily(cells, square_value))
  rows = x.stats.to_pylist()
  # day 2 has one missing cell: n counts the cells with data; day 3 is constant: sd 0, every statistic 8
  assert [r["n"] for r in rows] == [6, 5, 6]
  assert (rows[0]["mean"], rows[0]["p10"], rows[0]["p90"]) == (3.5, 1.5, 5.5)
  assert (rows[2]["mean_wt"], rows[2]["sd"], rows[2]["min"], rows[2]["max"]) == (8.0, 0.0, 8.0, 8.0)


def test_the_stats_table_has_erddap_places_columns_and_types_with_the_identity_columns_filled():
  x, _ = run()
  assert tuple(x.stats.column_names) == STATS_COLUMNS
  assert STATS_COLUMNS == ("date", "n", "mean", "mean_wt", "sd", "min", "max", "p10", "p90", "weight_sum",
                           "place_id", "dataset_id", "variable")
  types = {f.name: f.type for f in x.stats.schema}
  assert types["date"] == pa.date32() and types["n"] == pa.int64()
  assert all(types[c] == pa.float64() for c in ("mean", "mean_wt", "sd", "min", "max", "p10", "p90", "weight_sum"))
  assert all(types[c] == pa.string() for c in ("place_id", "dataset_id", "variable"))
  assert {(r["place_id"], r["dataset_id"], r["variable"]) for r in x.stats.to_pylist()} == {
    ("TEST:square", "sst_deg1", "sst")}


# -- the square cut at +-180: two lobes -----------------------------------------------------------------------------------
def test_a_square_crossing_180_is_two_lobes_of_four_cells_west_first_with_the_typescript_weights():
  x, _ = run(value=am_value, place="square_am")
  assert (x.lobes, x.mask_cells) == (2, 8)
  w, e = ts_cells("square_am_deg1")
  assert [(r["latitude"], r["longitude"], r["weight"]) for r in x.mask.to_pylist()] == w + e
  assert [a["bbox"] for a in x.axes] == [[-180.0, 20.2, -178.4, 21.6], [178.3, 20.2, 180.0, 21.6]]


def test_the_cut_square_asks_one_request_per_lobe_west_then_east():
  x, log = run(value=am_value, place="square_am")
  assert x.urls == SQUARE_AM_URLS
  assert x.urls[0].endswith("%5B(21.5):1:(20.5)%5D%5B(-179.5):1:(-178.5)%5D")
  assert x.urls[1].endswith("%5B(21.5):1:(20.5)%5D%5B(178.5):1:(179.5)%5D")
  assert log == [INFO_URL, *SQUARE_AM_URLS]


def test_the_cut_square_stats_pool_both_lobes_into_one_row_a_day():
  x, _ = run(value=am_value, place="square_am")
  w, e = ts_cells("square_am_deg1")
  assert_rows(x.stats.to_pylist(), expected_daily(w + e, am_value))
  assert [r["mean"] for r in x.stats.to_pylist()] == [4.5, 14.5, 24.5]


# -- rules ---------------------------------------------------------------------------------------------------------------
def test_categorical_variables_run_stats_categorical_with_the_published_column_names():
  x, _ = run("cls", square_class)
  assert x.categorical and tuple(x.stats.column_names) == CATEGORICAL_COLUMNS
  cells = ts_cells("square_deg1")[0]
  total = math.fsum(w for _, _, w in cells)
  north = math.fsum(w for la, _, w in cells if la == 21.5)
  rows = [(r["date"].day, r["class"], r["n"], r["frac_area"], r["pct_cells"]) for r in x.stats.to_pylist()]
  assert [(d, c, n) for d, c, n, _, _ in rows] == [(1, 0, 3), (1, 1, 3), (2, 1, 6), (3, 0, 5), (3, 1, 1)]
  assert rows[1][3] == pytest.approx(north / total, rel=REL) and rows[2][3] == 1.0
  assert rows[4][3] == pytest.approx(0.7994803933429656 / total, rel=REL) and rows[0][4] == 50.0


def test_the_categorical_flag_can_be_overridden():
  x, _ = run("cls", square_class, categorical=False)
  assert not x.categorical and tuple(x.stats.column_names) == STATS_COLUMNS
  assert x.stats.to_pylist()[1]["mean"] == 1.0


def test_a_kelvin_variable_is_converted_to_celsius_by_the_value_expression():
  assert value_expr("sst_k", "kelvin") == '(s."sst_k" - 273.15)'
  assert value_expr("sst", "degree_C") == 's."sst"'
  assert [value_unit(u) for u in ("K", "degree_Kelvin", "degreeK", "degree_C", None)] == [
    "degree_C", "degree_C", "degree_C", "degree_C", None]
  x, _ = run("sst_k", lambda i, la, lo: 300.0)
  assert '(s."sst_k" - 273.15)' in x.sql
  assert x.stats.to_pylist()[0]["mean"] == pytest.approx(300.0 - 273.15, rel=REL)


def test_duplicate_slab_rows_count_once():
  # a coarse axis can snap two chunks onto one step: the slab is made DISTINCT before the statistics
  pages = served()
  t = pq.read_table(pa.BufferReader(pages[SQUARE_URL]))
  sink = pa.BufferOutputStream()
  pq.write_table(pa.concat_tables([t, t]), sink)
  pages[SQUARE_URL] = sink.getvalue().to_pybytes()
  x = extract(collection(), "TEST:square", wkb("square"), "sst", T0, T1, fetch=fetch_from(pages))
  assert [r["n"] for r in x.stats.to_pylist()] == [6, 5, 6]


def test_a_0_360_grid_masks_in_minus_180_180_and_joins_the_cells_in_the_dataset_frame():
  doc = collection(erddap__lon_range=[0, 360])
  doc["cube:dimensions"]["longitude"]["extent"] = [0.5, 359.5]
  sq = {"type": "Polygon", "coordinates": [[(-150.7, 20.2), (-148.4, 20.2), (-148.4, 21.7), (-150.7, 21.7),
                                            (-150.7, 20.2)]]}
  cells = [(la, lo) for la in (20.5, 21.5) for lo in (209.5, 210.5, 211.5)]
  u = url("sst", (20.5, 21.5), (209.5, 211.5))
  pages = {u: slab_bytes("sst", [21.5, 20.5], [209.5, 210.5, 211.5], lambda i, la, lo: float(cells.index((la, lo))))}
  pages[INFO_URL] = served()[INFO_URL]
  x = extract(doc, "TEST:pacific", sq, "sst", T0, T1, fetch=fetch_from(pages))
  assert x.urls == [u]
  assert sorted(x.mask["longitude"].to_pylist()) == [209.5, 209.5, 210.5, 210.5, 211.5, 211.5]
  assert [r["n"] for r in x.stats.to_pylist()] == [6, 6, 6]


def test_the_extraction_records_the_window_the_sql_and_the_source_block():
  x, _ = run()
  assert (x.t0, x.t1, x.start_date, x.end_date) == (T0, T1, T0, T1)
  assert (x.start_datetime, x.end_datetime, x.record_start) == (DAYS[0], DAYS[-1], "2000-01-01")
  assert x.live_extent and x.step_days == 1
  assert x.sql.startswith("-- daily key statistics") and 'sum(s."sst" * m.weight) / sum(m.weight)' in x.sql
  assert x.source["erddap:dataset_id"] == "sst_deg1" and x.dataset.dataset_id == "sst_deg1"


def test_a_variable_the_collection_lacks_and_a_place_without_polygons_raise():
  with pytest.raises(ValueError, match="'nope' is not a variable"):
    extract(collection(), "TEST:square", wkb("square"), "nope", T0, T1, fetch=fetch_from({}))
  with pytest.raises(ValueError, match="no polygon lobes"):
    extract(collection(), "TEST:x", {"type": "Point", "coordinates": [0, 0]}, "sst", T0, T1, fetch=fetch_from({}))


def test_default_coll_is_places_for_nms_and_the_lowercased_authority_otherwise():
  assert [default_coll(p) for p in ("NMS:CINMS", "MRGID:8371", "TEST:square")] == ["places", "mrgid", "test"]


def test_stats_provenance_has_erddap_places_keys():
  x, _ = run(value=am_value, place="square_am")
  p = stats_provenance(x, bytes_=123, generated="2026-10-22T00:00:00Z")
  assert list(p) == ["place_id", "dataset_id", "variable", "categorical", "start_date", "end_date", "start_datetime",
                     "end_datetime", "record_start", "lobes", "mask_cells", "axes", "griddap_urls",
                     "griddap_url_templates", "griddap_chunks", "griddap_requests", "rows", "bytes", "generated",
                     "erddap_base", "erddap_version", "sql"]
  assert (p["lobes"], p["mask_cells"], p["griddap_requests"], p["rows"], p["bytes"]) == (2, 8, 2, 3, 123)
  assert p["griddap_chunks"] == [[DAYS[0], DAYS[-1]]]
  assert p["griddap_url_templates"][0].endswith("?sst%5B({t0}):1:({t1})%5D%5B(21.5):1:(20.5)%5D%5B(-179.5):1:(-178.5)%5D")


def test_time_window_and_url_template_read_the_time_constraint():
  assert time_window(SQUARE_URL) == (DAYS[0], DAYS[-1])
  assert url_template(SQUARE_URL) == SQUARE_URL.replace(f"({DAYS[0]}):1:({DAYS[-1]})", "({t0}):1:({t1})")
  assert time_window("https://x.org/no-constraint") is None
