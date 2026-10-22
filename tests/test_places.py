"""om_extract.places: one WKB row of a GeoParquet places file, read by place_id (a synthetic two-row file in a tmp dir)."""
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely.geometry import box

from om_extract.places import PlaceNotFound, place_collection, place_geometry, place_ids, places_href

A = box(10, 20, 11, 21).wkb
B = box(-180, 0, -179, 1).wkb


def write_places(base, coll="places", ids=("NMS:AAA", "NMS:BBB"), geoms=(A, B), row_group_size=None):
  d = base / coll
  d.mkdir(parents=True)
  geo = {"version": "1.1.0", "primary_column": "geometry",
         "columns": {"geometry": {"encoding": "WKB", "geometry_types": ["Polygon"]}}}
  t = pa.table({"place_id": list(ids), "name": [i.split(":")[1] for i in ids], "geometry": pa.array(geoms, pa.binary())})
  t = t.replace_schema_metadata({"geo": json.dumps(geo)})
  pq.write_table(t, d / "places.parquet", row_group_size=row_group_size)


def test_one_place_is_its_stored_wkb(tmp_path):
  write_places(tmp_path)
  assert place_geometry("NMS:AAA", "places", tmp_path) == A
  assert place_geometry("NMS:BBB", "places", tmp_path) == B


def test_one_row_group_per_place_still_finds_each(tmp_path):
  write_places(tmp_path, row_group_size=1)
  assert pq.ParquetFile(tmp_path / "places" / "places.parquet").metadata.num_row_groups == 2
  assert place_geometry("NMS:BBB", "places", tmp_path) == B


def test_an_unknown_place_raises(tmp_path):
  write_places(tmp_path)
  with pytest.raises(PlaceNotFound, match="no place NMS:ZZZ in the places collection"):
    place_geometry("NMS:ZZZ", "places", tmp_path)


def test_the_ids_of_a_collection_by_prefix(tmp_path):
  write_places(tmp_path, ids=("NMS:BBB", "PSGID:1"), geoms=(A, B))
  assert place_ids("places", tmp_path, "NMS:") == ["NMS:BBB"]
  assert place_ids("places", tmp_path) == ["NMS:BBB", "PSGID:1"]


def test_the_href_is_base_coll_places_parquet():
  assert places_href("places", "https://storage.oceanmetrics.io/gazetteer/") == \
    "https://storage.oceanmetrics.io/gazetteer/places/places.parquet"


def test_the_index_names_a_place_collection(tmp_path):
  (tmp_path / "index").mkdir()
  pq.write_table(pa.table({"place_id": ["NMS:AAA", "MRGID:1"], "collection": ["places", "mr_eez"]}),
                 tmp_path / "index" / "places_index.parquet")
  assert place_collection("MRGID:1", tmp_path) == "mr_eez"
  with pytest.raises(PlaceNotFound):
    place_collection("MRGID:2", tmp_path)
