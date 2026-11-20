"""Reads of the store: check and batch check, list objects and users, expand and the derivation walked to its end, the
tuple changelog, tuple pages and whole reads, and the grant census.

Every read sends the condition clock (:func:`condition_context`) and asks past OpenFGA's caches
(:func:`_fresh_read_options`), and no tuple may carry that clock (:func:`reject_stored_clock`), which the writes run
too.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable
from datetime import UTC, datetime
from typing import Any, Final, NamedTuple

from lance_namespace import InvalidInputError, ServiceUnavailableError
from openfga_sdk import OpenFgaClient
from openfga_sdk.client.models import (
    ClientBatchCheckItem,
    ClientBatchCheckRequest,
    ClientCheckRequest,
    ClientExpandRequest,
    ClientListObjectsRequest,
    ClientTuple,
)
from openfga_sdk.client.models.list_users_request import ClientListUsersRequest
from openfga_sdk.client.models.read_changes_request import ClientReadChangesRequest
from openfga_sdk.constants import CLIENT_MAX_BATCH_SIZE
from openfga_sdk.models.consistency_preference import ConsistencyPreference
from openfga_sdk.models.fga_object import FgaObject
from openfga_sdk.models.read_request_tuple_key import ReadRequestTupleKey
from openfga_sdk.models.user_type_filter import UserTypeFilter
from pydantic import BaseModel, Field

from lakehouse.governed.fga_ids import is_object_id, subject_of, user_object
from lakehouse.governed.fga_resilience import DEFAULT_RETRY, RetryPolicy, UnansweredBatchItems, guarded


log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


#: The condition parameter that is the CHECK's clock: `condition_context` supplies it on every read, and
#: :func:`reject_stored_clock` keeps it off every tuple OpenFGA will evaluate.
CLOCK_PARAMETER: Final = "current_time"


def _fresh_read_options(**options: int | str) -> dict[str, int | str | dict[str, int | str]]:
    """The options every read of the store sends: ``HIGHER_CONSISTENCY``, plus the call's own ``options``.

    OpenFGA's check, iterator and list-objects caches are the platform's to turn on for scale, and a read they answer
    may predate the last write: measured on OpenFGA v1.18.3 with them on, ListObjects kept listing a table for its
    revoked reader, and a Check whose context repeats kept allowing it ([[LH-488]]). ``HIGHER_CONSISTENCY`` skips
    every one of those caches (`openfga run --help`), so a revoke is answered by the next read, whichever request it
    comes from. With the caches off, as the bundled OpenFGA runs, it costs nothing.

    A new dict per call, since the SDK mutates the dict it is handed (:func:`read_tuples`).
    """
    return {"consistency": ConsistencyPreference.HIGHER_CONSISTENCY, **options}


#: The checks one OpenFGA batch request carries: the SDK's own batch size, which :func:`batch_check` sends by.
CHECKS_PER_BATCH: Final = CLIENT_MAX_BATCH_SIZE
#: The most batch requests one :func:`batch_check` sends at once: a decision question's whole 1,000 objects
#: (`decider.MAX_OBJECTS_PER_QUESTION`), where the SDK's default of 10 sent them in two waves ([[LH-590]]). It holds
#: for every caller, the catalog's filtered listings among them: a check of up to 1,000 objects is one wave of at most
#: twenty requests on OpenFGA, rather than two of ten.
_MOST_PARALLEL_BATCHES: Final = 20


def _parallel_batches(checks: int) -> int:
    """How many batch requests to send at once so ``checks`` checks go in one wave, up to :data:`_MOST_PARALLEL_BATCHES`.

    Measured on the dev estate 2026-11-13: lineage governing a 2,000-row page named 668 objects, which the catalog's
    decision door sent as 14 batches, ten at a time, in 397 ms.
    """
    return max(1, min(_MOST_PARALLEL_BATCHES, -(-checks // CHECKS_PER_BATCH)))


def reject_stored_clock(tuples: Iterable[ClientTuple]) -> None:
    """Refuse any tuple whose condition context carries :data:`CLOCK_PARAMETER`.

    OpenFGA evaluates a parameter stored on the tuple over the one a check sends (pinned in
    `auth/model.fga.yaml`), so a stored clock freezes "now" inside a grant's window and the grant never
    lapses. :func:`write_tuples` and :func:`check`'s ``contextual_tuples`` run this themselves, so no
    caller can forget it; :func:`delete_tuples` does not, because a delete evaluates nothing and a
    pinned grant must stay revocable.

    Raises:
        InvalidInputError: naming the first offending tuple.
    """
    for t in tuples:
        condition = t.condition
        if condition is not None and CLOCK_PARAMETER in (condition.context or {}):
            raise InvalidInputError(
                f"condition {condition.name!r} on {t.object}#{t.relation}@{t.user} cannot carry {CLOCK_PARAMETER} on the tuple: it is the "
                "check's clock, supplied per request, and a stored value would override every check's and keep the grant from ever expiring"
            )


def condition_context(context: dict[str, Any] | None = None) -> dict[str, Any]:
    """The context a CONDITION is evaluated against, with the clock supplied by default.

    ONE helper for the whole estate, because the failure it prevents is silent. The model's
    ``non_expired_grant`` takes ``current_time`` from the CALLER (the tuple carries only
    ``grant_time`` + ``grant_duration``), and OpenFGA cannot evaluate a CEL expression whose
    parameters are missing — so a read that omits the clock does not get "no opinion", it gets a
    **DENY**, and a perfectly valid time-boxed grant reads as already expired. Every read below
    therefore defaults the clock in rather than trusting each call site to remember it, which is
    exactly the class of bug F2 records: the wrappers could not even accept a context, so no caller
    could have passed one.

    An explicit value WINS on every key — an audit or access-simulation that asks "was this allowed
    last Tuesday" passes its own ``current_time`` and gets it. Keys the matched condition does not
    declare are simply unused, so defaulting the clock is safe for unconditional tuples too.

    RFC 3339 with an explicit offset is what the CEL ``timestamp`` type parses; a naive
    ``datetime.now()`` would serialise without one and fail the parse at the server.
    """
    return {CLOCK_PARAMETER: datetime.now(UTC).isoformat(), **(context or {})}


async def check(
    client: OpenFgaClient,
    *,
    user: str,
    relation: str,
    obj: str,
    qualify: bool = True,
    contextual_tuples: list[ClientTuple] | None = None,
    context: dict[str, Any] | None = None,
    retry: RetryPolicy = DEFAULT_RETRY,
) -> bool:
    """Return whether ``user:<user>`` has ``relation`` on ``obj`` (e.g. ``table:db1$t``).

    ``context`` supplies the runtime values a CONDITION evaluates against — for the model's
    ``non_expired_grant`` that is ``current_time``. The split is deliberate: the tuple carries the
    window (``grant_time`` + ``grant_duration``, written once at grant time) and the check carries the
    clock. It is sent through :func:`condition_context`, so an omitted ``current_time`` is evaluated at
    the server's now and an explicit one wins.

    ``contextual_tuples`` evaluates the check AS IF those tuples existed, without writing anything.
    That is the whole of "assert before you grant": ask whether a proposed grant would actually produce
    the access someone wants — and, just as often, what ELSE it would unlock — while the store is
    untouched. A concentric model makes this necessary rather than nice: a grant three levels up
    cascades to every child, and reading the DSL to predict that is exactly the reasoning humans get
    wrong. Contextual tuples are per-request and never persisted, but they are evaluated like stored
    ones, so one carrying the clock is refused (:func:`reject_stored_clock`) before anything is sent.

    Callers pass a principal key (the caller's ``subject``) and this sends the user it is (:func:`user_object`). Pass
    ``qualify=False`` when ``user`` is ALREADY a full subject — a ``type:id`` user or a ``type:id#rel``
    userset — so it is sent verbatim (the access-simulator probes arbitrary subjects, incl. usersets;
    double-prefixing them to ``user:user:…`` would make every Check falsely deny — review 2026-07-20).

    Transient OpenFGA failures (network/timeout, 429, 5xx) are retried with
    backoff; a definitive ``allowed=false`` is a normal result and is NOT retried.
    On exhausted retries / outage we fail closed with ``ServiceUnavailableError``.
    The per-request timeout is carried by ``client`` (see ``make_client``).
    """
    reject_stored_clock(contextual_tuples or ())
    subject = user_object(user) if qualify else user

    async def _do_check() -> bool:
        response = await client.check(
            ClientCheckRequest(
                user=subject,
                relation=relation,
                object=obj,
                contextual_tuples=contextual_tuples,
                context=condition_context(context),
            ),
            options=_fresh_read_options(),
        )
        return bool(response.allowed)

    return await guarded(
        _do_check,
        event="openfga_check_unavailable",
        extra={"relation": relation, "object": obj},
        retry=retry,
    )


async def batch_check(
    client: OpenFgaClient,
    *,
    user: str,
    relation: str,
    objects: list[str],
    context: dict[str, Any] | None = None,
    retry: RetryPolicy = DEFAULT_RETRY,
) -> dict[str, bool]:
    """Return ``{object: allowed}`` for one user across many objects in one round-trip.

    ``context`` is per-item and defaulted through :func:`condition_context`, so a batch spanning
    time-boxed grants answers on the same clock as a single :func:`check` would. Without it every
    conditional tuple in the batch would come back denied.

    A read-only/idempotent authorization path, so it gets the same bounded retry +
    fail-closed treatment as :func:`check` (transient OpenFGA/transport faults retried;
    outage → ``ServiceUnavailableError`` → 503).

    An item OpenFGA answers with an error (a per-check timeout, a resolution too complex, a relation
    the pinned model lacks) fails the whole call the same way, because the SDK hands it back as
    ``allowed=False`` and ``{object: bool}`` has no third value. A server-side item fault is retried
    like a 5xx; an input fault is not, like a 400.

    An object that is not an object id (:func:`is_object_id`) is answered ``False`` without being sent:
    no tuple can name it, and sending it would fail the item, or the whole request, for every object
    beside it.
    """
    unaskable = {o: False for o in objects if not is_object_id(o)}
    items = [
        ClientBatchCheckItem(user=user_object(user), relation=relation, object=o, context=condition_context(context)) for o in objects if o not in unaskable
    ]
    if not items:
        return unaskable

    async def _do_batch_check() -> dict[str, bool]:
        options = _fresh_read_options(max_parallel_requests=_parallel_batches(len(items)))
        response = await client.batch_check(ClientBatchCheckRequest(checks=items), options=options)
        unanswered = {r.request.object: r.error for r in response.result if r.error is not None}
        if unanswered:
            raise UnansweredBatchItems(unanswered)
        return unaskable | {r.request.object: bool(r.allowed) for r in response.result}

    return await guarded(
        _do_batch_check,
        event="openfga_batch_check_unavailable",
        extra={"relation": relation},
        retry=retry,
    )


class ObjectListing(NamedTuple):
    """What the caller may reach, AND whether that answer is complete.

    A bare `list[str]` cannot express the difference between "this subject holds three tables" and
    "the server stopped counting at 1000", and the five governed listings that intersect their rows
    against this call then mint a page cursor from the shortened list — so paging forward cannot
    recover the hidden rows either. `pagination.md`'s rule is that the CLIENT must be able to learn
    the answer was cut; a log tells an operator, which is a different person at a different time.

    A `NamedTuple` so the existing `set(...)`/iteration idioms fail LOUDLY at every call site rather
    than silently producing a two-element set of `[objects]` and `False`.
    """

    objects: list[str]
    truncated: bool


async def list_objects(
    client: OpenFgaClient,
    *,
    user: str,
    relation: str,
    object_type: str,
    qualify: bool = True,
    context: dict[str, Any] | None = None,
    retry: RetryPolicy = DEFAULT_RETRY,
) -> ObjectListing:
    """Return the objects of ``object_type`` the user has ``relation`` on (e.g. ``table:…``).

    Callers pass a principal key (the caller's ``subject``) and this sends the user it is (:func:`user_object`). Pass
    ``qualify=False`` when ``user`` is ALREADY a full subject — a ``type:id`` user or a ``type:id#rel``
    userset — exactly as :func:`check` does. Without that hatch an estate admin's access review could only ask
    "what can this *user* reach", never "what can ``role:<id>#assignee`` reach", and a grant to a role is
    the userset question that explains a real grant.

    ``context`` is defaulted through :func:`condition_context`. It matters MORE here than on a single
    check: a listing that omits the clock silently drops every object reachable only through a
    time-boxed grant, and a short list reads as a correct answer rather than an error.

    Read-only/idempotent, so it gets the same bounded retry + fail-closed treatment as
    :func:`check`.
    """
    subject = user_object(user) if qualify else user

    async def _do_list_objects() -> ObjectListing:
        response = await client.list_objects(
            ClientListObjectsRequest(user=subject, relation=relation, type=object_type, context=condition_context(context)), options=_fresh_read_options()
        )
        objects = list(response.objects)
        # THE SAME GUARD `list_users` HAS CARRIED SINCE IT WAS WRITTEN, and the asymmetry is how this
        # one stayed invisible. ListObjects has no pagination either, so a result sitting exactly on
        # the server's ceiling is far more likely the truncation point than the true count.
        #
        # Five governed collection listings intersect their rows against this call, so a silent
        # truncation hides an entitled caller's OWN rows — and the page cursor is then minted from the
        # shortened list, so paging forward cannot recover them either. It fails CLOSED (hides rather
        # than exposes) and only past the cap, which nothing here is near — but "nothing is near it"
        # is an assumption, and this is what turns it into something the estate would tell you about.
        truncated = len(objects) >= LIST_OBJECTS_SERVER_CAP
        if truncated:
            log.warning(
                "openfga_list_objects_possibly_truncated",
                extra={"relation": relation, "object_type": object_type, "objects": len(objects)},
            )
        return ObjectListing(objects=objects, truncated=truncated)

    return await guarded(
        _do_list_objects,
        event="openfga_list_objects_unavailable",
        extra={"relation": relation, "object_type": object_type},
        retry=retry,
    )


#: Tuples per page of every whole read (:func:`read_all_tuples`): one object's, one subject's, or the store's. The
#: server caps a Read page at 100, so a larger number would buy nothing.
READ_PAGE_SIZE = 100

#: Ceiling on the pages of one whole read, so a server that returns a non-advancing cursor cannot spin forever. 500
#: pages is 50,000 tuples: measured 2026-09-11, the whole estate is 5,027 across 51 pages, so the widest read (the
#: census, which reads the store) has an order of magnitude of headroom, and one object's grants are far fewer.
READ_MAX_PAGES = 500


#: OpenFGA's default ``listUsersMaxResults`` — ListUsers has no pagination, so a result this large
#: is likely the server's silent truncation point, not the true grantee count.
LIST_USERS_SERVER_CAP = 1000

#: OpenFGA's default ``listObjectsMaxResults``. Same number, same reason, same silent behaviour as its
#: sibling above — declared beside it deliberately, because the two calls having the same ceiling and
#: only ONE of them checking it is exactly how the unguarded half went unnoticed.
LIST_OBJECTS_SERVER_CAP = 1000


async def list_users(
    client: OpenFgaClient,
    *,
    relation: str,
    obj: str,
    user_type: str = "user",
    user_relation: str | None = None,
    qualified: bool = False,
    context: dict[str, Any] | None = None,
    retry: RetryPolicy = DEFAULT_RETRY,
) -> list[str]:
    """The ``user:`` subjects holding ``relation`` on ``obj`` (e.g. every reader of ``table:db1$t``),
    sorted — the access-review primitive (#51), the inverse of :func:`list_objects`.

    ``user_type`` (+ optional ``user_relation``) selects WHICH subject kind to enumerate — OpenFGA takes
    exactly ONE filter per call, so ``user_type="role", user_relation="assignee"`` answers "which roles
    hold this", a different question from "which users". The default keeps the review contract every
    existing caller relies on: bare ``user:`` ids.

    ``qualified=True`` returns FULL subjects (``user:alice``, ``role:<id>#assignee``, ``user:*``) instead of
    bare ids. Off by default because the access-review callers built their output around bare ids; an
    estate-wide access view needs the qualified form, since a bare ``eng`` is ambiguous once the filter is not
    ``user``.

    ListUsers *expands* the model (role assignees, nested roles, the parent cascade), so the answer is
    effective access, not just direct tuples — exactly what a reviewer needs. A public ``user:*``
    wildcard surfaces as ``"*"`` (the model doesn't write one today, but a review must never hide it).
    Read-only/idempotent, so it gets the same bounded retry + fail-closed treatment as :func:`check`
    (outage → ``ServiceUnavailableError`` → 503 — never an empty list that reads as "nobody").

    ``context`` is defaulted through :func:`condition_context`, so an access review counts the
    holders of a live time-boxed grant instead of omitting them — the same silent-undercount hazard
    as :func:`list_objects`, pointed the other way.

    Caveat: the ListUsers API has no pagination, and the server truncates silently at its
    ``listUsersMaxResults`` / ``listUsersDeadline`` limits (OpenFGA defaults: 1000 subjects / 3s) —
    so a result at or past the cap is logged as possibly truncated; an under-reported review must
    never pass unnoticed.
    """
    obj_type, _, obj_id = obj.partition(":")

    async def _do_list_users() -> list[str]:
        response = await client.list_users(
            ClientListUsersRequest(
                object=FgaObject(type=obj_type, id=obj_id),
                relation=relation,
                user_filters=[UserTypeFilter(type=user_type, relation=user_relation)],
                context=condition_context(context),
            ),
            options=_fresh_read_options(),
        )
        subjects: list[str] = []
        for user in response.users or []:
            # The three arms of ListUsers' User union. A userset arm only ever arrives when the filter
            # ASKS for one (user_relation set); dropping it silently — as this did before — hid exactly
            # the subjects this model routes access through.
            if (obj_arm := getattr(user, "object", None)) is not None:
                # A bare id is a SUBJECT, so a `user` id is decoded (:func:`subject_of`); a qualified one is the
                # store's own object id, which a caller passes back verbatim (`qualify=False`).
                subjects.append(f"{obj_arm.type}:{obj_arm.id}" if qualified else subject_of(obj_arm.id) if obj_arm.type == "user" else obj_arm.id)
            elif (userset_arm := getattr(user, "userset", None)) is not None:
                subjects.append(f"{userset_arm.type}:{userset_arm.id}#{userset_arm.relation}" if qualified else userset_arm.id)
            elif (wildcard_arm := getattr(user, "wildcard", None)) is not None:
                subjects.append(f"{wildcard_arm.type}:*" if qualified else "*")
        if len(subjects) >= LIST_USERS_SERVER_CAP:
            log.warning(
                "openfga_list_users_possibly_truncated",
                extra={"relation": relation, "object": obj, "subjects": len(subjects)},
            )
        return sorted(subjects)

    return await guarded(
        _do_list_users,
        event="openfga_list_users_unavailable",
        extra={"relation": relation, "object": obj},
        retry=retry,
    )


#: Depth ceiling for the Expand tree walk. OpenFGA expands ONE relation's rewrite expression (it does
#: not recurse through `tuple_to_userset` into the parent's own tree), so real depth is the nesting of
#: `or`/`and`/`but not` in the DSL — 3 in this model. The cap only stops a malformed/cyclic response
#: from spinning the normaliser; hitting it is logged, never silently truncated into a valid-looking tree.
_MAX_EXPAND_DEPTH = 32


def _expand_node(node: Any, depth: int = 0) -> dict[str, Any]:
    """Normalise one SDK ``Node`` into plain JSON-able dicts, verbatim in structure.

    The SDK's tree is a graph of model objects whose ``to_dict`` drags along OpenAPI machinery, so the
    admin API would leak SDK shape into a frozen contract. This keeps the OpenFGA vocabulary
    (``union`` / ``intersection`` / ``difference`` / ``leaf``) intact — the caller wants the DERIVATION,
    and flattening it here would throw away exactly the operator that explains a surprising grant.
    """
    if depth >= _MAX_EXPAND_DEPTH:
        log.warning("openfga_expand_depth_capped", extra={"depth": depth, "node": getattr(node, "name", None)})
        return {"name": getattr(node, "name", None), "truncated": True}

    out: dict[str, Any] = {"name": getattr(node, "name", None)}

    if (leaf := getattr(node, "leaf", None)) is not None:
        computed = getattr(leaf, "computed", None)
        ttu = getattr(leaf, "tuple_to_userset", None)
        users = getattr(leaf, "users", None)
        out["leaf"] = {
            # `users` is the terminal set — the actual subjects, incl. a `user:*` wildcard verbatim.
            "users": list(getattr(users, "users", None) or []) if users is not None else None,
            "computed": getattr(computed, "userset", None) if computed is not None else None,
            "tuple_to_userset": (
                {
                    "tupleset": getattr(ttu, "tupleset", None),
                    "computed": [c.userset for c in (getattr(ttu, "computed", None) or []) if getattr(c, "userset", None)],
                }
                if ttu is not None
                else None
            ),
        }
    if (union := getattr(node, "union", None)) is not None:
        out["union"] = [_expand_node(n, depth + 1) for n in (getattr(union, "nodes", None) or [])]
    if (intersection := getattr(node, "intersection", None)) is not None:
        out["intersection"] = [_expand_node(n, depth + 1) for n in (getattr(intersection, "nodes", None) or [])]
    if (difference := getattr(node, "difference", None)) is not None:
        base = getattr(difference, "base", None)
        subtract = getattr(difference, "subtract", None)
        out["difference"] = {
            "base": _expand_node(base, depth + 1) if base is not None else None,
            "subtract": _expand_node(subtract, depth + 1) if subtract is not None else None,
        }
    return out


async def expand(
    client: OpenFgaClient,
    *,
    relation: str,
    obj: str,
    retry: RetryPolicy = DEFAULT_RETRY,
) -> dict[str, Any]:
    """The userset TREE for ``relation`` on ``obj`` — how the grant is composed, not whether it holds.

    This is the one primitive that answers *why*. :func:`check` returns a bare boolean and
    :func:`list_users` returns the effective subject set; neither says which rung or which parent hop
    produced the answer, so a permission derived through three hops is indistinguishable from a direct
    grant — and the two have very different blast radii when revoked.

    Returns the tree normalised to plain dicts (see :func:`_expand_node`), rooted at the relation:
    ``{"name": "table:db1$t#reader", "union": [{"leaf": {"users": [...]}}, {"leaf": {"computed": ...}}, …]}``.
    An empty/absent tree comes back as ``{}`` rather than ``None``, so callers branch on emptiness, not
    on a null that also means "unavailable".

    Note Expand resolves ONE level: a ``tuple_to_userset`` leaf names the tupleset and the computed
    relation, it does not recurse into the parent object's own tree. Walking the full chain is the
    caller's job (one Expand per hop) — which is deliberate: the model owns the shape, not this wrapper.

    Read-only/idempotent, so it gets the same bounded retry + fail-closed treatment as :func:`check`
    (outage → ``ServiceUnavailableError`` → 503, never an empty tree that reads as "derived from nothing").
    """

    async def _do_expand() -> dict[str, Any]:
        response = await client.expand(ClientExpandRequest(relation=relation, object=obj), options=_fresh_read_options())
        tree = getattr(response, "tree", None)
        root = getattr(tree, "root", None) if tree is not None else None
        return _expand_node(root) if root is not None else {}

    return await guarded(
        _do_expand,
        event="openfga_expand_unavailable",
        extra={"relation": relation, "object": obj},
        retry=retry,
    )


#: How many hops :func:`expand_tree` will follow. The model's deepest real chain is
#: role → project → warehouse → namespace → nested namespace → table, i.e. 5 hops, so this covers it with
#: no room for a pathological store to turn one request into an unbounded fan-out.
MAX_EXPAND_TREE_DEPTH = 6


def _relation_of(userset: str) -> str:
    """The relation half of an OpenFGA userset reference.

    Expand answers with QUALIFIED usersets — `"table:db1$t#reader"`, not `"reader"` — in both the
    `computed` leaf and each `tupleToUserset.computed` entry. Everything downstream wants the bare
    relation, and passing the qualified form on is a malformed request rather than a wrong answer, so
    it surfaces as a 500 from the server and a fail-closed 503 here. Bare input passes through, because
    the shape is not guaranteed across versions and a stricter reader would trade one bug for another.
    """
    return userset.rpartition("#")[2] or userset


def _object_of(userset: str, fallback: str) -> str:
    """The object half of a qualified userset, or ``fallback`` when it carries none.

    A `computed` leaf is by definition a rung on the SAME object, so the fallback is the normal path;
    the split exists so a qualified reference is never silently re-anchored to the wrong object.
    """
    obj = userset.rpartition("#")[0]
    return obj or fallback


async def expand_tree(
    client: OpenFgaClient,
    *,
    relation: str,
    obj: str,
    max_depth: int = 1,
    retry: RetryPolicy = DEFAULT_RETRY,
) -> dict[str, Any]:
    """:func:`expand`, followed recursively — the FULL derivation chain, not one level of it.

    A single Expand stops at the edge: a ``tuple_to_userset`` leaf says "``owner`` from whatever
    ``parent`` points at" without saying what that is or who holds it there. That is the exact hop where
    a concentric model's answer lives, so a one-level tree cannot explain a cascaded grant — it can only
    say that a cascade exists. This follows both continuing leaf kinds:

    * ``computed`` (a same-object rung — ``owner`` implying ``writer``) → expand that rung on this object.
    * ``tuple_to_userset`` (``X from Y``) → read the tupleset's tuples off this object to find the target
      objects, then expand each computed relation on each target.

    Each resolved child is attached to its leaf as ``expanded``, so the shape stays the OpenFGA tree with
    the hops filled in rather than a flattened path that has already thrown the operators away.

    ``max_depth`` counts hops (1 = a plain Expand, the faithful primitive), clamped to
    :data:`MAX_EXPAND_TREE_DEPTH`. Both terminations are marked in the tree rather than silently
    dropped, because a derivation that stopped early must never read as one that ended:

    * ``leaf.continues`` — the budget ran out AT this leaf. Decided BEFORE any round trip, so a depth-1
      call costs exactly one Expand while still telling a UI where "expand further" is offerable. This
      is the only way the walk stops on budget; there is deliberately no half-taken hop.
    * ``cycle`` — this ``object#relation`` is already ON THE PATH to itself. ``namespace`` self-nests
      under ``namespace``, so a loop is reachable through misconfigured tuples, not only through a bug.
      Scoped to the path and not to the whole walk on purpose: a concentric model reaches the same rung
      down several branches all the time (``can_read`` via ``writer`` and via ``reader`` both land on
      ``owner``), and a walk-global set called that a loop and threw the second branch away.

    (``truncated`` may still appear, from :func:`_expand_node` — that is the *normaliser's* structural
    depth cap on one malformed response, a different failure from this walk's hop budget.)

    Read-only/idempotent throughout — every leg inherits the bounded retry + fail-closed behaviour of
    :func:`expand` and :func:`read_object_tuples`, so a partial tree is never returned in place of a 503.
    """
    depth_budget = max(1, min(max_depth, MAX_EXPAND_TREE_DEPTH))

    async def _walk(rel: str, target: str, depth: int, path: frozenset[str]) -> dict[str, Any]:
        key = f"{target}#{rel}"
        # PATH-scoped, never walk-global (SKG-03). A cycle is an `object#relation` that appears on the
        # way to ITSELF; an `object#relation` reached twice down two different branches is a DIAMOND,
        # which a concentric model produces constantly (`can_read` → `writer` → `owner` and
        # `can_read` → `reader` → `owner`). A single visited set for the whole walk cannot tell them
        # apart, so it stamped the second arm `cycle: True` and dropped its subtree — the one branch a
        # reader opened `expand_tree` to see. An IMMUTABLE path handed down, rather than a shared set
        # marked on entry and un-marked on exit: the un-mark is the half that gets forgotten on an
        # early return, and this walk has several. Cost is bounded by the hop budget (max 6) — the same
        # rung may now be expanded once per branch that reaches it, which is what makes each branch
        # answerable.
        if key in path:
            return {"name": key, "cycle": True}
        node = await expand(
            client,
            relation=rel,
            obj=target,
            retry=retry,
        )
        await _resolve(node, target, depth, path | {key})
        return node

    async def _resolve(node: dict[str, Any], target: str, depth: int, path: frozenset[str]) -> None:
        """Walk the normalised tree in place, expanding every leaf that continues somewhere."""
        for branch in ("union", "intersection"):
            for child in node.get(branch) or []:
                await _resolve(child, target, depth, path)
        if (difference := node.get("difference")) is not None:
            for side in ("base", "subtract"):
                if (child := difference.get(side)) is not None:
                    await _resolve(child, target, depth, path)

        leaf = node.get("leaf")
        if not leaf:
            return
        if depth >= depth_budget:
            # Out of budget: mark that this leaf CONTINUES and stop — before spending a tuple read
            # whose only product would be a truncation stub. The flag is what lets a UI offer "expand
            # further" instead of drawing a dead end that is not one.
            if leaf.get("computed") is not None or leaf.get("tuple_to_userset") is not None:
                leaf["continues"] = True
            return
        if (computed := leaf.get("computed")) is not None:
            # `computed.userset` comes back as a FULL `object#relation` ("table:db1$t#reader"), not a
            # bare relation name. Feeding the whole string back as a relation makes the next Expand
            # malformed and OpenFGA answers 500 — which this module dutifully fails closed into a 503,
            # so the symptom was "the derivation is unavailable" on every relation that resolves
            # through a rung, i.e. all of the interesting ones. Verified against the live store:
            # expand(can_read_data, table:bronze$pages) → {"computed":{"userset":"table:bronze$pages#reader"}}.
            leaf["expanded"] = [await _walk(_relation_of(computed), _object_of(computed, target), depth + 1, path)]
            return
        if (ttu := leaf.get("tuple_to_userset")) is not None:
            # `tupleset` arrives as "<object>#<relation>"; the relation half is the edge to follow, and
            # the tuples holding it name the parent objects (`table:x#parent@namespace:db1` → namespace:db1).
            tupleset_relation = str(ttu.get("tupleset") or "").rpartition("#")[2]
            if not tupleset_relation:
                return
            edges = await read_object_tuples(client, target, retry=retry)
            parents = [t.user for t in edges if t.relation == tupleset_relation]
            expanded: list[dict[str, Any]] = []
            for parent in parents:
                for computed_relation in ttu.get("computed") or []:
                    # Same normalisation as the `computed` leaf above: these arrive qualified too.
                    expanded.append(await _walk(_relation_of(computed_relation), parent, depth + 1, path))
            leaf["expanded"] = expanded

    return await _walk(relation, obj, 1, frozenset())


async def read_changes(
    client: OpenFgaClient,
    *,
    object_type: str,
    page_size: int | None = None,
    continuation_token: str | None = None,
    start_time: str | None = None,
    retry: RetryPolicy = DEFAULT_RETRY,
) -> tuple[list[dict[str, Any]], str | None]:
    """The tuple CHANGELOG for one object type — every write and delete, in order, with timestamps.

    This is the only "when" the estate has. A Read shows what is true NOW; it cannot say when a grant
    landed, and it cannot show a grant that was later revoked. The changelog shows both, which makes it
    the difference between an access review and an access snapshot.

    What it deliberately does NOT carry is WHO. An OpenFGA change record is
    ``{tuple_key, operation, timestamp}`` and nothing else — the store has no notion of an actor, and
    no amount of reading it will produce one. The acting principal exists only in this estate's own
    audit stream (``access_tuple_write`` / ``access_tuple_delete``, which record ``subject``), and
    joining the two is a correlation by (resource, relation, grantee, time) rather than a lookup.
    Callers must not present a timestamp as attribution.

    ``object_type`` is REQUIRED by the API — the changelog is filterable by type and by nothing else,
    so "the history of THIS tuple" is a scan of its type's changes, not a query. Bounded by the caller.

    **Termination is the caller's, and it is not the token.** ReadChanges returns the same non-empty
    continuation token forever once the stream is exhausted, because it is a resumable cursor rather
    than an end-of-list sentinel. Loop on the PAGE, never the token::

        changes, token = await read_changes(client, object_type="table")
        while changes:
            ...
            changes, token = await read_changes(client, object_type="table", continuation_token=token)

    Read-only/idempotent, so it gets the same bounded retry + fail-closed treatment as :func:`check`:
    an outage is a 503, never an empty history that reads as "nothing ever happened here".
    """

    async def _do_read_changes() -> tuple[list[dict[str, Any]], str | None]:
        # Built INSIDE the retry closure for the same reason read_tuples does it: the SDK pops
        # pagination out of the options dict it is handed, so a shared dict loses it on attempt two.
        options: dict[str, int | str | dict[str, int | str]] = {}
        if page_size is not None:
            options["page_size"] = page_size
        if continuation_token:
            options["continuation_token"] = continuation_token
        response = await client.read_changes(
            ClientReadChangesRequest(type=object_type, start_time=start_time),
            options=options or None,
        )
        changes: list[dict[str, Any]] = []
        for c in response.changes or []:
            key = c.tuple_key
            stamp = getattr(c, "timestamp", None)
            changes.append(
                {
                    "user": key.user,
                    "relation": key.relation,
                    "object": key.object,
                    # "TUPLE_OPERATION_WRITE" / "…_DELETE" → "write" / "delete".
                    "operation": str(c.operation).rsplit("_", 1)[-1].lower(),
                    "timestamp": stamp.isoformat() if hasattr(stamp, "isoformat") else (str(stamp) if stamp else None),
                }
            )
        # The token is returned RAW, and callers must stop on an empty page rather than on a falsy
        # token. OpenFGA's ReadChanges keeps handing back the SAME non-empty continuation token once the
        # stream is exhausted — it is a resumable cursor, not an end-of-list sentinel — so the usual
        # `while token:` loop never terminates. Normalising it to None here would be worse: it would
        # discard a cursor a caller may legitimately persist to resume later.
        return changes, response.continuation_token

    return await guarded(
        _do_read_changes,
        event="openfga_read_changes_unavailable",
        extra={"object_type": object_type},
        retry=retry,
    )


async def read_object_tuples(client: OpenFgaClient, obj: str, *, retry: RetryPolicy = DEFAULT_RETRY) -> list[ClientTuple]:
    """Every tuple whose object is ``obj`` (all grants on ``table:<uuid>``), paged to the end (:func:`read_all_tuples`).

    Raises:
        ServiceUnavailableError: OpenFGA did not answer, or the read reached its page ceiling.
    """
    return await read_all_tuples(client, obj=obj, retry=retry)


async def read_tuples(
    client: OpenFgaClient,
    *,
    user: str | None = None,
    relation: str | None = None,
    obj: str | None = None,
    page_size: int | None = None,
    continuation_token: str | None = None,
    retry: RetryPolicy = DEFAULT_RETRY,
) -> tuple[list[ClientTuple], str | None]:
    """One page of the OpenFGA Read API — the DIRECT tuples matching the filter, plus the next-page token.

    Unlike :func:`list_users`, Read never expands the model: the answer is the raw stored tuples (the
    estate-admin tuple browser's primitive), paginated by the server. ``obj`` is a full object
    (``table:<uuid>``), with or without ``user``, or a bare type (``table:``) beside a ``user``; no filter at
    all reads the whole store. OpenFGA refuses every other filter, a ``user`` with no object type and a bare
    type with no ``user`` ("the object type field is required and both the object id and user cannot be
    empty", v1.18.3), and this wrapper reports that refusal as an outage like any other, so callers enforce
    the precondition up front (the admin API maps each to a 400). ``relation`` narrows a full object's tuples to that
    one relation, on the server.
    Read-only/idempotent, so it gets the same bounded retry + fail-closed treatment as :func:`check`
    (outage → ``ServiceUnavailableError`` → 503, never an empty page that reads as "no grants").
    """
    tuple_key = ReadRequestTupleKey(user=user, relation=relation, object=obj)  # the SDK omits it when every field is None

    async def _do_read_page() -> tuple[list[ClientTuple], str | None]:
        # Built INSIDE the retry closure: the SDK pops page_size/continuation_token out of the options
        # dict it is handed, so a shared dict would silently lose the pagination on the second attempt.
        options = _fresh_read_options()
        if page_size is not None:
            options["page_size"] = page_size
        if continuation_token:
            options["continuation_token"] = continuation_token
        response = await client.read(tuple_key, options=options)
        # The condition rides the tuple back out. Dropping it (as this did) makes a time-boxed grant
        # read as permanent everywhere downstream — the revoke-on-delete path would then try to delete
        # a tuple it has mis-described, and the UI would show an expiring grant as forever.
        page = [
            ClientTuple(
                user=t.key.user,
                relation=t.key.relation,
                object=t.key.object,
                condition=getattr(t.key, "condition", None),
            )
            for t in response.tuples or []
        ]
        return page, response.continuation_token or None

    return await guarded(
        _do_read_page,
        event="openfga_read_unavailable",
        extra={"object": obj, "user": user},
        retry=retry,
    )


async def read_all_tuples(
    client: OpenFgaClient, *, user: str | None = None, obj: str | None = None, relation: str | None = None, retry: RetryPolicy = DEFAULT_RETRY
) -> list[ClientTuple]:
    """Every direct tuple the filter matches (:func:`read_tuples`), paged to the end: the one loop every whole read runs.

    The answer means "all of them" and nothing else. A caller that mutates deletes exactly what it is handed, and one
    that counts decides which objects hold no grant, so a short list would be a revoke that leaves live grants behind
    or a census that invents ghosts, each reporting success. ``READ_MAX_PAGES`` exists so a cursor that never ends
    cannot spin a request; reaching it with a cursor outstanding is a pathological store or a continuation-token bug,
    and leaves as the outage every other unanswerable read is.

    Raises:
        ServiceUnavailableError: OpenFGA did not answer a page, or ``READ_MAX_PAGES`` pages left a cursor outstanding.
    """
    found: list[ClientTuple] = []
    token: str | None = None
    for _ in range(READ_MAX_PAGES):
        page, token = await read_tuples(client, user=user, obj=obj, relation=relation, page_size=READ_PAGE_SIZE, continuation_token=token, retry=retry)
        found.extend(page)
        if not token:
            return found
    log.error("openfga_read_truncated", extra={"object": obj, "user": user, "relation": relation, "max_pages": READ_MAX_PAGES})
    raise ServiceUnavailableError(f"a tuple read (object {obj}, user {user}) exceeded {READ_MAX_PAGES} pages — refusing to answer with a partial set")


async def read_subject_tuples(client: OpenFgaClient, user: str, fga_type: str) -> list[ClientTuple]:
    """Every direct tuple naming ``user`` on an object of ``fga_type``, paged to the end (:func:`read_all_tuples`)."""
    return await read_all_tuples(client, user=user, obj=f"{fga_type}:")


#: Relations that hold bookkeeping rather than a grant: no permission reads them, and a scan counting an object's grants
#: leaves them out.
ADMIN_REVISION: Final = "admin_revision"
BOOKKEEPING_RELATIONS: Final = frozenset({ADMIN_REVISION})


class GrantCensus(BaseModel):
    """Every object's grant count, bucketed by its type: what a drift report's authorization categories read."""

    #: ``{object_type: {object_id: tuple_count}}``, bookkeeping relations excluded: a project holding only its admin
    #: revision is as stranded as one holding nothing.
    counts_by_type: dict[str, dict[str, int]] = Field(default_factory=dict)


async def census(client: OpenFgaClient, *, exclude: Collection[str] = ()) -> GrantCensus:
    """Read the WHOLE store once, paged to the end, and count every object's tuples by its type, but those ``exclude``
    names (the objects a deployment keeps for itself, which no registry names).

    **One unfiltered read, not one bare-type read per type, and that is a correctness fix rather than an optimization.**
    OpenFGA rejects a Read whose ``tuple_key`` is present but carries neither an object id nor a user: ``the 'tuple_key'
    field was provided but the object type field is required and both the object id and user cannot be empty`` (HTTP
    400). A bare-type filter (``obj="project:"``) is exactly that shape, so every reader built on one failed against a
    real server while passing against a double that accepted the filter (measured live 2026-08-04), and the wrapper
    reported that 400 as ``ServiceUnavailableError``, a permanent outage on a healthy server.
    """
    counted = GrantCensus()
    for item in await read_all_tuples(client):
        obj_type, _, obj_id = str(item.object).partition(":")
        if not obj_id or item.relation in BOOKKEEPING_RELATIONS or str(item.object) in exclude:
            continue
        bucket = counted.counts_by_type.setdefault(obj_type, {})
        bucket[obj_id] = bucket.get(obj_id, 0) + 1
    return counted


__all__ = [
    "ADMIN_REVISION",
    "BOOKKEEPING_RELATIONS",
    "CHECKS_PER_BATCH",
    "CLOCK_PARAMETER",
    "LIST_OBJECTS_SERVER_CAP",
    "LIST_USERS_SERVER_CAP",
    "MAX_EXPAND_TREE_DEPTH",
    "READ_MAX_PAGES",
    "READ_PAGE_SIZE",
    "GrantCensus",
    "ObjectListing",
    "batch_check",
    "census",
    "check",
    "condition_context",
    "expand",
    "expand_tree",
    "list_objects",
    "list_users",
    "read_all_tuples",
    "read_changes",
    "read_object_tuples",
    "read_subject_tuples",
    "read_tuples",
    "reject_stored_clock",
]
