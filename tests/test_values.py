"""om_extract.values: an Extraction as contract rows and provenance. Rows and provenance validate with the contract."""
import dataclasses
import datetime as dt

import pyarrow as pa
import pytest

from om_extract import contract
from om_extract.engine import extract
from om_extract.values import (COARSER, METHOD, indicator_id, native_grain, provenance_dataset, provenance_for,
                               step_days_of, values_rows)
from fixtures_engine import SQUARE_URL, collection, fetch_from, served, square_class, square_value, wkb

T0, T1 = "2026-01-01", "2026-01-03"


def ex(var="sst", value=square_value, place_id="TEST:square", **kw):
  return extract(kw.pop("doc", None) or collection(), place_id, wkb("square"), var, T0, T1,
                 fetch=fetch_from(served(var, value)), **kw)


def full_prov(rows, prov):
  """the provenance as write_values() completes it."""
  return {"indicator": rows[0]["indicator"], "coll": rows[0]["coll"], **prov, "contract": contract.CONTRACT_VERSION,
          "rows": len(rows)}


# -- rows ----------------------------------------------------------------------------------------------------------------
def test_continuous_rows_validate_and_are_one_a_day_at_the_day_grain():
  x = ex()
  rows = values_rows(x)
  assert contract.validate_rows(rows) == []
  assert [r["time"] for r in rows] == [dt.date(2026, 1, d) for d in (1, 2, 3)]
  assert {(r["time_grain"], r["class"], r["unit"], r["coll"], r["place_id"]) for r in rows} == {
    ("day", None, "degree_C", "test", "TEST:square")}


def test_the_indicator_id_is_erddap_dataset_variable_stat():
  assert {r["indicator"] for r in values_rows(ex())} == {"erddap:sst_deg1:sst:mean_wt"}
  assert {r["indicator"] for r in values_rows(ex(), stat="p90")} == {"erddap:sst_deg1:sst:p90"}
  assert indicator_id("sst_deg1", "cls", "mean_wt", categorical=True) == "erddap:sst_deg1:cls:share"


def test_value_is_the_stat_column_and_n_the_cells_with_data():
  x = ex()
  st = x.stats.to_pylist()
  rows = values_rows(x)
  assert [r["value"] for r in rows] == [s["mean_wt"] for s in st]
  assert [r["n"] for r in rows] == [6.0, 5.0, 6.0]
  assert [r["value"] for r in values_rows(x, stat="mean")] == [3.5, 13.0, 8.0]
  assert [r["value"] for r in values_rows(x, stat="min")] == [1.0, 11.0, 8.0]


def test_sd_is_the_cell_sd_for_mean_wt_and_mean_and_null_for_other_stats():
  x = ex()
  sds = [s["sd"] for s in x.stats.to_pylist()]
  assert [r["sd"] for r in values_rows(x)] == sds
  assert [r["sd"] for r in values_rows(x, stat="mean")] == sds
  for stat in ("sd", "min", "max", "p10", "p90"):
    assert {r["sd"] for r in values_rows(x, stat=stat)} == {None}


def test_a_non_finite_value_is_null_and_so_is_its_sd():
  x = ex()
  st = x.stats.set_column(3, "mean_wt", pa.array([float("nan"), 1.0, float("inf")], pa.float64()))
  rows = values_rows(dataclasses.replace(x, stats=st))
  assert [(r["value"], r["sd"] is None) for r in rows] == [(None, True), (1.0, False), (None, True)]
  assert contract.validate_rows(rows) == []


def test_an_unknown_stat_raises():
  with pytest.raises(ValueError, match="stat 'median' is not one of"):
    values_rows(ex(), stat="median")


def test_a_kelvin_variable_has_unit_degree_c():
  rows = values_rows(ex("sst_k", lambda i, la, lo: 300.0))
  assert {r["unit"] for r in rows} == {"degree_C"}


def test_a_variable_without_unit_raises():
  # the erddap-grid schema requires a unit, so the reader refuses such a collection first; values_rows checks again
  x = ex()
  doc = collection()
  del doc["cube:variables"]["sst"]["unit"]
  with pytest.raises(ValueError, match="has no unit"):
    values_rows(dataclasses.replace(x, collection=doc))


def test_categorical_rows_are_one_per_class_with_the_area_share_and_the_class_count():
  x = ex("cls", square_class)
  rows = values_rows(x)
  assert contract.validate_rows(rows) == []
  st = x.stats.to_pylist()
  assert [(r["time"].day, r["class"], r["n"]) for r in rows] == [(1, "0", 3.0), (1, "1", 3.0), (2, "1", 6.0),
                                                                 (3, "0", 5.0), (3, "1", 1.0)]
  assert [r["value"] for r in rows] == [s["frac_area"] for s in st]
  assert {(r["indicator"], r["unit"], r["sd"]) for r in rows} == {("erddap:sst_deg1:cls:share", "1", None)}
  with pytest.raises(ValueError, match="one stat, the area share"):
    values_rows(x, stat="p10")


def test_coll_defaults_from_the_place_authority_and_a_given_coll_wins():
  assert {r["coll"] for r in values_rows(ex(place_id="NMS:SQUARE"))} == {"places"}
  assert {r["coll"] for r in values_rows(ex(place_id="MRGID:1"))} == {"mrgid"}
  assert {r["coll"] for r in values_rows(ex(coll="mr_eez"))} == {"mr_eez"}


# -- grain ---------------------------------------------------------------------------------------------------------------
def test_native_grain_is_day_for_p1d_month_for_p1m_else_from_the_step_in_days():
  assert [native_grain(s) for s in ("P1D", "P1M", "P1Y", "P8D", None)] == ["day", "month", "year", "day", "day"]
  assert [native_grain(None, d) for d in (1, 8, 27.9, 28, 30.4, 359, 365.25)] == [
    "day", "day", "day", "month", "month", "month", "year"]
  assert [step_days_of(s) for s in ("P1D", "P8D", "P1M", "P1Y", "PT1H")] == [1.0, 8.0, 30.4375, 365.25, None]


def test_a_monthly_grid_writes_month_rows_at_the_first_of_the_month():
  doc = collection()
  doc["cube:dimensions"]["time"]["step"] = "P1M"
  x = dataclasses.replace(ex(doc=doc), step_days=None)
  rows = values_rows(x)
  assert {r["time_grain"] for r in rows} == {"month"}
  # three daily dates in one month collapse onto one key: the contract refuses them, as it must
  assert any("duplicate" in e for e in contract.validate_rows(rows))
  assert {r["time"] for r in rows} == {dt.date(2026, 1, 1)}


def test_a_grain_other_than_the_native_one_raises_coarser_grain_is_build_week_1():
  x = ex()
  assert values_rows(x, grain="day") == values_rows(x)
  for g in ("month", "year", "all"):
    with pytest.raises(ValueError, match="aggregation to a coarser grain is build week 1"):
      values_rows(x, grain=g)
  assert COARSER == "aggregation to a coarser grain is build week 1"
  with pytest.raises(ValueError, match="unknown grain"):
    values_rows(x, grain="week")


# -- provenance ----------------------------------------------------------------------------------------------------------
def test_the_provenance_validates_with_the_rows():
  x = ex()
  rows = values_rows(x)
  prov = provenance_for(x, "0.4.0")
  assert contract.validate_provenance(full_prov(rows, prov)) == []


def test_the_provenance_dataset_comes_from_the_collection():
  d = provenance_for(ex(), "0.4.0")["dataset"]
  assert d == {"id": "sst_deg1", "title": "Daily 1 deg SST (synthetic)", "via": "https://example.org/sst_deg1.html",
               "providers": [{"name": "Example Ocean Agency", "roles": ["producer", "licensor"],
                              "url": "https://example.org"},
                             {"name": "Example ERDDAP", "roles": ["host"], "url": "https://erddap.example.org/erddap"}],
               "license": "CC-BY-4.0", "citation": "Synthetic grid, Ocean Metrics tests (2026).",
               "doi": "https://doi.org/10.0000/example.sst"}


def test_the_dataset_via_falls_back_to_about_then_license_and_doi_to_sci_doi():
  c = collection(links=[{"rel": "about", "href": "https://a.org"}, {"rel": "license", "href": "https://l.org"}])
  c["sci:doi"] = "10.1/x"
  d = provenance_dataset(c)
  assert (d["via"], d["doi"]) == ("https://a.org", "https://doi.org/10.1/x")
  d = provenance_dataset(collection(links=[{"rel": "license", "href": "https://l.org"}], license=None))
  assert (d["via"], d["doi"], d["license"]) == ("https://l.org", None, "other")


def test_the_sources_are_each_request_as_data_and_the_collection_as_product():
  x = dataclasses.replace(ex(), collection_href="https://storage.example.org/erddap/sst_deg1/collection.json")
  s = provenance_for(x, "0.4.0")["sources"]
  assert s == [{"href": SQUARE_URL, "item": None, "role": "data"},
               {"href": "https://storage.example.org/erddap/sst_deg1/collection.json", "item": None, "role": "product"}]
  # a dict collection with no href: its id
  assert provenance_for(ex(), "0.4.0")["sources"][-1]["href"] == "erddap/sst_deg1"


def test_the_provenance_names_producer_code_method_license_and_source_version():
  p = provenance_for(ex(), "0.4.0")
  assert p["producer"] == {"name": "oceanmetrics/extract", "url": "https://github.com/oceanmetrics/extract",
                           "version": "0.4.0"}
  assert p["code"] == {"name": "om-extract", "version": "0.4.0", "adapter": "om_extract.engine"}
  assert p["method"] == METHOD and "centres fall inside" in METHOD and "partial-area" in METHOD and "SD" in METHOD
  assert p["license"] == "CC-BY-4.0"
  assert p["source_version"] == "ERDDAP 2.29, erddap/sst_deg1 updated 2026-10-01T00:00:00Z"
  assert len(p["created"]) == 20 and p["created"].endswith("Z")


def test_write_values_accepts_the_rows_and_provenance(tmp_path):
  x = ex()
  path = contract.write_values(values_rows(x), tmp_path, provenance_for(x, "0.4.0"))
  assert path == tmp_path / "values" / "indicator=erddap:sst_deg1:sst:mean_wt" / "coll=test" / "data.parquet"
  assert contract.validate_file(path) == []
