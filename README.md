# extract: the Ocean Metrics extraction engine

`om-extract` is the Python extraction engine of the Ocean Metrics platform: one package that summarises a layer (an
ERDDAP grid, a Zarr store, a COG, an H3 Parquet table, point records) by gazetteer places into indicator values, with
one weighting rule, on the Mac mini runner and with the same SQL in the browser.

## What arrives when

| When | What |
|---|---|
| Day 4 (2026-10-25) | Reader interface `extent()`, `axes(bbox, depth)`, `slab(bbox, t0, t1, var)`, `slice(bbox, t, var)`; `grid_mask` ported from erddap-places with its fixture bit for bit; the CLI; output through the values contract |
| Build week 1 (2026-10-30 to 11-05) | ERDDAP, COG and H3 readers; the 18 sanctuaries recomputed; the Copernicus Marine producer as a reader (done 2026-10-23, 0.6.0, with the stats Item writer and `om-extract run --chunks --stats-dir`) |

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

## Run

`om-extract run` summarises one variable of a layer over one place and one time window, end to end, and writes the
values file. It is erddap-places' precompute `computeOne()` in Python: the same lobes, axes, mask, requests and SQL.

```sh
uv run om-extract run --collection collection.json --place NMS:CINMS --geometry cinms.wkb \
  --var CRW_SST --t0 2026-09-01 --t1 2026-09-30 --out out/ [--stat mean_wt] [--coll places] [--slab-dir slabs/]
```

It writes:

| File | What |
|---|---|
| `<out>/stats.parquet` | erddap-places' stats table: one row per UTC day, `date, n, mean, mean_wt, sd, min, max, p10, p90, weight_sum, place_id, dataset_id, variable` (categorical: `date, class, n, weight, frac_area, pct_cells, place_id, dataset_id, variable`) |
| `<out>/provenance.json` | erddap-places' provenance keys: place, dataset, variable, window, `lobes`, `mask_cells`, `axes`, `griddap_urls` (up to 24), `griddap_url_templates`, `griddap_chunks`, `griddap_requests`, `rows`, `bytes`, `generated`, `erddap_base`, `erddap_version`, `sql` |
| `<out>/values/indicator=<id>/coll=<slug>/data.parquet` | the contract 1.2.0 values file, written by `contract.write_values()` and checked by `contract.validate_file()`; the row count is printed, and any contract error exits 1 with the message |

- `--geometry` is a WKB file or a GeoJSON file (a geometry, Feature or FeatureCollection), already cut at +-180.
- `--stat` is the stats column that becomes `value`: `mean_wt` (default), `mean`, `sd`, `min`, `max`, `p10`, `p90`.
  The indicator is `erddap:<dataset_id>:<variable>:<stat>`; a categorical variable gives `erddap:<dataset_id>:<variable>:share`,
  one row per class with `value` = `frac_area`.
- `n` is the cells with data at the step; `sd` the SD across them for `mean_wt` and `mean`, else null.
- Rows are at the grid's native grain (`day` for `P1D`, `month` for `P1M`, else from the step in days); aggregation to
  a coarser grain is build week 1.
- `--coll` defaults from the place id: `NMS:` is `places`, any other authority lowercased.
- `--slab-dir` runs offline: each request URL is read from `<slab-dir>/<sha1 of the URL>.parquet` (the hex sha1 of the
  URL's UTF-8 bytes, `om_extract.run.slab_file(url)`). A URL without a file is an error that names it; the info table
  is one such URL, so the time extent is the catalog's.

**The request workflow's call** (oceanmetrics/indicators `requests.yml`): the chunked engine over the whole record,
the place read from the gazetteer, and erddap-places' `stats/` layout with its STAC Item:

```sh
uv run om-extract run --collection https://storage.oceanmetrics.io/gazetteer/erddap/dhw_5km/collection.json \
  --place NERRS:WEL --coll noaa_nerrs --var CRW_SST --chunks ~/.cache/oceanmetrics/chunks \
  --stats-dir out/gazetteer/stats --out out/run
```

- `--chunks DIR` runs `extract(..., chunks=DIR)` (see "Parity"): the live extent, `window(ext, 'all')` unless
  `--t0`/`--t1` pin it, only the chunks the store lacks fetched. Without it `--t0` and `--t1` are required.
- No `--geometry`: the place is one row of `<base>/<coll>/places.parquet` (`--base`, default the published
  gazetteer), or of the private full-resolution copy `<places-internal>/<coll>/places.parquet` when this machine holds
  one (a reference collection, `mr_*`; `--places-internal`, default `$PLACES_INTERNAL`, else
  `~/data/oceanmetrics/places-internal`), with its `name` for the Item (`places.place_record()`).
- `--stats-dir DIR` writes, in upload order, `DIR/<dataset>/<variable>/<place>.parquet` (the chunked writer's file),
  `DIR/<dataset>/<variable>/<place>.provenance.json` (`provenance_text()`) and `DIR/items/<dataset>_<variable>_<place
  with - for :>.json`, the Item (`stac.build_item()`, erddap-places' `buildItem()`: lobe-box geometry, the bbox across
  +-180 for a split place, `erddap-places:*` properties, `status` complete or partial, the assets' `file:size` and
  multihash `file:checksum`, the dataset's `via` link). Built from the published files of NMS:CINMS x CRW_SST, the
  Item is the published one byte for byte except its asset hrefs (`../` here; erddap-places fixed `../../` on
  2026-10-22 and the published Item predates it), a `live` test. Nothing here uploads: the workflow does, and until the parity criterion is restated (the
  engine's sums differ from the published ones in the last bits, see "Parity"; ledger 2026-10-23) it does not: its
  `OM_STATS_PUBLISH` defaults to a dry run that lists the files and attaches them to the run as an artifact.

In Python the steps are three calls (`om_extract/engine.py`, `om_extract/values.py`):

```python
from om_extract.engine import extract
from om_extract.values import values_rows, provenance_for
from om_extract import contract

x = extract(collection, "NMS:CINMS", wkb_bytes, "CRW_SST", "2026-09-01", "2026-09-30")   # x.stats, x.urls, x.mask_cells
contract.write_values(values_rows(x, stat="mean_wt"), "out", provenance_for(x, "0.4.0"))
```

## Parity

`om-extract parity` runs the engine live for one place over the window of erddap-places' published file, downloads
that file with its provenance and STAC item, and says where they agree. It is the gate of build week 1: the engine's
stats against the runner's.

```sh
uv run om-extract parity --dataset dhw_5km --variable CRW_SST --place NMS:CINMS --out parity/cinms \
  --reference-chunks ~/actions-runner/erddap-places/_work/erddap-places/precompute-chunks --fetch all
# every NMS: place, both variables: <out>/<variable>/parity_<place>.json and <out>/summary.md
uv run om-extract parity --dataset dhw_5km --variable CRW_SST --variable CRW_BAA --place all --out parity/batch \
  --reference-chunks ~/actions-runner/erddap-places/_work/erddap-places/precompute-chunks --fetch last:3
```

| Option | |
|---|---|
| `--chunks DIR` | the engine's chunk store (default `<out>/chunks`) |
| `--out DIR` | `parity.json`, the engine's stats file and provenance in `engine/`, the published ones in `ref/` |
| `--base URL` | the gazetteer (default `https://storage.oceanmetrics.io/gazetteer`, or a local copy): `erddap/<dataset>/collection.json`, `<coll>/places.parquet`, `stats/...` |
| `--reference-chunks DIR` | the runner's chunk store (`PRECOMPUTE_CHUNKS`), read only |
| `--fetch all\|none\|last:N` | with `--reference-chunks`, which chunks the engine computes itself; the rest are copied from the runner's store |
| `--window published\|live` | the published file's `start_date`..`end_date` (default), or `window(ext, 'all')` |
| `--tries N` | tries in a row without a new chunk before a place is given up, resting 10, 30, 60 min between (default 3) |

It compares:

- **rows**, matched by `date` (categorical: `date, class`), every column: `==` for `n`, the ids and the date; floats
  exact first, else the rows that differ, the largest absolute and relative difference and the largest distance in
  ulps. Duplicated keys on either side are counted. The verdict is `exact`, `float sums differ (max rel x)` (keys,
  integers, strings, `min`, `max`, `p10`, `p90` equal; only order-dependent sums differ, by at most 1e-12 relative)
  or `differs`;
- **provenance**, key by key (which differ, and for a list its first differing element); `generated` always differs;
- **item** properties stated from the provenance (`start_datetime`, `table:row_count`, `erddap-places:*`);
- **bytes** of the parquet, and why not (the writer's version, row groups, schema, codecs, values);
- **chunks**: the mask key against the runner's directory name, and every chunk the engine fetched against the
  runner's chunk of the same name.

**The chunk store is the runner's.** `om_extract.chunks` keeps chunk rows exactly where and as precompute does:

```
<chunks>/<dataset_id>/<variable>/<place_id>/<mask_key>/<start>_<end>.parquet     # stats.ts:357-360
<chunks>/.slabs/<dataset_id>/<variable>/<place_id>/lobe<i>_<start>_<end>.parquet   # precompute/.cache: deleted once aggregated
```

`mask_key()` is `maskKey()` (`stats.ts:289-294`) character for character: sha1 over `JSON.stringify([baseUrl,
datasetId, depth ?? null, name, valueExpr, statsTemplate])`, then `${lon},${lat},${weight};` per mask cell with
JavaScript's number printing, first 12 hex. So the engine names the runner's directory (`NMS:CINMS` x `CRW_SST`:
`a91ae222cbfe`, a test), and a store copied from the runner resumes in the engine. `plan_chunks()` is `planChunks()`:
done chunks in the window are kept whatever size they were cut at, a short last chunk is refetched to the new end and
superseded, gaps are cut at the chunk size, other files dropped. With `--reference-chunks` the engine plans from the
runner's chunk names (so its slices, `griddap_chunks` and `griddap_requests` are the runner's, mixed sizes included),
fetches what `--fetch` says and compares each fetched chunk with the runner's.

`extract(..., chunks=DIR)` is that run in Python (`engine.extract_chunked()`, `computeOne()`): the live extent from
`info/<id>/index.json` (5 tries, 5/20/60/120 s apart, a 4xx at once; no fallback to the catalog), axes from the
server per lobe (`fetchAxis`, 3 tries of 120 s), the chunk size from the largest lobe's axis cells for every lobe,
two requests in flight 0.75 s apart, each slab downloaded with one retry, aggregated by
`COPY (SELECT <selectList> FROM (<template over SELECT DISTINCT * FROM read_parquet([lobe slabs])>))` to
`<chunk>.tmp` and renamed, and the stats file written by
`COPY (SELECT *, place_id, dataset_id, variable FROM (SELECT DISTINCT * FROM read_parquet([chunks])) ORDER BY date[, class]) TO ... (FORMAT PARQUET, COMPRESSION ZSTD)`
(one row group), with one rule more than the TS: a row outside its chunk's `<start>..<end>` is dropped and one row per key is kept (`assemble_sql()`; see below). DuckDB runs with one thread. A zero-row place writes the file and its provenance. The provenance has the TS keys in the TS order,
`griddap_chunks` from the chunk instants and `sql` rendered with the TS's slab placeholder, written as
`JSON.stringify(prov, null, 2)` (`provenance_text()`).

The place geometry comes from the gazetteer by range: `places.place_geometry(place_id, coll, base)` reads one WKB row
of `<base>/<coll>/places.parquet` through DuckDB (`place_id =` pushed down to the row groups, HTTP ranges).

**What parity found (2026-10-22, `dhw_5km`).** The order-independent columns (`date`, `n`, `min`, `max`, `p10`, `p90`,
the ids) match exactly on every chunk the engine recomputed (85 CINMS chunks; PacIOOS then stopped answering).
`mean`, `mean_wt`, `sd` and `weight_sum` differ in the last bits (at most 1.1e-13 absolute, 2.8e-14 relative):
DuckDB's multi-threaded aggregates add in a different order on every run, the runner's included (the same slab and
mask twice give different sums on half the days), so the published sums are one draw that no rerun reproduces. Bit
for bit on those columns cannot be met by anyone; the engine runs DuckDB with one thread (`threads=1`), so its own
runs are reproducible to the byte (a test). Given the runner's chunk rows, the engine's plan reproduces every
published `griddap_chunks` and its writer, under DuckDB 1.5.5 (the runner's), writes the 30 published files without a
duplicated key byte for byte. Six published files carry a date (categorical: a date and class) twice: a chunk that
ends on a day missing from the ERDDAP time axis is answered up to the next day, the next chunk's first, and the TS's
`SELECT DISTINCT` keeps both rows because their sums differ in the last bits. The engine keeps a day only from the
chunk whose span holds it, one row per key (`assemble_sql()`, a test with a gap in a synthetic axis). Python DuckDB
2.0 writes other bytes (its version string; the constant id columns `required`). The tally per sanctuary x dataset,
the criterion proposed for the gate and the commands to resume are in the parity directory's `SUMMARY.md`
(`/Volumes/msens_big/_big/oceanmetrics/parity/` on the Mac mini).

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

## Readers: COG and H3

A raster is summarised on its own grid by the mask rule; H3 membership serves only layers whose native grid is H3.
`readers.for_collection(doc)` dispatches in order: `erddap:protocol` is ERDDAP, `h3:resolutions` is H3, a STAC Item
(`type: Feature`) with a data COG asset is COG.

**The COG reader** (`om_extract/readers/cog.py`, `CogReader(item, *, asset="cog", fetch=None, item_href=None)`) reads a
STAC Item that passes the platform `cog` profile (`InvalidProfile` lists the errors) with rasterio: a local path, or an
`http(s)` href that GDAL reads by range requests; `fetch` is a rasterio `opener`. Its rules:

- **CRS**: EPSG:4326 only, in `proj:code` and in the file; anything else raises `UnsupportedCrs`. The EPSG:3857 copies
  the map draws are for display and are never summarised. Longitudes in [-180, 180].
- **Axes**: the pixel centres of the GeoTIFF's geotransform (`x0 + (col + 0.5) * dx`) over every pixel whose
  footprint overlaps the bbox, plus one pixel of margin on each side, so a lone centre never gets a +-0.5 deg cell from
  the mask's edge rule; north to south for a north-up raster. A bbox that misses the raster gives no pixels.
- **Band**: `var` is a band name of the asset's `bands` or `raster:bands`, or a 1-based number; None is `atlas:bidx`,
  else 1. Nodata (the file's, else the band's: a number, `nan`, `inf`, `-inf`) and NaN are null; `raster:scale` and
  `raster:offset` are applied.
- **Time**: a COG is one time step, at the item's `datetime`, else `start_datetime`, spanning `[start_datetime,
  end_datetime]`. A window that overlaps the span gives that step, one outside it zero rows; an item with no time is in
  every window with a null time.
- **The engine**: `engine.extract()` takes a COG Item (a dict with `type: Feature`) or any reader object in place of
  the collection and hands it to `engine_reader.extract_reader()`: the same lobes, `grid_mask()` on the native axes and
  `stats_daily` SQL, the same `Extraction` and stats columns (`dataset_id` is the item id, `urls` the asset href). A
  place or a window that misses the raster gives no rows. `t0` and `t1` are required, and `chunks=` raises: the chunk
  store is the ERDDAP precompute. `values_rows()` refuses a COG extraction until the atlas indicators fix its ids and
  grains (vision plan, "From SDMs to indicators", step 2).

```python
x = extract(item, "NMS:CINMS", wkb_bytes, "density", "1991-01-01", "2018-12-31")   # x.stats: one row, the item's step
```

**The H3 reader** (`om_extract/readers/h3.py`, `H3Reader(collection, *, resolution=None, fetch=None, root=None,
keys=None)`) reads a collection that passes the `h3` profile. `cells(res, h3_ids)` returns the layer's rows for those
cells: the whole file (`h3:layout.template` with `{res}`) for a resolution that is not split, only the parent
partitions (`parent_template`, `p=<parent>` at `parent_res[res]`, computed by `h3_parent()` with bit arithmetic) for
one that is. Paths resolve against `root`, else the directory of the `rel: self` link; other placeholders come from
`keys`; a missing partition holds no rows. A `string` cell column matches lowercase hex, a `uint64` one the integer.
With `h3:periods`, each period is a file when the template holds its placeholder, else a column of that name; it comes
back as `period`. `axes()`, `slab()` and `slice()` raise: an H3 layer has cells, not axes.

**Membership** (`om_extract/membership.py`): `load_membership(slug, place_id, res, base=GAZETTEER)` reads
`<base>/<slug>/h3/membership.json`, then `h3/res=<r>/membership.parquet`, or for res 6 and 7 only the place's parent
partitions `h3/res=<r>/p=<parent>/membership.parquet` named by `h3/parents.parquet`, with a `place_id =` filter in
DuckDB (local paths and `https` alike). Columns `place_id, h3, share` (float32); the gazetteer keeps a cell when
its share is at least `min_share` (0.01) or it holds that share of the place. `base` defaults to
`https://storage.oceanmetrics.io/gazetteer`.

**`extract_h3(collection, place_id, membership, var, res)`** (`om_extract/engine_h3.py`) joins the layer's cells with
the place's membership in DuckDB (`om_extract/sql/stats_h3.sql`, the grid rule with the membership as the mask):
per period, over the place's cells that have a value, `mean_wt = sum(value * share) / sum(share)`, `weight_sum =
sum(share)`, and `n` (the cells with a value), `mean`, `sd` (n - 1), `min`, `max`, `p10`, `p90` unweighted. The stats
table has the grid stats' columns (`H3_STATS_COLUMNS`) with `date` the first day of the period (`1990s` is
1990-01-01; null and one row for a layer without periods) and `period` the label. `values_rows()` gives one contract
row per period: indicator `h3:<collection id>:<var>:mean` (`mean_unw` for the unweighted mean), `time_grain` the
periods' grain or `all`, `unit` the column's `unit` in `table:columns`, else `1`; `h3_provenance_for()` gives its
provenance.

```python
from om_extract.membership import load_membership
from om_extract.engine_h3 import extract_h3, h3_provenance_for

mem = load_membership("places", "NMS:CINMS", 5)
x = extract_h3(obis_h3_collection, "NMS:CINMS", mem, "shannon", 5)        # root from the collection's self link
contract.write_values(values_rows(x), "out", h3_provenance_for(x, om_extract.__version__))
```

## Readers: Copernicus Marine

`om_extract/readers/copernicus.py` is the Copernicus Marine place producer of oceanmetrics/indicators
(`om_indicators/producers/cmems_place.py`, which is now a thin call into it) as a reader. `CopernicusReader(dataset_id,
variable, *, info=None, subset=None, version=None)` puts the Toolbox behind the reader interface (`extent`, `axes`:
the catalogue's lattice, `slab`, `slice`, `source_block`: `copernicus:*`); `info` is the catalogue record
(`info_from_catalogue()`, default the live catalogue, which needs no login) and `subset(dataset, var, window, start,
end)` the data (default `toolbox_subset()`, with the maintainer's login in `~/.copernicusmarine`; nothing here reads
it). `extract_place(reader, place_id, geom, coll)` is the one call: windows, size check, subsets, mean, rows. The rules:

- **Windows**: the polygon's bbox grown by one grid cell, clamped to the dataset's range; a place across the
  antimeridian (split at +-180) gets one window per side.
- **Subset**: the surface layer only (depth 0 to 0.51 m), the shallowest level kept, the whole record or `--t0`/`--t1`.
- **Mean**: per time step the mean of the cells whose centres fall inside the polygon (boundary included), NaN skipped,
  and the sample SD of those cells; a cell in two windows counts once. This is eco-indicators' `terra::extract(...,
  fun = mean)`, not the grid rule's partial-area weights: the Copernicus record keeps eco-indicators' method.
- **Rows**: a month row is the monthly mean, `n = 1`, the cell SD in `sd`; a year row the mean of its 12 monthly means,
  `n = 12`, only for a complete year. Indicator `cmems:<dataset id>:<variable>:mean`.
- **Provenance**: `dataset` the Copernicus Marine product (id, title, DOI, the "E.U. Copernicus Marine Service
  Information; <title>; <doi>" citation); `sources[]` the subset (`copernicusmarine:<dataset id>`, its version and the
  Toolbox version in the title), the product page and the polygon's source.

```sh
uv sync --extra cmems          # copernicusmarine, xarray and the NetCDF backends (h5netcdf, h5py, netCDF4)
uv run om-extract run --reader copernicus --dataset cmems_mod_glo_bgc_my_0.25deg_P1M-m --var no3 \
  --place MRGID:5670 --coll mr_eez --out out/ [--geometry place.geojson] [--t0 1993-01-01 --t1 2024-12-01] [--max-values N]
```

It writes `<out>/values/indicator=cmems:<dataset>:<var>:mean/coll=<slug>/data.parquet` through the contract writer and
`<out>/provenance.json` (windows, grid, cells, depth used, Toolbox version). The tests stand a small xarray DataArray in
for the subset (`tests/fixtures_copernicus.py`, ported from indicators' fixture with the rules), so none needs the
Toolbox or a login; indicators keeps a test that the producer's output folder is byte for byte what it wrote before
the move.

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
