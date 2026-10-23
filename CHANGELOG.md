# om-extract 0.6.3 (2026-10-23)

- `om_extract.storage.DEFAULT_STORAGE_HOST` is now `STORAGE_HOST` (`https://storage.oceanmetrics.io`), flipped with
  `@oceanmetrics/catalog` 0.6.0. Why: the storage cutover of 2026-10-23 is done (`storage.oceanmetrics.io` is served by
  the om-gazetteer Worker from R2, and `platform/scripts/storage-check.sh` passes 21 of 21 on it), so every
  `storage_url()` without a host, and every run without `OM_STORAGE`, reads the bucket from the storage host. Rollback:
  `OM_STORAGE=s3` per run.
- tests: `OM_STORAGE_CASES` points `tests/test_storage.py` at another copy of the platform's case table (a branch in a
  worktree); the default stays the sibling checkout.

# om-extract 0.6.2 (2026-10-23)

- DuckDB pinned to exactly 1.5.5 (was `>=1.4`, resolved to 2.0.0), the runner's version: the parity criterion asks for
  byte-identical stats files under the same DuckDB with one thread, and a parquet records its writer's version.
- The parity criterion Ben accepted for build week 1 is the harness's verdict: `parity.verdict(cmp)` passes when the
  schema and the keys are the same with one row per key in the engine's file, every column but the sums (`SUM_COLUMNS`:
  `mean`, `mean_wt`, `sd`, `weight_sum`, categorical `weight`, `frac_area`) is `==`, and the sums are within `SUM_REL`
  = 1e-9 relative; a key the published file carries twice (the duplicated-date bug) is counted, not failed.
  `rows_verdict()` now says `exact`, `within the criterion (...)` or `differs` (was `float sums differ`, 1e-12), and
  `parity.json` carries the `verdict`. `compare_rows()` adds `keys_only_ref` and `keys_only_engine` (distinct keys).
- New `om-extract compare ENGINE.parquet REF.parquet [--json FILE]` (`parity.compare_files()`, `compare_text()`):
  two stats files by the criterion and their bytes; exit 1 when the criterion fails.

# om-extract 0.6.1 (2026-10-23)

- New `om_extract.storage`, the python twin of `@oceanmetrics/catalog/storage` (platform catalog 0.5.1, build week 6
  step 0): the hosts `STORAGE_HOST`, `S3_PATH_STYLE`, `S3_VIRTUAL_HOST`, `WORKER_PREVIEW` (`STORAGE_HOSTS` by name),
  `DEFAULT_STORAGE_HOST` (path-style S3 until the DNS cutover flips it), `storage_url(key, host=, api_key=)`,
  `rewrite_storage_urls(value, from_, to)` (text, or a JSON value walked; any other host untouched),
  `rewrite_storage_urls_counted()`, `count_storage_urls()`, `parse_storage_host()`, `resolve_storage_host()` and
  `storage_host_from_env()` (the `OM_STORAGE` override). Held to the platform's case table
  (`packages/catalog/fixtures/storage/cases.json`) by `tests/test_storage.py`.

# om-extract 0.6.0 (2026-10-23)

- New Copernicus Marine reader `readers.copernicus.CopernicusReader(dataset_id, variable, *, info=None, subset=None,
  version=None)` and `extract_place()`: the Toolbox subset of the surface layer per window (one per side of +-180), the
  mean and sample SD of the cells whose centres fall inside the polygon (eco-indicators' method), month rows (`n = 1`)
  and year rows (complete years, `n = 12`), `build_provenance()` with the Copernicus product as `provenance.dataset` and
  the dataset id and version as the data source. Moved from oceanmetrics/indicators' `cmems_place` producer with its
  rules and test fixture. New extra `cmems` (copernicusmarine, xarray, NetCDF backends); xarray joins the dev group.
- `om-extract run --reader copernicus --dataset <id> --var <v> --place <id> [--coll] [--geometry] [--t0 --t1]
  [--max-values]` writes the values file and `provenance.json`.
- New `om_extract.stac`: `build_item()` (erddap-places' `buildItem()`: id, lobe-box geometry, bbox across +-180,
  properties, status, assets with `file:size` and multihash `file:checksum`, links), `write_stats()` (the `stats/`
  layout: parquet, provenance, `items/<id>.json`), `item_text()`, `union_bbox()`, `item_id()`, `stats_href()`. The
  published NMS:CINMS x CRW_SST Item is rebuilt byte for byte from its files, the asset hrefs aside (`../`, erddap-places'
  fix of 2026-10-22).
- `om-extract run` gains `--chunks` (the chunked engine; the window is then optional), `--stats-dir` (the `stats/`
  layout and its Item), `--base` and `--places-internal`; `--geometry` is optional (the place from the gazetteer, or
  the private copy of a reference collection, `places.place_record()`). A place with no rows writes its stats but no
  values file.
- New `places.place_record(place_id, coll, base, internal)` (geometry, name, where it was read) and
  `places.internal_dir()`.

# om-extract 0.5.0 (2026-10-23)

- New `om_extract.chunks`: erddap-places' chunk store (`<root>/<dataset>/<variable>/<place>/<mask_key>/<start>_<end>.parquet`,
  the runner's `PRECOMPUTE_CHUNKS` layout), `mask_key()` (the TS `maskKey()`, so a key names the runner's directory),
  `plan_chunks(existing, window, chunk_days)` (keep / todo / drop / supersede as `planChunks()`), `window()` (`window(ext,
  'all')`), `ChunkStore`, `write_atomic()`.
- `engine.extract()` gains `chunks=DIR` (and `stats_path=`, `existing=`, `threads=`, `should_stop=`): precompute
  `computeOne()` through the chunk store. The live extent is required (5 tries, then an error), axes come from the
  server, the largest lobe sets the chunk size for every lobe, only missing chunks are fetched, each chunk is
  aggregated by DuckDB `COPY` and written atomically, slabs are deleted, and the stats file is
  `COPY (SELECT DISTINCT * ... ORDER BY date[, class])` with ZSTD in one row group, zero rows included.
  `t0`/`t1` are optional with `chunks=` (default: the whole record). New `engine.live_extent()`, `download()`,
  `select_list()`, `ts_sql()`, `provenance_text()`, `StopRequested`.
- `engine.stats_provenance()`: the TS key order, `griddap_chunks` from the chunk instants of a chunked run, `sql`
  rendered with the TS slab placeholder, `erddap_version` left out when the collection has none.
- New `om_extract.places`: `place_geometry(place_id, coll, base)` reads one WKB row of `<base>/<coll>/places.parquet`
  (DuckDB, `place_id` pushed down, HTTP ranges), `place_ids()`, `place_collection()` from `places_index`.
- New `om_extract.parity` and CLI `om-extract parity --dataset --variable --place [--chunks] [--out] [--base] [--coll]
  [--reference-chunks] [--fetch all|none|last:N] [--window published|live] [--tries]`: the engine against
  erddap-places' published stats (rows per column exact or max abs / ulp difference, provenance keys, item properties,
  parquet bytes, fetched chunks against the runner's); several places or variables write
  `<out>/<variable>/parity_<place>.json` and `<out>/summary.md`.
- The chunked engine runs DuckDB with one thread (`threads=1`), so two runs of one slab write identical bytes (the
  runner's multi-threaded sums differ from run to run in the last bits).
- The stats file keeps a day only from the chunk whose span holds it, one row per key (`engine.assemble_sql()`): a chunk
  ending on a day missing from the ERDDAP time axis is answered up to the next day, and erddap-places' published files
  carry that day twice (six of the 36 dhw_5km files). Files without such a day are written exactly as erddap-places
  writes them (30 of 30 byte for byte under DuckDB 1.5.5).
- Tests marked `live` reach real servers and run only with `OM_LIVE=1`.
- New COG reader `readers.cog.CogReader(item, *, asset="cog", fetch=None, item_href=None)`: a STAC Item that passes the
  `cog` profile, read with rasterio (local path, or range requests over HTTP); `axes(bbox)` the native pixel centres
  over the bbox with one pixel of margin, `slab()` / `slice()` one band (name, number or `atlas:bidx`) with nodata and
  NaN as null and `raster:scale` / `raster:offset` applied, the item's one time step (none outside its span, a null
  time for a timeless item), `source_block()` the href, band, nodata and `proj:code`. EPSG:4326 only: any other CRS
  raises `UnsupportedCrs` (3857 copies are for display).
- `engine.extract()` takes a COG Item or a reader object in place of the collection and hands it to the new
  `engine_reader.extract_reader()`: the same lobes, mask, `stats_daily` SQL and `Extraction` on the raster's own grid.
  It needs `t0` and `t1`, and `chunks=` with a COG or reader raises ValueError (the chunk store is the ERDDAP precompute).
  `values_rows()` of a COG extraction raises `NotImplementedError` until the atlas indicators fix their ids.
- New H3 reader `readers.h3.H3Reader(collection, *, resolution=None, fetch=None, root=None, keys=None)`: a collection
  that passes the `h3` profile; `cells(res, h3_ids)` reads the whole file of an unsplit resolution or only the parent
  partitions of a split one (`h3_parent()`), with periods from the path or a column; `period_date()`.
- New `om_extract.membership.load_membership(slug, place_id, res, base=GAZETTEER)`: the gazetteer's place x H3
  membership (`membership.json`, then the res file or the place's parent partitions from `parents.parquet`) in DuckDB.
- New `om_extract.engine_h3.extract_h3(collection, place_id, membership, var, res)`: the layer's cells joined with the
  membership, share-weighted (`sql/stats_h3.sql`), one row per period (`H3_STATS_COLUMNS`); `values.values_rows()`
  gives `h3:<collection>:<var>:mean` rows at the periods' grain or `all`; `h3_provenance_for()`.
- `readers.for_collection()` dispatches `h3:resolutions` to H3 and a Feature with a data COG asset to COG; the profile
  errors of both are `readers.InvalidProfile` (a subclass of `InvalidCollection`).
- New dependency: `rasterio>=1.4`.

# om-extract 0.4.0 (2026-10-22)

- New `om_extract.engine`: `extract(collection, place_id, geometry_wkb, var, t0, t1, *, fetch=None, coll=None,
  categorical=None)` runs one place x variable x window end to end (erddap-places' precompute `computeOne()`): lobes,
  axes, mask (0..360 grids masked in -180..180 and joined in the dataset's frame), the chunked slab requests, the
  DISTINCT slab and `stats_daily` / `stats_categorical`. It returns an `Extraction` with the stats table in
  erddap-places' published columns (`STATS_COLUMNS`, `CATEGORICAL_COLUMNS`: `frac_area`, `pct_cells`) and its identity
  columns, the request URLs, the mask, the mask cell and lobe counts, the SQL, the source block, the dataset record and
  the window. `value_expr()` converts a Kelvin variable to deg C as erddap-places does; `stats_provenance()` gives the
  stats file's provenance with erddap-places' keys.
- New `om_extract.values`: `values_rows(extraction, *, stat="mean_wt", grain=None)` turns the stats into contract rows at
  the native grain (indicator `erddap:<dataset>:<variable>:<stat>`, categorical `...:share`; `n` the cells with data;
  `sd` the cell SD for `mean_wt` and `mean`); another grain raises "aggregation to a coarser grain is build week 1".
  `provenance_for(extraction, code_version)` states producer, dataset (from the collection's providers, licence and
  links), one `data` source per request URL plus the collection as `product`, method, code, licence and source version.
- New CLI `om-extract run --collection --place --geometry --var --t0 --t1 --out [--stat] [--coll] [--slab-dir]`
  (`om_extract.run.run()`): writes `stats.parquet`, `provenance.json` and the values file, validates it and prints the
  row count; exit 1 with the message on any contract error. `--slab-dir` serves each URL from `<sha1 of the URL>.parquet`.

# om-extract 0.3.0 (2026-10-22)

- New `om_extract.geom`: `wkb_to_geojson()` / `from_wkb()` (a port of erddap-places `wkb.ts`) and `place_lobes()` /
  `lobes()` (gazetteer.ts `placeLobes()`: one lobe per side of +-180, parts untouched).
- New `om_extract.mask`: `grid_mask(lobe, lat_axis, lon_axis)` returns a `MaskTable` (`lon`, `lat`, `weight`,
  `n_candidates`, `n_inside`, `method`; `to_arrow()` gives the SQL's `mask` table), a port of gridMask.ts: cells by
  centre with partial-area weights by turf's spherical area. Equal to the TypeScript bit for bit on 15 fixtures
  (CINMS, FKNMS, HIHWNMS, PMNM on 0.05 and 1/12 deg grids, and synthetic squares), written by the TypeScript itself.
- New `om_extract.stats`: `stats(slab, mask, template)` runs erddap-places' `stats_daily.sql`, `stats_categorical.sql`
  and `last_step.sql` (shipped byte-identical in `om_extract/sql/`) in DuckDB; `render()` / `lit()` follow `sql.ts`.
- New dependency: `shapely>=2.0`.

# om-extract 0.2.0 (2026-10-22)

- New `om_extract.contract`: the values contract 1.2.0, moved from oceanmetrics/indicators `om_indicators/contract.py`
  with every name and signature kept (`CONTRACT_VERSION`, `ARROW_SCHEMA`, `CORE`, `SD_MIN_N`, `ContractError`,
  `period_start()`, `validate_rows()`, `validate_provenance()`, `validate_table()`, `values_path()`, `validate_file()`,
  `validate_tree()`, `to_table()`, `write_values()`, `read_values()`, `utc_now()`), so callers change only the import.
  Its schemas are the platform's `values-row` and `values-provenance`, read through `om_extract.schemas.load_schema()`
  (`contract.load_schema(name)` takes those names). `validate_coverage()` stays in indicators (its coverage table).
- README: a "Contract" section (the values file, the 1.2 columns, the provenance).
- New `om_extract.readers`: the reader interface (`Reader` protocol: `extent()`, `axes(bbox, depth)`,
  `slab(bbox, t0, t1, var)`, `slice(bbox, t, var)`, `source_block()`; dataclasses `Extent` and `Axes`) and a registry,
  `readers.for_collection(collection)`, that dispatches on the profile (`erddap:protocol` present means ERDDAP).
- New ERDDAP reader `readers.erddap.ErddapGrid`, ported from erddap-places: `griddap_url`, `tabledap_url` (byte-identical
  to the TypeScript), `pick_format`, `chunk_days_for` (90 days or 100,000 values a request), the live time extent from
  `/info/<id>/index.json`, axes from `cube:dimensions` or the server, and `slab()` as chunked Parquet requests
  (injectable `fetch`, one retry, a per-attempt timeout, errors naming the host) returned as the long table
  `time, lat, lon, value`.
- New `om_extract.layers`: `load_collection(path_or_url)` and `dataset_of(collection)` (erddap-places' `toDataset()`).
- New CLI `om-extract layer info <collection.json> [--info info.json] [--json]`.
- New dependency: `httpx>=0.28`.
- `schemas.schema_dir()` also looks relative to the working directory (`node_modules/@oceanmetrics/catalog/schemas`, then `../platform/...`), so an app that installs om-extract from git finds the schemas without `OM_SCHEMAS_DIR`.

# om-extract 0.1.0 (2026-10-22)

- New `om_extract.schemas`: `schema_dir()` finds the platform schemas (`$OM_SCHEMAS_DIR`, the npm package in
  `node_modules`, or the sibling `../platform` checkout), `load_schema(name)` and `validate(name, doc)`.
- New `om_extract.notebooks`: reads the `om:` front-matter block of workflow notebooks, validates it against the
  `om-notebook` schema, checks the graph (duplicate target, unknown dependency, cycle) and generates the run order,
  the mermaid DAG and the index page.
- New CLI subcommands `om-extract notebooks check <dir>` (exit 1 on any error) and
  `om-extract notebooks index <dir> [--title ...]` (writes `index.qmd`, `_om/order.json`, `_om/dag.mmd`); new
  `--version` flag.
- New dependencies: `jsonschema>=4.23`, `pyyaml>=6`.
- CI checks out `oceanmetrics/platform` beside the repo so the schemas resolve.
