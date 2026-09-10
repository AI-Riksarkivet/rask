"""In-service authz for the lineage read + ingest endpoints.

The lineage service owns the audit graph, so it must protect it itself (in-service, not
via a gateway). It mirrors the catalog's authz guard (``services/catalog/api/fga_deps.py``) and **reuses
the catalog's core** — :func:`service_kit.governed.fga.check` / ``batch_check`` — so the OpenFGA check has
one source of truth. The thin FastAPI authz + filter dependencies are re-derived here
because they bind to ``LineageSettings`` rather than the catalog's ``Settings``. (Shared
*library* code; the service makes no runtime call to the catalog — it talks only to the IdP
and the shared OpenFGA store, read-only.)

Three holes this closes (audit ``w8u4rc2tg``):

* **Reads** (``upstream``/``downstream``/``producers``/``graph``) leaked the entire data
  estate. Each is now gated on OpenFGA ``can_get_metadata`` of ``table:<dataset>`` — the
  same permission the catalog requires to ``describe`` that table.
* **Transitive disclosure.** A neighbor/graph read also returns *related* dataset names, so
  :class:`DatasetFilter` (and :func:`governed`) batch-check each and drop the ones the caller
  may not see — mirroring the catalog's ``list_objects``-filtered enumerations.
* **Ingest** was unauthenticated and the run ``author`` was a producer-supplied facet, so
  provenance was forgeable. The author is taken from the verified token
  (:func:`enforce_author`) — the client-claimed facet is overwritten.

Default OFF (``RASK_FGA_ENABLED``), exactly like the catalog; production enables it.
Fail-closed when enabled-but-unwired (503, never silent allow).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated, Final

from fastapi import Depends, Request
from lance_namespace import (
    PermissionDeniedError,
    ServiceUnavailableError,
    UnauthenticatedError,
)
from openfga_sdk import OpenFgaClient

from lineage.api.dependencies import RepositoryDep, SettingsDep
from lineage.api.security import CurrentToken, Principal
from lineage.core.config import LineageSettings
from lineage.models import RunEvent, author_sub_from_payload
from service_kit.governed import fga


log = logging.getLogger(__name__)


#: Operations that are MAINTENANCE rather than data writes. `model.fga` separates the two rungs in
#: both directions on purpose — a maintainer rewrites HOW a dataset is stored, a writer changes WHAT it
#: says — and the owner ruling (2026-09-08, zero trust) is that the sweep gets the former and never the
#: latter, so authorizing its provenance on `can_write_data` would demand a grant the estate refuses to
#: issue. `create_index` is here because building an index changes no row either; it is emitted by BOTH
#: the sweep and the catalog's own door, which is exactly why the rule below accepts EITHER rung rather
#: than swapping one for the other.
_MAINTENANCE_OPERATIONS: Final = frozenset({"compaction", "create_index"})

#: The relation a data write demands. Named once so the two doors cannot drift on it.
_WRITE_RELATIONS: Final = ("can_write_data",)
_MAINTENANCE_RELATIONS: Final = ("can_write_data", "can_maintain")


def relations_for_operation(operation: str | None) -> tuple[str, ...]:
    """Which rungs may record this operation — ANY of them suffices.

    THE RELATION FOLLOWS THE OPERATION, NOT THE CALLER (owner ruling 2026-09-08). A compaction is a
    maintenance act whoever emits it, and an insert is a write whoever emits it, so keying on the
    subject would make the same act authorizable for one producer and not another.

    A maintenance operation accepts EITHER rung, never `can_maintain` alone: `create_index` is emitted
    by the catalog's own door as well as by the sweep, and that door gates it at the writer tier — so
    accepting only the maintainer rung would refuse the human who actually built the index. Adding the
    maintainer path is strictly additive; it takes nothing away from a writer. Same shape as the
    catalog's `_ALTERNATIVE_RUNGS`, and for the same reason.
    """
    return _MAINTENANCE_RELATIONS if operation in _MAINTENANCE_OPERATIONS else _WRITE_RELATIONS


async def _denied_objects(client: OpenFgaClient, *, user: str, relations: tuple[str, ...], names: list[str], object_type: str) -> list[str]:
    """The names the subject may NOT record against — denied under EVERY acceptable relation.

    Batched per relation rather than per object, and short-circuited: the common case is one relation
    and one round trip. A name still denied after the last relation is genuinely refused.
    """
    remaining = list(names)
    for relation in relations:
        if not remaining:
            break
        allowed = await fga.batch_check(client, user=user, relation=relation, objects=[f"{object_type}:{n}" for n in remaining])
        remaining = [n for n in remaining if not allowed.get(f"{object_type}:{n}")]
    return sorted(remaining)


async def _require_relation(relation: str, name: str, request: Request, settings: LineageSettings, token: Principal | None) -> None:
    """The one fail-closed authz ladder every per-``{name}`` gate shares (audit 2026-07-16: it had
    grown four near-copies). No-op when FGA is off; unwired client → 503; unauthenticated → 401;
    deny → 403; an OpenFGA outage inside ``fga.check`` → 503, never allow."""
    if not settings.fga_enabled:
        return
    client = getattr(request.app.state, "fga", None)
    if client is None:
        raise ServiceUnavailableError("authorization service is not available")
    if token is None:
        raise UnauthenticatedError("authentication required")
    obj = f"{settings.fga_object_type}:{name}"
    if not await fga.check(client, user=token.sub, relation=relation, obj=obj):
        log.info("access_denied", extra={"sub": token.sub, "relation": relation, "object": obj})
        raise PermissionDeniedError(f"{relation} required on {obj}")


async def require_estate_observer(request: Request, settings: LineageSettings, token: Principal | None) -> None:
    """Gate a WHOLE-ESTATE read on ``can_observe_events`` at the configured root object.

    The sibling of :func:`_require_relation` and deliberately not a call into it: that helper composes
    ``<fga_object_type>:<name>``, which on this service is always ``table:``, and an estate privilege is
    not a relation of any one table. It is checked on `settings.fga_root_object` verbatim — the same
    object `POST /v1/projects` and `POST /v1/stores` gate on, so "may observe the estate" means one
    thing everywhere rather than one thing per service.

    The same fail-closed ladder as every other gate here: FGA off → no-op; unwired client → 503;
    unauthenticated → 401; deny → 403; an OpenFGA outage inside `check` → 503, never allow.
    """
    if not settings.fga_enabled:
        return
    client = getattr(request.app.state, "fga", None)
    if client is None:
        raise ServiceUnavailableError("authorization service is not available")
    if token is None:
        raise UnauthenticatedError("authentication required")
    obj = settings.fga_root_object
    if not await fga.check(client, user=token.sub, relation="can_observe_events", obj=obj):
        log.info("access_denied", extra={"sub": token.sub, "relation": "can_observe_events", "object": obj})
        raise PermissionDeniedError(f"can_observe_events required on {obj}")


async def require_metadata_access(name: str, request: Request, settings: SettingsDep, token: CurrentToken) -> None:
    """Gate a dataset read on OpenFGA ``can_get_metadata`` for ``<type>:<name>`` — the same metadata-read
    permission the catalog requires to describe that table."""
    await _require_relation("can_get_metadata", name, request, settings, token)


async def require_write_access(name: str, request: Request, settings: SettingsDep, token: CurrentToken) -> None:
    """Gate a governance write (tags/description, #49) on OpenFGA ``can_write_data`` for ``<type>:<name>``
    — the same writer rung :func:`enforce_output_authz` requires of a producer recording provenance:
    curating a dataset's governance metadata is a write on that dataset."""
    await _require_relation("can_write_data", name, request, settings, token)


async def audit_read(name: str, settings: SettingsDep, token: CurrentToken, repository: RepositoryDep) -> None:
    """Record a read-audit row (WHO read this dataset) on a gated read — best-effort, off by default (#6).

    Complements the write provenance in the AGE graph with an access log. No-op when ``read_audit_enabled``
    is off or the request is unauthenticated (no subject to attribute). An audit-write failure is logged,
    never raised — auditing must never break a read. Runs AFTER :func:`require_metadata_access`, so only an
    authorized read is logged.
    """
    if not settings.read_audit_enabled or token is None:
        return
    try:
        await repository.record_read(reader=token.sub, dataset=name)
    except Exception as exc:
        log.warning("read_audit_failed", extra={"reader": token.sub, "dataset": name, "error": str(exc)})


def enforce_author(event: RunEvent, token: Principal | None) -> None:
    """Bind the run author to the *verified* principal — never trust the request body.

    When the request is authenticated, overwrite the ``author`` run facet with the token
    subject so a producer cannot self-assert someone else's identity (provenance forgery).
    When OIDC is off (dev/tests) the body-supplied author is left as-is.
    """
    if token is not None:
        event.run.facets["author"] = {"name": token.sub, "sub": token.sub}


def is_external_source(namespace: str, name: str) -> bool:
    """Is this dataset OUTSIDE the governed estate — a raw source rather than a table we authorize?

    R23 draws the line the whole medallion rests on: the governed tiers are exactly bronze -> silver ->
    gold, and **raw is the external world, never a governed tier**. So a producer that honestly records
    where its data came from names something that has no catalog entry, no ``table:`` object, and
    therefore no tuple that could ever be written for it. Authorizing those inputs the same way as
    governed ones is not strict — it is unsatisfiable, and it refused every such producer permanently:

        403 "can_get_metadata required on inputs: bind86-src/run1"

    on the ingest plane's START event, whose input is the S3 prefix the run reads. The run landed its
    data, the terminal event was authorized fine, and the graph stayed empty because the run was never
    opened. Ten configuration causes were investigated before the service's own message was read.

    The discriminator has TWO forms and lives in `service_kit.lakehouse.naming`, because it is a
    convention this service and every producer must agree on — the `project_namespace` precedent. A
    store URI carrying a scheme (``s3://bucket``) is external by shape. A BARE identifier cannot be
    decided by shape at all, since that is exactly what a catalog namespace looks like (``bronze``,
    ``bind86-bronze``), so the bare external namespaces are DECLARED: ``file`` and ``lance``.

    Testing the scheme ALONE was a one-sided failure that looked like nothing. Of ingest's three source
    kinds only `s3-prefix` mints a scheme; `local-dir` mints ``file`` and `lance-append` mints
    ``lance``, so both were authorized as governed and both had their START event refused — while the
    terminal event, which carries no inputs, was accepted. The run reached the graph with half its
    provenance and every check that asks "does a run exist" passed (measured on the deployed estate
    2026-09-10: ``ingest_input_denied ... inputs=['/tmp/ingest-fixtures']``, 403). Pinned by
    `tests/unit/test_an_external_source_is_one_the_graph_does_not_authorize.py`, which reads the source
    registry itself so a fourth kind is covered without anyone remembering to add it.

    **This does not reopen the forgery hole it sits next to.** The guard exists so an authenticated
    reader cannot record "I read ``gold$catalog``" into the audit graph. A namespace is PART OF A
    DATASET'S IDENTITY, so a caller who fakes ``s3://anything`` as the namespace of ``gold$catalog``
    creates an *external* node named ``s3://anything / gold$catalog`` — a different node from the
    governed ``gold / gold$catalog``, connected to nothing that resolves. It cannot impersonate a
    governed dataset; it can only assert an edge to a node that is, correctly, outside the estate. What
    remains protected is exactly what the guard was written to protect: claiming to have read a
    GOVERNED dataset you cannot see.

    Outputs are deliberately NOT filtered this way. Writing is the direction that mutates the estate,
    and this plane never writes outside it — an output naming an external namespace is a producer
    claiming to have written the outside world, which is not a case to make permissive.
    """
    from service_kit.lakehouse.naming import is_external_source_namespace

    return is_external_source_namespace(namespace, name)


class _StampedAuthor:
    """The subject a BUS producer stamped on its own event, as a :class:`Principal`.

    NOT a verified identity, and the gate does not pretend otherwise — the bus door authenticates the
    SIDECAR (a shared app token), so nothing proves the stamp. What the gate changes is that the stamp
    becomes CONSEQUENTIAL: a forged subject must still hold the rung on every output, so a forger can
    only claim an identity that was already authorized to write those datasets — which bounds the
    forgery to producers that could have recorded it honestly. That is strictly stronger than the door
    it replaces, which accepted any stamp at all.
    """

    __slots__ = ("sub",)

    def __init__(self, sub: str) -> None:
        self.sub = sub


async def enforce_bus_authz(event: RunEvent, request: Request, settings: LineageSettings) -> None:
    """Output-scoped authz for a DAPR-DELIVERED event, as the subject the producer stamped (§ E2).

    The HTTP door proves WHO is ingesting (`enforce_author`) and then what they may write
    (`enforce_output_authz`); the bus door could do neither, because it has no principal — it is
    authenticated by the sidecar's shared credential and reads the author off the payload. So a
    producer holding that one token could record any provenance it liked about any dataset, including
    a `drop_table` operation on a table it has never seen, which the reconcile sweep then honours.

    THE SAME FUNCTION, not a second copy: this delegates to `enforce_output_authz` with a synthetic
    principal, so the run-mutation check and the output check apply identically at both doors. Two
    implementations of "may you record this" would drift, and the asymmetry between the doors is the
    defect this closes.

    AN UNAUTHORED EVENT IS REFUSED. That was not safe until the producers signed: measured 2026-09-08,
    664 of 5 644 runs carried no author and 518 of those were the sweep's, so this gate would have
    silently deleted the maintenance plane's entire provenance. Re-measured 2026-09-09 after the
    producer half landed: of the 69 runs in thirty hours, the only unauthored ones are e2e fixture rows
    written straight to the repository (which never reach this door) and two compactions from before
    that roll. `author_sub_from_payload` is the reader — `sub` only, never the `name` or `ownership`
    facets, because those are for attribution on a board and would let a producer authorize itself
    under someone else's display name.
    """
    if not settings.fga_enabled:
        return
    payload = event.model_dump(by_alias=True)
    subject = author_sub_from_payload(payload)
    try:
        if not subject:
            raise PermissionDeniedError("a bus-delivered run must carry a verified author sub to be authorized")
        await enforce_output_authz(event, request, settings, _StampedAuthor(subject), relations=relations_for_operation(event.operation))
    except PermissionDeniedError:
        if not await _is_replay(event, payload, request):
            raise
        log.info("lineage_replay_not_reauthorized", extra={"run": event.run.run_id, "event_type": event.event_type})


async def _is_replay(event: RunEvent, payload: dict[str, object], request: Request) -> bool:
    """Is this the SAME event the feed already holds — a redelivery rather than a new assertion?

    THE BUS RE-PRESENTS EVERY RETAINED EVENT ON EVERY RESTART. The consumer is ephemeral with
    `deliverPolicy: all`, which is the estate's recovery story ("the stream retains it and this
    consumer re-sees it on restart"), so an authorization gate meets the whole history again each time
    lineage rolls. Those runs are already in the graph — measured 2026-09-09, a full 2 161-message
    replay left the durable feed flat at 3 248 rows — so refusing them loses nothing and merely turns
    an ordinary restart into a burst of refusals indistinguishable from a producer under attack.

    CHECKED ONLY AFTER A DENIAL, so the authorized path pays no extra read: a replay of an event that
    still authorizes cleanly never reaches here.

    BYTE-IDENTICAL, never a key match. The graph MERGEs on run id and SETs `author`, `operation` and
    `event_type` last-wins, so exempting anything whose `(run_id, event_type)` merely EXISTS would let a
    forger rewrite an existing run's author, or restamp it as a `drop_table` the reconcile sweep then
    honours — precisely the mutation the run check above exists to refuse. An event that differs in any
    field is a new assertion and stays refused.
    """
    repository = getattr(request.app.state, "repository", None)
    if repository is None or not event.run.run_id:
        return False
    stored = await repository.recorded_event(event.run.run_id, event.event_type)
    return stored is not None and stored == payload


async def enforce_output_authz(
    event: RunEvent, request: Request, settings: LineageSettings, token: Principal | None, *, relations: tuple[str, ...] = _WRITE_RELATIONS
) -> None:
    """Output-scoped ingest authz: require the producer may WRITE every output dataset it claims.

    :func:`enforce_author` proves WHO is ingesting; this proves they were AUTHORIZED to write those outputs —
    a producer cannot record provenance for a table it has no ``can_write_data`` on (the same write
    permission the catalog requires to mutate that table). No-op when FGA is off. Fail-closed BEFORE any
    empty-set short-circuit: unwired client → 503, unauthenticated → 401, any non-writable output → 403.
    GOVERNED inputs are also authorized (``can_get_metadata``): you may only record READING a dataset you
    can see, so an authenticated reader can't forge READ-edge provenance for datasets outside its reach.
    External sources are exempt and :func:`is_external_source` says why — they are unauthorizable by
    construction, and requiring a tuple for one refused every honest producer of a raw ingest.
    """
    if not settings.fga_enabled:
        return
    # Fail-closed FIRST — BEFORE any empty-set short-circuit: an authenticated but unauthorized caller must
    # not be able to ingest a run (forging graph state) merely by declaring no outputs. (bug hunt 2026-07-13)
    client = getattr(request.app.state, "fga", None)
    if client is None:
        raise ServiceUnavailableError("authorization service is not available")
    if token is None:
        raise UnauthenticatedError("authentication required")
    object_type = settings.fga_object_type
    # MUTATING AN EXISTING RUN IS AUTHORIZED BY THE DATA THAT RUN WROTE.
    #
    # `MERGE (r:Run {run_id:$rid})` merges on the run id alone and last-event-wins-SETs `event_type`,
    # `author`, `producer`, `error_message` and `operation` — and run ids are PUBLIC (`/runs`,
    # `/events`, `/producers` serve them; the in-repo ones are deterministic UUID5 seeds). Both checks
    # below are gated on a non-empty list, so an event naming NO dataset used to be authorized having
    # checked nothing and still rewrote the run: set `operation` to `drop_table` and the reconcile
    # sweep skips that dataset forever, or set FAIL and serve an invented author to every viewer.
    #
    # KEYED ON DATA, NOT IDENTITY, and the difference is not cosmetic. Author equality is the obvious
    # rule and is wrong here: the HTTP door overwrites the author with the caller's verified sub
    # (`enforce_author`) while the BUS handler applies neither that nor this check — it is gated only
    # by the shared Dapr token. One run's events can therefore legitimately carry different authors
    # depending on the door, and an identity rule would refuse honest traffic. "May you write what this
    # run wrote" is stable across both.
    #
    # A run that does not exist yet returns NO outputs and is created freely — refusing that would
    # re-break ingest's START event, whose only input is an external prefix it cannot authorize.
    repository = getattr(request.app.state, "repository", None)
    if repository is not None and event.run.run_id:
        prior = await repository.run_output_names(event.run.run_id)
        if prior:
            refused = await _denied_objects(client, user=token.sub, relations=relations, names=prior, object_type=object_type)
            if refused:
                log.info("ingest_run_mutation_denied", extra={"sub": token.sub, "run_id": event.run.run_id, "outputs": refused})
                raise PermissionDeniedError(f"{' or '.join(relations)} required to amend run {event.run.run_id}: {', '.join(refused)}")
    outputs = [d.name for d in event.outputs if d.name]
    if outputs:
        denied = await _denied_objects(client, user=token.sub, relations=relations, names=outputs, object_type=object_type)
        if denied:
            log.info("ingest_denied", extra={"sub": token.sub, "relation": "|".join(relations), "outputs": denied})
            raise PermissionDeniedError(f"{' or '.join(relations)} required on outputs: {', '.join(denied)}")
    # Inputs: you may only RECORD reading a dataset you can SEE — else an authenticated reader (e.g. the
    # service-web read identity) could forge READ-edge provenance like "service-web read gold$catalog" into
    # the governed audit graph. `writer ⊇ reader` in model.fga, so stage runners (writers) and the trainer (reader)
    # still pass; only a claim to have read an unreachable dataset is refused. (bug hunt 2026-07-13)
    # THE AUTHZ SET MUST COVER THE WRITE SET, and it did not.
    #
    # `ingest_event` merges dataset vertices from THREE sources: `inputs`, `outputs`, and the column
    # upstreams inside `outputs[].facets.columnLineage.fields[*].inputFields[]`. Only the first two
    # were ever checked. So a caller holding `can_write_data` on one sandbox table could name a
    # GOVERNED table as a column upstream and have the ingest merge that vertex and assert a
    # `DERIVED_FROM` into it — the governed name never reaching a single FGA check.
    #
    # The column upstreams join the INPUT set rather than getting a check of their own: recording
    # "my column came from yours" is a claim to have READ your column, which is exactly what
    # `can_get_metadata` on the input side already governs. Same exemption too — an external upstream
    # has no `table:` object, and (since `vertex_name`) cannot collide with a governed vertex either.
    column_upstreams = {edge.name for out in event.outputs for edge in out.column_edges if edge.name and not is_external_source(edge.namespace, edge.name)}
    inputs = sorted({d.name for d in event.inputs if d.name and not is_external_source(d.namespace, d.name)} | column_upstreams)
    if inputs:
        objs = [f"{object_type}:{n}" for n in inputs]
        seen = await fga.batch_check(client, user=token.sub, relation="can_get_metadata", objects=objs)
        hidden = sorted(n for n in inputs if not seen.get(f"{object_type}:{n}"))
        if hidden:
            log.info(
                "ingest_input_denied",
                extra={"sub": token.sub, "relation": "can_get_metadata", "inputs": hidden},
            )
            raise PermissionDeniedError(f"can_get_metadata required on inputs: {', '.join(hidden)}")


class DatasetFilter:
    """Drop datasets the caller may not see from a lineage result (fail-closed).

    A neighbor/graph read returns *related* dataset names beyond the requested one; without
    filtering, one table grant would disclose the existence of every table in its lineage
    neighborhood. This batch-checks ``can_get_metadata`` (fail-closed: an OpenFGA outage →
    503) and returns only the authorized names — the lineage analogue of the catalog's
    ``list_objects``-filtered enumerations (``services/catalog/api/v1/endpoints/tables.py``). Pass-through
    when FGA is off (dev/tests).
    """

    def __init__(self, request: Request, settings: LineageSettings, token: Principal | None) -> None:
        self._request = request
        self._settings = settings
        self._token = token

    async def visible(self, names: list[str]) -> set[str]:
        """Return the subset of ``names`` the caller may read (``can_get_metadata``)."""
        if not self._settings.fga_enabled or not names:
            return set(names)
        client = getattr(self._request.app.state, "fga", None)
        if client is None:
            raise ServiceUnavailableError("authorization service is not available")
        if self._token is None:
            raise UnauthenticatedError("authentication required")
        object_type = self._settings.fga_object_type
        # Dedupe before the round trip: callers legitimately pass per-item dataset names (one per
        # column in the subgraph view), and a duplicate-laden batch payload is pure wasted checks.
        unique = list(dict.fromkeys(names))
        allowed = await fga.batch_check(
            client,
            user=self._token.sub,
            relation="can_get_metadata",
            objects=[f"{object_type}:{n}" for n in unique],
        )
        return {n for n in unique if allowed.get(f"{object_type}:{n}")}


def get_dataset_filter(request: Request, settings: SettingsDep, token: CurrentToken) -> DatasetFilter:
    """Build the per-request dataset-visibility filter."""
    return DatasetFilter(request, settings, token)


FilterDep = Annotated[DatasetFilter, Depends(get_dataset_filter)]


async def governed[T](
    datasets: DatasetFilter,
    fga_enabled: bool,
    items: list[T],
    refs: Callable[[T], set[str]],
) -> list[T]:
    """Drop items the caller may not see: any referencing a non-visible dataset, and — when FGA is on —
    any dataset-less item (it would otherwise pass vacuously, leaking run/author/error to a caller with
    no grants). Auth off → ``visible`` is pass-through, so nothing is dropped. (#22 audit)
    """
    referenced = {name for item in items for name in refs(item)}
    visible = await datasets.visible(list(referenced))
    kept: list[T] = []
    for item in items:
        names = refs(item)
        if fga_enabled and not names:
            continue
        if names <= visible:
            kept.append(item)
    return kept
