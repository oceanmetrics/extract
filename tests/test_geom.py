"""om_extract.geom: the wkb.ts decoder and the gazetteer.ts placeLobes() split, one test per rule.

The WKB fixtures are the ones of erddap-places `wkb.test.ts` (DuckDB's own ST_AsWKB() output, little-endian, and
the same square written by hand big-endian)."""
import struct

import pytest
from shapely.geometry import MultiPolygon, Polygon

from om_extract.geom import from_wkb, lobes, part_bbox, place_lobes, polygon_coordinates, wkb_to_geojson

# POLYGON((0 0,3 0,3 2,0 2,0 0),(1 .5,2 .5,2 1.5,1 1.5,1 .5)): outer ring + hole
POLY_LE = (
  "01030000000200000005000000000000000000000000000000000000000000000000000840000000000000000000000000000008400000"
  "000000000040000000000000000000000000000000400000000000000000000000000000000005000000000000000000F03F0000000000"
  "00E03F0000000000000040000000000000E03F0000000000000040000000000000F83F000000000000F03F000000000000F83F00000000"
  "0000F03F000000000000E03F")
# MULTIPOLYGON(((-179 20,...)),((179 20,...))): the two antimeridian lobes
MULTI_LE = (
  "0106000000020000000103000000010000000500000000000000006066C0000000000000344000000000004066C0000000000000344000"
  "000000004066C0000000000000354000000000006066C0000000000000354000000000006066C000000000000034400103000000010000"
  "00050000000000000000606640000000000000344000000000008066400000000000003440000000000080664000000000000035400000"
  "000000606640000000000000354000000000006066400000000000003440")
# big-endian POLYGON((0 0,1 0,1 1,0 1,0 0))
POLY_BE = ("0000000003" "00000001" "00000005"
           "00000000000000000000000000000000" "3FF00000000000000000000000000000" "3FF00000000000003FF0000000000000"
           "00000000000000003FF0000000000000" "00000000000000000000000000000000")


def sq(x0, y0, x1, y1):
  return [[(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]]


# ── wkb.ts ────────────────────────────────────────────────────────────────────
def test_wkb_little_endian_polygon_with_hole():
  g = wkb_to_geojson(bytes.fromhex(POLY_LE))
  assert g["type"] == "Polygon"
  assert len(g["coordinates"]) == 2
  assert g["coordinates"][0] == [(0, 0), (3, 0), (3, 2), (0, 2), (0, 0)]
  assert g["coordinates"][1][0] == (1, 0.5)


def test_wkb_multipolygon_one_ring_set_per_part():
  g = wkb_to_geojson(bytes.fromhex(MULTI_LE))
  assert g["type"] == "MultiPolygon"
  assert len(g["coordinates"]) == 2
  assert g["coordinates"][0][0][0] == (-179, 20)
  assert g["coordinates"][1][0][1] == (180, 20)
  assert len(polygon_coordinates(g)) == 2


def test_wkb_big_endian_polygon():
  assert wkb_to_geojson(bytes.fromhex(POLY_BE))["coordinates"][0] == [(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]


def test_wkb_iso_z_and_ewkb_srid_drop_to_xy():
  # ISO POLYGON Z (type 1003) and EWKB POLYGON with SRID 4326 (0x20000003): z read and dropped, srid skipped
  ring = [(0, 0, 9), (1, 0, 9), (1, 1, 9), (0, 0, 9)]
  iso = struct.pack("<BII", 1, 1003, 1) + struct.pack("<I", 4) + b"".join(struct.pack("<3d", *p) for p in ring)
  ewkb = (struct.pack("<BIII", 1, 0x20000003, 4326, 1) + struct.pack("<I", 4)
          + b"".join(struct.pack("<2d", *p[:2]) for p in ring))
  want = [(0, 0), (1, 0), (1, 1), (0, 0)]
  assert wkb_to_geojson(iso)["coordinates"][0] == want
  assert wkb_to_geojson(ewkb)["coordinates"][0] == want


def test_wkb_rejects_unknown_type():
  with pytest.raises(ValueError, match="unsupported geometry type"):
    wkb_to_geojson(bytes.fromhex("0163000000"))


def test_polygon_coordinates_of_non_areal_is_empty():
  assert polygon_coordinates({"type": "Point", "coordinates": (0, 0)}) == []


def test_from_wkb_gives_shapely_with_the_same_vertices():
  g = from_wkb(bytes.fromhex(POLY_LE))
  assert isinstance(g, Polygon)
  assert list(g.exterior.coords) == [(0, 0), (3, 0), (3, 2), (0, 2), (0, 0)]
  assert len(g.interiors) == 1


# ── placeLobes() ──────────────────────────────────────────────────────────────
def test_lobes_one_side_stays_one_lobe_with_every_part():
  # two parts 100 degrees apart: extent 100 <= 180, so no split, id is the place id, bbox the union
  geom = {"type": "MultiPolygon", "coordinates": [sq(-120, 30, -119, 31), sq(-20, 10, -19, 12)]}
  ls = place_lobes(geom, "NMS:X")
  assert [(lb.id, lb.side, lb.n_parts) for lb in ls] == [("NMS:X", "all", 2)]
  assert ls[0].bbox == (-120, 10, -19, 31)


def test_lobes_split_when_extent_exceeds_180_west_first():
  # the wkb.test.ts antimeridian pair: east part listed first, lobes sorted by west edge
  geom = {"type": "MultiPolygon", "coordinates": [sq(179, 20, 180, 21), sq(-180, 20, -179, 21)]}
  ls = place_lobes(geom, "NMS:PMNM")
  assert [(lb.id, lb.side) for lb in ls] == [("NMS:PMNM:W", "W"), ("NMS:PMNM:E", "E")]
  assert [lb.bbox for lb in ls] == [(-180, 20, -179, 21), (179, 20, 180, 21)]


def test_lobes_extent_exactly_180_is_not_split():
  geom = {"type": "MultiPolygon", "coordinates": [sq(-90, 0, -89, 1), sq(89, 0, 90, 1)]}
  assert [lb.side for lb in place_lobes(geom)] == ["all"]


def test_lobes_part_touching_the_line_keeps_its_convention():
  # a part from 170 to 180 and one from -180 to -170: each keeps its own +-180 vertex, untouched
  e, w = sq(170, 0, 180, 1), sq(-180, 0, -170, 1)
  ls = place_lobes({"type": "MultiPolygon", "coordinates": [e, w]})
  assert ls[0].parts == [w] and ls[1].parts == [e]
  assert ls[0].bbox[0] == -180 and ls[1].bbox[2] == 180


def test_lobes_midpoint_zero_goes_east():
  # split (extent > 180) with a part whose bbox midpoint is exactly 0: (b[0] + b[2]) / 2 < 0 is false -> E
  geom = {"type": "MultiPolygon", "coordinates": [sq(-1, 0, 1, 1), sq(-180, 0, -179, 1)]}
  ls = place_lobes(geom, "P")
  assert [(lb.side, lb.n_parts) for lb in ls] == [("W", 1), ("E", 1)]
  assert ls[1].parts == [sq(-1, 0, 1, 1)]


def test_lobes_bbox_is_of_outer_rings_only():
  assert part_bbox([[(0, 0), (2, 0), (2, 3), (0, 0)], [(-5, -5), (9, 9), (0, 9), (-5, -5)]]) == (0, 0, 2, 3)


def test_lobes_empty_geometry_gives_no_lobe():
  assert place_lobes({"type": "Point", "coordinates": (0, 0)}) == []


def test_lobes_accepts_wkb_and_shapely_and_returns_shapely():
  wkb = bytes.fromhex(MULTI_LE)
  by_wkb, by_shapely = place_lobes(wkb), place_lobes(from_wkb(wkb))
  assert [lb.parts for lb in by_wkb] == [lb.parts for lb in by_shapely]
  gs = lobes(wkb)
  assert len(gs) == 2 and all(isinstance(g, Polygon) for g in gs)
  one = lobes({"type": "MultiPolygon", "coordinates": [sq(0, 0, 1, 1), sq(2, 0, 3, 1)]})
  assert len(one) == 1 and isinstance(one[0], MultiPolygon) and len(one[0].geoms) == 2
