"""Every (object_type, relation) the app can CHECK must exist in the compiled OpenFGA model.

The bug class this exists to make unshippable: the app checks a ``can_*`` relation that the target
type does not define. OpenFGA answers that with a 400 (``relation 'namespace#can_write_data' not
found``, error code 2000) — an ``ApiException`` that is NOT retryable, so ``service_kit.governed.fga.check``'s
fail-closed handler converts it to ``ServiceUnavailableError`` → **HTTP 503 for every caller, owners
included**, logged as ``openfga_check_unavailable`` (an outage, which it is not). It is a total
outage of the route that masquerades as an infra blip. That is exactly how the namespaced
transaction ``alter`` path (``namespace#can_write_data``, a table-only action) shipped broken.

Mocked authz tests cannot catch it: they monkeypatch ``fga.check`` and assert the relation string
the app passed, which passes happily against a phantom relation. The ``.fga.yaml`` tests cannot
catch it either: they assert whatever (object, relation) pairs the *test author* chose, which may
not be the pairs the *app* sends (the yaml exercised ``transaction:``; the app checks
``namespace:``). Only a cross-check of the app's own mapping tables against the compiled model
closes it — so this test enumerates the pairs by DRIVING the real resolvers in
``catalog.api.fga_deps`` (never a hand-copied list, which would drift) and asserts each pair
resolves in ``service_kit/governed/auth/model.json``, the file the app actually loads.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import Request
from lance_namespace import InternalError
from openfga_sdk import OpenFgaClient

from catalog.api import fga_deps
from catalog.core.config import Settings
from service_kit.governed import fga as fga_module
from service_kit.governed.oidc import IDToken


REPO_ROOT = Path(__file__).resolve().parents[2]

#: Sentinel FGA client. The resolvers only ever hand it to ``fga.check``/``fga.batch_check``, which the
#: recording fakes below replace — so nothing ever calls a method on it, and the cast is safe.
_CLIENT = cast("OpenFgaClient", object())


# --------------------------------------------------------------------------- #
# The compiled model (what the app LOADS) -> {type: {relation, ...}}
# --------------------------------------------------------------------------- #


def _model_relations() -> dict[str, set[str]]:
    """``{object_type: {relation, ...}}`` parsed from the compiled ``model.json``."""
    model = fga_module.load_model()
    return {td["type"]: set(td.get("relations") or {}) for td in model["type_definitions"]}


# --------------------------------------------------------------------------- #
# Enumerate every (type, relation) pair the CATALOG can check — by driving fga_deps
# --------------------------------------------------------------------------- #


def _settings(*, lock_root: bool) -> Settings:
    # model_validate (not Settings(...)) so field-name keys validate cleanly — the fields carry
    # LANCE_* aliases. OIDC is required whenever FGA is on (authz needs an authenticated user).
    return Settings.model_validate(
        {
            "oidc_enabled": True,
            "oidc_issuer": "https://idp.example",
            "oidc_audience": "lance",
            "fga_enabled": True,
            "fga_api_url": "http://openfga:8080",
            "fga_lock_root_create": lock_root,
            "s3_access_key_id": "x",
            "s3_secret_access_key": "x",
        }
    )


class _FakeRequest:
    """The only thing ``_authorize_batch`` touches on the Request is ``await request.json()``."""

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    async def json(self) -> dict[str, Any]:
        return self._body


def _obj_type(obj: str) -> str:
    return obj.split(":", 1)[0]


def _catalog_pairs(monkeypatch: pytest.MonkeyPatch) -> set[tuple[str, str]]:
    """Every ``(object_type, relation)`` ``catalog.api.fga_deps`` can send to OpenFGA.

    Driven through the REAL mapping tables + resolvers, so a future edit to ``_action_relation`` /
    ``_OWNER_SUFFIX_RELATION`` / ``_BATCH_OWNER_OPS`` / ``_create_parent_check`` /
    ``_authorize_transaction`` is picked up here automatically instead of drifting from a copy.
    """
    pairs: set[tuple[str, str]] = set()
    settings = _settings(lock_root=False)
    root_settings = _settings(lock_root=True)

    # 1. Non-create ops: _action_relation(type, suffix) for EVERY guarded type x EVERY suffix the rung
    #    maps declare — owner- and writer-tier full suffixes, reader-tier and maintenance trailing
    #    actions. A suffix a type does not declare is refused and sends OpenFGA nothing, so it adds no
    #    pair. (`transaction` never routes through here.)
    suffixes = (
        {s for m in fga_deps._OWNER_SUFFIX_RELATION.values() for s in m}
        | {s for m in fga_deps._WRITER_SUFFIX_RELATION.values() for s in m}
        | set(fga_deps._META_READ_ACTIONS)
        | set(fga_deps._DATA_READ_ACTIONS)
        | set(fga_deps._MAINTENANCE_ACTIONS)
    )
    # `transaction` and `classification` both return from their own branch in `authorize` before
    # `_action_relation` is consulted, so enumerating the generic suffix map against them would assert
    # relations the app can never send. `classification` refuses every suffix but `my-permissions`.
    for fga_type in set(fga_deps._FGA_TYPE.values()) - {"transaction", "classification"}:
        for suffix in suffixes:
            try:
                relation = fga_deps._action_relation(fga_type, suffix)
            except InternalError:
                continue
            pairs.add((fga_type, relation))

    # 2. Create-on-parent: the parent is a namespace for a NESTED child, and the configured root
    #    object (a `warehouse:`) for a TOP-LEVEL child when fga_lock_root_create is on — so each
    #    can_create_* must resolve on BOTH types.
    for resource in fga_deps._CREATE_ON_PARENT_SUFFIXES:
        nested = fga_deps._create_parent_check(resource, ["parent_ns", "child"], settings)
        assert nested is not None
        pairs.add((_obj_type(nested[0]), nested[1]))
        top = fga_deps._create_parent_check(resource, ["top_level_child"], root_settings)
        assert top is not None, "fga_lock_root_create must gate a top-level create on the root object"
        # WHERE it is checked may change; WHICH permission it needs may not. `lockRootCreate` moves the
        # check from the parent namespace to the root object — it is not a licence to ask for a weaker
        # relation there. Asserting only `is not None` and that the pair RESOLVES (which is all this
        # test did until 2026-08-22) cannot tell the tiers apart: `can_get_metadata` resolves on the
        # root type perfectly well. Measured — swapping this branch to the reader-tier
        # `can_get_metadata` left 3,125 tests passing, and `chart/values-prod.yaml:22` ships
        # `lockRootCreate: true` as the ONLY thing stopping any authenticated token from minting
        # top-level namespaces and tables in production.
        assert top[1] == nested[1], (
            f"the locked-root create for {resource!r} asks for {top[1]!r} while the nested create asks "
            f"for {nested[1]!r} — locking the root must not downgrade the tier it demands"
        )
        pairs.add((_obj_type(top[0]), top[1]))

    # 3. Transactions (parent-scoped for a namespaced id, object-scoped for an opaque one) and the
    #    batch routes: drive the real coroutines with a recording fga.check / fga.batch_check.
    checked: list[tuple[str, str]] = []

    async def rec_check(_client: object, *, user: str, relation: str, obj: str) -> bool:
        checked.append((_obj_type(obj), relation))
        return True

    async def rec_batch(_client: object, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        checked.extend((_obj_type(o), relation) for o in objects)
        return dict.fromkeys(objects, True)

    # Patch the fga module AS SEEN BY fga_deps (the consuming module), so the recording fakes
    # intercept the exact `fga.check`/`fga.batch_check` references the resolvers call.
    monkeypatch.setattr(fga_deps.fga, "check", rec_check)
    monkeypatch.setattr(fga_deps.fga, "batch_check", rec_batch)

    for segments in (["db1", "txn1"], ["txn1"]):  # namespaced (parent-scoped) + opaque (object-scoped)
        for suffix in ("describe", "alter"):
            asyncio.run(fga_deps._authorize_transaction(_CLIENT, settings, segments, suffix, user="alice"))

    body = {
        "entries": [{"id": ["db1", "users"]}],
        "operations": [
            {"declare_table": {"id": ["db1", "new"]}},  # create-on-parent
            *[{op: {"id": ["db1", "a"]}} for op in fga_deps._BATCH_OWNER_OPS],  # owner tier
            {"commit_table": {"id": ["db1", "b"]}},  # generic writer tier
        ],
    }
    asyncio.run(fga_deps._authorize_batch(cast("Request", _FakeRequest(body)), _CLIENT, settings, user="alice"))

    # 4. The endpoint-level gates that check outside `authorize` (Overwrite / rename-into-namespace).
    token = cast("IDToken", _Token())
    asyncio.run(fga_deps.require_can_drop_table(_CLIENT, settings, token, segments=["db1", "t"]))
    for resource in ("table", "namespace", "materialized_view"):
        asyncio.run(fga_deps.require_create_on_parent(_CLIENT, settings, token, resource=resource, segments=["db1", "child"]))

    pairs.update(checked)
    return pairs


class _Token:
    sub = "alice"


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
        f"the app can check relations that do NOT exist in service_kit/governed/auth/model.json: {phantom}. "
        "OpenFGA rejects these (relation not found) and service_kit.governed.fga fails closed → 503 for every caller."
    )


def test_chart_medallion_required_actions_exist_on_namespace() -> None:
    """CONTRACT: every ``requiredAction`` the chart configures for a medallion stage runner is a real
    ``namespace`` relation (the stage runner checks it on ``namespace:<toNamespace>``). A typo here would
    503 the whole cascade at the FGA gate, not deny it."""
    namespace_relations = _model_relations()["namespace"]
    actions = {
        m
        for values in ("values.yaml", "values-prod.yaml")
        for m in re.findall(r"requiredAction:\s*['\"]?([A-Za-z_]+)", (REPO_ROOT / "chart" / values).read_text())
    }
    assert actions, "no requiredAction found in the chart — the regex drifted"
    phantom = sorted(actions - namespace_relations)
    assert not phantom, f"chart requiredAction(s) not defined on the `namespace` type: {phantom}"


def test_transaction_alter_checks_a_real_namespace_writer_relation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONTRACT (the regression this file was born from): a namespaced txn ``alter`` is checked
    against the enclosing ``namespace:`` at the WRITER rung, using a relation the ``namespace`` type
    actually defines. ``can_write_data`` is a TABLE-only action — checking it on a namespace 400s."""
    checked: list[tuple[str, str]] = []

    async def rec_check(_c: object, *, user: str, relation: str, obj: str) -> bool:
        checked.append((obj, relation))
        return True

    monkeypatch.setattr(fga_deps.fga, "check", rec_check)
    asyncio.run(fga_deps._authorize_transaction(_CLIENT, _settings(lock_root=False), ["db1", "txn1"], "alter", user="alice"))

    obj, relation = checked[-1]
    assert obj == "namespace:db1"  # parent-scoped, not a per-txn object nothing seeds
    assert relation in _model_relations()["namespace"], f"namespace#{relation} does not exist"
    assert relation == "can_update_properties"  # the namespace WRITER rung (can_write_data is table-only)


def test_access_disclosure_routes_are_owner_tier() -> None:
    """CONTRACT (security, #51/#68): the access-DISCLOSURE routes — ``access/list`` (enumerate who holds
    access), ``access/check`` (simulate an arbitrary (user, relation)) and ``access/graph`` — must clear
    the OWNER bar, never the writer rung. They reveal the authz graph; a mere writer must not reach them.

    The pair-existence contract above canNOT catch a downgrade here: moving a suffix from the owner map to
    the writer map resolves ``can_write_data`` — a real relation, so that test stays green while the gate
    quietly weakens. This asserts the resolved tier directly, so such a refactor fails loudly."""
    from catalog.api.fga_deps import _action_relation

    for suffix in ("access/list", "access/check", "access/graph"):
        assert _action_relation("table", suffix) == "can_drop", suffix
        assert _action_relation("namespace", suffix) == "can_delete", suffix


@pytest.mark.parametrize("suffix", ["branches/delete", "tags/delete"])
def test_a_destructive_door_is_owner_tier(suffix: str) -> None:
    """CONTRACT (security): a door that destroys a table's versions or unpins them clears the OWNER bar
    (``can_drop``), the same bar as ``maintenance/run``, which reclaims old versions and EXEMPTS tagged ones.

    Undeclared a suffix is refused for every caller; on the writer map a plain data writer clears it.
    ``tags/delete`` at the writer rung removes the pin that defeats the owner-tier rollback guard
    (publication refuses to move ``published`` backwards, so a writer deletes the tag and republishes at
    an older version). ``published`` is the estate's serving pointer, and Lance keeps the CAS in the
    object store, so the pointer is a tag INSIDE the dataset and authorization carries the weight a
    catalog transaction would carry elsewhere. A branch FORKS HISTORY (``branches/create``), so refusing a
    writer the pointer's delete while permitting the fork's is incoherent.

    Measured on the deployed catalog 2026-09-11, with a writer on ``bronze$events`` holding no owner rung:
    ``branches/delete`` answered 422 (cleared authz, failed body validation) while ``tags/delete`` and
    ``deregister`` answered 403.
    """
    from catalog.api.fga_deps import _action_relation

    relation = _action_relation("table", suffix)

    assert relation == "can_drop", f"{suffix!r} resolved to {relation!r}: a destructive door clears the owner bar, never the writer rung"
    assert relation == _action_relation("table", "maintenance/run"), (
        f"{suffix!r} is gated below the tag-respecting reclamation: the unguarded door must not be the cheaper one"
    )


def test_every_DATA_READ_door_is_gated_as_a_READ() -> None:
    """CONTRACT (security): a door that returns table DATA is authorized with ``can_read_data``.

    A read door is a read only when a read vocabulary names it: undeclared it is refused for every
    caller, and at the writer rung it refuses every reader who is not also a writer — for a change feed,
    its whole audience — while the audit trail describes a write that never happened. Measured
    2026-09-08 on `POST /{id}/changes` at the writer rung: `can_write_data ALLOW` on
    `table:bronze$pages` beside the `read_data` record for the same call.

    Asserting the SET is not enough — membership can be true while `_action_relation` routes elsewhere —
    so this resolves each one through the real classifier.
    """
    from catalog.api.fga_deps import _DATA_READ_ACTIONS, _META_READ_ACTIONS, _action_relation

    assert "changes" in _DATA_READ_ACTIONS, "the change feed is the most disclosing read a table has"
    for action in _DATA_READ_ACTIONS:
        assert _action_relation("table", action) == "can_read_data", action

    # `history`'s endpoint runs its own `require_can_get_metadata`, a check that runs SECOND: any higher
    # rung the router asks first leaves it reachable only by callers who already cleared that bar.
    # Measured 2026-09-09 at the writer rung: one `GET /history` logged `can_write_data ALLOW` and then
    # `can_get_metadata ALLOW`, in that order.
    assert "history" in _META_READ_ACTIONS, "a commit log is metadata about the data, not a write"
    for action in _META_READ_ACTIONS:
        assert _action_relation("table", action) == "can_get_metadata", action


def test_grant_routes_are_intercepted_before_the_resolver() -> None:
    """CONTRACT (security, #72 + the grant axis): ``access/grant`` / ``access/revoke`` are authorized
    PER RUNG from the request body by ``_authorize_grant``, not by the suffix map.

    This test replaced an assertion that they resolve to the owner tier. That assertion was right while
    granting was welded to ownership and is wrong now — but deleting it would have removed the guard it
    existed to be. Neither suffix is declared in a rung map, so without the interception every grant
    and revoke would be refused for every caller, owners included. So the guard is re-pointed, not
    dropped. It now pins the interception itself, which is the thing that must not regress.

    ``authorize`` returns inside the ``_GRANT_SUFFIXES`` branch before ``_action_relation`` is ever
    consulted. Both halves are asserted here — that the suffixes are declared intercepted, AND that every rung the grant API
    accepts has a ``can_grant_*`` gate to be checked against. A rung without one would be refused by
    ``_authorize_grant``, so this cannot silently open; it would simply make the rung ungrantable."""
    from catalog.api.fga_deps import _GRANT_SUFFIXES, _grant_actions
    from catalog.api.v1.endpoints import access as access_module
    from catalog.api.v1.endpoints.access import _grantable_relations

    assert frozenset({"access/grant", "access/revoke"}) == _GRANT_SUFFIXES
    # WHAT THE DOOR ADMITS, on the TYPES THE DOOR SERVES — and neither half is a hand-kept list any
    # more. They stopped agreeing when `_GRANTABLE_BASE` grew type-specific rungs ([[LH-055]]:
    # `classifier` on the data types, `apply` on `classification` alone), so iterating the base tuple
    # against three named types began asking `warehouse` about a rung only `classification` defines.
    #
    # The SUBJECTS come from the routes that exist, because a rung is only grantable if something
    # serves it: `estate` declares `owner`/`writer`/`reader` and has no grant route, so demanding
    # `can_grant_owner` there would be a gate on an unreachable door. A new `<type>_router` carrying
    # `/access/grant` is covered the day it is added, which is the property a list cannot have.
    served = {
        name.removesuffix("_router")
        for name, obj in vars(access_module).items()
        if name.endswith("_router") and any(r.path.endswith("/access/grant") for r in obj.routes)
    }
    assert served == {"table", "namespace", "classification"}, f"the grant doors changed: {sorted(served)}"
    for fga_type in sorted(served):
        grantable = _grantable_relations(fga_type)
        assert grantable, f"{fga_type} has a grant route and no grantable rung — the door admits nothing"
        actions = _grant_actions(fga_type)
        for rung in grantable:
            assert f"can_grant_{rung}" in actions, f"{fga_type}: {rung} is grantable with no can_grant_{rung} gate"


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
    # The forward `parent`/`tenant` edges live ON the dropped object, so a read-by-object sees them.
    ("namespace", "parent", "namespace"): "tuple's object IS the dropped object",
    ("namespace", "parent", "warehouse"): "tuple's object IS the dropped object",
    ("table", "parent", "namespace"): "tuple's object IS the dropped object",
    ("materialized_view", "parent", "namespace"): "tuple's object IS the dropped object",
    ("transaction", "parent", "namespace"): "tuple's object IS the dropped object",
    ("transaction", "parent", "warehouse"): "tuple's object IS the dropped object",
    # A tag's parent edge ([[LH-055]]). Same shape as the rows above — dropping a TAG reads by object
    # and finds it — and the estate side cannot dangle for a second reason: there is exactly one
    # `estate:` object, seeded at bootstrap, and no door deletes it.
    ("classification", "parent", "estate"): "tuple's object IS the dropped object; the estate is never dropped",
    # Subject types, not governed objects with a lifecycle of their own — nothing drops them.
    ("project", "team", "team"): "team is a subject type, not a droppable object",
    ("namespace", "managed_access", "role"): "role is a subject type, not a droppable object",
    ("warehouse", "managed_access", "role"): "role is a subject type, not a droppable object",
    # A project cannot be deleted while a warehouse claims it — `projects.py`'s emptiness refusal
    # 409s naming every held warehouse, and a warehouse's own delete revokes this edge with it.
    ("warehouse", "project", "project"): "unreachable: project delete refuses while warehouses exist",
    # THE ONE THAT GENUINELY SURVIVES. Deleting a project leaves `annotation_project:X#tenant@project:Y`
    # pointing at a project that is gone. Known, and reported by the reconciler as drift rather than
    # repaired — the annotator owns that object and the catalog's revoke does not reach across.
    ("annotation_project", "tenant", "project"): "KNOWN RESIDUAL: reconciler-reported drift",
    # A role scoped to a deleted project keeps pointing at it, and that is the SAFE direction rather
    # than a leak: the grant door reads this edge to refuse a cross-tenant grant, so a role whose
    # tenant is gone stops being grantable anywhere instead of becoming estate-wide. Repairing it
    # would have to decide whether such a role is re-homed or retired, which is a lifecycle question
    # nothing else in the model answers yet.
    ("role", "project", "project"): "KNOWN RESIDUAL: fails CLOSED at the grant door",
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
    model = fga_module.load_model()
    governed = {td["type"] for td in model["type_definitions"]} - {"user"}
    found: set[tuple[str, str, str]] = set()
    for td in model["type_definitions"]:
        for relation, info in ((td.get("metadata") or {}).get("relations") or {}).items():
            for direct in info.get("directly_related_user_types") or []:
                # `relation`-qualified (`team#member`) and wildcard entries are usersets/public
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
#: The set is exactly the three data rungs (`reader`, `writer`, `validator`) plus the materialized
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
#: `publisher` qualifies on the same rule, decided 2026-09-10 when the rung was added. An expired
#: publisher leaves the `published` tag exactly where it was last moved: the version that was blessed
#: stays blessed, consumers keep resolving it, and nothing is stranded, unlistable or unrevocable. The
#: only loss is the ability to bless the NEXT version, which is the fail-safe direction and is
#: repairable by an owner moving the tag or by re-granting. It wants an expiry for the same reason
#: `maintainer` does — a promotion campaign scoped to one migration should lapse on its own.
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
        ("warehouse", "validator"),
        ("namespace", "reader"),
        ("namespace", "writer"),
        ("namespace", "validator"),
        ("table", "reader"),
        ("table", "writer"),
        ("table", "validator"),
        ("materialized_view", "reader"),
        ("materialized_view", "refresher"),
        ("warehouse", "maintainer"),
        ("namespace", "maintainer"),
        ("table", "maintainer"),
        ("warehouse", "publisher"),
        ("namespace", "publisher"),
        ("table", "publisher"),
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
    model = fga_module.load_model()
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
