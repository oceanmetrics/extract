"""Chunking and the live time extent, ported from precompute.test.ts, extent.test.ts and the fixture of erddap-places."""
import json
from pathlib import Path

import pytest

from om_extract.readers.erddap import (MAX_CHUNK_DAYS, MAX_REQUEST_VALUES, TimeExtent, add_days, chunk_days_for, chunks,
                                       clamp_window, days_between, default_window, noon_z, parse_info_extent, parse_spacing,
                                       span_days, step_label, time_instant)

INFO = json.loads((Path(__file__).parent / "data" / "info_noaa_aoml_seascapes_8day.json").read_text())
# the 8-day Seascapes grid as of 2026-09-15: months of latency, so "today - 2" is far past its end
SEASCAPES = TimeExtent("2002-08-29T12:00:00Z", "2026-06-26T12:00:00Z", 8.0055, "8-day", 1088)
DAILY = TimeExtent("1985-04-01T12:00:00Z", "2026-09-13T12:00:00Z", 1, "daily")


# -- chunk_days_for (precompute.test.ts) -------------------------------------------------------------------------------
def test_chunk_days_for_caps_at_max_chunk_days_for_a_small_place():
  assert chunk_days_for(6) == MAX_CHUNK_DAYS  # Gray's Reef: 6 cells a step


def test_chunk_days_for_shrinks_so_one_request_stays_under_100k_values():
  assert chunk_days_for(1_200) == 83  # a Monterey-sized box
  assert chunk_days_for(39_026) == 2  # a 39 k-cell bbox
  assert chunk_days_for(39_026) * 39_026 <= MAX_REQUEST_VALUES


def test_chunk_days_for_never_goes_below_a_single_step_however_big_the_bbox():
  assert chunk_days_for(50_000_000) == 1
  assert chunk_days_for(101_000) == 1  # NMS:PMNM's main lobe: one day a request


def test_chunk_days_for_counts_an_8_day_product_in_days_not_steps():
  assert chunk_days_for(25_000, 8) == 32  # 4 steps x 8 days
  assert chunk_days_for(250_000, 8) == 8  # one step


def test_chunk_days_for_counts_up_to_max_chunk_days_time_steps_for_a_monthly_product():
  assert chunk_days_for(100, 30) == MAX_CHUNK_DAYS * 30  # 90 months in one request


def test_chunk_days_for_still_shrinks_a_monthly_product_under_the_value_cap():
  assert chunk_days_for(5_000, 30) == 600  # 20 steps x 30 days


def test_chunk_days_for_takes_the_caps_as_arguments_and_treats_unknown_cells_as_the_cap():
  assert chunk_days_for(1_000, 1, max_values=5_000, max_days=30) == 5
  assert chunk_days_for(float("nan")) == MAX_CHUNK_DAYS
  assert chunk_days_for(0) == MAX_CHUNK_DAYS


def test_chunks_cover_the_window_exactly_once_consecutive_non_overlapping_no_gaps():
  assert chunks("2026-01-01", "2026-01-10", 4) == [("2026-01-01", "2026-01-04"), ("2026-01-05", "2026-01-08"),
                                                   ("2026-01-09", "2026-01-10")]


def test_chunks_is_a_single_chunk_when_the_window_fits():
  assert chunks("2026-01-01", "2026-01-10", 90) == [("2026-01-01", "2026-01-10")]


def test_chunks_handles_a_one_day_window():
  assert chunks("2026-01-01", "2026-01-01", 90) == [("2026-01-01", "2026-01-01")]


# -- parse_info_extent (extent.test.ts) --------------------------------------------------------------------------------
def test_parse_info_extent_reads_actual_range_and_the_average_spacing_of_seascapes():
  e = parse_info_extent(INFO)
  assert e.start == "2002-08-29T12:00:00Z"
  assert e.end == "2026-06-26T12:00:00Z"  # not today - 2
  assert e.n_values == 1088
  assert e.step_days == pytest.approx(8.006, abs=0.01)
  assert e.step_label == "8-day"


def test_parse_info_extent_falls_back_to_the_global_coverage_attributes_when_actual_range_is_missing():
  rows = [r for r in INFO["table"]["rows"] if r[2] != "actual_range"]
  e = parse_info_extent({"table": {"columnNames": INFO["table"]["columnNames"], "rows": rows}})
  assert (e.start, e.end) == ("2002-08-29T12:00:00Z", "2026-06-26T12:00:00Z")


def test_parse_info_extent_reverses_a_descending_actual_range_and_returns_none_without_one():
  rows = [["attribute", "time", "actual_range", "double", "1.7824752E9, 1.0306224E9"]]
  assert parse_info_extent({"table": {"columnNames": [], "rows": rows}}).end == "2026-06-26T12:00:00Z"
  assert parse_info_extent({"table": {"columnNames": [], "rows": []}}) is None
  assert parse_info_extent({}) is None


def test_parse_spacing_and_step_label():
  assert parse_spacing("8 days 0h 7m 57s") == pytest.approx(8.0055, abs=1e-3)
  assert parse_spacing("1 day") == 1
  assert parse_spacing("12h") == pytest.approx(0.5)
  assert parse_spacing("") is None
  assert step_label(1) == "daily"
  assert step_label(8.0055) == "8-day"
  assert step_label(30.4) == "monthly"
  assert step_label(7) == "weekly"
  assert step_label(365) == "annual"
  assert step_label(0.5) == "sub-daily"
  assert step_label(None) is None
  assert step_label(2.5) == "3-day"  # javascript's Math.round, half up


# -- the date window (extent.test.ts) ----------------------------------------------------------------------------------
def test_default_window_is_the_last_30_days_ending_at_the_last_step_for_a_daily_dataset():
  assert default_window(DAILY) == ("2026-08-15", "2026-09-13")
  assert span_days(DAILY) == 30


def test_default_window_widens_to_cover_several_steps_of_a_coarser_dataset_capped_at_max_days():
  start, end = default_window(SEASCAPES)
  assert end == "2026-06-26"  # the dataset's last step, not today - 2
  assert days_between(start, end) + 1 == span_days(SEASCAPES) == 65  # 8 steps x 8.006 days, ceil
  assert span_days(TimeExtent(SEASCAPES.start, SEASCAPES.end, 30)) == 90


def test_default_window_never_starts_before_the_first_time_step():
  short = TimeExtent("2026-09-01T12:00:00Z", "2026-09-05T12:00:00Z", 1)
  assert default_window(short) == ("2026-09-01", "2026-09-05")


def test_clamp_window_snaps_back_when_the_start_is_after_the_dataset_end_and_says_why():
  s, e, snapped, reason = clamp_window("2026-08-15", "2026-09-13", SEASCAPES)
  assert snapped and e == "2026-06-26"
  assert "starts after this dataset ends (2026-06-26)" in reason


def test_clamp_window_snaps_back_when_the_window_ends_before_the_dataset_starts():
  _, _, snapped, reason = clamp_window("1990-01-01", "1990-02-01", SEASCAPES)
  assert snapped and "ends before this dataset starts (2002-08-29)" in reason


def test_clamp_window_clamps_an_overlapping_window_to_the_extent():
  s, e, snapped, reason = clamp_window("2026-06-01", "2026-08-01", SEASCAPES)
  assert (s, e, snapped) == ("2026-06-01", "2026-06-26", True)
  assert "clamped to the dataset extent" in reason


def test_clamp_window_leaves_a_window_inside_the_extent_alone():
  assert clamp_window("2026-05-01", "2026-06-01", SEASCAPES) == ("2026-05-01", "2026-06-01", False, None)


def test_clamp_window_does_nothing_without_an_extent():
  assert clamp_window("2026-05-01", "2026-06-01", None)[2] is False


def test_add_days_and_days_between_are_utc_calendar_days():
  assert add_days("2026-02-28", 1) == "2026-03-01"  # 2026 is not a leap year
  assert days_between("2026-06-26T12:00:00Z", "2026-06-27") == 1


def test_time_instant_uses_the_extent_endpoints_verbatim_so_a_request_cannot_overshoot_a_non_noon_axis():
  mur = TimeExtent("2002-06-01T09:00:00Z", "2026-09-12T09:00:00Z", 1)
  assert time_instant("2026-09-12", mur, noon_z) == "2026-09-12T09:00:00Z"
  assert time_instant("2002-06-01", mur, noon_z) == "2002-06-01T09:00:00Z"
  assert time_instant("2026-09-01", mur, noon_z) == "2026-09-01T12:00:00Z"
  assert time_instant("2026-09-01", None, noon_z) == "2026-09-01T12:00:00Z"
