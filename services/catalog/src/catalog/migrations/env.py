"""Alembic's environment for the catalog's revisions: run on the connection :func:`catalog.core.schema.migrate` opened,
inside the transaction it holds the advisory lock in, so every pending revision commits together or not at all.

Alembic sets ``context.config`` only while it runs a migration, so a module imported outside one (a walk over the
package's modules) runs nothing.
"""

from alembic import context
from alembic.config import Config
from sqlalchemy import Connection

from catalog.core.rows import SCHEMA


def _run(config: Config) -> None:
    connection = config.attributes.get("connection")
    if not isinstance(connection, Connection):
        raise TypeError("the catalog's revisions run only on the connection catalog.core.schema.migrate opens")
    context.configure(connection=connection, version_table_schema=SCHEMA, transaction_per_migration=False)
    with context.begin_transaction():
        context.run_migrations()


if isinstance(config := getattr(context, "config", None), Config):
    _run(config)
