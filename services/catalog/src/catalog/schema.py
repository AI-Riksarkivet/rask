"""``python -m catalog.schema migrate|wait``: the catalog's database schema, from the release.

``migrate`` is the release's migration Job, as the migrator role (:func:`catalog.core.schema.migrate`); ``wait`` is the
catalog pod's init step, as the catalog's role (:func:`catalog.core.schema.wait`). Each exits 0 with the revision the
database holds, and 1 with the reason it refused, on one line an operator reads in the pod's log.
"""

import asyncio
import sys

from lance_namespace import ServiceUnavailableError
from sqlalchemy import exc as sa_exc

from catalog.core.schema import MigrateSettings, SchemaMismatchError, SchemaSettings, migrate, wait


_USAGE = "usage: python -m catalog.schema migrate|wait\n"


def main(argv: list[str]) -> int:
    """Run the command ``argv`` names, and return the process's exit status."""
    match argv:
        case ["migrate"]:
            run = migrate(MigrateSettings())
        case ["wait"]:
            run = wait(SchemaSettings())
        case _:
            sys.stderr.write(_USAGE)
            return 2
    try:
        revision = asyncio.run(run)
    except (SchemaMismatchError, ServiceUnavailableError) as refused:
        sys.stderr.write(f"{refused}\n")
        return 1
    except sa_exc.DBAPIError as refused:
        # The database's own words, without the statement and the parameters SQLAlchemy appends.
        sys.stderr.write(f"the catalog's database refused: {' '.join(str(refused.orig).split())}\n")
        return 1
    sys.stdout.write(f"the catalog's database schema is at revision {revision}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
