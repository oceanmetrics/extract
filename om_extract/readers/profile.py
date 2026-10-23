"""Profile checks shared by the readers: a STAC document validated against its platform schema before it is read."""
from om_extract import schemas
from om_extract.readers.erddap import InvalidCollection


class InvalidProfile(InvalidCollection):
  """The document does not validate against its profile schema (`cog`, `h3`); `errors` holds the messages. A subclass
  of `InvalidCollection`, so one `except` catches every reader's refusal."""

  def __init__(self, doc_id, profile: str, errors: list[str]):
    ValueError.__init__(self, f"{doc_id!r} is not a valid {profile}:\n" + "\n".join(f"  {e}" for e in errors))
    self.errors = errors
    self.profile = profile


def check_profile(profile: str, doc: dict) -> None:
  """Raise `InvalidProfile` when `doc` fails the platform schema `profile` (read through `om_extract.schemas`)."""
  errors = schemas.validate(profile, doc)
  if errors:
    raise InvalidProfile(doc.get("id"), profile, errors)
