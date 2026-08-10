#!/usr/bin/env python3
"""Seed the demo estate through the catalog's REAL APIs, in hierarchy order.

WHY THIS EXISTS — the ghost projects. ``scripts/fga_seed_demo.py`` writes FGA TUPLES ONLY, and a tuple
write does not require its object to exist: OpenFGA happily stores ``user:alice admin project:acme``
whether or not any catalog registry record calls ``acme`` a tenant. That is how the live estate ended up
holding three projects the catalog had never heard of — authorization said they were there, the registry
said nothing, and the projects gallery broke on the difference. A seed that writes authz without writing
EXISTENCE can manufacture a state no user could ever reach through the UI, and then every surface built
on the catalog reads that state as corruption.

So this seeds the same estate through the doors a human uses, in the order the hierarchy demands:

    POST /v1/projects                        the estate admin mints the tenant
    POST /v1/warehouses                      a project admin gives it a bucket
    POST /v1/warehouses/{id}/namespaces      the ONLY door for a top-level namespace
    POST /v1/table/{ns}${name}/declare       the tables inside those namespaces
    POST /v1/access/tuples                   the extra grants, LAST

Every guard therefore runs: an unknown tenant 404s at the warehouse door naming ``POST /v1/projects``,
a top-level namespace has no door that does not name a warehouse, and a table cannot be declared outside
a namespace. **A state that cannot be reached through the UI cannot be seeded here either** — which is
the whole point, and the reason the grants go last: a grant whose object is a create that FAILED is
skipped rather than written, because writing it is precisely how a ghost is made.

THE ESTATE MIRRORS THE FGA FIXTURES (``service_kit/governed/auth/model.fga.yaml``), the same set
``fga_seed_demo.py`` loads and CI asserts against, so this is a replacement rather than a toy: three
projects, three warehouses, the bronze/silver/gold cascade under acme, a ``role:validators`` persona,
and the users that exercise the interesting cases (a writer who is NOT a validator, a validator who
reaches gold only through a role, a tenant member who is not an admin). Two deliberate differences:

* **Ids are DNS-safe.** The fixtures write ``warehouse:acme_bucket``; the catalog validates warehouse,
  bucket and namespace ids against ``^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$``, so an underscore cannot be
  created through the API at all. Underscores become hyphens.
* **A table's id is its PATH.** The fixtures name ``table:acme_silver_features`` as a flat illustrative
  id; a table created through the real door is identified by ``<namespace>$<name>``, so the seeded
  object is ``table:acme-silver$features``. There is no door that produces the flat form.

Three fixture tuples are deliberately NOT seeded, each for the ghost reason this script exists for:
``transaction:standalone_txn`` (a transaction is minted by a write op, not by a seed), the
``annotation_project:labels_2026`` grants (that object's existence lives in the annotator service's own
registry, not the catalog's), and ``user:root_admin owner warehouse:lance_catalog`` (the estate root is
the bootstrap job's to grant, not a demo seed's).

THE SUBJECT MAPPING is inherited verbatim from ``fga_seed_demo.py``: the fixtures say ``user:alice``, a
real store keys on the OIDC ``sub``, and seeding the literal makes every check for the signed-in alice
deny — correctly, and confusingly. Demo identities are rewritten to their real subs; the rest stay
literal (personas with no IdP account, which still make the graph worth reading).

Idempotent by construction: project, warehouse and tuple creates are idempotent server-side, and the two
that are not (namespace, table) treat an already-exists 409 as convergence and SAY so. Re-running over a
partial seed is the normal case. But a 409 is tolerated for what it MEANS, never for its status alone: the
namespace door answers the same error class and code for "already here" and for the two binding guards
refusing a takeover, so those wordings (``BINDING_REFUSALS``) stay failures.

Usage (against a reachable catalog — ``kubectl port-forward svc/rask-catalog 2333:2333``)::

    uv run python scripts/seed_estate.py
    uv run python scripts/seed_estate.py --dry-run
    SEED_CATALOG_TOKEN="$(…dex id_token…)" uv run python scripts/seed_estate.py
    uv run python scripts/seed_estate.py --map alice=<sub> --map bob=<sub>

Env (argparse overrides all of them)::

    SEED_CATALOG_URL    default http://127.0.0.1:2333
    SEED_CATALOG_TOKEN  the OIDC bearer; empty = an auth-off local stack
    SEED_NS_DELIMITER   default $, must match the catalog's LANCE_NS_DELIMITER

Exits non-zero if any step failed OR the read-back does not SHOW every object the plan named — a seed that
cannot show you what it made has not proved anything, and "the calls returned 2xx" is not the claim being
made here. The registry is the witness, so the summary compares against it rather than reprinting the plan.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

import httpx


#: The catalog's own port (``rask-catalog``), not the gateway's — the seed drives the service directly so
#: a routing problem in front of it cannot be mistaken for a guard's refusal.
CATALOG_URL = os.environ.get("SEED_CATALOG_URL", "http://127.0.0.1:2333")
#: The catalog accepts ONLY IdP bearers when auth is on, and every door here is a governed write — so a
#: seed against an auth-enabled estate must carry one or the first POST 401s. Empty = auth off.
CATALOG_TOKEN = os.environ.get("SEED_CATALOG_TOKEN", "")
#: Must agree with the catalog's ``LANCE_NS_DELIMITER``: it is what joins a namespace and a table name
#: into the id BOTH the route and the FGA object use, so a client that disagrees grants on one id and
#: checks another.
DELIMITER = os.environ.get("SEED_NS_DELIMITER", "$")

#: Dex's two static demo identities, encoded the way an OIDC `sub` actually arrives. Kept in step with
#: ``fga_seed_demo._DEX_SUBS`` (the chart pins these userIDs in ``chart/templates/dex.yaml``) — a seed
#: that drifted from them would grant a subject nobody can sign in as.
DEX_SUBS: Mapping[str, str] = MappingProxyType(
    {
        "alice": "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjYSBWxvY2Fs",
        "bob": "CiQxYjZlMmY1YS03YzRkLTRlOWItOWYxYS0yZDNjNGI1YTZlN2YSBWxvY2Fs",
    }
)

#: A status a step tolerates → (outcome state, the word printed for it).
#:
#: ONLY the creates the server does not already make idempotent carry this. A 409 from
#: ``POST /v1/warehouses`` is a cross-tenant id/bucket collision — the guard's whole purpose — and
#: swallowing it as "already there" would hide exactly the takeover it just refused.
CONVERGED: Mapping[int, tuple[str, str]] = MappingProxyType({409: ("converged", "already exists")})
#: Phrases that turn a TOLERATED status back into a failure.
#:
#: ``POST /v1/warehouses/{id}/namespaces`` answers 409 for three different things, all of them the SAME
#: ``NamespaceAlreadyExistsError`` (spec code 2): the namespace is already here (convergence — the normal
#: re-run), the name is already bound to ANOTHER warehouse, or it already exists unbound in the default
#: root. The last two are the binding guards refusing a takeover, and swallowing them is the ghost this
#: script exists to stop making at its worst: the run would then declare tables into a bucket it does not
#: own and write ``namespace:<name>#validator`` onto another tenant's namespace, and report success. Same
#: class and same code, so the guard's own wording is the only discriminator there is — pinned against the
#: catalog's source by ``test_the_binding_guards_still_say_what_this_script_matches_on``.
BINDING_REFUSALS: tuple[str, ...] = ("already bound to another warehouse", "already exists in the default root")
#: ``/v1/access`` answers 501 when the stack runs auth-off: there is no tuple store to seed, which is a
#: legitimate local configuration, not a failure. Loud, never silent — the estate is then unguarded.
FGA_OFF: Mapping[int, tuple[str, str]] = MappingProxyType({501: ("skipped", "authorization is off — no tuple store to seed")})


# --------------------------------------------------------------------------- #
# The estate description — declarative; the walk below is what turns it into calls
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Namespace:
    """One top-level namespace bound to a warehouse, and the tables declared inside it."""

    name: str
    tables: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Warehouse:
    """One warehouse = one S3 bucket. ``bucket`` defaults to the id server-side; ``serving="gold"``
    marks the project's gold serving warehouse."""

    id: str
    namespaces: tuple[Namespace, ...] = ()
    bucket: str | None = None
    serving: str | None = None


@dataclass(frozen=True, slots=True)
class Project:
    """A tenant — the TOP of the hierarchy. One project may hold many warehouses."""

    id: str
    warehouses: tuple[Warehouse, ...] = ()


@dataclass(frozen=True, slots=True)
class Grant:
    """One raw tuple written through ``POST /v1/access/tuples`` after the objects exist.

    ``object`` may carry the literal ``{delimiter}`` placeholder, and a grant on a TABLE must: a table's
    id is its PATH (``<namespace>$<name>``), the separator is configurable per run (``--delimiter`` /
    ``SEED_NS_DELIMITER``), and a grant that hardcoded ``$`` would name a table this run did not create
    the moment the two disagreed — writing the tuple anyway, against an object that does not exist,
    which is the ghost this whole script is built to stop making. Substituted in :func:`plan`.
    """

    user: str
    relation: str
    object: str


@dataclass(frozen=True, slots=True)
class Estate:
    """Everything to seed: the hierarchy, then the grants that decorate it."""

    projects: tuple[Project, ...]
    grants: tuple[Grant, ...] = ()


DEMO_ESTATE = Estate(
    projects=(
        Project(
            id="acme",
            warehouses=(
                Warehouse(
                    id="acme-bucket",
                    namespaces=(
                        # The medallion cascade the fixtures describe. bronze holds no table there, and
                        # holding none here too keeps the two readable as the same estate.
                        Namespace(name="acme-bronze"),
                        Namespace(name="acme-silver", tables=("features",)),
                        Namespace(name="acme-gold", tables=("catalog",)),
                    ),
                ),
            ),
        ),
        # team:eng owns TWO projects — the fixture's proof that a team is not a tenant. Their warehouses
        # carry no namespaces, exactly as the fixtures leave them.
        Project(id="research", warehouses=(Warehouse(id="research-bucket"),)),
        Project(
            id="beta",
            warehouses=(
                Warehouse(
                    id="beta-bucket",
                    # The MANAGED-ACCESS scope (C4). The fixtures put it on its OWN warehouse on purpose:
                    # the flag changes who may grant, so setting it on acme-bucket would silently rewrite
                    # every grant-axis expectation over there.
                    namespaces=(Namespace(name="beta-locked", tables=("records",)),),
                ),
            ),
        ),
    ),
    grants=(
        # Teams and their members. `team:` and `role:` objects have no catalog existence to seed — they
        # are pure authorization constructs with no registry record, so a tuple IS their whole life.
        Grant("user:alice", "member", "team:eng"),
        Grant("user:bob", "member", "team:eng"),
        Grant("user:frank", "member", "team:partners"),
        # The team → project edges, which is what makes alice an admin of acme AND research.
        Grant("team:eng", "team", "project:acme"),
        Grant("team:eng", "team", "project:research"),
        Grant("team:partners", "team", "project:beta"),
        # dave: a plain WRITER on the bucket — writes any stage, but is NOT a validator.
        Grant("user:dave", "writer", "warehouse:acme-bucket"),
        # carol: reaches gold ONLY through a role, plus reader on the bucket so she can read silver.
        Grant("user:carol", "assignee", "role:validators"),
        Grant("role:validators#assignee", "validator", "namespace:acme-gold"),
        Grant("user:carol", "reader", "warehouse:acme-bucket"),
        # gina: a plain tenant member (not via team:eng) — a viewer who is not an admin.
        Grant("user:gina", "member", "project:acme"),
        # ---- UPWARD VISIBILITY (C1) --------------------------------------------------------------
        # ivan holds ONE grant, on ONE table. The containers above it become VISIBLE — the breadcrumb
        # to his own data resolves instead of 403ing — while a SIBLING stage stays invisible. This is
        # the only persona whose object is a table, and therefore the only one that needs the
        # delimiter placeholder.
        Grant("user:ivan", "reader", "table:acme-gold{delimiter}catalog"),
        # ---- THE GRANT AXIS (C2) — administering access is not the same as holding it -------------
        # judy: grant authority on the bucket and NO data rung — the access-review persona that was
        # unexpressible while granting was welded to `owner`.
        Grant("user:judy", "manage_grants", "warehouse:acme-bucket"),
        # kevin: a DELEGATE on the silver stage — a writer who may hand on exactly what he holds, and
        # no more.
        Grant("user:kevin", "writer", "namespace:acme-silver"),
        Grant("user:kevin", "pass_grants", "namespace:acme-silver"),
        # leo: holds the grant OPTION and no rung — the option is worthless alone, by construction.
        Grant("user:leo", "pass_grants", "namespace:acme-silver"),
        # ---- MANAGED ACCESS (C4) -----------------------------------------------------------------
        # The flag, written as the ONE canonical tuple the catalog stores it as (`_MANAGED_ACCESS_SUBJECT`
        # in the catalog's access endpoints). `user:*` is a TYPE RESTRICTION used as a switch, not a
        # public grant: no rule reads it as "everyone", only `managed_access_inheritance` reads it at
        # all. It has a real door of its own (`POST /v1/warehouses/{id}/managed-access/set`), but that
        # door is gated on `can_set_managed_access` HELD ON THE BUCKET, which is a tenant's relation and
        # not a seed's; the raw tuple editor is where every other grant here goes and it accepts the
        # flag as the directly-assignable relation it is.
        Grant("user:*", "managed_access", "warehouse:beta-bucket"),
        # mona OWNS the stage inside the managed scope — which would normally carry the right to grant,
        # and under the flag does not. She keeps every data power.
        Grant("user:mona", "owner", "namespace:beta-locked"),
        # nora is the records manager: grant authority at the warehouse, which the flag centralizes on her.
        Grant("user:nora", "manage_grants", "warehouse:beta-bucket"),
    ),
)


# --------------------------------------------------------------------------- #
# The walk
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Step:
    """One HTTP call, plus what it means for the ones that come after it.

    ``key`` is the object this step brings into existence (also its FGA object id), ``parent`` the object
    that must exist first. A step whose parent failed is SKIPPED, not attempted — for a create that keeps
    the output honest, and for a grant it is load-bearing: a tuple write succeeds against an object that
    does not exist, which is the ghost this script exists to stop making.
    """

    layer: str
    label: str
    method: str
    path: str
    body: dict[str, object] | None = None
    key: str | None = None
    parent: str | None = None
    tolerate: Mapping[int, tuple[str, str]] = MappingProxyType({})
    #: Phrases in the problem body that REVOKE ``tolerate`` for this response. A tolerated status is not a
    #: tolerated refusal — see ``BINDING_REFUSALS``.
    refuses: tuple[str, ...] = ()


def remap_subject(subject: str, subs: Mapping[str, str]) -> str:
    """Rewrite ``user:<name>`` to ``user:<oidc-sub>`` for identities that really exist in the IdP.

    Only the subject side is rewritten. A ``user:`` id never appears as an OBJECT in this model, and
    rewriting one would invent a resource rather than name a principal.
    """
    if not subject.startswith("user:"):
        return subject
    name = subject.removeprefix("user:")
    return f"user:{subs[name]}" if name in subs else subject


def _warehouse_pairs(estate: Estate) -> list[tuple[Project, Warehouse]]:
    """Every ``(project, warehouse)`` in the estate — the warehouse layer, flattened."""
    return [(project, warehouse) for project in estate.projects for warehouse in project.warehouses]


def _namespace_pairs(estate: Estate) -> list[tuple[Warehouse, Namespace]]:
    """Every ``(warehouse, namespace)`` in the estate — the namespace layer, flattened."""
    return [(warehouse, namespace) for _, warehouse in _warehouse_pairs(estate) for namespace in warehouse.namespaces]


def _table_pairs(estate: Estate) -> list[tuple[Namespace, str]]:
    """Every ``(namespace, table name)`` in the estate — the table layer, flattened."""
    return [(namespace, table) for _, namespace in _namespace_pairs(estate) for table in namespace.tables]


def _warehouse_body(project: Project, warehouse: Warehouse) -> dict[str, object]:
    """The create payload. ``bucket`` and ``serving`` are OMITTED when unset rather than sent as null —
    absent is the wire contract for "defaults to the id" and "a work warehouse"."""
    body: dict[str, object] = {"id": warehouse.id, "project": project.id}
    if warehouse.bucket:
        body["bucket"] = warehouse.bucket
    if warehouse.serving:
        body["serving"] = warehouse.serving
    return body


def plan(estate: Estate, subs: Mapping[str, str], *, delimiter: str = DELIMITER) -> list[Step]:
    """The estate description flattened into calls, LAYER BY LAYER: projects, warehouses, namespaces,
    tables, then grants.

    Layer-by-layer rather than depth-first per tenant on purpose: it makes the invariant the script is
    built on ("no warehouse is attempted until every project exists") true of the whole run, not just of
    one branch — so a single broken tenant cannot leave a later one half-made.
    """
    steps: list[Step] = [
        Step(
            layer="project",
            label=f"project {project.id}",
            method="POST",
            path="/v1/projects",
            body={"id": project.id},
            key=f"project:{project.id}",
        )
        for project in estate.projects
    ]
    steps.extend(
        Step(
            layer="warehouse",
            label=f"warehouse {warehouse.id} (project {project.id})",
            method="POST",
            path="/v1/warehouses",
            body=_warehouse_body(project, warehouse),
            key=f"warehouse:{warehouse.id}",
            parent=f"project:{project.id}",
        )
        for project, warehouse in _warehouse_pairs(estate)
    )
    steps.extend(
        Step(
            layer="namespace",
            label=f"namespace {namespace.name} (in {warehouse.id})",
            method="POST",
            # The warehouse-scoped route is the ONLY door for a top-level namespace:
            # /v1/namespace/{id}/create refuses one outright (it cannot name a warehouse to bind it to),
            # so a namespace created there would have nowhere for its bytes to land.
            path=f"/v1/warehouses/{warehouse.id}/namespaces",
            body={"namespace": namespace.name},
            key=f"namespace:{namespace.name}",
            parent=f"warehouse:{warehouse.id}",
            tolerate=CONVERGED,
            refuses=BINDING_REFUSALS,
        )
        for warehouse, namespace in _namespace_pairs(estate)
    )
    steps.extend(
        Step(
            layer="table",
            label=f"table {namespace.name}{delimiter}{table}",
            method="POST",
            # `declare` and not `create`: it reserves an EMPTY table over JSON and seeds the same
            # ownership + lineage a create does, where `create` wants an Arrow stream. A seed's job is
            # that the object exists and is governed, not that it holds rows.
            path=f"/v1/table/{namespace.name}{delimiter}{table}/declare",
            body={"id": [namespace.name, table]},
            key=f"table:{namespace.name}{delimiter}{table}",
            parent=f"namespace:{namespace.name}",
            tolerate=CONVERGED,
        )
        for namespace, table in _table_pairs(estate)
    )
    planned = {step.key for step in steps}
    for grant in estate.grants:
        # A table grant names its object by PATH, so it carries the delimiter this run is using rather
        # than a hardcoded one — see Grant. Resolved here, once, so `body`, `label` and the `parent`
        # lookup all speak of the SAME object; resolving it in only one of the three is how a grant
        # ends up correctly addressed and wrongly ordered, or the reverse.
        obj = grant.object.replace("{delimiter}", delimiter)
        user = remap_subject(grant.user, subs)
        steps.append(
            Step(
                layer="grant",
                label=f"{obj} #{grant.relation} @ {user}",
                method="POST",
                path="/v1/access/tuples",
                body={"user": user, "relation": grant.relation, "object": obj},
                # No key: a tuple brings nothing into existence, so nothing hangs off it. The parent is
                # the object it decorates WHEN this run is what creates that object — see Step.parent.
                parent=obj if obj in planned else None,
                tolerate=FGA_OFF,
            )
        )
    return steps


# --------------------------------------------------------------------------- #
# Execution + reporting
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one step actually did: ``created`` / ``converged`` / ``skipped`` / ``failed``."""

    step: Step
    state: str
    note: str = ""


def problem(response: httpx.Response) -> str:
    """The refusal in the guard's OWN words — the RFC 9457 ``title``/``detail``/``code``, not a bare status.

    A seed exists to make a refusal legible: "409" says nothing, "NamespaceAlreadyExistsError [code 2]:
    namespace 'acme-gold' is already bound to another warehouse" says what to do next.
    """
    try:
        body = response.json()
    except ValueError:
        return (response.text or "").strip()[:400] or "<empty body>"
    if not isinstance(body, dict):
        return json.dumps(body)[:400]
    title = str(body.get("title") or "")
    detail = str(body.get("detail") or body.get("error") or "")
    if errors := body.get("errors"):
        # The 422 validation shape carries no `detail` — the field errors ARE the message.
        detail = "; ".join(f"{e.get('field')}: {e.get('message')}" for e in errors if isinstance(e, dict))
    code = body.get("code")
    head = f"{title} [code {code}]" if code is not None and title else title
    if head and detail:
        return f"{head}: {detail}"
    return detail or head or json.dumps(body)[:400]


def seed(http: httpx.Client, steps: Sequence[Step]) -> list[Outcome]:
    """Walk the plan, printing one line per step; never stop early, never lie about what landed."""
    outcomes: list[Outcome] = []
    blocked: set[str] = set()
    layer = ""
    for step in steps:
        if step.layer != layer:
            layer = step.layer
            print(f"\n== {layer}s")
        if step.parent is not None and step.parent in blocked:
            note = f"its parent {step.parent} did not land"
            print(f"  -  {step.label}: skipped, {note}")
            outcomes.append(Outcome(step, "skipped", note))
            if step.key is not None:
                blocked.add(step.key)
            continue
        try:
            response = http.request(step.method, step.path, json=step.body)
        except httpx.HTTPError as exc:
            note = f"{type(exc).__name__}: {exc}"
            print(f"  !! {step.label}: {note}", file=sys.stderr)
            outcomes.append(Outcome(step, "failed", note))
            if step.key is not None:
                blocked.add(step.key)
            continue
        if response.is_success:
            print(f"  +  {step.label}")
            outcomes.append(Outcome(step, "created"))
            continue
        note = problem(response)
        tolerated = step.tolerate.get(response.status_code)
        # A tolerated STATUS is not a tolerated REFUSAL. The catalog answers a takeover guard with the same
        # error class and code as "it is already here", so the phrase — not the status — decides.
        if tolerated is not None and not any(phrase in note for phrase in step.refuses):
            state, word = tolerated
            print(f"  =  {step.label}: {word} ({note})")
            outcomes.append(Outcome(step, state, note))
            continue
        note = f"HTTP {response.status_code} {note}"
        print(f"  !! {step.label}: {note}", file=sys.stderr)
        outcomes.append(Outcome(step, "failed", note))
        if step.key is not None:
            blocked.add(step.key)
    return outcomes


def _read(http: httpx.Client, path: str) -> tuple[object | None, str]:
    """GET ``path`` and DECODE it, collapsing transport failure, refusal and an unreadable body into one note.

    All three are the same finding for a read-back — "the estate could not be shown" — and all three have
    to be caught HERE: an exception escaping this function aborts the run AFTER the writes landed, which is
    the one moment the operator most needs the summary. A 200 carrying HTML (a proxy answering instead of
    the catalog) is exactly that case, and it is the likeliest one when the URL is wrong.
    """
    try:
        response = http.get(path)
    except httpx.HTTPError as exc:
        return None, f"GET {path} -> {type(exc).__name__}: {exc}"
    if not response.is_success:
        return None, f"GET {path} -> HTTP {response.status_code} {problem(response)}"
    try:
        return response.json(), ""
    except ValueError:
        return None, f"GET {path} -> HTTP {response.status_code} with a body that is not JSON: {(response.text or '')[:200]!r}"


def _as_list(value: object) -> list[object]:
    """``value`` when it really is a list, else empty.

    The read-back parses whatever the catalog sent, so every field is ``object`` until proven otherwise. A
    field of the wrong shape must degrade to "nothing there" — which the comparison then reports as missing
    — rather than raise, because an exception here loses the summary after the writes have landed.
    """
    return list(value) if isinstance(value, list) else []


def _as_record(value: object) -> dict[str, object]:
    """``value`` when it really is a JSON object, else empty. Same posture as :func:`_as_list`."""
    return {str(key): item for key, item in value.items()} if isinstance(value, dict) else {}


def summarize(http: httpx.Client, estate: Estate, *, delimiter: str = DELIMITER) -> list[str]:
    """Print the estate as the CATALOG now sees it, COMPARED against the plan; return everything missing.

    Read back and checked, not merely printed. A 2xx on every write proves the calls were ACCEPTED, and the
    ghost estate is precisely the state where authorization agrees and the registry does not — so a summary
    that only renders whatever the catalog happens to return can print an empty estate under a run that
    reports success. Every project, warehouse, namespace and table the plan named must appear in a real
    read; one that does not is returned as a failure, because the closing claim of this script is that they
    exist in the catalog and not only in the tuple store.
    """
    print("\n== the estate now (read back from the catalog)")
    problems: list[str] = []

    def absent(note: str) -> None:
        print(f"  !! {note}", file=sys.stderr)
        problems.append(note)

    payload, note = _read(http, "/v1/projects")
    if not isinstance(payload, list):
        absent(note or f"GET /v1/projects answered {type(payload).__name__}, not a list of projects")
        return problems
    records = [_as_record(item) for item in payload]
    listed = {str(record["project"]): record for record in records if "project" in record}

    for project in estate.projects:
        record = listed.get(project.id)
        if record is None:
            absent(f"project {project.id} is absent from GET /v1/projects — the registry never got it")
        else:
            print(f"  project {project.id}")
            held: set[str] = set()
            for entry in (_as_record(item) for item in _as_list(record.get("warehouses"))):
                held.add(str(entry.get("id")))
                serving = f" serving={entry['serving']}" if entry.get("serving") else ""
                print(f"    warehouse {entry.get('id')}  bucket={entry.get('bucket')}  status={entry.get('status')}{serving}")
            admins = ", ".join(str(admin) for admin in _as_list(record.get("admins")))
            print(f"    admins: {admins or '(none — FGA off or unavailable)'}")
            for warehouse_spec in project.warehouses:
                if warehouse_spec.id not in held:
                    absent(f"warehouse {warehouse_spec.id} is absent from project {project.id}'s warehouses")
        # Read the namespaces WHETHER OR NOT the project listed. Nesting this under the listing is how a
        # missing tenant takes its whole subtree's verification down with it in silence — and a subtree
        # nobody checked is indistinguishable from one that is there.
        for warehouse_spec in project.warehouses:
            for namespace in warehouse_spec.namespaces:
                _summarize_namespace(http, namespace, delimiter=delimiter, absent=absent)
    return problems


def _summarize_namespace(http: httpx.Client, namespace: Namespace, *, delimiter: str, absent: Callable[[str], None]) -> None:
    """Print one namespace's tables as the catalog lists them, reporting every declared table that is not there."""
    payload, note = _read(http, f"/v1/namespace/{namespace.name}/table/list")
    if not isinstance(payload, dict):
        absent(note or f"namespace {namespace.name} could not be listed")
        return
    tables = [str(name) for name in _as_list(_as_record(payload).get("tables"))]
    print(f"    namespace {namespace.name}: {', '.join(tables) or '(no tables)'}")
    for table in namespace.tables:
        # `list_tables` answers with names relative to the namespace; the qualified form is accepted too
        # rather than depending on which one a given catalog build returns.
        if table not in tables and f"{namespace.name}{delimiter}{table}" not in tables:
            absent(f"table {table} is absent from namespace {namespace.name}")


def main(argv: Sequence[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    """Seed the estate; return 0 only when every step landed AND the result could be read back."""
    parser = argparse.ArgumentParser(description="Seed the demo estate through the catalog's real APIs, in hierarchy order.")
    parser.add_argument("--catalog", default=CATALOG_URL, help=f"catalog base URL (default {CATALOG_URL})")
    parser.add_argument("--token", default=CATALOG_TOKEN, help="OIDC bearer for the governed doors (default $SEED_CATALOG_TOKEN; empty = auth off)")
    parser.add_argument("--delimiter", default=DELIMITER, help=f"namespace/table delimiter, must match LANCE_NS_DELIMITER (default {DELIMITER!r})")
    parser.add_argument("--timeout", type=float, default=30.0, help="per-request timeout in seconds (default 30)")
    parser.add_argument("--dry-run", action="store_true", help="print the calls in order, make none of them")
    parser.add_argument("--map", action="append", default=[], metavar="NAME=SUB", help="override a demo identity's OIDC subject (repeatable)")
    args = parser.parse_args(argv)

    subs = dict(DEX_SUBS)
    for pair in args.map:
        name, _, sub = pair.partition("=")
        if not sub:
            print(f"!! --map wants NAME=SUB, got {pair!r}", file=sys.stderr)
            return 1
        subs[name] = sub

    steps = plan(DEMO_ESTATE, subs, delimiter=args.delimiter)
    print(f"  {len(steps)} steps against {args.catalog}")
    print(f"  identities mapped to their OIDC subject: {', '.join(sorted(subs))}")
    print(f"  bearer: {'yes' if args.token else 'NO — assuming an auth-off stack'}")
    if args.dry_run:
        for step in steps:
            print(f"    {step.layer:9} {step.method:4} {step.path}  {json.dumps(step.body) if step.body else ''}")
        return 0

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    with httpx.Client(base_url=args.catalog.rstrip("/"), timeout=args.timeout, headers=headers, transport=transport) as http:
        outcomes = seed(http, steps)
        problems = summarize(http, DEMO_ESTATE, delimiter=args.delimiter)

    tally: dict[str, int] = {}
    for outcome in outcomes:
        tally[outcome.state] = tally.get(outcome.state, 0) + 1
    print("\n  " + "  ".join(f"{state}={count}" for state, count in sorted(tally.items())))
    failed = [outcome for outcome in outcomes if outcome.state == "failed"]
    if failed or problems:
        print("\n!! NOT seeded — the estate is incomplete:", file=sys.stderr)
        for outcome in failed:
            print(f"   {outcome.step.label}: {outcome.note}", file=sys.stderr)
        for note in problems:
            print(f"   read-back: {note}", file=sys.stderr)
        return 1
    print("\n  seeded — every object above exists in the catalog, not only in the tuple store")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
