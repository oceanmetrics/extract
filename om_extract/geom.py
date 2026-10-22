"""Place geometry for the mask: WKB decoding and the lobe split at +-180, ported from erddap-places.

Sources ported (erddap-places `app/src/lib/`):

- `wkb.ts` `wkbToGeoJSON()` / `polygonCoordinates()`: `wkb_to_geojson()` / `polygon_coordinates()`.
- `gazetteer.ts` `placeLobes()`: `place_lobes()` / `lobes()`.

The coordinates are kept as python floats exactly as decoded (no reprojection, no normalisation, no ring
reorientation), because the mask's arithmetic depends on the vertex order. A place arrives already cut at
+-180 (the gazetteer cuts it); `place_lobes()` only groups its polygon parts by side, it never modifies them.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

# a polygon part: its rings (outer first, then holes), each a closed list of (x, y)
Ring = list[tuple[float, float]]
Rings = list[Ring]


# ── WKB (wkb.ts) ──────────────────────────────────────────────────────────────
def _header(buf: memoryview, off: int) -> tuple[int, int, str, int]:
  """byte order + type code at `off` (wkb.ts header()): returns (type, dims, struct prefix, new offset).

  both byte orders and both type-code dialects: ISO (type + 1000 Z, + 2000 M, + 3000 ZM) and EWKB (high bits
  0x80000000 Z, 0x40000000 M, 0x20000000 SRID follows the type)."""
  order = buf[off]
  if order not in (0, 1):
    raise ValueError(f"WKB: bad byte order {order} at {off}")
  e = "<" if order == 1 else ">"
  (raw,) = struct.unpack_from(e + "I", buf, off + 1)
  off += 5
  ewkb_z = (raw & 0x80000000) != 0
  ewkb_m = (raw & 0x40000000) != 0
  srid = (raw & 0x20000000) != 0
  base = raw & 0x0FFFFFFF
  iso = base // 1000  # 0 = XY, 1 = Z, 2 = M, 3 = ZM
  typ = base % 1000
  if srid:
    off += 4  # skip the SRID
  dims = 2 + (1 if ewkb_z else 0) + (1 if ewkb_m else 0) + (1 if iso in (1, 2) else 2 if iso == 3 else 0)
  return typ, dims, e, off


def _points(buf: memoryview, off: int, e: str, dims: int) -> tuple[list[tuple[float, float]], int]:
  (n,) = struct.unpack_from(e + "I", buf, off)
  off += 4
  flat = struct.unpack_from(f"{e}{n * dims}d", buf, off)
  off += 8 * n * dims
  # z / m are read and dropped: the mask only wants x, y
  return [(flat[i], flat[i + 1]) for i in range(0, n * dims, dims)], off


def _geometry(buf: memoryview, off: int) -> tuple[dict, int]:
  typ, dims, e, off = _header(buf, off)
  if typ == 1:
    xy = struct.unpack_from(f"{e}{dims}d", buf, off)
    return {"type": "Point", "coordinates": (xy[0], xy[1])}, off + 8 * dims
  if typ == 2:
    pts, off = _points(buf, off, e, dims)
    return {"type": "LineString", "coordinates": pts}, off
  if typ == 3:
    (n,) = struct.unpack_from(e + "I", buf, off)
    off += 4
    rings = []
    for _ in range(n):
      r, off = _points(buf, off, e, dims)
      rings.append(r)
    return {"type": "Polygon", "coordinates": rings}, off
  if typ in (4, 5, 6, 7):
    (n,) = struct.unpack_from(e + "I", buf, off)
    off += 4
    parts = []
    for _ in range(n):  # each part carries its own header
      g, off = _geometry(buf, off)
      parts.append(g)
    if typ == 7:
      return {"type": "GeometryCollection", "geometries": parts}, off
    kind = {4: "MultiPoint", 5: "MultiLineString", 6: "MultiPolygon"}[typ]
    return {"type": kind, "coordinates": [p["coordinates"] for p in parts]}, off
  raise ValueError(f"WKB: unsupported geometry type {typ}")


def wkb_to_geojson(data: bytes | bytearray | memoryview) -> dict:
  """Decode one WKB (or EWKB) geometry to a 2-D GeoJSON-like dict (wkb.ts `wkbToGeoJSON()`).

  Positions are `(x, y)` tuples of the exact float64 values in the bytes."""
  g, _ = _geometry(memoryview(bytes(data)), 0)
  return g


def polygon_coordinates(g: dict) -> list[Rings]:
  """Every polygon of a GeoJSON-like geometry as a list of ring lists (wkb.ts `polygonCoordinates()`)."""
  t = g.get("type")
  if t == "Polygon":
    return [_rings(g["coordinates"])]
  if t == "MultiPolygon":
    return [_rings(p) for p in g["coordinates"]]
  if t == "GeometryCollection":
    return [p for sub in g["geometries"] for p in polygon_coordinates(sub)]
  if t == "Feature":
    return polygon_coordinates(g["geometry"]) if g.get("geometry") else []
  if t == "FeatureCollection":
    return [p for f in g["features"] for p in polygon_coordinates(f)]
  return []


def _rings(rings: Any) -> Rings:
  return [[(float(p[0]), float(p[1])) for p in r] for r in rings]


def _shapely_parts(geom: Any) -> list[Rings]:
  """the polygon parts of a shapely geometry, vertex order untouched (z dropped)."""
  t = geom.geom_type
  if t == "Polygon":
    if geom.is_empty:
      return []
    return [[_rings([geom.exterior.coords])[0], *_rings([r.coords for r in geom.interiors])]]
  if t in ("MultiPolygon", "GeometryCollection"):
    return [p for g in geom.geoms for p in _shapely_parts(g)]
  return []


def parts_of(geometry: Any) -> list[Rings]:
  """Polygon parts of a geometry given as WKB bytes, a GeoJSON-like dict, a shapely geometry or a `Lobe`."""
  if isinstance(geometry, Lobe):
    return geometry.parts
  if isinstance(geometry, (bytes, bytearray, memoryview)):
    return polygon_coordinates(wkb_to_geojson(geometry))
  if isinstance(geometry, dict):
    return polygon_coordinates(geometry)
  if hasattr(geometry, "geom_type"):
    return _shapely_parts(geometry)
  raise TypeError(f"not a geometry: {type(geometry).__name__}")


def from_wkb(data: bytes | bytearray | memoryview):
  """A shapely geometry from WKB, decoded by the `wkb.ts` port (so the coordinates are exactly the bytes')."""
  from shapely.geometry import shape

  return shape(wkb_to_geojson(data))


# ── lobes (gazetteer.ts placeLobes) ───────────────────────────────────────────
def part_bbox(rings: Rings) -> tuple[float, float, float, float]:
  """[xmin, ymin, xmax, ymax] of a part's OUTER ring (gazetteer.ts `partBbox`, gridMask.ts polygonParts())."""
  x0 = y0 = float("inf")
  x1 = y1 = float("-inf")
  for x, y in rings[0]:
    if x < x0: x0 = x
    if x > x1: x1 = x
    if y < y0: y0 = y
    if y > y1: y1 = y
  return (x0, y0, x1, y1)


@dataclass(frozen=True)
class Lobe:
  """One griddap request's worth of a place (gazetteer.ts `Lobe`).

  `side` is `"all"` for a place that does not span the antimeridian, else `"W"` or `"E"`; `id` is
  `place_id` with `:W` / `:E` appended when split, as the TS builds it."""
  id: str
  side: str
  bbox: tuple[float, float, float, float]
  parts: list[Rings]
  n_parts: int

  @property
  def geometry(self):
    """the lobe as a shapely Polygon (one part) or MultiPolygon (several), parts in order."""
    from shapely.geometry import MultiPolygon, Polygon

    polys = [Polygon(r[0], r[1:]) for r in self.parts]
    return polys[0] if len(polys) == 1 else MultiPolygon(polys)


def place_lobes(geometry: Any, place_id: str = "") -> list[Lobe]:
  """Split a place into the lobes that each get one griddap request (gazetteer.ts `placeLobes()`).

  Rules, each mirroring the TS line:

  - parts are the place's polygons in order (`polygonCoordinates(place.geometry)`); none gives `[]`.
  - the place is split only when its parts' lon extent is wider than 180 deg (`split = x1 - x0 > 180`),
    as for PMNM; a place wholly on one side stays one lobe `"all"` holding every part.
  - when split, a part goes to `"W"` when the midpoint of its bbox is below 0 (`(b[0] + b[2]) / 2 < 0`), else
    `"E"`: a part touching the line keeps its own longitude convention (-180 on the west, 180 on the east)
    and a part whose midpoint is exactly 0 is `"E"`.
  - groups keep first-seen order (a `Map`), each lobe's bbox is the min/max of its parts' bboxes, and lobes
    are sorted by bbox west edge (`lobes.sort((a, b) => a.bbox[0] - b.bbox[0])`, a stable sort).
  - the parts themselves are never modified (the gazetteer has already cut them at +-180).
  """
  parts = parts_of(geometry)
  if not parts:
    return []
  boxes = [part_bbox(r) for r in parts]
  x0 = min(b[0] for b in boxes)
  x1 = max(b[2] for b in boxes)
  split = x1 - x0 > 180
  groups: dict[str, list[int]] = {}
  for i, b in enumerate(boxes):
    g = "all" if not split else ("W" if (b[0] + b[2]) / 2 < 0 else "E")
    groups.setdefault(g, []).append(i)
  out = []
  for g, idx in groups.items():
    bbox = (min(boxes[i][0] for i in idx), min(boxes[i][1] for i in idx),
            max(boxes[i][2] for i in idx), max(boxes[i][3] for i in idx))
    out.append(Lobe(id=place_id if g == "all" else f"{place_id}:{g}", side=g, bbox=bbox,
                    parts=[parts[i] for i in idx], n_parts=len(idx)))
  return sorted(out, key=lambda lobe: lobe.bbox[0])


def lobes(geometry: Any) -> list:
  """The lobes of a place as shapely geometries (Polygon for one part, MultiPolygon for several); see
  `place_lobes()` for the rule and for each lobe's id and bbox."""
  return [lobe.geometry for lobe in place_lobes(geometry)]
