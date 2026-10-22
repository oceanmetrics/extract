"""Ocean Metrics extraction engine: one rule for summarising a layer by places into indicator values."""
from importlib.metadata import PackageNotFoundError, version

try:
  __version__ = version("om-extract")
except PackageNotFoundError:
  __version__ = "0.0.1"

__all__ = ["__version__"]
