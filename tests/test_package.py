import tomllib
from pathlib import Path

import om_extract
from om_extract.cli import main


def test_version_matches_pyproject():
  pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
  expected = tomllib.loads(pyproject.read_text())["project"]["version"]
  assert om_extract.__version__ == expected


def test_main_without_args_exits_0_and_prints_version(capsys):
  assert main([]) == 0
  assert om_extract.__version__ in capsys.readouterr().out
