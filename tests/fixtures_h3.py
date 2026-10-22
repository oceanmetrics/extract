"""Synthetic fixtures for the H3 reader, the membership loader and extract_h3: obis-h3-like collections and layer
files, and a gazetteer membership tree in the published layout, written with pyarrow. The cell ids are real H3 cells
(near lat 1.5, lon 1.5 and lat 10.5, lon 20.5), their parents as `h3.cell_to_parent` gives them (h3 4.x)."""
import copy
import json

import pyarrow as pa
import pyarrow.parquet as pq

# res 5: three cells of the place and one outside it
R5 = ["85754527fffffff", "85756a43fffffff", "85756a53fffffff"]
R5_OUT = "85756a57fffffff"
# res 7: two cells under the res 3 parent 83756afffffffff and one under 836bacfffffffff
R7_A = ["87756a522ffffff", "87756a535ffffff"]
R7_B = ["876bacad8ffffff"]
P3_A, P3_B = "83756afffffffff", "836bacfffffffff"

COLUMNS = [{"name": "h3", "type": "VARCHAR"}, {"name": "cell_id", "type": "BIGINT"}, {"name": "n", "type": "BIGINT"},
           {"name": "sp", "type": "BIGINT"}, {"name": "shannon", "type": "DOUBLE"}, {"name": "es", "type": "DOUBLE"}]


def collection(**over) -> dict:
  """An obis-h3-like collection (passes the `h3` profile): res 5 whole, res 7 split by res 3 parents."""
  doc = {
    "type": "Collection", "stac_version": "1.1.0", "id": "obis_h3_test", "title": "synthetic obis-h3",
    "description": "synthetic", "license": "CC-BY-4.0", "links": [],
    "providers": [{"name": "Example OBIS node", "roles": ["producer"], "url": "https://example.org"}],
    "h3:resolutions": [5, 7], "h3:cell_column": "h3", "h3:cell_type": "string",
    "h3:layout": {"template": "all/res={res}/data_0.parquet",
                  "parent_template": "all/res={res}/p={parent}/data_0.parquet", "parent_res": {"7": 3}},
    "table:columns": copy.deepcopy(COLUMNS),
  }
  for k, v in over.items():
    if v is None:
      doc.pop(k, None)
    else:
      doc[k] = v
  return doc


def layer_rows(cells, values, var="shannon", **extra) -> pa.Table:
  cols = {"h3": pa.array(cells, pa.string()), "cell_id": pa.array([int(c, 16) for c in cells], pa.int64()),
          var: pa.array(values, pa.float64())}
  cols.update(extra)
  return pa.table(cols)


def write(path, table: pa.Table) -> str:
  path.parent.mkdir(parents=True, exist_ok=True)
  pq.write_table(table, path)
  return str(path)


def membership_tree(root, rows: dict[int, list[tuple[str, str, float]]], parent_res: dict[int, int] | None = None,
                    slug="places") -> str:
  """The gazetteer layout under `root/<slug>/h3/`: `res=<r>/membership.parquet`, or per parent
  `res=<r>/p=<parent>/membership.parquet` for a split res, `parents.parquet` and `membership.json`; returns `root`."""
  from om_extract.readers.h3 import h3_parent
  parent_res = parent_res or {}
  base = root / slug
  schema = pa.schema([("place_id", pa.string()), ("h3", pa.string()), ("share", pa.float32())])
  files, parents = [], []
  for res, rs in rows.items():
    rs = sorted(rs)
    if res in parent_res:
      groups: dict[str, list] = {}
      for r in rs:
        groups.setdefault(h3_parent(r[1], parent_res[res]), []).append(r)
      for p, g in sorted(groups.items()):
        rel = f"h3/res={res}/p={p}/membership.parquet"
        write(base / rel, pa.Table.from_pylist([dict(zip(schema.names, r)) for r in g], schema))
        files.append({"res": res, "parent": p, "path": rel, "rows": len(g)})
        parents += sorted({(r[0], res, p) for r in g})
    else:
      rel = f"h3/res={res}/membership.parquet"
      write(base / rel, pa.Table.from_pylist([dict(zip(schema.names, r)) for r in rs], schema))
      files.append({"res": res, "parent": None, "path": rel, "rows": len(rs)})
  pq.write_table(pa.table({"place_id": [p[0] for p in parents], "res": pa.array([p[1] for p in parents], pa.int8()),
                           "parent": [p[2] for p in parents]}), base / "h3" / "parents.parquet")
  manifest = {"version": "test", "min_share": 0.01, "columns": ["place_id", "h3", "share"], "res": sorted(rows),
              "parent_res": {str(k): v for k, v in parent_res.items()}, "files": files,
              "parents": "h3/parents.parquet"}
  (base / "h3" / "membership.json").write_text(json.dumps(manifest))
  return str(root)
