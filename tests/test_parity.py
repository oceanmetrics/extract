"""om_extract.parity: the comparisons on synthetic tables, and the harness end to end on a local gazetteer copy and a
synthetic ERDDAP (no network)."""
import datetime as dt
import json
import math

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fixtures_chunks import FakeErddap
from fixtures_engine import collection, wkb
from om_extract.engine import extract, provenance_text, stats_provenance
from om_extract.cli import main
from om_extract.parity import (ITEM_KEYS, SUM_REL, column_verdict, compare_bytes, compare_chunks, compare_files,
                               compare_item, compare_provenance, compare_rows, reference_key_dir, rows_verdict,
                               run_batch, run_parity, run_with_tries, seed_chunks, summary_markdown, ulps, verdict,
                               verdict_table)

D = [dt.date(2026, 1, d) for d in (1, 2, 3)]


def table(dates=D, mean=(1.0, 2.0, 3.0), n=(6, 6, 6), place="TEST:x"):
  return pa.table({"date": pa.array(dates, pa.date32()), "n": pa.array(n, pa.int64()),
                   "mean": pa.array(mean, pa.float64()), "place_id": pa.array([place] * len(dates), pa.string())})


# -- rows -----------------------------------------------------------------------------------------------------------
def test_equal_tables_are_exact():
  c = compare_rows(table(), table())
  assert c["exact"] and rows_verdict(c) == "exact" and c["paired"] == 3
  assert all(v["exact"] for v in c["columns"].values())


def test_one_value_off_by_one_ulp_is_a_float_sum_difference():
  off = math.nextafter(2.0, 3.0)
  c = compare_rows(table(mean=(1.0, off, 3.0)), table())
  m = c["columns"]["mean"]
  assert not c["exact"] and m["n_diff"] == 1 and m["max_ulps"] == 1 and m["max_abs"] == off - 2.0
  assert m["first_diff"] == {"key": "2026-01-02", "engine": off, "ref": 2.0}
  assert rows_verdict(c) == "within the criterion (sums max rel 2e-16)"
  assert column_verdict(m) == "max 4.4e-16 (1 ulp), 1 rows"


def test_a_large_float_difference_differs():
  c = compare_rows(table(mean=(1.0, 2.5, 3.0)), table())
  assert rows_verdict(c) == "differs" and c["columns"]["mean"]["max_abs"] == 0.5


def test_one_row_missing_is_reported_by_key():
  c = compare_rows(table(dates=D[:2], mean=(1.0, 2.0), n=(6, 6)), table())
  assert c["missing_in_engine"] == 1 and c["missing_examples"] == ["2026-01-03"] and c["paired"] == 2
  assert not c["exact"] and rows_verdict(c) == "differs"


def test_an_extra_row_is_reported():
  c = compare_rows(table(), table(dates=D[:2], mean=(1.0, 2.0), n=(6, 6)))
  assert c["extra_in_engine"] == 1 and c["extra_examples"] == ["2026-01-03"]


def test_an_integer_column_compares_by_equality():
  c = compare_rows(table(n=(6, 5, 6)), table())
  assert c["columns"]["n"] == {"type": "int64", "exact": False, "n_diff": 1,
                               "first_diff": {"key": "2026-01-02", "engine": 5, "ref": 6}}
  assert rows_verdict(c) == "differs"


def test_a_duplicated_key_pairs_in_file_order_and_is_counted():
  ref = table(dates=[D[0], D[1], D[1]], mean=(1.0, 2.0, math.nextafter(2.0, 3.0)), n=(6, 6, 6))
  c = compare_rows(table(dates=D[:2], mean=(1.0, 2.0), n=(6, 6)), ref)
  assert c["duplicate_keys_ref"] == 1 and c["duplicate_examples_ref"] == ["2026-01-02"]
  assert c["missing_in_engine"] == 1 and c["columns"]["mean"]["exact"]


def test_categorical_rows_match_by_date_and_class():
  def cat(cls, w):
    return pa.table({"date": pa.array([D[0], D[0]], pa.date32()), "class": pa.array(cls, pa.int64()),
                     "weight": pa.array(w, pa.float64())})
  c = compare_rows(cat([1, 0], [2.0, 1.0]), cat([0, 1], [1.0, 2.0]))
  assert c["keys"] == ["date", "class"] and c["exact"]


def test_a_type_difference_is_a_schema_difference():
  e = table().set_column(1, "n", pa.array([6, 6, 6], pa.int32()))
  c = compare_rows(e, table())
  assert c["schema_diff"] == ["n: int32 (engine) vs int64 (reference)"] and rows_verdict(c) == "differs"


def test_nan_and_null_equal_themselves():
  c = compare_rows(table(mean=(float("nan"), None, 3.0)), table(mean=(float("nan"), None, 3.0)))
  assert c["exact"]


# -- the criterion (ledger 2026-10-23): order-independent columns ==, sums within 1e-9 relative, one row per key ------
def stats(mean=(10.0, 20.0, 30.0), mx=(11.0, 21.0, 31.0), dates=D, n=(6, 6, 6)):
  return pa.table({"date": pa.array(dates, pa.date32()), "n": pa.array(n, pa.int64()),
                   "mean": pa.array(mean, pa.float64()), "sd": pa.array([0.5] * len(dates), pa.float64()),
                   "max": pa.array(mx, pa.float64()), "place_id": pa.array(["TEST:x"] * len(dates), pa.string())})


def test_the_criterion_passes_the_observed_last_bit_sums():
  # CINMS CRW_SST, 85 recomputed chunks: at most 2.8e-14 relative on the sums
  c = compare_rows(stats(mean=(10.0, 20.0 * (1 + 2.8e-14), 30.0)), stats())
  v = verdict(c)
  assert v["pass"] and v["reasons"] == [] and 2e-14 < v["sums_max_rel"] < 3e-14
  assert rows_verdict(c) == "within the criterion (sums max rel 3e-14)"


def test_the_criterion_passes_at_1e9_and_fails_beyond():
  assert verdict(compare_rows(stats(mean=(10.0, 20.0 * (1 + 0.9 * SUM_REL), 30.0)), stats()))["pass"]
  v = verdict(compare_rows(stats(mean=(10.0, 20.0 * (1 + 2 * SUM_REL), 30.0)), stats()))
  assert not v["pass"] and v["reasons"][0].startswith("mean: max rel 2e-09 > 1e-09")


def test_an_order_independent_float_off_by_one_ulp_fails():
  c = compare_rows(stats(mx=(11.0, math.nextafter(21.0, 22.0), 31.0)), stats())
  v = verdict(c)
  assert not v["pass"] and v["reasons"] == ["max not exact (max 3.6e-15 (1 ulp), 1 rows)"]
  assert rows_verdict(c) == "differs"


def test_an_integer_off_by_one_fails_the_criterion():
  assert verdict(compare_rows(stats(n=(6, 7, 6)), stats()))["reasons"] == ["n not exact (1 rows differ)"]


def test_a_sum_that_is_null_against_a_value_fails():
  v = verdict(compare_rows(stats(mean=(10.0, None, 30.0)), stats()))
  assert not v["pass"] and "mean: a null or NaN" in v["reasons"][0]


def test_a_key_missing_from_the_engine_fails():
  v = verdict(compare_rows(stats(dates=D[:2], mean=(10.0, 20.0), mx=(11.0, 21.0), n=(6, 6)), stats()))
  assert not v["pass"] and v["reasons"] == ["1 key(s) only in the published file (first ['2026-01-03'])"]


def test_a_duplicated_published_date_written_once_passes():
  # the duplicated-date bug: the published file carries 2026-01-02 twice, its sums a few ulp apart; the engine once
  ref = stats(dates=[D[0], D[1], D[1], D[2]], mean=(10.0, 20.0, math.nextafter(20.0, 21.0), 30.0),
              mx=(11.0, 21.0, 21.0, 31.0), n=(6, 6, 6, 6))
  c = compare_rows(stats(), ref)
  v = verdict(c)
  assert v["pass"] and v["duplicate_keys_ref"] == 1 and c["missing_in_engine"] == 1 and c["keys_only_ref"] == 0
  assert rows_verdict(c) == "within the criterion (sums exact; 1 duplicated key(s) published, written once)"


def test_a_duplicated_date_in_the_engine_fails():
  eng = stats(dates=[D[0], D[1], D[1], D[2]], mean=(10.0, 20.0, 20.0, 30.0), mx=(11.0, 21.0, 21.0, 31.0),
              n=(6, 6, 6, 6))
  v = verdict(compare_rows(eng, stats()))
  assert not v["pass"] and v["reasons"] == ["1 key(s) more than once in the engine's file"]


def test_categorical_weights_are_sums_and_pct_cells_is_exact():
  def cat(w, pct):
    return pa.table({"date": pa.array([D[0], D[0]], pa.date32()), "class": pa.array([0, 1], pa.int64()),
                     "weight": pa.array(w, pa.float64()), "frac_area": pa.array([0.25, 0.75], pa.float64()),
                     "pct_cells": pa.array(pct, pa.float64())})
  assert verdict(compare_rows(cat([1.0, 3.0 * (1 + 1e-12)], [25.0, 75.0]), cat([1.0, 3.0], [25.0, 75.0])))["pass"]
  v = verdict(compare_rows(cat([1.0, 3.0], [25.0, math.nextafter(75.0, 76.0)]), cat([1.0, 3.0], [25.0, 75.0])))
  assert not v["pass"] and v["reasons"][0].startswith("pct_cells not exact")


def test_compare_on_files_exits_by_the_criterion(tmp_path, capsys):
  pq.write_table(stats(), tmp_path / "ref.parquet")
  pq.write_table(stats(mean=(10.0, 20.0 * (1 + 1e-13), 30.0)), tmp_path / "ok.parquet")
  pq.write_table(stats(mx=(11.0, 21.5, 31.0)), tmp_path / "bad.parquet")
  res = compare_files(tmp_path / "ok.parquet", tmp_path / "ref.parquet")
  assert res["verdict"]["pass"] and not res["bytes"]["equal"]
  assert compare_files(tmp_path / "ref.parquet", tmp_path / "ref.parquet")["bytes"]["equal"]
  assert main(["compare", str(tmp_path / "ok.parquet"), str(tmp_path / "ref.parquet"),
               "--json", str(tmp_path / "c.json")]) == 0
  assert "verdict: PASS" in capsys.readouterr().out
  assert json.loads((tmp_path / "c.json").read_text())["verdict"]["pass"] is True
  assert main(["compare", str(tmp_path / "bad.parquet"), str(tmp_path / "ref.parquet")]) == 1
  assert "verdict: FAIL -> differs\n  max not exact" in capsys.readouterr().out


def test_ulps_counts_representable_doubles():
  assert ulps(1.0, 1.0) == 0 and ulps(1.0, math.nextafter(1.0, 2.0)) == 1
  assert ulps(-0.0, 0.0) == 0 and ulps(math.nextafter(0.0, -1.0), math.nextafter(0.0, 1.0)) == 2


# -- provenance, item, bytes ------------------------------------------------------------------------------------------
def test_provenance_keys_that_differ_say_how():
  ref = {"a": 1, "b": [1, 2, 3], "generated": "x", "only_ref": 0}
  eng = {"a": 1, "b": [1, 9, 3], "generated": "y", "only_eng": 0}
  c = compare_provenance(eng, ref)
  assert c["equal"] == ["a"]
  assert c["differ"] == {"b": {"len_engine": 3, "len_ref": 3, "first_diff_index": 1, "engine": 9, "ref": 2}}
  assert c["expected_diff"] == {"generated": {"engine": "y", "ref": "x"}}
  assert c["only_engine"] == ["only_eng"] and c["only_ref"] == ["only_ref"] and c["key_order_equal"]


def test_a_longer_list_differs_at_its_first_extra_element():
  c = compare_provenance({"b": [1, 2, 3]}, {"b": [1, 2]})
  assert c["differ"]["b"] == {"len_engine": 3, "len_ref": 2, "first_diff_index": 2, "engine": 3}


def test_item_properties_are_checked_against_the_provenance():
  prov = {"start_datetime": "s", "end_datetime": "e", "rows": 3, "mask_cells": 6, "generated": "g1"}
  item = {"properties": {"start_datetime": "s", "end_datetime": "e", "table:row_count": 4,
                         "erddap-places:mask_cells": 6, "created": "g0"}}
  c = compare_item(item, prov)
  assert sorted(c["equal"]) == ["end_datetime", "erddap-places:mask_cells", "start_datetime"]
  assert c["differ"] == {"table:row_count": {"engine": 3, "ref": 4}} and "created" in c["expected_diff"]
  assert set(ITEM_KEYS.values()) >= {"rows", "griddap_requests", "generated"}


def test_bytes_equal_and_not_equal_with_the_reasons(tmp_path):
  pq.write_table(table(), tmp_path / "a.parquet", compression="zstd")
  pq.write_table(table(), tmp_path / "b.parquet", compression="zstd")
  pq.write_table(table(), tmp_path / "c.parquet", compression="snappy", row_group_size=2)
  assert compare_bytes(tmp_path / "a.parquet", tmp_path / "b.parquet")["equal"]
  c = compare_bytes(tmp_path / "a.parquet", tmp_path / "c.parquet", rows_exact=True)
  assert not c["equal"] and "row groups: 1 vs 2" in c["reasons"] and "codecs: ['ZSTD'] vs ['SNAPPY']" in c["reasons"]


# -- chunks ---------------------------------------------------------------------------------------------------------
def make_ref_store(root, names):
  d = root / "ds" / "v" / "TEST:x" / "abc123abc123"
  d.mkdir(parents=True)
  for n in names:
    pq.write_table(table(), d / n)
  return d


NAMES = ["2026-01-01_2026-01-31.parquet", "2026-02-01_2026-02-28.parquet", "2026-03-01_2026-03-10.parquet"]


def test_the_reference_key_dir_is_found_and_seeded_by_mode(tmp_path):
  d = make_ref_store(tmp_path / "ref", NAMES)
  assert reference_key_dir(tmp_path / "ref", "ds", "v", "TEST:x") == d
  assert reference_key_dir(tmp_path / "ref", "ds", "v", "TEST:y") is None
  names, copied = seed_chunks(d, tmp_path / "a", "all")
  assert names == NAMES and copied == [] and not list((tmp_path / "a").iterdir())
  names, copied = seed_chunks(d, tmp_path / "b", "last:1")
  assert copied == NAMES[:2] and sorted(p.name for p in (tmp_path / "b").iterdir()) == NAMES[:2]
  _, copied = seed_chunks(d, tmp_path / "c", "none")
  assert copied == NAMES
  with pytest.raises(ValueError, match="fetch mode"):
    seed_chunks(d, tmp_path / "d", "some")


def test_fetched_chunks_are_compared_with_the_reference(tmp_path):
  ref = make_ref_store(tmp_path / "ref", NAMES[:2])
  eng = tmp_path / "eng"
  eng.mkdir()
  pq.write_table(table(), eng / NAMES[0])
  pq.write_table(table(mean=(1.0, 2.0, math.nextafter(3.0, 4.0))), eng / NAMES[1])
  c = compare_chunks([("2026-01-01", "2026-01-31"), ("2026-02-01", "2026-02-28")], eng, ref)
  assert c["compared"] == 2 and c["exact"] == 1 and c["columns"]["mean"]["max_ulps"] == 1
  assert compare_chunks([], eng, None) == {"compared": 0}


# -- the harness end to end (offline) ---------------------------------------------------------------------------------
PID = "TEST:square"


def publish(tmp_path):
  """A local gazetteer copy: the collection, the places file, and a published stats file, provenance and item written
  by the engine itself from a first chunk store (the 'runner')."""
  gz = tmp_path / "gz"
  (gz / "erddap" / "sst_deg1").mkdir(parents=True)
  (gz / "erddap" / "sst_deg1" / "collection.json").write_text(json.dumps(collection()))
  (gz / "places").mkdir()
  pq.write_table(pa.table({"place_id": [PID], "geometry": pa.array([wkb("square")], pa.binary())}),
                 gz / "places" / "places.parquet")
  stats = gz / "stats" / "sst_deg1" / "sst"
  stats.mkdir(parents=True)
  x = extract(collection(), PID, wkb("square"), "sst", chunks=tmp_path / "runner", fetch=FakeErddap(),
              stats_path=stats / f"{PID}.parquet", sleep=lambda s: None)
  prov = stats_provenance(x, generated="2026-10-22T00:00:00Z")
  (stats / f"{PID}.provenance.json").write_text(provenance_text(prov))
  (gz / "stats" / "items").mkdir()
  item = {"properties": {ik: prov[pk] for ik, pk in ITEM_KEYS.items() if pk in prov}}
  item["properties"]["created"] = prov["generated"]
  (gz / "stats" / "items" / "sst_deg1_sst_TEST-square.json").write_text(json.dumps(item))
  return gz


def test_the_engine_against_its_own_published_file_is_exact_to_the_byte(tmp_path):
  gz = publish(tmp_path)
  res = run_parity("sst_deg1", "sst", PID, tmp_path / "out", base=gz, fetch=FakeErddap(), sleep=lambda s: None,
                   http=lambda u: pytest.fail(f"no http: {u}"))
  assert res["rows_verdict"] == "exact" and res["rows"]["rows_engine"] == 100 and res["verdict"]["pass"]
  assert res["bytes"]["equal"]
  assert res["provenance"]["differ"] == {} and list(res["provenance"]["expected_diff"]) == ["generated"]
  assert res["item"]["differ"] == {}
  assert json.loads((tmp_path / "out" / "parity.json").read_text())["rows_verdict"] == "exact"
  txt = verdict_table(res)
  assert "-> exact" in txt and "bytes: equal" in txt


def test_with_the_runner_store_the_key_matches_and_fresh_chunks_are_compared(tmp_path):
  gz = publish(tmp_path)
  res = run_parity("sst_deg1", "sst", PID, tmp_path / "out", base=gz, fetch=FakeErddap(), sleep=lambda s: None,
                   reference_chunks=tmp_path / "runner", fetch_mode="last:1")
  assert res["mask_key_equal"] and res["copied_from_reference"] == 1 and res["fetched"] == 1
  assert res["chunks"]["compared"] == 1 and res["chunks"]["exact"] == 1
  assert res["rows_verdict"] == "exact"


def test_a_batch_writes_one_parity_file_per_place_and_a_summary(tmp_path):
  gz = publish(tmp_path)
  res = run_batch("sst_deg1", ["sst"], [PID, "TEST:none"], tmp_path / "out", base=gz, fetch=FakeErddap(),
                  sleep=lambda s: None, attempts=2, rest_s=(0,))
  assert res[0]["rows_verdict"] == "exact" and "error" in res[1] and res[1]["tries"] == 2
  assert (tmp_path / "out" / "sst" / "parity_TEST:square.json").is_file()
  md = (tmp_path / "out" / "summary.md").read_text()
  assert "| sst | TEST:square | 100 / 100 | exact |" in md and "| sst | TEST:none |" in md


def test_tries_rest_between_failures_and_raise_the_last():
  calls, sleeps = [], []

  def fn():
    calls.append(1)
    raise OSError("down")
  with pytest.raises(OSError, match="down"):
    run_with_tries(fn, 3, rest_s=(600, 1800), sleep=sleeps.append)
  assert len(calls) == 3 and sleeps == [600, 1800]


def test_the_summary_shows_columns_that_are_not_exact():
  c = compare_rows(table(mean=(1.0, math.nextafter(2.0, 3.0), 3.0)), table())
  r = {"variable": "v", "place_id": "P", "rows": c, "rows_verdict": rows_verdict(c), "provenance": {"differ": {}},
       "bytes": {"equal": False}, "chunks": {"compared": 0}, "engine_seconds": 1.0}
  assert "mean (max 4.4e-16 (1 ulp), 1 rows)" in summary_markdown([r])


def test_a_try_that_finishes_chunks_resets_the_count():
  # schedule.ts: a place is given up only after `attempts` tries in a row that finish no chunk
  done, sleeps = [0], []

  def fn():
    done[0] += 1 if len(sleeps) < 3 else 0      # the first three tries each finish a chunk, then none do
    raise OSError("down")
  with pytest.raises(OSError):
    run_with_tries(fn, 2, rest_s=(1, 2), sleep=sleeps.append, progress=lambda: done[0])
  assert sleeps == [1, 1, 1, 1]   # three tries with progress, then two without: the second gives up
