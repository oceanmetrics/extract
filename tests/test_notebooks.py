import json
from datetime import datetime, timezone

import pytest

from om_extract import notebooks as nb


def qmd(title, om=None, extra=""):
  fm = f"title: {title!r}\n{extra}"
  if om:
    fm += "om:\n" + "".join(f"  {line}\n" for line in om.strip().splitlines())
  return f"---\n{fm}---\n\nbody\n"


A = """
target_name: alpha
workflow_type: ingest
output: _output/alpha.html
description: Loads the alpha data.
"""
B = """
target_name: beta
workflow_type: publish
dependency: [alpha]
output: https://example.org/beta/provenance.json
products:
  - {href: "https://example.org/beta/collection.json", kind: stac-collection, title: Beta collection}
schedule: manual
"""
Z = """
target_name: zeta
workflow_type: explore
status: archived
output: _output/zeta.html
"""


@pytest.fixture
def wf(tmp_path):
  (tmp_path / "alpha.qmd").write_text(qmd("Alpha notebook", A))
  (tmp_path / "beta.qmd").write_text(qmd("Beta notebook", B))
  (tmp_path / "zeta.qmd").write_text(qmd("Zeta notebook", Z))
  return tmp_path


def by_name(blocks):
  return {b["target_name"]: b for b in blocks}


# parse_front_matter ----

def test_parse_front_matter_reads_first_yaml_pair(tmp_path):
  p = tmp_path / "x.qmd"
  p.write_text("---\ntitle: T\nom:\n  target_name: x\n---\n\n---\nnot: this\n---\n")
  assert nb.parse_front_matter(p) == {"title": "T", "om": {"target_name": "x"}}


def test_parse_front_matter_without_front_matter_is_empty(tmp_path):
  p = tmp_path / "x.qmd"
  p.write_text("# just a heading\n")
  assert nb.parse_front_matter(p) == {}


# read_blocks ----

def test_read_blocks_lists_every_notebook_with_block(wf):
  blocks = nb.read_blocks(wf)
  assert [b["path"] for b in blocks] == ["alpha.qmd", "beta.qmd", "zeta.qmd"]
  assert [b["target_name"] for b in blocks] == ["alpha", "beta", "zeta"]
  assert by_name(blocks)["alpha"]["title"] == "Alpha notebook"
  assert all(b["errors"] == [] for b in blocks)


def test_read_blocks_missing_block_is_listed_not_an_error(wf):
  (wf / "plain.qmd").write_text(qmd("Plain"))
  b = next(b for b in nb.read_blocks(wf) if b["path"] == "plain.qmd")
  assert b["block"] is None and b["target_name"] is None
  assert b["note"] == "no om: block" and b["errors"] == []
  assert nb.check_graph(nb.read_blocks(wf)) == []


def test_read_blocks_skips_index_and_underscore_files(wf):
  (wf / "index.qmd").write_text(qmd("Index"))
  (wf / "_draft.qmd").write_text(qmd("Draft"))
  assert "index.qmd" not in [b["path"] for b in nb.read_blocks(wf)]
  assert "_draft.qmd" not in [b["path"] for b in nb.read_blocks(wf)]


def test_read_blocks_invalid_block_error_names_file_and_schema_problem(wf):
  (wf / "bad.qmd").write_text(qmd("Bad", "target_name: Bad-Name\nworkflow_type: nonsense\n"))
  b = next(b for b in nb.read_blocks(wf) if b["path"] == "bad.qmd")
  msg = "\n".join(b["errors"])
  assert "bad.qmd" in msg
  assert "/target_name" in msg and "/workflow_type" in msg and "output" in msg  # pattern, enum, required
  assert nb.check_graph(nb.read_blocks(wf)) == b["errors"]


def test_read_blocks_invalid_yaml_is_an_error_naming_file(wf):
  (wf / "broken.qmd").write_text("---\ntitle: [unclosed\n---\n")
  b = next(b for b in nb.read_blocks(wf) if b["path"] == "broken.qmd")
  assert len(b["errors"]) == 1 and b["errors"][0].startswith("broken.qmd: ")


# check_graph ----

def test_check_graph_sound_graph_has_no_errors(wf):
  assert nb.check_graph(nb.read_blocks(wf)) == []


def test_check_graph_duplicate_target_name(wf):
  (wf / "alpha2.qmd").write_text(qmd("Alpha again", A))
  errs = nb.check_graph(nb.read_blocks(wf))
  assert errs == ["duplicate target_name 'alpha' in alpha.qmd, alpha2.qmd"]


def test_check_graph_unknown_dependency(wf):
  (wf / "gamma.qmd").write_text(qmd("Gamma", "target_name: gamma\nworkflow_type: extract\noutput: g.json\ndependency: [nowhere]\n"))
  errs = nb.check_graph(nb.read_blocks(wf))
  assert errs == ["gamma.qmd: dependency 'nowhere' of 'gamma' is not a known target_name"]


def test_check_graph_cycle(wf):
  (wf / "alpha.qmd").write_text(qmd("Alpha", A + "dependency: [beta]\n"))
  errs = nb.check_graph(nb.read_blocks(wf))
  assert errs == ["dependency cycle: alpha -> beta -> alpha"]


def test_check_graph_self_dependency_is_a_cycle(wf):
  (wf / "alpha.qmd").write_text(qmd("Alpha", A + "dependency: [alpha]\n"))
  assert nb.check_graph(nb.read_blocks(wf)) == ["dependency cycle: alpha -> alpha"]


def test_check_graph_active_depending_on_archived_is_an_error(wf):
  (wf / "beta.qmd").write_text(qmd("Beta", B.replace("[alpha]", "[zeta]")))
  errs = nb.check_graph(nb.read_blocks(wf))
  assert errs == ["beta.qmd: 'beta' depends on archived target 'zeta'"]


# run_order ----

def test_run_order_dependencies_first_archived_excluded(wf):
  assert nb.run_order(nb.read_blocks(wf)) == ["alpha", "beta"]


def test_run_order_ties_by_target_name_and_is_deterministic(tmp_path):
  def mk(n, dep=""):
    return qmd(n, f"target_name: {n}\nworkflow_type: extract\noutput: {n}.json\n{dep}")
  for n in ["delta", "charlie", "bravo"]:
    (tmp_path / f"{n}.qmd").write_text(mk(n))
  (tmp_path / "alpha.qmd").write_text(mk("alpha", "dependency: [delta]\n"))
  order = nb.run_order(nb.read_blocks(tmp_path))
  assert order == ["bravo", "charlie", "delta", "alpha"]
  assert nb.run_order(list(reversed(nb.read_blocks(tmp_path)))) == order


def test_run_order_raises_on_cycle(wf):
  (wf / "alpha.qmd").write_text(qmd("Alpha", A + "dependency: [beta]\n"))
  with pytest.raises(ValueError, match="cycle"):
    nb.run_order(nb.read_blocks(wf))


# dag_mermaid ----

def test_dag_mermaid_nodes_edges_and_classes(wf):
  dag = nb.dag_mermaid(nb.read_blocks(wf))
  lines = dag.splitlines()
  assert lines[0] == "graph LR"
  assert '  t_alpha(["alpha"]):::ingest' in lines
  assert '  t_beta(["beta"]):::publish' in lines
  assert '  t_zeta(["zeta"]):::archived' in lines
  assert lines.count("  t_alpha --> t_beta") == 1
  assert sum("-->" in l for l in lines) == 1
  assert any(l.startswith("  classDef ingest ") for l in lines)
  assert any(l.startswith("  classDef publish ") for l in lines)
  assert any(l.startswith("  classDef archived ") and "stroke-dasharray" in l for l in lines)
  assert not any(l.startswith("  classDef explore ") for l in lines)  # only archived notebook of that type


# index_qmd ----

def test_index_qmd_mermaid_block_sections_and_cards(wf):
  page = nb.index_qmd(nb.read_blocks(wf), "My workflows")
  assert page.startswith('---\ntitle: "My workflows"\n')
  assert "```{mermaid}\ngraph LR" in page
  # active sections in type order, archived last
  assert page.index("## Ingest") < page.index("## Publish") < page.index("## Archived")
  assert "## Explore" not in page  # its only notebook is archived
  # card: title from the notebook, description, rendered html link, output, product
  assert "### Alpha notebook" in page
  assert "Loads the alpha data." in page
  assert "[alpha.html](alpha.html)" in page
  assert "[https://example.org/beta/provenance.json](https://example.org/beta/provenance.json)" in page
  assert "Product (stac-collection): [Beta collection](https://example.org/beta/collection.json)" in page
  assert "Depends on: `alpha`" in page and "schedule `manual`" in page
  assert page.index("### Zeta notebook") > page.index("## Archived")


def test_index_qmd_card_title_falls_back_to_target_name(tmp_path):
  (tmp_path / "x.qmd").write_text("---\nom:\n  target_name: x\n  workflow_type: extract\n  output: x.json\n---\n")
  assert "### x\n" in nb.index_qmd(nb.read_blocks(tmp_path))


# write_index ----

def test_write_index_writes_three_files(wf):
  now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
  paths = nb.write_index(wf, "T", now=now)
  assert [p.name for p in paths] == ["index.qmd", "order.json", "dag.mmd"]
  doc = json.loads((wf / "_om" / "order.json").read_text())
  assert doc["generated"] == "2026-10-22T12:00:00+00:00"
  assert doc["order"] == ["alpha", "beta"]
  assert doc["notebooks"][0] == {"target_name": "alpha", "path": "alpha.qmd", "workflow_type": "ingest",
                                 "status": "active", "dependency": [], "output": "_output/alpha.html"}
  assert [n["target_name"] for n in doc["notebooks"]] == ["alpha", "beta", "zeta"]
  assert doc["notebooks"][2]["status"] == "archived"
  assert (wf / "_om" / "dag.mmd").read_text() == nb.dag_mermaid(nb.read_blocks(wf))
  assert (wf / "index.qmd").read_text().startswith('---\ntitle: "T"')


def test_write_index_refuses_when_graph_has_errors_and_writes_nothing(wf):
  (wf / "alpha.qmd").write_text(qmd("Alpha", A + "dependency: [beta]\n"))
  with pytest.raises(ValueError, match="cycle"):
    nb.write_index(wf)
  assert not (wf / "index.qmd").exists() and not (wf / "_om").exists()


def test_write_index_is_idempotent_for_a_fixed_time(wf):
  now = datetime(2026, 10, 22, tzinfo=timezone.utc)
  nb.write_index(wf, "T", now=now)
  first = {p.name: p.read_text() for p in [wf / "index.qmd", wf / "_om" / "order.json", wf / "_om" / "dag.mmd"]}
  nb.write_index(wf, "T", now=now)  # index.qmd now exists and must not count as a notebook
  assert first == {p.name: p.read_text() for p in [wf / "index.qmd", wf / "_om" / "order.json", wf / "_om" / "dag.mmd"]}
