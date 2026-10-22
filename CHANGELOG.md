# om-extract (unreleased)

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
