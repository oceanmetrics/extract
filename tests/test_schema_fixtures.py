"""Cross-implementation check: the Python validator agrees with the platform fixtures (valid/ must pass, invalid/ must
fail). The fixtures sit beside the schemas dir: `<schema_dir>/../fixtures/<name>/{valid,invalid}/*.json`. Only pass/fail
is asserted here; the expected-message substrings in `invalid.json` belong to ajv's tests."""
import json
from pathlib import Path

import pytest

from om_extract.schemas import list_profiles, schema_dir, validate


def _cases() -> list:
  try:
    root = schema_dir().parent / "fixtures"
    names = list_profiles()
  except FileNotFoundError as e:
    return [pytest.param(None, None, None, id="no-schemas", marks=pytest.mark.skip(reason=str(e)))]
  if not root.is_dir():
    return [pytest.param(None, None, None, id="no-fixtures",
                         marks=pytest.mark.skip(reason=f"no fixtures dir at {root}"))]
  cases = []
  for name in names:
    for kind in ("valid", "invalid"):
      for f in sorted((root / name / kind).glob("*.json")):
        if f.name == "invalid.json":  # the message map, not a fixture
          continue
        cases.append(pytest.param(name, kind, f, id=f"{name}/{kind}/{f.name}"))
  if not cases:
    cases.append(pytest.param(None, None, None, id="no-cases",
                              marks=pytest.mark.skip(reason=f"no fixture files under {root}")))
  return cases


@pytest.mark.parametrize("name,kind,path", _cases())
def test_python_validator_agrees_with_platform_fixture(name, kind, path: Path):
  errors = validate(name, json.loads(path.read_text()))
  if kind == "valid":
    assert errors == [], f"{name}/valid/{path.name} should validate: {errors}"
  else:
    assert errors, f"{name}/invalid/{path.name} should fail validation"
