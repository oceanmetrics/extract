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
