"""The event-driven cascade head — react to a 'bronze batch written' lineage event by firing the pipeline.

medallion-producer subscribes to the shared lineage topic (the same events the catalog + producer already emit on a
write) and, **only** for a write to the bronze namespace/dataset (R23: bronze is the FIRST governed tier —
raw is the external world the producer harvests from), publishes the bronze stage trigger
(``medallion.bronze``) that the bronze->silver movers consume. So the cascade HEAD is driven by the
arrival of external raw INTO bronze — every stage, the head included, reacts to an event on the bus we
already run.

**Loop-guarded**: the lineage topic also carries the movers' own silver/gold writes; those are acked and
ignored (their output namespace isn't bronze), so publishing the trigger can never re-fire the head.
Best-effort with ``RETRY`` so a sidecar/broker outage is redelivered rather than dropped.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from dapr.aio.clients import DaprClient

from medallion.core.config import MedallionSettings, project_namespace
from medallion.core.metrics import record_transition
from service_kit import dapr_publish
from service_kit.lakehouse import transform_specs
from service_kit.lakehouse.warehouse_registry import is_safe_project, lane_key


log = logging.getLogger(__name__)

_SUCCESS = {"status": "SUCCESS"}
_RETRY = {"status": "RETRY"}


def _bronze_write_dataset(event: dict[str, Any], settings: MedallionSettings, project: str) -> str | None:
    """The bronze dataset this event COMPLETED a write to (the cascade's entry point), else ``None``.

    Filters on ``eventType == COMPLETE``: a START or FAIL bronze event announces intent / failure, not a
    landed batch, so firing the cascade off one would kick the pipeline over data that isn't there (yet).
    Only a terminal-success bronze write is a real arrival. TWO ingest lanes share the head: the events
    lane (``bronze_dataset``) — the returned name is
    the one actually written, so the trigger tells the mover which lane fired.

    With a ``project`` (#84, from the event's ``lance.project`` facet) the expected pair is the
    project-QUALIFIED one (``acme-bronze`` / ``acme-bronze$events``) — a per-project bronze write fires
    the head for exactly its own tenant. Empty project keeps the fixed single-tenant pair byte-identically,
    and the loop guard holds either way: a mover's output namespace (``[<project>-]silver/gold``) never
    equals the (equally qualified) bronze namespace.
    """
    if str(event.get("eventType", "")).upper() != "COMPLETE":
        return None
    expected_namespace = project_namespace(project, settings.bronze_namespace)
    expected = {project_namespace(project, settings.bronze_dataset): settings.bronze_dataset}
    outputs = event.get("outputs") or []
    for output in outputs:
        if not isinstance(output, dict):
            continue
        name = str(output.get("name") or "")
        if output.get("namespace") == expected_namespace and name in expected:
            return expected[name]
        # THE DECLARATION IS THE OPT-IN. Before this, the head recognised exactly one hard-coded
        # dataset and acked everything else without publishing — so a table created from the UI
        # produced NO trigger at all, the mover's guard was never reached, and an agnostic platform
        # behaved as a fixed pipeline needing a values edit per table.
        #
        # A table a lane DECLARES is now a cascade head too. Deliberately not "publish everything and
        # let movers filter": that spends delivery on work nobody declared, and leaves "why didn't my
        # table cascade" with no visible answer. With this, the answer is "no lane declares it", and
        # there is an audited door to change that.
        #
        # Returned as a LANE KEY, tenant-free, exactly like the configured branch above.
        #
        # This used to return the declared `from_id` VERBATIM (a catalog id), on the reasoning that
        # the mover resolves its identity from the same record so both sides read one string. That
        # reasoning is sound for IDENTITY and wrong for the TRIGGER: a trigger's `dataset` is a lane
        # key -- the same string for every tenant, with the tenant travelling separately on
        # `trigger.project`. `publication_trigger` learned that the hard way (it published the
        # catalog identifier once and every tenant's publication was dropped as another lane's), and
        # this branch never followed. The result was ONE function returning two different kinds of
        # thing depending on which branch fired, so a lane declared through the door was reachable
        # from this head and not from the publication head.
        if name and _has_declared_lane(settings, project=project, table_id=name):
            return lane_key(project, name)
    return None


def _has_declared_lane(settings: MedallionSettings, *, project: str, table_id: str) -> bool:
    """Whether any lane in this project declares ``table_id`` as its input.

    Never raises: a control root that cannot be read must not stop the CONFIGURED dataset from
    cascading, so an unreadable registry degrades to "nothing extra is declared" rather than taking
    the head down. Logged, because a registry that cannot be read is a real fault.
    """
    control_root = getattr(settings, "control_root", "")
    if not project or not control_root:
        return False
    try:
        specs = transform_specs.list_specs(control_root, settings.storage_options(), project)
    except Exception:  # noqa: BLE001 — a registry read must not break the cascade head
        log.exception("cascade_head_lane_lookup_failed", extra={"project": project, "table_id": table_id})
        return False
    return any(spec.from_id == table_id for spec in specs)


def _cascade_token(event: dict[str, Any]) -> str:
    """The correlation token that threads one cascade — the bronze-write event's ``lance.token`` run facet.

    The run ``runId`` is now an opaque UUID (spec fix), so the human-readable token that ties all the
    stages together rides the ``lance`` facet instead. Fall back to the ``runId`` (still a stable
    per-run handle), then to a fresh id, so an external bronze writer that omits the facet still cascades.
    """
    run = event.get("run")
    if isinstance(run, dict):
        lance = (run.get("facets") or {}).get("lance")
        if isinstance(lance, dict) and lance.get("token"):
            return str(lance["token"])
        if run.get("runId"):
            return str(run["runId"])
    return uuid.uuid4().hex[:12]


def _cascade_project(event: dict[str, Any]) -> str:
    """The per-tenant project this bronze write belongs to — the ``lance.project`` run facet (#84), or ``""``.

    Absent/unsafe → ``""`` (the single-tenant default): a value outside the path-safe shape must never
    become an S3 prefix or a lineage-name qualifier, and with ``""`` the qualified bronze filter reduces to
    the fixed pair — so a forged/garbage facet cannot fire the head for a tenant.
    """
    run = event.get("run")
    if isinstance(run, dict):
        lance = (run.get("facets") or {}).get("lance")
        if isinstance(lance, dict):
            project = lance.get("project")
            if isinstance(project, str) and is_safe_project(project):
                return project
    return ""


def _cascade_originator(event: dict[str, Any]) -> str:
    """The HUMAN whose request produced this bronze write — the ``lance.originator`` run facet, or ``""``.

    The cascade head is the last place a verified subject exists: by the time a silver or gold stage
    fails, the HTTP request that started it is long gone and the mover authors as a role. Reading it here
    and putting it on the trigger is what lets a failure five stages later still name the person whose
    work it was.
    """
    run = event.get("run")
    if isinstance(run, dict):
        facets = run.get("facets")
        if isinstance(facets, dict):
            lance = facets.get("lance")
            if isinstance(lance, dict):
                originator = lance.get("originator")
                if isinstance(originator, str) and originator.strip():
                    return originator.strip()
    return ""


async def handle_bronze_arrival(dapr: DaprClient, settings: MedallionSettings, event: Any) -> dict[str, str]:
    """Fire the cascade head when a bronze-dataset write arrives; ack-and-ignore everything else.

    ``event`` is the untrusted Dapr CloudEvent envelope (hence ``Any`` + the ``isinstance`` guards); its
    ``data`` is the OpenLineage run event. Only a write to ``bronze_namespace``/``bronze_dataset`` (or the
    page lane) publishes the ``medallion.bronze`` trigger — a downstream
    mover's event (silver/gold) is acked and skipped, so the head never self-triggers (loop guard). A
    publish outage returns ``RETRY`` for redelivery.
    """
    data = event.get("data") if isinstance(event, dict) else None
    if not isinstance(data, dict):
        return _SUCCESS  # not a parseable lineage event — ack so Dapr doesn't redeliver
    project = _cascade_project(data)
    dataset = _bronze_write_dataset(data, settings, project)
    if dataset is None:
        return _SUCCESS  # not a bronze ingest — ack so Dapr doesn't redeliver, but drive nothing
    token = _cascade_token(data)
    # THE BATCH IDENTITY IS MINTED HERE (§8 change 9), because this is where a batch begins: one
    # `/produce`, one bronze write, one cascade. Every tier below carries this same id, so the runs of
    # one batch are joinable in the graph instead of three unrelated hops sharing only a dataset name.
    #
    # SEEDED FROM the bronze-write token rather than a fresh uuid: that token already identifies this
    # ingest, so a person holding it can find the whole cascade, and a redelivered head produces the
    # SAME batch id rather than forking the batch in two.
    trigger = {
        "token": token,
        "cascade_id": token,
        "dataset": dataset,
        "namespace": settings.bronze_namespace,
    }
    if project:  # #84: PROPAGATE the tenant onto the stage trigger; omitted (byte-identical) when unset
        trigger["project"] = project
    originator = _cascade_originator(data)
    if originator:  # the human the whole cascade is for; omitted (byte-identical) when unset
        trigger["originator"] = originator
    try:
        await dapr_publish.publish_event(
            dapr,
            timeout_seconds=settings.publish_timeout_seconds,
            pubsub_name=settings.pubsub,
            topic_name=settings.bronze_topic,
            data=json.dumps(trigger),
            data_content_type="application/json",
        )
        record_transition(f"source->{settings.bronze_namespace}")
    except Exception as exc:
        log.warning("medallion_bronze_arrival_publish_failed", extra={"token": token, "error": str(exc)})
        return _RETRY
    log.info("medallion_cascade_triggered", extra={"token": token, "dataset": dataset})
    return _SUCCESS
