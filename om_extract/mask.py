"""The grid mask: which cells of an ERDDAP axis lattice belong to a place, and with what weight.

A port of erddap-places `app/src/lib/gridMask.ts` `gridMask()` (turf 7.4.0 underneath) that returns the same
rows in the same order with the same float64 weights, bit for bit (tests/test_mask_parity.py measures that
against output written by the TypeScript itself). Each rule below names the TS it mirrors.

The rule (README.md "The rule"): a cell belongs to a place by its CENTRE, and every cell the polygon touches
gets a partial-area weight in (0, 1] = (geodesic area of the polygon clipped to the cell) / (geodesic area of
the cell), both areas by turf's spherical ring-area formula on a sphere of radius 6,371,008.8 m.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

import numpy as np

from ._v8math import v8_sin
from .geom import Rings, part_bbox, parts_of

# turf/helpers earthRadius; turf/area FACTOR and PI_OVER_180, computed as turf computes them
EARTH_RADIUS = 6371008.8
_FACTOR = EARTH_RADIUS * EARTH_RADIUS / 2
_PI_OVER_180 = math.pi / 180
_INF = float("inf")

# robust-predicates orient2d: epsilon = 2^-53, ccwerrboundA = (3 + 16 eps) eps
_EPS = 1.1102230246251565e-16
_CCWERRBOUND_A = (3 + 16 * _EPS) * _EPS


# ── result ────────────────────────────────────────────────────────────────────
@dataclass(eq=False)
class MaskTable:
  """The mask of one lobe on one grid (gridMask.ts `MaskResult`).

  `lon`, `lat`, `weight` are the rows of `MaskResult.cells` (one per included cell, sorted by lat then lon);
  `n_candidates`, `n_inside`, `method`, `ms` are `nCandidates`, `nInside`, `method`, `ms`. `to_arrow()` gives
  the `mask` table the SQL templates join on (`latitude`, `longitude`, `weight`, as engine.ts `insertMask()`
  and precompute `insertMask()` build it)."""
  lon: np.ndarray
  lat: np.ndarray
  weight: np.ndarray
  n_candidates: int = 0
  n_inside: int = 0
  method: str = "scan"
  ms: float = field(default=0.0, compare=False)

  def __len__(self) -> int:
    return len(self.weight)

  def __eq__(self, other: object) -> bool:
    """same rows (exact float equality) and the same counts and method; `ms` is not compared."""
    if not isinstance(other, MaskTable):
      return NotImplemented
    return (self.lon.tolist() == other.lon.tolist() and self.lat.tolist() == other.lat.tolist()
            and self.weight.tolist() == other.weight.tolist()
            and (self.n_candidates, self.n_inside, self.method) == (other.n_candidates, other.n_inside, other.method))

  def to_arrow(self):
    import pyarrow as pa

    return pa.table({"latitude": pa.array(self.lat, pa.float64()), "longitude": pa.array(self.lon, pa.float64()),
                     "weight": pa.array(self.weight, pa.float64())})


# ── axis geometry (gridMask.ts axisEdges / prepAxis / cellIndex) ──────────────
def axis_edges(v) -> list[float]:
  """Cell edges of a monotonic axis: midpoints between neighbours, the two ends extrapolated by half the end
  spacing; a single value gets +-0.5 (gridMask.ts `axisEdges()`, lines 29-38)."""
  v = [float(x) for x in v]
  n = len(v)
  if n == 0:
    return []
  if n == 1:
    return [v[0] - 0.5, v[0] + 0.5]
  e = [0.0] * (n + 1)
  for i in range(1, n):
    e[i] = (v[i - 1] + v[i]) / 2
  e[0] = v[0] - (v[1] - v[0]) / 2
  e[n] = v[n - 1] + (v[n - 1] - v[n - 2]) / 2
  return e


class _Axis:
  """gridMask.ts `prepAxis()` (lines 49-58): ascending values, lower/upper edges, the spacing when uniform."""
  __slots__ = ("val", "lo", "hi", "uniform", "min", "max", "n")

  def __init__(self, v):
    v = [float(x) for x in v]
    desc = len(v) > 1 and v[1] < v[0]
    self.val = v[::-1] if desc else v
    e = axis_edges(self.val)
    self.lo, self.hi = e[:-1], e[1:]
    val = self.val
    uniform = val[1] - val[0] if len(val) > 1 else 0.0
    for i in range(2, len(val)):
      if abs(val[i] - val[i - 1] - uniform) > abs(uniform) * 1e-6:
        uniform = 0.0
        break
    self.uniform = uniform
    self.n = len(val)
    self.min = e[0] if e else 0.0
    self.max = e[-1] if e else 0.0

  def cell_index(self, x: float) -> int:
    """index of the cell containing x, or -1; uniform axes by division, others by binary search (cellIndex())."""
    if x < self.min or x > self.max:
      return -1
    if self.uniform:
      i = math.floor((x - self.min) / self.uniform)
      return 0 if i < 0 else self.n - 1 if i >= self.n else i
    lo, hi = 0, self.n - 1
    h = self.hi
    while lo < hi:
      m = (lo + hi) >> 1
      if x > h[m]:
        lo = m + 1
      else:
        hi = m
    return lo

  def lower_index(self, x: float) -> int:
    """smallest index with hi[i] >= x (lowerIndex())."""
    if x <= self.min:
      return 0
    if x > self.max:
      return self.n
    return self.cell_index(x)

  def upper_index(self, x: float) -> int:
    """upperIndex()."""
    if x >= self.max:
      return self.n - 1
    if x < self.min:
      return -1
    return self.cell_index(x)


# ── polygon parts (gridMask.ts polygonParts) ──────────────────────────────────
class _Part:
  __slots__ = ("rings", "bbox", "np_rings", "n_outer", "ring_box")

  def __init__(self, rings: Rings):
    self.rings = rings
    self.bbox = part_bbox(rings)
    self.np_rings = [np.asarray(r, dtype=np.float64).reshape(-1, 2) for r in rings]
    self.n_outer = len(rings[0])
    # every ring's own [xmin, ymin, xmax, ymax], by identity of its array (see _clip_is_empty)
    self.ring_box = {id(r): (float(r[:, 0].min()), float(r[:, 1].min()), float(r[:, 0].max()), float(r[:, 1].max()))
                     for r in self.np_rings}


def count_vertices(parts: list[Rings]) -> int:
  """gridMask.ts `countVertices()`: every position of every ring, closing positions included."""
  return sum(len(r) for p in parts for r in p)


# ── point in polygon (turf booleanPointInPolygon -> point-in-polygon-hao 1.2.4) ─
def _orient_sign(u1: float, u2: float, v1: float, v2: float) -> float:
  """the sign of robust-predicates `orient2d(u1, u2, v1, v2, 0, 0)` = u2*v1 - u1*v2 (exact when it matters)."""
  detleft = u2 * v1
  detright = u1 * v2
  det = detleft - detright
  if abs(det) >= _CCWERRBOUND_A * abs(detleft + detright):
    return det
  ex = Fraction(u2) * Fraction(v1) - Fraction(u1) * Fraction(v2)
  return 1.0 if ex > 0 else -1.0 if ex < 0 else 0.0


def _point_in_part(x: float, y: float, part: _Part) -> bool:
  """turf `booleanPointInPolygon([x, y], polygon(rings))` with the boundary counted inside: point-in-polygon-hao's
  crossing count over every ring, vectorised over the edges (a boundary hit anywhere returns 0 = inside in the
  JS, and the crossing count is a sum, so the edge order does not change the answer)."""
  k = 0
  for ring in part.np_rings:
    n = len(ring) - 1
    if ring[0, 0] != ring[n, 0] and ring[0, 1] != ring[n, 1]:
      raise ValueError("First and last coordinates in a ring must be the same")
    u = ring[:, 0] - x
    v = ring[:, 1] - y
    u1, v1, u2, v2 = u[:-1], v[:-1], u[1:], v[1:]
    flat = (v1 == 0) & (v2 == 0)
    if np.any(flat & (((u2 <= 0) & (u1 >= 0)) | ((u1 <= 0) & (u2 >= 0)))):
      return True
    span = ~flat & (((v2 >= 0) & (v1 <= 0)) | ((v2 <= 0) & (v1 >= 0)))
    if not np.any(span):
      continue
    su1, su2, sv1, sv2 = u1[span], u2[span], v1[span], v2[span]
    detleft = su2 * sv1
    detright = su1 * sv2
    f = detleft - detright
    unsure = ~(np.abs(f) >= _CCWERRBOUND_A * np.abs(detleft + detright))
    if np.any(unsure):
      f = f.copy()
      for i in np.nonzero(unsure)[0]:
        f[i] = _orient_sign(float(su1[i]), float(su2[i]), float(sv1[i]), float(sv2[i]))
    if np.any(f == 0):
      return True
    k += int(np.count_nonzero(((f > 0) & (sv2 > 0) & (sv1 <= 0)) | ((f < 0) & (sv2 <= 0) & (sv1 > 0))))
  return k % 2 == 1


# ── masking: candidates, turf, scan (gridMask.ts lines 117-176) ───────────────
def _index_range(ax: _Axis, lo: float, hi: float) -> list[int]:
  """the indices whose centre lies in [lo, hi], scanned exactly as candidates() scans them."""
  i0, i1 = ax.lower_index(lo), ax.upper_index(hi)
  val = ax.val
  return [i for i in range(i0, i1 + 1) if not (i < 0 or i >= ax.n or val[i] < lo or val[i] > hi)]


def _candidates(ps: list[_Part], ax: _Axis, ay: _Axis) -> np.ndarray:
  """candidates(): the union over parts of the cells whose centre lies in that part's bbox, as a bool grid [i, j]."""
  grid = np.zeros((ax.n, ay.n), dtype=bool)
  for p in ps:
    x0, y0, x1, y1 = p.bbox
    ii, jj = _index_range(ax, x0, x1), _index_range(ay, y0, y1)
    if ii and jj:
      grid[np.ix_(ii, jj)] = True
  return grid


def _mask_turf(ps: list[_Part], cands: np.ndarray, ax: _Axis, ay: _Axis) -> np.ndarray:
  """maskTurf(): turf point-in-polygon per candidate centre, with the part-bbox prefilter."""
  m = np.zeros_like(cands)
  for i, j in zip(*np.nonzero(cands)):
    x, y = ax.val[i], ay.val[j]
    for p in ps:
      x0, y0, x1, y1 = p.bbox
      if x < x0 or x > x1 or y < y0 or y > y1:
        continue
      if _point_in_part(x, y, p):
        m[i, j] = True
        break
  return m


def _mask_scan(ps: list[_Part], ax: _Axis, ay: _Axis) -> np.ndarray:
  """maskScan(): per grid row, the x-crossings of every ring of a part (`(ay > y) !== (by > y)`, crossing at
  `ax + (y - ay) * (bx - ax) / (by - ay)`), sorted, and the columns whose centre lies in each pair filled.
  The crossings are computed with numpy elementwise in the same operation order (so the same doubles)."""
  m = np.zeros((ax.n, ay.n), dtype=bool)
  for p in ps:
    _, y0, _, y1 = p.bbox
    edges = [(r[:-1, 0], r[:-1, 1], r[1:, 0], r[1:, 1]) for r in p.np_rings]
    exa = np.concatenate([e[0] for e in edges])
    eya = np.concatenate([e[1] for e in edges])
    exb = np.concatenate([e[2] for e in edges])
    eyb = np.concatenate([e[3] for e in edges])
    j0 = max(0, ay.lower_index(y0))
    j1 = min(ay.n - 1, ay.upper_index(y1))
    for j in range(j0, j1 + 1):
      y = ay.val[j]
      if y < y0 or y > y1:
        continue
      hit = (eya > y) != (eyb > y)
      if not hit.any():
        continue
      a_x, a_y, b_x, b_y = exa[hit], eya[hit], exb[hit], eyb[hit]
      xs = np.sort(a_x + (y - a_y) * (b_x - a_x) / (b_y - a_y)).tolist()
      for k in range(0, len(xs) - 1, 2):
        xa, xb = xs[k], xs[k + 1]
        i0 = ax.lower_index(xa)
        if i0 < ax.n and ax.val[i0] < xa:
          i0 += 1
        i1 = ax.upper_index(xb)
        if i1 >= 0 and ax.val[i1] > xb:
          i1 -= 1
        lo, hi = max(0, i0), min(ax.n - 1, i1)
        if lo <= hi:
          m[lo:hi + 1, j] = True
  return m


# ── reducing a big ring to one rectangle (gridMask.ts reduceRing, lines 184-216) ─
def _out_codes(x: np.ndarray, y: np.ndarray, r) -> np.ndarray:
  """outCode() vectorised: 1 left, 2 right, 4 below, 8 above."""
  cx = np.where(x < r[0], 1, np.where(x > r[2], 2, 0))
  cy = np.where(y < r[1], 4, np.where(y > r[3], 8, 0))
  return cx | cy


def reduce_ring(ring: np.ndarray, r) -> np.ndarray | None:
  """gridMask.ts `reduceRing()`: the ring with every run of vertices beyond ONE side of `r` collapsed, or None
  when nothing of it can reach `r`; a ring of fewer than 8 vertices (9 positions) is returned as is.

  The stack walk is the TS one. Before it, the interior of every run of three or more consecutive vertices
  with the same non-zero out-code is dropped: the walk would pop each of them on the next vertex (it pops the
  top while `oc[-2] & oc[-1] & c`), and what it keeps of the run, the first and last vertex or the last only,
  depends only on the vertex before the run, so the result is the same ring. That makes the per-band and
  per-cell reductions cost the vertices near the rectangle, as in the TS."""
  n = len(ring) - 1  # closed ring: the last position repeats the first
  if n < 8:
    return ring
  codes = _out_codes(ring[:n, 0], ring[:n, 1], r)
  c = codes[1:n]
  keep = np.ones(n - 1, dtype=bool)
  if n - 1 >= 3:
    mid = c[1:-1]
    keep[1:-1] = ~((mid != 0) & (c[:-2] == mid) & (c[2:] == mid))
  idx = (np.nonzero(keep)[0] + 1).tolist()
  cl = codes.tolist()
  out = [0]
  oc = [cl[0]]
  for i in idx:
    ci = cl[i]
    while len(out) >= 2 and (oc[-2] & oc[-1] & ci):
      out.pop()
      oc.pop()
    out.append(i)
    oc.append(ci)
  # the ring is a cycle: the tail can still collapse into the (kept) first vertex
  while len(out) >= 2 and (oc[-2] & oc[-1] & oc[0]):
    out.pop()
    oc.pop()
  if len(out) < 3:
    return None
  out.append(0)
  return ring[out]


def reduce_rings(rings: list[np.ndarray], r) -> list[np.ndarray] | None:
  """gridMask.ts `reduceRings()`: None when the outer ring cannot reach `r`; a hole that cannot is dropped."""
  outer = reduce_ring(rings[0], r)
  if outer is None:
    return None
  out = [outer]
  for h in rings[1:]:
    hr = reduce_ring(h, r)
    if hr is not None:
      out.append(hr)
  return out


# ── turf area and bboxClip (turf 7.4.0, exact port) ───────────────────────────
def _make_sin():
  memo: dict[float, float] = {}

  def sin(x: float) -> float:
    s = memo.get(x)
    if s is None:
      s = memo[x] = v8_sin(x)
    return s
  return sin


def ring_area(coords, sin=v8_sin) -> float:
  """turf/area `ringArea()`: sum of (upperX - lowerX) * sin(middleY) over the closed ring, times R^2 / 2."""
  n = len(coords) - 1
  if n <= 2:
    return 0.0
  total = 0.0
  p = _PI_OVER_180
  for i in range(n):
    lower = coords[i]
    middle = coords[0 if i + 1 == n else i + 1]
    upper = coords[(i + 2) % n if i + 2 >= n else i + 2]
    total += (upper[0] * p - lower[0] * p) * sin(middle[1] * p)
  return total * _FACTOR


def polygon_area(rings, sin=v8_sin) -> float:
  """turf/area `polygonArea()`: |outer| minus each |hole|."""
  total = 0.0
  if rings:
    total += abs(ring_area(rings[0], sin))
    for r in rings[1:]:
      total -= abs(ring_area(r, sin))
  return total


def _bit_code(x: float, y: float, b) -> int:
  code = 0
  if x < b[0]:
    code |= 1
  elif x > b[2]:
    code |= 2
  if y < b[1]:
    code |= 4
  elif y > b[3]:
    code |= 8
  return code


def _intersect(a, bb, edge: int, b):
  if edge & 8:
    return (a[0] + (bb[0] - a[0]) * (b[3] - a[1]) / (bb[1] - a[1]), b[3])
  if edge & 4:
    return (a[0] + (bb[0] - a[0]) * (b[1] - a[1]) / (bb[1] - a[1]), b[1])
  if edge & 2:
    return (b[2], a[1] + (bb[1] - a[1]) * (b[2] - a[0]) / (bb[0] - a[0]))
  return (b[0], a[1] + (bb[1] - a[1]) * (b[0] - a[0]) / (bb[0] - a[0]))


def polygon_clip(points: list, b) -> list:
  """turf/bbox-clip `polygonclip()` (lineclip's Sutherland-Hodgman), one pass per edge 1, 2, 4, 8."""
  result: list = []
  edge = 1
  while edge <= 8:
    result = []
    prev = points[-1]
    prev_inside = not (_bit_code(prev[0], prev[1], b) & edge)
    for p in points:
      inside = not (_bit_code(p[0], p[1], b) & edge)
      if inside != prev_inside:
        result.append(_intersect(prev, p, edge, b))
      if inside:
        result.append(p)
      prev = p
      prev_inside = inside
    points = result
    if not points:
      break
    edge *= 2
  return result


# the y margin of _clip_is_empty: far above the rounding error of turf's intersect() on coordinates <= 360 in size
# (a few ulps of 360, ~1e-13), far below any real gap between a ring and a cell edge
_CLIP_MARGIN = 1e-9


def _clip_is_empty(box, b) -> bool:
  """True only when turf `polygonclip(ring, b)` is certainly empty, from the ring's own bbox `box`.

  - every vertex left of b (`xmax < b[0]`): the first pass (edge 1) keeps nothing.
  - every vertex right of b (`xmin > b[2]`): pass 1 keeps the ring unchanged (none is left), pass 2 keeps nothing.
  - every vertex below / above b by more than `_CLIP_MARGIN`: passes 1 and 2 may add intersection points, but
    each is a convex combination of two vertices computed with an error of a few ulps, so it stays below /
    above b too, and pass 4 / pass 8 keeps nothing.

  An empty clip is what `clipPolygon()` drops anyway, so skipping it changes no area; it only saves the clip of
  the many small rings (islands as holes) far from the cell, which the TS clips every time."""
  return (box[2] < b[0] or box[0] > b[2] or box[3] < b[1] - _CLIP_MARGIN or box[1] > b[3] + _CLIP_MARGIN)


def clip_polygon(rings, b) -> list:
  """turf/bbox-clip `clipPolygon()`: each ring clipped, closed again if needed, kept when >= 4 positions."""
  out = []
  for ring in rings:
    clipped = polygon_clip(ring, b)
    if clipped:
      if clipped[0][0] != clipped[-1][0] or clipped[0][1] != clipped[-1][1]:
        clipped.append(clipped[0])
      if len(clipped) >= 4:
        out.append(clipped)
  return out


# ── partial-cell weights (gridMask.ts cellWeights, lines 219-265) ─────────────
def _cell_weights(ps: list[_Part], inside: np.ndarray, ax: _Axis, ay: _Axis) -> dict[tuple[int, int], float]:
  sin = _make_sin()
  allg = inside.copy()
  # also the 8-neighbours: cells whose centre is outside but whose square the polygon touches
  for di in (-1, 0, 1):
    for dj in (-1, 0, 1):
      src = inside[max(0, -di):ax.n - max(0, di), max(0, -dj):ay.n - max(0, dj)]
      allg[max(0, di):ax.n - max(0, -di), max(0, dj):ay.n - max(0, -dj)] |= src
  # a polygon smaller than a cell can miss every centre: seed with the cells whose square overlaps a part bbox
  if not inside.any():
    lo_x, hi_x = np.asarray(ax.lo), np.asarray(ax.hi)
    lo_y, hi_y = np.asarray(ay.lo), np.asarray(ay.hi)
    for p in ps:
      px0, py0, px1, py1 = p.bbox
      okx = ~((hi_x < px0) | (lo_x > px1))
      oky = ~((hi_y < py0) | (lo_y > py1))
      allg |= np.outer(okx, oky)
  w: dict[tuple[int, int], float] = {}
  # by grid row (j, then i), so a part's rings are reduced to the row's band once and to each cell from that
  jj, ii = np.nonzero(allg.T)
  band_j = -1
  band: list = []
  for j, i in zip(jj.tolist(), ii.tolist()):
    x0, x1, y0, y1 = ax.lo[i], ax.hi[i], ay.lo[j], ay.hi[j]
    cell = ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0))
    cell_area = abs(ring_area(cell, sin))
    if not cell_area > 0:
      continue
    if j != band_j:
      band_j = j
      band = [reduce_rings(p.np_rings, (-_INF, y0, _INF, y1)) if p.n_outer > 8 else p.np_rings for p in ps]
    a = 0.0
    rect = (x0, y0, x1, y1)
    for pi, p in enumerate(ps):
      px0, py0, px1, py1 = p.bbox
      if x1 < px0 or x0 > px1 or y1 < py0 or y0 > py1:
        continue
      rings = reduce_rings(band[pi], rect) if band[pi] is not None and p.n_outer > 8 else band[pi]
      if rings is None:
        continue
      boxes = p.ring_box
      todo = [r.tolist() for r in rings if not (id(r) in boxes and _clip_is_empty(boxes[id(r)], rect))]
      if not todo:
        continue  # area(bboxClip()) of no ring is 0, and a += 0 changes nothing
      a += polygon_area(clip_polygon(todo, rect), sin)
    f = a / cell_area
    if f > 0:
      w[(i, j)] = min(f, 1.0)
  return w


# ── gridMask ──────────────────────────────────────────────────────────────────
def grid_mask(lobe: Any, lat_axis, lon_axis, *, lat_descending: bool | None = None, weights: bool = True,
              method: str = "auto") -> MaskTable:
  """Grid cells of the (lon, lat) lattice covered by `lobe` (gridMask.ts `gridMask()`, lines 276-303).

  `lobe` is a `geom.Lobe`, a shapely (Multi)Polygon, a GeoJSON-like dict or WKB bytes, already split at +-180
  (`geom.place_lobes()`); `lat_axis` / `lon_axis` are the dataset's own axis values (cell centres, any monotonic
  direction, any spacing), as ERDDAP returns them. Note the argument order: latitude first (the TS takes
  `lon, lat`). The rules, in the TS order:

  1. parts (`polygonParts()`, lines 84-109): each polygon of the lobe is a part, its bbox from the OUTER ring;
     no part or an empty axis gives an empty mask (line 281).
  2. axes (`prepAxis()` 49-58, `axisEdges()` 29-38, `cellIndex()` 60-69, `lowerIndex()`/`upperIndex()` 71-80):
     a descending axis (`v[1] < v[0]`) is reversed; cell edges are the midpoints between neighbours with the
     ends extrapolated by half the end spacing, +-0.5 for a single value; an axis is uniform when every step is
     within 1e-6 (relative) of the first, then indexed by `floor((x - min) / step)`, else by binary search on
     the upper edges. `lat_descending`, when given, must agree with the axis: the TS reads the direction from
     the values, so this is a check and never a reorder of its own.
  3. candidates (`candidates()`, 118-133): the union over parts of the cells whose centre lies in the part's
     bbox (bounds inclusive); its size is `n_candidates`.
  4. method (line 286-288): `auto` is `turf` when there are < 10,000 candidates and < 5,000 vertices (every
     ring position, closing ones included), else `scan`. `turf` (`maskTurf()`, 136-148): turf
     `booleanPointInPolygon` per candidate centre after the part-bbox prefilter, the boundary counting as
     inside (point-in-polygon-hao with robust-predicates' exact `orient2d` sign). `scan` (`maskScan()`,
     151-176): per row, the x-crossings `ax + (y - ay) * (bx - ax) / (by - ay)` of every edge with
     `(ay > y) != (by > y)`, sorted, and the columns whose centre lies in each [x_k, x_k+1] filled. Both are
     exact and agree; `n_inside` is the count.
  5. weights (`cellWeights()`, 219-265, when `weights`): the inside cells plus their 8-neighbours (222-229);
     when no centre is inside, every cell whose square overlaps a part bbox (232-238); taken by row, then
     column (240). Per cell the area is turf `area()` of the rectangle `[[x0,y0],[x1,y0],[x1,y1],[x0,y1],[x0,y0]]`
     from the edges, the cell skipped unless > 0 (245-248); the part's rings (> 8 positions) are reduced to the
     row band `[-inf, y0, inf, y1]` once per row (249-252, `reduceRings()` 194-216) and then to the cell
     (257); the overlap is the sum over the parts whose bbox meets the cell of turf `area(bboxClip(rings,
     cell))` (254-260); weight = min(overlap / area, 1), kept only when > 0 (261-262). turf `area()` is the
     spherical ring area `sum((x[i+2] - x[i]) * sin(y[i+1])) * R^2 / 2`, R = 6,371,008.8 m, with V8's
     `Math.sin` reproduced bit for bit (`_v8math.v8_sin`); `bboxClip()` is lineclip's Sutherland-Hodgman.
     Without weights (line 299) every inside cell gets weight 1.
  6. rows (293-300): `lon` / `lat` are the axis values themselves; rows sorted by lat, then lon, ascending.

  The port changes no number; three things only make it faster, each proved not to change a bit: the scan
  crossings and the reduceRing out-codes are computed with numpy (same IEEE operations, same order), runs of
  three or more same-side vertices are dropped before the reduceRing stack walk (the walk would pop them, see
  `reduce_ring()`), and a ring whose bbox proves its clip empty is not clipped (`_clip_is_empty()`).
  """
  t0 = time.perf_counter()
  ps = [_Part(r) for r in parts_of(lobe)]
  lat_vals = [float(x) for x in np.asarray(lat_axis, dtype=np.float64).tolist()]
  lon_vals = [float(x) for x in np.asarray(lon_axis, dtype=np.float64).tolist()]
  if lat_descending is not None and len(lat_vals) > 1 and (lat_vals[1] < lat_vals[0]) != bool(lat_descending):
    raise ValueError(f"lat_descending={lat_descending} but the latitude axis runs "
                     f"{'down' if lat_vals[1] < lat_vals[0] else 'up'} ({lat_vals[0]}, {lat_vals[1]}, ...)")
  ax, ay = _Axis(lon_vals), _Axis(lat_vals)
  empty = np.zeros(0, dtype=np.float64)
  if not ps or not ax.n or not ay.n:
    return MaskTable(empty, empty.copy(), empty.copy(), 0, 0, "scan", 0.0)

  cands = _candidates(ps, ax, ay)
  n_cands = int(cands.sum())
  nv = count_vertices([p.rings for p in ps])
  if method in ("scan", "turf"):
    use = method
  elif method == "auto":
    use = "turf" if (n_cands < 10_000 and nv < 5_000) else "scan"
  else:
    raise ValueError(f"method must be auto, scan or turf, not {method!r}")
  inside = _mask_turf(ps, cands, ax, ay) if use == "turf" else _mask_scan(ps, ax, ay)

  if weights:
    w = _cell_weights(ps, inside, ax, ay)
    keys = list(w)
    wt = [w[k] for k in keys]
  else:
    ii, jj = np.nonzero(inside)
    keys = list(zip(ii.tolist(), jj.tolist()))
    wt = [1.0] * len(keys)
  lon = np.array([ax.val[i] for i, _ in keys], dtype=np.float64)
  lat = np.array([ay.val[j] for _, j in keys], dtype=np.float64)
  weight = np.array(wt, dtype=np.float64)
  order = np.lexsort((lon, lat))  # cells.sort((a, b) => a.lat - b.lat || a.lon - b.lon)
  return MaskTable(lon[order], lat[order], weight[order], n_cands, int(inside.sum()), use,
                   (time.perf_counter() - t0) * 1000)
