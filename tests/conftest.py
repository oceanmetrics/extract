"""Shared test settings: tests marked `live` reach real servers and run only when OM_LIVE=1."""
import os

import pytest


def pytest_configure(config):
  config.addinivalue_line("markers", "live: reaches a real server (ERDDAP, the bucket); runs only when OM_LIVE=1")


def pytest_collection_modifyitems(config, items):
  if os.environ.get("OM_LIVE") == "1":
    return
  skip = pytest.mark.skip(reason="live: set OM_LIVE=1 to run")
  for item in items:
    if "live" in item.keywords:
      item.add_marker(skip)
