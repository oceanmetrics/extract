"""om_extract.chunks: the runner's chunk store layout, mask key and chunk plan (precompute/src/stats.ts:117-190, :289-294,
:357-390). The plan cases mirror precompute's chunks.test.ts; the key is checked against the runner's own directory."""
import gzip
from pathlib import Path

import numpy as np
import pytest

from om_extract.chunks import (ChunkPlan, ChunkStore, chunk_name, chunk_span, file_safe, mask_key, plan_chunks, window,
                               write_atomic)

FIX = Path(__file__).resolve().parent / "fixtures"
PAC = "https://pae-paha.pacioos.hawaii.edu/erddap"


# -- names and the window ----------------------------------------------------------------------------------------------
def test_a_place_id_keeps_its_colon_in_the_path():
  assert file_safe("NMS:CINMS") == "NMS:CINMS"


def test_the_store_layout_is_dataset_variable_place_key():
  d = ChunkStore("/x").dir_for("dhw_5km", "CRW_SST", "NMS:CINMS", "a91ae222cbfe")
  assert d == Path("/x/dhw_5km/CRW_SST/NMS:CINMS/a91ae222cbfe")


def test_a_chunk_name_round_trips_and_other_files_are_not_chunks():
  assert chunk_name("2026-01-01", "2026-03-31") == "2026-01-01_2026-03-31.parquet"
  assert chunk_span("2026-01-01_2026-03-31.parquet") == ("2026-01-01", "2026-03-31")
  assert chunk_span("2026-01-01_2026-03-31.parquet.tmp") is None
  assert chunk_span("notes.parquet") is None


def test_the_window_all_is_the_extent_by_day():
  assert window("1985-04-01T12:00:00Z", "2026-10-21T12:00:00Z") == ("1985-04-01", "2026-10-21")


def test_a_window_of_days_ends_at_the_extent_end_and_never_starts_before_it():
  assert window("2026-01-01T12:00:00Z", "2026-04-10T12:00:00Z", 30) == ("2026-03-12", "2026-04-10")
  assert window("2026-04-01T12:00:00Z", "2026-04-10T12:00:00Z", 30) == ("2026-04-01", "2026-04-10")


def test_a_window_needs_the_extent_end():
  with pytest.raises(ValueError, match="live extent"):
    window("2026-01-01T12:00:00Z", None)


# -- the plan (chunks.test.ts) -------------------------------------------------------------------------------------------
def test_an_empty_store_fetches_every_chunk_and_the_last_is_short():
  p = plan_chunks([], ("2026-01-01", "2026-04-10"), 90)
  assert p == ChunkPlan(todo=[("2026-01-01", "2026-03-31"), ("2026-04-01", "2026-04-10")])


def test_a_week_later_the_full_chunk_is_kept_and_the_short_last_chunk_is_superseded():
  p = plan_chunks(["2026-01-01_2026-03-31.parquet", "2026-04-01_2026-04-10.parquet"], ("2026-01-01", "2026-04-17"), 90)
  assert p.keep == [("2026-01-01", "2026-03-31")]
  assert p.supersede == "2026-04-01_2026-04-10.parquet"
  assert p.todo == [("2026-04-01", "2026-04-17")]
  assert p.slices == [("2026-01-01", "2026-03-31"), ("2026-04-01", "2026-04-17")]


def test_the_gap_after_a_kept_chunk_is_cut_at_chunk_days():
  p = plan_chunks(["2026-01-01_2026-03-31.parquet"], ("2026-01-01", "2026-07-31"), 90)
  assert p.keep == [("2026-01-01", "2026-03-31")] and p.supersede is None
  assert p.todo == [("2026-04-01", "2026-06-29"), ("2026-06-30", "2026-07-31")]


def test_a_short_last_chunk_that_ends_with_the_window_is_kept():
  p = plan_chunks(["2026-01-01_2026-03-31.parquet", "2026-04-01_2026-04-10.parquet"], ("2026-01-01", "2026-04-10"), 90)
  assert p.keep == [("2026-01-01", "2026-03-31"), ("2026-04-01", "2026-04-10")]
  assert p.todo == [] and p.supersede is None


def test_gaps_between_kept_chunks_are_cut_at_chunk_days():
  p = plan_chunks(["2026-01-01_2026-01-10.parquet", "2026-05-01_2026-05-10.parquet"], ("2026-01-01", "2026-05-10"), 30)
  assert p.keep == [("2026-01-01", "2026-01-10"), ("2026-05-01", "2026-05-10")]
  assert p.todo == [("2026-01-11", "2026-02-09"), ("2026-02-10", "2026-03-11"), ("2026-03-12", "2026-04-10"),
                    ("2026-04-11", "2026-04-30")]


def test_chunks_cut_at_two_sizes_from_one_start_are_both_kept():
  p = plan_chunks(["2026-01-01_2026-01-20.parquet", "2026-01-01_2026-03-31.parquet"], ("2026-01-01", "2026-03-31"), 90)
  assert p.keep == [("2026-01-01", "2026-01-20"), ("2026-01-01", "2026-03-31")]
  assert p.todo == [] and p.supersede is None


def test_a_short_last_chunk_inside_another_kept_chunk_is_not_superseded():
  p = plan_chunks(["2026-01-01_2026-03-31.parquet", "2026-03-01_2026-03-10.parquet"], ("2026-01-01", "2026-04-10"), 90)
  assert p.supersede is None
  assert p.keep == [("2026-01-01", "2026-03-31"), ("2026-03-01", "2026-03-10")]
  assert p.todo == [("2026-04-01", "2026-04-10")]


def test_files_outside_the_window_and_other_files_are_dropped():
  p = plan_chunks(["2025-10-01_2025-12-29.parquet", "junk.parquet", "2026-01-01_2026-01-31.parquet"],
                  ("2026-01-01", "2026-01-31"), 90)
  assert p.drop == ["2025-10-01_2025-12-29.parquet", "junk.parquet"]
  assert p.keep == [("2026-01-01", "2026-01-31")]


def test_the_listing_order_does_not_change_the_plan():
  names = ["2026-04-01_2026-04-10.parquet", "2026-01-01_2026-03-31.parquet"]
  assert plan_chunks(names, ("2026-01-01", "2026-04-17"), 90) == plan_chunks(names[::-1], ("2026-01-01", "2026-04-17"), 90)


# -- the mask key ------------------------------------------------------------------------------------------------------
CELLS = [(-80.9, 31.4, 1.0)]


def test_the_key_is_12_hex_and_stable():
  k = mask_key(PAC, "dhw_5km", None, "CRW_SST", 's."CRW_SST"', "stats_daily", CELLS)
  assert len(k) == 12 and int(k, 16) >= 0
  assert mask_key(PAC, "dhw_5km", None, "CRW_SST", 's."CRW_SST"', "stats_daily", list(CELLS)) == k


def test_the_key_changes_with_a_weight_a_variable_or_a_depth():
  k = mask_key(PAC, "dhw_5km", None, "CRW_SST", 's."CRW_SST"', "stats_daily", CELLS)
  assert mask_key(PAC, "dhw_5km", None, "CRW_SST", 's."CRW_SST"', "stats_daily", [(-80.9, 31.4, 0.5)]) != k
  assert mask_key(PAC, "dhw_5km", None, "CRW_BAA", 's."CRW_BAA"', "stats_categorical", CELLS) != k
  assert mask_key(PAC, "dhw_5km", 0.0, "CRW_SST", 's."CRW_SST"', "stats_daily", CELLS) != k


def test_the_key_hashes_javascript_numbers_and_json():
  # the TS hashes JSON.stringify([...]) then `${lon},${lat},${weight};`: 5 and 1 are "5" and "1", not "5.0"
  import hashlib
  h = hashlib.sha1(b'["https://e.org/erddap","g",5,"v","s.\\"v\\"","stats_daily"]')
  h.update(b"-180,20.5,1;")
  assert mask_key("https://e.org/erddap", "g", 5.0, "v", 's."v"', "stats_daily", [(-180.0, 20.5, 1.0)]) == h.hexdigest()[:12]


def test_the_cinms_key_is_the_runners_directory():
  # regression (2026-10-22): the published CINMS geometry on the server's dhw_5km axes (the provenance's `axes`) gives the
  # key of the runner's chunk directory precompute-chunks/dhw_5km/CRW_SST/NMS:CINMS/a91ae222cbfe
  from om_extract.engine import _mask_lobe, value_expr
  from om_extract.geom import place_lobes
  from om_extract.readers.base import Axes
  wkb = gzip.decompress((FIX / "parity" / "cinms_places.wkb.gz").read_bytes())
  [lobe] = place_lobes(wkb, "NMS:CINMS")
  lon = [round(-120.625 + 0.05 * i, 3) for i in range(35)]
  lat = [round(34.225 - 0.05 * i, 3) for i in range(18)]
  m = _mask_lobe(lobe, Axes(lat=np.array(lat), lon=np.array(lon), step=(None, None), lat_descending=True,
                            lon_range=(-180, 180)), (-180, 180))
  cells = zip(m["longitude"].to_pylist(), m["latitude"].to_pylist(), m["weight"].to_pylist())
  assert m.num_rows == 215
  assert mask_key(PAC, "dhw_5km", None, "CRW_SST", value_expr("CRW_SST", "Celsius"), "stats_daily", cells) == "a91ae222cbfe"


# -- writes ------------------------------------------------------------------------------------------------------------
def test_a_write_is_atomic_and_leaves_no_tmp(tmp_path):
  p = tmp_path / "a" / "2026-01-01_2026-01-02.parquet"
  write_atomic(p, b"x")
  assert p.read_bytes() == b"x" and sorted(x.name for x in p.parent.iterdir()) == [p.name]


def test_the_listing_is_sorted_parquet_only(tmp_path):
  for n in ("b.parquet", "a.parquet", "c.parquet.tmp", "d.csv"):
    (tmp_path / n).write_bytes(b"")
  assert ChunkStore.listing(tmp_path) == ["a.parquet", "b.parquet"]
  assert ChunkStore.listing(tmp_path / "missing") == []
