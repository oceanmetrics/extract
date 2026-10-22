"""Workflow notebooks: read the `om:` front-matter blocks and generate the run order, the DAG and the index page.

The block is defined by the platform schema `om-notebook`. One block per `*.qmd` names the target, its dependencies
and the manifest it writes; everything else here is derived from the blocks.
"""
import heapq
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from om_extract.schemas import validate

SCHEMA = "om-notebook"
NO_BLOCK = "no om: block"
TYPE_ORDER = ["ingest", "extract", "publish", "release", "reference", "explore"]
TYPE_TITLES = {
  "ingest": "Ingest", "extract": "Extract", "publish": "Publish", "release": "Release",
  "reference": "Reference", "explore": "Explore",
}
TYPE_COLORS = {
  "ingest": "#cdd9e5", "extract": "#9ec5fe", "publish": "#20c997", "release": "#f06595",
  "reference": "#ffd43b", "explore": "#b197fc",
}


# read ----

def parse_front_matter(path) -> dict:
  """The YAML between the first pair of `---` lines of a file; `{}` when the file has none."""
  lines = Path(path).read_text().splitlines()
  if not lines or lines[0].strip() != "---":
    return {}
  for i, line in enumerate(lines[1:], start=1):
    if line.strip() == "---":
      doc = yaml.safe_load("\n".join(lines[1:i]))
      return doc if isinstance(doc, dict) else {}
  return {}


def read_blocks(directory) -> list[dict]:
  """One record per `*.qmd` in `directory` (not `index.qmd`, not `_*`), sorted by file name.

  Keys: `target_name`, `path` (relative to `directory`), `block` (the `om:` dict), `title` (the notebook's own),
  `errors` (schema errors, each naming the file) and `note`. A notebook without an `om:` key has `block` None,
  `target_name` None and `note` "no om: block"; it is not an error.
  """
  directory = Path(directory)
  out = []
  for p in sorted(directory.glob("*.qmd")):
    if p.name == "index.qmd" or p.name.startswith("_"):
      continue
    rec = {"target_name": None, "path": p.name, "block": None, "title": None, "errors": [], "note": None}
    try:
      fm = parse_front_matter(p)
    except yaml.YAMLError as e:
      rec["errors"].append(f"{p.name}: front matter is not valid YAML: {str(e).splitlines()[0]}")
      out.append(rec)
      continue
    rec["title"] = fm.get("title")
    block = fm.get("om")
    if block is None:
      rec["note"] = NO_BLOCK
    else:
      rec["block"] = block
      errs = validate(SCHEMA, block)
      rec["errors"] = [f"{p.name}: om:{e}" for e in errs]
      if isinstance(block, dict):
        rec["target_name"] = block.get("target_name")
    out.append(rec)
  return out


def _valid(blocks: list[dict]) -> list[dict]:
  return [b for b in blocks if b.get("block") is not None and not b.get("errors")]


def _deps(b: dict) -> list[str]:
  return list(b["block"].get("dependency") or [])


def _status(b: dict) -> str:
  return b["block"].get("status", "active")


# graph ----

def check_graph(blocks: list[dict]) -> list[str]:
  """Errors in the notebooks: schema errors, duplicate target_name, unknown dependency, an active notebook that
  depends on an archived one, and cycles. Empty when the graph is sound."""
  errors = [e for b in blocks for e in b.get("errors", [])]
  ok = _valid(blocks)
  by_name: dict[str, list[dict]] = {}
  for b in ok:
    by_name.setdefault(b["target_name"], []).append(b)
  for name, bs in sorted(by_name.items()):
    if len(bs) > 1:
      errors.append(f"duplicate target_name {name!r} in " + ", ".join(sorted(x["path"] for x in bs)))
  first = {name: bs[0] for name, bs in by_name.items()}
  for b in ok:
    for d in _deps(b):
      if d not in first:
        errors.append(f"{b['path']}: dependency {d!r} of {b['target_name']!r} is not a known target_name")
      elif _status(b) != "archived" and _status(first[d]) == "archived":
        errors.append(f"{b['path']}: {b['target_name']!r} depends on archived target {d!r}")
  errors += _cycles({n: [d for d in _deps(b) if d in first] for n, b in first.items()})
  return errors


def _cycles(graph: dict[str, list[str]]) -> list[str]:
  """One error per cycle found by depth-first search (each cycle reported once, from its smallest name)."""
  errors, seen_cycles = [], set()
  state: dict[str, int] = {}  # 1 on the stack, 2 done
  stack: list[str] = []

  def visit(n: str):
    state[n] = 1
    stack.append(n)
    for d in sorted(graph[n]):
      if state.get(d) == 1:
        cyc = stack[stack.index(d):]
        k = cyc.index(min(cyc))
        cyc = cyc[k:] + cyc[:k]
        if tuple(cyc) not in seen_cycles:
          seen_cycles.add(tuple(cyc))
          errors.append("dependency cycle: " + " -> ".join(cyc + [cyc[0]]))
      elif d not in state:
        visit(d)
    stack.pop()
    state[n] = 2

  for n in sorted(graph):
    if n not in state:
      visit(n)
  return errors


def run_order(blocks: list[dict]) -> list[str]:
  """Deterministic topological order of the target names (dependencies first, ties by target_name).
  Archived notebooks are excluded. Raises ValueError when `check_graph` finds errors."""
  errors = check_graph(blocks)
  if errors:
    raise ValueError("; ".join(errors))
  live = {b["target_name"]: b for b in _valid(blocks) if _status(b) != "archived"}
  indeg = {n: len(_deps(b)) for n, b in live.items()}
  children: dict[str, list[str]] = {n: [] for n in live}
  for n, b in live.items():
    for d in _deps(b):
      children[d].append(n)
  ready = [n for n, k in indeg.items() if k == 0]
  heapq.heapify(ready)
  order = []
  while ready:
    n = heapq.heappop(ready)
    order.append(n)
    for c in children[n]:
      indeg[c] -= 1
      if indeg[c] == 0:
        heapq.heappush(ready, c)
  return order


def _sid(name: str) -> str:
  return f"t_{name}"


def dag_mermaid(blocks: list[dict]) -> str:
  """The DAG as a mermaid `graph LR`: one node per target (labelled with the target_name), one edge per dependency
  (dependency --> dependent), a class per workflow_type and a dashed grey `archived` class (instead of the type
  class) for archived notebooks."""
  ok = sorted(_valid(blocks), key=lambda b: b["target_name"])
  names = {b["target_name"] for b in ok}
  lines = ["graph LR"]
  for b in ok:
    cls = "archived" if _status(b) == "archived" else b["block"]["workflow_type"]
    lines.append(f'  {_sid(b["target_name"])}(["{b["target_name"]}"]):::{cls}')
  for b in ok:
    for d in sorted(_deps(b)):
      if d in names:
        lines.append(f"  {_sid(d)} --> {_sid(b['target_name'])}")
  used = sorted({b["block"]["workflow_type"] for b in ok if _status(b) != "archived"}, key=TYPE_ORDER.index)
  for t in used:
    lines.append(f"  classDef {t} fill:{TYPE_COLORS[t]},stroke:#00000033,color:#10161c;")
  if any(_status(b) == "archived" for b in ok):
    lines.append("  classDef archived fill:#eeeeee,stroke:#999999,stroke-dasharray:4 3,color:#777777;")
  return "\n".join(lines) + "\n"


# index ----

def _card(b: dict) -> str:
  blk = b["block"]
  name = Path(b["path"]).stem
  title = b.get("title") or blk["target_name"]
  status = _status(b)
  badge = f" *({status})*" if status == "draft" else ""
  out = ["::: {.om-card}", f"### {title}{badge}", ""]
  if blk.get("description"):
    out += [blk["description"], ""]
  out.append(f"- Notebook: [{name}.html]({name}.html)")
  out.append(f"- Target: `{blk['target_name']}`" + (f", schedule `{blk['schedule']}`" if blk.get("schedule") else ""))
  if _deps(b):
    out.append("- Depends on: " + ", ".join(f"`{d}`" for d in sorted(_deps(b))))
  out.append("- Output: " + (f"[{blk['output']}]({blk['output']})" if blk["output"].startswith(("http://", "https://")) else f"`{blk['output']}`"))
  for p in blk.get("products", []):
    text = p.get("title") or p["href"]
    href = p["href"]
    out.append(f"- Product ({p['kind']}): " + (f"[{text}]({href})" if href.startswith(("http://", "https://")) else f"{text} (`{href}`)"))
  out += [":::", ""]
  return "\n".join(out)


_CSS = """```{=html}
<style>
.om-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(22rem, 1fr)); gap: 1rem; }
.om-card { border: 1px solid #8884; border-radius: 6px; padding: 0 1rem 0.5rem; }
.om-card h3 { font-size: 1.1rem; margin-top: 1rem; }
</style>
```
"""


def index_qmd(blocks: list[dict], title: str = "Workflows") -> str:
  """The index page (Quarto): the DAG as a mermaid block, then one card per notebook grouped by workflow_type
  (active and draft first, archived last under "Archived")."""
  ok = sorted(_valid(blocks), key=lambda b: b["target_name"])
  esc = title.replace('"', '\\"')
  out = [f'---\ntitle: "{esc}"\nformat:\n  html:\n    toc: true\n    code-tools: false\n---\n', _CSS,
         "## Pipeline\n", "```{mermaid}", dag_mermaid(blocks).rstrip("\n"), "```\n"]

  def section(heading: str, bs: list[dict]):
    if bs:
      out.extend([f"## {heading}\n", '::: {.om-cards}\n', *[_card(b) for b in bs], ":::\n"])

  for t in TYPE_ORDER:
    section(TYPE_TITLES[t], [b for b in ok if _status(b) != "archived" and b["block"]["workflow_type"] == t])
  section("Archived", sorted([b for b in ok if _status(b) == "archived"],
                             key=lambda b: (TYPE_ORDER.index(b["block"]["workflow_type"]), b["target_name"])))
  return "\n".join(out)


# write ----

def check_dir(directory) -> list[str]:
  """Errors of every notebook in `directory` and of the graph (empty when sound)."""
  return check_graph(read_blocks(directory))


def write_index(directory, title: str = "Workflows", now: datetime | None = None) -> list[Path]:
  """Write `index.qmd`, `_om/order.json` and `_om/dag.mmd` into `directory`; return the paths written.
  Raises ValueError (listing every error) when the notebooks do not pass `check_graph`."""
  directory = Path(directory)
  blocks = read_blocks(directory)
  errors = check_graph(blocks)
  if errors:
    raise ValueError("\n".join(errors))
  order = run_order(blocks)
  ok = {b["target_name"]: b for b in _valid(blocks)}
  archived = sorted(n for n, b in ok.items() if _status(b) == "archived")
  doc = {
    "generated": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
    "order": order,
    "notebooks": [
      {"target_name": n, "path": ok[n]["path"], "workflow_type": ok[n]["block"]["workflow_type"],
       "status": _status(ok[n]), "dependency": _deps(ok[n]), "output": ok[n]["block"]["output"]}
      for n in order + archived],
  }
  (directory / "_om").mkdir(exist_ok=True)
  paths = [directory / "index.qmd", directory / "_om" / "order.json", directory / "_om" / "dag.mmd"]
  paths[0].write_text(index_qmd(blocks, title))
  paths[1].write_text(json.dumps(doc, indent=2) + "\n")
  paths[2].write_text(dag_mermaid(blocks))
  return paths
