"""Command line entry point (`om-extract`). The subcommands arrive on Day 4 (2026-10-25)."""
from om_extract import __version__


def main(argv: list[str] | None = None) -> int:
  """Print the version and say when the subcommands arrive; return the exit code (0)."""
  print(f"om-extract {__version__}")
  print("subcommands arrive on Day 4 (2026-10-25)")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
