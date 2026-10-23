"""load_membership() over a synthetic gazetteer membership tree in the published layout (`fixtures_h3`). No network."""
import pytest

from om_extract.membership import SCHEMA, load_membership, membership_files, membership_manifest
from fixtures_h3 import P3_A, P3_B, R5, R5_OUT, R7_A, R7_B, membership_tree

ROWS = {5: [("TEST:a", R5[0], 1.0), ("TEST:a", R5[1], 0.5), ("TEST:a", R5[2], 0.25), ("TEST:b", R5_OUT, 1.0)],
        7: [("TEST:a", R7_A[0], 1.0), ("TEST:a", R7_A[1], 0.5), ("TEST:b", R7_B[0], 0.75)]}


@pytest.fixture
def base(tmp_path):
  return membership_tree(tmp_path, ROWS, {7: 3})


def test_an_unsplit_resolution_is_the_one_file_filtered_to_the_place(base):
  t = load_membership("places", "TEST:a", 5, base)
  assert t.schema == SCHEMA
  assert [(r["h3"], r["share"]) for r in t.to_pylist()] == [(R5[0], 1.0), (R5[1], 0.5), (R5[2], 0.25)]
  assert set(t["place_id"].to_pylist()) == {"TEST:a"}


def test_a_split_resolution_reads_only_the_places_parent_partitions_from_parents_parquet(base, tmp_path):
  m = membership_manifest("places", base)
  root = str(tmp_path / "places")
  assert membership_files(m, "TEST:a", 7, root) == [str(tmp_path / f"places/h3/res=7/p={P3_A}/membership.parquet")]
  # TEST:b's partition is made unreadable: TEST:a never opens it
  (tmp_path / f"places/h3/res=7/p={P3_B}/membership.parquet").write_bytes(b"not parquet")
  t = load_membership("places", "TEST:a", 7, base)
  assert t["h3"].to_pylist() == R7_A and t["share"].to_pylist() == [1.0, 0.5]


def test_a_place_with_no_cells_at_a_resolution_is_an_empty_table(base):
  t = load_membership("places", "TEST:none", 7, base)
  assert t.num_rows == 0 and t.schema == SCHEMA
  assert load_membership("places", "TEST:none", 5, base).num_rows == 0


def test_a_resolution_the_manifest_lacks_or_a_missing_manifest_is_named(base, tmp_path):
  with pytest.raises(ValueError, match=r"no H3 membership at res 6; the manifest has \[5, 7\]"):
    load_membership("places", "TEST:a", 6, base)
  with pytest.raises(FileNotFoundError, match="no H3 membership manifest at .*mr_eez/h3/membership.json"):
    load_membership("mr_eez", "TEST:a", 5, base)


def test_the_default_base_is_the_public_gazetteer_url():
  seen = []
  def fetch(url):
    seen.append(url)
    return None
  with pytest.raises(FileNotFoundError):
    membership_manifest("places", fetch=fetch)
  assert seen == ["https://storage.oceanmetrics.io/gazetteer/places/h3/membership.json"]
