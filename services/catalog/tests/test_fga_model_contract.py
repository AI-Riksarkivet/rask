"""Every (object_type, relation) the app can CHECK must exist in the compiled OpenFGA model.

The bug class this exists to make unshippable: the app checks a ``can_*`` relation that the target
type does not define. OpenFGA answers that with a 400 (``relation 'namespace#can_write_data' not
found``, error code 2000) — an ``ApiException`` that is NOT retryable, so ``lakehouse.governed.fga_reads.check``'s
fail-closed handler converts it to ``ServiceUnavailableError`` → **HTTP 503 for every caller, owners
included**, logged as ``openfga_check_unavailable`` (an outage, which it is not). It is a total
outage of the route that masquerades as an infra blip. That is exactly how the namespaced
transaction ``alter`` path (``namespace#can_write_data``, a table-only action) shipped broken.

Mocked authz tests cannot catch it: they monkeypatch ``fga_reads.check`` and assert the relation string
the app passed, which passes happily against a phantom relation. The ``.fga.yaml`` tests cannot
catch it either: they assert whatever (object, relation) pairs the *test author* chose, which may
not be the pairs the *app* sends (the yaml exercised ``transaction:``; the app checks
``namespace:``). Only a cross-check of the app's own mapping tables against the compiled model
closes it — so this test enumerates the pairs by DRIVING the real resolvers in
``catalog.api.fga_deps`` (never a hand-copied list, which would drift) and asserts each pair
resolves in ``lakehouse/governed/auth/model.json``, the file the app actually loads.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import httpx
import pyarrow as pa
import pytest
from fastapi import Request
from lance_namespace import InternalError
from openfga_sdk import OpenFgaClient

from catalog.api import fga_deps, read_actions
from catalog.api.dependencies import Resolver
from catalog.core import write_judge
from catalog.core.config import Settings
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.governed import fga_model
from lakehouse.governed.idp_identity import PersonPrincipal
from lakehouse.governed.oidc import IDToken
from lakehouse.governed.principal import Principal
from tests.unit.named_resolver import NamedResolver


#: Sentinel FGA client. The resolvers only ever hand it to ``fga_reads.check``/``fga_reads.batch_check``, which the
#: recording fakes below replace — so nothing ever calls a method on it, and the cast is safe.
_CLIENT = cast("OpenFgaClient", object())


# --------------------------------------------------------------------------- #
# The compiled model (what the app LOADS) -> {type: {relation, ...}}
# --------------------------------------------------------------------------- #


def _model_relations() -> dict[str, set[str]]:
    """``{object_type: {relation, ...}}`` parsed from the compiled ``model.json``."""
    model = fga_model.load_model()
    return {td["type"]: set(td.get("relations") or {}) for td in model["type_definitions"]}


# --------------------------------------------------------------------------- #
# Enumerate every (type, relation) pair the CATALOG can check — by driving fga_deps
# --------------------------------------------------------------------------- #


def _settings() -> Settings:
    # model_validate (not Settings(...)) so field-name keys validate cleanly — the fields carry
    # LANCE_* aliases. OIDC is required whenever FGA is on (authz needs an authenticated user).
    return Settings.model_validate(
        {
            "oidc_enabled": True,
            "oidc_issuer": "https://idp.example",
            "oidc_audience": "lance",
            "oidc_idp_id": "dex",
            "oidc_connectors": {"local": "dex"},
            "fga_enabled": True,
            "fga_api_url": "http://openfga:8080",
            "s3_access_key_id": "x",
            "s3_secret_access_key": "x",
        }
    )


class _FakeRequest:
    """The only thing ``_authorize_batch`` and ``_authorize_grant`` touch on the Request is ``await request.json()``."""

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    async def json(self) -> dict[str, Any]:
        return self._body


def _rung_type(rung: write_judge.Rung) -> str:
    """The object type a rung is asked on: a named table's or namespace's kind, else its object's type."""
    return rung.on.kind if isinstance(rung.on, write_judge.Named) else _obj_type(rung.on)


def _obj_type(obj: str) -> str:
    return obj.split(":", 1)[0]


def _catalog_pairs(monkeypatch: pytest.MonkeyPatch) -> set[tuple[str, str]]:
    """Every ``(object_type, relation)`` ``catalog.api.fga_deps`` can send to OpenFGA.

    Driven through the REAL mapping tables + resolvers, so a future edit to ``_action_relation`` /
    ``_OWNER_SUFFIX_RELATION`` / ``_BATCH_OWNER_OPS`` / ``_create_parent_rung`` /
    ``_authorize_transaction`` is picked up here automatically instead of drifting from a copy.
    """
    pairs: set[tuple[str, str]] = set()
    settings = _settings()

    # 1. Non-create ops: _action_relation(type, suffix) for EVERY guarded type x EVERY suffix the rung
    #    maps declare — owner- and writer-tier full suffixes and reader-tier trailing actions. A suffix a type does not declare is refused and sends OpenFGA nothing, so it adds no
    #    pair. (`transaction` never routes through here.)
    suffixes = (
        {s for m in fga_deps._OWNER_SUFFIX_RELATION.values() for s in m}
        | {s for m in fga_deps._WRITER_SUFFIX_RELATION.values() for s in m}
        | set(read_actions.META_READ_ACTIONS)
        | set(read_actions.DATA_READ_ACTIONS)
    )
    # `classification` returns from its own branch in `authorize` before `_action_relation` is consulted, so
    # enumerating the generic suffix map against it would assert relations the app can never send: it refuses every
    # suffix but `my-permissions`.
    for fga_type in set(fga_deps._FGA_TYPE.values()) - {"classification"}:
        for suffix in suffixes:
            for method in ("POST", "GET"):
                try:
                    relation = fga_deps._action_relation(fga_type, suffix, method=method)
                except InternalError:
                    continue
                pairs.add((fga_type, relation))

    # 2. Create-on-parent: the parent of a child nested in a namespace is that namespace, and the parent of a namespace
    #    directly in a warehouse is the warehouse, which the resolver governs a one-segment namespace name as — so each
    #    can_create_* must resolve on BOTH types.
    resolver = Resolver(cast("Request", None), settings)
    for resource in fga_deps._CREATE_ON_PARENT_SUFFIXES:
        nested = fga_deps._create_parent_rung(resource, ["acme-wh", "parent_ns", "child"])
        assert nested is not None
        pairs.add((_rung_type(nested), nested.relation))
        top = fga_deps._create_parent_rung(resource, ["acme-wh", "child"])
        assert top is not None and isinstance(top.on, write_judge.Named)
        warehouse = asyncio.run(resolver.object(top.on.kind, list(top.on.segments)))
        assert warehouse is not None and _obj_type(warehouse) == "warehouse", f"a create directly in a warehouse is asked on {warehouse!r}"
        # WHERE it is checked differs; WHICH permission it needs may not. A create directly in a warehouse is no licence
        # to ask for a weaker relation there: `can_get_metadata` resolves on the warehouse type perfectly well, so
        # asserting only that the pair RESOLVES cannot tell the tiers apart.
        assert top.relation == nested.relation, (
            f"the create in a warehouse for {resource!r} asks for {top.relation!r} while the nested create asks "
            f"for {nested.relation!r}: a warehouse parent must not downgrade the tier it demands"
        )
        pairs.add((_obj_type(warehouse), top.relation))

    # 3. Transactions (asked on the table their id names) and the batch routes: drive the real coroutines with a
    #    recording fga_reads.check / fga_reads.batch_check.
    checked: list[tuple[str, str]] = []

    async def rec_check(_client: object, *, user: str, relation: str, obj: str) -> bool:
        checked.append((_obj_type(obj), relation))
        return True

    async def rec_batch(_client: object, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        checked.extend((_obj_type(o), relation) for o in objects)
        return dict.fromkeys(objects, True)

    # Patch the fga module AS SEEN BY fga_deps (the consuming module), so the recording fakes
    # intercept the exact `fga_reads.check`/`fga_reads.batch_check` references the resolvers call.
    monkeypatch.setattr(fga_deps.fga_reads, "check", rec_check)
    monkeypatch.setattr(fga_deps.fga_reads, "batch_check", rec_batch)

    token = PersonPrincipal(principal=Principal(idp_id="dex", subject="alice"), claims=IDToken(iss="i", sub="alice", aud="lance", exp=1, iat=1))
    for suffix in ("describe", "alter"):
        asyncio.run(fga_deps._authorize_transaction(_CLIENT, NamedResolver(), ["acme-wh", "db1", "t", "txn1"], suffix, token=token))

    body = {
        "entries": [{"id": ["acme-wh", "db1", "users"]}],
        "operations": [
            {"declare_table": {"id": ["acme-wh", "db1", "new"]}},  # create-on-parent
            *[{op: {"id": ["acme-wh", "db1", "a"]}} for op in fga_deps._BATCH_OWNER_OPS],  # owner tier
            {"commit_table": {"id": ["acme-wh", "db1", "b"]}},  # generic writer tier
        ],
    }
    asyncio.run(fga_deps._authorize_batch(cast("Request", _FakeRequest(body)), _CLIENT, NamedResolver(), user="dex~alice"))

    # 4. A rename: the rung the router asks on its source and the one its door asks where the table lands, each within
    #    the source's namespace and into another.
    for destination in (["acme-wh", "db1", "renamed"], ["acme-wh", "db2", "child"]):
        pairs.add(("table", fga_deps._rename_relation(["acme-wh", "db1", "child"], destination)))
        asyncio.run(fga_deps.require_rename_destination(_CLIENT, settings, token, NamedResolver(), source=["acme-wh", "db1", "child"], destination=destination))

    pairs.update(checked)
    return pairs


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_every_relation_the_catalog_checks_exists_in_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """CONTRACT: every ``(type, relation)`` ``fga_deps`` can check is DEFINED on that type in
    ``model.json``. A phantom relation (e.g. the shipped ``namespace#can_write_data``) is a 400 from
    OpenFGA → fail-closed 503 for EVERY caller, so it must fail the suite loudly, right here."""
    model = _model_relations()
    pairs = _catalog_pairs(monkeypatch)
    assert pairs, "enumeration produced no pairs — the resolvers are not being driven"

    phantom = sorted(f"{t}#{r}" for t, r in pairs if t not in model or r not in model.get(t, set()))
    assert not phantom, (
        f"the app can check relations that do NOT exist in lakehouse/governed/auth/model.json: {phantom}. "
        "OpenFGA rejects these (relation not found) and lakehouse.governed.fga_resilience fails closed → 503 for every caller."
    )


#: alice's table and namespace, on which bob holds one rung when he sends a door's request.
_TIER_TABLE, _TIER_NAMESPACE = "lance_catalog$db$t", "lance_catalog$db"
_TIER_ROWS = encode_arrow_stream(pa.table({"id": pa.array([1, 2], pa.int64()), "v": pa.array([10, 20], pa.int64())}))
_TIER_QUERY = {"k": 10, "vector": {"single_vector": []}}

#: Each door whose tier is the claim: (the type bob is granted on, the rung that refuses him, the rung that admits him,
#: method, path, keywords). An owner-tier door refuses a writer, the rung below it; a read door admits a reader and refuses
#: a caller holding nothing.
_TIERED: list[Any] = [
    *[
        pytest.param("table", "writer", "owner", "POST", f"/management/v1/table/{_TIER_TABLE}/{suffix}", {"json": body}, id=f"table-{suffix}")
        for suffix, body in (("access/list", {}), ("access/check", {"user": "dex~carol", "relation": "can_read_data"}), ("access/graph", {}))
    ],
    *[
        pytest.param("namespace", "writer", "owner", "POST", f"/management/v1/namespace/{_TIER_NAMESPACE}/{suffix}", {"json": body}, id=f"namespace-{suffix}")
        for suffix, body in (("access/list", {}), ("access/check", {"user": "dex~carol", "relation": "can_get_metadata"}), ("access/graph", {}))
    ],
    pytest.param("table", "writer", "owner", "POST", f"/v1/table/{_TIER_TABLE}/branches/delete", {"json": {"name": "b"}}, id="branches-delete"),
    pytest.param("table", "writer", "owner", "POST", f"/v1/table/{_TIER_TABLE}/tags/delete", {"json": {"tag": "t"}}, id="tags-delete"),
    *[
        pytest.param("table", None, "reader", method, path, sent, id=name)
        for name, method, path, sent in (
            ("query", "POST", f"/v1/table/{_TIER_TABLE}/query", {"json": _TIER_QUERY}),
            ("count-rows", "POST", f"/v1/table/{_TIER_TABLE}/count_rows", {"json": {}}),
            ("explain-plan", "POST", f"/v1/table/{_TIER_TABLE}/explain_plan", {"json": {"query": _TIER_QUERY}}),
            ("analyze-plan", "POST", f"/v1/table/{_TIER_TABLE}/analyze_plan", {"json": _TIER_QUERY}),
            ("changes", "POST", f"/management/v1/table/{_TIER_TABLE}/changes", {"json": {"begin_version": 0, "end_version": 1}}),
            ("credentials", "POST", f"/management/v1/table/{_TIER_TABLE}/credentials", {"json": {}}),
            ("blobs", "GET", f"/management/v1/table/{_TIER_TABLE}/blobs?column=v&row=0", {}),
            ("describe", "POST", f"/v1/table/{_TIER_TABLE}/describe", {"json": {}}),
            ("stats", "POST", f"/v1/table/{_TIER_TABLE}/stats", {"json": {}}),
            ("tags-version", "POST", f"/v1/table/{_TIER_TABLE}/tags/version", {"json": {"tag": "t"}}),
            ("version-list", "POST", f"/v1/table/{_TIER_TABLE}/version/list", {"json": {}}),
            ("history", "GET", f"/management/v1/table/{_TIER_TABLE}/history", {}),
        )
    ],
]


@pytest.mark.parametrize(("on", "below", "rung", "method", "path", "sent"), _TIERED)
def test_a_door_admits_the_rung_its_tier_names_and_refuses_the_one_below(
    governed_catalog: Any,  # noqa: ANN401 — the catalog conftest's GovernedCatalog
    on: str,
    below: str | None,
    rung: str,
    method: str,
    path: str,
    sent: dict[str, Any],
) -> None:
    """CONTRACT (security, #51/#68): a door that discloses the authorization graph (``access/list``, ``access/check``,
    ``access/graph``) or destroys a table's versions or their pins (``branches/delete``, ``tags/delete``) clears the OWNER
    bar, so a writer is refused it; a door that returns a table's data or metadata is a READ, so a reader is admitted.

    Judged on the real model: bob holds exactly one rung on alice's object, granted through its own door. Measured on the
    deployed catalog 2026-09-11, with a writer on ``bronze$events`` holding no owner rung: ``branches/delete`` answered 422
    (past the gate) while ``tags/delete`` and ``deregister`` answered 403. Measured 2026-09-08 on ``changes`` at the writer
    rung: a change feed refused every reader who is not also a writer, its whole audience.
    """
    catalog, alice = governed_catalog, governed_catalog.bearer("alice")
    assert catalog.create_in_the_estate("alice", "db").status_code == 200
    created = catalog.client.post(f"/v1/table/{_TIER_TABLE}/create", content=_TIER_ROWS, headers={**alice, "Content-Type": ARROW_STREAM_MEDIA_TYPE})
    assert created.status_code == 200, created.text
    for ref, body in (("tags/create", {"tag": "t", "version": 1}), ("branches/create", {"name": "b"})):
        assert catalog.client.post(f"/v1/table/{_TIER_TABLE}/{ref}", json=body, headers=alice).status_code == 200
    grant_door = f"/management/v1/{on}/{_TIER_TABLE if on == 'table' else _TIER_NAMESPACE}/access/grant"

    def _as_bob_holding(relation: str | None) -> httpx.Response:
        if relation is not None:
            granted = catalog.client.post(grant_door, json={"user": "dex~bob", "relation": relation}, headers=alice)
            assert granted.status_code == 200, granted.text
        return catalog.client.request(method, path, headers=catalog.bearer("bob"), **sent)

    refused = _as_bob_holding(below)
    assert (refused.status_code, refused.json()["code"]) in {(403, 15), (404, 4)}, f"{path} admitted bob holding {below!r}: {refused.text}"
    assert below is None or refused.status_code == 403, f"{path} hid itself from bob holding {below!r}: {refused.text}"
    admitted = _as_bob_holding(rung)
    assert admitted.status_code not in {401, 403, 404}, f"{path} refused bob holding {rung!r}: {admitted.status_code} {admitted.text}"


def _types_served_by(suffix: str) -> set[str]:
    """The FGA types whose ``<type>_router`` in the access endpoints carries a route ending in ``suffix``."""
    from catalog.api.v1.endpoints import access as access_module

    return {
        name.removesuffix("_router")
        for name, obj in vars(access_module).items()
        if name.endswith("_router") and any(r.path.endswith(f"/{suffix}") for r in obj.routes)
    }


def test_grant_routes_are_intercepted_before_the_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """CONTRACT (security, #72 + the grant axis): ``access/grant`` / ``access/revoke`` are authorized
    PER RUNG from the request body by ``_authorize_grant``, not by the suffix map.

    This test replaced an assertion that they resolve to the owner tier. That assertion was right while
    granting was welded to ownership and is wrong now — but deleting it would have removed the guard it
    existed to be. Neither suffix is declared in a rung map, so without the interception every grant
    and revoke would be refused for every caller, owners included. So the guard is re-pointed, not
    dropped. It now pins the interception itself, which is the thing that must not regress.

    ``authorize`` returns inside the ``_GRANT_SUFFIXES`` branch before ``_action_relation`` is ever
    consulted. Three things are asserted here: the suffixes are declared intercepted, every rung the grant API accepts
    has a ``can_grant_*`` gate to be checked against, and every type a revoke route serves defines the relation the
    revoke gate checks on it. A rung without a grant gate is refused by ``_authorize_grant``, so it cannot silently
    open; it would simply make the rung ungrantable. A type without the revoke gate's relation fails the other way:
    OpenFGA answers a check on a relation the type does not define with a 400, which fails closed to a 503 for every
    revoke there, the curator's included."""
    from catalog.api.fga_deps import _GRANT_SUFFIXES, _grant_actions
    from catalog.api.v1.endpoints.access import _grantable_relations

    assert frozenset({"access/grant", "access/revoke"}) == _GRANT_SUFFIXES
    # WHAT THE DOOR ADMITS, on the TYPES THE DOOR SERVES — and neither half is a hand-kept list any
    # more. They stopped agreeing when `_GRANTABLE_BASE` grew type-specific rungs ([[LH-055]]:
    # `classifier` on the data types, `apply` on `classification` alone), so iterating the base tuple
    # against three named types began asking `warehouse` about a rung only `classification` defines.
    #
    # The SUBJECTS come from the routes that exist, because a rung is only grantable if something
    # serves it: `estate` declares `owner` and has no grant route, so demanding
    # `can_grant_owner` there would be a gate on an unreachable door. A new `<type>_router` carrying
    # `/access/grant` is covered the day it is added, which is the property a list cannot have.
    served = _types_served_by("access/grant")
    assert served == {"table", "namespace", "warehouse", "project", "classification"}, f"the grant doors changed: {sorted(served)}"
    for fga_type in sorted(served):
        grantable = _grantable_relations(fga_type)
        assert grantable, f"{fga_type} has a grant route and no grantable rung — the door admits nothing"
        actions = _grant_actions(fga_type)
        for rung in grantable:
            assert f"can_grant_{rung}" in actions, f"{fga_type}: {rung} is grantable with no can_grant_{rung} gate"

    # THE REVOKE HALF IS DRIVEN, not restated: the gate asks one revoke permission on every type it serves, so the
    # relation is read off the gate itself, for every rung the door admits. A grant door with no revoke door beside it
    # would hand out grants nothing could take back.
    revoked = _types_served_by("access/revoke")
    assert revoked == served, f"each grant door needs its revoke door: grant serves {sorted(served)}, revoke {sorted(revoked)}"
    checked: set[tuple[str, str]] = set()

    async def rec_check(_client: object, *, user: str, relation: str, obj: str) -> bool:
        checked.add((_obj_type(obj), relation))
        return True

    monkeypatch.setattr(fga_deps.fga_reads, "check", rec_check)
    for fga_type in sorted(revoked):
        for rung in _grantable_relations(fga_type):
            request = cast("Request", _FakeRequest({"user": "dex~bob", "relation": rung}))
            asyncio.run(fga_deps._authorize_grant(request, _CLIENT, NamedResolver(), user="dex~alice", fga_type=fga_type, segments=["x"], revoking=True))
    model = _model_relations()
    undefined = sorted(f"{t}#{r}" for t, r in checked if r not in model[t])
    assert not undefined, f"the revoke gate checks {undefined}, which the model does not define: OpenFGA refuses each, and every revoke there answers 503"


# --------------------------------------------------------------------------- #
# diff2 F10 item 9 — every shape where a governed OBJECT is the user on another object
# --------------------------------------------------------------------------- #

#: Shapes that place a governed object in the USER position, and how each one gets cleaned up when
#: that object is dropped. `revoke_object_tuples` reads tuples BY OBJECT, so a tuple whose *user* is
#: the dropped object is invisible to it — it reconstructs exactly ONE such tuple, the inverse
#: `child` edge, from the `parent` relation it just read.
#:
#: value = why this shape does not leave a dangling tuple on drop.
_OBJECT_AS_USER_SHAPES: dict[tuple[str, str, str], str] = {
    # The parent/child pairs: `revoke_object_tuples` rebuilds `<obj> child <parent>` from the
    # `parent` tuple, which is the one case it explicitly handles.
    ("namespace", "child", "namespace"): "inverse child edge, reconstructed on revoke",
    ("namespace", "child", "table"): "inverse child edge, reconstructed on revoke",
    ("namespace", "child", "materialized_view"): "inverse child edge, reconstructed on revoke",
    ("warehouse", "child", "namespace"): "inverse child edge, reconstructed on revoke",
    # The forward `parent` edges live ON the dropped object, so a read-by-object sees them.
    ("namespace", "parent", "namespace"): "tuple's object IS the dropped object",
    ("namespace", "parent", "warehouse"): "tuple's object IS the dropped object",
    ("table", "parent", "namespace"): "tuple's object IS the dropped object",
    ("materialized_view", "parent", "namespace"): "tuple's object IS the dropped object",
    # A tag's parent edge ([[LH-055]]). Same shape as the rows above — dropping a TAG reads by object
    # and finds it — and the estate side cannot dangle for a second reason: there is exactly one
    # `estate:` object, seeded at bootstrap, and no door deletes it.
    ("classification", "parent", "estate"): "tuple's object IS the dropped object; the estate is never dropped",
    # A project's admin revision ([[LH-434]]): `revision` has no relations and no door makes or drops one, and the
    # project's delete removes the tuple with every other tuple on the project.
    ("project", "admin_revision", "revision"): "revision is a bookkeeping subject, not a droppable object",
    ("namespace", "managed_access", "role"): "role is a subject type, not a droppable object",
    ("warehouse", "managed_access", "role"): "role is a subject type, not a droppable object",
    # A project cannot be deleted while a warehouse claims it — `projects.py`'s emptiness refusal
    # 409s naming every held warehouse, and a warehouse's own delete revokes this edge with it.
    ("warehouse", "project", "project"): "unreachable: project delete refuses while warehouses exist",
    # A project's delete ends each of its roles, every tuple on and naming the role with it, this edge included
    # ([[LH-557]]); a role the raw tuple editor scoped stays pointing at its gone project, which the grant door
    # reads to refuse it everywhere rather than as estate-wide.
    ("role", "project", "project"): "project delete ends its roles",
}


def test_no_new_object_as_user_shape_slips_past_revoke() -> None:
    """A new model shape that puts an object in the USER position must force a revoke review.

    `revoke_object_tuples` deletes every tuple whose OBJECT is the dropped object, plus the single
    inverse `child` edge it can reconstruct. Any OTHER shape where the dropped object appears as the
    *user* survives the drop — a dangling grant or edge pointing at something that no longer exists,
    which is the stale-tuple privilege-bleed class the revoke exists to prevent.

    The model cannot express "clean this up on delete", so this test is the tripwire: adding such a
    shape fails HERE, at the point of the model edit, with the reason it must be triaged. Extending
    the dict is a fine resolution — but only after deciding, and recording, which of the four
    categories the new shape falls into.
    """
    model = fga_model.load_model()
    governed = {td["type"] for td in model["type_definitions"]} - {"user"}
    found: set[tuple[str, str, str]] = set()
    for td in model["type_definitions"]:
        for relation, info in ((td.get("metadata") or {}).get("relations") or {}).items():
            for direct in info.get("directly_related_user_types") or []:
                # `relation`-qualified (`role#assignee`) and wildcard entries are usersets/public
                # grants, not an object standing in the user position.
                if direct.get("type") in governed and not direct.get("relation") and not direct.get("wildcard"):
                    found.add((td["type"], relation, str(direct["type"])))

    unreviewed = found - set(_OBJECT_AS_USER_SHAPES)
    assert not unreviewed, (
        f"new object-as-user shape(s) {sorted(unreviewed)} — a dropped object in the USER position is "
        "invisible to revoke_object_tuples (it reads BY OBJECT). Decide how each is cleaned up, then "
        "add it to _OBJECT_AS_USER_SHAPES with that reason."
    )
    # And the reverse: a shape removed from the model must not linger here pretending to be reviewed.
    stale = set(_OBJECT_AS_USER_SHAPES) - found
    assert not stale, f"_OBJECT_AS_USER_SHAPES lists {sorted(stale)}, which the model no longer defines"


#: The (type, relation) pairs a TIME-BOXED grant may be written against — every direct type
#: restriction carrying the `non_expired_grant` condition.
#:
#: The set is exactly the two data rungs (`reader`, `writer`) plus the materialized
#: view's `refresher`, plus `maintainer`, and its shape is the invariant: **ownership and grant
#: AUTHORITY are never time-boxable.**
#:
#: `maintainer` qualifies on the rule this list encodes rather than by resemblance to its neighbours,
#: and the rule is "what does the expiry LEAVE BEHIND". An expired maintainer leaves a dataset
#: unmaintained — it stops being compacted, its versions stop being reclaimed — which is the fail-safe
#: direction and reversible by re-granting. Nothing is stranded, nothing becomes unrevocable, and no
#: caller loses an ability it needs to clean up. It is also the rung that most WANTS an expiry: a
#: one-off compaction campaign should lapse on its own rather than depend on someone remembering to
#: revoke it (owner ruling 2026-09-08: zero trust). An expiring `owner` leaves the object with no owner at all — invisible to every
#: list (per-item filtering) and undroppable by every caller including an estate admin, which is the
#: stranded-object class the estate has spent real effort closing. An expiring `manage_grants` or
#: `pass_grants` is the same failure one axis over: the delegation lapses while the grants it issued
#: stand, and nobody is left able to revoke them.
#:
#: `tagger` qualifies on the same rule, decided 2026-09-10 when the rung was added. An expired
#: tagger leaves each tag exactly where it was last moved: the version that was tagged
#: stays tagged, consumers keep resolving it, and nothing is stranded, unlistable or unrevocable. The
#: only loss is the ability to tag the NEXT version, which is the fail-safe direction and is
#: repairable by an owner moving the tag or by re-granting. It wants an expiry for the same reason
#: `maintainer` does: a campaign scoped to one migration should lapse on its own.
#:
#: `classifier` qualifies, and it is the CLEAREST case of the rule yet ([[LH-058]], 2026-09-22). What an
#: expiry leaves behind is a table whose classification STANDS: the label lives in Lance field metadata
#: and does not lapse with the grant, so the table stays refused for raw credential vending exactly as
#: before. The only thing that lapses is the ability to change a label — including the ability to REMOVE
#: one, which is the direction that would weaken the estate. So an expired classifier leaves data more
#: protected rather than less, strands nothing, and makes nothing unrevocable: an owner still holds
#: `can_classify` concentrically and can relabel or re-grant. It also wants an expiry more than most —
#: a data-protection review is exactly the kind of engagement that should lapse on its own rather than
#: leave a standing capability over data its holder may not read.
_CONDITIONAL_GRANT_RUNGS: frozenset[tuple[str, str]] = frozenset(
    {
        ("warehouse", "reader"),
        ("warehouse", "writer"),
        ("namespace", "reader"),
        ("namespace", "writer"),
        ("table", "reader"),
        ("table", "writer"),
        ("materialized_view", "reader"),
        ("materialized_view", "refresher"),
        ("warehouse", "maintainer"),
        ("namespace", "maintainer"),
        ("table", "maintainer"),
        ("warehouse", "tagger"),
        ("namespace", "tagger"),
        ("table", "tagger"),
        ("warehouse", "classifier"),
        ("namespace", "classifier"),
        ("table", "classifier"),
        # [[LH-055]] — the per-tag delegation. WHAT THE EXPIRY LEAVES BEHIND is nothing, which is why
        # it is safe here and why it is the rung most worth time-boxing: `apply` grants the power to
        # ATTACH a label, never to remove one, so a lapsed grant strands no object in a state its
        # holder can no longer leave. The labels an expired classifier applied stay applied, and
        # un-labelling is a separate action gated on `can_classify` at the table.
        ("classification", "apply"),
    }
)


def test_only_the_data_rungs_accept_a_TIME_BOXED_grant() -> None:
    """Adding `user with non_expired_grant` to a rung must be a decision, not a diff nobody notices.

    This is a STRUCTURAL tripwire because no behavioural test can be one. A type restriction is
    ADDITIVE — it permits new tuples and changes the resolution of not a single existing one — so
    widening `namespace#owner` to accept a conditional grant leaves every check in `model.fga.yaml`
    answering exactly as before. Measured: that mutation passes the whole suite, before and after the
    fixtures were made mutation-sensitive.

    What it would cost is not visible until the clock runs out. A time-boxed OWNER expires into an
    object with no owner — not listed (the listings filter per item), not droppable at any tier, not
    repairable except by hand-writing tuples. The model cannot express "and then what?", so the
    guard has to live here, at the point of the edit, with the reason attached.

    Extending `_CONDITIONAL_GRANT_RUNGS` is a fine resolution — after deciding what an expiry leaves
    behind on that rung, and recording it.
    """
    model = fga_model.load_model()
    found = {
        (td["type"], relation)
        for td in model["type_definitions"]
        for relation, info in ((td.get("metadata") or {}).get("relations") or {}).items()
        for direct in info.get("directly_related_user_types") or []
        if direct.get("condition")
    }

    widened = found - _CONDITIONAL_GRANT_RUNGS
    assert not widened, (
        f"{sorted(widened)} now accept a TIME-BOXED grant. Ownership and grant authority must not "
        "expire: an expired owner leaves the object unlistable and undroppable by everyone, and an "
        "expired manage_grants/pass_grants leaves the grants it issued with nobody able to revoke "
        "them. Decide what the expiry leaves behind, then add the rung here with that reason."
    )
    # The reverse, matching `_OBJECT_AS_USER_SHAPES`: a rung removed from the model must not linger
    # here pretending to be reviewed.
    stale = _CONDITIONAL_GRANT_RUNGS - found
    assert not stale, f"_CONDITIONAL_GRANT_RUNGS lists {sorted(stale)}, which the model no longer defines"
