import json

import pytest

from om_extract import schemas

TINY = {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}, "additionalProperties": False}


def _make(tmp_path, name="tiny"):
  d = tmp_path / "schemas"
  d.mkdir()
  (d / f"{name}.schema.json").write_text(json.dumps(TINY))
  return d


def test_schema_dir_env_var_wins(tmp_path, monkeypatch):
  d = _make(tmp_path)
  monkeypatch.setenv("OM_SCHEMAS_DIR", str(d))
  assert schemas.schema_dir() == d


def test_schema_dir_env_var_not_a_directory_raises_naming_places(tmp_path, monkeypatch):
  monkeypatch.setenv("OM_SCHEMAS_DIR", str(tmp_path / "nope"))
  with pytest.raises(FileNotFoundError, match=r"OM_SCHEMAS_DIR.*node_modules.*\.\./platform"):
    schemas.schema_dir()


def test_schema_dir_prefers_node_modules_over_sibling(tmp_path, monkeypatch):
  monkeypatch.delenv("OM_SCHEMAS_DIR", raising=False)
  root = tmp_path / "extract"
  nm = root / "node_modules" / "@oceanmetrics" / "catalog" / "schemas"
  sib = tmp_path / "platform" / "packages" / "catalog" / "schemas"
  nm.mkdir(parents=True)
  sib.mkdir(parents=True)
  assert schemas.schema_dir(root) == nm


def test_schema_dir_falls_back_to_sibling_checkout(tmp_path, monkeypatch):
  monkeypatch.delenv("OM_SCHEMAS_DIR", raising=False)
  root = tmp_path / "extract"
  root.mkdir()
  sib = tmp_path / "platform" / "packages" / "catalog" / "schemas"
  sib.mkdir(parents=True)
  assert schemas.schema_dir(root) == sib


def test_schema_dir_none_exists_raises_naming_the_places(tmp_path, monkeypatch):
  monkeypatch.delenv("OM_SCHEMAS_DIR", raising=False)
  with pytest.raises(FileNotFoundError) as e:
    schemas.schema_dir(tmp_path / "extract", cwd=tmp_path / "app")
  msg = str(e.value)
  assert "OM_SCHEMAS_DIR" in msg and "<repo>/node_modules/@oceanmetrics/catalog/schemas" in msg
  assert "<repo>/../platform/packages/catalog/schemas" in msg and "<cwd>/../platform/packages/catalog/schemas" in msg


def test_load_schema_reads_named_file(tmp_path, monkeypatch):
  monkeypatch.setenv("OM_SCHEMAS_DIR", str(_make(tmp_path)))
  assert schemas.load_schema("tiny") == TINY


def test_load_schema_missing_name_raises(tmp_path, monkeypatch):
  monkeypatch.setenv("OM_SCHEMAS_DIR", str(_make(tmp_path)))
  with pytest.raises(FileNotFoundError, match="other"):
    schemas.load_schema("other")


def test_validate_valid_doc_returns_empty_list(tmp_path, monkeypatch):
  monkeypatch.setenv("OM_SCHEMAS_DIR", str(_make(tmp_path)))
  assert schemas.validate("tiny", {"a": 1}) == []


def test_validate_returns_path_and_message_per_error(tmp_path, monkeypatch):
  monkeypatch.setenv("OM_SCHEMAS_DIR", str(_make(tmp_path)))
  errs = schemas.validate("tiny", {"a": "x", "b": 2})
  assert len(errs) == 2
  assert any(e.startswith("/a: ") and "'x'" in e for e in errs)
  assert any(e.startswith("/: ") and "'b'" in e for e in errs)


def test_list_profiles_returns_sorted_schema_names_only(tmp_path, monkeypatch):
  d = _make(tmp_path, "zeta")
  (d / "alpha.schema.json").write_text(json.dumps(TINY))
  (d / "notes.txt").write_text("not a schema")
  (d / "other.json").write_text("{}")
  monkeypatch.setenv("OM_SCHEMAS_DIR", str(d))
  assert schemas.list_profiles() == ["alpha", "zeta"]


def test_cwd_sibling_platform_is_found_when_the_package_root_has_none(tmp_path, monkeypatch):
  # om-extract installed in site-packages (no sibling platform there); the app's own sibling platform is used
  monkeypatch.delenv("OM_SCHEMAS_DIR", raising=False)
  install = tmp_path / "site-packages" / "om_extract"
  install.mkdir(parents=True)
  app = tmp_path / "apps" / "indicators"
  app.mkdir(parents=True)
  sibling = tmp_path / "apps" / "platform" / "packages" / "catalog" / "schemas"
  sibling.mkdir(parents=True)
  from om_extract import schemas
  assert schemas.schema_dir(root=install, cwd=app) == sibling


def test_cwd_node_modules_wins_over_cwd_sibling(tmp_path, monkeypatch):
  monkeypatch.delenv("OM_SCHEMAS_DIR", raising=False)
  install = tmp_path / "site-packages" / "om_extract"
  install.mkdir(parents=True)
  app = tmp_path / "apps" / "indicators"
  nm = app / "node_modules" / "@oceanmetrics" / "catalog" / "schemas"
  nm.mkdir(parents=True)
  sibling = tmp_path / "apps" / "platform" / "packages" / "catalog" / "schemas"
  sibling.mkdir(parents=True)
  from om_extract import schemas
  assert schemas.schema_dir(root=install, cwd=app) == nm
