"""URL rules ported from erddap-places: the expected strings are copied from erddap.test.ts and tabledap.test.ts."""
import pytest

from om_extract.readers.erddap import (TableConstraint, constraint, griddap_slice_url, griddap_url, griddap_urls, js_num,
                                       noon_z, pick_format, table_constraint, tabledap_place_constraints, tabledap_url)

BASE = "https://pae-paha.pacioos.hawaii.edu/erddap"
KW = dict(dataset_id="d", variable="v", time=["a", "b"], lat=[1, 2], lon=[3, 4])


def test_griddap_url_encodes_only_the_brackets_and_honours_a_descending_latitude():
  url = griddap_url(base=BASE + "/", dataset_id="dhw_5km", variable="CRW_SST",
                    time=["2026-08-01T12:00:00Z", "2026-08-30T12:00:00Z"], lat=[18.8, 22.4], lon=[-160.3, -154.5],
                    lat_descending=True)
  assert url == (BASE + "/griddap/dhw_5km.parquet?CRW_SST"
                 "%5B(2026-08-01T12:00:00Z):1:(2026-08-30T12:00:00Z)%5D"
                 "%5B(22.4):1:(18.8)%5D"
                 "%5B(-160.3):1:(-154.5)%5D")


def test_griddap_url_puts_an_ascending_latitude_axis_the_other_way_round():
  assert "%5B(1):1:(2)%5D%5B(3):1:(4)%5D" in griddap_url(base=BASE, lat_descending=False, **KW)


def test_griddap_url_slices_a_single_depth_level_between_time_and_latitude():
  url = griddap_url(base=BASE, dataset_id="d", variable="chl", time=["a", "b"], lat=[1, 2], lon=[3, 4],
                    lat_descending=False, depth=0.4940254)
  assert url == BASE + "/griddap/d.parquet?chl%5B(a):1:(b)%5D%5B(0.4940254):1:(0.4940254)%5D%5B(1):1:(2)%5D%5B(3):1:(4)%5D"


def test_griddap_url_adds_no_depth_constraint_to_a_3d_grid():
  assert griddap_url(base=BASE, lat_descending=False, **KW).count("%5B") == 3


def test_griddap_url_appends_the_jsonp_callback_and_uses_json():
  url = griddap_url(base=BASE, format="jsonp", callback="cb1", **KW)
  assert "/griddap/d.json?" in url and url.endswith("&.jsonp=cb1")


def test_griddap_urls_gives_one_url_per_lobe():
  u = griddap_urls(base=BASE, dataset_id="d", variable="v", time=["a", "b"],
                   bboxes=[(-180, 20, -175, 30), (175, 20, 180, 30)])
  assert len(u) == 2 and "(-180):1:(-175)" in u[0] and "(175):1:(180)" in u[1]


def test_griddap_slice_url_asks_for_one_time_step_and_keeps_the_rest_of_the_request():
  url = griddap_slice_url(base=BASE + "/", dataset_id="dhw_5km", variable="CRW_SST", at="2026-10-06T12:00:00Z",
                          lat=[18.8, 22.4], lon=[-160.3, -154.5], lat_descending=True, format="parquetWMeta")
  assert url == (BASE + "/griddap/dhw_5km.parquetWMeta?CRW_SST"
                 "%5B(2026-10-06T12:00:00Z):1:(2026-10-06T12:00:00Z)%5D%5B(22.4):1:(18.8)%5D%5B(-160.3):1:(-154.5)%5D")
  assert griddap_slice_url(base=BASE, dataset_id="d", variable="chl", at="T", lat=[1, 2], lon=[3, 4],
                           lat_descending=False, depth=0.49) == (
    BASE + "/griddap/d.parquet?chl%5B(T):1:(T)%5D%5B(0.49):1:(0.49)%5D%5B(1):1:(2)%5D%5B(3):1:(4)%5D")
  assert griddap_slice_url(base=BASE, dataset_id="d", variable="v", at="T", lat=[1, 2], lon=[3, 4], format="jsonp",
                           callback="cb0").endswith("&.jsonp=cb0")


def test_constraint_and_noon_z():
  assert constraint(1, 2) == "%5B(1):1:(2)%5D"
  assert noon_z("2026-09-10") == "2026-09-10T12:00:00Z"
  from datetime import datetime, timezone
  assert noon_z(datetime(2026, 9, 10, 3, tzinfo=timezone.utc)) == "2026-09-10T12:00:00Z"


@pytest.mark.parametrize("x,expected", [(1.0, "1"), (22.4, "22.4"), (-160.3, "-160.3"), (0.4940254, "0.4940254"), (-0.0, "0"),
                                        (0.000001, "0.000001"), (1e-7, "1e-7"), (1.5e-7, "1.5e-7"), (1e21, "1e+21"),
                                        (123456789012.0, "123456789012"), (7, "7")])
def test_js_num_prints_numbers_as_a_javascript_template_literal_does(x, expected):
  assert js_num(x) == expected


def test_a_float_integer_latitude_prints_without_a_decimal_point():
  assert "%5B(22):1:(18)%5D" in griddap_url(base=BASE, dataset_id="d", variable="v", time=["a", "b"], lat=[18.0, 22.0],
                                            lon=[3.0, 4.0])


def test_pick_format_prefers_parquet_when_the_server_advertises_it():
  assert pick_format({"erddap": {"cors": True, "formats": [".parquet", ".csvp", ".json"]}}) == "parquet"


def test_pick_format_falls_back_to_csvp_for_an_older_cors_server():
  assert pick_format({"erddap": {"cors": True, "formats": [".csvp", ".json"]}}) == "csvp"


def test_pick_format_is_jsonp_when_cors_is_off_whatever_the_formats():
  assert pick_format({"erddap": {"cors": False, "formats": [".parquet"]}}) == "jsonp"


def test_pick_format_ignores_cors_for_the_runner():
  assert pick_format({"erddap": {"cors": False, "formats": [".parquet"]}}, browser=False) == "parquet"


def test_pick_format_prefers_parquetwmeta_and_defaults_to_parquet():
  assert pick_format({"erddap": {"cors": True, "formats": ["parquetWMeta", "parquet"]}}) == "parquetWMeta"
  assert pick_format({"erddap": {"cors": True, "formats": []}}) == "parquet"
  assert pick_format({}) == "parquet"
  assert pick_format({"erddap": {"cors": True, "formats": ["json"]}}) == "jsonp"


# -- tabledap -------------------------------------------------------------------------------------------------------------
TBASE = "https://erddap.calcofi.io/erddap"
COLS = ["longitude", "latitude", "time", "depth", "measurement_type", "measurement_value"]


def test_table_constraint_encodes_only_the_comparison_characters():
  assert table_constraint(TableConstraint("time", ">=", "2015-01-01T00:00:00Z")) == "time%3E=2015-01-01T00:00:00Z"
  assert table_constraint(TableConstraint("longitude", "<=", -119)) == "longitude%3C=-119"
  assert table_constraint(TableConstraint("latitude", ">", 33.5)) == "latitude%3E33.5"
  assert table_constraint(TableConstraint("measurement_type", "=", "%22temperature%22")) == "measurement_type=%22temperature%22"


def test_tabledap_url_builds_the_verified_calcofi_request():
  url = tabledap_url(base=TBASE + "/", dataset_id="calcofi_bottle", columns=COLS,
                     constraints=tabledap_place_constraints(bbox=(-121, 33.5, -119, 34.5), from_="2015-01-01", to="2016-01-01"))
  assert url == ("https://erddap.calcofi.io/erddap/tabledap/calcofi_bottle.parquetWMeta"
                 "?longitude%2Clatitude%2Ctime%2Cdepth%2Cmeasurement_type%2Cmeasurement_value"
                 "&time%3E=2015-01-01T00:00:00Z&time%3C=2016-01-01T23:59:59Z"
                 "&longitude%3E=-121&longitude%3C=-119"
                 "&latitude%3E=33.5&latitude%3C=34.5")
  assert "," not in url and "%5B" not in url


def test_tabledap_url_adds_the_long_format_row_filter_quoted_with_22():
  url = tabledap_url(base=TBASE, dataset_id="calcofi_bottle", columns=COLS,
                     constraints=tabledap_place_constraints(bbox=(-121, 33.5, -119, 34.5), from_="2015-01-01", to="2016-01-01",
                                                            type_column="measurement_type", type_value="temperature"))
  assert url.endswith("&measurement_type=%22temperature%22")


def test_tabledap_url_appends_server_side_functions_after_the_constraints():
  url = tabledap_url(base=TBASE, dataset_id="calcofi_bottle", columns=["time"], functions=['orderByMinMax(%22time%22)'],
                     format="csvp")
  assert url == f"{TBASE}/tabledap/calcofi_bottle.csvp?time&orderByMinMax(%22time%22)"


def test_tabledap_url_falls_back_to_jsonp_with_a_callback():
  assert tabledap_url(base=TBASE, dataset_id="x", columns=["time"], format="jsonp", callback="cb0") == (
    f"{TBASE}/tabledap/x.json?time&.jsonp=cb0")
