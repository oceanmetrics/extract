"""Place geometry from the gazetteer: one WKB row of `<base>/<coll>/places.parquet`, read by range.

erddap-places reads the published `places/places.parquet` (`stats.ts:51-54`, the 18 `NMS:*` rows, WKB in the GeoParquet
`geometry` column); the app reads one row of any collection by range with hyparquet (`placeGeometry()`,
`readParquetPlace()`). Here DuckDB's `read_parquet` with a `place_id =` filter does the same: the filter is pushed down
to the row groups whose `place_id` statistics admit the id, and over HTTP only those byte ranges are read. `base` is a
URL (`https://storage.oceanmetrics.io/gazetteer`) or a local directory.

`places_index` (`<base>/index/places_index.parquet`) names each place's collection (`collection`), so
`place_collection()` finds where a place's geometry lives when the id alone does not say.
"""
from __future__ import annotations

from pathlib import Path

GAZETTEER_BASE = "https://storage.oceanmetrics.io/gazetteer"


class PlaceNotFound(LookupError):
  """No row for the place id in the collection (or the index)."""


def _href(base: str | Path, *parts: str) -> str:
  b = str(base).rstrip("/")
  return "/".join([b, *parts])


def _connect():
  import duckdb
  con = duckdb.connect(":memory:")
  con.execute("SET enable_progress_bar = false")
  # the geometry column as the stored WKB bytes, not DuckDB's GEOMETRY type (1.5 cannot cast that back to BLOB)
  con.execute("SET enable_geoparquet_conversion = false")
  return con


def places_href(coll: str, base: str | Path = GAZETTEER_BASE) -> str:
  """`<base>/<coll>/places.parquet`."""
  return _href(base, coll, "places.parquet")


def place_geometry(place_id: str, coll: str = "places", base: str | Path = GAZETTEER_BASE) -> bytes:
  """The WKB geometry of `place_id` in `<base>/<coll>/places.parquet`, the stored bytes (GeoParquet conversion is off,
  so DuckDB reads the column as BLOB). Raises `PlaceNotFound`."""
  con = _connect()
  try:
    row = con.execute(f"SELECT geometry FROM read_parquet('{places_href(coll, base)}') WHERE place_id = ? LIMIT 1",
                      [place_id]).fetchone()
  finally:
    con.close()
  if row is None:
    raise PlaceNotFound(f"no place {place_id} in the {coll} collection ({places_href(coll, base)})")
  return bytes(row[0])


def place_ids(coll: str = "places", base: str | Path = GAZETTEER_BASE, prefix: str = "") -> list[str]:
  """Every place id of a collection that starts with `prefix`, sorted (the 18 `NMS:` ids of `places`)."""
  con = _connect()
  try:
    rows = con.execute(f"SELECT place_id FROM read_parquet('{places_href(coll, base)}') WHERE starts_with(place_id, ?) "
                       "ORDER BY place_id", [prefix]).fetchall()
  finally:
    con.close()
  return [r[0] for r in rows]


def place_collection(place_id: str, base: str | Path = GAZETTEER_BASE) -> str:
  """The collection slug of a place from `<base>/index/places_index.parquet` (its `collection` column)."""
  con = _connect()
  try:
    row = con.execute(f"SELECT collection FROM read_parquet('{_href(base, 'index', 'places_index.parquet')}') "
                      "WHERE place_id = ? LIMIT 1", [place_id]).fetchone()
  finally:
    con.close()
  if row is None:
    raise PlaceNotFound(f"no place {place_id} in the places index")
  return str(row[0])
