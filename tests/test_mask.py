"""om_extract.mask: the rules of gridMask.ts one at a time, on small synthetic fixtures.

The full parity (every row and weight of the TypeScript's own output on the sanctuary polygons) is in
test_mask_parity.py; these pin the individual rules so a break says which one."""
import gzip
import json
import math
import random
from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest

from om_extract._v8math import fma_exact, v8_sin
from om_extract.mask import (MaskTable, axis_edges, clip_polygon, grid_mask, polygon_area, reduce_ring,
                             reduce_rings, ring_area)

FIX = Path(__file__).parent / "fixtures" / "mask"


def sq(x0, y0, x1, y1):
  return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def poly(*rings):
  return {"type": "Polygon", "coordinates": [list(r) for r in rings]}


def wavy_ring(n, cx=0.013, cy=0.007, r=1.5):
  """gridMask.test.ts ring(): a circle with a 0.01 degree ripple, closed."""
  pts = []
  for i in range(n):
    a = (2 * math.pi * i) / n
    rr = r + 0.01 * math.sin(a * 997)
    pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
  pts.append(pts[0])
  return pts


# ── axes ──────────────────────────────────────────────────────────────────────
def test_axis_edges_midpoints_and_extrapolated_ends():
  assert axis_edges([0, 1, 2]) == [-0.5, 0.5, 1.5, 2.5]


def test_axis_edges_descending_and_irregular():
  assert axis_edges([10, 6, 4]) == [12, 8, 5, 3]
  assert axis_edges([0, 1, 3, 7]) == [-0.5, 0.5, 2, 5, 9]


def test_axis_edges_single_value_assumes_unit_spacing():
  assert axis_edges([5]) == [4.5, 5.5]
  assert axis_edges([]) == []


def test_latitude_order_does_not_change_the_rows():
  g = poly(sq(0.1, 0.1, 2.7, 1.6))
  lon, lat = [0.5, 1.5, 2.5], [0.25, 0.75, 1.25, 1.75]
  a, b = grid_mask(g, lat, lon), grid_mask(g, lat[::-1], lon)
  assert a == b and len(a) == 12


def test_lat_descending_is_checked_not_applied():
  g = poly(sq(0, 0, 1, 1))
  assert len(grid_mask(g, [0.75, 0.25], [0.25, 0.75], lat_descending=True)) == 4
  with pytest.raises(ValueError, match="lat_descending=False"):
    grid_mask(g, [0.75, 0.25], [0.25, 0.75], lat_descending=False)


# ── candidates, method, centres ───────────────────────────────────────────────
def test_auto_picks_turf_for_a_small_place():
  # gridMask.test.ts: the unit square on a 3 x 3 lattice, 9 centres inside
  m = grid_mask(poly(sq(0, 0, 1, 1)), [0.75, 0.5, 0.25], [0.25, 0.5, 0.75], weights=False)
  assert (m.method, m.n_inside, m.n_candidates) == ("turf", 9, 9)


def test_auto_picks_scan_at_5000_vertices():
  ring = wavy_ring(5_000, r=0.3)  # 5,001 positions
  m = grid_mask(poly(ring), [0.0], [0.0], weights=False)
  assert m.method == "scan"
  assert grid_mask(poly(wavy_ring(4_000, r=0.3)), [0.0], [0.0], weights=False).method == "turf"


def test_scan_and_turf_agree_on_centres():
  g = poly(wavy_ring(600, r=1.2), [(p[0] * 0.3, p[1] * 0.3) for p in wavy_ring(80, r=1.0)][::-1])
  ax = [round(-1.6 + 0.1 * k, 3) for k in range(33)]
  s, t = grid_mask(g, ax, ax, method="scan", weights=False), grid_mask(g, ax, ax, method="turf", weights=False)
  assert s.n_inside == t.n_inside > 0
  assert s.lon.tolist() == t.lon.tolist() and s.lat.tolist() == t.lat.tolist()


def test_centre_on_the_boundary_is_inside():
  # turf booleanPointInPolygon counts the boundary as inside (point-in-polygon-hao returns 0 there)
  m = grid_mask(poly(sq(0, 0, 1, 1)), [0.5], [1.0], method="turf", weights=False)
  assert m.n_inside == 1


def test_unweighted_rows_are_the_inside_centres_with_weight_1():
  m = grid_mask(poly(sq(0.3, 0.3, 1.7, 0.7)), [0.5], [0.5, 1.5, 2.5], weights=False)
  assert m.lon.tolist() == [0.5, 1.5] and m.weight.tolist() == [1.0, 1.0]


def test_empty_inputs_give_an_empty_mask():
  assert len(grid_mask({"type": "Point", "coordinates": (0, 0)}, [0.5], [0.5])) == 0
  assert len(grid_mask(poly(sq(0, 0, 1, 1)), [], [0.5])) == 0


def test_bad_method_raises():
  with pytest.raises(ValueError, match="method"):
    grid_mask(poly(sq(0, 0, 1, 1)), [0.5], [0.5], method="raster")


# ── weights ───────────────────────────────────────────────────────────────────
def test_weight_is_geodesic_overlap_over_cell_area():
  # a square covering the left half of one 1 x 1 cell: the weight is the ratio of turf's spherical areas,
  # 0.5 on the plane; on the sphere the halves are equal too (same latitudes)
  m = grid_mask(poly(sq(0, 10, 0.5, 11)), [10.5], [0.5])
  assert len(m) == 1 and m.weight[0] == pytest.approx(0.5, abs=1e-12)
  # a cell of the same size nearer the pole has the same weight; the areas themselves shrink with cos(lat)
  assert abs(ring_area(sq(0, 60, 1, 61))) < 0.6 * abs(ring_area(sq(0, 10, 1, 11)))


def test_weights_include_outside_centre_neighbours():
  # the square reaches 0.3 into the cell east of the one holding its centre: that cell's centre is outside
  m = grid_mask(poly(sq(0.2, 0.2, 1.3, 0.8)), [0.5], [0.5, 1.5, 2.5])
  assert m.n_inside == 1 and m.lon.tolist() == [0.5, 1.5]
  assert 0 < m.weight[1] < m.weight[0] < 1


def test_polygon_smaller_than_a_cell_keeps_its_partial_cells():
  # gridMask.test.ts Gray's Reef: no centre inside, the cells overlapping the bbox get their fraction
  g = poly(sq(-80.95, 31.3, -80.85, 31.4))
  m = grid_mask(g, [31.25, 31.5], [-81, -80.75])
  assert m.n_inside == 0 and len(m) == 4
  assert float(m.weight.sum()) == pytest.approx(0.16, abs=5e-3)
  assert len(grid_mask(g, [31.25, 31.5], [-81, -80.75], weights=False)) == 0


def test_weight_is_capped_at_1():
  m = grid_mask(poly(sq(-5, -5, 5, 5)), [0.0], [0.0])
  assert m.weight.tolist() == [1.0]


def test_hole_is_subtracted():
  hole = sq(0.25, 0.25, 0.75, 0.75)[::-1]
  m = grid_mask(poly(sq(-1, -1, 2, 2), hole), [0.5], [0.5])
  assert m.n_inside == 0  # the centre sits in the hole: the cell is seeded from the part bbox
  assert m.weight[0] == pytest.approx(0.75, abs=1e-4)


def test_rows_sorted_by_lat_then_lon():
  m = grid_mask(poly(sq(0, 0, 3, 2)), [1.5, 0.5], [2.5, 1.5, 0.5])
  assert list(zip(m.lat.tolist(), m.lon.tolist())) == [(0.5, 0.5), (0.5, 1.5), (0.5, 2.5), (1.5, 0.5), (1.5, 1.5),
                                                       (1.5, 2.5)]


def test_to_arrow_is_the_sql_mask_table():
  m = grid_mask(poly(sq(0, 0, 1, 1)), [0.5], [0.5])
  t = m.to_arrow()
  assert t.column_names == ["latitude", "longitude", "weight"]
  assert t.schema.field("weight").type == pa.float64()
  assert isinstance(m, MaskTable) and len(m) == 1


# ── reduceRing (the TS cases) ─────────────────────────────────────────────────
def test_reduce_ring_keeps_short_ring_drops_far_ring_keeps_surrounding_ring():
  square = np.array(sq(0, 0, 1, 1), dtype=float)
  assert reduce_ring(square, (0.2, 0.2, 0.3, 0.3)) is square  # under 8 vertices: untouched
  assert reduce_ring(np.array(wavy_ring(100, 10, 10, 1)), (0, 0, 0.1, 0.1)) is None
  around = reduce_ring(np.array(wavy_ring(100, 0, 0, 1)), (-0.1, -0.1, 0.1, 0.1))
  assert around is not None and len(around) < 100
  r = (-0.1, -0.1, 0.1, 0.1)
  clipped = polygon_area(clip_polygon([around.tolist()], r))
  assert clipped / abs(ring_area(sq(-0.1, -0.1, 0.1, 0.1))) == pytest.approx(1, abs=1e-9)


def test_reduce_ring_never_changes_the_clipped_area():
  # gridMask.test.ts: an outer ring and a hole, 40 rectangles; the TS asserts 3 decimals, the port is exact
  rings = [np.array(wavy_ring(5_000)), np.array(wavy_ring(400, 0.2, 0.1, 0.4)[::-1])]
  for k in range(40):
    x0, y0 = -1.8 + (k % 8) * 0.45, -1.8 + (k // 8) * 0.7
    r = (x0, y0, x0 + 0.05, y0 + 0.05)
    red = reduce_rings(rings, r)
    a0 = polygon_area(clip_polygon([x.tolist() for x in rings], r))
    a1 = polygon_area(clip_polygon([x.tolist() for x in red], r)) if red else 0.0
    assert a1 == a0
    if red:
      assert len(red[0]) < len(rings[0])


# ── V8 Math.sin ───────────────────────────────────────────────────────────────
def test_v8_sin_matches_node_bit_for_bit():
  d = json.loads(gzip.decompress((FIX / "v8_sin.json.gz").read_bytes()))
  assert [v8_sin(x) for x in d["x"]] == d["sin"]
  # and the platform libm does not, which is why the port exists
  assert sum(math.sin(x) != s for x, s in zip(d["x"], d["sin"])) > 0 or d["generator"]["arch"] != "arm64"


def test_v8_sin_refuses_outside_its_range():
  with pytest.raises(ValueError):
    v8_sin(3.0)


def test_fma_exact_is_a_single_rounding():
  rnd = random.Random(7)
  for _ in range(2_000):
    a, b, c = rnd.uniform(-2, 2), rnd.uniform(-2, 2), rnd.uniform(-1, 1)
    want = math.fma(a, b, c) if hasattr(math, "fma") else None
    got = fma_exact(a, b, c)
    if want is not None:
      assert got == want
  # 0.1 * 10 - 1 is 5.55e-17 exactly once rounded, 0 when the product is rounded first
  assert fma_exact(0.1, 10.0, -1.0) == 5.551115123125783e-17
