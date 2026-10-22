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

## Commands

```sh
uv sync --group dev
uv run pytest -q
uv run om-extract --version
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
