"""The index-build subscription: ONE index per delivery.

The catalog's `create_index` / `create_scalar_index` doors publish; this executes. Registered only
when an index topic is configured, so a deployment without a broker never advertises a subscription
the sidecar would try to deliver to — and the catalog reads the same topic name, so neither side can
believe a worker exists when it does not.

Its own COMPONENT beside the maintenance work queue, and the reason is the ack rather than tidiness:
one `ackWait` cannot serve both a minutes-long compaction and a vector index over a large table — and
`ackWait`, `durableName` and `queueGroupName` are all per-COMPONENT in Dapr's JetStream pubsub, so a
second topic on the work component would inherit the work queue's window. See `IndexWorkItem` for why
the whole build crosses rather than a plan.

Authenticated by the Dapr app-api-token like every other subscription here: a forged unit could
otherwise name any URI in any bucket and have this service write an index into it.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from dapr.ext.fastapi import DaprApp
from fastapi import Depends, FastAPI
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError

from maintenance.api.dependencies import SettingsDep
from maintenance.core.config import MaintenanceSettings
from maintenance.services import credentials
from maintenance.services.index_build import UnknownIndexKindError, build_index
from maintenance.services.work_queue import SUCCESS
from service_kit.draining import retry_when_draining
from service_kit.governed.dapr_auth import require_dapr_token
from service_kit.lakehouse.work_items import IndexWorkItem


log = logging.getLogger(__name__)

#: Dapr's own retry verdict. Named rather than inlined so the two answers this handler can give read
#: as a pair — `work_queue.SUCCESS` supplies the other.
RETRY = "RETRY"


async def handle_index_unit(event: dict[str, Any], settings: MaintenanceSettings) -> dict[str, str]:
    """Build one index and decide whether the unit is done — the testable half of the route.

    THREE ANSWERS, and the middle one is the one worth stating. A malformed unit is ACKED: it will not
    parse on the tenth attempt either, and retrying only delays the DLQ while occupying a worker. An
    unknown index KIND is also acked — same reason, it is a producer defect no redelivery repairs. Any
    other failure RETRIES, because a store outage or a mid-build crash is exactly what redelivery is
    for, and an index build is idempotent under `replace` and re-runnable without it (a duplicate name
    is refused by Lance rather than silently doubling the index).
    """
    try:
        item = IndexWorkItem.model_validate(event.get("data", event))
    except ValidationError:
        log.warning("index_unit_malformed", extra={"event": str(event)[:200]})
        return {"status": SUCCESS}
    # THE SAME CREDENTIAL PATH THE REWRITE TAKES. An index build writes files under the table's own
    # prefix, so it is a write, and it is signed by a credential scoped to that one table where the
    # vending door offers one — falling back to the ambient credential otherwise, which is what this
    # service always used.
    options = settings.storage_options()
    write_options = await run_in_threadpool(credentials.write_options_for, item.uri, settings, fallback=options, declared_table_id=item.table_id or None)
    try:
        outcome = await run_in_threadpool(build_index, item, write_options=write_options)
    except UnknownIndexKindError:
        log.warning("index_unit_unbuildable", extra={"uri": item.uri, "kind": item.kind})
        return {"status": SUCCESS}
    except Exception:
        log.exception("index_build_failed", extra={"uri": item.uri, "column": item.column, "index_type": item.index_type})
        return {"status": RETRY}
    log.info("index_unit_done", extra={"uri": item.uri, "index": outcome.name, "version": outcome.version})
    return {"status": SUCCESS}


def register_index_route(app: FastAPI, settings: MaintenanceSettings, dapr_app: DaprApp | None = None) -> DaprApp | None:
    """Register the index subscription, or nothing when this deployment has no index queue.

    Takes an existing :class:`DaprApp` when the caller already made one — a second ``DaprApp(app)``
    re-registers ``/dapr/subscribe`` and the sidecar then reads only one of the two.
    """
    if not settings.index_topic or not settings.execute_work:
        return dapr_app
    wrapper = dapr_app or DaprApp(app)

    @wrapper.subscribe(
        pubsub=settings.index_pubsub,
        topic=settings.index_topic,
        route="/maintenance-index",
        dead_letter_topic=settings.index_dlq_topic or None,
    )
    async def on_index_unit(
        event: dict[str, Any],
        *,
        config: SettingsDep,
        _: Annotated[None, Depends(require_dapr_token)],
        drain: Annotated[dict[str, str] | None, Depends(retry_when_draining)] = None,
    ) -> dict[str, str]:
        """``event`` is typed ``dict`` so FastAPI parses the CloudEvent body — an ``Any`` param becomes
        a query param and 422s.

        A draining replica asks for REDELIVERY rather than starting a build. Dapr's delivery does not
        consult a readiness probe, so without this a pod mid-shutdown would begin an index it cannot
        finish and leave its partial segments for the next GC.
        """
        if drain is not None:
            return drain
        return await handle_index_unit(event, config)

    return wrapper
