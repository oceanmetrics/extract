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
