"""Platform JSON Schemas: where they live, how to load one and how to validate a document against it."""
import json
import os
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_ENV = "OM_SCHEMAS_DIR"


def _repo_root() -> Path:
  return Path(__file__).resolve().parent.parent


def _candidates(root: Path | None = None) -> list[tuple[str, Path]]:
  root = root or _repo_root()
  return [
    (f"${SCHEMA_ENV}", Path(os.environ[SCHEMA_ENV]) if os.environ.get(SCHEMA_ENV) else None),
    ("<repo>/node_modules/@oceanmetrics/catalog/schemas", root / "node_modules" / "@oceanmetrics" / "catalog" / "schemas"),
    ("<repo>/../platform/packages/catalog/schemas", root.parent / "platform" / "packages" / "catalog" / "schemas"),
  ]


def schema_dir(root: Path | None = None) -> Path:
  """Directory holding the platform schemas.

  In order: `$OM_SCHEMAS_DIR` (authoritative when set), `<repo>/node_modules/@oceanmetrics/catalog/schemas`
  (after the npm publish), the sibling checkout `<repo>/../platform/packages/catalog/schemas`. Raises
  FileNotFoundError naming the three places when none exists.
  """
  cands = _candidates(root)
  env = cands[0][1]
  if env is not None:
    if env.is_dir():
      return env
    raise FileNotFoundError(f"{cands[0][0]}={env} is not a directory; schemas are looked up in: " + _places(cands))
  for _, p in cands[1:]:
    if p.is_dir():
      return p
  raise FileNotFoundError("no platform schemas found; looked in: " + _places(cands))


def _places(cands: list[tuple[str, Path | None]]) -> str:
  return "; ".join(f"{label} ({p if p is not None else 'unset'})" for label, p in cands)


def load_schema(name: str) -> dict:
  """Load `<name>.schema.json` from `schema_dir()`."""
  path = schema_dir() / f"{name}.schema.json"
  if not path.is_file():
    raise FileNotFoundError(f"schema {name!r} not found: {path}")
  return json.loads(path.read_text())


def validate(name: str, doc) -> list[str]:
  """Validate `doc` against the named schema; return error strings `"<json pointer>: <message>"` (empty when valid)."""
  validator = Draft202012Validator(load_schema(name))
  errors = sorted(validator.iter_errors(doc), key=lambda e: [str(p) for p in e.absolute_path])
  return [f"/{'/'.join(str(p) for p in e.absolute_path)}: {e.message}" for e in errors]


def list_profiles() -> list[str]:
  """Names of the schemas found in `schema_dir()` (`<name>.schema.json`), sorted."""
  return sorted(p.name.removesuffix(".schema.json") for p in schema_dir().glob("*.schema.json"))
