"""load_collection and dataset_of (erddap-places' toDataset()): one test per default."""
import json

import pytest

from om_extract.layers import CubeVariable, dataset_of, format_layer_info, is_categorical, layer_info, load_collection, to_long_format
from fixtures_erddap import BASE, grid, info_table


def test_dataset_of_reads_the_erddap_fields_and_cube_variables_of_a_grid():
  d = dataset_of(grid(**{"erddap-places__depth": 0.494025}))
  assert (d.id, d.title, d.base_url, d.dataset_id, d.protocol) == ("erddap/dhw_5km", "Daily 5km SST (synthetic)", BASE, "dhw_5km", "griddap")
  assert (d.version, d.cors, d.formats, d.lon_range, d.lat_descending) == ("2.29", True, ["parquet", "csvp", "json"], (-180, 180), True)
  assert (d.time_step, d.time_extent, d.depth, d.status, d.format) == ("P1D", ["1985-04-01T12:00:00Z", None], 0.494025, None, "parquet")
  assert d.variables[0] == CubeVariable("CRW_SST", "Celsius", "CRW_SST", False, None)


def test_the_title_defaults_to_the_id():
  assert dataset_of(grid(title=None)).title == "erddap/dhw_5km"


def test_the_protocol_is_griddap_unless_it_is_exactly_tabledap():
  assert dataset_of(grid(erddap__protocol="tabledap")).protocol == "tabledap"
  assert dataset_of(grid(erddap__protocol="opendap")).protocol == "griddap"
  assert dataset_of(grid(erddap__protocol=None)).protocol == "griddap"


def test_cors_is_on_unless_it_is_false():
  assert dataset_of(grid(erddap__cors=False)).cors is False
  assert dataset_of(grid(erddap__cors=None)).cors is True


def test_formats_default_to_none():
  assert dataset_of(grid(erddap__formats=None)).formats == []


def test_lon_range_defaults_to_minus_180_180():
  assert dataset_of(grid(erddap__lon_range=None)).lon_range == (-180, 180)
  assert dataset_of(grid(erddap__lon_range=[0, 360])).lon_range == (0, 360)


def test_latitude_ascends_unless_flagged_descending():
  assert dataset_of(grid(erddap__lat_descending=None)).lat_descending is False
  assert dataset_of(grid(erddap__lat_descending="true")).lat_descending is False  # exactly true only
  assert dataset_of(grid()).lat_descending is True


def test_only_data_variables_count_and_a_missing_type_is_data():
  doc = grid()
  doc["cube:variables"] = {"a": {"type": "data"}, "b": {"type": "auxiliary"}, "c": {"unit": "K"}}
  assert [v.name for v in dataset_of(doc).variables] == ["a", "c"]


def test_a_variables_unit_defaults_to_none_and_its_description_to_its_name():
  doc = grid()
  doc["cube:variables"] = {"a": {"type": "data", "unit": None, "description": "sea surface temperature"}, "b": {}}
  a, b = dataset_of(doc).variables
  assert (a.unit, a.description) == (None, "sea surface temperature") and (b.unit, b.description) == (None, "b")


def test_categorical_is_read_whether_published_as_a_boolean_or_the_string_true():
  assert is_categorical({"erddap-places:categorical": True}) and is_categorical({"erddap-places:categorical": "true"})
  assert not is_categorical({"erddap-places:categorical": False}) and not is_categorical({}) and not is_categorical(None)


def test_a_categorical_variable_carries_its_class_labels_and_the_others_do_not():
  v = {x.name: x for x in dataset_of(grid()).variables}
  assert v["CRW_BAA"].categorical and v["CRW_BAA"].classes == {"0": "No Stress", "1": "Bleaching Watch"}
  assert not v["CRW_SST"].categorical and v["CRW_SST"].classes is None


def test_depth_is_a_finite_number_or_none():
  assert dataset_of(grid(**{"erddap-places__depth": 0.494})).depth == 0.494
  assert dataset_of(grid(**{"erddap-places__depth": "0.494"})).depth == 0.494
  assert dataset_of(grid()).depth is None
  assert dataset_of(grid(**{"erddap-places__depth": "deep"})).depth is None
  assert dataset_of(grid(**{"erddap-places__depth": float("inf")})).depth is None


def test_status_pending_is_carried_and_absent_otherwise():
  assert dataset_of(grid(**{"erddap-places__status": "pending"})).status == "pending"
  assert dataset_of(grid()).status is None


def test_format_is_the_best_the_server_serves():
  assert dataset_of(grid(erddap__formats=["parquetWMeta", "parquet"])).format == "parquetWMeta"
  assert dataset_of(grid(erddap__formats=["csvp", "json"])).format == "csvp"
  assert dataset_of(grid(erddap__cors=False)).format == "jsonp"
  assert dataset_of(grid(erddap__formats=None, erddap__cors=None)).format == "parquet"


def test_time_step_and_extent_default_to_none_without_a_time_dimension():
  doc = grid()
  doc["cube:dimensions"] = {}
  d = dataset_of(doc)
  assert d.time_step is None and d.time_extent is None


def test_long_format_needs_both_columns():
  assert to_long_format({"type_column": "measurement_type", "value_column": "measurement_value"}) == {
    "type_column": "measurement_type", "value_column": "measurement_value"}
  assert to_long_format({"type_column": "x"}) is None and to_long_format(None) is None
  doc = grid(**{"erddap-places__long_format": {"type_column": "measurement_type", "value_column": "measurement_value"}})
  assert dataset_of(doc).long_format["value_column"] == "measurement_value"


def test_load_collection_reads_a_file_and_a_file_url(tmp_path):
  p = tmp_path / "collection.json"
  p.write_text(json.dumps(grid()))
  assert load_collection(p)["id"] == "erddap/dhw_5km" and load_collection(f"file://{p}")["id"] == "erddap/dhw_5km"


def test_load_collection_fetches_a_url_through_the_injected_fetch():
  seen = []

  def fetch(url):
    seen.append(url)
    return json.dumps(grid()).encode()

  assert load_collection("https://example.org/erddap/dhw_5km/collection.json", fetch=fetch)["id"] == "erddap/dhw_5km"
  assert seen == ["https://example.org/erddap/dhw_5km/collection.json"]


def test_layer_info_is_the_dataset_record_and_the_time_extent_from_a_saved_info_table(tmp_path):
  c, i = tmp_path / "c.json", tmp_path / "info.json"
  c.write_text(json.dumps(grid()))
  i.write_text(json.dumps(info_table("1985-04-01T12:00:00Z", "2026-09-13T12:00:00Z")))
  rec = layer_info(c, info_path=i)
  assert rec["dataset"]["dataset_id"] == "dhw_5km" and rec["dataset"]["variables"][1]["classes"]["1"] == "Bleaching Watch"
  assert rec["extent"]["time_end"] == "2026-09-13T12:00:00Z" and rec["extent"]["live"] is True
  text = format_layer_info(rec)
  assert "time extent   1985-04-01T12:00:00Z to 2026-09-13T12:00:00Z (live from the server)" in text
  assert "variables     CRW_SST, CRW_BAA (categorical)" in text
