"""engine.extract(..., chunks=): precompute computeOne() through the chunk store, on a synthetic ERDDAP (no network).

The cases mirror precompute's chunks.test.ts (first run, a week later, a stopped run) and stats.ts's rules: the live
extent is required, axes come from the server, the largest lobe sizes every lobe's chunks, the stats file is one
DuckDB COPY (DISTINCT, ordered by date or date and class, ZSTD), a zero-row place still writes it."""
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from shapely.geometry import MultiPolygon, box

from fixtures_chunks import FakeErddap, doy
from fixtures_engine import INFO_URL, collection, wkb
from om_extract.chunks import mask_key
from om_extract.engine import (StopRequested, download, extract, provenance_text, stats_provenance, ts_sql)
from om_extract.readers.erddap import ErddapError, ErddapUnreachable

PID = "TEST:square"


def run(tmp_path, erd, *, place="square", pid=PID, var="sst", doc=None, sleeps=None, **kw):
  geom = wkb(place) if isinstance(place, str) else place
  return extract(doc or collection(), pid, geom, var, chunks=tmp_path / "chunks", fetch=erd,
                 sleep=(sleeps.append if sleeps is not None else (lambda s: None)), **kw)


def chunk_names(x):
  return sorted(p.name for p in __import__("pathlib").Path(x.chunk_dir).iterdir())


# -- chunks.test.ts ------------------------------------------------------------------------------------------------------
def test_the_first_run_fetches_every_chunk_and_the_next_only_the_new_last_chunk(tmp_path):
  erd = FakeErddap(end="2026-04-10T12:00:00Z")
  x = run(tmp_path, erd)
  assert sorted(erd.slabs) == ["2026-01-01..2026-03-31", "2026-04-01..2026-04-10"]
  assert x.rows == 100 and x.stats.num_rows == 100
  p = stats_provenance(x)
  assert p["griddap_requests"] == 2 and len(p["griddap_urls"]) == 2
  assert p["griddap_chunks"][1] == ["2026-04-01T12:00:00Z", "2026-04-10T12:00:00Z"]
  assert "sst%5B({t0}):1:({t1})%5D" in p["griddap_url_templates"][0]

  erd2 = FakeErddap(end="2026-04-17T12:00:00Z")
  x2 = run(tmp_path, erd2)
  assert erd2.slabs == ["2026-04-01..2026-04-17"]
  assert x2.rows == 107
  assert chunk_names(x2) == ["2026-01-01_2026-03-31.parquet", "2026-04-01_2026-04-17.parquet"]
  rows = x2.stats.to_pylist()
  assert [r["date"].isoformat() for r in rows][:1] == ["2026-01-01"] and rows[0]["mean"] == 1
  assert rows[89]["mean"] == 90 and rows[106]["mean"] == 107
  assert len({r["date"] for r in rows}) == 107


def test_a_stopped_place_keeps_its_finished_chunks_and_the_next_run_fetches_the_rest(tmp_path):
  erd = FakeErddap(end="2026-07-31T12:00:00Z")
  n = [0]

  def stop():
    n[0] += 1
    return n[0] > 1
  with pytest.raises(StopRequested):
    run(tmp_path, erd, should_stop=stop)
  assert erd.slabs == ["2026-01-01..2026-03-31"]
  erd2 = FakeErddap(end="2026-07-31T12:00:00Z")
  x = run(tmp_path, erd2)
  assert sorted(erd2.slabs) == ["2026-04-01..2026-06-29", "2026-06-30..2026-07-31"]   # two in flight: any order
  assert x.rows == 212


# -- stats.ts rules ------------------------------------------------------------------------------------------------------
def test_the_axes_come_from_the_server_longitude_then_latitude(tmp_path):
  erd = FakeErddap()
  run(tmp_path, erd)
  axis = [u for u in erd.log if ".json?" in u]
  assert len(axis) == 2 and "json?longitude%5B(10.3):1:(12.6)%5D" in axis[0] and "json?latitude%5B(21.7):1:(20.2)%5D" in axis[1]


def test_the_largest_lobe_sizes_the_chunks_of_every_lobe(tmp_path):
  # west lobe 50 x 40 = 2,000 cells per step: 100,000 / 2,000 = 50 days; the 2 x 2 east lobe is cut at 50 days too
  geom = MultiPolygon([box(-180, 0, -130.2, 39.8), box(178.2, 0.2, 180, 1.8)])
  erd = FakeErddap(end="2026-03-01T12:00:00Z")
  x = run(tmp_path, erd, place=geom, pid="TEST:am")
  assert x.lobes == 2 and x.chunk_days == 50
  assert sorted(erd.slabs) == ["2026-01-01..2026-02-19"] * 2 + ["2026-02-20..2026-03-01"] * 2
  assert len(x.urls) == 4 and x.urls[0].count("-179.5") == 1 and "178.5" in x.urls[2]


def test_without_an_answer_on_the_extent_the_run_raises_after_five_tries(tmp_path):
  erd = FakeErddap(info_error=ErddapUnreachable("erddap.example.org", "no response within 120 s", 1, INFO_URL))
  sleeps: list[float] = []
  with pytest.raises(ErddapUnreachable, match="extent probe failed 5 times"):
    run(tmp_path, erd, sleeps=sleeps)
  assert erd.log == [INFO_URL] * 5 and sleeps == [5, 20, 60, 120]


def test_a_4xx_on_the_extent_raises_at_once(tmp_path):
  erd = FakeErddap(info_error=ErddapError("404 Not Found from ERDDAP: Currently unknown datasetID=sst_deg1"))
  with pytest.raises(ErddapError, match="unknown datasetID"):
    run(tmp_path, erd)
  assert erd.log == [INFO_URL]


def test_a_zero_row_place_still_writes_the_file_and_its_provenance(tmp_path):
  erd = FakeErddap(value=lambda d, la, lo: None)
  x = run(tmp_path, erd)
  assert x.rows == 0 and x.mask_cells == 6
  t = pq.read_table(x.stats_path)
  assert t.num_rows == 0
  assert t.column_names == ["date", "n", "mean", "mean_wt", "sd", "min", "max", "p10", "p90", "weight_sum", "place_id",
                            "dataset_id", "variable"]
  assert stats_provenance(x)["rows"] == 0


def test_categorical_rows_are_ordered_by_date_then_class(tmp_path):
  erd = FakeErddap(end="2026-01-03T12:00:00Z", value=lambda d, la, lo: 1.0 if la == 21.5 else 0.0)
  x = run(tmp_path, erd, var="cls")
  rows = x.stats.to_pylist()
  assert x.stats.column_names == ["date", "class", "n", "weight", "frac_area", "pct_cells", "place_id", "dataset_id",
                                  "variable"]
  assert [(r["date"].isoformat(), r["class"]) for r in rows] == [
    ("2026-01-01", 0), ("2026-01-01", 1), ("2026-01-02", 0), ("2026-01-02", 1), ("2026-01-03", 0), ("2026-01-03", 1)]


def test_the_file_is_one_zstd_row_group_and_overlapping_chunks_merge_distinct(tmp_path):
  erd = FakeErddap(end="2026-03-31T12:00:00Z")
  x = run(tmp_path, erd, existing=["2026-01-01_2026-01-20.parquet", "2026-01-01_2026-03-31.parquet"])
  assert sorted(erd.slabs) == ["2026-01-01..2026-01-20", "2026-01-01..2026-03-31"]
  meta = pq.ParquetFile(x.stats_path).metadata
  assert meta.num_row_groups == 1 and meta.row_group(0).column(0).compression == "ZSTD"
  dates = [r["date"] for r in x.stats.to_pylist()]
  assert len(dates) == 90 and dates == sorted(set(dates))


def test_the_existing_names_set_the_slices_and_every_missing_chunk_is_fetched(tmp_path):
  erd = FakeErddap(end="2026-04-10T12:00:00Z")
  x = run(tmp_path, erd, existing=["2026-01-01_2026-02-14.parquet", "2026-02-15_2026-04-10.parquet"])
  assert sorted(erd.slabs) == ["2026-01-01..2026-02-14", "2026-02-15..2026-04-10"]
  assert stats_provenance(x)["griddap_chunks"][0] == ["2026-01-01T12:00:00Z", "2026-02-14T12:00:00Z"]


def test_slabs_are_deleted_after_aggregation_and_no_tmp_remains(tmp_path):
  x = run(tmp_path, FakeErddap())
  slabs = tmp_path / "chunks" / ".slabs"
  assert [p for p in slabs.rglob("*") if p.is_file()] == []
  assert not [p for p in (tmp_path / "chunks").rglob("*.tmp")]
  assert chunk_names(x) == ["2026-01-01_2026-03-31.parquet", "2026-04-01_2026-04-10.parquet"]


def test_the_mask_key_names_the_chunk_directory(tmp_path):
  x = run(tmp_path, FakeErddap())
  m = x.mask
  key = mask_key("https://erddap.example.org/erddap", "sst_deg1", None, "sst", 's."sst"', "stats_daily",
                 zip(m["longitude"].to_pylist(), m["latitude"].to_pylist(), m["weight"].to_pylist()))
  assert x.mask_key == key and x.chunk_dir.endswith(f"sst_deg1/sst/TEST:square/{key}")


def test_the_window_can_be_pinned(tmp_path):
  erd = FakeErddap(end="2026-04-10T12:00:00Z")
  x = run(tmp_path, erd, t0="2026-01-01", t1="2026-01-31")
  assert erd.slabs == ["2026-01-01..2026-01-31"] and x.end_date == "2026-01-31"
  assert x.end_datetime == "2026-01-31T12:00:00Z" and x.extent_end == "2026-04-10T12:00:00Z"


# -- provenance -------------------------------------------------------------------------------------------------------
def test_the_provenance_has_the_ts_keys_in_order_and_the_ts_sql(tmp_path):
  x = run(tmp_path, FakeErddap())
  p = stats_provenance(x, generated="2026-10-22T00:00:00Z")
  assert list(p) == ["place_id", "dataset_id", "variable", "categorical", "start_date", "end_date", "start_datetime",
                     "end_datetime", "record_start", "lobes", "mask_cells", "axes", "griddap_urls",
                     "griddap_url_templates", "griddap_chunks", "griddap_requests", "rows", "bytes", "generated",
                     "erddap_base", "erddap_version", "sql"]
  assert p["sql"] == ts_sql("stats_daily", 's."sst"')
  assert "FROM (SELECT DISTINCT * FROM read_parquet([<one griddap slab per lobe for the chunk>])) s" in p["sql"]
  assert p["bytes"] == x.bytes and p["record_start"] == "2026-01-01"
  assert p["axes"][0] == {"bbox": list(x.axes[0]["bbox"]), "lon": [10.5, 11.5, 12.5], "lat": [21.5, 20.5]}


def test_the_provenance_leaves_out_a_missing_erddap_version(tmp_path):
  # JSON.stringify drops `erddap_version: ds.version` when the dataset has none
  from dataclasses import replace
  x = run(tmp_path, FakeErddap())
  assert "erddap_version" not in stats_provenance(replace(x, dataset=replace(x.dataset, version=None)))


def test_the_provenance_text_is_json_stringify_with_two_spaces():
  txt = provenance_text({"a": [-180.0, 1.5], "b": "x"})
  assert txt == '{\n  "a": [\n    -180,\n    1.5\n  ],\n  "b": "x"\n}\n' and json.loads(txt)["a"] == [-180, 1.5]


# -- download --------------------------------------------------------------------------------------------------------
def test_a_download_is_retried_once_then_names_its_url(tmp_path):
  calls, sleeps = [], []

  def bad(u):
    calls.append(u)
    raise ErddapError("404 Not Found from ERDDAP: nope")
  with pytest.raises(ErddapError, match="griddap failed twice: https://e.org/x"):
    download("https://e.org/x", tmp_path / "x.parquet", bad, sleep=sleeps.append)
  assert len(calls) == 2 and sleeps == [5]


def test_a_cached_slab_is_reused(tmp_path):
  f = tmp_path / "x.parquet"
  f.write_bytes(b"abc")
  assert download("https://e.org/x", f, lambda u: pytest.fail("fetched")) == 3


def test_without_chunks_the_window_is_required():
  with pytest.raises(ValueError, match="t0 and t1 are required"):
    extract(collection(), PID, wkb("square"), "sst")


# -- regressions found by the parity runs (2026-10-22) -------------------------------------------------------------------
def test_a_chunk_end_on_a_missing_axis_day_does_not_give_the_next_day_twice(tmp_path):
  # dhw_5km lacks 2024-07-25: the chunk ..._2024-07-25 was answered up to 2024-07-26, the next chunk's first day, and
  # the two rows of 2024-07-26 differed in the last bits, so the runner's DISTINCT kept both (GFNMS, HIHWNMS, MBNMS,
  # PMNM). Here the axis lacks 2026-01-10; the first chunk is answered up to 01-11 with values 1e-6 off the second's
  erd = FakeErddap(end="2026-01-20T12:00:00Z", missing=("2026-01-10",),
                   request_value=lambda asked, d, la, lo: doy(d, la, lo) + (1e-6 if asked == "2026-01-01" else 0.0))
  x = run(tmp_path, erd, existing=["2026-01-01_2026-01-10.parquet", "2026-01-11_2026-01-20.parquet"])
  first = pq.read_table(Path(x.chunk_dir) / "2026-01-01_2026-01-10.parquet").to_pylist()
  assert first[-1]["date"].isoformat() == "2026-01-11"            # the snapped day is in the first chunk's rows
  rows = x.stats.to_pylist()
  days = [r["date"].isoformat() for r in rows]
  assert len(days) == 19 and len(set(days)) == 19 and "2026-01-10" not in days
  [r11] = [r for r in rows if r["date"].isoformat() == "2026-01-11"]
  assert r11["n"] == 6 and r11["mean"] == 11.0 and r11["min"] == 11.0   # the second chunk's row, the day's own chunk
  assert x.rows == 19


def test_two_engine_runs_of_one_slab_write_identical_bytes(tmp_path):
  # DuckDB's multi-threaded sum/avg/stddev add in a new order on every run (the runner's published sums are one draw);
  # the engine runs DuckDB with one thread so its own runs are reproducible to the byte. 90 days x 2,000 cells of
  # irrational values: enough rows for a parallel plan to reorder the sums
  import math as _m
  geom = MultiPolygon([box(10.1, 20.1, 59.9, 59.9)])            # 50 x 40 = 2,000 cells
  def val(d, la, lo):
    return _m.sin(la * 12.9898 + lo * 78.233 + int(d[8:10])) * 43758.5453 % 1 + 20

  def once(sub):
    return run(tmp_path / sub, FakeErddap(end="2026-03-31T12:00:00Z", value=val), place=geom, pid="TEST:big")
  a, b = once("a"), once("b")
  assert a.rows == 90 and a.chunk_days == 50
  assert Path(a.stats_path).read_bytes() == Path(b.stats_path).read_bytes()
  for (sa, ea) in a.chunks_at:
    n = f"{sa[:10]}_{ea[:10]}.parquet"
    assert (Path(a.chunk_dir) / n).read_bytes() == (Path(b.chunk_dir) / n).read_bytes()


# -- coarser grids than daily (the sanctuaries series, ledger week 6, 2026-10-24) -----------------------------------------
def monthly_steps(y0: int, y1: int) -> list[str]:
  """The first of every month at 00:00Z, as IMERG_monthly_global_precip's axis."""
  return [f"{y}-{m:02d}-01T00:00:00Z" for y in range(y0, y1 + 1) for m in range(1, 13)]


def eight_day_steps(first: str, n: int) -> list[str]:
  """`n` steps 8 days apart at 12:00Z, as noaa_aoml_seascapes_8day's axis."""
  from datetime import date, timedelta
  d = date.fromisoformat(first)
  return [f"{(d + timedelta(days=8 * i)).isoformat()}T12:00:00Z" for i in range(n)]


def test_a_monthly_step_two_chunks_snap_onto_is_kept_once_from_the_chunk_that_holds_it(tmp_path):
  # IMERG: 90 monthly steps a request (2,739 days at 30.4375 d a step); the first chunk ends on 2007-07-01, a step, asked
  # at 12:00Z; the second starts 2007-07-02T12:00Z, which ERDDAP snaps back to the 2007-07-01T00:00Z step, so both
  # chunks carry that month, and only the first chunk's span holds it
  steps = monthly_steps(2000, 2009)
  erd = FakeErddap(steps=steps, spacing="30 days 10h 30m 0s")
  x = run(tmp_path, erd)
  assert x.chunk_days == 2739
  assert sorted(erd.slabs) == ["2000-01-01..2007-07-01", "2007-07-02..2009-12-01"]
  second = pq.read_table(Path(x.chunk_dir) / "2007-07-02_2009-12-01.parquet").to_pylist()
  assert second[0]["date"].isoformat() == "2007-07-01"                 # the snapped month is in the second chunk's rows
  rows = x.stats.to_pylist()
  assert x.rows == 120 and len(rows) == 120
  assert [r["date"].isoformat() for r in rows] == [s[:10] for s in steps]
  assert [r["date"].isoformat() for r in rows].count("2007-07-01") == 1
  p = stats_provenance(x)
  assert p["griddap_chunks"] == [["2000-01-01T00:00:00Z", "2007-07-01T12:00:00Z"],
                                 ["2007-07-02T12:00:00Z", "2009-12-01T00:00:00Z"]]


def test_an_8_day_step_snapped_into_the_next_chunk_is_kept_once(tmp_path):
  # seascapes: 90 steps of 8 days = 720 days a request; the first chunk ends 2004-08-17, asked at 12:00Z, which ERDDAP
  # snaps forward to the 2004-08-18 step, the second chunk's first day: that step is kept from the second chunk only
  steps = eight_day_steps("2002-08-29", 100)
  erd = FakeErddap(steps=steps, spacing="8 days")
  x = run(tmp_path, erd)
  assert x.chunk_days == 720
  assert sorted(erd.slabs) == ["2002-08-29..2004-08-17", "2004-08-18..2004-10-29"]
  first = pq.read_table(Path(x.chunk_dir) / "2002-08-29_2004-08-17.parquet").to_pylist()
  assert first[-1]["date"].isoformat() == "2004-08-18"                 # outside the first chunk's span
  rows = x.stats.to_pylist()
  assert x.rows == 100 and [r["date"].isoformat() for r in rows] == [s[:10] for s in steps]
