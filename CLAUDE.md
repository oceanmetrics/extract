# CLAUDE.md

Guidance for Claude Code in `oceanmetrics/extract`: the Python extraction engine of the Ocean Metrics platform. It
summarises a layer (ERDDAP grid, Zarr, COG, H3 Parquet, point records) by gazetteer places into indicator values with
one weighting rule (see README.md "The rule"), on the Mac mini runner and with the same SQL in the browser.

**This repo is public from its first commit.** Never commit secrets, private emails or internal data: no Marine
Regions copies, no PPOW geometry. Fixtures are synthetic or from public sources.

## Commands

```sh
uv sync --group dev
uv run pytest -q                    # must be green before every commit
uv run pytest -q tests/test_package.py -k version
uv run om-extract
```

## Conventions

- 2-space indentation, snake_case, lowercase comments.
- One pytest per rule, with a small synthetic fixture asserting the exact output. A bug fix adds a named regression
  test. Pytest green before every commit.

## Structure first

Core logic lives in a documented, tested function in `om_extract/`. Notebooks and the CLI call it; they hold no logic
of their own, so a test can assert what they run.
