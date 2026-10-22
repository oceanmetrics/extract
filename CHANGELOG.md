# om-extract (unreleased: mask branch)

- New `om_extract.geom`: `wkb_to_geojson()` / `from_wkb()` (a port of erddap-places `wkb.ts`) and `place_lobes()` /
  `lobes()` (gazetteer.ts `placeLobes()`: one lobe per side of +-180, parts untouched).
- New `om_extract.mask`: `grid_mask(lobe, lat_axis, lon_axis)` returns a `MaskTable` (`lon`, `lat`, `weight`,
  `n_candidates`, `n_inside`, `method`; `to_arrow()` gives the SQL's `mask` table), a port of gridMask.ts: cells by
  centre with partial-area weights by turf's spherical area. Equal to the TypeScript bit for bit on 15 fixtures
  (CINMS, FKNMS, HIHWNMS, PMNM on 0.05 and 1/12 deg grids, and synthetic squares), written by the TypeScript itself.
- New `om_extract.stats`: `stats(slab, mask, template)` runs erddap-places' `stats_daily.sql`, `stats_categorical.sql`
  and `last_step.sql` (shipped byte-identical in `om_extract/sql/`) in DuckDB; `render()` / `lit()` follow `sql.ts`.
- New dependency: `shapely>=2.0`.

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
