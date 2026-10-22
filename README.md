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
