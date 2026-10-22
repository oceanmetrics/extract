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
