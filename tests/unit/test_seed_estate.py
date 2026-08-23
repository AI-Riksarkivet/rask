"""``scripts/seed_estate.py`` — the seed that cannot create a state the UI could not.

The defect this script replaces is not "the demo estate was thin", it is GHOST PROJECTS: an FGA-only
seed wrote tuples for three tenants the catalog had no registry record for, and every surface built on
the catalog then disagreed with every surface built on authz. A tuple write does not require its object
to exist, so the ONLY thing standing between a seeder and another ghost is the order it calls in and
what it does when a call is refused. Both are what these tests pin.

No live catalog is involved: the HTTP layer is an ``httpx.MockTransport`` that records every request, so
the ordering, the route CHOICE (a top-level namespace has exactly one legal door), the convergence rule
and the refusal handling are all assertable offline.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from pathlib import Path
from types import ModuleType

import httpx
import pytest


_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: @dataclass resolves stringified annotations through
    # sys.modules[cls.__module__], and the decorator crashes on a module loaded from a bare path.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


seed_estate = _load("seed_estate")

#: A refusal rule: given a request, either a ``(status, problem body)`` to answer with, or None to let
#: the fake catalog succeed. A callable rather than a path→response map because three namespaces share
#: one route — the body is what tells them apart.
Refusal = Callable[[httpx.Request], "tuple[int, object] | None"]

#: The layer each POST route belongs to, in hierarchy order. A seeded estate is only reachable if these
#: happen in this sequence.
_ORDER = {"project": 0, "warehouse": 1, "namespace": 2, "table": 3, "grant": 4}


def _body(request: httpx.Request) -> dict[str, object]:
    return json.loads(request.content) if request.content else {}


def _layer(request: httpx.Request) -> str:
    path = request.url.path
    if path == "/v1/projects":
        return "project"
    if path == "/v1/warehouses":
        return "warehouse"
    if path.endswith("/namespaces"):
        return "namespace"
    if path.endswith("/declare"):
        return "table"
    if path == "/v1/access/tuples":
        return "grant"
    return "other"


def _problem(title: str, detail: str, code: int) -> dict[str, object]:
    """The RFC 9457 body the catalog's ``install_problem_handlers`` actually returns."""
    return {"type": f"https://lance.org/problems/{title.lower()}", "title": title, "detail": detail, "code": code, "error": detail}


#: What the fake catalog's registry holds after a clean seed — the estate the read-back must find. Kept as
#: DATA so a test can take something away from it and prove the read-back notices; a mock that answered the
#: same listing regardless of what was written could not tell a seeded estate from an empty one.
_REGISTRY = [
    {"project": "acme", "warehouses": [{"id": "acme-bucket", "bucket": "acme-bucket", "status": "active"}], "admins": ["user:alice"]},
    {"project": "beta", "warehouses": [{"id": "beta-bucket", "bucket": "beta-bucket", "status": "active"}], "admins": []},
    {"project": "research", "warehouses": [{"id": "research-bucket", "bucket": "research-bucket", "status": "active"}], "admins": []},
]
#: Per-namespace, not one answer for all of them: `acme-gold` holds `catalog`, not `features`, and a mock
#: that ignored the namespace let a read-back "verify" a table the plan never declared there.
#: `beta-locked` is the managed-access scope's stage — its table is what makes the C4 fixture a real
#: object rather than a tuple about nothing.
_TABLES = {"acme-bronze": [], "acme-silver": ["features"], "acme-gold": ["catalog"], "beta-locked": ["records"]}


def _fake_catalog(recorded: list[httpx.Request], refuse: Refusal | None = None) -> httpx.MockTransport:
    """A catalog that records everything, succeeds by default, and refuses whatever ``refuse`` picks."""

    def handle(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        if refuse is not None and (canned := refuse(request)) is not None:
            status, body = canned
            return httpx.Response(status, json=body)
        if request.method == "GET" and request.url.path == "/v1/projects":
            return httpx.Response(200, json=_REGISTRY)
        if request.url.path.endswith("/table/list"):
            namespace = request.url.path.removeprefix("/v1/namespace/").removesuffix("/table/list")
            return httpx.Response(200, json={"tables": _TABLES.get(namespace, [])})
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handle)


def _run(recorded: list[httpx.Request], refuse: Refusal | None = None, argv: list[str] | None = None) -> int:
    return seed_estate.main(argv or ["--catalog", "http://catalog"], transport=_fake_catalog(recorded, refuse))


def _index(recorded: list[httpx.Request], predicate: Callable[[httpx.Request], bool]) -> int:
    return next(i for i, request in enumerate(recorded) if predicate(request))


# --------------------------------------------------------------------------- #
# Order
# --------------------------------------------------------------------------- #


def test_a_project_is_created_before_its_warehouse_before_its_namespace_before_its_table() -> None:
    recorded: list[httpx.Request] = []
    assert _run(recorded) == 0

    writes = [request for request in recorded if request.method == "POST"]
    ranks = [_ORDER[_layer(request)] for request in writes]
    assert ranks == sorted(ranks), [(_layer(r), r.url.path) for r in writes]
    # …and the acme chain specifically, since a globally-sorted run could still mis-order one tenant.
    assert (
        _index(recorded, lambda r: r.url.path == "/v1/projects" and _body(r).get("id") == "acme")
        < _index(recorded, lambda r: r.url.path == "/v1/warehouses" and _body(r).get("id") == "acme-bucket")
        < _index(recorded, lambda r: r.url.path.endswith("/namespaces") and _body(r).get("namespace") == "acme-gold")
        < _index(recorded, lambda r: r.url.path == "/v1/table/acme-gold$catalog/declare")
    )


def test_every_grant_is_written_after_the_object_it_decorates_exists() -> None:
    recorded: list[httpx.Request] = []
    assert _run(recorded) == 0

    first_grant = _index(recorded, lambda r: r.url.path == "/v1/access/tuples")
    creates = [i for i, request in enumerate(recorded) if request.method == "POST" and _layer(request) != "grant"]
    assert max(creates) < first_grant


def test_the_estate_is_read_back_from_the_catalog_after_the_writes() -> None:
    recorded: list[httpx.Request] = []
    assert _run(recorded) == 0

    listing = _index(recorded, lambda r: r.method == "GET" and r.url.path == "/v1/projects")
    assert listing > max(i for i, request in enumerate(recorded) if request.method == "POST")
    assert any(request.url.path == "/v1/namespace/acme-silver/table/list" for request in recorded)


# --------------------------------------------------------------------------- #
# Route choice
# --------------------------------------------------------------------------- #


def test_a_top_level_namespace_is_created_through_its_warehouse_and_never_the_generic_route() -> None:
    recorded: list[httpx.Request] = []
    assert _run(recorded) == 0

    # /v1/namespace/{id}/create cannot name a warehouse, so a top-level namespace made there has no
    # bucket to route to — the catalog refuses it outright, and a seeder must not even ask.
    assert not [request for request in recorded if request.url.path.startswith("/v1/namespace/") and request.url.path.endswith("/create")]
    namespaces = [request for request in recorded if _layer(request) == "namespace"]
    # Both buckets that hold a stage, not just acme's: the route choice is a property of EVERY
    # top-level namespace, and pinning only the first warehouse let a second one take the generic
    # door unnoticed.
    assert {request.url.path for request in namespaces} == {"/v1/warehouses/acme-bucket/namespaces", "/v1/warehouses/beta-bucket/namespaces"}
    assert [_body(request)["namespace"] for request in namespaces] == ["acme-bronze", "acme-silver", "acme-gold", "beta-locked"]


def test_a_table_is_declared_under_its_namespace_path_not_as_a_flat_id() -> None:
    recorded: list[httpx.Request] = []
    assert _run(recorded) == 0

    declares = [request for request in recorded if _layer(request) == "table"]
    assert [request.url.path for request in declares] == [
        "/v1/table/acme-silver$features/declare",
        "/v1/table/acme-gold$catalog/declare",
        "/v1/table/beta-locked$records/declare",
    ]
    assert _body(declares[0])["id"] == ["acme-silver", "features"]


def test_a_grant_on_a_table_names_the_table_this_run_actually_created() -> None:
    """The one grant whose object is a TABLE, and therefore the one that depends on the delimiter.

    A table's id is its PATH and the separator is per-run (``--delimiter`` / ``SEED_NS_DELIMITER``), so a
    grant that hardcoded ``$`` would address a table nobody created the moment the two disagreed — and a
    tuple write SUCCEEDS against an object that does not exist. That is the ghost the whole script is
    built to stop making, re-entering through the grant layer.

    Driven with a NON-default delimiter on purpose: under the default the hardcoded form and the correct
    form are the same string, so the bug is invisible exactly where it would be tested.
    """
    recorded: list[httpx.Request] = []
    assert _run(recorded, argv=["--catalog", "http://catalog", "--delimiter", "."]) == 0

    declared = [request.url.path for request in recorded if _layer(request) == "table"]
    granted = [str(_body(request)["object"]) for request in recorded if _layer(request) == "grant"]

    assert "/v1/table/acme-gold.catalog/declare" in declared
    assert "table:acme-gold.catalog" in granted, f"the table grant did not follow the run's delimiter: {granted}"
    assert not [obj for obj in granted if "{delimiter}" in obj], f"the placeholder reached the wire: {granted}"
    # And it is still ORDERED behind the declare — resolving the object without resolving the parent
    # lookup would write the tuple first, against a table that does not exist yet.
    assert _index(recorded, lambda r: r.url.path == "/v1/table/acme-gold.catalog/declare") < _index(
        recorded, lambda r: _layer(r) == "grant" and _body(r)["object"] == "table:acme-gold.catalog"
    )


def test_the_bearer_token_rides_every_call_and_is_absent_when_there_is_none() -> None:
    recorded: list[httpx.Request] = []
    assert _run(recorded, argv=["--catalog", "http://catalog", "--token", "id-token-abc"]) == 0
    assert {request.headers.get("authorization") for request in recorded} == {"Bearer id-token-abc"}

    unauthenticated: list[httpx.Request] = []
    assert _run(unauthenticated) == 0
    assert not [request for request in unauthenticated if "authorization" in request.headers]


# --------------------------------------------------------------------------- #
# Convergence
# --------------------------------------------------------------------------- #


def test_an_already_exists_refusal_is_convergence_and_the_run_still_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    def refuse(request: httpx.Request) -> tuple[int, object] | None:
        if _layer(request) in {"namespace", "table"}:
            return 409, _problem("NamespaceAlreadyExistsError", "namespace 'acme-gold' already exists", 2)
        return None

    recorded: list[httpx.Request] = []
    assert _run(recorded, refuse) == 0

    out = capsys.readouterr().out
    assert "already exists" in out
    # Convergence is not silence: the run says which steps were already there, and it does not stop the
    # tables (whose namespaces "failed") or the grants from being attempted.
    assert [request.url.path for request in recorded if _layer(request) == "table"]
    assert [request.url.path for request in recorded if _layer(request) == "grant"]


def test_a_warehouse_id_collision_is_a_failure_not_convergence() -> None:
    # 409 on warehouse-create is the cross-tenant takeover guard, not "already there" — the create is
    # idempotent server-side for the SAME project, so this 409 can only mean another project owns the id.
    def refuse(request: httpx.Request) -> tuple[int, object] | None:
        if _layer(request) == "warehouse" and _body(request).get("id") == "acme-bucket":
            return 409, _problem("NamespaceAlreadyExistsError", "warehouse 'acme-bucket' is already registered to another project", 2)
        return None

    recorded: list[httpx.Request] = []
    assert _run(recorded, refuse) == 1


def test_a_namespace_already_bound_to_another_warehouse_is_a_failure_not_convergence() -> None:
    # The namespace door answers 409 for THREE things with one error class and one code: "already here"
    # (the re-run this script must survive) and the two binding guards. Swallowing a binding guard is the
    # ghost at its worst — the run would declare tables into a bucket it does not own and hand a validator
    # rung on another tenant's stage to role:validators, then print "seeded".
    def refuse(request: httpx.Request) -> tuple[int, object] | None:
        if _layer(request) == "namespace" and _body(request).get("namespace") == "acme-gold":
            return 409, _problem("NamespaceAlreadyExistsError", "namespace 'acme-gold' is already bound to another warehouse", 2)
        return None

    recorded: list[httpx.Request] = []
    assert _run(recorded, refuse) == 1

    grants = [_body(request) for request in recorded if request.url.path == "/v1/access/tuples"]
    assert not [grant for grant in grants if grant["object"] == "namespace:acme-gold"]
    assert "/v1/table/acme-gold$catalog/declare" not in [request.url.path for request in recorded]
    # The sibling namespace is untouched: one refused bind must not empty the rest of the estate.
    assert "/v1/table/acme-silver$features/declare" in [request.url.path for request in recorded]


def test_a_namespace_name_taken_in_the_default_root_is_a_failure_not_convergence() -> None:
    # The other binding guard, same shape: binding a name that already exists unbound would orphan that
    # namespace's tables, and the catalog refuses precisely so nobody does it by accident.
    def refuse(request: httpx.Request) -> tuple[int, object] | None:
        if _layer(request) == "namespace" and _body(request).get("namespace") == "acme-silver":
            detail = "namespace 'acme-silver' already exists in the default root; binding it to a warehouse would orphan its tables"
            return 409, _problem("NamespaceAlreadyExistsError", detail, 2)
        return None

    recorded: list[httpx.Request] = []
    assert _run(recorded, refuse) == 1
    assert "/v1/table/acme-silver$features/declare" not in [request.url.path for request in recorded]


def test_the_binding_guards_still_say_what_this_script_matches_on() -> None:
    """The drift gate under ``BINDING_REFUSALS``: the seed tells a takeover 409 from a convergence 409 by
    the guard's WORDING, because the catalog raises one error class with one code for both. That is only
    safe while the wording is the catalog's and this test is what makes it so — reword the guard without
    this list and the seed silently starts swallowing the refusal again.
    """
    from catalog.api.v1.endpoints import warehouses as warehouse_routes

    source = inspect.getsource(warehouse_routes.create_warehouse_namespace)
    assert [phrase for phrase in seed_estate.BINDING_REFUSALS if phrase not in source] == []


def test_grants_are_skipped_not_failed_when_the_stack_runs_authorization_off(capsys: pytest.CaptureFixture[str]) -> None:
    def refuse(request: httpx.Request) -> tuple[int, object] | None:
        if request.url.path == "/v1/access/tuples":
            return 501, _problem("UnsupportedOperationError", "FGA administration requires OpenFGA (this stack runs auth-off)", 0)
        return None

    recorded: list[httpx.Request] = []
    assert _run(recorded, refuse) == 0
    assert "authorization is off" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def test_a_refused_step_prints_the_problem_body_and_exits_non_zero(capsys: pytest.CaptureFixture[str]) -> None:
    detail = "can_create_warehouse required on project:acme"

    def refuse(request: httpx.Request) -> tuple[int, object] | None:
        return (403, _problem("PermissionDeniedError", detail, 15)) if _layer(request) == "warehouse" else None

    recorded: list[httpx.Request] = []
    assert _run(recorded, refuse) == 1

    captured = capsys.readouterr()
    # The guard's own words, not a bare status — a refusal nobody can read moves the search to the user.
    assert detail in captured.err
    assert "PermissionDeniedError" in captured.err
    assert "403" in captured.err


def test_a_grant_is_never_written_against_an_object_whose_create_failed() -> None:
    # THE ghost rule. OpenFGA stores a tuple whether or not its object exists, so writing the gold
    # namespace's validator grant after that namespace failed to be created is exactly how the live
    # estate got three tenants the catalog had never heard of.
    def refuse(request: httpx.Request) -> tuple[int, object] | None:
        if _layer(request) == "namespace" and _body(request).get("namespace") == "acme-gold":
            return 403, _problem("PermissionDeniedError", "can_create_namespace required on warehouse:acme-bucket", 15)
        return None

    recorded: list[httpx.Request] = []
    assert _run(recorded, refuse) == 1

    grants = [_body(request) for request in recorded if request.url.path == "/v1/access/tuples"]
    assert not [grant for grant in grants if grant["object"] == "namespace:acme-gold"]
    # Everything the failure does NOT block still lands: one broken branch must not empty the estate.
    assert {"user:dave", "user:carol", "user:gina"} <= {str(grant["user"]) for grant in grants}


def test_a_failed_create_blocks_only_its_own_descendants() -> None:
    def refuse(request: httpx.Request) -> tuple[int, object] | None:
        if _layer(request) == "project" and _body(request).get("id") == "beta":
            return 403, _problem("PermissionDeniedError", "can_observe_events required on warehouse:lance_catalog", 15)
        return None

    recorded: list[httpx.Request] = []
    assert _run(recorded, refuse) == 1

    warehouses = [_body(request).get("id") for request in recorded if _layer(request) == "warehouse"]
    assert "beta-bucket" not in warehouses
    assert {"acme-bucket", "research-bucket"} <= set(warehouses)


def test_an_estate_that_cannot_be_read_back_is_not_reported_as_seeded(capsys: pytest.CaptureFixture[str]) -> None:
    def refuse(request: httpx.Request) -> tuple[int, object] | None:
        if request.method == "GET" and request.url.path == "/v1/projects":
            return 403, _problem("PermissionDeniedError", "can_observe_events required on warehouse:lance_catalog", 15)
        return None

    recorded: list[httpx.Request] = []
    assert _run(recorded, refuse) == 1
    assert "can_observe_events" in capsys.readouterr().err


def test_a_transport_failure_is_reported_against_the_step_that_hit_it(capsys: pytest.CaptureFixture[str]) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    assert seed_estate.main(["--catalog", "http://catalog"], transport=httpx.MockTransport(handle)) == 1
    err = capsys.readouterr().err
    assert "connection refused" in err
    # Against the STEP, not as one anonymous stack trace: the first failing line names the object it was
    # making, which is what tells an operator whether the run died at the door or three layers in.
    assert "project acme" in err


# --------------------------------------------------------------------------- #
# The read-back is the WITNESS — a 2xx per write is not the claim this script makes
# --------------------------------------------------------------------------- #


def test_an_estate_the_registry_does_not_hold_is_not_reported_as_seeded(capsys: pytest.CaptureFixture[str]) -> None:
    # THE ghost, stated as a test: every write is accepted and the registry holds nothing. This is not a
    # hypothetical — it is the exact state the live estate was found in, and a seed whose summary just
    # renders the listing prints an empty estate under the words "every object above exists in the catalog".
    def refuse(request: httpx.Request) -> tuple[int, object] | None:
        return (200, []) if request.method == "GET" and request.url.path == "/v1/projects" else None

    recorded: list[httpx.Request] = []
    assert _run(recorded, refuse) == 1
    err = capsys.readouterr().err
    assert "project acme is absent" in err
    assert "project research is absent" in err


def test_a_namespace_is_verified_even_when_its_project_is_missing_from_the_listing() -> None:
    # A missing tenant must not take its whole subtree's verification with it. Nesting the namespace reads
    # under the project listing is how a seed ends up checking nothing at all and saying so confidently.
    def refuse(request: httpx.Request) -> tuple[int, object] | None:
        return (200, []) if request.method == "GET" and request.url.path == "/v1/projects" else None

    recorded: list[httpx.Request] = []
    assert _run(recorded, refuse) == 1
    assert {"acme-bronze", "acme-silver", "acme-gold"} <= {
        request.url.path.removeprefix("/v1/namespace/").removesuffix("/table/list") for request in recorded if request.url.path.endswith("/table/list")
    }


def test_a_warehouse_the_project_does_not_hold_is_not_reported_as_seeded(capsys: pytest.CaptureFixture[str]) -> None:
    thin = [dict(record, warehouses=[]) if record["project"] == "acme" else record for record in _REGISTRY]

    def refuse(request: httpx.Request) -> tuple[int, object] | None:
        return (200, thin) if request.method == "GET" and request.url.path == "/v1/projects" else None

    recorded: list[httpx.Request] = []
    assert _run(recorded, refuse) == 1
    assert "warehouse acme-bucket is absent from project acme" in capsys.readouterr().err


def test_a_declared_table_the_namespace_does_not_list_is_not_reported_as_seeded(capsys: pytest.CaptureFixture[str]) -> None:
    # `declare` returning 200 says the call was accepted. Only the namespace's own listing says the table
    # is there — and the two disagreeing is the whole class of bug this script was written against.
    def refuse(request: httpx.Request) -> tuple[int, object] | None:
        return (200, {"tables": []}) if request.url.path == "/v1/namespace/acme-gold/table/list" else None

    recorded: list[httpx.Request] = []
    assert _run(recorded, refuse) == 1
    assert "table catalog is absent from namespace acme-gold" in capsys.readouterr().err


def test_a_read_back_that_is_not_json_is_a_failure_and_never_a_crash(capsys: pytest.CaptureFixture[str]) -> None:
    # A proxy answering 200 with HTML instead of the catalog. Letting the decode raise would lose the
    # summary at the one moment it matters — right after the writes landed.
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="<html>404 page not found</html>")
        return httpx.Response(200, json={"ok": True})

    assert seed_estate.main(["--catalog", "http://catalog"], transport=httpx.MockTransport(handle)) == 1
    assert "not JSON" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# The description itself
# --------------------------------------------------------------------------- #


def test_the_demo_estate_covers_every_tenant_warehouse_and_namespace_the_fga_fixtures_name() -> None:
    """The replacement claim, made checkable: what ``fga_seed_demo`` seeds as tuples exists here as objects.

    Ids are normalised ``_`` → ``-`` because the catalog validates warehouse/bucket/namespace ids against
    a DNS-safe pattern — the fixture's ``acme_bucket`` cannot be created through any door.
    """
    fixtures = _load("fga_seed_demo").load_fixtures()
    named = {tuple_["object"] for tuple_ in fixtures} | {tuple_["user"] for tuple_ in fixtures}

    def wanted(prefix: str, *, excluding: AbstractSet[str] = frozenset()) -> set[str]:
        return {obj.removeprefix(prefix).replace("_", "-") for obj in named if obj.startswith(prefix)} - excluding

    estate = seed_estate.DEMO_ESTATE
    assert wanted("project:") == {project.id for project in estate.projects}
    # `lance-catalog` is the ESTATE ROOT, granted by the bootstrap job — not a tenant's warehouse.
    assert wanted("warehouse:", excluding={"lance-catalog"}) == {warehouse.id for _, warehouse in seed_estate._warehouse_pairs(estate)}
    assert wanted("namespace:") == {namespace.name for _, namespace in seed_estate._namespace_pairs(estate)}


def test_every_seeded_id_can_survive_the_catalogs_dns_safe_id_pattern() -> None:
    # Each id against the pattern of the door that actually creates it, and the catalog's OWN compiled
    # objects rather than a transcription: a copied regex agrees with the door on the day it is written and
    # never again — and this seed's entire premise is that it can only make states the API can make.
    from catalog.api.v1.endpoints import projects as project_routes
    from catalog.api.v1.endpoints import warehouses as warehouse_routes

    estate = seed_estate.DEMO_ESTATE
    refused = [project.id for project in estate.projects if not project_routes._ID_RE.match(project.id)]
    # The warehouse module validates the warehouse id, its bucket name AND the namespace name it binds.
    refused += [warehouse.id for _, warehouse in seed_estate._warehouse_pairs(estate) if not warehouse_routes._ID_RE.match(warehouse.id)]
    refused += [namespace.name for _, namespace in seed_estate._namespace_pairs(estate) if not warehouse_routes._ID_RE.match(namespace.name)]
    assert refused == []


def test_a_demo_identity_is_seeded_as_its_oidc_subject_and_a_persona_stays_literal() -> None:
    # Seeding the literal `user:alice` makes every check for the signed-in alice deny — correctly, and
    # confusingly. Personas with no IdP account stay literal; they still make the graph worth reading.
    subs = dict(seed_estate.DEX_SUBS)
    assert seed_estate.remap_subject("user:alice", subs) == f"user:{subs['alice']}"
    assert seed_estate.remap_subject("user:carol", subs) == "user:carol"
    # Only the SUBJECT side: a userset or an object type is never a principal to remap.
    assert seed_estate.remap_subject("role:validators#assignee", subs) == "role:validators#assignee"
    assert seed_estate.remap_subject("team:eng", subs) == "team:eng"


# --------------------------------------------------------------------------- #
# Adoption — the fourth meaning of 409
# --------------------------------------------------------------------------- #


def test_by_default_a_namespace_create_does_not_ask_to_adopt() -> None:
    """Adopting a namespace whose bytes are already in the bucket is the hazard the binding guards
    exist for, so it is never the default: a typo'd create must not inherit a stranger's data."""
    recorded: list[httpx.Request] = []
    assert _run(recorded) == 0
    bodies = [_body(request) for request in recorded if _layer(request) == "namespace"]
    assert bodies, "no namespace creates recorded"
    assert all("adopt_existing" not in body or body["adopt_existing"] is False for body in bodies), bodies


def test_adopt_existing_asks_the_catalog_to_converge_the_unbound_namespace() -> None:
    """The FOURTH meaning of 409, and the one this script could not converge.

    `POST /v1/warehouses/{id}/namespaces` answers `NamespaceAlreadyExistsError` for four states, not the
    three listed beside BINDING_REFUSALS: converged, bound elsewhere, taken in the default root — and
    the namespace whose BYTES are already at THIS warehouse's root while its binding was never written.
    That last one was observed on the live estate 2026-08-23: the acme tiers existed as directories, the
    bindings map held no entry for them, so routing fell through to the default root and every table
    declare answered `NamespaceNotFoundError` for a namespace whose create had just said "already
    exists". The run reported converged and then failed two tables.

    It is not convergence and it is not a takeover; it is the bytes-first migration mid-flight, and the
    catalog's own door for it is `adopt_existing`, which runs the binding + FGA trailer the state is
    missing. Opt-in, because the hazard guards it does NOT relax are the whole reason it is a flag.
    """
    recorded: list[httpx.Request] = []
    assert _run(recorded, argv=["--catalog", "http://catalog", "--adopt-existing"]) == 0
    bodies = [_body(request) for request in recorded if _layer(request) == "namespace"]
    assert bodies, "no namespace creates recorded"
    assert all(body.get("adopt_existing") is True for body in bodies), bodies


def test_adoption_never_relaxes_the_takeover_guards() -> None:
    """`adopt_existing` converges bytes at THIS warehouse's root; a name bound ELSEWHERE stays a failure."""

    def refuse(request: httpx.Request) -> tuple[int, object] | None:
        if _layer(request) == "namespace":
            return 409, _problem("NamespaceAlreadyExistsError", "namespace 'acme-gold' is already bound to another warehouse", 2)
        return None

    recorded: list[httpx.Request] = []
    assert _run(recorded, refuse, argv=["--catalog", "http://catalog", "--adopt-existing"]) == 1
