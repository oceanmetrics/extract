"""The ERDDAP reader on a synthetic dhw_5km-like collection: extent, axes, slab, slice, provenance. No network."""
import json
import math
import re
from datetime import datetime, timezone

import pytest

from om_extract import readers
from om_extract.readers import Axes, Extent, InvalidCollection, Reader, SLAB_SCHEMA, SLICE_SCHEMA, UnknownProfile
from om_extract.readers.erddap import ErddapError, ErddapGrid
from fixtures_erddap import BASE, grid, info_table, slab_parquet

BBOX = (-155.03, 20.02, -154.97, 20.08)  # 2 x 2 cells of the 0.05 lattice: lat 20.075, 20.025; lon -155.025, -154.975
INFO = info_table("1985-04-01T12:00:00Z", "2026-09-13T12:00:00Z")


def _no_fetch(url):
  raise AssertionError(f"unexpected request: {url}")


def reader(doc=None, **kw) -> ErddapGrid:
  kw.setdefault("info", INFO)
  kw.setdefault("fetch", _no_fetch)
  return ErddapGrid(doc or grid(), **kw)


def _ts(iso):
  return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


# -- construction and the interface ------------------------------------------------------------------------------------
def test_the_reader_satisfies_the_reader_protocol_and_declares_its_kind():
  r = reader()
  assert isinstance(r, Reader) and r.kind == "erddap"


def test_an_invalid_collection_raises_with_the_schema_errors():
  with pytest.raises(InvalidCollection) as e:
    ErddapGrid(grid(erddap__base_url=None))
  assert any("erddap:base_url" in m for m in e.value.errors) and "erddap/dhw_5km" in str(e.value)


def test_for_collection_dispatches_on_the_erddap_profile_for_a_dict_and_for_json_text():
  assert isinstance(readers.for_collection(grid(), info=INFO), ErddapGrid)
  assert isinstance(readers.for_collection(json.dumps(grid()), info=INFO), ErddapGrid)
  assert readers.kinds()[0] == "erddap"  # first in dispatch order (h3 and cog follow)


def test_for_collection_names_the_profiles_it_found_when_no_reader_matches():
  with pytest.raises(UnknownProfile, match=r"profile keys \['cog'\]"):
    readers.for_collection({"id": "atlas/x", "cog:crs": "EPSG:3857"})


# -- extent --------------------------------------------------------------------------------------------------------------
def test_extent_takes_the_live_time_range_from_the_info_table_not_the_open_catalog_end():
  e = reader().extent()
  assert isinstance(e, Extent)
  assert (e.time_start, e.time_end, e.live) == ("1985-04-01T12:00:00Z", "2026-09-13T12:00:00Z", True)
  assert e.step_days == 1 and e.step_label == "daily"
  assert e.bbox == (-179.975, -89.975, 179.975, 89.975) and e.depth_levels == ()


def test_extent_asks_the_server_for_info_when_none_is_given():
  seen = []

  def fetch(url):
    seen.append(url)
    return json.dumps(INFO).encode()

  e = ErddapGrid(grid(), fetch=fetch).extent()
  assert seen == [BASE + "/info/dhw_5km/index.json"] and e.live


def test_extent_falls_back_to_the_catalog_when_the_server_does_not_answer():
  def down(url):
    raise ErddapError("pae-paha.pacioos.hawaii.edu did not answer")

  e = ErddapGrid(grid(), fetch=down).extent()
  assert (e.time_start, e.time_end, e.live, e.step_days) == ("1985-04-01T12:00:00Z", None, False, None)


def test_extent_reports_the_depth_level_of_a_grid_with_a_depth_axis():
  doc = grid(erddap__lat_descending=False, **{"erddap-places__depth": 0.494025})
  doc["cube:dimensions"]["depth"] = {"type": "spatial", "axis": "z", "extent": [0.494025, 5727.917], "unit": "m"}
  assert reader(doc).extent().depth_levels == (0.494025,)


def test_extent_is_asked_once():
  n = []

  def fetch(url):
    n.append(url)
    return json.dumps(INFO).encode()

  r = ErddapGrid(grid(), fetch=fetch)
  assert r.extent() is r.extent() and len(n) == 1


# -- axes ----------------------------------------------------------------------------------------------------------------
def test_axes_are_the_lattice_centres_snapped_to_the_bbox_and_run_high_to_low_on_a_descending_grid():
  a = reader().axes(BBOX)
  assert isinstance(a, Axes) and a.source == "cube" and a.n_cells == 4
  assert a.lat.tolist() == [20.075, 20.025] and a.lon.tolist() == [-155.025, -154.975]
  assert a.step == (0.05, 0.05) and a.lat_descending is True and a.lon_range == (-180, 180) and a.depth is None


def test_axes_run_low_to_high_on_an_ascending_grid():
  assert reader(grid(erddap__lat_descending=False)).axes(BBOX).lat.tolist() == [20.025, 20.075]


def test_axes_shift_a_bbox_into_a_0_360_grid():
  doc = grid(erddap__lon_range=[0, 360])
  doc["cube:dimensions"]["longitude"]["extent"] = [0.025, 359.975]
  assert reader(doc).axes(BBOX).lon.tolist() == [204.975, 205.025]


def test_axes_clamp_a_lobe_running_to_180_to_a_grid_ending_at_179_75():
  doc = grid(erddap__lon_range=[-180, 179.75])
  doc["cube:dimensions"]["longitude"] = {"type": "spatial", "axis": "x", "extent": [-180, 179.75], "step": 0.25}
  a = reader(doc).axes((177.84, 25, 180, 30))
  assert a.lon[0] == 177.75 and a.lon[-1] == 179.75 and len(a.lon) == 9


def test_axes_do_not_drift_along_an_axis_whose_published_step_is_rounded():
  doc = grid()
  doc["cube:dimensions"]["longitude"] = {"type": "spatial", "axis": "x", "extent": [-180, 179.9167], "step": 0.083333}
  a = reader(doc).axes((-180, 0, 179.9167, 1))
  assert (a.lon[0], a.lon[-1], len(a.lon)) == (-180, 179.9167, 4320)


def test_axes_carry_the_depth_level_the_collection_declares_or_the_one_asked_for():
  doc = grid(**{"erddap-places__depth": 0.494025})
  assert reader(doc).axes(BBOX).depth == 0.494025
  assert reader(doc).axes(BBOX, depth=10.0).depth == 10.0


def test_axes_refuse_a_bbox_that_crosses_the_antimeridian_or_the_seam_of_a_0_360_grid():
  with pytest.raises(ValueError, match="antimeridian"):
    reader().axes((175, 20, -175, 30))
  doc = grid(erddap__lon_range=[0, 360])
  doc["cube:dimensions"]["longitude"]["extent"] = [0.025, 359.975]
  with pytest.raises(ValueError, match="seam"):
    reader(doc).axes((-1, 20, 1, 30))


def test_axes_refuse_a_bbox_wholly_outside_the_grid():
  doc = grid()
  doc["cube:dimensions"]["latitude"]["extent"] = [-80, -60]
  with pytest.raises(ValueError, match="outside the axis"):
    reader(doc).axes(BBOX)


def test_live_axes_ask_the_server_in_the_axis_direction_and_keep_its_order():
  seen = []

  def fetch(url):
    seen.append(url)
    rows = [[20.075], [20.025]] if "latitude" in url else [[-155.025], [-154.975]]
    return json.dumps({"table": {"columnNames": ["x"], "rows": rows}}).encode()

  a = ErddapGrid(grid(), fetch=fetch, info=INFO).axes(BBOX, live=True)
  assert a.source == "live" and a.lat.tolist() == [20.075, 20.025] and a.lon.tolist() == [-155.025, -154.975]
  assert a.step == (0.05, 0.05)
  assert seen == [BASE + "/griddap/dhw_5km.json?longitude%5B(-155.03):1:(-154.97)%5D",
                  BASE + "/griddap/dhw_5km.json?latitude%5B(20.08):1:(20.02)%5D"]


def test_axes_go_live_when_the_catalog_gives_no_step():
  doc = grid()
  del doc["cube:dimensions"]["latitude"]["step"]

  def fetch(url):
    return json.dumps({"table": {"columnNames": ["x"], "rows": [[1.0], [2.0]]}}).encode()

  a = ErddapGrid(doc, fetch=fetch, info=INFO).axes(BBOX)
  assert a.source == "live" and a.step[0] == 1.0


# -- slab and slice -------------------------------------------------------------------------------------------------------
def test_slab_urls_are_one_chunked_request_for_a_small_window_with_the_axis_ends_and_noon_instants():
  urls = reader().slab_urls(BBOX, "2026-08-01", "2026-08-03", "CRW_SST")
  assert urls == [BASE + "/griddap/dhw_5km.parquet?CRW_SST"
                  "%5B(2026-08-01T12:00:00Z):1:(2026-08-03T12:00:00Z)%5D"
                  "%5B(20.075):1:(20.025)%5D%5B(-155.025):1:(-154.975)%5D"]


def test_slab_urls_cut_a_long_window_at_90_days_and_cover_it_once():
  urls = reader().slab_urls(BBOX, "2026-01-01", "2026-07-19", "CRW_SST")
  spans = [re.search(r"\((\S{20})\):1:\((\S{20})\)", u).groups() for u in urls]
  assert spans == [("2026-01-01T12:00:00Z", "2026-03-31T12:00:00Z"), ("2026-04-01T12:00:00Z", "2026-06-29T12:00:00Z"),
                   ("2026-06-30T12:00:00Z", "2026-07-19T12:00:00Z")]


def test_slab_urls_shrink_the_chunk_so_a_request_stays_under_100k_values():
  doc = grid()
  big = (-160.0, 10.0, -150.0, 20.0)  # 200 x 200 cells = 40,000 a step: 2 days a request
  assert reader(doc).axes(big).n_cells == 40_401
  assert len(reader(doc).slab_urls(big, "2026-08-01", "2026-08-05", "CRW_SST")) == 3


def test_slab_urls_use_the_extent_end_verbatim_and_clamp_a_window_that_runs_past_it():
  urls = reader().slab_urls(BBOX, "2026-09-10", "2026-12-31", "CRW_SST")
  assert len(urls) == 1 and "(2026-09-10T12:00:00Z):1:(2026-09-13T12:00:00Z)" in urls[0]


def test_slab_urls_refuse_a_window_wholly_outside_the_extent_or_reversed():
  with pytest.raises(ValueError, match="outside dhw_5km's time extent"):
    reader().slab_urls(BBOX, "2026-10-01", "2026-10-05", "CRW_SST")
  with pytest.raises(ValueError, match="starts after it ends"):
    reader().slab_urls(BBOX, "2026-08-05", "2026-08-01", "CRW_SST")


def test_slab_urls_refuse_an_unknown_variable_and_name_the_known_ones():
  with pytest.raises(ValueError, match=r"'nope' is not a variable of dhw_5km; it has \['CRW_SST', 'CRW_BAA'\]"):
    reader().slab_urls(BBOX, "2026-08-01", "2026-08-03", "nope")


def test_slab_urls_carry_the_depth_level_between_time_and_latitude():
  doc = grid(erddap__lat_descending=False, **{"erddap-places__depth": 0.494025})
  url = reader(doc).slab_urls(BBOX, "2026-08-01", "2026-08-01", "CRW_SST")[0]
  assert "%5D%5B(0.494025):1:(0.494025)%5D%5B(20.025):1:(20.075)%5D" in url


def test_slab_urls_use_parquetwmeta_when_the_server_offers_it_and_refuse_a_server_without_parquet():
  assert ".parquetWMeta?" in reader(grid(erddap__formats=["parquetWMeta", "parquet"])).slab_urls(BBOX, "2026-08-01", "2026-08-01", "CRW_SST")[0]
  with pytest.raises(ValueError, match="offers no Parquet"):
    reader(grid(erddap__formats=["csvp", "json"])).slab_urls(BBOX, "2026-08-01", "2026-08-01", "CRW_SST")


def test_slab_is_the_long_table_of_a_2x2x3_parquet_slab_exactly():
  times = ["2026-08-01T12:00:00Z", "2026-08-02T12:00:00Z", "2026-08-03T12:00:00Z"]
  data = slab_parquet(times, [20.075, 20.025], [-155.025, -154.975], "CRW_SST")
  seen = []

  def fetch(url):
    seen.append(url)
    return data

  tab = ErddapGrid(grid(), fetch=fetch, info=INFO).slab(BBOX, "2026-08-01", "2026-08-03", "CRW_SST")
  assert len(seen) == 1 and tab.schema == SLAB_SCHEMA
  assert tab.to_pylist() == [
    {"time": _ts(t), "lat": la, "lon": lo, "value": float(i * 4 + j * 2 + k)}
    for i, t in enumerate(times) for j, la in enumerate([20.075, 20.025]) for k, lo in enumerate([-155.025, -154.975])]


def test_slab_concatenates_the_chunks_in_time_order():
  def fetch(url):
    first = re.search(r"\((\d{4}-\d{2}-\d{2}T12:00:00Z)\)", url).group(1)  # one row a chunk, stamped with its first day
    return slab_parquet([first], [20.075], [-155.025], "CRW_SST", values=[1.0])

  tab = ErddapGrid(grid(), fetch=fetch, info=INFO).slab(BBOX, "2026-01-01", "2026-07-19", "CRW_SST")
  assert [t.strftime("%Y-%m-%d") for t in tab["time"].to_pylist()] == ["2026-01-01", "2026-04-01", "2026-06-30"]


def test_slab_keeps_missing_values_and_ignores_a_depth_column():
  data = slab_parquet(["2026-08-01T12:00:00Z"], [20.075, 20.025], [-155.025], "CRW_SST", values=[float("nan"), 3.5])
  tab = ErddapGrid(grid(), fetch=lambda u: data, info=INFO).slab(BBOX, "2026-08-01", "2026-08-01", "CRW_SST")
  assert math.isnan(tab["value"][0].as_py()) and tab["value"][1].as_py() == 3.5 and tab.column_names == ["time", "lat", "lon", "value"]


def test_slab_names_the_missing_column_and_the_url_when_the_parquet_is_not_the_requested_variable():
  data = slab_parquet(["2026-08-01T12:00:00Z"], [20.075], [-155.025], "OTHER")
  with pytest.raises(ValueError, match=r"lacks column\(s\) \['CRW_SST'\]"):
    ErddapGrid(grid(), fetch=lambda u: data, info=INFO).slab(BBOX, "2026-08-01", "2026-08-01", "CRW_SST")


def test_slice_is_one_time_step_without_the_time_column():
  data = slab_parquet(["2026-09-13T12:00:00Z"], [20.075, 20.025], [-155.025, -154.975], "CRW_SST")
  seen = []

  def fetch(url):
    seen.append(url)
    return data

  tab = ErddapGrid(grid(), fetch=fetch, info=INFO).slice(BBOX, "2026-09-13", "CRW_SST")
  assert tab.schema == SLICE_SCHEMA
  assert tab.to_pylist() == [{"lat": 20.075, "lon": -155.025, "value": 0.0}, {"lat": 20.075, "lon": -154.975, "value": 1.0},
                             {"lat": 20.025, "lon": -155.025, "value": 2.0}, {"lat": 20.025, "lon": -154.975, "value": 3.0}]
  assert "(2026-09-13T12:00:00Z):1:(2026-09-13T12:00:00Z)" in seen[0]  # the extent's end, one step


def test_slab_of_a_tabledap_collection_says_what_is_not_there_yet():
  doc = grid(erddap__protocol="tabledap")
  with pytest.raises(NotImplementedError, match="tabledap"):
    reader(doc).slab(BBOX, "2026-08-01", "2026-08-03", "CRW_SST")


# -- provenance ----------------------------------------------------------------------------------------------------------
def test_source_block_is_the_erddap_keys_of_the_collection_and_nothing_else():
  doc = grid(**{"erddap-places__depth": 0.494025})
  b = reader(doc).source_block()
  assert b == {"erddap:base_url": BASE, "erddap:dataset_id": "dhw_5km", "erddap:protocol": "griddap", "erddap:version": "2.29",
               "erddap:cors": True, "erddap:formats": ["parquet", "csvp", "json"], "erddap:lon_range": [-180, 180],
               "erddap:lat_descending": True, "erddap-places:depth": 0.494025}
