"""In-service authz for the lineage read + ingest endpoints.

The lineage service owns the commit feed and the edge list, so it must protect them itself (in-service: the
cluster route in front of it checks nothing). It mirrors the catalog's authz guard (``services/catalog/api/fga_deps.py``)
and asks every question through ``app.state.decider`` (:mod:`lakehouse.governed.decider`): deployed, that is the
catalog's decision door, and lineage holds no credential to the OpenFGA store at all ([[LH-442]]). The thin FastAPI
authz + filter dependencies are re-derived here because they bind to ``LineageSettings`` rather than the catalog's
``Settings``.

Three holes this closes (audit ``w8u4rc2tg``):

* **Reads** leaked the entire data estate. Each is now gated on OpenFGA ``can_get_metadata`` of
  ``table:<uuid>`` — the same permission the catalog requires to ``describe`` that table, on the object its grants are
  held by ([[LH-524]]).
* **Transitive disclosure.** A feed or edge read also returns *related* dataset names, so
  :class:`DatasetFilter` (and :func:`governed`) batch-check each and drop the ones the caller
  may not see — mirroring the catalog's per-item filtered enumerations.
* **Ingest** was unauthenticated and the run ``author`` was a producer-supplied facet, so
  provenance was forgeable. The author is taken from the verified token
  (:func:`enforce_author`) — the client-claimed facet is overwritten.

Default OFF (``RASK_FGA_ENABLED``), exactly like the catalog; production enables it.
Fail-closed when enabled-but-unwired (503, never silent allow).
"""

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Annotated, Any, Final, TypeGuard

from fastapi import Depends, FastAPI, Request
from lance_namespace import (
    PermissionDeniedError,
    ServiceUnavailableError,
    UnauthenticatedError,
)
from pydantic import BaseModel, ConfigDict

from lakehouse.events.lineage_events import AUTHOR_FACET, dataset_uuids
from lakehouse.events.openlineage import author_facet, lifecycle_state
from lakehouse.events.signing import VerifiedSignature
from lakehouse.governed import fga_ids
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit
from lakehouse.governed.decider import AccessDecider, TableDecision
from lakehouse.governed.principal import is_principal
from lakehouse.governed.settings import FgaSettings
from lakehouse.naming import identifier_location
from lineage.api.dependencies import BearerDep, NamesDep, SettingsDep, app_wire
from lineage.api.security import Caller, CurrentToken
from lineage.core.config import LineageSettings
from lineage.core.refusals import ForeignRunError, UnauthoredRunError, UngovernedOutputError
from lineage.core.sources import is_external_source
from lineage.models import MAINTENANCE_OPERATIONS, Dataset, DatasetEvent, RunEvent, author_sub_from_payload
from lineage.services.provenance import ProvenanceStore, feed_key
from lineage.services.signature import staged_signature, verify_arrived_event


log = logging.getLogger(__name__)

#: The producer this door names on the author facet it stamps, the module that stamps it.
_PRODUCER: Final = "https://github.com/AI-Riksarkivet/rask/tree/main/services/lineage/src/lineage/api/fga_deps.py"


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
#: `compact_table` messages on the LINEAGE stream were rejected `can_write_data required on outputs`, and none
#: was recorded.

#: The relation a data write demands. Named once so the two doors cannot drift on it.
_WRITE_RELATIONS: Final = ("can_write_data",)
_MAINTENANCE_RELATIONS: Final = ("can_write_data", "can_maintain")


def relations_for_operation(operation: str | None) -> tuple[str, ...]:
    """Which rungs may record this operation — ANY of them suffices.

    THE RELATION FOLLOWS THE OPERATION, NOT THE CALLER (owner ruling 2026-09-08). A compaction is a
    maintenance act whoever emits it, and an insert is a write whoever emits it, so keying on the
    subject would make the same act authorizable for one producer and not another.

    A maintenance operation accepts EITHER rung, never `can_maintain` alone: the catalog gates an index
    commit at the writer tier, so accepting only the maintainer rung would refuse the person who actually
    built the index. Adding the
    maintainer path is strictly additive; it takes nothing away from a writer.
    """
    return _MAINTENANCE_RELATIONS if operation in MAINTENANCE_OPERATIONS else _WRITE_RELATIONS


class Governed(BaseModel):
    """What an item names in the governed estate: the objects its tables' grants are held by, ``table:<uuid>``, and the
    governed tables it names without a uuid, which no tuple can name ([[LH-524]])."""

    model_config = ConfigDict(frozen=True)

    objects: frozenset[str] = frozenset()
    unresolved: frozenset[str] = frozenset()

    @property
    def names_nothing(self) -> bool:
        return not self.objects and not self.unresolved


def governed_refs(datasets: Iterable[object], *, column_upstreams: Iterable[str] = ()) -> Governed:
    """The :class:`Governed` of an event's ``datasets`` (models or payload dicts) and the column upstreams it names.

    An external source is the outside world (:func:`is_external_source`) and governs nothing. A governed dataset is the
    table its ``rask_lance`` facet's uuid names, and one without a uuid is unresolved. A column upstream is the table a
    dataset of the event by that name is, else unresolved.
    """
    objects: set[str] = set()
    unresolved: set[str] = set()
    by_name: dict[str, str] = {}
    for dataset in datasets:
        payload = dataset.model_dump(by_alias=True) if isinstance(dataset, BaseModel) else dataset
        if not isinstance(payload, dict) or not payload.get("name"):
            continue
        name, namespace = str(payload["name"]), str(payload.get("namespace") or "")
        if is_external_source(namespace, name):
            continue
        uuid = dataset_uuids(payload).table_uuid
        if uuid is None:
            unresolved.add(name)
        else:
            objects.add(fga_ids.table_object(uuid))
            by_name[name] = fga_ids.table_object(uuid)
    for name in column_upstreams:
        if name in by_name:
            objects.add(by_name[name])
        else:
            unresolved.add(name)
    return Governed(objects=frozenset(objects), unresolved=frozenset(unresolved - set(by_name)))


def output_refs(outputs: Iterable[object]) -> Governed:
    """The :class:`Governed` of an event's outputs (models or payload dicts), none exempted: an output is the table its
    ``rask_lance`` facet's uuid names, and one without a uuid, an external one included, is unresolved."""
    objects: set[str] = set()
    unresolved: set[str] = set()
    for dataset in outputs:
        payload = dataset.model_dump(by_alias=True) if isinstance(dataset, BaseModel) else dataset
        if not isinstance(payload, dict) or not payload.get("name"):
            continue
        name, namespace = str(payload["name"]), str(payload.get("namespace") or "")
        uuid = None if is_external_source(namespace, name) else dataset_uuids(payload).table_uuid
        if uuid is None:
            unresolved.add(name)
        else:
            objects.add(fga_ids.table_object(uuid))
    return Governed(objects=frozenset(objects), unresolved=frozenset(unresolved))


async def _denied_objects(decider: AccessDecider, *, user: str, relations: tuple[str, ...], objects: list[str]) -> list[str]:
    """The objects the subject may NOT record against — denied under EVERY acceptable relation.

    Batched per relation rather than per object, and short-circuited: the common case is one relation
    and one round trip. An object still denied after the last relation is genuinely refused.
    """
    remaining = list(objects)
    for relation in relations:
        if not remaining:
            break
        allowed = await decider.batch_check(user=user, relation=relation, objects=remaining)
        remaining = [obj for obj in remaining if not allowed.get(obj)]
    return sorted(remaining)


async def _none_are_governed(decider: AccessDecider, *, objects: list[str]) -> bool:
    """True only when EVERY denied object carries zero tuples — i.e. there is nothing to grant on.

    Decides which verdict a refusal earns ([[LH-166]]): a grant needs an object, so a denial naming only
    objects FGA has no record of can never be repaired, and parking it would keep a copy nothing can
    repair. One targeted read per denied object, on the refusal path only.

    FAIL-CLOSED TO THE REPAIRABLE ARM. An unreadable store answers False, so the caller parks: parking
    a repairable event costs a kept copy, taking an unreadable one deletes provenance, and only the
    second cannot be undone. Short-circuits on the first governed object — one is enough to make the
    refusal repairable.
    """
    try:
        if any((await decider.governed(objects)).values()):
            return False
    except Exception as exc:  # noqa: BLE001 — unreadable is not evidence of absence
        log.warning("ingest_governance_probe_unreadable", extra={"outputs": objects, "error": str(exc)})
        return False
    return True


def _denied(event: str, *, subject: str, relation: str, resources: Sequence[str]) -> None:
    """Record one refusal: who was refused on the audit trail, one record per object as the catalog's doors write them,
    and what was refused on the service's own log, which names no subject (the trace id joins the two)."""
    for resource in resources:
        audit(AuditAction.AUTHZ, AuditOutcome.DENY, relation=relation, subject=subject, resource=resource)
    log.info(event, extra={"relation": relation, "objects": list(resources)})


async def _require_relation(relation: str, obj: str | None, *, label: str, request: Request, settings: FgaSettings, token: Caller | None) -> None:
    """The one fail-closed authz ladder every per-``{name}`` gate shares (audit 2026-07-16: it had
    grown four near-copies). No-op when FGA is off; unwired client → 503; unauthenticated → 401;
    deny → 403, also for a name that resolved to no object (``obj`` is ``None``), named by ``label`` either way;
    an unanswerable decision → 503, never allow."""
    if not settings.fga_enabled:
        return
    decider = getattr(request.app.state, "decider", None)
    if decider is None:
        raise ServiceUnavailableError("authorization service is not available")
    if token is None:
        raise UnauthenticatedError("authentication required")
    if obj is None or not await decider.check(user=token.subject, relation=relation, obj=obj):
        _denied("access_denied", subject=token.subject, relation=relation, resources=[obj or label])
        raise PermissionDeniedError(f"{relation} required on {label}")


#: The estate rung that opens the unfiltered run feed (`model.fga`, `type estate`).
_EVENT_FEED_RELATION: Final = "can_read_event_feed"

#: The estate admin's verbs for the kept parks (`model.fga`, `type estate`): reading them, and replaying one.
_READ_PARKS_RELATION: Final = "can_read_parked_events"
_REPLAY_PARKS_RELATION: Final = "can_replay_parked_events"


async def require_event_feed_reader(request: Request, settings: FgaSettings, token: Caller | None) -> None:
    """Gate the unfiltered run feed on ``can_read_event_feed`` at the configured root object ([[CTL-021]]).

    The sibling of :func:`_require_relation` and deliberately not a call into it: that helper asks about one
    table, and an estate privilege is not a relation of any one table. It is checked on `settings.fga_root_object` verbatim, the object
    every estate privilege is checked on.

    ITS OWN RUNG rather than an estate-admin verb: the reconciler that reads this feed holds `event_reader` and
    nothing that mints a tenant or edits the authorization graph (`model.fga.yaml` pins both halves).

    The same fail-closed ladder as every other gate here: FGA off → no-op; unwired client → 503;
    unauthenticated → 401; deny → 403; an OpenFGA outage inside `check` → 503, never allow.
    """
    await _require_estate_rung(_EVENT_FEED_RELATION, request, settings, token=token)


async def require_parks_reader(request: Request, settings: SettingsDep, token: CurrentToken) -> None:
    """Gate the list of kept parks on ``can_read_parked_events`` at the root object ([[LH-148]]).

    The parks are every tenant's refused deliveries, so reading them is the estate admin's. The ladder is
    :func:`require_event_feed_reader`'s.
    """
    await _require_estate_rung(_READ_PARKS_RELATION, request, settings, token=token)


async def require_parks_replayer(request: Request, settings: SettingsDep, token: CurrentToken) -> None:
    """Gate the replay of a kept park on ``can_replay_parked_events`` at the root object ([[LH-148]]).

    The re-presentation of a kept park records provenance for whoever the event's author is, so it is no rung of the
    caller's own data, and a verb of its own beside the read: a replay writes to the feed. The ladder is
    :func:`require_event_feed_reader`'s.
    """
    await _require_estate_rung(_REPLAY_PARKS_RELATION, request, settings, token=token)


async def _require_estate_rung(relation: str, request: Request, settings: FgaSettings, *, token: Caller | None) -> None:
    if not settings.fga_enabled:
        return
    decider = getattr(request.app.state, "decider", None)
    if decider is None:
        raise ServiceUnavailableError("authorization service is not available")
    if token is None:
        raise UnauthenticatedError("authentication required")
    obj = settings.fga_root_object
    if not await decider.check(user=token.subject, relation=relation, obj=obj):
        _denied("access_denied", subject=token.subject, relation=relation, resources=[obj])
        raise PermissionDeniedError(f"{relation} required on {obj}")


async def require_metadata_access(name: str, *, request: Request, settings: SettingsDep, token: CurrentToken, names: NamesDep, bearer: BearerDep) -> None:
    """Gate a dataset read on OpenFGA ``can_get_metadata`` on the table ``name`` names now — the same metadata-read
    permission the catalog requires to describe that table, asked about the uuid the catalog resolves the name to as
    the caller ([[LH-524]]). A name the caller may not see resolves to nothing and is refused alike."""
    if not settings.fga_enabled:
        return
    uuid = (await names.table(name, bearer=bearer)).table_uuid
    await _require_relation(
        "can_get_metadata", fga_ids.table_object(uuid) if uuid else None, label=f"table:{name}", request=request, settings=settings, token=token
    )


def enforce_author(event: RunEvent | DatasetEvent, token: Caller | None) -> None:
    """Bind the author to the *verified* principal — never trust the request body.

    When the request is authenticated, overwrite the ``rask_author`` facet with the token subject so a
    producer cannot self-assert someone else's identity (provenance forgery). When OIDC is off
    (dev/tests) the body-supplied author is left as-is.

    THE FACET LIVES SOMEWHERE ELSE ON A STATIC EVENT, and stamping the wrong place is silent: a
    `DatasetEvent` has no run, so its author rides the DATASET's `rask_author` facet — which is exactly
    where :attr:`DatasetEvent.author` reads it, and so where the feed's `author` column and the
    targeting a notifier does take it from. Writing a run facet onto it would leave the
    verified subject unread and the create attributed to whatever the body claimed.
    """
    if token is None:
        return
    stamp = author_facet(_PRODUCER, token.subject)
    if isinstance(event, DatasetEvent):
        event.dataset.facets[AUTHOR_FACET] = stamp
    else:
        event.run.facets[AUTHOR_FACET] = stamp


class _StampedAuthor:
    """The subject a BUS producer stamped on its own event, as a :class:`Caller`.

    WHAT AUTHENTICATES THE STAMP IS THE SIGNATURE, not the bus. The bus's permission table admits a TRANSPORT (a
    bus user that may publish on the topic), never a producer, so on an estate that does not enforce signing nothing proves the stamp, and what the gate changes
    is that the stamp becomes CONSEQUENTIAL: a forged subject must still hold the rung on every output, so a
    forger can only claim an identity that was already authorized to write those datasets, which bounds the
    forgery to producers that could have recorded it honestly. Where signing is enforced the stamp is also
    proven: the signer IS the author, or a delegator declared acting for them (`verify_arrived_event`). An event the
    catalog attests (`_attested`) is then recorded on that signature, and any other event's subject is authorized
    exactly as below.
    """

    __slots__ = ("subject",)

    def __init__(self, subject: str) -> None:
        self.subject = subject


async def enforce_bus_authz(event: RunEvent | DatasetEvent, app: FastAPI, settings: LineageSettings, *, arrived: Mapping[str, Any]) -> frozenset[str]:
    """Output-scoped authz for an event delivered from the BUS, as the subject the producer stamped (§ E2).

    Returns what :func:`enforce_output_authz` returns, the tables the event's datasets alias, for the feed row's scope;
    empty for an event admitted without that check (a drop the catalog attests, a redelivery, authorization off).

    The HTTP door proves WHO is ingesting (`enforce_author`) and then what they may write
    (`enforce_output_authz`); the bus door can do neither by itself, because it has no principal — a delivery
    arrives from the stream, and the author is read off the payload. So without this gate a producer the bus lets
    publish could record any provenance it liked about any dataset, including a `drop_table` operation on a table it
    has never seen.

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
    apply the one rule: the bus lane, the outbox drain and the estate admin's replay of a parked delivery. So do
    they all record an announcement the catalog attests on its signature (`_attested`, [[LH-551]]):
    a drop here, a write with no grant re-derived in `enforce_output_authz`.
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
    verified = await verify_arrived_event(arrived, app, settings)
    if _is_an_attested_drop(event, verified, settings):
        log.info("lineage_catalog_drop_admitted", extra={"dataset": event.dataset.name, "operation": event.operation})
        return frozenset()
    if not settings.fga_enabled:
        return frozenset()
    # The DUMP, deliberately, for everything below: `_is_replay` compares byte-for-byte against the row
    # the feed already holds, which was itself built from the model.
    payload = event.model_dump(by_alias=True)
    subject = author_sub_from_payload(payload)
    try:
        if not subject:
            raise UnauthoredRunError("a bus-delivered run must carry a verified author sub to be authorized")
        # A stamp that names no principal is no author: nothing could be authorized as it ([[LH-063]]).
        if not is_principal(subject):
            raise UnauthoredRunError(f"a bus-delivered run's author {subject[:128]!r} is not a principal (<idp-id>~<subject>)")
        return await enforce_output_authz(
            event,
            app,
            settings,
            token=_StampedAuthor(subject),
            relations=relations_for_operation(event.operation),
            signed_by=verified,
        )
    except PermissionDeniedError:
        if not await _is_replay(event, payload, app):
            raise
        log.info("lineage_replay_not_reauthorized", extra={"run": event.run_id, "event_type": event.feed_event_type})
        return frozenset()


def _is_an_attested_drop(event: RunEvent | DatasetEvent, verified: VerifiedSignature | None, settings: LineageSettings) -> TypeGuard[DatasetEvent]:
    """A DROP the catalog attests (:func:`_attested`) — admitted on that signature, never on the author's grants (owner ruling
    2026-09-26).

    THE GRANTS ARE GONE BY DESIGN. The catalog emits `drop_table` / `deregister_table` and then revokes the
    table's tuples in the same request, so a delivery that arrives after the revoke — every bus delivery
    and every outbox drain — asks FGA about a table nobody may write, and the drop could never be recorded.
    The catalog enforced `can_drop` before it mutated anything; its verified signature is that
    attestation, so the catalog's own signed event is a fact here rather than a grant to ask again.
    """
    return isinstance(event, DatasetEvent) and _attested(verified, settings) and lifecycle_state(event.operation or "") == "DROP"


def _attested(verified: VerifiedSignature | None, settings: LineageSettings) -> bool:
    """Whether the catalog attests the event ``verified`` was verified on: one of the chart's delegators signed it, as this
    call verified (owner rulings 2026-09-26, 2026-10-20 and 2026-11-01, [[LH-551]], [[LH-550]]).

    WHAT THE SIGNATURE ATTESTS. The catalog checks the caller's rung on every table a write or a drop names, its outputs and
    the inputs its run declares, before it commits, records the caller it authorized the commit for, and signs the
    announcement for that caller. A grant revoked between the commit and the recording (a delivery the bus held, a lineage
    outage, the offboarding door revoking every tuple of a principal) changes nothing the catalog attests, so re-deriving it
    here would erase a true record. The commit reconciler's re-announcement of a commit whose own announcement was lost is
    signed for the author the catalog recorded authorizing it, never for the ``rask.author`` a version carries, and a
    version it recorded no author for is announced for nobody (ADR 0187), so it attests what the original would have.

    ONLY A SIGNATURE THIS CALL VERIFIED, AND ONLY AS ONE OF THE CHART'S DELEGATORS. The catalog is the delegator set
    (`LINEAGE_DELEGATORS`, derived from the identities the chart mints keys for), so the name trusted here cannot differ
    from the name the catalog signs under. A `rask_signature` merely PRESENT proves nothing and is not read here: where
    signing is not enforced ``verified`` is None, and an unsigned event, or one signed by any other identity, keeps the
    full check. The signing key's reach bounds the attestation: only the catalog's own login to the store may read its
    private key, since its role's policy is the only one that grants it (ADR 0169).
    """
    return verified is not None and verified.identity in settings.signing.delegators


async def _is_replay(event: RunEvent | DatasetEvent, payload: dict[str, object], app: FastAPI) -> bool:
    """Is this the SAME event the feed already holds — a redelivery rather than a new assertion?

    A REPLAY RE-PRESENTS EVERY RETAINED EVENT. A `lineage-ingest` consumer the chart declares afresh begins at
    the stream's first message, which is the estate's recovery story, so an authorization gate meets the
    whole history again each time. Those events are already in the
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
    # THE FEED'S OWN ROW KEY, never `run_id`: a static change has no run id and is stored under the id derived from its
    # content and who vouches for it (`feed_key`), so only its own row can say whether this is that row again.
    if not event.feed_id:
        return False
    stored = await app_wire(app, "provenance", ProvenanceStore).recorded_event(feed_key(event))
    return stored is not None and stored == payload


class Aliases(BaseModel):
    """The governed tables an event's datasets are named as besides their own names ([[LH-553]]), as the catalog answers.

    ``objects`` are the tables each dataset (by its index in the list asked about) names by a storage location or
    another identifier (:attr:`Dataset.other_identifiers`); ``located`` the indices of the datasets whose own name is a location a governed table holds,
    each of which is that table; ``hidden`` are, by the same index, the identifiers naming a governed table by name the
    caller may not see, which no grant asked about the dataset reaches; ``tables`` are the uuids of every table
    ``objects`` names."""

    model_config = ConfigDict(frozen=True)

    objects: dict[int, frozenset[str]] = {}
    located: frozenset[int] = frozenset()
    hidden: dict[int, frozenset[str]] = {}
    tables: frozenset[str] = frozenset()

    @property
    def hidden_names(self) -> frozenset[str]:
        """Every identifier of ``hidden``, whichever dataset listed it."""
        return frozenset[str]().union(*self.hidden.values())


async def resolve_aliases(decider: AccessDecider, datasets: list[Dataset], *, user: str) -> Aliases:
    """The governed tables ``datasets`` are named as by a storage location or another identifier: a ``symlinks`` entry or a
    ``lifecycleStateChange``'s ``previousIdentifier``.

    A GRAPH BACKEND JOINS A DATASET BY EVERY IDENTIFIER IT CARRIES. The catalog names each table's location in its events'
    ``symlinks`` facet, so an engine naming the table by that location (``s3://<bucket>`` and the key) is recorded on the
    governed table in Marquez (`DatasetSymlinkDao`, 0.51.1), and so is an identifier a producer lists in its own
    ``symlinks``. Exempting a location as the outside world would let a producer record reading, or writing, a table it
    may not see on that table's node. So the catalog is asked which table each location holds (``locate``): one a table
    holds is that table, and one no table holds stays the outside world. A ``symlinks`` identifier naming a governed table
    by its name is resolved as the caller (``check_tables`` on ``can_get_metadata``), and one the caller may not see is
    ``hidden``. A ``lifecycleStateChange``'s ``previousIdentifier`` is one more identifier its dataset is named by
    (:attr:`Dataset.other_identifiers`), since a backend following a rename joins that node to the dataset's, and is
    resolved the same way: a governed name no table holds any longer, which is every true rename's, is ``hidden``.

    Never asked about an event the catalog attests (:func:`_attested`), on any door: the catalog names its tables by uuid
    and vouches for the names it uses, so its announcement of a rename is recorded, listed and replayed without its
    previous name being resolved (:func:`enforce_output_authz`, :func:`governed_with_aliases`).
    """
    locations: dict[str, list[int]] = {}
    names: dict[str, list[int]] = {}
    own: dict[int, str] = {}
    for index, dataset in enumerate(datasets):
        if is_external_source(dataset.namespace, dataset.name) and (location := identifier_location(dataset.namespace, dataset.name)) is not None:
            own[index] = location
            locations.setdefault(location, []).append(index)
        for namespace, name in dataset.other_identifiers:
            if not is_external_source(namespace, name):
                names.setdefault(name, []).append(index)
            elif (location := identifier_location(namespace, name)) is not None:
                locations.setdefault(location, []).append(index)
    objects: dict[int, set[str]] = {}
    tables: set[str] = set()
    held: set[str] = set()
    for location, table_uuid in (await _located(decider, locations)).items():
        if table_uuid is None:
            continue
        held.add(location)
        tables.add(table_uuid)
        for index in locations[location]:
            objects.setdefault(index, set()).add(fga_ids.table_object(table_uuid))
    hidden: dict[int, set[str]] = {}
    for name, decision in (await _names_decided(decider, names, user=user)).items():
        if (table_uuid := decision.table_uuid) is None:
            for index in names[name]:
                hidden.setdefault(index, set()).add(name)
            continue
        tables.add(table_uuid)
        for index in names[name]:
            objects.setdefault(index, set()).add(fga_ids.table_object(table_uuid))
    return Aliases(
        objects={index: frozenset(found) for index, found in objects.items()},
        located=frozenset(index for index, location in own.items() if location in held),
        hidden={index: frozenset(unseen) for index, unseen in hidden.items()},
        tables=frozenset(tables),
    )


async def _located(decider: AccessDecider, locations: Mapping[str, list[int]]) -> dict[str, str | None]:
    """The uuid of the table holding each of ``locations``, as the catalog answers it; nothing asked for none."""
    if not locations:
        return {}
    return await decider.locate(sorted(locations))


async def _names_decided(decider: AccessDecider, names: Mapping[str, list[int]], *, user: str) -> dict[str, TableDecision]:
    """The catalog's ``can_get_metadata`` decision for ``user`` on each table ``names`` names; nothing asked for none."""
    if not names:
        return {}
    return await decider.check_tables(user=user, relation="can_get_metadata", tables=sorted(names))


def _upstreams_named_by_a_location(datasets: Iterable[Dataset]) -> list[Dataset]:
    """The column upstreams ``datasets`` name outside the catalog's own names, each as a dataset: one named by a location
    a governed table holds is read as that table, as an input is (:func:`resolve_aliases`)."""
    return [Dataset(namespace=up.namespace, name=up.name) for ds in datasets for up in ds.column_upstreams if is_external_source(up.namespace, up.name)]


def upstreams_named_as_tables(datasets: Iterable[Dataset]) -> set[str]:
    """The column upstreams ``datasets`` name by a governed table's own name.

    Passed every dataset of an event, inputs as well as outputs: a ``columnLineage`` facet on either is stored and
    forwarded with the event, so each names a table its writer claims to have read.
    """
    return {up.name for ds in datasets for up in ds.column_upstreams if not is_external_source(up.namespace, up.name)}


async def governed_with_aliases(decider: AccessDecider, events: Sequence[RunEvent | DatasetEvent], *, user: str, attested: Sequence[bool]) -> list[Governed]:
    """What each of ``events`` names in the governed estate for the reader ``user``, under every identifier it uses.

    For an event no door has scoped yet, a staged one ([[LH-598]]), by the rule `enforce_output_authz` admits one on:
    beside the tables its datasets carry the uuids of, it names every table an input, an output or a column upstream is
    named as by a storage location or another identifier (:func:`resolve_aliases`), and a name the reader may not see,
    another identifier's or a column upstream's, is unresolved, so the event is hidden. An event the catalog attests
    (``attested``, by position, :func:`_attested` over `staged_signature`) names its tables by uuid alone, as at
    the doors that record it. Asked in one question about every location and one about every other name, whatever the
    number of events.
    """
    named_by = [
        [] if vouched else [*event.inputs, *event.outputs, *_upstreams_named_by_a_location([*event.inputs, *event.outputs])]
        for event, vouched in zip(events, attested, strict=True)
    ]
    owners = [position for position, datasets in enumerate(named_by) for _ in datasets]
    aliases = await resolve_aliases(decider, [*datasets for datasets in named_by], user=user)
    objects: list[set[str]] = [set() for _ in events]
    unseen: list[set[str]] = [set() for _ in events]
    for index, owner in enumerate(owners):
        objects[owner] |= aliases.objects.get(index, frozenset())
        unseen[owner] |= aliases.hidden.get(index, frozenset())
    scoped: list[Governed] = []
    for event, aliased, hidden in zip(events, objects, unseen, strict=True):
        named = governed_refs([*event.inputs, *event.outputs], column_upstreams=upstreams_named_as_tables([*event.inputs, *event.outputs]))
        scoped.append(Governed(objects=named.objects | aliased, unresolved=named.unresolved | hidden))
    return scoped


async def enforce_output_authz(
    event: RunEvent | DatasetEvent,
    app: FastAPI,
    settings: LineageSettings,
    *,
    token: Caller | None,
    relations: tuple[str, ...] = _WRITE_RELATIONS,
    signed_by: VerifiedSignature | None = None,
    names_vouched_by: VerifiedSignature | None = None,
) -> frozenset[str]:
    """Output-scoped ingest authz: require the producer may WRITE every output dataset it claims.

    :func:`enforce_author` proves WHO is ingesting; this proves they were AUTHORIZED to write those outputs —
    a producer cannot record provenance for a table it has no ``can_write_data`` on (the same write
    permission the catalog requires to mutate that table). No-op when FGA is off. Fail-closed BEFORE any
    empty-set short-circuit: unwired client → 503, unauthenticated → 401, any non-writable output → 403.
    GOVERNED inputs are also authorized (``can_read_data``, the rung the catalog asks of a commit's inputs): you may
    only record READING a dataset you may read, so a caller who merely sees a table ([[LH-570]]) cannot forge
    READ-edge provenance from it.
    External sources are exempt and :func:`is_external_source` says why — they are unauthorizable by
    construction, and requiring a tuple for one refused every honest producer of a raw ingest.

    Every governed dataset is asked about as the table its ``rask_lance`` facet's uuid names, ``table:<uuid>`` ([[LH-524]]):
    the catalog stamps it on every event it signs, and the HTTP door stamps what the caller's name resolves to. A
    rename keeps the uuid, so a rename's register names its source by the uuid the destination holds, and its grants
    with it. A governed dataset carrying no uuid names no table the producer may see, so it is refused.

    ``signed_by`` is the signature the bus door verified (`verify_arrived_event`), never a caller's claim. An event the
    catalog attests with it (:func:`_attested`) has neither its outputs' rung nor its inputs' re-derived ([[LH-551]]): the
    catalog checked both before it committed the write, and a grant revoked since, or a table dropped since, takes nothing
    back. Everything else still holds for it: the run-author check, and the refusal of an output or input that names no
    uuid, since neither depends on when the event is recorded.

    ``names_vouched_by`` is a staged event's own signature (`staged_signature`) where ``token`` is not its producer:
    an operator replaying it. One the catalog attests vouches for the names its event uses, so they are not resolved, and
    for nothing about the caller, whose rungs on the event's datasets are asked as for any other event.

    Returns:
        The uuids of the governed tables the event's datasets are named as by a storage location or another
        identifier (a ``symlinks`` entry or a ``previousIdentifier``), as the catalog answered (:func:`resolve_aliases`): what the event's feed row is scoped by beside the
        uuids its datasets carry, so a reader of the feed is shown the row only if it may see those tables too
        ([[LH-598]]). Empty with authorization off, and for an event the catalog attests or whose names it vouches for
        (``names_vouched_by``), which names its tables by uuid.
    """
    if not settings.fga_enabled:
        return frozenset()
    # Fail-closed FIRST — BEFORE any empty-set short-circuit: an authenticated but unauthorized caller must
    # not be able to record a run merely by declaring no outputs. (bug hunt 2026-07-13)
    decider = getattr(app.state, "decider", None)
    if decider is None:
        raise ServiceUnavailableError("authorization service is not available")
    if token is None:
        raise UnauthenticatedError("authentication required")
    await _require_run_author(app, event, subject=token.subject)
    attested = _attested(signed_by, settings)
    # WHAT THE EVENT NAMES BY A LOCATION OR ANOTHER IDENTIFIER ([[LH-553]]): the catalog attests its own, so they are not re-derived.
    vouched = attested or _attested(names_vouched_by, settings)
    written = Aliases() if vouched else await resolve_aliases(decider, list(event.outputs), user=token.subject)
    # A column upstream named by a location is read as its table too: a graph backend joins it there as it joins an input.
    sources = [*event.inputs, *_upstreams_named_by_a_location([*event.inputs, *event.outputs])]
    read_as = Aliases() if vouched else await resolve_aliases(decider, sources, user=token.subject)
    outputs = _require_governed_outputs(event, subject=token.subject, relations=relations, written=written)
    if outputs.objects and not attested:
        await _require_output_rung(decider, event, subject=token.subject, relations=relations, outputs=outputs, written=written)
    read = _governed_inputs(event, read_as)
    hidden = set(read.unresolved)
    if read.objects and not attested:
        hidden |= await _unseen_inputs(decider, subject=token.subject, read=read, sources=sources, read_as=read_as)
    if hidden:
        _denied("ingest_input_denied", subject=token.subject, relation="can_read_data", resources=sorted(hidden))
        raise PermissionDeniedError(f"can_read_data required on inputs: {', '.join(sorted(hidden))}")
    return written.tables | read_as.tables


async def _require_run_author(app: FastAPI, event: RunEvent | DatasetEvent, *, subject: str) -> None:
    """Refuse an event extending a recorded run that ``subject`` did not found (``ForeignRunError``, 403)."""
    # AN EXISTING RUN IS EXTENDED ONLY BY ITS AUTHOR ([[LIN-5]]).
    #
    # An event naming a recorded run is recorded as that run's next state, and run ids are public (every feed row
    # serves one), so a caller who could name any run could restate it — its state, its operation, its error — for
    # every reader of the feed. Authorizing the extension by the data the run wrote left two holes: a run that
    # recorded no output had nothing to authorize against, and a run that did could be extended by anyone else who
    # may write those outputs. The author is the verified subject of the run's first recorded event: the HTTP door
    # stamps the caller's token subject (`enforce_author`), and the bus, the outbox and a kept delivery's replay present the subject a
    # listed signer verified, so one rule holds at every door. A run the feed does not hold yet has no author, and
    # its first event founds it. The feed is a wire the lifespan always sets: a lineage without it faults (`app_wire`),
    # never skips the check.
    if event.run_id:
        author = await app_wire(app, "provenance", ProvenanceStore).run_author(event.run_id)
        if author is not None and author != subject:
            _denied("ingest_run_mutation_denied", subject=subject, relation="author", resources=[f"run:{event.run_id}"])
            raise ForeignRunError(f"run {event.run_id} was founded by another subject; only its author may extend it")


def _require_governed_outputs(event: RunEvent | DatasetEvent, *, subject: str, relations: tuple[str, ...], written: Aliases) -> Governed:
    """The tables ``event``'s outputs name, by uuid or as ``written`` resolved them; refuses an output naming none."""
    # OUTPUTS ARE NEVER EXEMPTED: writing is the direction that mutates the estate, so an output naming an external
    # namespace is a producer claiming to have written the outside world, and it names no object, as an output carrying
    # no uuid names none. No grant can name either, so the refusal is unrepairable ([[LH-166]]). An output named by the
    # location of a governed table is that table, and is decided on its grants.
    outputs = output_refs(event.outputs)
    unnamed = frozenset(name for index, out in enumerate(event.outputs) if index not in written.located for name in output_refs([out]).unresolved)
    outputs = Governed(objects=outputs.objects.union(*written.objects.values()), unresolved=unnamed | written.hidden_names)
    if outputs.unresolved:
        reason = f"{' or '.join(relations)} required on outputs: {', '.join(sorted(outputs.unresolved))}"
        _denied("ingest_denied", subject=subject, relation="|".join(relations), resources=sorted(outputs.unresolved))
        raise UngovernedOutputError(reason)
    return outputs


async def _require_output_rung(
    decider: AccessDecider, event: RunEvent | DatasetEvent, *, subject: str, relations: tuple[str, ...], outputs: Governed, written: Aliases
) -> None:
    """Refuse ``event`` unless ``subject`` holds one of ``relations`` on every table its ``outputs`` are."""
    denied = await _denied_objects(decider, user=subject, relations=relations, objects=sorted(outputs.objects))
    if denied:
        _denied("ingest_denied", subject=subject, relation="|".join(relations), resources=denied)
        reason = f"{' or '.join(relations)} required on outputs: {', '.join(_names_of(event.outputs, denied, written))}"
        if not await _none_are_governed(decider, objects=denied):
            raise PermissionDeniedError(reason)
        raise UngovernedOutputError(reason)


def _governed_inputs(event: RunEvent | DatasetEvent, read_as: Aliases) -> Governed:
    """Every governed dataset ``event`` claims to have read, its inputs and column upstreams, as tables or unresolved."""
    # Inputs: you may only RECORD reading a dataset you can SEE — else an authenticated reader (e.g. the
    # service-web read identity) could forge provenance like "service-web read gold$catalog" into the
    # feed and the edge list. `writer ⊇ reader` in model.fga, so writers and readers
    # still pass; only a claim to have read an unreachable dataset is refused. (bug hunt 2026-07-13)
    # THE AUTHZ SET MUST COVER EVERY DATASET THE EVENT ASSERTS READING.
    #
    # This check covers an event's `inputs`, its `outputs` and the column upstreams the `columnLineage` facet of any of
    # them names (`Dataset.column_upstreams`); the other identifiers a dataset is named by (`Dataset.other_identifiers`: its `symlinks` and its `previousIdentifier`) are resolved with the names. The feed stores the whole event and the
    # forwarder sends it to the configured graph backend, so a column upstream naming a GOVERNED table is
    # a claim to have read it whether or not lineage keeps a column plane of its own.
    #
    # The column upstreams join the INPUT set rather than getting a check of their own: recording
    # "my column came from yours" is a claim to have READ your column, which is exactly what
    # `can_read_data` on the input side already governs. Same exemption too — an external upstream
    # has no `table:` object, and (since `lineage_name`) cannot be recorded as a governed table either. A column
    # upstream is the table a dataset of the event by that name is.
    read = governed_refs(event.inputs, column_upstreams=upstreams_named_as_tables([*event.inputs, *event.outputs]))
    return Governed(objects=read.objects.union(*read_as.objects.values()), unresolved=read.unresolved | read_as.hidden_names)


async def _unseen_inputs(decider: AccessDecider, *, subject: str, read: Governed, sources: list[Dataset], read_as: Aliases) -> set[str]:
    """The names of the tables in ``read`` that ``subject`` may not read, as ``sources`` name them."""
    seen = await decider.batch_check(user=subject, relation="can_read_data", objects=sorted(read.objects))
    return set(_names_of(sources, [obj for obj in read.objects if not seen.get(obj)], read_as))


def _names_of(datasets: Iterable[Dataset], objects: Iterable[str], aliases: Aliases | None = None) -> list[str]:
    """The names ``datasets`` give the tables ``objects`` are, for a refusal: a caller reads names, never uuids. A dataset
    named by a location or a symlink of one of them (``aliases``) is named too, so its refusal names what the caller sent."""
    wanted = set(objects)
    found = (aliases or Aliases()).objects
    return sorted(
        {
            d.name
            for index, d in enumerate(datasets)
            if ((uuid := dataset_uuids(d.model_dump(by_alias=True)).table_uuid) and fga_ids.table_object(uuid) in wanted)
            or found.get(index, frozenset()) & wanted
        }
    )


class DatasetFilter:
    """Drop datasets the caller may not see from a lineage result (fail-closed).

    A feed row or an edge read names *related* datasets beyond the one asked about; without
    filtering, one table grant would disclose the existence of every table in its lineage
    neighborhood. This batch-checks ``can_get_metadata`` on the tables' objects (``table:<uuid>``; fail-closed: an
    OpenFGA outage → 503) and returns only the authorized ones — the lineage analogue of the catalog's per-item
    filtered enumerations. Pass-through when FGA is off (dev/tests).
    """

    def __init__(self, request: Request, settings: LineageSettings, token: Caller | None) -> None:
        self._request = request
        self._settings = settings
        self._token = token
        #: This request's decisions, so a page governed a chunk at a time asks about each object once ([[LH-590]]).
        self._decided: dict[str, bool] = {}

    def undecided(self, objects: Iterable[str]) -> int:
        """How many of ``objects`` :meth:`visible` would ask the authorization service about: none while FGA is off, and
        none that is no object id, which the check answers without asking."""
        if not self._settings.fga_enabled:
            return 0
        return len({obj for obj in objects if obj not in self._decided and fga_ids.is_object_id(obj)})

    async def visible(self, objects: Iterable[str]) -> set[str]:
        """Return the subset of ``objects`` the caller may read (``can_get_metadata``)."""
        unique = list(dict.fromkeys(objects))
        if not self._settings.fga_enabled or not unique:
            return set(unique)
        undecided = [obj for obj in unique if obj not in self._decided]
        if undecided:
            decider, subject = self._asking()
            allowed = await decider.batch_check(user=subject, relation="can_get_metadata", objects=undecided)
            self._decided.update({obj: bool(allowed.get(obj)) for obj in undecided})
        return {obj for obj in unique if self._decided[obj]}

    async def governed[T](self, items: list[T], *, refs: Callable[[T], Governed]) -> list[T]:
        """Drop items the caller may not see: any naming a table the caller may not read, or a governed table recorded
        without a uuid (no tuple can name it), and — when FGA is on — any item naming no table at all (it would otherwise
        pass vacuously, leaking run/author/error to a caller with no grants). Auth off → nothing is dropped. (#22 audit)
        """
        if not self._settings.fga_enabled:
            return list(items)
        named = [(item, refs(item)) for item in items]
        visible = await self.visible({obj for _, governs in named for obj in governs.objects})
        return [item for item, governs in named if not governs.names_nothing and not governs.unresolved and governs.objects <= visible]

    async def attestation(self, arrived: Mapping[str, Any]) -> VerifiedSignature | None:
        """The signature a staged event's bytes carry, verified as the doors that record it verify it, or ``None`` for one
        that does not verify now (`staged_signature`)."""
        return await staged_signature(arrived, self._request.app, self._settings)

    async def scope_unrecorded(self, events: Sequence[RunEvent | DatasetEvent], signatures: Sequence[VerifiedSignature | None]) -> list[Governed]:
        """Ask the authorization service what each of ``events`` names for this caller: for an event no door has recorded,
        whose aliases the catalog answers now (:func:`governed_with_aliases`), unless the catalog attests it by its
        signature in ``signatures`` (:meth:`attestation`, by position). With FGA off nothing is asked."""
        if not self._settings.fga_enabled:
            return [governed_refs([*event.inputs, *event.outputs]) for event in events]
        decider, subject = self._asking()
        attested = [_attested(signature, self._settings) for signature in signatures]
        return await governed_with_aliases(decider, events, user=subject, attested=attested)

    def _asking(self) -> tuple[AccessDecider, str]:
        """The authorization service and the subject it is asked as; fail-closed where either is missing."""
        decider = getattr(self._request.app.state, "decider", None)
        if decider is None:
            raise ServiceUnavailableError("authorization service is not available")
        if self._token is None:
            raise UnauthenticatedError("authentication required")
        return decider, self._token.subject


def get_dataset_filter(request: Request, settings: SettingsDep, token: CurrentToken) -> DatasetFilter:
    """Build the per-request dataset-visibility filter."""
    return DatasetFilter(request, settings, token)


FilterDep = Annotated[DatasetFilter, Depends(get_dataset_filter)]
