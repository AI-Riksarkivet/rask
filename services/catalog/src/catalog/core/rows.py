"""The catalog's rows in its own database, schema ``rask`` (ADR 0230): one table model a table, shaped as its migration
in ``catalog.migrations`` creates it."""

from datetime import datetime
from typing import Final, Literal

from sqlalchemy import Column, DateTime, Text, func
from sqlmodel import Field, SQLModel


__all__ = ["SCHEMA", "PrincipalRow"]

#: The schema every table of the catalog's lives in, and Alembic's version table with them.
SCHEMA: Final = "rask"


class PrincipalRow(SQLModel, table=True):
    """A principal the estate admin offboarded: written once by the offboarding door, never updated or deleted, so it
    carries no ``updated_at``. ``deleted_at`` is the database's clock (``DEFAULT now()``), read back with the insert."""

    __tablename__ = "principals"
    __table_args__ = ({"schema": SCHEMA},)

    principal: str = Field(sa_column=Column(Text, primary_key=True))
    #: Held to these two by the table's CHECK as well.
    kind: Literal["person", "service"] = Field(sa_column=Column(Text, nullable=False))
    deleted_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))
    deleted_by: str = Field(sa_column=Column(Text, nullable=False))
