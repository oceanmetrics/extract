import subprocess
import sys

import om_extract
from om_extract.cli import main

NB = "---\ntitle: N\nom:\n  target_name: {n}\n  workflow_type: extract\n  output: {n}.json\n{extra}---\n"


def _dir(tmp_path, extra_b=""):
  (tmp_path / "a.qmd").write_text(NB.format(n="a", extra=""))
  (tmp_path / "b.qmd").write_text(NB.format(n="b", extra=extra_b))
  (tmp_path / "plain.qmd").write_text("---\ntitle: P\n---\n")
  return tmp_path


def test_no_args_prints_version(capsys):
  assert main([]) == 0
  assert om_extract.__version__ in capsys.readouterr().out


def test_version_flag_prints_version(capsys):
  assert main(["--version"]) == 0
  assert capsys.readouterr().out.strip() == f"om-extract {om_extract.__version__}"


def test_notebooks_check_ok_exit_0_and_notes_missing_block(tmp_path, capsys):
  assert main(["notebooks", "check", str(_dir(tmp_path, "  dependency: [a]\n"))]) == 0
  out = capsys.readouterr().out
  assert "plain.qmd: no om: block" in out and "ok" in out


def test_notebooks_check_exit_1_prints_errors(tmp_path, capsys):
  assert main(["notebooks", "check", str(_dir(tmp_path, "  dependency: [missing]\n"))]) == 1
  assert "dependency 'missing' of 'b'" in capsys.readouterr().err


def test_notebooks_index_writes_files_with_title(tmp_path, capsys):
  d = _dir(tmp_path, "  dependency: [a]\n")
  assert main(["notebooks", "index", str(d), "--title", "My title"]) == 0
  assert 'title: "My title"' in (d / "index.qmd").read_text()
  assert (d / "_om" / "order.json").exists() and (d / "_om" / "dag.mmd").exists()


def test_notebooks_index_exit_1_on_errors_and_writes_nothing(tmp_path, capsys):
  d = _dir(tmp_path, "  dependency: [missing]\n")
  assert main(["notebooks", "index", str(d)]) == 1
  assert not (d / "index.qmd").exists()


def test_cli_via_subprocess_exit_code(tmp_path):
  d = _dir(tmp_path, "  dependency: [missing]\n")
  r = subprocess.run([sys.executable, "-m", "om_extract.cli", "notebooks", "check", str(d)], capture_output=True, text=True)
  assert r.returncode == 1 and "missing" in r.stderr


def _layer(tmp_path, doc=None):
  from fixtures_erddap import grid, info_table
  import json
  c, i = tmp_path / "collection.json", tmp_path / "info.json"
  c.write_text(json.dumps(doc or grid()))
  i.write_text(json.dumps(info_table("1985-04-01T12:00:00Z", "2026-09-13T12:00:00Z")))
  return c, i


def test_layer_info_prints_the_dataset_record_and_the_time_extent(tmp_path, capsys):
  c, i = _layer(tmp_path)
  assert main(["layer", "info", str(c), "--info", str(i)]) == 0
  out = capsys.readouterr().out
  assert "dhw_5km" in out and "lat_descending true" in out and "1985-04-01T12:00:00Z to 2026-09-13T12:00:00Z" in out


def test_layer_info_json_prints_the_record(tmp_path, capsys):
  import json
  c, i = _layer(tmp_path)
  assert main(["layer", "info", str(c), "--info", str(i), "--json"]) == 0
  rec = json.loads(capsys.readouterr().out)
  assert rec["dataset"]["protocol"] == "griddap" and rec["extent"]["time_end"] == "2026-09-13T12:00:00Z"


def test_layer_info_exits_1_with_the_schema_errors_for_an_invalid_collection(tmp_path, capsys):
  from fixtures_erddap import grid
  c, i = _layer(tmp_path, grid(erddap__base_url=None))
  assert main(["layer", "info", str(c), "--info", str(i)]) == 1
  assert "erddap:base_url" in capsys.readouterr().err


def test_layer_info_exits_1_for_a_collection_no_reader_knows(tmp_path, capsys):
  c, i = _layer(tmp_path, {"id": "atlas/x", "cog:crs": "EPSG:3857"})
  assert main(["layer", "info", str(c), "--info", str(i)]) == 1
  assert "no reader for collection 'atlas/x'" in capsys.readouterr().err
