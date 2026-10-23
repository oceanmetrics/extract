"""The Copernicus Marine reader: windows, the cell-centre mean, the rows, the reader interface, the provenance and
`om-extract run --reader copernicus`. One test per rule on synthetic grids whose values name their cells (ported with
the rules from indicators' place producer); a small xarray DataArray stands in for the Toolbox subset, so no test needs
the Toolbox or a login."""
import json
import math
import types

import numpy as np
import pytest
import shapely

from fixtures_copernicus import DATASET, INFO, NO3, box, date, grid, months, xr
from om_extract import contract
from om_extract.readers import copernicus as cm

TWO_CELLS = box(0.5, 0.5, 2.5, 1.5)        # the centres (lon 1 and 2, lat 1): the cells 11 and 12 of month 0


def reader(arrays=None, **kw):
  arrays = arrays if arrays is not None else {}
  return cm.CopernicusReader(DATASET, "no3", info=INFO, version="2.5.0",
                             subset=kw.pop("subset", None) or (lambda ds, v, w, s, e: arrays.get(w, grid(3, depth=0.5057))), **kw)


# ---- windows ----

def test_one_window_is_the_bbox_grown_by_a_cell_and_clamped_to_the_dataset():
  assert cm.windows(box(10, 40, 12, 42), 0.25, 0.25) == [(9.75, 39.75, 12.25, 42.25)]
  assert cm.windows(box(-179.9, -79.9, -178, -70), 0.25, 0.25, (-180, 179.75), (-80, 90)) == [(-180, -80, -177.75, -69.75)]


def test_a_place_across_the_antimeridian_gets_one_window_per_side():
  fiji = shapely.MultiPolygon([box(177.0, -20.0, 180.0, -15.0), box(-180.0, -19.0, -178.0, -16.0)])
  east, west = cm.windows(fiji, 0.25, 0.25, (-180, 179.75), (-80, 90))
  assert east == (176.75, -20.25, 179.75, -14.75)
  assert west == (-180, -19.25, -177.75, -15.75)


def test_a_multipolygon_inside_one_hemisphere_is_one_window():
  assert len(cm.windows(shapely.MultiPolygon([box(-10, 0, -9, 1), box(5, 2, 6, 3)]), 0.25, 0.25)) == 1


def test_window_cells_counts_the_cells_the_toolbox_returns():
  assert cm.window_cells((9.75, 39.75, 12.25, 42.25), 0.25, 0.25) == 11 * 11


# ---- the mean over the cells under the polygon ----

def test_the_mean_is_over_the_cells_whose_centres_are_inside_exact_values():
  s = cm.cell_means([grid()], TWO_CELLS)
  assert s.cells == 2 and s.n_cells == [2, 2, 2]
  assert s.times == [date(2020, 1), date(2020, 2), date(2020, 3)]
  assert s.mean == [11.5, 111.5, 211.5]
  assert s.sd == [pytest.approx(math.sqrt(0.5))] * 3         # sample SD of 11 and 12


def test_a_cell_whose_centre_is_outside_is_not_counted_however_much_of_it_overlaps():
  s = cm.cell_means([grid()], box(1.2, 0.2, 2.4, 1.4))
  assert s.cells == 1 and s.mean == [12.0, 112.0, 212.0] and s.sd == [None, None, None]


def test_nan_cells_are_skipped_and_a_single_cell_has_no_sd():
  a = grid()
  a[0, 1, 1] = np.nan
  s = cm.cell_means([a], TWO_CELLS)
  assert s.mean == [12.0, 111.5, 211.5] and s.n_cells == [1, 2, 2]
  assert s.sd[0] is None and s.sd[1] == pytest.approx(math.sqrt(0.5))


def test_a_month_with_every_cell_nan_has_no_mean():
  a = grid()
  a[1, :, :] = np.nan
  s = cm.cell_means([a], TWO_CELLS)
  assert s.mean == [11.5, None, 211.5] and s.sd[1] is None and s.n_cells == [2, 0, 2]


def test_a_polygon_with_no_cell_centre_inside_is_too_small_for_the_grid():
  with pytest.raises(cm.NoCellsError, match="too small for this grid"):
    cm.cell_means([grid()], box(0.1, 0.1, 0.4, 0.4))


def test_cells_that_are_all_masked_are_not_a_mean_of_nothing():
  with pytest.raises(cm.NoCellsError, match="2 cell centres inside the place polygon have no value"):
    cm.cell_means([grid() * np.nan], TWO_CELLS)


def test_a_cell_centred_on_the_polygon_boundary_counts():
  s = cm.cell_means([grid()], box(1.0, 0.5, 2.0, 1.5))
  assert s.cells == 2 and s.mean == [11.5, 111.5, 211.5]


def test_two_windows_are_one_set_of_cells_and_overlap_counts_once():
  a = grid(lon=(0.0, 1.0, 2.0, 3.0))
  b = grid(lon=(2.0, 3.0, 4.0, 5.0))
  b = b.copy(data=a.values[:, :, :1].repeat(4, axis=2))       # every cell of b reads lon index 0
  s = cm.cell_means([a, b], box(0.5, 0.5, 4.5, 1.5))          # centres lon 1, 2, 3 (a) and 2, 3, 4 (b), lat 1
  assert s.cells == 4                                         # lon 2 and 3 are seen twice, counted once (a wins)
  assert s.mean[0] == pytest.approx((11 + 12 + 13 + 10) / 4)


def test_a_cell_centred_on_minus_180_is_inside_the_western_part_of_a_split_place():
  s = cm.cell_means([grid(lon=(-180.0, -179.0, -178.0, -177.0))], box(-180.0, 0.5, -178.5, 1.5))
  assert s.cells == 2 and s.mean[0] == 10.5


def test_windows_with_different_time_steps_are_refused():
  with pytest.raises(ValueError, match="different time steps"):
    cm.cell_means([grid(3), grid(2)], TWO_CELLS)


def test_the_surface_is_the_shallowest_depth_level():
  t = np.array(months(1), dtype="datetime64[ns]")
  da = xr.DataArray(np.array([[[[1.0]], [[2.0]], [[3.0]]]]), dims=("time", "depth", "latitude", "longitude"),
                    coords={"time": t, "depth": [30.0, 0.5, 5.0], "latitude": [0.0], "longitude": [0.0]})
  s = cm.surface(da)
  assert float(s["depth"]) == 0.5 and s.values.tolist() == [[[2.0]]]


# ---- rows ----

def test_three_months_are_three_month_rows_through_the_contract():
  rows = cm.place_rows("MRGID:5670", "mr_eez", NO3, "mmol m-3", cm.cell_means([grid()], TWO_CELLS))
  assert [(r["time_grain"], r["time"], r["value"], r["n"]) for r in rows] == [
    ("month", date(2020, 1), 11.5, 1.0), ("month", date(2020, 2), 111.5, 1.0), ("month", date(2020, 3), 211.5, 1.0)]
  assert {(r["place_id"], r["coll"], r["indicator"], r["unit"], r["class"]) for r in rows} == {
    ("MRGID:5670", "mr_eez", NO3, "mmol m-3", None)}
  assert [r["sd"] for r in rows] == [pytest.approx(math.sqrt(0.5))] * 3
  assert contract.validate_rows(rows) == []


def test_a_complete_year_adds_a_year_row_that_is_the_mean_of_its_twelve_months():
  rows = cm.place_rows("MRGID:5670", "mr_eez", NO3, "mmol m-3", cm.cell_means([grid(14)], TWO_CELLS))
  years = [r for r in rows if r["time_grain"] == "year"]
  assert [(r["time"], r["n"], r["sd"]) for r in years] == [(date(2020, 1, 1), 12.0, None)]
  assert years[0]["value"] == sum(100.0 * k + 11.5 for k in range(12)) / 12 == 561.5
  assert len([r for r in rows if r["time_grain"] == "month"]) == 14
  assert contract.validate_rows(rows) == []


def test_a_month_with_no_value_has_no_row_and_blocks_its_year():
  a = grid(12)
  a[5, :, :] = np.nan
  rows = cm.place_rows("MRGID:5670", "mr_eez", NO3, "mmol m-3", cm.cell_means([a], TWO_CELLS))
  assert len(rows) == 11 and {r["time_grain"] for r in rows} == {"month"}


# ---- the reader interface ----

def test_the_reader_is_a_reader_and_asks_the_toolbox_once_per_window():
  from om_extract.readers import Reader
  asked = []
  r = reader(subset=lambda ds, v, w, s, e: asked.append((ds, v, w, s, e)) or grid(3))
  assert isinstance(r, Reader) and r.kind == "copernicus"
  r.subset((0, 0, 3, 3), "2020-01-01", None)
  r.slab((0, 0, 3, 3), "2020-01-01", None)
  assert asked == [(DATASET, "no3", (0, 0, 3, 3), "2020-01-01", None)]


def test_the_slab_is_the_long_table_of_the_subset_with_nan_as_null():
  a = grid(2, lon=(0.0, 1.0), lat=(0.0, 1.0))
  a[1, 0, 1] = np.nan
  t = reader({(0, 0, 1, 1): a}).slab((0, 0, 1, 1), None, None).to_pylist()
  assert [(r["time"].date().isoformat(), r["lat"], r["lon"], r["value"]) for r in t] == [
    ("2020-01-01", 0.0, 0.0, 0.0), ("2020-01-01", 0.0, 1.0, 1.0), ("2020-01-01", 1.0, 0.0, 10.0), ("2020-01-01", 1.0, 1.0, 11.0),
    ("2020-02-01", 0.0, 0.0, 100.0), ("2020-02-01", 0.0, 1.0, None), ("2020-02-01", 1.0, 0.0, 110.0), ("2020-02-01", 1.0, 1.0, 111.0)]
  assert str(reader().slab((0, 0, 1, 1), None, None).schema.field("time").type) == "timestamp[us, tz=UTC]"


def test_the_axes_are_the_catalogue_lattice_inside_the_bbox():
  ax = reader().axes((0.5, -0.5, 3.0, 1.2))
  assert ax.lon.tolist() == [1.0, 2.0, 3.0] and ax.lat.tolist() == [0.0, 1.0] and ax.step == (1.0, 1.0)
  assert reader().axes((178.2, 0, 181, 0)).lon.tolist() == [179.0]          # clamped to the dataset's range


def test_the_source_block_names_the_dataset_its_version_and_the_toolbox():
  assert reader().source_block() == {
    "copernicus:dataset_id": DATASET, "copernicus:dataset_version": "202406",
    "copernicus:product_id": "GLOBAL_MULTIYEAR_BGC_001_029", "copernicus:variable": "no3", "copernicus:depth": [0.0, 0.51],
    "copernicus:toolbox": "2.5.0"}


# ---- one place ----

def test_extract_place_gives_the_windows_the_size_and_the_rows():
  x = cm.extract_place(reader(), "MRGID:5670", TWO_CELLS, "mr_eez")
  assert x.indicator == NO3 and x.windows == [(-0.5, -0.5, 3.5, 2.5)]
  assert x.grid_cells == 5 * 4 and x.grid_values == 5 * 4 * 3 and x.series.cells == 2
  assert [r["value"] for r in x.rows] == [11.5, 111.5, 211.5] and x.unit == "mmol m-3" and x.depth == 0.5057


def test_a_check_run_resolves_the_job_and_reads_nothing():
  x = cm.extract_place(reader(subset=lambda *a: pytest.fail("the Toolbox was asked")), "MRGID:5670", TWO_CELLS, "mr_eez",
                       check=True)
  assert x.grid_values == 60 and x.rows == [] and x.series is None


def test_a_job_over_the_limit_is_refused_before_any_download():
  with pytest.raises(ValueError, match="about 60 grid values .* is over the 5 limit"):
    cm.extract_place(reader(subset=lambda *a: pytest.fail("the Toolbox was asked")), "MRGID:5670", TWO_CELLS, "mr_eez",
                     max_values=5)


def test_the_unit_comes_from_the_data_else_the_catalogue():
  x = cm.extract_place(reader(subset=lambda *a: grid(3, units="")), "MRGID:5670", TWO_CELLS, "mr_eez")
  assert x.unit == "mmol m-3"
  r = cm.CopernicusReader(DATASET, "no3", info={**INFO, "units": None}, version="2.5.0", subset=lambda *a: grid(3, units=""))
  with pytest.raises(ValueError, match="no units"):
    cm.extract_place(r, "MRGID:5670", TWO_CELLS, "mr_eez")


# ---- provenance and the values file ----

def test_the_provenance_states_the_product_and_the_dataset_id_and_version_as_the_source():
  r = reader()
  x = cm.extract_place(r, "MRGID:5670", TWO_CELLS, "mr_eez")
  emb, side = cm.build_provenance(x, r, {"kind": "published", "url": "https://g.example/mr_eez/places.parquet"},
                                  created="2026-10-23T00:00:00Z")
  assert emb["dataset"]["id"] == "GLOBAL_MULTIYEAR_BGC_001_029" and emb["dataset"]["doi"] == "https://doi.org/10.48670/moi-00019"
  assert emb["dataset"]["citation"] == ("E.U. Copernicus Marine Service Information; Global Ocean Biogeochemistry Hindcast; "
                                        "https://doi.org/10.48670/moi-00019")
  assert emb["dataset"]["providers"][1] == {"name": "oceanmetrics/extract", "roles": ["processor"], "url": cm.EXTRACT_URL}
  data, product, polygon = emb["sources"]
  assert (data["href"], data["title"], data["role"], data["end_datetime"]) == (
    f"copernicusmarine:{DATASET}", f"{DATASET} 202406 no3, Copernicus Marine Toolbox 2.5.0", "data", "2020-03-01")
  assert product["href"].endswith("/product/GLOBAL_MULTIYEAR_BGC_001_029/description") and polygon["role"] == "geometry"
  assert emb["source_version"] == f"{DATASET} 202406 2020-01-01 to 2020-03-01"
  assert emb["code"]["adapter"] == "om_extract.readers.copernicus" and emb["method"] == cm.METHOD
  assert side["depth"] == {"requested": [0.0, 0.51], "used_m": 0.5057} and side["cells"] == 2 and side["rows"] == 3


def test_run_writes_a_valid_values_file_and_its_sidecar(tmp_path):
  res = cm.run(DATASET, "no3", "MRGID:5670", shapely.to_wkb(TWO_CELLS), tmp_path, coll="mr_eez", reader=reader(),
               created="2026-10-23T00:00:00Z")
  assert res["values"] == contract.values_path(tmp_path, NO3, "mr_eez") and res["rows"] == 3
  assert contract.validate_file(res["values"]) == []
  table, prov = contract.read_values(res["values"])
  assert table.column("value").to_pylist() == [11.5, 111.5, 211.5] and prov["dataset"]["id"] == "GLOBAL_MULTIYEAR_BGC_001_029"
  side = json.loads(res["provenance"].read_text())
  assert side["indicator"] == NO3 and side["dataset_version"] == "202406" and side["windows"] == [[-0.5, -0.5, 3.5, 2.5]]


def test_a_geojson_feature_collection_is_one_geometry():
  fc = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": shapely.geometry.mapping(box(0, 0, 1, 1))},
                                                  {"type": "Feature", "geometry": shapely.geometry.mapping(box(2, 0, 3, 1))}]}
  assert cm.to_shapely(fc).area == 2.0 and cm.to_shapely(fc["features"][0]).area == 1.0


def test_om_extract_run_reader_copernicus_on_the_synthetic_subset(tmp_path, monkeypatch, capsys):
  from om_extract.cli import main
  monkeypatch.setattr(cm, "dataset_info", lambda ds, v, dois=None: INFO)
  monkeypatch.setattr(cm, "toolbox_subset", lambda ds, v, w, s, e, workdir, name: grid(3, depth=0.5057))
  monkeypatch.setattr(cm, "toolbox_version", lambda: "2.5.0")
  (tmp_path / "place.geojson").write_text(json.dumps(shapely.geometry.mapping(TWO_CELLS)))
  rc = main(["run", "--reader", "copernicus", "--dataset", DATASET, "--var", "no3", "--place", "MRGID:5670",
             "--coll", "mr_eez", "--geometry", str(tmp_path / "place.geojson"), "--out", str(tmp_path / "out")])
  out = capsys.readouterr().out
  assert rc == 0, out
  path = contract.values_path(tmp_path / "out", NO3, "mr_eez")
  assert f"wrote {path} (3 rows, {NO3}, coll mr_eez)" in out and "2 cell centres inside" in out
  _, prov = contract.read_values(path)
  assert prov["sources"][2]["href"] == "file:place.geojson"


def test_om_extract_run_reader_copernicus_needs_a_dataset(tmp_path, capsys):
  from om_extract.cli import main
  assert main(["run", "--reader", "copernicus", "--var", "no3", "--place", "MRGID:1", "--out", str(tmp_path)]) == 1
  assert "needs --dataset" in capsys.readouterr().err


def test_the_toolbox_is_asked_for_the_window_and_the_surface_and_its_netcdf_is_read_back(tmp_path, monkeypatch):
  cmar = pytest.importorskip("copernicusmarine")              # the `cmems` extra
  asked = {}

  def fake_subset(**kw):
    asked.update(kw)
    t = np.array(months(2), dtype="datetime64[ns]")
    da = xr.DataArray(np.array([[[[9.0]], [[1.0]]], [[[8.0]], [[2.0]]]]), dims=("time", "depth", "latitude", "longitude"),
                      coords={"time": t, "depth": [40.0, 0.5], "latitude": [1.0], "longitude": [1.0]}, name="no3")
    path = tmp_path / kw["output_filename"]
    da.to_dataset().to_netcdf(path)
    return types.SimpleNamespace(file_path=path)
  monkeypatch.setattr(cmar, "subset", fake_subset)
  da = cm.toolbox_subset("cmems_x", "no3", (-0.5, -0.5, 3.5, 2.5), "2020-01-01", None, tmp_path, "w0")
  assert (asked["minimum_depth"], asked["maximum_depth"]) == (0.0, 0.51) and da.values.ravel().tolist() == [1.0, 2.0]
