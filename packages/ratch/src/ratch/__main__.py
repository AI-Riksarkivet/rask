"""``python -m ratch`` entry point (same CLI as the ``ratch`` console script)."""

import sys

from ratch.cli import app
from ratch.errors import RatchError


def main() -> None:
    try:
        app()
    except RatchError as e:
        # Library code raises domain errors; the entry point owns the exit.
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
