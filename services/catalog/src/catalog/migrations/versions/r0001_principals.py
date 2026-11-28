"""The principals the estate admin offboarded: one row each, written once, never updated or deleted.

Revision ID: 0001
"""

import sqlalchemy as sa
from alembic import context, op

from catalog.core.rows import SCHEMA
from catalog.core.schema import MIGRATION_ROLES, MigrationRoles


revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    roles = context.config.attributes[MIGRATION_ROLES]
    if not isinstance(roles, MigrationRoles):
        raise TypeError(f"the roles a revision grants to are a MigrationRoles, not {type(roles).__name__}")
    op.create_table(
        "principals",
        sa.Column("principal", sa.Text, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_by", sa.Text, nullable=False),
        sa.CheckConstraint("kind IN ('person', 'service')", name="principals_kind"),
        schema=SCHEMA,
    )
    quote = op.get_bind().dialect.identifier_preparer.quote_identifier
    catalog, maintenance = quote(roles.catalog), quote(roles.maintenance)
    # Least privilege, reviewed with the table: the catalog reads the revision at its start, and writes a record once
    # and reads it, so it holds no UPDATE or DELETE; maintenance reads only the revision until a table it reads exists.
    op.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {catalog}, {maintenance}")
    op.execute(f"GRANT SELECT ON {SCHEMA}.alembic_version TO {catalog}, {maintenance}")
    op.execute(f"GRANT SELECT, INSERT ON {SCHEMA}.principals TO {catalog}")


def downgrade() -> None:
    raise NotImplementedError("the catalog's database schema has no down migrations: a release moves it forward only")
