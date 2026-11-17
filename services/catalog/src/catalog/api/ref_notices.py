"""A ref door's notice to the person who puts a ref an erasure release will take (ADR 0233 §9).

The door runs its ref write with :class:`catalog.services.erasure_release.RefNotices` as its ``before_ref``, which files
the notice inside the table's history hold before the ref is written, and this sends what it filed once the write
returns: every notice when the ref was written, and only those whose ref the store shows landed when the write failed,
so a ref that never landed is never announced and the release, which waits while a filing stands, is not held up by
one. A filing a dead request left is sent by the catalog's finish (``catalog.api.announcing``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi.concurrency import run_in_threadpool

from catalog.api.announcing import Announcer
from catalog.api.security import Caller
from catalog.core.config import Settings
from catalog.services import erasure_release
from catalog.services.dataplane import BeforeRef
from catalog.services.table_claims import DoorTable
from lakehouse.governed import fga


log = logging.getLogger(__name__)


async def putting_ref[T](put: Callable[[BeforeRef | None], T], *, table: DoorTable, settings: Settings, announcer: Announcer, token: Caller | None) -> T:
    """Run ``put``, a ref door's write given its ``before_ref``, in the thread pool, telling its caller of each release that
    will take the ref. A caller no token names is nobody a notice can address, and nothing is filed for it.

    Raises:
        Exception: Whatever ``put`` raised, once what it filed is judged.
    """
    if token is None:
        return await run_in_threadpool(put, None)
    table_uuid = table.resolved.require_uuid()
    notices = erasure_release.RefNotices(
        settings.registry_root,
        settings.storage_options(),
        table_uuid=table_uuid,
        subject=fga.user_object(token.subject),
        object_id=fga.table_object(table_uuid),
    )
    try:
        response = await run_in_threadpool(put, notices)
    except Exception:
        for key, told in notices.filed:
            try:
                await announcer.send_landed(key, told)
            except Exception as exc:  # noqa: BLE001 — the write's own failure is the answer; the finish sends what stands
                log.warning("erasure_release_creator_unjudged", extra={"key": key, "error": f"{type(exc).__name__}: {exc}"[:300]})
        raise
    for key, told in notices.filed:
        await announcer.send_filed(key, told)
    return response
