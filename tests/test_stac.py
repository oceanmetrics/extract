"""om_extract.stac: the stats Item (erddap-places' buildItem()) and the stats/ layout `om-extract run --chunks
--stats-dir` writes, on the synthetic ERDDAP of the chunked engine (no network). The live test builds the published
NMS:CINMS x dhw_5km/CRW_SST Item from its published files and compares it with the published Item (OM_LIVE=1)."""
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely.geometry import MultiPolygon, box

from fixtures_chunks import FakeErddap
from fixtures_engine import collection, wkb
from om_extract import contract
from om_extract.places import place_record
from om_extract.run import run
from om_extract.stac import (build_item, file_stats, item_id, item_status, item_text, multihash, place_bbox,
                             stats_href, union_bbox)

SPLIT = MultiPolygon([box(178.3, -1.0, 180.0, 1.0), box(-180.0, -1.0, -178.4, 1.0)])


def prov(**over):
  p = {"place_id": "TEST:square", "dataset_id": "sst_deg1", "variable": "sst", "categorical": False,
       "start_date": "2026-01-01", "end_date": "2026-04-10", "start_datetime": "2026-01-01T12:00:00Z",
       "end_datetime": "2026-04-10T12:00:00Z", "record_start": "2026-01-01", "lobes": 1, "mask_cells": 6,
       "griddap_url_templates": ["https://erddap.example.org/x?{t0}"], "griddap_chunks": [], "griddap_requests": 2,
       "griddap_urls": ["u1", "u2"], "rows": 100, "bytes": 1, "generated": "2026-10-23T00:00:00Z"}
  p.update(over)
  return p


# ---- names and geometry ----

def test_the_item_id_and_the_href_keep_the_layout_erddap_places_publishes():
  assert item_id("dhw_5km", "CRW_SST", "NMS:CINMS") == "dhw_5km_CRW_SST_NMS-CINMS"
  assert stats_href("dhw_5km", "CRW_SST", "NMS:CINMS") == "./dhw_5km/CRW_SST/NMS:CINMS.parquet"
  assert stats_href("d", "v", "BOEM:OCS P/1(a)") == "./d/v/BOEM:OCS%20P%2F1(a).parquet"     # encodeURIComponent


def test_the_bbox_of_a_place_across_the_antimeridian_crosses_it():
  assert union_bbox([(178.3, -1.0, 180.0, 1.0), (-180.0, -1.0, -178.4, 1.0)]) == [178.3, -1.0, -178.4, 1.0]
  assert place_bbox(SPLIT) == [178.3, -1.0, -178.4, 1.0]
  assert union_bbox([(10, 20, 11, 21), (12, 19, 13, 20)]) == [10, 19, 13, 21]


def test_the_item_geometry_is_the_lobe_boxes_not_the_outline():
  g = build_item(prov(), SPLIT, "Split", "SST")["geometry"]
  assert g == {"type": "MultiPolygon", "coordinates": [
    [[[-180.0, -1.0], [-178.4, -1.0], [-178.4, 1.0], [-180.0, 1.0], [-180.0, -1.0]]],
    [[[178.3, -1.0], [180.0, -1.0], [180.0, 1.0], [178.3, 1.0], [178.3, -1.0]]]]}


def test_status_is_complete_from_the_record_start_and_partial_after_it():
  assert item_status(prov()) == "complete"
  assert item_status(prov(start_datetime="2026-02-01T12:00:00Z")) == "partial"
  doc = collection()
  assert item_status(prov(record_start=None), doc) is None                  # the fixture's interval is not set
  doc["extent"] = {"temporal": {"interval": [["2025-06-01T12:00:00Z", None]]}}
  assert item_status(prov(record_start=None), doc) == "partial"


def test_file_stats_are_the_size_and_the_sha256_multihash(tmp_path):
  f = tmp_path / "a.bin"
  f.write_bytes(b"abc")
  assert file_stats(f) == {"file:size": 3, "file:checksum":
                           "1220ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"}
  assert file_stats(tmp_path / "none") == {} and multihash(f).startswith("1220")


def test_the_item_properties_and_links_in_the_ts_order():
  it = build_item(prov(), wkb("square"), "Square", "Daily 1 deg SST (synthetic)", coll="noaa_nerrs", collection=collection())
  assert list(it) == ["type", "stac_version", "stac_extensions", "id", "collection", "geometry", "bbox", "properties",
                      "assets", "links"]
  p = it["properties"]
  assert p["title"] == "Square — sst (sst_deg1)"
  assert p["description"] == ("Precomputed daily statistics of sst from Daily 1 deg SST (synthetic) over Square "
                              "(TEST:square), 2026-01-01 to 2026-04-10.")
  assert list(p)[:8] == ["title", "description", "datetime", "start_datetime", "end_datetime", "created",
                         "table:row_count", "gazetteer:collection"]
  assert p["gazetteer:collection"] == "noaa_nerrs" and p["erddap-places:status"] == "complete"
  assert p["erddap-places:griddap_urls"] == ["u1", "u2"] and p["erddap-places:griddap_requests"] == 2
  assert it["assets"]["data"]["href"] == "../sst_deg1/sst/TEST:square.parquet"
  assert it["assets"]["provenance"]["href"] == "../sst_deg1/sst/TEST:square.provenance.json"
  assert [(lk["rel"], lk["href"]) for lk in it["links"]][3:] == [
    ("related", "../../erddap/sst_deg1/collection.json"), ("related", "../../noaa_nerrs/collection.json"),
    ("via", "https://example.org/sst_deg1.html")]
  assert it["links"][5] == {"href": "https://example.org/sst_deg1.html", "title": "sst_deg1", "rel": "via", "type": "text/html"}
  assert "erddap-places:griddap_urls" not in build_item(prov(griddap_urls=None), wkb("square"), "S", "T")["properties"]


def test_the_item_text_is_json_stringify_with_two_spaces():
  it = build_item(prov(), wkb("square"), "Square", "T")
  assert item_text(it).startswith('{\n  "type": "Feature",\n') and item_text(it).endswith("}\n")


# ---- om-extract run --chunks --stats-dir ----

def write_places(base, coll, rows):
  d = base / coll
  d.mkdir(parents=True)
  pq.write_table(pa.table({"place_id": [r[0] for r in rows], "name": [r[1] for r in rows],
                           "geometry": pa.array([r[2] for r in rows], pa.binary())}), d / "places.parquet")


def test_a_private_copy_of_a_reference_collection_is_read_before_the_published_one(tmp_path):
  write_places(tmp_path / "pub", "mr_eez", [("MRGID:1", "published", box(0, 0, 1, 1).wkb)])
  write_places(tmp_path / "own", "mr_eez", [("MRGID:1", "private", box(0, 0, 2, 2).wkb)])
  r = place_record("MRGID:1", "mr_eez", tmp_path / "pub", tmp_path / "own")
  assert (r["name"], r["copy"]) == ("private", "internal")
  r = place_record("MRGID:1", "mr_eez", tmp_path / "pub", tmp_path / "nothing")
  assert (r["name"], r["copy"], r["href"]) == ("published", "published", str(tmp_path / "pub" / "mr_eez" / "places.parquet"))


def test_run_with_chunks_and_a_stats_dir_writes_the_stats_layout_and_its_item(tmp_path):
  (tmp_path / "collection.json").write_text(json.dumps(collection()))
  write_places(tmp_path / "gaz", "noaa_nerrs", [("NERRS:SQ", "Square Reserve", wkb("square"))])
  stats = tmp_path / "gazetteer" / "stats"
  res = run(tmp_path / "collection.json", "NERRS:SQ", None, "sst", None, None, tmp_path / "out", coll="noaa_nerrs",
            fetch=FakeErddap(end="2026-04-10T12:00:00Z"), chunks=tmp_path / "chunks", stats_dir=stats,
            base=tmp_path / "gaz", internal=tmp_path / "none")
  parquet = stats / "sst_deg1" / "sst" / "NERRS:SQ.parquet"
  provf = stats / "sst_deg1" / "sst" / "NERRS:SQ.provenance.json"
  itemf = stats / "items" / "sst_deg1_sst_NERRS-SQ.json"
  assert (res["stats"], res["provenance"], res["item"]) == (parquet, provf, itemf)
  assert sorted(str(p.relative_to(stats)) for p in stats.rglob("*") if p.is_file()) == [
    "items/sst_deg1_sst_NERRS-SQ.json", "sst_deg1/sst/NERRS:SQ.parquet", "sst_deg1/sst/NERRS:SQ.provenance.json"]
  p = json.loads(provf.read_text())
  assert p["rows"] == 100 and p["bytes"] == parquet.stat().st_size and p["start_date"] == "2026-01-01"
  it = json.loads(itemf.read_text())
  assert it["properties"]["title"] == "Square Reserve — sst (sst_deg1)" and it["properties"]["gazetteer:collection"] == "noaa_nerrs"
  assert it["properties"]["table:row_count"] == 100 and it["properties"]["erddap-places:status"] == "complete"
  assert it["assets"]["data"]["file:size"] == parquet.stat().st_size
  assert it["assets"]["provenance"]["file:checksum"] == multihash(provf)
  assert res["rows"] == 100 and contract.validate_file(res["values"]) == []
  assert not (tmp_path / "out" / "stats.parquet").exists()


def test_run_without_chunks_needs_a_window(tmp_path):
  (tmp_path / "collection.json").write_text(json.dumps(collection()))
  (tmp_path / "p.wkb").write_bytes(wkb("square"))
  with pytest.raises(ValueError, match="--t0 and --t1 are required without --chunks"):
    run(tmp_path / "collection.json", "TEST:square", tmp_path / "p.wkb", "sst", None, None, tmp_path / "out")


# ---- live: the published Item ----

@pytest.mark.live
def test_the_published_cinms_item_is_built_again_from_its_published_files(tmp_path):
  """everything equal but the asset hrefs: the published Item predates erddap-places' fix of `../../` to `../`"""
  from om_extract.places import GAZETTEER_BASE as G, place_geometry
  from om_extract.readers.erddap import make_fetch
  from om_extract.stac import stats_rel
  f = make_fetch(timeout_s=120, attempts=2)
  text = f(f"{G}/stats/items/dhw_5km_CRW_SST_NMS-CINMS.json").decode()
  provb = f(f"{G}/stats/dhw_5km/CRW_SST/NMS:CINMS.provenance.json")
  p = tmp_path / stats_rel("dhw_5km", "CRW_SST", "NMS:CINMS")
  p.parent.mkdir(parents=True)
  p.write_bytes(f(f"{G}/stats/dhw_5km/CRW_SST/NMS:CINMS.parquet"))
  p.with_name("NMS:CINMS.provenance.json").write_bytes(provb)
  coll = json.loads(f(f"{G}/erddap/dhw_5km/collection.json"))
  item = build_item(json.loads(provb), place_geometry("NMS:CINMS"), "Channel Islands National Marine Sanctuary",
                    coll["title"], collection=coll, stats_root=tmp_path)
  assert item_text(item) == text.replace('"../../dhw_5km/', '"../dhw_5km/')
