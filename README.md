# extract: the Ocean Metrics extraction engine

`om-extract` is the Python extraction engine of the Ocean Metrics platform: one package that summarises a layer (an
ERDDAP grid, a Zarr store, a COG, an H3 Parquet table, point records) by gazetteer places into indicator values, with
one weighting rule, on the Mac mini runner and with the same SQL in the browser.

## What arrives when

| When | What |
|---|---|
| Day 4 (2026-10-25) | Reader interface `extent()`, `axes(bbox, depth)`, `slab(bbox, t0, t1, var)`, `slice(bbox, t, var)`; `grid_mask` ported from erddap-places with its fixture bit for bit; the CLI; output through the values contract |
| Build week 1 (2026-10-30 to 11-05) | ERDDAP, COG and H3 readers; the 18 sanctuaries recomputed; the Copernicus Marine producer as a reader |

## The rule

Cells are assigned to a place by their centre, with partial-area weights (erddap-places' `gridMask()`). Per place and
time step the engine returns: cells, weighted mean, unweighted mean, SD across cells, min, max, p10, p90 and the share
of cells with data. The record is computed on the runner and the preview in the browser from the same SQL over the
same mask; a parity test asserts identical numbers.

## Mask and statistics

`om_extract.geom`, `om_extract.mask` and `om_extract.stats` are a port of erddap-places' mask (`app/src/lib/gridMask.ts`,
`wkb.ts`, `gazetteer.ts` `placeLobes()`, on turf 7.4.0) and its SQL, so the runner computes the numbers the browser
shows, bit for bit.

```python
from om_extract.geom import place_lobes
from om_extract.mask import grid_mask
from om_extract.stats import stats

for lobe in place_lobes(wkb_bytes, "NMS:PMNM"):              # one lobe per side of +-180
  m = grid_mask(lobe, lat_axis, lon_axis)                     # the ERDDAP axis values of the lobe's bbox
  daily = stats(slab, m)                                      # stats_daily.sql in DuckDB: one row per day
```

The rules, each mirroring a TS line (the docstrings name them):

- **Lobes** (`place_lobes()`): a place is split only when its parts span more than 180 deg of longitude; a part goes
  west when the midpoint of its bbox is below 0, else east; parts are never modified (the gazetteer has cut them at
  +-180, each keeps its own -180 or 180); lobes are sorted by west edge, ids `<place_id>:W` / `:E`.
- **Cells by centre** (`grid_mask()`): a cell is inside when its centre is (boundary included); cell edges are the
  midpoints between axis values, the ends extrapolated; `auto` uses point-in-polygon below 10,000 candidates and
  5,000 vertices, the scanline fill above; both give the same cells.
- **Partial-area weights**: every inside cell and its 8 neighbours (or, for a place smaller than a cell, every cell
  its bbox overlaps) get weight = min(area of the polygon clipped to the cell / area of the cell, 1), kept when > 0.
  Areas are turf's spherical ring area (R = 6,371,008.8 m), not planar degrees.
- **Rows**: `lon`, `lat` are the axis values, sorted by lat then lon; `MaskTable.to_arrow()` is the `mask` table
  (`latitude`, `longitude`, `weight`) the SQL joins on.
- **Statistics** (`stats()`): `om_extract/sql/stats_daily.sql`, `stats_categorical.sql` and `last_step.sql` are
  byte-for-byte copies of erddap-places `sql/` (a test checks them against a sibling checkout), rendered with the
  same `{{expr}}`, `{{slab}}`, `{{mask}}` convention (`render()`, `lit()` as `sql.ts`). `mean_wt = sum(value *
  weight) / sum(weight)`; `n`, `mean`, `sd`, `min`, `max`, `p10`, `p90` are over the cells with data, unweighted;
  `weight_sum = sum(weight)`. The slab joins the mask on `ROUND(x, 3)`.

**Parity is exact.** `tests/fixtures/mask/make_expected.mjs` imports erddap-places' own TypeScript and writes the
expected rows; `tests/test_mask_parity.py` asserts the same rows in the same order with `weight` equal by `==`.

| Fixture | Lobes | Rows (inside, method) |
|---|---|---|
| `cinms_crw005_desc`, `_asc` (0.05 deg, lat descending / ascending) | 1 | 215 (152, scan) |
| `cinms_crw005_turf` (point-in-polygon forced) | 1 | 215 (152, turf) |
| `cinms_crw005_centres` (no weights) | 1 | 152 (152, scan) |
| `fknms_crw005` | 1 | 462 (352, scan) |
| `hihwnms_crw005` / `hihwnms_cmems083` (1/12 deg, non-uniform 5-decimal axis) | 1 | 230 (124) / 109 (50), scan |
| `pmnm_crw005` | W + E | 51,247 (50,725, scan) + 3,859 (3,755, turf) |
| `pmnm_cmems083` | W + E | 18,655 (18,270, scan) + 1,376 (1,312, turf) |
| `square_deg1`, `square_am_deg1` (synthetic, 1 deg; the second cut at +-180) | 1 / W + E | 6 / 4 + 4 |
| `holed_deg1` (a hole), `subcell_q025` (smaller than a cell), `subcell_q025_centres`, `unit_q025` | 1 | 12 / 4 / 0 / 9 |

Exactness needs V8's `Math.sin`, not the platform's: V8 runs fdlibm compiled with fused multiply-adds on arm64,
which differs from libm in the last bit for about 3.6% of latitudes. `om_extract/_v8math.py` ports it (with
`math.fma`, or an exact integer fallback on Python 3.12) and matches node on every value tested. The fixtures come
from node 26 on arm64, the Mac mini runner's architecture; a browser on x86-64 computes `Math.sin` without the fused
multiply-adds and can differ from both in the last bit of a weight (about 1e-16 relative). The FKNMS mask (13 parts,
39,645 vertices) builds in about 0.5 s (0.24 s in node); a test holds it under 2 s.

Regenerate the fixtures after a change to the TypeScript (a test compares their recorded source hashes with a
sibling erddap-places checkout):

```sh
cd ../erddap-places/app
npx tsx ../../extract/tests/fixtures/mask/make_expected.mjs --lib src/lib --geojson src/lib/__fixtures__ \
  --out ../../extract/tests/fixtures/mask
```

The place polygons in the fixtures are NOAA national marine sanctuary boundaries (public), stored as WKB.

## Commands

```sh
uv sync --group dev
uv run pytest -q
uv run om-extract --version
```

## Contract

A **values file** is the output of every indicator producer: one Parquet file per (indicator, place collection) at
`values/indicator=<id>/coll=<slug>/data.parquet`, one row per (place, indicator, period), contract 1.2.0:

| column | type | |
|---|---|---|
| `place_id` | string | gazetteer id, `<authority>:<source_id>` (`NMS:CINMS`, `MRGID:8371`) |
| `coll` | string | gazetteer collection of the place (`places`, `mr_eez`) |
| `indicator` | string | `erddap:<dataset>:<variable>:<stat>`, `cmems:<dataset>:<variable>:mean`, `sdg:<series>`, `obis:<metric>[:<eov>]` |
| `time_grain` | string | `all`, `decade`, `year`, `month` or `day` |
| `time` | date | first day of the period; null for `all` |
| `class` | string | categorical indicators only: the class whose area share `value` is |
| `value` | double | finite or null |
| `n` | double | what the value summarises (time steps, records; see the manifest's `n_meaning`) |
| `sd` | double | contract 1.2: the sample standard deviation (n - 1) of the layer's cell values across the place's cells at that time step, in `unit`; finite and >= 0, or null |
| `unit` | string | |
| `x_*` | any | optional producer columns |

Each file carries its provenance (producer, the original dataset, the exact sources read, method, code version,
created, licence and row count) as JSON in the Parquet key-value metadata under `om:provenance`. The schemas are in
`oceanmetrics/platform`, `packages/catalog/schemas/values-row.schema.json` (one row) and
`values-provenance.schema.json` (the provenance), read through `om_extract.schemas` like every platform schema and never
copied here. Validator, writer and reader: [`om_extract/contract.py`](om_extract/contract.py)
(`write_values()`, `read_values()`, `validate_rows()`, `validate_table()`, `validate_provenance()`, `validate_file()`,
`validate_tree()`). `write_values()` validates rows and provenance and refuses to write on any error; the rules beyond
JSON Schema (time aligned to its grain, unique keys, one indicator and coll per file, finite values, class on all rows
or none, `sd` null where `value` is null or `n` is 0) live in `validate_rows()`, each with a test.

**Contract 1.2.0** adds three things to 1.1:

- **`sd`**, the tenth core column, right after `n`. It is the spread inside the place that the area-weighted `value`
  hides. It is null where `value` is null, where `n` is 0, wherever a row summarises several time steps (an SD of
  monthly or daily means is not the cell SD: erddap-places writes `sd` only for a month that is one time step, null for
  daily-to-month rows and for years) and for producers with no cells (OBIS, UN SDG). Copernicus Marine rows carry the
  spatial SD that was `x_sd` in 1.1.
- **`dataset`** in the provenance is required: the original dataset (`id`, `title`, `providers`, `license`, `via`,
  `doi` / `citation` when present). The erddap-places adapter states the same dataset the manifest reads from the
  ERDDAP collection.
- **`sources[]`** entries gain `item` (the URL of the STAC Item that describes the source file, or null) and `role`
  (`data`, the default, `geometry`, `product` or `manifest`). At least one source has the role `data`.

**1.1 files stay readable and valid.** `validate_table()` and `validate_file()` read the file's own `contract`: a 1.1.x
file needs no `sd` column and no `dataset`, and a 1.2.x file without `sd` fails with `missing column 'sd'`. A reader
treats a missing `sd` column as null. `write_values()` always writes 1.2.0 and fills `sd` with nulls when the rows
carry none.

```sql
-- every SST value for every sanctuary, straight from the indicators bucket (published daily)
SELECT place_id, time, value FROM read_parquet(
  'https://s3.us-east-1.amazonaws.com/oceanmetrics.io-public/indicators/values/indicator=erddap:dhw_5km:CRW_SST:mean/coll=places/data.parquet')
WHERE time_grain = 'year' ORDER BY place_id, time;
## Readers

One reader per source, behind one interface (`om_extract/readers/base.py`), chosen by the profile a STAC collection
declares: `readers.for_collection(collection)` returns the `ErddapGrid` reader when `erddap:protocol` is present.

```python
class Reader(Protocol):
  kind: str                                                # "erddap" today; "cog", "h3", "zarr" follow
  def extent(self) -> Extent                               # time_start, time_end, bbox, depth_levels (+ step_days, live)
  def axes(self, bbox, depth=None) -> Axes                 # lat, lon centres, step, lat_descending, lon_range
  def slab(self, bbox, t0, t1, var) -> pyarrow.Table       # time, lat, lon, value (the long table erddap-places shapes)
  def slice(self, bbox, t, var) -> pyarrow.Table           # lat, lon, value (one time step)
  def source_block(self) -> dict                           # the erddap:* / cog:* / ... keys, for the provenance
```

`bbox` is `(lon_min, lat_min, lon_max, lat_max)`, one polygon lobe, never across the antimeridian.

**The ERDDAP reader** (`om_extract/readers/erddap.py`, ported from erddap-places' `erddap.ts`, `extent.ts` and
`fetchRetry.ts`; the tests copy the expected URLs from its tests) builds on an `erddap-grid` collection, validated
against the platform schema first (`InvalidCollection` lists the errors). Its rules:

- **URL**: `{base}/griddap/{dataset}.{fmt}?{var}[(t0):1:(t1)][(lat):1:(lat)][(lon):1:(lon)]`; each `[` and `]` is
  percent-encoded and nothing else is. A descending latitude axis is requested high to low. A depth level is one more
  constraint after time. tabledap URLs (`tabledap_url`) encode `%2C` between columns and the comparison operators only.
- **Longitude** is shifted into the dataset's frame and clamped to `erddap:lon_range`. A lobe that straddles the seam of
  a 0..360 grid is refused (split it).
- **Chunks**: a slab is cut into requests of at most 90 days or 100,000 values (`chunk_days_for(cells, step_days)`; an
  8-day product counts steps in its own days), fetched one by one as Parquet, concatenated in time order and renamed
  to the long table.
- **Extent**: the time range is asked of `/info/<id>/index.json` (the catalog's end is null or stale for live grids);
  when the server does not answer, the catalog's range is used and `Extent.live` is False. A slab window is clamped to
  the extent; a window wholly outside it raises.
- **Axes**: the catalog's `cube:dimensions` lattice by default (no request), snapped to the nearest cell at each end as
  ERDDAP does; `axes(bbox, live=True)`, or an axis without a published step, asks the server for the axis vectors.
- **Fetch**: `fetch(url) -> bytes` is injectable (tests feed Parquet bytes). The default is `httpx` with a per-attempt
  timeout, one retry on a network error, a timeout or a 5xx, none on a 4xx, and an error that names the host
  (`pae-paha.pacioos.hawaii.edu did not answer (no response within 20 s, 2 tries)`).
- **Format**: `parquetWMeta`, then `parquet`; a server offering neither is refused (CSV and JSONP are browser fallbacks).
- tabledap collections build a reader and report their extent; `slab()` raises `NotImplementedError` until the point mask.

`om_extract/layers.py` loads a collection (`load_collection(path_or_url)`) and returns the dataset record the apps read
(`dataset_of(collection)`, erddap-places' `toDataset()` with its defaults).

```sh
uv run om-extract layer info collection.json                    # asks the server for the live time extent
uv run om-extract layer info collection.json --info info.json   # a saved info/<id>/index.json instead (offline)
uv run om-extract layer info collection.json --json
```

## Notebooks

Each workflow notebook (`*.qmd` in `oceanmetrics/workflows`) carries an `om:` block in its YAML front matter, validated
against the platform schema `om-notebook` (`packages/catalog/schemas/om-notebook.schema.json`):

```yaml
om:
  target_name: aquamaps_eov          # unique, snake_case
  workflow_type: publish             # ingest | extract | publish | release | reference | explore
  dependency: []                     # target_names that run first
  output: https://storage.oceanmetrics.io/atlas/aquamaps_eov/provenance.json   # the manifest = the done marker
  products:
    - {href: https://storage.oceanmetrics.io/atlas/aquamaps_eov/collection.json, kind: stac-collection}
  schedule: manual                   # or a five-field cron
  status: active                     # active | archived | draft
  description: One sentence for the index card.
```

```sh
uv run om-extract notebooks check <dir>                       # exit 1 on schema, duplicate, unknown-dependency or cycle errors
uv run om-extract notebooks index <dir> --title "Workflows"   # writes index.qmd, _om/order.json, _om/dag.mmd
```

The schemas are found in `$OM_SCHEMAS_DIR`, else `node_modules/@oceanmetrics/catalog/schemas`, else the sibling
checkout `../platform/packages/catalog/schemas`.

## Public repo

This repo is public from its first commit. Nothing internal is committed: no Marine Regions copies, no PPOW geometry,
no private emails. Fixtures are synthetic or from public sources.

Code is MIT, Copyright (c) 2026 Ocean Metrics LLC.
