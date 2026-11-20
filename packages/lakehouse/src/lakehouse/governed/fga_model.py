"""The authorization model as data: the model this image carries, the type restrictions a tuple must meet, and the
hierarchy's two-way edge.

The authorization model (``lakehouse/governed/auth/model.fga`` / ``model.json``) defines TEN types —
``user``, ``role`` (``role#assignee``, the one group subject), ``revision`` (bookkeeping of a project's admins),
``project``, ``estate`` (the estate root), ``warehouse`` (the S3-bucket root), a self-nesting ``namespace``,
``table``, ``classification`` and ``materialized_view`` (hanging off namespace/warehouse and inheriting the same
rungs via ``parent``). Every one is a lakehouse object or a subject of one: a tool that works on the lakehouse
authorizes its own objects itself (ADR 0171). The count is worth keeping honest because it is the first thing
a reader checks this docstring against.
Privileges are concentric (``owner`` ⊇ ``writer`` ⊇ ``reader``) and cascade DOWN the hierarchy via ``parent`` (project → warehouse → namespace →
nested namespace → table / materialized_view). Every API operation is checked against a
``can_*`` ACTION relation — the model owns the op→privilege map, the app just names the action (see
``services/catalog/api/fga_deps.py``).

Nothing here dials OpenFGA; the reads, the writes and the provisioning do (`fga_reads`, `fga_writes`, `fga_provision`).
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

from openfga_sdk.client.models import ClientTuple


_MODEL_PATH = Path(__file__).resolve().parent / "auth" / "model.json"
_MODEL_DSL_PATH = _MODEL_PATH.with_name("model.fga")


# Parent types that declare a `child` relation in the model, and therefore take the inverse edge
# `grant_on_create` writes for upward visibility. Deliberately a whitelist keyed on the PARENT's type:
# the model gives `project` no `child`, and a `child` tuple on a type that does not define it is
# accepted by OpenFGA and read by no rule — a silent no-op. Adding a `child` relation to a type in
# `model.fga` means adding it here too.
CHILD_EDGE_PARENT_TYPES = frozenset({"warehouse", "namespace"})


def hierarchy_edge_tuples(*, child_object: str, parent_object: str) -> list[ClientTuple]:
    """BOTH directions of one parent→child link, so a caller cannot write half of it.

    OpenFGA cannot walk a tuple backwards, so `can_get_metadata: reader or can_get_metadata from
    child` needs the inverse edge STORED. An object holding the forward `parent` edge alone has
    silently dead upward visibility: the grantee can read the table and cannot see the namespace or
    warehouse that contains it, so their own breadcrumb 403s and every list above it comes back empty.

    That is why the pairing is a function rather than a rule people are expected to remember — a
    one-directional link is a perfectly valid tuple that simply never resolves, so it is invisible to
    every mocked-client test and to the API itself. The set of callers is closed, and the TID251
    banned-api entry on `lakehouse.governed.fga_model.hierarchy_edge_tuples` holds it that way: its callers are
    `grant_on_create` in `fga_writes`, where the catalog's create-door seed lands, and the steps a link moves or arrives
    by (`move_parent`, `leave_parent`, `unname_child`, `name_child`, `grant_before_arrival`), which leave
    it whole once the object is at rest.

    The inverse is written only when the PARENT's type declares `child` (:data:`CHILD_EDGE_PARENT_TYPES`):
    a `child` tuple on a type that does not is accepted by the API and read by no rule.

    Idempotent by construction — `write_tuples` swallows a duplicate — so it is safe over an estate
    that already has some of these, and `revoke_object_tuples` already reconstructs and removes the
    inverse on drop, so nothing is orphaned by adding them.
    """
    tuples = [ClientTuple(user=parent_object, relation="parent", object=child_object)]
    if parent_object.split(":", 1)[0] in CHILD_EDGE_PARENT_TYPES:
        tuples.append(ClientTuple(user=child_object, relation="child", object=parent_object))
    return tuples


def load_model() -> dict[str, Any]:
    """Load the authorization model JSON shipped with the app."""
    return json.loads(_MODEL_PATH.read_text())


@functools.cache
def model_types() -> frozenset[str]:
    """The object types the bundled authorization model defines."""
    return frozenset(str(definition["type"]) for definition in load_model()["type_definitions"])


@functools.cache
def _type_restrictions(fga_type: str) -> dict[str, frozenset[str]]:
    """Each relation the bundled model defines on ``fga_type`` and the subjects a tuple of it may name, as the model's DSL
    writes them (``user``, ``user:*``, ``role#assignee``, ``user with non_expired_grant``); a computed relation names none."""
    definition = next((d for d in load_model()["type_definitions"] if d["type"] == fga_type), {})
    relations = (definition.get("metadata") or {}).get("relations") or {}
    return {relation: frozenset(map(_restriction, meta.get("directly_related_user_types") or ())) for relation, meta in relations.items()}


def _restriction(entry: dict[str, Any]) -> str:
    """One type restriction of the model's JSON, written as its DSL writes it."""
    if entry.get("relation"):
        form = f"{entry['type']}#{entry['relation']}"
    elif "wildcard" in entry:
        form = f"{entry['type']}:*"
    else:
        form = str(entry["type"])
    return f"{form} with {entry['condition']}" if entry.get("condition") else form


def type_restrictions(fga_type: str, relation: str) -> frozenset[str]:
    """The subjects a tuple of ``relation`` on ``fga_type`` may name, as the model's DSL writes them; none for a relation
    the model does not define there."""
    return _type_restrictions(fga_type).get(relation, frozenset())


def admits(fga_type: str, relation: str, user: str) -> bool:
    """Whether a tuple of ``relation`` on ``fga_type`` carrying no condition may name ``user``, as the store judges it:
    ``user:<id>`` meets ``user``, ``user:*`` meets ``user:*`` and ``role:<id>#assignee`` meets ``role#assignee``."""
    head, _, userset = user.partition("#")
    user_type, _, user_id = head.partition(":")
    met = f"{user_type}#{userset}" if userset else f"{user_type}:*" if user_id == "*" else user_type
    return met in type_restrictions(fga_type, relation)


def machine_relations(fga_type: str) -> frozenset[str]:
    """The relations on ``fga_type`` only a user holds directly (``[user]``), the model's machine roles: no role
    assignment, userset, wildcard or condition confers one, so each grant of one names one account ([[LH-563]])."""
    return frozenset(relation for relation, restrictions in _type_restrictions(fga_type).items() if restrictions == {"user"})


def load_model_dsl() -> str:
    """The checked-in authorization model DSL text (``model.fga``) — the human-readable twin of the
    compiled ``model.json`` the app loads (the two are kept in sync; ``model.fga.yaml`` proves the
    behaviour). Served read-only by the estate-admin ``GET /management/v1/access/model`` surface."""
    return _MODEL_DSL_PATH.read_text()


__all__ = [
    "CHILD_EDGE_PARENT_TYPES",
    "admits",
    "hierarchy_edge_tuples",
    "load_model",
    "load_model_dsl",
    "machine_relations",
    "model_types",
    "type_restrictions",
]
