"""The lakehouse side of the publish saga — the concrete :class:`~annotator.projects.saga.Publisher`.

Posts through the catalog's REST API over the stock Lance Namespace SDK, the stack the media plane's
merge transport uses (`client_kit.lancekit.catalog`) — **the annotator never writes Lance directly**
(§7.1): the catalog is what places the new table in its namespace's authorization graph and emits the
``CREATE`` RunEvent.

Retry safety maps onto the catalog's own contract: ``create`` posts ``mode=exist_ok``. Fresh id ⇒ the
catalog creates and emits the event. Replayed id ⇒ the catalog KEEPS the existing table and returns its
version — exactly the "existing table with this id is success" the saga relies on.

The publish tags nothing. A tag in the lakehouse is a publication pointer, created and moved by an owner
or a publisher (`can_create_tag`), and the annotator's account is neither: a service is never made the
owner of what it creates, and its grant is writer on the target. The version the create returns, which
the project records and whose commit names the publish as its run, is the publish's reference; blessing
it is the tenant's act at the catalog's publish door.

A create the catalog does not answer is sent again (:data:`CREATE_ATTEMPTS`), which ``mode=exist_ok``
makes safe.

**The saga publishes as the annotator's own ServiceAccount** (ADR 0171): its projected token, which the
catalog verifies like any service's, read again on every call because the kubelet replaces it before it
expires. A project admin grants that principal writer on the target namespace and read on its inputs
through the catalog's grant door, and ``x-lance-originator`` names the person the publish is for, so the
catalog tells them their run committed. The saga outlives the person's request, so it cannot use their
token.

`spawn_publish` is the seam the project actor's watchdog reminder calls: it schedules
:func:`run_publish_for` on the event loop OUTSIDE the actor's turn, because the saga drives the
actor through its own proxy surface and an in-turn await would deadlock on the actor's mailbox.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from annotator.core.config import get_annotator_settings
from annotator.projects.publish import PUBLISHED_LABELS_SCHEMA, PublishPlan, namespace_for
from annotator.projects.saga import PublishOutcome, run_publish
from client_kit.lancekit.catalog import NamespaceCatalogWriteTransport, connect_catalog
from service_kit.exceptions import ServiceUnavailableError
from service_kit.governed.machine_identity import identity_bearer
from service_kit.naming import CATALOG_DELIMITER


logger = logging.getLogger(__name__)

#: The catalog Context headers that name a write's run and its inputs (ADR 0156 C2).
RUN_ID_HEADER = "x-rask-run-id"
INPUTS_HEADER = "x-rask-inputs"

#: Attempts at the publish's create before it fails the saga run. A dropped connection or an unanswered call is retried
#: here rather than left to the watchdog's next tick, and retrying is safe by construction, not by hope: `create` posts
#: `mode=exist_ok`, the property this module's retry-safety argument rests on. pylance 13.0.0's REST client retries
#: nothing itself (measured 2026-11-02: one attempt against a reset connection, then an error).
CREATE_ATTEMPTS = 3
#: The pause before the second attempt, doubled before each later one.
CREATE_BACKOFF_S = 0.5


def _arrow_stream_bytes(plan: PublishPlan) -> bytes:
    """The plan's rows as an Arrow-IPC stream body for the catalog's create. Explicit schema, so an
    all-sentinel publish still produces correctly-typed columns."""
    import pyarrow as pa  # noqa: PLC0415 - heavy import, publish path only

    from service_kit.arrow_ipc import encode_arrow_stream  # noqa: PLC0415 - keeps pyarrow off module import

    return encode_arrow_stream(pa.Table.from_pylist(plan.rows, schema=PUBLISHED_LABELS_SCHEMA))


class CatalogPublisher:
    """`saga.Publisher` over the catalog's REST API, through the stock Lance Namespace SDK.

    The create is a blocking SDK call, so it runs in a worker thread; a namespace is built per call, carrying that call's
    bearer and context headers."""

    def __init__(
        self,
        base_url: str,
        *,
        delimiter: str = CATALOG_DELIMITER,
        token_file: Path | None = None,
        originator: str | None = None,
    ) -> None:
        self._base_url = base_url
        self._delimiter = delimiter
        self._token_file = token_file
        self._originator = originator

    def _headers(self) -> dict[str, str]:
        """This call's headers: the annotator's own token read now, and the person the publish is for.

        WHO this publish is for. The token is the service's credential and the catalog authors every resulting event
        as that service, so the originator is the only place the human survives. It authorizes nothing — the
        notifications plane re-derives visibility per recipient at delivery — which is why it may ride beside a
        service bearer.
        """
        headers = identity_bearer(self._token_file) if self._token_file is not None else {}
        if self._originator:
            headers["x-lance-originator"] = self._originator
        return headers

    async def create_table(
        self,
        table_id: str,
        plan: PublishPlan,
        *,
        properties: dict[str, str],
        run_id: str,
        source: str | None = None,
        source_version: int | None = None,
    ) -> int:
        """Create (or converge on) the published table; return its version."""
        body = _arrow_stream_bytes(plan)
        headers = {**self._headers(), RUN_ID_HEADER: run_id}
        if source is not None and source_version is not None:
            headers[INPUTS_HEADER] = json.dumps([{"table": source.split(self._delimiter), "version": source_version}])
        catalog = NamespaceCatalogWriteTransport(
            connect_catalog(self._base_url, token=None, delimiter=self._delimiter, headers=headers),
            table_id.split(self._delimiter),
        )
        version = await asyncio.to_thread(_create_with_retries, catalog, body, properties)
        return version or 1


def _create_with_retries(catalog: NamespaceCatalogWriteTransport, body: bytes, properties: dict[str, str]) -> int | None:
    """The create, sent again after an unavailable answer until :data:`CREATE_ATTEMPTS` are spent."""
    for attempt in range(1, CREATE_ATTEMPTS):
        try:
            return catalog.create(body, mode="exist_ok", properties=properties)
        except ServiceUnavailableError:
            logger.warning("the catalog did not take the publish's create (attempt %d of %d); sending it again", attempt, CREATE_ATTEMPTS)
            time.sleep(CREATE_BACKOFF_S * 2 ** (attempt - 1))
    return catalog.create(body, mode="exist_ok", properties=properties)


# --------------------------------------------------------------------------------------------------
# The runner — what the watchdog reminder actually drives
# --------------------------------------------------------------------------------------------------

#: Projects with a saga run live IN THIS PROCESS. Actors are single-placement, so the reminder for a
#: given project only ever fires on the pod hosting its actor — a process-local guard is the whole
#: story, and a 60 s tick racing a live run stands down here instead of double-driving the catalog.
_RUNNING: set[str] = set()


#: Strong references to in-flight saga tasks. asyncio holds only a weak reference to a running task,
#: so without this a publish could be collected mid-flight — losing the work and leaking `_RUNNING`.
_TASKS: set[asyncio.Task[None]] = set()


def spawn_publish(project_id: str) -> asyncio.Task[None] | None:
    """Schedule the saga for one project, unless it is already running here."""
    if project_id in _RUNNING:
        logger.debug("publish for %s already running — the tick stands down", project_id)
        return None

    async def _drive() -> None:
        try:
            outcome = await run_publish_for(project_id)
            if outcome is not None:
                logger.info(
                    "published project %s: table %s v%d (%d rows, %d shapes)%s",
                    outcome.project_id,
                    outcome.table_id,
                    outcome.version,
                    outcome.rows,
                    outcome.shapes,
                    " [converged]" if outcome.already_published else "",
                )
        except Exception:
            # `run_publish` already fired `publish_failed` with the reason; the next watchdog tick
            # (or an operator retry) re-drives. This log is the operator-facing trace.
            logger.exception("publish saga failed for project %s", project_id)
        finally:
            _RUNNING.discard(project_id)

    task = asyncio.get_running_loop().create_task(_drive())
    # Claimed only once the task EXISTS. Adding to `_RUNNING` first meant a `create_task` that raised
    # pinned the id forever, and `spawn_publish` would then stand down on every later tick — a project
    # that can never publish again, with nothing naming why.
    _RUNNING.add(project_id)
    # A STRONG REFERENCE, which asyncio requires and this did not hold: the event loop keeps only a
    # weak one, so an unreferenced task can be garbage-collected mid-flight. That loses the publish
    # AND leaks `_RUNNING`, because `_drive`'s `finally` never runs. The caller's returned handle is
    # not enough — `run_watchdog` discards it.
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


async def run_publish_for(project_id: str) -> PublishOutcome | None:
    """Run the saga for one project against the real actors and the real catalog.

    Reads the pinned target namespace and trigger off the project document — the endpoint
    authorized THAT namespace, so this never guesses one.
    """
    from annotator.projects.actor import AnnotationTaskActorInterface  # noqa: PLC0415 - avoids an import cycle
    from annotator.projects.project_actor import AnnotationProjectActorInterface  # noqa: PLC0415
    from annotator.projects.proxies import typed_proxy  # noqa: PLC0415 - opens a sidecar channel

    project_handle: Any = typed_proxy("AnnotationProjectActor", project_id, AnnotationProjectActorInterface)

    def task_handle(task_id: str) -> Any:
        return typed_proxy("AnnotationTaskActor", task_id, AnnotationTaskActorInterface)

    raw = await project_handle.get()
    if raw is None:
        logger.warning("publish watchdog fired for project %s but its actor holds no state", project_id)
        return None
    # Qualified by the project's own tenant, not a bare literal: this is the crash-resume path, so
    # the namespace it picks is the one a publish lands in with no request left to correct it.
    namespace = str(raw.get("pending_target_namespace") or namespace_for(str(raw.get("tenant") or ""), "silver"))
    subject = str(raw.get("pending_publish_by") or "system")

    settings = get_annotator_settings()
    if not settings.catalog_uri:
        # No transport is a CONFIGURATION failure, and it must surface as one — `publish_failed`
        # with a reason the UI can show — rather than stranding the project in `publishing` while
        # the watchdog ticks forever against a catalog that was never named.
        try:
            await project_handle.fire(
                {
                    "event": "publish_failed",
                    "actor": subject,
                    "error": "MEDIA_CATALOG_URI is not configured — the publish transport has no catalog to post to",
                }
            )
        except Exception:
            logger.exception("could not record the missing-catalog failure for project %s", project_id)
        return None

    # The saga's identity is this pod's own ServiceAccount token, read on each call. One that cannot be read fails the
    # call it was for, which surfaces as publish_failed with the reason, and the watchdog's next tick reads it again.
    publisher = CatalogPublisher(
        settings.catalog_uri,
        delimiter=settings.catalog_delimiter,
        token_file=settings.catalog_token_file,
        originator=subject,
    )
    return await run_publish(
        project_handle=project_handle,
        task_handle=task_handle,
        publisher=publisher,
        namespace=namespace,
        subject=subject,
    )
