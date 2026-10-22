"""`om-extract run` on the engine fixture, offline through --slab-dir. No network."""
import hashlib
import json

import pyarrow.parquet as pq

from om_extract import contract
from om_extract.cli import main
from om_extract.run import read_geometry, slab_dir_fetch, slab_file
from fixtures_engine import am_value, collection, served, square_value, wkb

STATS_COLUMNS = ["date", "n", "mean", "mean_wt", "sd", "min", "max", "p10", "p90", "weight_sum",
                 "place_id", "dataset_id", "variable"]


def setup(tmp_path, place="square_am", var="sst"):
  """collection.json, the place's WKB and a slab directory with one `<sha1(url)>.parquet` per request (no info table:
  the extent comes from the catalog)."""
  (tmp_path / "collection.json").write_text(json.dumps(collection()))
  (tmp_path / "place.wkb").write_bytes(wkb(place))
  slabs = tmp_path / "slabs"
  slabs.mkdir()
  for u, data in served(var, am_value if place == "square_am" else square_value, place, info=False).items():
    (slabs / slab_file(u)).write_bytes(data)
  return tmp_path


def argv(d, *extra):
  return ["run", "--collection", str(d / "collection.json"), "--place", "NMS:AM", "--geometry", str(d / "place.wkb"),
          "--var", "sst", "--t0", "2026-01-01", "--t1", "2026-01-03", "--out", str(d / "out"),
          "--slab-dir", str(d / "slabs"), *extra]


def test_run_writes_stats_provenance_and_a_valid_values_file(tmp_path, capsys):
  d = setup(tmp_path)
  assert main(argv(d)) == 0
  out = capsys.readouterr().out
  values = d / "out" / "values" / "indicator=erddap:sst_deg1:sst:mean" / "coll=places" / "data.parquet"
  assert f"wrote {values} (3 rows, erddap:sst_deg1:sst:mean, coll places)" in out
  assert contract.validate_file(values) == []
  table, prov = contract.read_values(values)
  assert table.num_rows == 3 and prov["rows"] == 3 and prov["code"]["adapter"] == "om_extract.engine"
  assert [s["role"] for s in prov["sources"]] == ["data", "data", "product"]
  assert prov["sources"][-1]["href"] == str(d / "collection.json")


def test_run_stats_parquet_has_the_erddap_places_columns(tmp_path):
  d = setup(tmp_path)
  assert main(argv(d)) == 0
  t = pq.read_table(d / "out" / "stats.parquet")
  assert t.column_names == STATS_COLUMNS
  assert t.num_rows == 3 and set(t["place_id"].to_pylist()) == {"NMS:AM"}


def test_run_provenance_json_has_the_requests_mask_and_file_size(tmp_path):
  d = setup(tmp_path)
  assert main(argv(d)) == 0
  p = json.loads((d / "out" / "provenance.json").read_text())
  assert (p["lobes"], p["mask_cells"], p["griddap_requests"], p["rows"]) == (2, 8, 2, 3)
  assert p["bytes"] == (d / "out" / "stats.parquet").stat().st_size
  assert all((d / "slabs" / slab_file(u)).is_file() for u in p["griddap_urls"])


def test_run_stat_and_coll_options(tmp_path):
  d = setup(tmp_path)
  assert main(argv(d, "--stat", "max", "--coll", "mr_eez")) == 0
  values = d / "out" / "values" / "indicator=erddap:sst_deg1:sst:max" / "coll=mr_eez" / "data.parquet"
  t, _ = contract.read_values(values)
  assert t["value"].to_pylist() == [8.0, 18.0, 28.0] and set(t["sd"].to_pylist()) == {None}


def test_run_exits_1_with_the_message_on_a_contract_error(tmp_path, capsys):
  d = setup(tmp_path)
  assert main(argv(d, "--coll", "Not-A-Slug")) == 1
  err = capsys.readouterr().err
  assert "error: " in err and "coll" in err and "Not-A-Slug" in err
  assert not (d / "out" / "values").exists()


def test_run_exits_1_naming_the_url_when_a_slab_file_is_missing(tmp_path, capsys):
  d = setup(tmp_path)
  for f in (d / "slabs").iterdir():
    f.unlink()
  assert main(argv(d)) == 1
  err = capsys.readouterr().err
  assert "error: no file" in err and "griddap/sst_deg1.parquet?sst" in err


def test_slab_file_is_the_sha1_of_the_url_and_the_fetch_reads_it(tmp_path):
  u = "https://erddap.example.org/erddap/griddap/x.parquet?v%5B(1):1:(2)%5D"
  assert slab_file(u) == hashlib.sha1(u.encode()).hexdigest() + ".parquet"
  (tmp_path / slab_file(u)).write_bytes(b"abc")
  assert slab_dir_fetch(tmp_path)(u) == b"abc"


def test_read_geometry_takes_geojson_or_wkb(tmp_path):
  (tmp_path / "p.geojson").write_text('{"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}')
  (tmp_path / "p.wkb").write_bytes(wkb("square"))
  assert read_geometry(tmp_path / "p.geojson")["type"] == "Polygon"
  assert read_geometry(tmp_path / "p.wkb") == wkb("square")


def test_run_takes_a_geojson_geometry(tmp_path):
  d = setup(tmp_path, place="square")
  feature = {"type": "Feature", "properties": {},
             "geometry": {"type": "Polygon", "coordinates": [[[10.3, 20.2], [12.6, 20.2], [12.6, 21.7], [10.3, 21.7],
                                                              [10.3, 20.2]]]}}
  (d / "place.geojson").write_text(json.dumps(feature))
  args = argv(d)
  args[args.index("--geometry") + 1] = str(d / "place.geojson")
  assert main(args) == 0
  t = pq.read_table(d / "out" / "stats.parquet")
  assert t["n"].to_pylist() == [6, 5, 6]
