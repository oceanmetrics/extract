# om-extract 0.5.0 (2026-10-22)

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
- Tests marked `live` reach real servers and run only with `OM_LIVE=1`.

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
