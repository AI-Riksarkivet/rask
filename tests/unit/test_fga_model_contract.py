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

Also guards the 3-copy model sync (model.fga authored / model.fga.yaml tested / model.json loaded)
at the type+relation level, so a relation added to one copy but not the others fails here rather
than only in CI's ``fga`` CLI job.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, cast

import pytest
from catalog.api import fga_deps
from catalog.api.v1.endpoints import access
from catalog.core.config import Settings
from fastapi import Request
from openfga_sdk import OpenFgaClient

from service_kit.governed import fga as fga_module
from service_kit.governed.oidc import IDToken


REPO_ROOT = Path(__file__).resolve().parents[2]
AUTH_DIR = REPO_ROOT / "packages" / "service-kit" / "src" / "service_kit" / "governed" / "auth"

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


def _dsl_relations(text: str) -> dict[str, set[str]]:
    """``{object_type: {relation, ...}}`` parsed from OpenFGA DSL source (``.fga`` text)."""
    types: dict[str, set[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()  # strip DSL comments
        if line.startswith("type "):
            current = line.removeprefix("type ").strip()
            types[current] = set()
        elif line.startswith("define ") and current:
            types[current].add(line.removeprefix("define ").split(":", 1)[0].strip())
    return types


def _yaml_inline_model() -> str:
    """The DSL embedded in ``model.fga.yaml`` under the ``model: |`` block (no pyyaml dependency)."""
    lines = (AUTH_DIR / "model.fga.yaml").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.rstrip() == "model: |")
    block: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith((" ", "\t")):  # next top-level key ends the block
            break
        block.append(line)
    return "\n".join(block)


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

    # 1. Non-create ops: _action_relation(type, suffix) for EVERY guarded type x EVERY route suffix
    #    shape the router can produce — owner-tier full suffixes, reader-tier trailing actions, and a
    #    sentinel that falls through to the writer tier. (`transaction` never routes through here.)
    owner_suffixes = {s for m in fga_deps._OWNER_SUFFIX_RELATION.values() for s in m}
    suffixes = (
        owner_suffixes
        | set(fga_deps._META_READ_ACTIONS)
        | set(fga_deps._DATA_READ_ACTIONS)
        | {"", "index/create", "version/delete", "insert", "some/unmapped/mutation"}
    )
    for fga_type in set(fga_deps._FGA_TYPE.values()) - {"transaction"}:
        for suffix in suffixes:
            pairs.add((fga_type, fga_deps._action_relation(fga_type, suffix)))

    # 2. Create-on-parent: the parent is a namespace for a NESTED child, and the configured root
    #    object (a `warehouse:`) for a TOP-LEVEL child when fga_lock_root_create is on — so each
    #    can_create_* must resolve on BOTH types.
    for resource in fga_deps._CREATE_ON_PARENT_SUFFIXES:
        nested = fga_deps._create_parent_check(resource, ["parent_ns", "child"], settings)
        assert nested is not None
        pairs.add((_obj_type(nested[0]), nested[1]))
        top = fga_deps._create_parent_check(resource, ["top_level_child"], root_settings)
        assert top is not None, "fga_lock_root_create must gate a top-level create on the root object"
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


# Checks made OUTSIDE catalog/api/fga_deps.py. Static strings in app code (grep `relation=`), so
# they are listed here with their source — the model cross-check below is what matters.
_OTHER_SERVICE_PAIRS: dict[tuple[str, str], str] = {
    ("table", "can_read_data"): "catalog/api/v1/endpoints/tables.py (list_objects filter)",
    ("table", "can_write_data"): "catalog/api/v1/endpoints/credentials.py (write-tier vend)",
    ("table", "can_get_metadata"): "lineage/api/fga_deps.py (LINEAGE_FGA_OBJECT_TYPE default=table)",
    ("namespace", "can_create_table"): "medallion/services/train.py + transform.py default",
    ("namespace", "can_promote"): "medallion silver->gold mover (chart requiredAction)",
    ("table", "can_promote"): "catalog require_can_promote (#17 model promotion endpoint)",
    ("project", "can_create_warehouse"): "catalog fga_deps.require_can_create_warehouse (#3-A)",
    ("project", "can_administer"): "catalog/api/v1/endpoints/policies.py (#84 project policy routes)",
}


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


def test_every_relation_other_services_check_exists_in_the_model() -> None:
    """CONTRACT: the same guarantee for the checks made outside ``catalog.api.fga_deps`` (lineage's
    dataset gates, medallion's stage-promotion gate, the catalog endpoint-level gates)."""
    model = _model_relations()
    phantom = sorted(f"{t}#{r} ({src})" for (t, r), src in _OTHER_SERVICE_PAIRS.items() if r not in model.get(t, set()))
    assert not phantom, f"phantom relations checked by app code: {phantom}"


def test_chart_medallion_required_actions_exist_on_namespace() -> None:
    """CONTRACT: every ``requiredAction`` the chart configures for a medallion mover is a real
    ``namespace`` relation (the mover checks it on ``namespace:<toNamespace>``). A typo here would
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


def test_access_review_enumerates_exactly_the_models_can_relations() -> None:
    """CONTRACT (#51): the access-review endpoints ask ListUsers for exactly the ``can_*`` set the
    compiled model defines on the type — never a hand-kept list (which would drift into phantom
    relations: OpenFGA 400 → fail-closed 503 for every reviewer) and never a filtered subset (which
    would silently hide grants from the review)."""
    model = _model_relations()
    for fga_type in ("table", "namespace"):
        enumerated = set(access._can_relations(fga_type))
        expected = {r for r in model[fga_type] if r.startswith("can_")}
        assert enumerated, f"access review enumerates no relations for {fga_type}"
        assert enumerated == expected, (
            f"access review's {fga_type} relation set drifted from model.json: missing={sorted(expected - enumerated)} phantom={sorted(enumerated - expected)}"
        )


def test_all_three_model_copies_agree() -> None:
    """CONTRACT: ``model.fga`` (authored), ``model.fga.yaml`` (tested by ``fga model test``) and
    ``model.json`` (LOADED by the app) define the same types + relations. A relation added to the
    authored DSL but not regenerated into the JSON means the app checks something the deployed model
    has never heard of — the same 503 failure mode, one file removed."""
    compiled = _model_relations()
    authored = _dsl_relations((AUTH_DIR / "model.fga").read_text())
    tested = _dsl_relations(_yaml_inline_model())

    assert authored == compiled, "model.json is STALE — regenerate: fga model transform --file model.fga"
    assert tested == compiled, "model.fga.yaml's inline model drifted from model.fga/model.json"


def test_access_disclosure_routes_are_owner_tier() -> None:
    """CONTRACT (security, #51/#68/#72): the access-DISCLOSURE routes — ``access/list`` (enumerate who holds
    access) and ``access/check`` (simulate an arbitrary (user, relation)) — AND the MUTATE routes
    ``access/grant`` / ``access/revoke`` must clear the OWNER bar, never the writer fall-through. The reads
    reveal the authz graph; the writes HAND OUT ownership — a mere writer must not reach any of them.

    The pair-existence contract above canNOT catch a downgrade here: dropping a suffix from the owner map
    falls it through to ``can_write_data`` — a real relation, so that test stays green while the gate quietly
    weakens. This asserts the resolved tier directly, so such a refactor fails loudly."""
    from catalog.api.fga_deps import _action_relation

    for suffix in ("access/list", "access/check", "access/grant", "access/revoke", "access/graph"):
        assert _action_relation("table", suffix) == "can_drop", suffix
        assert _action_relation("namespace", suffix) == "can_delete", suffix
