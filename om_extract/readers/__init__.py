"""Readers: one per source, behind the interface in `base`, chosen by the profile a collection declares."""
import json
from collections.abc import Callable

from om_extract.readers.base import Axes, BBox, Extent, Reader, SLAB_SCHEMA, SLICE_SCHEMA
from om_extract.readers.erddap import ErddapGrid, InvalidCollection

__all__ = ["Axes", "BBox", "ErddapGrid", "Extent", "InvalidCollection", "Reader", "SLAB_SCHEMA", "SLICE_SCHEMA",
           "UnknownProfile", "for_collection", "kinds", "register"]


class UnknownProfile(ValueError):
  """The collection declares no source profile any registered reader recognises."""


# (kind, does this collection belong to the reader?, build the reader from the collection and keyword arguments)
_REGISTRY: list[tuple[str, Callable[[dict], bool], Callable[..., Reader]]] = []


def register(kind: str, matches: Callable[[dict], bool], build: Callable[..., Reader]) -> None:
  """Add a reader. The first registered reader whose `matches(collection)` is true wins."""
  _REGISTRY.append((kind, matches, build))


def kinds() -> list[str]:
  """The registered reader kinds, in dispatch order."""
  return [k for k, _, _ in _REGISTRY]


def for_collection(collection: dict | str | bytes, **kwargs) -> Reader:
  """The reader for a collection (a dict, or its JSON text), dispatched on the profile it declares.

  Today `erddap:protocol` present means ERDDAP. Keyword arguments (`fetch=`, `info=`) go to the reader. Raises
  `UnknownProfile` naming the profile prefixes the document has when no reader matches.
  """
  doc = json.loads(collection) if isinstance(collection, (str, bytes)) else collection
  for _, matches, build in _REGISTRY:
    if matches(doc):
      return build(doc, **kwargs)
  prefixes = sorted({k.split(":")[0] for k in doc if ":" in k})
  raise UnknownProfile(f"no reader for collection {doc.get('id')!r}: profile keys {prefixes or 'none'}; "
                       f"readers: {kinds()}")


register("erddap", lambda doc: "erddap:protocol" in doc, ErddapGrid)
