"""om_extract.stats_collection: erddap-places' buildCollection() / writeThumbnail() / listedItems() in Python, one test
per rule on synthetic Items, and a byte-level test against the last collection.json erddap-places published.

The fixture `tests/fixtures/stats_collection/` is public data read from https://storage.oceanmetrics.io/gazetteer on
2026-10-24: `collection.erddap-places.json` (the last one erddap-places' stac.ts wrote, 2026-10-23 22:05Z, before its
precompute went off), `items.json.gz` (the 346 Items it links, the engine's), `erddap.json.gz` (the 12 datasets'
collections), `stats.thumb.png` (its thumbnail) and `units.tsv` (indicators' scripts/parity_units.tsv at 3e8f770)."""
import gzip
import json
from pathlib import Path

import pytest

from om_extract import stats_collection as sc
from om_extract.cli import main

FIX = Path(__file__).parent / "fixtures" / "stats_collection"
#: the fields a rebuild changes by design from the same Items: the build time (everything else is the same bytes)
DESIGNED = {"updated"}


def item(ds="dhw_5km", var="CRW_SST", pid="NMS:CINMS", start="1985-04-01T12:00:00Z", end="2026-10-22T12:00:00Z",
         record_start="1985-04-01", boxes=((-120.6, 33.4, -119.0, 34.2),), bbox=None):
  props = {"start_datetime": start, "end_datetime": end, "erddap-places:place_id": pid,
           "erddap-places:dataset_id": ds, "erddap-places:variable": var}
  if record_start:
    props["erddap-places:record_start"] = record_start
  return {"type": "Feature", "id": sc.item_id(ds, var, pid), "collection": "stats",
          "geometry": {"type": "MultiPolygon",
                       "coordinates": [[[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]] for x0, y0, x1, y1 in boxes]},
          "bbox": bbox or list(boxes[0]), "properties": props}


def ref(**kw):
  return sc.item_ref(item(**kw))


def unit(pid="NMS:CINMS", coll="places", ds="dhw_5km", var="CRW_SST"):
  return {"place_id": pid, "coll": coll, "dataset_id": ds, "variable": var}


def dataset(ds="dhw_5km", start="1985-04-01T12:00:00Z", providers=(), via=None):
  links = [{"rel": "via", "type": "text/html", "href": via[0], **({"title": via[1]} if via[1] else {})}] if via else []
  return {"id": ds, "extent": {"temporal": {"interval": [[start, None]]}}, "providers": list(providers), "links": links}


# -- what is listed -----------------------------------------------------------------------------------------------------
def test_only_units_of_the_places_collection_are_listed_a_requested_place_is_not():
  items = {r["id"]: r for r in (ref(), ref(pid="NERRS:WEL"))}
  listed, pending = sc.listed_refs([unit(), unit(pid="NERRS:WEL", coll="nerrs")], items)
  assert [r["id"] for r in listed] == ["dhw_5km_CRW_SST_NMS-CINMS"]
  assert pending == []


def test_a_unit_without_an_item_is_pending_once_sorted_by_id():
  items = {ref()["id"]: ref()}
  listed, pending = sc.listed_refs([unit(pid="NMS:PMNM"), unit(), unit(pid="NMS:FKNMS"), unit(pid="NMS:PMNM")], items)
  assert [r["id"] for r in listed] == ["dhw_5km_CRW_SST_NMS-CINMS"]
  assert pending == [
    {"id": "dhw_5km_CRW_SST_NMS-FKNMS", "dataset_id": "dhw_5km", "variable": "CRW_SST", "place_id": "NMS:FKNMS"},
    {"id": "dhw_5km_CRW_SST_NMS-PMNM", "dataset_id": "dhw_5km", "variable": "CRW_SST", "place_id": "NMS:PMNM"}]
  c = sc.build_collection(listed, {"dhw_5km": dataset()}, pending, updated="x")
  assert c["erddap-places:item_counts"] == {"complete": 1, "partial": 0, "pending": 2}
  assert [p["id"] for p in c["erddap-places:pending"]] == ["dhw_5km_CRW_SST_NMS-FKNMS", "dhw_5km_CRW_SST_NMS-PMNM"]
  assert not any(lk.get("href", "").endswith("NMS-PMNM.json") for lk in c["links"])


def test_a_unit_listed_twice_is_one_item_link():
  listed, _ = sc.listed_refs([unit(), unit()], {ref()["id"]: ref()})
  assert len(listed) == 1


def test_the_previous_order_is_kept_and_new_items_go_at_the_end_by_id():
  refs = [ref(pid=p) for p in ("NMS:A", "NMS:B", "NMS:C", "NMS:D", "NMS:E")]
  prev = {"links": [{"rel": "root", "href": "../catalog.json"},
                    *({"rel": "item", "href": f"./items/dhw_5km_CRW_SST_NMS-{p}.json"} for p in ("D", "B", "Z"))]}
  order = sc.previous_order(prev)
  assert order == ["dhw_5km_CRW_SST_NMS-D", "dhw_5km_CRW_SST_NMS-B", "dhw_5km_CRW_SST_NMS-Z"]
  assert [r["place_id"] for r in sc.order_refs(refs, order)] == ["NMS:D", "NMS:B", "NMS:A", "NMS:C", "NMS:E"]
  assert [r["place_id"] for r in sc.order_refs(refs[::-1], [])] == ["NMS:A", "NMS:B", "NMS:C", "NMS:D", "NMS:E"]


def test_an_item_whose_id_is_not_its_dataset_variable_and_place_is_refused():
  it = item()
  it["id"] = "dhw_5km_CRW_SST_NMS-PMNM"
  with pytest.raises(ValueError, match="not its dataset"):
    sc.item_ref(it)


# -- status -------------------------------------------------------------------------------------------------------------
def test_status_complete_from_the_record_start_partial_when_later():
  cols = {"dhw_5km": dataset()}
  c = sc.build_collection([ref(pid="NMS:A"), ref(pid="NMS:B", start="2025-10-12T12:00:00Z")], cols, updated="x")
  links = [lk for lk in c["links"] if lk["rel"] == "item"]
  assert [lk["erddap-places:status"] for lk in links] == ["complete", "partial"]
  assert c["erddap-places:item_counts"] == {"complete": 1, "partial": 1, "pending": 0}
  assert c["summaries"]["erddap-places:status"] == ["complete", "partial"]


def test_status_falls_back_to_the_dataset_collections_start_and_unknown_has_none():
  r = ref(record_start=None, start="2000-01-01T00:00:00Z")
  assert sc.ref_status(r, dataset(start="2000-01-01T00:00:00Z")) == "complete"
  assert sc.ref_status(r, dataset(start="1999-01-01T00:00:00Z")) == "partial"
  assert sc.ref_status(r, None) is None
  c = sc.build_collection([r], {}, updated="x")
  link = [lk for lk in c["links"] if lk["rel"] == "item"][0]
  assert "erddap-places:status" not in link
  assert c["erddap-places:item_counts"] == {"complete": 0, "partial": 0, "pending": 0}
  assert c["summaries"]["erddap-places:status"] == []


def test_the_status_summary_lists_only_the_counted_statuses():
  c = sc.build_collection([ref(start="2020-01-01T00:00:00Z")], {"dhw_5km": dataset()}, updated="x")
  assert c["summaries"]["erddap-places:status"] == ["partial"]


# -- extent and summaries -----------------------------------------------------------------------------------------------
def test_the_spatial_extent_is_the_union_of_the_item_bboxes_round_the_globe():
  pmnm = ref(pid="NMS:PMNM", boxes=((177.8, 18.0, 180.0, 31.0), (-180.0, 18.0, -161.9, 31.0)),
             bbox=[177.8, 18.0, -161.9, 31.0])
  fk = ref(pid="NMS:FKNMS", boxes=((-83.1, 24.3, -80.1, 25.8),))
  c = sc.build_collection([fk, pmnm], {}, updated="x")
  assert c["extent"]["spatial"]["bbox"] == [[177.8, 18.0, -80.1, 31.0]]
  assert sc.build_collection([fk], {}, updated="x")["extent"]["spatial"]["bbox"] == [[-83.1, 24.3, -80.1, 25.8]]


def test_the_temporal_extent_is_the_earliest_start_to_the_latest_end():
  c = sc.build_collection([ref(pid="NMS:A", start="2002-07-31T00:00:00Z", end="2023-12-31T00:00:00Z"),
                           ref(pid="NMS:B", start="1985-04-01T12:00:00Z", end="2026-10-22T12:00:00Z"),
                           ref(pid="NMS:C", start="1998-01-01T00:00:00Z", end="2026-10-23T12:00:00Z")], {}, updated="x")
  assert c["extent"]["temporal"]["interval"] == [["1985-04-01T12:00:00Z", "2026-10-23T12:00:00Z"]]


def test_summaries_datasets_and_variables_as_first_seen_places_sorted():
  refs = [ref(ds="seascapes", var="CLASS", pid="NMS:PMNM"), ref(ds="dhw_5km", var="CRW_SST", pid="NMS:CINMS"),
          ref(ds="seascapes", var="CLASS", pid="NMS:FKNMS"), ref(ds="dhw_5km", var="CRW_BAA", pid="NMS:CINMS")]
  s = sc.build_collection(refs, {}, updated="x")["summaries"]
  assert s["erddap-places:dataset_id"] == ["seascapes", "dhw_5km"]
  assert s["erddap-places:variable"] == ["CLASS", "CRW_SST", "CRW_BAA"]
  assert s["erddap-places:place_id"] == ["NMS:CINMS", "NMS:FKNMS", "NMS:PMNM"]


# -- providers and links ------------------------------------------------------------------------------------------------
def test_providers_producers_and_licensors_once_by_name_in_dataset_order_ocean_metrics_last():
  cols = {
    "b": dataset("b", providers=[{"name": "NOAA CRW", "roles": ["producer", "licensor", "host"], "url": "https://crw"},
                                 {"name": "Ocean Metrics LLC", "roles": ["producer"]},
                                 {"name": "PacIOOS", "roles": ["host"]}]),
    "a": dataset("a", providers=[{"name": "NOAA CRW", "roles": ["producer"]}, {"name": "CMEMS", "roles": ["licensor"]}]),
  }
  assert sc.dataset_providers(["b", "a"], cols) == [
    {"name": "NOAA CRW", "roles": ["producer", "licensor"], "url": "https://crw"},
    {"name": "CMEMS", "roles": ["licensor"]},
    {"name": "Ocean Metrics LLC", "roles": ["processor", "host"], "url": "https://oceanmetrics.io"}]


def test_links_related_per_dataset_then_places_then_via_then_items_with_title():
  cols = {"dhw_5km": dataset(via=("https://pacioos/dhw_5km", "CRW daily")), "x": dataset("x", via=("https://x", None))}
  c = sc.build_collection([ref(), ref(ds="x", var="v")], cols, updated="x")
  assert [lk["rel"] for lk in c["links"]] == ["root", "parent", "agents", "describedby", "related", "related", "related",
                                              "via", "via", "item", "item"]
  assert c["links"][4] == {"rel": "related", "href": "../erddap/dhw_5km/collection.json", "type": "application/json",
                           "title": "source dataset: dhw_5km"}
  assert c["links"][6]["href"] == "../places/collection.json"
  assert c["links"][7] == {"href": "https://pacioos/dhw_5km", "title": "CRW daily", "rel": "via", "type": "text/html"}
  assert c["links"][8]["title"] == "x"
  assert c["links"][9] == {"rel": "item", "href": "./items/dhw_5km_CRW_SST_NMS-CINMS.json",
                           "type": "application/geo+json", "title": "NMS:CINMS — CRW_SST",
                           "erddap-places:status": "complete"}


def test_no_items_is_refused():
  with pytest.raises(ValueError, match="no Items"):
    sc.build_collection([], {})


def test_updated_is_utc_whole_seconds():
  assert len(sc.utc_now()) == 20 and sc.utc_now().endswith("Z")


# -- thumbnail ----------------------------------------------------------------------------------------------------------
def test_thumbnail_rounds_half_up_as_math_round():
  assert [sc._round(x) for x in (0.5, 1.5, 2.5, -0.5, -1.5)] == [1, 2, 3, 0, -1]


def test_thumbnail_a_tiny_place_is_one_filled_pixel_with_an_outline():
  px = sc.thumbnail_pixels([[(0.0, 0.0, 0.0, 0.0)]], w=480, h=240)
  at = lambda x, y: tuple(px[(y * 480 + x) * 3:(y * 480 + x) * 3 + 3])
  x, y = sc._round(180 / 360 * 479), sc._round(90 / 180 * 239)
  assert at(x, y) == (56, 142, 168)
  assert at(x - 1, y) == at(x + 1, y + 1) == (240, 196, 92)
  assert at(10, 10) == (10, 26, 47)
  assert at(10, sc._round(90 / 180 * 239)) == (24, 48, 80)            # the equator


def test_thumbnail_png_round_trips_its_pixels():
  px = sc.thumbnail_pixels([[(-120.6, 33.4, -119.0, 34.2)]])
  png = sc.png_bytes(px, 480, 240)
  assert sc.png_pixels(png) == (480, 240, px)


# -- the I/O ------------------------------------------------------------------------------------------------------------
def host(files):
  """a fetch over {path under gazetteer/: dict or bytes or Exception}; a missing path is a 404."""
  calls = []

  def fetch(url):
    rel = url.split("/gazetteer/", 1)[1]
    calls.append(rel)
    v = files.get(rel)
    if isinstance(v, Exception):
      raise v
    return None if v is None else (v if isinstance(v, bytes) else json.dumps(v).encode())
  fetch.calls = calls
  return fetch


def test_rebuild_lists_the_hosts_items_pending_the_missing_and_writes_the_thumbnail_then_the_collection(tmp_path):
  a, b = item(pid="NMS:A"), item(pid="NMS:B")
  f = host({f"stats/items/{a['id']}.json": a, f"stats/items/{b['id']}.json": b, "erddap/dhw_5km/collection.json": dataset()})
  res = sc.rebuild([unit("NMS:B"), unit("NMS:A"), unit("NMS:C")], tmp_path, fetch=f, updated="2026-10-24T09:30:00Z")
  assert (res["items"], res["pending"], res["previous"]) == (2, 1, False)
  c = json.loads((tmp_path / "collection.json").read_text())
  assert [lk["href"] for lk in c["links"] if lk["rel"] == "item"] == [f"./items/{a['id']}.json", f"./items/{b['id']}.json"]
  assert c["erddap-places:pending"][0]["place_id"] == "NMS:C"
  assert c["assets"]["thumbnail"]["file:size"] == (tmp_path / sc.THUMB).stat().st_size
  assert c["assets"]["thumbnail"]["file:checksum"] == sc.file_stats(tmp_path / sc.THUMB)["file:checksum"]
  assert c["updated"] == "2026-10-24T09:30:00Z"


def test_rebuild_takes_the_runs_own_item_over_the_hosts(tmp_path):
  old, new = item(end="2026-10-21T12:00:00Z"), item(end="2026-10-23T12:00:00Z")
  d = tmp_path / "run" / "items"
  d.mkdir(parents=True)
  (d / f"{new['id']}.json").write_text(json.dumps(new))
  f = host({f"stats/items/{old['id']}.json": old, "erddap/dhw_5km/collection.json": dataset()})
  res = sc.rebuild([unit()], tmp_path / "out", fetch=f, local_items=[d], updated="x")
  assert res["local"] == 1
  assert f"stats/items/{old['id']}.json" not in f.calls
  c = json.loads((tmp_path / "out" / "collection.json").read_text())
  assert c["extent"]["temporal"]["interval"][0][1] == "2026-10-23T12:00:00Z"


def test_rebuild_ignores_a_local_item_of_no_unit(tmp_path):
  a, stray = item(), item(pid="NERRS:WEL")
  d = tmp_path / "items"
  d.mkdir()
  (d / f"{stray['id']}.json").write_text(json.dumps(stray))
  f = host({f"stats/items/{a['id']}.json": a, "erddap/dhw_5km/collection.json": dataset()})
  sc.rebuild([unit()], tmp_path / "out", fetch=f, local_items=[d], updated="x")
  c = json.loads((tmp_path / "out" / "collection.json").read_text())
  assert [lk["title"] for lk in c["links"] if lk["rel"] == "item"] == ["NMS:CINMS — CRW_SST"]


def test_rebuild_refuses_on_a_host_error_and_writes_nothing(tmp_path):
  a = item()
  f = host({f"stats/items/{a['id']}.json": RuntimeError("HTTP 503"), "erddap/dhw_5km/collection.json": dataset()})
  with pytest.raises(RuntimeError, match="503"):
    sc.rebuild([unit()], tmp_path, fetch=f)
  assert not (tmp_path / "collection.json").exists()


def test_rebuild_refuses_when_no_unit_has_an_item_or_a_dataset_has_no_collection(tmp_path):
  with pytest.raises(ValueError, match="no unit has a published Item"):
    sc.rebuild([unit()], tmp_path, fetch=host({}))
  a = item()
  with pytest.raises(ValueError, match="no dataset collection"):
    sc.rebuild([unit()], tmp_path, fetch=host({f"stats/items/{a['id']}.json": a}))
  assert not (tmp_path / "collection.json").exists()


def test_rebuild_refuses_an_item_that_is_not_its_units(tmp_path):
  a = item(pid="NMS:PMNM")
  f = host({"stats/items/dhw_5km_CRW_SST_NMS-CINMS.json": a, "erddap/dhw_5km/collection.json": dataset()})
  with pytest.raises(ValueError, match="is dhw_5km_CRW_SST_NMS-PMNM"):
    sc.rebuild([unit()], tmp_path, fetch=f)


def test_read_units_skips_comments_blank_lines_and_the_header(tmp_path):
  p = tmp_path / "u.tsv"
  p.write_text("# a comment\n\nplace\tcoll\tdataset\tvariable\tcadence\nNMS:CINMS\tplaces\tdhw_5km\tCRW_SST\tdaily\n"
               "NMS:PMNM\tplaces\tx\ty\n")
  assert sc.read_units(p) == [unit(), unit("NMS:PMNM", ds="x", var="y")]
  p.write_text("NMS:CINMS\tplaces\n")
  with pytest.raises(ValueError, match="place, coll, dataset, variable"):
    sc.read_units(p)


# -- the byte-level test against erddap-places' last collection ---------------------------------------------------------
def published_host():
  items = json.loads(gzip.decompress((FIX / "items.json.gz").read_bytes()))
  cols = json.loads(gzip.decompress((FIX / "erddap.json.gz").read_bytes()))
  files = {f"stats/items/{it['id']}.json": it for it in items}
  files.update({f"erddap/{d}/collection.json": c for d, c in cols.items()})
  files["stats/collection.json"] = (FIX / "collection.erddap-places.json").read_bytes()
  return host(files)


def test_the_rebuild_reproduces_erddap_places_last_collection_byte_for_byte_but_the_designed_fields(tmp_path):
  published = (FIX / "collection.erddap-places.json").read_text()
  units = sc.read_units(FIX / "units.tsv")
  res = sc.rebuild(units, tmp_path / "a", fetch=published_host())
  assert (res["items"], res["pending"]) == (346, 0)
  ours, theirs = json.loads((tmp_path / "a" / "collection.json").read_text()), json.loads(published)
  assert {k for k in ours.keys() | theirs.keys() if ours.get(k) != theirs.get(k)} <= DESIGNED
  # with the designed fields set to the published values the file is the same bytes, and so is the thumbnail
  sc.rebuild(units, tmp_path / "b", fetch=published_host(), updated=theirs["updated"])
  assert (tmp_path / "b" / "collection.json").read_text() == published
  assert (tmp_path / "b" / sc.THUMB).read_bytes() == (FIX / "stats.thumb.png").read_bytes()


def test_the_cli_writes_the_collection_from_the_units(tmp_path, monkeypatch, capsys):
  f = published_host()
  monkeypatch.setattr(sc, "http_fetch", f)
  assert main(["stats-collection", "--units", str(FIX / "units.tsv"), "--out", str(tmp_path)]) == 0
  assert "346 items (346 complete, 0 partial), 0 pending" in capsys.readouterr().out
  assert json.loads((tmp_path / "collection.json").read_text())["erddap-places:item_counts"]["complete"] == 346
  assert "stats/collection.json" in f.calls and len(f.calls) == 346 + 1 + 12
