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
