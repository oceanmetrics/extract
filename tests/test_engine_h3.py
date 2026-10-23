"""extract_h3(): an H3 layer joined with a place's H3 membership, share-weighted (`fixtures_h3`). No network.

By hand: the place holds three res 5 cells with shares 1, 0.5 and 0.25; the layer has 10, 20 and 40 there and 80 in a
cell outside the place. Over the three cells: n 3, weight_sum 1.75, mean_wt (10 * 1 + 20 * 0.5 + 40 * 0.25) / 1.75
= 30 / 1.75 = 120 / 7, mean 70 / 3, sd the sample SD of 10, 20, 40, min 10, max 40, p10 12, p90 36.
"""
import datetime as dt
import statistics

import pytest

from om_extract import contract
from om_extract.engine_h3 import H3_STATS_COLUMNS, extract_h3, h3_provenance_for
from om_extract.membership import load_membership
from om_extract.values import values_rows
from fixtures_h3 import P3_A, R5, R5_OUT, R7_A, collection, layer_rows, membership_tree, write

REL = 1e-12
MEMBERSHIP = [{"place_id": "TEST:a", "h3": R5[0], "share": 1.0}, {"place_id": "TEST:a", "h3": R5[1], "share": 0.5},
              {"place_id": "TEST:a", "h3": R5[2], "share": 0.25}, {"place_id": "TEST:b", "h3": R5_OUT, "share": 1.0}]


def layer(tmp_path, values=(10, 20, 40, 80)):
  write(tmp_path / "all/res=5/data_0.parquet", layer_rows(R5 + [R5_OUT], list(values)))


def test_the_share_weighted_mean_and_the_cell_statistics_by_hand(tmp_path):
  layer(tmp_path)
  x = extract_h3(collection(), "TEST:a", MEMBERSHIP, "shannon", 5, root=tmp_path)
  assert x.stats.column_names == list(H3_STATS_COLUMNS) and x.stats.num_rows == 1
  r = x.stats.to_pylist()[0]
  assert (r["date"], r["period"], r["n"], r["min"], r["max"]) == (None, None, 3, 10, 40)
  for k, want in (("mean_wt", 120 / 7), ("mean", 70 / 3), ("sd", statistics.stdev([10, 20, 40])),
                  ("weight_sum", 1.75), ("p10", 12), ("p90", 36)):
    assert r[k] == pytest.approx(want, rel=REL), k
  assert (r["place_id"], r["dataset_id"], r["variable"]) == ("TEST:a", "obis_h3_test", "shannon")
  assert (x.membership_cells, x.layer_cells, x.time_grain) == (3, 3, "all")


def test_n_counts_the_cells_with_a_value_and_weight_sum_is_their_shares(tmp_path):
  layer(tmp_path, values=(10, None, 40, 80))
  r = extract_h3(collection(), "TEST:a", MEMBERSHIP, "shannon", 5, root=tmp_path).stats.to_pylist()[0]
  assert (r["n"], r["weight_sum"]) == (2, 1.25)
  assert r["mean_wt"] == pytest.approx((10 + 40 * 0.25) / 1.25, rel=REL)


def test_a_period_layer_gives_one_row_per_period_at_the_first_day_of_each(tmp_path):
  doc = collection(**{"h3:layout": {"template": "{period}/res={res}/data_0.parquet"}, "h3:resolutions": [5],
                      "h3:periods": {"grain": "decade", "values": ["1990s", "2000s"], "placeholder": "period"}})
  write(tmp_path / "1990s/res=5/data_0.parquet", layer_rows(R5, [10, 20, 40]))
  write(tmp_path / "2000s/res=5/data_0.parquet", layer_rows(R5, [1, 1, 1]))
  x = extract_h3(doc, "TEST:a", MEMBERSHIP, "shannon", 5, root=tmp_path)
  rows = x.stats.to_pylist()
  assert [(r["date"], r["period"], r["n"]) for r in rows] == [(dt.date(1990, 1, 1), "1990s", 3),
                                                               (dt.date(2000, 1, 1), "2000s", 3)]
  assert rows[0]["mean_wt"] == pytest.approx(120 / 7, rel=REL) and rows[1]["mean_wt"] == pytest.approx(1, rel=REL)
  vr = values_rows(x)
  assert [(v["time_grain"], v["time"]) for v in vr] == [("decade", dt.date(1990, 1, 1)), ("decade", dt.date(2000, 1, 1))]


def test_res_7_reads_the_membership_and_the_layer_by_the_places_parent_partition(tmp_path):
  base = membership_tree(tmp_path / "gaz", {7: [("TEST:a", R7_A[0], 1.0), ("TEST:a", R7_A[1], 0.5)]}, {7: 3})
  write(tmp_path / f"all/res=7/p={P3_A}/data_0.parquet", layer_rows(R7_A, [2, 8]))
  mem = load_membership("places", "TEST:a", 7, base)
  x = extract_h3(collection(), "TEST:a", mem, "shannon", 7, root=tmp_path)
  assert x.files == [str(tmp_path / f"all/res=7/p={P3_A}/data_0.parquet")]
  assert x.stats["mean_wt"].to_pylist() == pytest.approx([(2 + 8 * 0.5) / 1.5], rel=REL)


def test_a_place_with_no_cells_in_the_layer_gives_no_rows(tmp_path):
  layer(tmp_path)
  x = extract_h3(collection(), "TEST:c", [{"place_id": "TEST:c", "h3": "85756a5bfffffff", "share": 1.0}], "shannon",
                 5, root=tmp_path)
  assert x.stats.num_rows == 0 and x.stats.column_names == list(H3_STATS_COLUMNS)


def test_a_variable_that_is_not_a_metric_column_is_named(tmp_path):
  with pytest.raises(ValueError, match=r"'sst' is not a metric column of obis_h3_test"):
    extract_h3(collection(), "TEST:a", MEMBERSHIP, "sst", 5, root=tmp_path)


def test_values_rows_carry_the_h3_indicator_id_the_grain_all_and_write_through_the_contract(tmp_path):
  layer(tmp_path)
  x = extract_h3(collection(), "TEST:a", MEMBERSHIP, "shannon", 5, root=tmp_path, coll="test")
  rows = values_rows(x)
  assert len(rows) == 1
  r = rows[0]
  assert (r["indicator"], r["time_grain"], r["time"], r["coll"], r["unit"], r["n"], r["class"]) == \
         ("h3:obis_h3_test:shannon:mean", "all", None, "test", "1", 3.0, None)
  assert r["value"] == pytest.approx(120 / 7, rel=REL) and r["sd"] == pytest.approx(statistics.stdev([10, 20, 40]))
  assert values_rows(x, stat="mean")[0]["indicator"] == "h3:obis_h3_test:shannon:mean_unw"
  assert values_rows(x, stat="max")[0]["sd"] is None
  with pytest.raises(ValueError, match="coarser grain"):
    values_rows(x, grain="year")
  path = contract.write_values(rows, tmp_path / "out", h3_provenance_for(x, "0.0.0"))
  assert contract.validate_file(path) == []
  _, prov = contract.read_values(path)
  assert [s["role"] for s in prov["sources"]] == ["data", "product"]


def test_a_unit_on_the_column_is_the_rows_unit(tmp_path):
  layer(tmp_path)
  doc = collection()
  next(c for c in doc["table:columns"] if c["name"] == "shannon")["unit"] = "nats"
  x = extract_h3(doc, "TEST:a", MEMBERSHIP, "shannon", 5, root=tmp_path)
  assert values_rows(x)[0]["unit"] == "nats"
