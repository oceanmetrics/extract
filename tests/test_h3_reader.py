"""The H3 reader on synthetic obis-h3-like layers (`fixtures_h3`): paths, parents, periods, cells. No network."""
import datetime as dt

import pyarrow as pa
import pytest

from om_extract import readers
from om_extract.readers import H3Reader, InvalidProfile, Reader
from om_extract.readers.h3 import h3_parent, h3_res, period_date
from fixtures_h3 import P3_A, P3_B, R5, R5_OUT, R7_A, R7_B, collection, layer_rows, write


def logged(log):
  from om_extract.readers.h3 import default_fetch
  def fetch(p):
    log.append(p)
    return default_fetch(p)
  return fetch


def test_the_h3_reader_satisfies_the_reader_protocol_and_for_collection_dispatches_on_h3_resolutions(tmp_path):
  r = readers.for_collection(collection(), root=tmp_path)
  assert isinstance(r, H3Reader) and isinstance(r, Reader) and r.kind == "h3"
  assert (r.resolution, r.grain) == (7, "all")  # the finest published resolution by default; no periods


def test_a_collection_that_fails_the_h3_profile_raises_with_the_schema_errors(tmp_path):
  with pytest.raises(InvalidProfile, match="not a valid h3"):
    H3Reader(collection(**{"h3:layout": {"template": "all/data_0.parquet"}}), root=tmp_path)


def test_h3_parent_is_the_h3_librarys_cell_to_parent_by_bit_arithmetic():
  # pairs from h3 4.x: h3.cell_to_parent(cell, res)
  assert h3_parent("872830828ffffff", 3) == "832830fffffffff"
  assert h3_parent("872830828ffffff", 2) == "822837fffffffff"
  assert h3_parent("87756a535ffffff", 5) == "85756a53fffffff"
  assert [h3_parent(c, 3) for c in R7_A + R7_B] == [P3_A, P3_A, P3_B]
  assert (h3_res("87756a535ffffff"), h3_parent("87756a535ffffff", 7)) == (7, "87756a535ffffff")
  with pytest.raises(ValueError, match="not coarser"):
    h3_parent("85756a53fffffff", 6)


def test_period_labels_are_the_first_day_of_their_period():
  assert period_date("decade", "1990s") == dt.date(1990, 1, 1)
  assert period_date("year", "2005") == dt.date(2005, 1, 1)
  assert period_date("month", "2005-03") == dt.date(2005, 3, 1)
  assert period_date("all", "anything") is None and period_date("decade", None) is None
  with pytest.raises(ValueError, match="not a decade label"):
    period_date("decade", "nineties")


def test_a_whole_file_resolution_reads_one_file_and_returns_only_the_cells_asked(tmp_path):
  write(tmp_path / "all/res=5/data_0.parquet", layer_rows(R5 + [R5_OUT], [10, 20, 40, 80]))
  log = []
  r = H3Reader(collection(), resolution=5, root=tmp_path, fetch=logged(log))
  t = r.cells(5, [c.upper() for c in R5])  # a cell id is matched as lowercase hex
  assert log == [str(tmp_path / "all/res=5/data_0.parquet")]
  assert sorted(zip(t["h3"].to_pylist(), t["shannon"].to_pylist())) == list(zip(R5, [10.0, 20.0, 40.0]))
  assert r.cells(5).num_rows == 4  # all rows when no cells are given


def test_a_split_resolution_reads_only_the_parent_partitions_of_the_cells_asked(tmp_path):
  write(tmp_path / f"all/res=7/p={P3_A}/data_0.parquet", layer_rows(R7_A, [1, 2]))
  write(tmp_path / f"all/res=7/p={P3_B}/data_0.parquet", layer_rows(R7_B, [3]))
  log = []
  r = H3Reader(collection(), root=tmp_path, fetch=logged(log))
  assert r.paths(7, R7_A) == [(str(tmp_path / f"all/res=7/p={P3_A}/data_0.parquet"), None)]
  t = r.cells(7, R7_A[:1])
  assert log == [str(tmp_path / f"all/res=7/p={P3_A}/data_0.parquet")] and t["h3"].to_pylist() == R7_A[:1]
  with pytest.raises(ValueError, match="split by parent"):
    r.cells(7)


def test_a_missing_partition_holds_no_rows(tmp_path):
  r = H3Reader(collection(), root=tmp_path)
  t = r.cells(7, R7_B)
  assert t.num_rows == 0 and r.files_read == []


def test_a_uint64_cell_column_is_matched_by_the_integer_index(tmp_path):
  doc = collection(**{"h3:cell_column": "cell_id", "h3:cell_type": "uint64", "h3:resolutions": [5],
                      "h3:layout": {"template": "res={res}.parquet"}})
  write(tmp_path / "res=5.parquet", layer_rows(R5 + [R5_OUT], [10, 20, 40, 80]))
  t = H3Reader(doc, root=tmp_path).cells(5, R5[:2])
  assert t["cell_id"].to_pylist() == [int(c, 16) for c in R5[:2]]


def test_periods_come_from_the_path_when_the_template_holds_the_placeholder(tmp_path):
  doc = collection(**{"h3:layout": {"template": "{period}/res={res}/data_0.parquet"}, "h3:resolutions": [5],
                      "h3:periods": {"grain": "decade", "values": ["1990s", "2000s"], "placeholder": "period"}})
  write(tmp_path / "1990s/res=5/data_0.parquet", layer_rows(R5, [1, 2, 3]))
  write(tmp_path / "2000s/res=5/data_0.parquet", layer_rows(R5, [4, 5, 6]))
  r = H3Reader(doc, root=tmp_path)
  t = r.cells(5, R5[:1])
  assert list(zip(t["period"].to_pylist(), t["shannon"].to_pylist())) == [("1990s", 1.0), ("2000s", 4.0)]
  assert (r.grain, r.extent().time_start, r.extent().time_end) == ("decade", "1990-01-01", "2000-01-01")


def test_periods_come_from_a_column_named_by_the_placeholder_otherwise(tmp_path):
  doc = collection(**{"h3:resolutions": [5], "h3:periods": {"grain": "decade", "values": ["1990s", "2000s"],
                                                            "placeholder": "decade"}})
  write(tmp_path / "all/res=5/data_0.parquet",
        layer_rows(R5[:1] * 2, [1, 4], decade=pa.array(["1990s", "2000s"])))
  t = H3Reader(doc, root=tmp_path).cells(5, R5[:1])
  assert t["period"].to_pylist() == ["1990s", "2000s"]
  write(tmp_path / "all/res=5/data_0.parquet", layer_rows(R5[:1], [1]))
  with pytest.raises(ValueError, match="neither in the path nor in a column 'decade'"):
    H3Reader(doc, root=tmp_path).cells(5, R5[:1])


def test_the_layouts_other_keys_are_filled_from_keys_and_a_missing_one_is_named(tmp_path):
  doc = collection(**{"h3:resolutions": [5], "h3:layout": {"template": "eov={eov}/res={res}.parquet",
                                                           "keys": ["eov"]}})
  assert H3Reader(doc, root=tmp_path, keys={"eov": "sst"}).paths(5) == [(str(tmp_path / "eov=sst/res=5.parquet"), None)]
  with pytest.raises(ValueError, match=r"needs \{eov\}"):
    H3Reader(doc, root=tmp_path).paths(5)


def test_an_h3_layer_has_no_axes_slab_or_slice(tmp_path):
  r = H3Reader(collection(), root=tmp_path)
  for call in (lambda: r.axes((0, 0, 1, 1)), lambda: r.slab((0, 0, 1, 1), "a", "b", "n"),
               lambda: r.slice((0, 0, 1, 1), "a", "n")):
    with pytest.raises(NotImplementedError, match="extract_h3"):
      call()


def test_the_root_defaults_to_the_directory_of_the_self_link():
  doc = collection(links=[{"rel": "self", "href": "https://example.org/obis/h3/collection.json"}])
  r = H3Reader(doc)
  assert r.paths(5) == [("https://example.org/obis/h3/all/res=5/data_0.parquet", None)]
  with pytest.raises(ValueError, match="no root"):
    H3Reader(collection())
