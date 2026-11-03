"""How a role is ended, in OpenFGA and in its registry, by the one path both doors that end a role take ([[LH-557]]).

A role's tuples are of two kinds: those NAMING it, where ``role:<uuid>#assignee`` is the subject (a rung on an object,
another role's assignee, a project's ``admin``), and those ON it (its assignees and its ``project`` edge). Ending a role
removes both, so nothing a role was granted survives it, and a role made later under its name is another uuid.

The order is what makes an ending that failed part-way one its door can finish: the grants naming the role and its
assignees go first, then the record is stamped ended, and the ``project`` edge goes last. Until the record is ended the
role is listed and its ``can_delete`` still resolves through the edge, so the door can be called again; once it is
ended, an edge whose removal failed names an object that grants nothing, which no door reaches.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from lance_namespace import ServiceUnavailableError
from starlette.concurrency import run_in_threadpool

from catalog.services import roles, subject_tuples
from service_kit.governed import fga


if TYPE_CHECKING:
    from openfga_sdk.client import OpenFgaClient

    from catalog.schemas import RoleRecord
    from service_kit.storage_options import StorageOptions


log = logging.getLogger(__name__)


async def end_role(client: OpenFgaClient | None, role: RoleRecord, *, control_root: str, storage_options: StorageOptions, actor: str) -> list[fga.ClientTuple]:
    """End ``role``: every tuple naming it and on it, and its record; the tuples removed. With ``client`` ``None``
    (authorization off) only the record is ended.

    Raises:
        ConcurrentModificationError: Ending it would leave a project with no direct admin; nothing was changed.
        lakehouse_kit.records.RecordChangedError: Its record changed while it was being ended.
    """
    if client is None:
        await run_in_threadpool(roles.end_role_record, control_root, storage_options, role)
        return []
    subject, obj = roles.assignees_of(role.id), roles.role_object(role.id)
    naming = await subject_tuples.tuples_naming(client, subject)
    await subject_tuples.refuse_stranding(client, subject, naming, who=f"role {role.name!r}")
    removed = await subject_tuples.revoke_naming(client, subject, naming, actor=actor, origin="lifecycle_delete")
    on_it = [fga.ClientTuple(user=t.user, relation=t.relation, object=obj) for t in await fga.read_object_tuples(client, obj) if t.object == obj]
    if assigned := [t for t in on_it if t.relation != "project"]:
        removed.extend(await fga.delete_tuples(client, assigned, actor=actor, origin="lifecycle_delete"))  # noqa: TID251
    await run_in_threadpool(roles.end_role_record, control_root, storage_options, role)
    if edge := [t for t in on_it if t.relation == "project"]:
        try:
            removed.extend(await fga.delete_tuples(client, edge, actor=actor, origin="lifecycle_delete"))  # noqa: TID251
        except ServiceUnavailableError:
            log.exception("role_edge_not_removed", extra={"role": obj})
    return removed
