"""The event-driven cascade head — react to a 'bronze batch written' lineage event by firing the pipeline.

medallion-producer subscribes to the shared lineage topic (the same events the catalog + producer already emit on a
write) and, **only** for a write to the bronze namespace/dataset (R23: bronze is the FIRST governed tier —
raw is the external world the producer harvests from), publishes the bronze stage trigger
(``medallion.bronze``) that the bronze->silver movers consume. So the cascade HEAD is driven by the
arrival of external raw INTO bronze — every stage, the head included, reacts to an event on the bus we
already run.

**Loop-guarded**: the lineage topic also carries the movers' own silver/gold writes; those are acked and
ignored (their output namespace isn't bronze), so publishing the trigger can never re-fire the head. The
second guard is by OPERATION rather than by namespace: the catalog publishes its own markers here, and an
attach/detach/declare names the bronze table on a ``COMPLETE`` event without a byte having moved — so
registering the head's tier would otherwise fire a second, batch-less cascade over the same data.
Best-effort with ``RETRY`` so a sidecar/broker outage is redelivered rather than dropped.

**The trigger NAMES the upstream** (I2), resolved through the catalog rather than composed — see
:func:`_vended_upstream`. The arrived table's location is a question only the catalog can answer,
because more than one writer creates bronze: ``POST /produce`` attaches its deployment-contract URI
through ``register_table``, while ``ingest`` creates through the catalog's own door and takes the
vended ``{root}/{hash}_{ns}${name}``. Without the field the mover composed ``{root}/medallion/{ns}``
and the cascade's first leg read whatever the OTHER writer's layout left there.
"""

from __future__ import annotations

import logging
import uuid
from functools import partial
from typing import Any

from dapr.aio.clients import DaprClient
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict

from medallion.core.config import MedallionSettings, dedicated_token_for, project_namespace
from medallion.core.metrics import record_transition
from medallion.services import catalog_register
from service_kit import dapr_publish
from service_kit.lakehouse import transform_specs
from service_kit.lakehouse.warehouse_registry import is_safe_project, lane_key


log = logging.getLogger(__name__)

_SUCCESS = {"status": "SUCCESS"}
_RETRY = {"status": "RETRY"}


#: Catalog operations that MOVE NO BYTES — an attach, a detach, or a metadata-only declaration.
#:
#: The catalog emits its markers on this very topic, as ``COMPLETE`` events whose single output is the
#: table's own ``(namespace, name)`` — indistinguishable, on the fields above, from a batch landing. So
#: the moment the cascade HEAD began registering the bronze it seeds, one ``/produce`` published TWO
#: matching events and would have driven two unrelated bronze->gold runs over one batch, the second with
#: a token nothing else in the cascade carries. Registering is a governance act, not an arrival.
#:
#: A DENYLIST, not an allowlist of write ops: an external OpenLineage producer names its own operations
#: (or none), and it must keep firing the head exactly as before. Only the ops the catalog itself stamps
#: for a byte-free change are excluded, and the strings are the wire contract — the medallion reads the
#: bus, it does not import the catalog.
_BYTE_FREE_CATALOG_OPERATIONS = frozenset({"register_table", "deregister_table", "declare_table"})


def _lance_facet(event: dict[str, Any]) -> dict[str, Any]:
    """The event's ``lance`` run facet, or an empty mapping — the untrusted-envelope guards in one place."""
    run = event.get("run")
    if isinstance(run, dict):
        facets = run.get("facets")
        if isinstance(facets, dict):
            lance = facets.get("lance")
            if isinstance(lance, dict):
                return lance
    return {}


class BronzeWrite(BaseModel):
    """One matched bronze arrival: the LANE the trigger names, and the CATALOG ID it was written as.

    The two are different strings and both are needed. ``lane`` is tenant-free (``bronze$events``) —
    the same value for every tenant, which is what makes a mover's discriminator work with the tenant
    travelling separately on ``trigger.project``. ``table_id`` is the catalog's own identifier
    (``acme-bronze$events``), and it is the only thing the catalog will answer a `describe` for.

    ``table_id`` is READ OFF THE EVENT, never recomposed from the lane. `project_namespace(project,
    lane)` is the right inverse only for the configured branch: a DECLARED lane's `from_id` need not
    carry the project prefix at all, so recomposing would ask the catalog about a table that does not
    exist and silently fall back to a composed path.
    """

    model_config = ConfigDict(frozen=True)

    lane: str
    table_id: str


def _bronze_write(event: dict[str, Any], settings: MedallionSettings, project: str) -> BronzeWrite | None:
    """The bronze dataset this event COMPLETED a write to (the cascade's entry point), else ``None``.

    Filters on ``eventType == COMPLETE``: a START or FAIL bronze event announces intent / failure, not a
    landed batch, so firing the cascade off one would kick the pipeline over data that isn't there (yet).
    Only a terminal-success bronze write is a real arrival — and an ATTACH is not a write, which is what
    :data:`_BYTE_FREE_CATALOG_OPERATIONS` excludes. TWO ingest lanes share the head: the events
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
    if str(_lance_facet(event).get("operation") or "") in _BYTE_FREE_CATALOG_OPERATIONS:
        return None
    expected_namespace = project_namespace(project, settings.bronze_namespace)
    expected = {project_namespace(project, settings.bronze_dataset): settings.bronze_dataset}
    outputs = event.get("outputs") or []
    for output in outputs:
        if not isinstance(output, dict):
            continue
        name = str(output.get("name") or "")
        if output.get("namespace") == expected_namespace and name in expected:
            return BronzeWrite(lane=expected[name], table_id=name)
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
            return BronzeWrite(lane=lane_key(project, name), table_id=name)
    return None


def _bronze_write_dataset(event: dict[str, Any], settings: MedallionSettings, project: str) -> str | None:
    """The LANE half of :func:`_bronze_write` — the head's matching contract, on its own.

    Kept as its own reader because the question "does this event fire the cascade, and for which lane"
    is asked by every suite that pins the head's agreement with a producer (the medallion's own, the
    ingest plane's, the governed-bronze end-to-end), and none of them cares where the table lives.
    """
    write = _bronze_write(event, settings, project)
    return write.lane if write is not None else None


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
    token = _lance_facet(event).get("token")
    if token:
        return str(token)
    run = event.get("run")
    if isinstance(run, dict) and run.get("runId"):
        return str(run["runId"])
    return uuid.uuid4().hex[:12]


def _cascade_project(event: dict[str, Any]) -> str:
    """The per-tenant project this bronze write belongs to — the ``lance.project`` run facet (#84), or ``""``.

    Absent/unsafe → ``""`` (the single-tenant default): a value outside the path-safe shape must never
    become an S3 prefix or a lineage-name qualifier, and with ``""`` the qualified bronze filter reduces to
    the fixed pair — so a forged/garbage facet cannot fire the head for a tenant.
    """
    project = _lance_facet(event).get("project")
    return project if isinstance(project, str) and is_safe_project(project) else ""


def _cascade_originator(event: dict[str, Any]) -> str:
    """The HUMAN whose request produced this bronze write — the ``lance.originator`` run facet, or ``""``.

    The cascade head is the last place a verified subject exists: by the time a silver or gold stage
    fails, the HTTP request that started it is long gone and the mover authors as a role. Reading it here
    and putting it on the trigger is what lets a failure five stages later still name the person whose
    work it was.
    """
    originator = _lance_facet(event).get("originator")
    return originator.strip() if isinstance(originator, str) else ""


async def _vended_upstream(settings: MedallionSettings, table_id: str) -> str:
    """Where the CATALOG says the arrived bronze table lives, or ``""`` to let the mover compose a path.

    I2 ON THE HEAD, and it is what makes the cascade's first leg independent of WHICH SERVICE CREATED
    THE TABLE. Without it the mover falls through to `_resolve_roots`' `{root}/medallion/{namespace}`,
    a path only `produce.py` writes — so a bronze table that `ingest` created through the catalog's own
    door lives at the vended `{root}/{hash}_{ns}${name}`, the mover opens the composed path, and the
    cascade fires correctly, wakes, finds none of those rows, and acks 200. `/publication-arrival` has
    carried the vended location since I2; this is the same field on the other head.

    THE ANSWER IS ADVISORY, and every way of not getting one degrades to ``""`` — the composed-path
    fallback, which is the CORRECT upstream for a produce-first estate (the chart renders
    `MEDALLION_BRONZE_URI` and the mover's `MEDALLION_FROM_URI` from one expression, so the composed
    path is where those bytes are). The same shape and the same reasoning as `_has_declared_lane`
    above: a catalog that cannot be read must not stop the head from firing, and a head that answered
    RETRY to a describe outage would halt a cascade that works. Logged, because an unreachable catalog
    is a real fault.

    The mover confines whatever is named here to the storage root it resolves (`_confine_from_uri`),
    so this is a claim on an untrusted-by-default field, not a read primitive.
    """
    if not settings.catalog_url:
        return ""  # the ungoverned dev shape — the same escape hatch `produce.py` and the movers keep
    try:
        location = await run_in_threadpool(
            partial(
                catalog_register.describe_table_location,
                catalog_url=settings.catalog_url,
                table_id=table_id,
                token=settings.catalog_token,
                app_token=settings.app_api_token,
                service_identity=settings.catalog_service_identity,
                dedicated_token=dedicated_token_for(settings),
            )
        )
    except catalog_register.RegisterError as exc:
        log.warning("cascade_head_location_lookup_failed", extra={"table_id": table_id, "error": str(exc)})
        return ""
    if not location:
        # Not a fault: an external OpenLineage producer may write a table this catalog does not
        # govern, and such a table cascades off the composed path.
        log.debug("cascade_head_table_not_governed", extra={"table_id": table_id})
    return location or ""


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
    write = _bronze_write(data, settings, project)
    if write is None:
        return _SUCCESS  # not a bronze ingest — ack so Dapr doesn't redeliver, but drive nothing
    dataset = write.lane
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
    # THE UPSTREAM THE CATALOG VENDED (I2) — the same field `/publication-arrival` puts on its trigger,
    # so both heads name where the mover should read. OMITTED rather than blank when unresolved: `""` is
    # not a location, and the mover reads an ABSENT `from_uri` as "compose the path", which is what
    # keeps an in-flight trigger from a pre-rollout head — and any external publisher that names no
    # upstream — working unchanged.
    from_uri = await _vended_upstream(settings, write.table_id)
    if from_uri:
        trigger["from_uri"] = from_uri
    originator = _cascade_originator(data)
    if originator:  # the human the whole cascade is for; omitted (byte-identical) when unset
        trigger["originator"] = originator
    landed = await dapr_publish.publish_json(
        dapr,
        pubsub_name=settings.pubsub,
        topic_name=settings.bronze_topic,
        payload=trigger,
        timeout_seconds=settings.publish_timeout_seconds,
        failure_event="medallion_bronze_arrival_publish_failed",
        context={"token": token, "dataset": dataset},
    )
    if not landed:
        return _RETRY
    record_transition(f"source->{settings.bronze_namespace}")
    log.info("medallion_cascade_triggered", extra={"token": token, "dataset": dataset})
    return _SUCCESS
