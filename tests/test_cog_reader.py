"""The COG reader on a synthetic 4 x 4 deg GeoTIFF (`fixtures_cog`), and engine.extract() over it. No network.

The engine case by hand: the place is the rectangle lon 0.5..2.5, lat 1..3. Its cells by centre are the pixels at
lat 2.5 and 1.5, lon 0.5, 1.5 and 2.5 (the centres at lon 0.5 and 2.5 lie on its edge, which counts as inside); the
polygon covers half of the lon 0.5 and 2.5 pixels (a meridian split: exactly half of the spherical area) and all of
the lon 1.5 ones. The values are 5, 6, 7 (lat 2.5) and 9, 10, nodata (lat 1.5), so over the five cells with data:
n 5, mean 37 / 5 = 7.4, mean_wt (5 * .5 + 6 + 7 * .5 + 9 * .5 + 10) / 3.5 = 26.5 / 3.5, sd sqrt(17.2 / 4),
min 5, max 10, p10 5.4, p90 9.6, weight_sum 3.5.
"""
import datetime as dt
import math

import numpy as np
import pytest
import shapely
from shapely.geometry import box

from om_extract import readers
from om_extract.engine import STATS_COLUMNS, extract
from om_extract.readers import CogReader, InvalidCollection, InvalidProfile, Reader, SLAB_SCHEMA, UnsupportedCrs
from om_extract.readers.cog import band_of, item_times, resolve_href
from om_extract.values import values_rows
from fixtures_cog import NODATA, VALUES, item, write_tif

REL = 1e-12
PLACE = shapely.to_wkb(box(0.5, 1.0, 2.5, 3.0))


@pytest.fixture
def tif(tmp_path):
  return write_tif(tmp_path / "sst.tif")


def reader(href, **kw) -> CogReader:
  return CogReader(item(href, **{k: v for k, v in kw.items() if k not in ("asset", "fetch", "item_href")}),
                   **{k: v for k, v in kw.items() if k in ("asset", "fetch", "item_href")})


# -- the profile and the interface ---------------------------------------------------------------------------------------
def test_the_cog_reader_satisfies_the_reader_protocol_and_for_collection_dispatches_an_item_to_it(tif):
  r = readers.for_collection(item(tif))
  assert isinstance(r, CogReader) and isinstance(r, Reader) and r.kind == "cog"
  assert readers.kinds()[:3] == ["erddap", "h3", "cog"]


def test_an_item_that_fails_the_cog_profile_raises_with_the_schema_errors(tif):
  doc = item(tif)
  del doc["properties"]["proj:code"]
  with pytest.raises(InvalidProfile, match="not a valid cog") as e:
    CogReader(doc)
  assert isinstance(e.value, InvalidCollection) and any("proj:code" in m for m in e.value.errors)


def test_an_item_in_epsg_3857_raises_a_clear_error_before_any_read(tif):
  with pytest.raises(UnsupportedCrs, match=r"EPSG:3857.*EPSG:4326 rasters only.*display"):
    reader(tif, crs="EPSG:3857")


def test_a_file_in_epsg_3857_behind_an_item_saying_4326_raises_on_the_first_read(tmp_path):
  href = write_tif(tmp_path / "merc.tif", crs="EPSG:3857", x0=0, y0=400000, res=100000)
  r = reader(href)
  with pytest.raises(UnsupportedCrs, match="EPSG:3857"):
    r.axes((0, 0, 1, 1))


# -- axes ------------------------------------------------------------------------------------------------------------------
def test_axes_are_the_pixel_centres_over_the_bbox_with_one_pixel_of_margin_north_first(tif):
  ax = reader(tif).axes((1.2, 1.2, 2.7, 2.2))
  assert ax.lat.tolist() == [3.5, 2.5, 1.5, 0.5] and ax.lon.tolist() == [0.5, 1.5, 2.5, 3.5]
  assert ax.lat_descending and ax.step == (1.0, 1.0) and ax.lon_range == (-180, 180) and ax.source == "cog"
  # a bbox inside one pixel: that pixel and its neighbours, so the mask sees the true spacing
  ax = reader(tif).axes((1.4, 1.4, 1.6, 1.6))
  assert ax.lat.tolist() == [2.5, 1.5, 0.5] and ax.lon.tolist() == [0.5, 1.5, 2.5]


def test_a_bbox_edge_on_a_pixel_edge_takes_only_the_pixels_it_overlaps_before_the_margin(tif):
  ax = reader(tif).axes((0.0, 0.0, 1.0, 1.0))  # the south-west pixel only, then one more on each side it has
  assert ax.lat.tolist() == [1.5, 0.5] and ax.lon.tolist() == [0.5, 1.5]


def test_a_bbox_outside_the_raster_gives_no_pixels_and_no_rows(tif):
  r = reader(tif)
  ax = r.axes((10, 10, 11, 11))
  assert ax.n_cells == 0
  assert r.slab((10, 10, 11, 11), "2020-01-01", "2020-12-31", "sst").num_rows == 0
  assert r.slice((10, 10, 11, 11), "2020-06-01", "sst").num_rows == 0


# -- slab, slice and the band ----------------------------------------------------------------------------------------------
def test_the_slab_is_one_step_at_the_item_datetime_with_nodata_as_null(tif):
  t = reader(tif).slab((0.1, 0.1, 3.9, 3.9), "2020-01-01", "2020-12-31", "sst")
  assert t.schema == SLAB_SCHEMA and t.num_rows == 16
  assert set(t["time"].to_pylist()) == {dt.datetime(2020, 6, 1, tzinfo=dt.timezone.utc)}
  got = {(r["lat"], r["lon"]): r["value"] for r in t.to_pylist()}
  assert got[(3.5, 0.5)] == 1 and got[(0.5, 3.5)] == 16 and got[(1.5, 2.5)] is None
  assert [r["value"] for r in t.to_pylist()][:4] == [1, 2, 3, 4]  # north row first, west to east


def test_a_window_that_misses_the_item_time_gives_zero_rows(tif):
  r = reader(tif)
  assert r.slab((0, 0, 4, 4), "2021-01-01", "2021-12-31", "sst").num_rows == 0
  assert r.slice((0, 0, 4, 4), "2020-06-02", "sst").num_rows == 0
  assert r.slice((0, 0, 4, 4), "2020-06-01", "sst").num_rows == 16


def test_an_item_with_a_start_and_end_is_one_step_at_its_start_covered_by_any_overlapping_window(tif):
  r = reader(tif, start="1991-07-01T00:00:00Z", end="2018-12-31T23:59:59Z")
  assert item_times(r.item) == ("1991-07-01T00:00:00Z", "2018-12-31T23:59:59Z", "1991-07-01T00:00:00Z")
  t = r.slab((1, 1, 2, 2), "2018-12-31", "2026-01-01", "sst")
  assert set(t["time"].to_pylist()) == {dt.datetime(1991, 7, 1, tzinfo=dt.timezone.utc)}
  assert r.slab((1, 1, 2, 2), "2019-01-01", "2026-01-01", "sst").num_rows == 0
  assert (r.extent().time_start, r.extent().time_end, r.extent().bbox) == \
         ("1991-07-01T00:00:00Z", "2018-12-31T23:59:59Z", (0.0, 0.0, 4.0, 4.0))


def test_a_timeless_item_is_in_every_window_with_a_null_time(tif):
  r = reader(tif, datetime=None)
  t = r.slab((1, 1, 2, 2), "1900-01-01", "1900-01-02", "sst")
  assert t.num_rows > 0 and set(t["time"].to_pylist()) == {None}


def test_the_band_is_a_name_a_number_or_the_assets_atlas_bidx(tmp_path):
  href = write_tif(tmp_path / "two.tif", bands=2)
  bands = [{"name": n, "data_type": "float32", "nodata": NODATA, "statistics": {"minimum": 0, "maximum": 1}}
           for n in ("density", "cv")]
  r = reader(href, bands=bands, bidx=2)
  cell = (2.1, 2.1, 2.9, 2.9)  # the pixel at lat 2.5, lon 2.5 (value 7) and its neighbours
  def v(var):
    return {(x["lat"], x["lon"]): x["value"] for x in r.slice(cell, None, var).to_pylist()}[(2.5, 2.5)]
  assert (v("density"), v("cv"), v(1), v("2"), v(None)) == (7, 107, 7, 107, 107)
  with pytest.raises(ValueError, match=r"'sst' is not a band.*\['density', 'cv'\]"):
    band_of(r.asset, "sst")
  r.slice(cell, None, "density")
  assert r.source_block()["cog:band"] == "density" and r.source_block("cv")["cog:bidx"] == 2


def test_a_nan_nodata_band_masks_nan_pixels_to_null(tmp_path):
  vals = VALUES.copy()
  vals[vals == NODATA] = np.nan
  href = write_tif(tmp_path / "nan.tif", vals, nodata=np.nan)
  bands = [{"name": "sst", "data_type": "float32", "nodata": "nan", "statistics": {"minimum": 1, "maximum": 16}}]
  got = {(x["lat"], x["lon"]): x["value"] for x in reader(href, bands=bands).slice((2, 1, 3, 2), None, "sst").to_pylist()}
  assert got[(1.5, 2.5)] is None and got[(1.5, 1.5)] == 10


def test_a_band_scale_and_offset_are_applied(tif):
  bands = [{"name": "sst", "data_type": "float32", "nodata": NODATA, "raster:scale": 0.5, "raster:offset": 10,
            "statistics": {"minimum": 1, "maximum": 16}}]
  got = {(x["lat"], x["lon"]): x["value"] for x in reader(tif, bands=bands).slice((0, 3, 1, 4), None, "sst").to_pylist()}
  assert got[(3.5, 0.5)] == 10.5 and got[(2.5, 0.5)] == 12.5


def test_a_relative_href_resolves_against_where_the_item_was_read(tmp_path, tif):
  assert resolve_href("./sst.tif", {}, tmp_path / "item.json") == str(tmp_path / "sst.tif")
  assert resolve_href("sst.tif", {"links": [{"rel": "self", "href": "https://x.org/a/item.json"}]}) == \
         "https://x.org/a/sst.tif"
  assert resolve_href("https://y.org/b.tif", {}, tmp_path) == "https://y.org/b.tif"
  r = CogReader(item("./sst.tif"), item_href=tmp_path / "item.json")
  assert r.href == tif and r.axes((1, 1, 2, 2)).n_cells == 9


def test_the_source_block_names_the_href_band_nodata_and_crs(tif):
  assert reader(tif).source_block("sst") == {
    "cog:href": tif, "cog:item": "synthetic_cog", "cog:asset": "cog", "cog:bidx": 1, "cog:band": "sst",
    "cog:nodata": NODATA, "proj:code": "EPSG:4326"}


# -- the engine end to end -----------------------------------------------------------------------------------------------
def test_engine_extract_takes_a_cog_item_and_gives_the_hand_computed_stats_row(tif):
  x = extract(item(tif), "TEST:rect", PLACE, "sst", "2020-01-01", "2020-12-31")
  cells = sorted((r["latitude"], r["longitude"], r["weight"]) for r in x.mask.to_pylist())
  assert [(la, lo) for la, lo, _ in cells] == [(1.5, 0.5), (1.5, 1.5), (1.5, 2.5), (2.5, 0.5), (2.5, 1.5), (2.5, 2.5)]
  assert [w for *_, w in cells] == pytest.approx([0.5, 1, 0.5, 0.5, 1, 0.5], rel=REL)
  assert x.stats.column_names == list(STATS_COLUMNS) and x.stats.num_rows == 1
  r = x.stats.to_pylist()[0]
  assert (r["date"], r["n"], r["mean"], r["min"], r["max"]) == (dt.date(2020, 6, 1), 5, 7.4, 5, 10)
  for k, want in (("mean_wt", 26.5 / 3.5), ("sd", math.sqrt(17.2 / 4)), ("weight_sum", 3.5), ("p10", 5.4),
                  ("p90", 9.6)):
    assert r[k] == pytest.approx(want, rel=REL), k
  assert (r["place_id"], r["dataset_id"], r["variable"]) == ("TEST:rect", "synthetic_cog", "sst")
  assert x.urls == [tif] and x.source["cog:band"] == "sst" and x.dataset.protocol == "cog"


def test_engine_extract_takes_a_reader_object_as_well(tif):
  x = extract(reader(tif), "TEST:rect", PLACE, "sst", "2020-01-01", "2020-12-31")
  assert x.stats["mean_wt"].to_pylist() == pytest.approx([26.5 / 3.5], rel=REL)


def test_engine_extract_of_a_cog_has_no_chunk_store_and_raises_before_any_read(tif, tmp_path):
  # merge of readers into the chunked engine: a COG never reaches extract_chunked()
  with pytest.raises(ValueError, match="chunk store"):
    extract(item(tif), "TEST:rect", PLACE, "sst", "2020-01-01", "2020-12-31", chunks=tmp_path / "chunks")
  assert not (tmp_path / "chunks").exists()


def test_engine_extract_of_a_cog_needs_t0_and_t1(tif):
  # t0/t1 became optional for the chunked ERDDAP path; the reader path still needs both
  with pytest.raises(ValueError, match="t0 and t1 are required for a COG"):
    extract(item(tif), "TEST:rect", PLACE, "sst")
  with pytest.raises(ValueError, match="t0 and t1 are required for a COG"):
    extract(reader(tif), "TEST:rect", PLACE, "sst", "2020-01-01")


def test_engine_extract_over_a_place_outside_the_raster_or_the_window_gives_zero_rows(tif):
  far = shapely.to_wkb(box(20, 20, 21, 21))
  x = extract(item(tif), "TEST:far", far, "sst", "2020-01-01", "2020-12-31")
  assert (x.stats.num_rows, x.mask_cells) == (0, 0) and x.stats.column_names == list(STATS_COLUMNS)
  assert extract(item(tif), "TEST:rect", PLACE, "sst", "2021-01-01", "2021-12-31").stats.num_rows == 0


def test_values_rows_of_a_cog_extraction_say_what_they_wait_on(tif):
  x = extract(item(tif), "TEST:rect", PLACE, "sst", "2020-01-01", "2020-12-31")
  with pytest.raises(NotImplementedError, match="atlas indicators"):
    values_rows(x)
