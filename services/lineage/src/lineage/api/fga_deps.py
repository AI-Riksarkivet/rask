"""In-service authz for the lineage read + ingest endpoints.

The lineage service owns the commit feed and the edge list, so it must protect them itself (in-service: the
cluster route in front of it checks nothing). It mirrors the catalog's authz guard (``services/catalog/api/fga_deps.py``)
and asks every question through ``app.state.decider`` (:mod:`service_kit.governed.decider`): deployed, that is the
catalog's decision door, and lineage holds no credential to the OpenFGA store at all ([[LH-442]]). The thin FastAPI
authz + filter dependencies are re-derived here because they bind to ``LineageSettings`` rather than the catalog's
``Settings``.

Three holes this closes (audit ``w8u4rc2tg``):

* **Reads** leaked the entire data estate. Each is now gated on OpenFGA ``can_get_metadata`` of
  ``table:<dataset>`` — the same permission the catalog requires to ``describe`` that table.
* **Transitive disclosure.** A feed or edge read also returns *related* dataset names, so
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
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Final, TypeGuard

from fastapi import Depends, Request
from lance_namespace import (
    PermissionDeniedError,
    ServiceUnavailableError,
    UnauthenticatedError,
)

from lineage.api.dependencies import SettingsDep
from lineage.api.security import CurrentToken, Principal
from lineage.core.config import LineageSettings
from lineage.models import MAINTENANCE_OPERATIONS, DatasetEvent, ForeignRunError, RunEvent, UnauthoredRunError, UngovernedOutputError, author_sub_from_payload
from lineage.services.signature import verify_arrived_event
from lineage_kit.signing import VerifiedSignature
from service_kit.governed.decider import AccessDecider
from service_kit.openlineage import lifecycle_state


log = logging.getLogger(__name__)


#: Operations that are MAINTENANCE rather than data writes. `model.fga` separates the two rungs in
#: both directions on purpose — a maintainer rewrites HOW a dataset is stored, a writer changes WHAT it
#: says — and the owner ruling (2026-09-08, zero trust) is that the sweep gets the former and never the
#: latter, so authorizing its provenance on `can_write_data` would demand a grant the estate refuses to
#: issue. Three operations are maintenance and none changes a row: the sweep's `expire_versions`
#: (`maintenance/core/lineage_emit.py`), and the catalog's `compact_table` and `create_index`
#: (`catalog/core/lineage_emit.py`), which name a bring-your-own job's compaction and index commits through
#: `version/create`. The catalog gates an index commit at the writer tier, which is why the rule below
#: accepts EITHER rung rather than swapping one for the other.
#:
#: An operation missing here falls to the data-write rule and demands `can_write_data`, the rung the owner's
#: zero-trust ruling refuses the sweep, so its event is refused terminally. Measured 2026-09-11: 9 of 9
#: `compact_table` messages on the LINEAGE stream were rejected `can_write_data required on outputs`, none
#: was recorded, and each is refused again on every restart replay. Nothing downstream notices: this
#: path does not dead-letter a refusal, and the outbox has already dropped its staged copy.

#: The relation a data write demands. Named once so the two doors cannot drift on it.
_WRITE_RELATIONS: Final = ("can_write_data",)
_MAINTENANCE_RELATIONS: Final = ("can_write_data", "can_maintain")

#: The tag gate's operations, recorded on the rungs of the catalog doors that perform them (ADR 0156 C1, ADR 0161).
#: `publish_table` moves the `published` tag and writes no row: the publish door moves it at `can_update_tag` and a
#: validator's approval at `can_promote`, and the writer rung, which can do neither, does not record one.
#: `restore_table` commits a restored version: the spec's restore door at `can_restore` (an owner, who writes) and a
#: validator's rejection of a held version at `can_promote`. Measured on the estate 2026-10-15 ([[LH-478]]): with the
#: writer rung demanded for both, a validator's approval was refused `can_write_data required on outputs` and
#: dead-lettered, so the commit feed a pipeline follows never showed the publication.
_GATE_RELATIONS: Final[Mapping[str, tuple[str, ...]]] = {
    "publish_table": ("can_update_tag", "can_promote"),
    "restore_table": ("can_write_data", "can_promote"),
}


def relations_for_operation(operation: str | None) -> tuple[str, ...]:
    """Which rungs may record this operation — ANY of them suffices.

    THE RELATION FOLLOWS THE OPERATION, NOT THE CALLER (owner ruling 2026-09-08). A compaction is a
    maintenance act whoever emits it, and an insert is a write whoever emits it, so keying on the
    subject would make the same act authorizable for one producer and not another.

    A maintenance operation accepts EITHER rung, never `can_maintain` alone: the catalog gates an index
    commit at the writer tier, so accepting only the maintainer rung would refuse the person who actually
    built the index. Adding the
    maintainer path is strictly additive; it takes nothing away from a writer. Same shape as the
    catalog's `_ALTERNATIVE_RUNGS`, and for the same reason.

    The tag gate's publication and restore accept the rungs of the doors that perform them (`_GATE_RELATIONS`).
    """
    if (gate := _GATE_RELATIONS.get(operation or "")) is not None:
        return gate
    return _MAINTENANCE_RELATIONS if operation in MAINTENANCE_OPERATIONS else _WRITE_RELATIONS


async def _denied_objects(decider: AccessDecider, *, user: str, relations: tuple[str, ...], names: list[str], object_type: str) -> list[str]:
    """The names the subject may NOT record against — denied under EVERY acceptable relation.

    Batched per relation rather than per object, and short-circuited: the common case is one relation
    and one round trip. A name still denied after the last relation is genuinely refused, and so is one
    whose ``<type>:<name>`` is no object id, which the decider answers False unasked.
    """
    remaining = list(names)
    for relation in relations:
        if not remaining:
            break
        allowed = await decider.batch_check(user=user, relation=relation, objects=[f"{object_type}:{n}" for n in remaining])
        remaining = [n for n in remaining if not allowed.get(f"{object_type}:{n}")]
    return sorted(remaining)


async def _none_are_governed(decider: AccessDecider, *, names: list[str], object_type: str) -> bool:
    """True only when EVERY denied name carries zero tuples — i.e. there is nothing to grant on.

    Decides which ack a refusal earns ([[LH-166]]): a grant needs an object, so a denial naming only
    objects FGA has no record of can never be repaired, and parking it appends a dead-letter copy per
    restart forever. One targeted read per denied name, on the refusal path only — `governed_objects`
    pages the whole store and is a per-SWEEP cost, not a per-event one.

    FAIL-CLOSED TO THE REPAIRABLE ARM. An unreadable store answers False, so the caller parks: parking
    a repairable event costs a duplicate, acking an unreadable one deletes provenance, and only the
    second cannot be undone. Short-circuits on the first governed name — one is enough to make the
    refusal repairable.
    """
    try:
        if any((await decider.governed([f"{object_type}:{name}" for name in names])).values()):
            return False
    except Exception as exc:  # noqa: BLE001 — unreadable is not evidence of absence
        log.warning("ingest_governance_probe_unreadable", extra={"outputs": names, "error": str(exc)})
        return False
    return True


async def _require_relation(relation: str, name: str, request: Request, settings: LineageSettings, token: Principal | None) -> None:
    """The one fail-closed authz ladder every per-``{name}`` gate shares (audit 2026-07-16: it had
    grown four near-copies). No-op when FGA is off; unwired client → 503; unauthenticated → 401;
    deny → 403; an unanswerable decision → 503, never allow."""
    if not settings.fga_enabled:
        return
    decider = getattr(request.app.state, "decider", None)
    if decider is None:
        raise ServiceUnavailableError("authorization service is not available")
    if token is None:
        raise UnauthenticatedError("authentication required")
    obj = f"{settings.fga_object_type}:{name}"
    if not await decider.check(user=token.sub, relation=relation, obj=obj):
        log.info("access_denied", extra={"sub": token.sub, "relation": relation, "object": obj})
        raise PermissionDeniedError(f"{relation} required on {obj}")


#: The estate rung that opens the unfiltered run feed (`model.fga`, `type estate`).
_EVENT_FEED_RELATION: Final = "can_read_event_feed"


async def require_event_feed_reader(request: Request, settings: LineageSettings, token: Principal | None) -> None:
    """Gate the unfiltered run feed on ``can_read_event_feed`` at the configured root object ([[CTL-021]]).

    The sibling of :func:`_require_relation` and deliberately not a call into it: that helper composes
    ``<fga_object_type>:<name>``, which on this service is always ``table:``, and an estate privilege is
    not a relation of any one table. It is checked on `settings.fga_root_object` verbatim, the object
    every estate privilege is checked on.

    ITS OWN RUNG rather than ``can_observe_events``, which also mints tenants, edits the authorization
    graph and registers stores: the reconciler that reads this feed holds `event_reader` and nothing that
    reaches those doors (`model.fga.yaml` pins both halves).

    The same fail-closed ladder as every other gate here: FGA off → no-op; unwired client → 503;
    unauthenticated → 401; deny → 403; an OpenFGA outage inside `check` → 503, never allow.
    """
    if not settings.fga_enabled:
        return
    decider = getattr(request.app.state, "decider", None)
    if decider is None:
        raise ServiceUnavailableError("authorization service is not available")
    if token is None:
        raise UnauthenticatedError("authentication required")
    obj = settings.fga_root_object
    if not await decider.check(user=token.sub, relation=_EVENT_FEED_RELATION, obj=obj):
        log.info("access_denied", extra={"sub": token.sub, "relation": _EVENT_FEED_RELATION, "object": obj})
        raise PermissionDeniedError(f"{_EVENT_FEED_RELATION} required on {obj}")


async def require_metadata_access(name: str, request: Request, settings: SettingsDep, token: CurrentToken) -> None:
    """Gate a dataset read on OpenFGA ``can_get_metadata`` for ``<type>:<name>`` — the same metadata-read
    permission the catalog requires to describe that table."""
    await _require_relation("can_get_metadata", name, request, settings, token)


def enforce_author(event: RunEvent | DatasetEvent, token: Principal | None) -> None:
    """Bind the author to the *verified* principal — never trust the request body.

    When the request is authenticated, overwrite the ``author`` facet with the token subject so a
    producer cannot self-assert someone else's identity (provenance forgery). When OIDC is off
    (dev/tests) the body-supplied author is left as-is.

    THE FACET LIVES SOMEWHERE ELSE ON A STATIC EVENT, and stamping the wrong place is silent: a
    `DatasetEvent` has no run, so its author rides the DATASET's `author` facet — which is exactly
    where :attr:`DatasetEvent.author` reads it, and so where the feed's `author` column and the
    notifications plane's targeting take it from. Writing a run facet onto it would leave the
    verified subject unread and the create attributed to whatever the body claimed.
    """
    if token is None:
        return
    stamp = {"name": token.sub, "sub": token.sub}
    if isinstance(event, DatasetEvent):
        event.dataset.facets["author"] = stamp
    else:
        event.run.facets["author"] = stamp


def is_external_source(namespace: str, name: str) -> bool:
    """Is this dataset OUTSIDE the governed estate — a raw source rather than a table we authorize?

    R23 draws the line the whole medallion rests on: the governed tiers are exactly bronze -> silver ->
    gold, and **raw is the external world, never a governed tier**. So a producer that honestly records
    where its data came from names something that has no catalog entry, no ``table:`` object, and
    therefore no tuple that could ever be written for it. Authorizing those inputs the same way as
    governed ones is not strict — it is unsatisfiable, and it refused every such producer permanently:

        403 "can_get_metadata required on inputs: bind86-src/run1"

    on the ingest plane's START event, whose input is the S3 prefix the run reads. The run landed its
    data, the terminal event was authorized fine, and the run was never recorded as opened. Ten
    configuration causes were investigated before the service's own message was read.

    The discriminator has TWO forms and lives in `service_kit.lakehouse.naming`, because it is a
    convention this service and every producer must agree on — the `project_namespace` precedent. A
    store URI carrying a scheme (``s3://bucket``) is external by shape. A BARE identifier cannot be
    decided by shape at all, since that is exactly what a catalog namespace looks like (``bronze``,
    ``bind86-bronze``), so the bare external namespaces are DECLARED: ``file`` and ``lance``.

    Testing the scheme ALONE was a one-sided failure that looked like nothing. Of ingest's three source
    kinds only `s3-prefix` mints a scheme; `local-dir` mints ``file`` and `lance-append` mints
    ``lance``, so both were authorized as governed and both had their START event refused — while the
    terminal event, which carries no inputs, was accepted. The run was recorded with half its
    provenance and every check that asks "does a run exist" passed (measured on the deployed estate
    2026-09-10: ``ingest_input_denied ... inputs=['/tmp/ingest-fixtures']``, 403).

    **This does not reopen the forgery hole it sits next to.** The guard exists so an authenticated
    reader cannot record "I read ``gold$catalog``" into the feed. A namespace is PART OF A DATASET'S
    IDENTITY (`Dataset.lineage_name`), so a caller who fakes ``s3://anything`` as the namespace of
    ``gold$catalog`` names an *external* dataset ``s3://anything/gold$catalog`` — a different identity from
    the governed ``gold$catalog``, on no governed table's edges. It cannot impersonate a governed dataset; it
    can only assert an edge from a source that is, correctly, outside the estate. What remains protected is
    exactly what the guard was written to protect: claiming to have read a GOVERNED dataset you cannot see.

    Outputs are deliberately NOT filtered this way. Writing is the direction that mutates the estate,
    and this plane never writes outside it — an output naming an external namespace is a producer
    claiming to have written the outside world, which is not a case to make permissive.
    """
    from service_kit.lakehouse.naming import is_external_source_namespace

    return is_external_source_namespace(namespace, name)


class _StampedAuthor:
    """The subject a BUS producer stamped on its own event, as a :class:`Principal`.

    WHAT AUTHENTICATES THE STAMP IS THE SIGNATURE, not the bus. The door authenticates the SIDECAR (a shared
    app token), so on an estate that does not enforce signing nothing proves the stamp, and what the gate changes
    is that the stamp becomes CONSEQUENTIAL: a forged subject must still hold the rung on every output, so a
    forger can only claim an identity that was already authorized to write those datasets, which bounds the
    forgery to producers that could have recorded it honestly. Where signing is enforced the stamp is also
    proven: the signer IS the author, or a delegator declared acting for them (`verify_arrived_event`), and the
    subject is then authorized exactly as below.
    """

    __slots__ = ("sub",)

    def __init__(self, sub: str) -> None:
        self.sub = sub


async def enforce_bus_authz(event: RunEvent | DatasetEvent, request: Request, settings: LineageSettings, arrived: Mapping[str, Any]) -> None:
    """Output-scoped authz for a DAPR-DELIVERED event, as the subject the producer stamped (§ E2).

    The HTTP door proves WHO is ingesting (`enforce_author`) and then what they may write
    (`enforce_output_authz`); the bus door could do neither, because it has no principal — it is
    authenticated by the sidecar's shared credential and reads the author off the payload. So a
    producer holding that one token could record any provenance it liked about any dataset, including
    a `drop_table` operation on a table it has never seen.

    THE SAME FUNCTION, not a second copy: this delegates to `enforce_output_authz` with a synthetic
    principal, so the run-author check and the output check apply identically at both doors. Two
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

    WHERE SIGNING IS ENFORCED THE EVENT MUST VERIFY FIRST ([[LH-064]]): `verify_arrived_event` refuses an event no
    listed signer's published key verifies (`UnverifiedEventError`) and answers an unreadable key list as an outage
    (`ServiceUnavailableError`). Every door that records a bus or staged event passes through here, so all four
    apply the one rule: the bus subscription, the outbox drain, the operator's DLQ replay and the parking route's
    re-ingest.
    """
    # OVER `arrived`, NEVER over a re-serialisation of the parsed model, and BEFORE `fga_enabled` is
    # consulted. Two separate rules, both load-bearing:
    #
    # THE BYTES THAT WERE SIGNED are the only document a signature covers. `model_dump` materialises
    # every field the model defaults — `RunEvent`'s three facet bags are `default_factory=dict`, so a
    # dump grows `job.facets = {}` and `outputs[].facets = {}` the producer never sent. The canonical
    # body then differs from the signed one and the honest producer is refused: an ack that discards the
    # event on the bus, and on the outbox drain the deletion of the event's only durable copy.
    #
    # A SIGNATURE IS AUTHENTICATION. `fga_enabled` decides whether OpenFGA is consulted about what a
    # subject may write; it says nothing about whether the producer is who it claims to be, so gating
    # the one on the other would let an estate with auth on and FGA off verify nothing at all.
    verified = await verify_arrived_event(arrived, request, settings)
    if _is_a_delegators_drop(event, verified, settings):
        log.info("lineage_catalog_drop_admitted", extra={"dataset": event.dataset.name, "operation": event.operation})
        return
    if not settings.fga_enabled:
        return
    # The DUMP, deliberately, for everything below: `_is_replay` compares byte-for-byte against the row
    # the feed already holds, which was itself built from the model.
    payload = event.model_dump(by_alias=True)
    subject = author_sub_from_payload(payload)
    try:
        if not subject:
            raise UnauthoredRunError("a bus-delivered run must carry a verified author sub to be authorized")
        await enforce_output_authz(event, request, settings, _StampedAuthor(subject), relations=relations_for_operation(event.operation))
    except PermissionDeniedError:
        if not await _is_replay(event, payload, request):
            raise
        log.info("lineage_replay_not_reauthorized", extra={"run": event.run_id, "event_type": event.feed_event_type})


def _is_a_delegators_drop(event: RunEvent | DatasetEvent, verified: VerifiedSignature | None, settings: LineageSettings) -> TypeGuard[DatasetEvent]:
    """A DROP the catalog signed — admitted on that signature, never on the author's grants (owner ruling 2026-09-26).

    THE GRANTS ARE GONE BY DESIGN. The catalog emits `drop_table` / `deregister_table` and then revokes the
    table's tuples in the same request, so a delivery that arrives after the revoke — every bus delivery
    and every outbox drain — asks FGA about a table nobody may write, and the drop could never be recorded.
    The catalog enforced `can_drop` before it mutated anything; its verified signature is that
    attestation, the way Lakekeeper treats the catalog's own events as facts rather than re-asking a grant.

    ONLY A SIGNATURE THIS CALL VERIFIED, AND ONLY AS ONE OF THE CHART'S DELEGATORS. The catalog is the delegator
    set (`LINEAGE_DELEGATORS`, derived from the identities the chart mints keys for), so the name the shortcut
    trusts cannot differ from the name the catalog signs under. A `rask_signature` merely PRESENT proves nothing
    and is not read here: where signing is not enforced `verified` is None and the shortcut is off, and an unsigned
    drop, or one signed by any other identity, keeps the full check. The signing key's reach bounds this
    attestation: custody of the keys is the Dapr-scope claim until XC-079.
    """
    return (
        isinstance(event, DatasetEvent)
        and verified is not None
        and verified.identity in settings.signing.delegators
        and lifecycle_state(event.operation or "") == "DROP"
    )


async def _is_replay(event: RunEvent | DatasetEvent, payload: dict[str, object], request: Request) -> bool:
    """Is this the SAME event the feed already holds — a redelivery rather than a new assertion?

    A REPLAY RE-PRESENTS EVERY RETAINED EVENT. Lineage's `deliverPolicy: all` consumer starts at the
    stream's first message on its first attach and on a rebuild, which is the estate's recovery story,
    so an authorization gate meets the whole history again each time. Those events are already in the
    feed — measured 2026-09-09, a full 2 161-message replay left the durable feed flat at 3 248 rows —
    so refusing them loses nothing and merely turns a replay into a burst of refusals
    indistinguishable from a producer under attack.

    CHECKED ONLY AFTER A DENIAL, so the authorized path pays no extra read: a replay of an event that
    still authorizes cleanly never reaches here.

    BYTE-IDENTICAL, never a key match. Exempting anything whose `(run_id, event_type)` merely EXISTS would
    admit a forged event that shares a recorded one's key — another subject's extension of a run, a different
    operation — on the strength of the honest row beside it, which is precisely what the run-author check
    exists to refuse. An event that differs in any field is a new assertion and stays refused.
    """
    # THE FEED'S OWN KEY, never `run_id`: a static change has no run id and is stored under the id
    # derived from its content, so looking it up by run id answered "not a replay" for every DatasetEvent.
    provenance = getattr(request.app.state, "provenance", None)
    if provenance is None or not event.feed_id:
        return False
    stored = await provenance.recorded_event(event.feed_id, event.feed_event_type)
    return stored is not None and stored == payload


async def enforce_output_authz(
    event: RunEvent | DatasetEvent, request: Request, settings: LineageSettings, token: Principal | None, *, relations: tuple[str, ...] = _WRITE_RELATIONS
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
    # not be able to record a run merely by declaring no outputs. (bug hunt 2026-07-13)
    decider = getattr(request.app.state, "decider", None)
    if decider is None:
        raise ServiceUnavailableError("authorization service is not available")
    if token is None:
        raise UnauthenticatedError("authentication required")
    object_type = settings.fga_object_type
    # AN EXISTING RUN IS EXTENDED ONLY BY ITS AUTHOR ([[LIN-5]]).
    #
    # An event naming a recorded run is recorded as that run's next state, and run ids are public (every feed row
    # serves one), so a caller who could name any run could restate it — its state, its operation, its error — for
    # every reader of the feed. Authorizing the extension by the data the run wrote left two holes: a run that
    # recorded no output had nothing to authorize against, and a run that did could be extended by anyone else who
    # may write those outputs. The author is the verified subject of the run's first recorded event: the HTTP door
    # stamps the caller's token subject (`enforce_author`), and the bus, the outbox and the DLQ present the subject a
    # listed signer verified, so one rule holds at every door. A run the feed does not hold yet has no author, and
    # its first event founds it.
    provenance = getattr(request.app.state, "provenance", None)
    if provenance is not None and event.run_id:
        author = await provenance.run_author(event.run_id)
        if author is not None and author != token.sub:
            log.info("ingest_run_mutation_denied", extra={"sub": token.sub, "run_id": event.run_id, "author": author})
            raise ForeignRunError(f"run {event.run_id} was founded by another subject; only its author may extend it")
    outputs = [d.name for d in event.outputs if d.name]
    if outputs:
        denied = await _denied_objects(decider, user=token.sub, relations=relations, names=outputs, object_type=object_type)
        if denied:
            log.info("ingest_denied", extra={"sub": token.sub, "relation": "|".join(relations), "outputs": denied})
            reason = f"{' or '.join(relations)} required on outputs: {', '.join(denied)}"
            if await _none_are_governed(decider, names=denied, object_type=object_type):
                raise UngovernedOutputError(reason)
            raise PermissionDeniedError(reason)
    # Inputs: you may only RECORD reading a dataset you can SEE — else an authenticated reader (e.g. the
    # service-web read identity) could forge provenance like "service-web read gold$catalog" into the
    # feed and the edge list. `writer ⊇ reader` in model.fga, so writers and readers
    # still pass; only a claim to have read an unreachable dataset is refused. (bug hunt 2026-07-13)
    # THE AUTHZ SET MUST COVER EVERY DATASET THE EVENT ASSERTS READING.
    #
    # An event names datasets in THREE places: `inputs`, `outputs`, and the column upstreams inside
    # `outputs[].facets.columnLineage.fields[*].inputFields[]`. The feed stores the whole event and the
    # forwarder sends it to the configured graph backend, so a column upstream naming a GOVERNED table is
    # a claim to have read it whether or not lineage keeps a column plane of its own.
    #
    # The column upstreams join the INPUT set rather than getting a check of their own: recording
    # "my column came from yours" is a claim to have READ your column, which is exactly what
    # `can_get_metadata` on the input side already governs. Same exemption too — an external upstream
    # has no `table:` object, and (since `lineage_name`) cannot be recorded as a governed table either.
    column_upstreams = {up.name for out in event.outputs for up in out.column_upstreams if not is_external_source(up.namespace, up.name)}
    inputs = sorted({d.name for d in event.inputs if d.name and not is_external_source(d.namespace, d.name)} | column_upstreams)
    if inputs:
        objs = [f"{object_type}:{n}" for n in inputs]
        seen = await decider.batch_check(user=token.sub, relation="can_get_metadata", objects=objs)
        hidden = sorted(n for n in inputs if not seen.get(f"{object_type}:{n}"))
        if hidden:
            log.info(
                "ingest_input_denied",
                extra={"sub": token.sub, "relation": "can_get_metadata", "inputs": hidden},
            )
            raise PermissionDeniedError(f"can_get_metadata required on inputs: {', '.join(hidden)}")


class DatasetFilter:
    """Drop datasets the caller may not see from a lineage result (fail-closed).

    A feed row or an edge read names *related* datasets beyond the one asked about; without
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
        decider = getattr(self._request.app.state, "decider", None)
        if decider is None:
            raise ServiceUnavailableError("authorization service is not available")
        if self._token is None:
            raise UnauthenticatedError("authentication required")
        object_type = self._settings.fga_object_type
        # Dedupe before the round trip: callers legitimately pass per-item dataset names, and a
        # duplicate-laden batch payload is pure wasted checks. A
        # name whose `<type>:<name>` is no object id (an external `s3://` source, say) is answered False
        # by the decider without being sent; other external names are sent and answered not granted.
        unique = list(dict.fromkeys(names))
        allowed = await decider.batch_check(
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
