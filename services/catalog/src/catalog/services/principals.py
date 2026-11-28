"""The record of a principal the estate admin offboarded, a row of ``rask.principals`` ([[LH-233]], ADR 0230).

A principal exists in the estate only as the tuples naming it, so once its grants are revoked nothing would say it was
ever removed, by whom or when. The offboarding door writes one row per principal, keyed by the principal itself. The
first deletion's row stands: a re-run of the door, which revokes whatever was granted since, and a second request racing
the first, which waits on the first's uncommitted key and then inserts nothing, both answer it.
"""

from sqlalchemy.dialects.postgresql import insert
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from catalog.core.rows import PrincipalRow
from catalog.schemas import PrincipalRecord
from lakehouse.governed.principal import Principal


async def read_record(session: AsyncSession, principal: str) -> PrincipalRecord | None:
    """The deletion record of ``principal``, or ``None`` when it was never offboarded."""
    row = (await session.exec(select(PrincipalRow).where(col(PrincipalRow.principal) == principal))).first()
    return None if row is None else PrincipalRecord.model_validate(row.model_dump())


async def record_deletion(session: AsyncSession, principal: str, *, deleted_by: str) -> PrincipalRecord:
    """Record ``principal`` deleted by ``deleted_by`` at the database's time, or answer the record an earlier deletion
    wrote."""
    kind = "service" if Principal.parse(principal).is_machine else "person"
    inserted = await session.exec(
        insert(PrincipalRow)
        .values(principal=principal, kind=kind, deleted_by=deleted_by)
        .on_conflict_do_nothing(index_elements=["principal"])
        .returning(col(PrincipalRow.principal), col(PrincipalRow.kind), col(PrincipalRow.deleted_at), col(PrincipalRow.deleted_by))
    )
    if (row := inserted.mappings().first()) is not None:
        return PrincipalRecord.model_validate(dict(row))
    standing = await read_record(session, principal)
    if standing is None:
        raise RuntimeError(f"the insert of {principal!r}'s record conflicted with a row the same transaction cannot read")
    return standing
