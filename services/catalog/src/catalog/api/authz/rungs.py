"""The rung each door asks, as data and pure lookups: the route mounts and suffixes, and the owner, writer, read,
create-on-parent, grant, batch, transaction, staged and vend-tier tables.

The op -> ``can_*`` mapping here is the ONLY policy logic in the app; the privilege math
(which rung each ``can_*`` reduces to, and how owner/writer/reader cascade through
``parent``) lives in the model (``lakehouse/governed/auth/model.fga``) — so it is testable in ``model.fga.yaml``. Mapping:

  read ops   -> can_get_metadata / can_read_data        (reader rung)
  mutations  -> can_write_data / can_update_properties   (writer rung)
  lifecycle  -> can_drop / can_deregister / can_delete   (owner rung)
  create     -> can_create_table / can_create_namespace on the PARENT (create-on-parent)
  move       -> can_move on the table / can_accept_moved_table on the namespace it enters (grant authority)
  undeclared -> refused for every caller                 (no rung is a default)

Create-on-parent: creating a child authorizes the parent (the child has no id yet), which for a
namespace directly in a warehouse is the warehouse.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from lance_namespace import InternalError

from catalog.api.read_actions import DATA_READ_ACTIONS, META_READ_ACTIONS
from catalog.core import write_judge


__all__ = [
    "ACCEPT_MOVED_TABLE_RELATION",
    "BATCH_OWNER_OPS",
    "BATCH_PATHS",
    "CLASSIFYING_DOOR",
    "CLASSIFY_RELATION",
    "CREATE_ON_PARENT_SUFFIXES",
    "FGA_TYPE",
    "GRANT_SUFFIXES",
    "JUDGED_RESOURCES",
    "LISTING_BATCH",
    "MOST_TABLES_IN_A_BATCH",
    "OWNER_SUFFIX_RELATION",
    "READ_RELATIONS",
    "ROOT_READ_SUFFIXES",
    "STAGED_OPERATION_RELATION",
    "TENANT_HELD",
    "TRANSACTION_SUFFIXES",
    "TRASH_DOORS",
    "UNIDENTIFIED_TABLE_ESTATE_VERB",
    "UNIDENTIFIED_TABLE_RUNG",
    "UUID_KEYED",
    "VEND_TIER_RUNGS",
    "WAREHOUSE_NAME_SUFFIXES",
    "WRITER_SUFFIX_RELATION",
    "action_relation",
    "create_parent_rung",
    "moves_namespace",
    "rename_relation",
    "resource_for",
    "route_suffix",
    "undeclared_door",
]


# Guarded resource prefixes, in match order. Each but ``transaction`` maps to its own dedicated FGA type in the model
# (``lakehouse/governed/auth/model.fga``). A transaction is no object of its own: its id names the table it belongs
# to, and both its doors are authorized on that table (``authorize_transaction``, [[LH-221]]).
#: `classification` is the odd one and belongs here anyway: it is a governance VOCABULARY rather than a
#: data object — no rows, no parent warehouse, and the path segment IS the label ([[LH-055]]). It
#: reaches this map because its grant/revoke doors must be authorized like every other, through
#: `authorize_grant` reading the rung out of the body.
_RESOURCES: tuple[str, ...] = ("namespace", "table", "materialized_view", "transaction", "classification")


FGA_TYPE: dict[str, str] = {
    "namespace": "namespace",
    "table": "table",
    "materialized_view": "materialized_view",
    "classification": "classification",
}


# The read actions and why each is one: `catalog.api.read_actions`, which the read-only window judges by too.

#: The rungs that mean "this door only READS". Keyed on the relation `action_relation` resolved, so a
#: door is classified by what it DOES rather than by how its path happens to be spelled — `access/list`
#: is owner-tier and its trailing segment is the word "list".
READ_RELATIONS: Final = frozenset({"can_get_metadata", "can_read_data", "can_read", "can_get_protection"})


#: A read served at the path of the door that sets what it reads, told from it by its method: the relation a ``GET``
#: there asks. The trail records the relation and never the route, so a read asked on the set door's relation would be
#: recorded as a set ([[LH-585]]).
_READ_AT_A_SET_DOOR: Final = {("table", "protection"): "can_get_protection", ("namespace", "protection"): "can_get_protection"}


# Full route suffixes that create a NEW child -> authorize the parent (create-on-parent).
CREATE_ON_PARENT_SUFFIXES: dict[str, frozenset[str]] = {
    "table": frozenset({"create", "declare", "register"}),
    "namespace": frozenset({"create"}),
    "materialized_view": frozenset({"create"}),
}


# Full route suffixes that are lifecycle/destructive/ref-plane on the object itself (owner rung).
# Matched on the FULL suffix so ``index/{index_name}/drop`` does not inherit ``drop``'s owner rung.
# ``rename`` is owner-tier: it RETIRES the name every reader and pipeline addresses the table by, so it is the owner's
# act on the source. Its grants stay on its uuid (ADR 0173 §8), which is why a rename into ANOTHER namespace asks
# :data:`_MOVE_RELATION` in this rung's place (:func:`rename_relation`).
# ``restore`` REWINDS the table to an older version (destructive) and ``branches/create`` FORKS history;
# both are privileged and stay at the owner rung. A table writer writes every branch of the table (ADR 0159 D3): a
# branch is a parameter of a table operation, so ``insert``, ``merge_insert`` and a write vend naming one ask
# ``can_write_data`` on the table as on main. ``tags/create`` / ``tags/update`` re-point the
# ref-plane and are privileged too — above the writer rung, since moving a named pointer is no data
# write — but they resolve through ``owner or tagger`` rather than
# ``owner`` alone (owner ruling 2026-09-10). A tag is a NAMED POINTER at a version that already exists,
# so blessing one destroys nothing, and welding that to ownership is what forced every pipeline identity
# to hold ``owner`` on every tenant. Their ``*/list`` / ``*/version`` siblings are reads, resolved on the
# trailing segment below; ``tags/delete`` is mapped explicitly just below.
OWNER_SUFFIX_RELATION: dict[str, dict[str, str]] = {
    "table": {
        "drop": "can_drop",
        "deregister": "can_deregister",
        "rename": "can_drop",
        "restore": "can_restore",
        "branches/create": "can_create_branch",
        # DELETING a branch ends its own version sequence and makes the files only it references reclaimable: history
        # reclaim, which protection does not guard (ADR 0150), so this rung is the door's whole gate. Absent from this
        # map it fell through to the
        # writer default, so measured on the deployed catalog 2026-09-11 a non-owner cleared
        # `branches/delete` (422, past the gate) while the same caller was refused `tags/delete` and
        # `deregister` (403): the estate permitted the greater destruction and refused the lesser,
        # because a tag is a POINTER and its delete was already mapped here.
        #
        # `can_drop`, not a new relation: the rung already means "may destroy this table's contents" and
        # is what `tags/delete` and `version/delete` clear. A `can_delete_branch` would be a rung the
        # model must define and every existing owner grant would then lack.
        "branches/delete": "can_drop",
        "tags/create": "can_create_tag",
        "tags/update": "can_update_tag",
        # DELETE completes the tag lifecycle, and it clears the owner bar because a tag pins its version against
        # cleanup: at the writer rung a plain data writer could remove the pin a reader's `stable` is.
        #
        # `can_drop` rather than a new relation: the bar it has to clear is the one already carried by
        # `maintenance/run`, which reclaims version history and EXEMPTS tagged versions. The door that
        # respects the tag cannot be the expensive one while the door that removes it is cheap.
        "tags/delete": "can_drop",
        # Same rung, same reason, one step more direct: this destroys VERSIONS rather than a pin — the
        # reclamation `maintenance/run` performs, on versions the caller names. The door refuses a
        # tagged, forked-from or current version itself; the rung decides who may shorten the history at
        # all. `version/list` and the read-side siblings still fall through to the reader tier.
        #
        # This rung carries more weight here than it would in an Iceberg catalog, and deliberately:
        # Lance keeps the CAS in the object store, so the serving pointer is a tag INSIDE the dataset
        # rather than a row a catalog transaction owns. Authorization is the only thing guarding it.
        "version/delete": "can_drop",
        # #50 maintenance policies: a retention policy authorizes destroying this table's version
        # history over time — the drop rung, not a plain write. `policy/describe` falls through to
        # the reader tier via its trailing segment.
        "policy/set": "can_drop",
        "policy/delete": "can_drop",
        # #51 access review: enumerating who holds access reveals principals, so it clears the
        # owner bar — a reader must not be able to map the ACL they are inside of.
        "access/list": "can_drop",
        # #68 access simulation (the "who can do what" playground check): probing an arbitrary
        # (user, relation) is the same authz-graph disclosure as enumerating it, so it clears the
        # same owner bar.
        "access/check": "can_drop",
        # The SELF-view is the one access route that must NOT clear the owner bar. `access/list` and
        # `access/check` disclose principals; "what may I do here" discloses only the caller to
        # themselves. Owner-gating it would mean only the people who already know the answer could
        # ask — which is precisely why no surface over these primitives ever shipped. Reader tier:
        # you may ask about anything you can see. Mapped EXPLICITLY: `my-permissions` is no read word, so
        # the trailing-segment rule below cannot classify it.
        "access/my-permissions": "can_get_metadata",
        # NOTE: `access/grant` and `access/revoke` are NOT here. They gated on the owner bar until the
        # grant axis landed, which meant handing out access required owning the data. They are now
        # authorized per-rung from the BODY (`can_grant_<relation>`) by `authorize_grant`, which
        # intercepts before `action_relation` is consulted — a mapping here would be dead code that
        # reads like the live rule.
        # #81 authorization graph: one hop of the relationship graph discloses principals + the cascade,
        # the same disclosure as access/list — owner bar.
        "access/graph": "can_drop",
        # #75 on-demand maintenance: previewing and reclaiming version history are the drop rung, like the
        # retention policy that schedules them.
        "maintenance/preview": "can_drop",
        "maintenance/run": "can_drop",
        # [[LH-575]] the two commits maintenance's passes make, which are the catalog's, since its vend puts nothing of a
        # table: a disarm removes Lance's commit-path cleanup keys and a floor raise commits a stamp, neither changing a row
        # or which versions a cleanup may reach. The maintainer rung the sweep holds, which every owner keeps.
        "maintenance/disarm": "can_maintain",
        "maintenance/floor": "can_maintain",
        # [[LH-073]] erasure destroys the table's PAST — every ref's rows, the tags pinning the versions
        # that held them, and the history itself. That is a stronger claim than a drop, and `can_drop`
        # is the highest rung there is, so it gates here. The writer rung would let a plain data writer
        # reclaim an owner's history under a compliance-shaped verb.
        "erasure": "can_drop",
        # [[LH-493]] verifying an erasure obligation reclaims the history the requester's `retain_days` already ordered
        # reclaimed and releases the obligation. It is the bring-your-own job's step, so it sits at the maintainer rung
        # the job holds beside `writer` (ADR 0163); the owner holds it too. The caller's word decides nothing: the
        # catalog reads storage itself and holds the predicate to the obligation's digest.
        "erasure/{obligation_id}/verify": "can_maintain",
        # #73 deletion protection: arming/disarming the safety on an object is a statement about its
        # DESTRUCTION, so it clears the same owner bar as the drop it guards — a writer must not be
        # able to disarm protection they could never act on.
        #
        # ITS OWN RELATION at that same tier ([[LH-055]]), not `can_drop`: `#41` audits every decision by
        # the RELATION, and the route suffix is not in the record, so sharing the name made "dropped this
        # table" and "armed the safety on it" write identical audit lines. `can_set_protection: owner` is
        # a computed userset, so every owner already holds it and no grant had to change.
        "protection": "can_set_protection",
        # Labels (ADR 0211) classify the table for the people who find and govern it, so attaching one is the owner's
        # act, as arming its protection is; its own relation so the trail tells the two apart. Reading them is
        # `labels/describe`, a metadata read by its trailing word.
        "labels/set": "can_label",
        "labels/remove": "can_label",
        # #75 undrop RESTORES the object into its namespace — the same authority as removing it.
        "undrop": "can_drop",
        # A table's data contract (ADR 0212) decides what its commits may do to its schema, so setting or deleting one
        # is the owner's act; its own relation so the trail tells it from a drop. Reading it is a metadata read.
        "contract/set": "can_set_contract",
        "contract/delete": "can_set_contract",
    },
    "namespace": {
        "drop": "can_delete",
        # #96 undrop RESTORES the subtree into the estate — the same authority as removing it (the
        # table door's #75 rule at the namespace rung).
        "undrop": "can_delete",
        # The namespace twin of the table door's rung — same tier as the `can_delete` it guards, its own
        # name for the same audit reason.
        "protection": "can_set_protection",
        # The table block's label doors, one level up.
        "labels/set": "can_label",
        "labels/remove": "can_label",
        "policy/set": "can_delete",
        "policy/delete": "can_delete",
        "access/list": "can_delete",
        "access/check": "can_delete",
        # Same as the table block: grant/revoke are body-authorized by `authorize_grant`.
        # #C4 managed access. SUFFIX PER OPERATION (`policy/describe` vs `policy/set`, the convention
        # already here) — and not stylistic: this map is keyed on the PATH ALONE, so one `managed-access`
        # suffix serving both a read and a write can only carry ONE tier, and the write's tier is the
        # wrong one for the read.
        #
        # The write is a grant-management act, not a property edit — `can_set_managed_access` derives
        # from `manage_grants`. At the writer rung (`can_update_properties`) any writer could switch off
        # the control that stops owners widening access.
        "managed-access/set": "can_set_managed_access",
        # The read sits at the READER bar deliberately: this flag is the REASON someone's grant controls
        # are missing, so the person who most needs the answer is exactly the one who may not change it.
        # Gating the read at the write's bar turns a stated policy back into an unexplained absence.
        "managed-access/describe": "can_get_metadata",
        # The self-view sits at the READER bar here too — see the table block for why. Spelled out per type:
        # each type's map is its own, and a suffix it does not declare is refused.
        "access/my-permissions": "can_get_metadata",
        "access/graph": "can_delete",
    },
}


#: What a rename into ANOTHER namespace asks: on the table, in the owner rung's place, and on the namespace it enters,
#: in ``can_create_table``'s ([[LH-568]]). A table's grants are held by its uuid and go where it goes, so a move
#: re-homes every one of them. Out of a managed scope that would hand its owner back the grant authority managed
#: access took, and into one it would bring grants nobody holding grant authority there made; the model states both
#: rules on these two relations.
_MOVE_RELATION: Final = "can_move"


ACCEPT_MOVED_TABLE_RELATION: Final = "can_accept_moved_table"


def moves_namespace(source: Sequence[str], destination: Sequence[str]) -> bool:
    """Whether a rename of table ``source`` to ``destination`` takes it into another namespace."""
    return tuple(source[:-1]) != tuple(destination[:-1])


def rename_relation(source: Sequence[str], destination: Sequence[str]) -> str:
    """The rung a rename of table ``source`` to ``destination`` asks on the source: the owner map's while the table
    stays in its namespace, :data:`_MOVE_RELATION` when it leaves it."""
    return _MOVE_RELATION if moves_namespace(source, destination) else OWNER_SUFFIX_RELATION["table"]["rename"]


#: The WRITER rung, declared door by door on the FULL suffix exactly like the owner map above, because a
#: suffix that neither map nor the read vocabulary names is REFUSED rather than
#: defaulted. A default rung is a permission nobody chose. At the writer rung it is one every plain data
#: writer already holds, so a new destructive verb would ship open to all of them with nothing red; and
#: a new read door would refuse its whole audience while the audit trail recorded a write. The refusal
#: is loud for every caller, owners included, and
#: `services/catalog/tests/test_an_undeclared_door_is_refused.py` drives the guard over every mounted
#: route, so a door added without a rung fails there rather than in front of its first caller.
#:
#: A `{name}` segment stands for exactly one path segment, as the route's own `str` convertor does, so
#: `index/{index_name}/drop` names the index-drop door without reaching the table's own `drop`.
#:
#: `namespace` declares no writer door. Every namespace route is a read, an owner-tier verb or a
#: create-on-parent, so a namespace mutation nobody declared is refused rather than resolved to
#: `can_update_properties`.
WRITER_SUFFIX_RELATION: Final[dict[str, dict[str, str]]] = {
    "table": {
        **dict.fromkeys(
            (
                "insert",
                "merge_insert",
                "update",
                "delete",
                "add_columns",
                "alter_columns",
                "drop_columns",
                "backfill_column",
                "update_field_metadata",
                "schema_metadata/update",
                "create_index",
                "create_scalar_index",
                "index/{index_name}/drop",
            ),
            "can_write_data",
        ),
        # A writer's or a maintainer's: the staged operation then asks its own rung (`require_staged_operation`), so a
        # compaction job commits as a maintainer and holds no writer rung.
        "version/create": "can_create_version",
    },
    # The async rematerialize. `create` is create-on-parent and never reaches the resolver.
    "materialized_view": {"refresh": "can_refresh"},
}


# Grant/revoke are authorized from the BODY, per rung, by `authorize_grant` — they appear in neither
# block above on purpose. `authorize` intercepts these suffixes before `action_relation` runs, so a
# mapping here would never be consulted while reading exactly like the live rule.
GRANT_SUFFIXES = frozenset({"access/grant", "access/revoke"})


# Body-keyed batch routes (no ``{id}`` path param); the tables are named in the body.
BATCH_PATHS = frozenset({"/v1/table/version/batch-create", "/v1/table/batch-commit"})


# A batch sub-operation is authorized at its dedicated route's rung, and a destructive one at the
# owner tier: deregister_table is the only destructive kind, since the wire model
# (CommitTableOperation) has no drop op. Both batch doors answer 406 and run nothing ([[LH-206]]);
# the guard answers first, as it does at every door, so a caller without the rung reads 403.
# Maps op key -> can_* relation.
BATCH_OWNER_OPS: dict[str, str] = {"deregister_table": "can_deregister"}


#: The mount prefixes this guard understands. `/management/v1` is the rask-only surface ([[LH-021]]),
#: and it is HERE rather than assumed because both helpers below matched `/v1/` literally: the first
#: route mounted on the management prefix resolved to no resource, fell out of `authorize` entirely,
#: and was reachable by any authenticated principal. It was the ERASURE door, which destroys a table's
#: history. This list is what keeps the next management route from repeating it, and a route that falls out of it
#: is one `services/catalog/tests/test_a_door_the_router_leaves_open_decides_for_itself.py` finds with no entry.
_MOUNTS: Final = ("/management/v1", "/v1")


def resource_for(path: str) -> str | None:
    """The guarded resource a ``<mount>/<resource>/…`` path belongs to, else ``None``."""
    for mount in _MOUNTS:
        for resource in _RESOURCES:
            if path.startswith(f"{mount}/{resource}/"):
                return resource
    return None


#: The only actions allowed on the root namespace, which names no FGA object: reads that filter every
#: item they return (`list`, `table/list`) or reveal nothing a tuple guards (`describe`, `exists`).
ROOT_READ_SUFFIXES: Final = frozenset({"list", "table/list", "describe", "exists"})


def route_suffix(path: str, resource: str, object_id: str) -> str:
    """The route part after ``<mount>/<resource>/{id}/`` (e.g. ``version/create``).

    Checked against every mount, longest first, so `/management/v1/table/{id}/erasure` yields
    `erasure`. A path no mount strips yields `""`, which names no door and is refused.
    """
    clean = path.rstrip("/")
    for mount in _MOUNTS:
        prefix = f"{mount}/{resource}/{object_id}/"
        if clean.startswith(prefix):
            return clean.removeprefix(prefix)
    return ""


#: The types whose objects are keyed by the uuid the catalog mints (ADR 0173 §8, [[LH-524]]).
UUID_KEYED: Final = frozenset({"table", "namespace"})


#: The doors that act on an object the trash may hold under its name, which are addressed by that name (ADR 0173 §9).
TRASH_DOORS: Final = frozenset({"undrop", "tasks"})


def _declared_relation(declared: Mapping[str, str], suffix: str) -> str | None:
    """The relation ``declared`` names for ``suffix``, else ``None``.

    A ``{name}`` segment of a declared suffix matches exactly one non-empty segment of ``suffix``.
    """
    if suffix in declared:
        return declared[suffix]
    parts = suffix.split("/")
    for pattern, relation in declared.items():
        segments = pattern.split("/")
        if len(segments) == len(parts) and all(
            segment == part or (segment.startswith("{") and segment.endswith("}") and part != "") for segment, part in zip(segments, parts, strict=True)
        ):
            return relation
    return None


def undeclared_door(fga_type: str, suffix: str) -> InternalError:
    """The refusal for a door no rung map declares, for every caller — see ``WRITER_SUFFIX_RELATION``.

    ``InternalError`` (spec code 18), not ``PermissionDenied``: authorization runs after routing
    matched, so a caller cannot invent a suffix and only a route shipped without its rung reaches this.
    Code 15 would send the caller to ask for access no grant can give.
    """
    return InternalError(f"{fga_type} declares no rung for {suffix!r}, so no caller may reach it")


def action_relation(fga_type: str, suffix: str, *, method: str) -> str:
    """The ``can_*`` relation to check for a non-create op, by object type, route suffix and, where a read shares its
    path with the door that sets what it reads (:data:`_READ_AT_A_SET_DOOR`), the request's method.

    Owner- and writer-tier doors are declared on the FULL suffix; reads are classified by the trailing segment. Every returned relation exists on ``fga_type`` in the model.

    Raises:
        InternalError: Nothing declares ``suffix`` for ``fga_type`` (see :func:`undeclared_door`).
    """
    if method == "GET" and (relation := _READ_AT_A_SET_DOOR.get((fga_type, suffix))) is not None:
        return relation
    if (relation := _declared_relation(OWNER_SUFFIX_RELATION.get(fga_type, {}), suffix)) is not None:
        return relation
    action = suffix.rsplit("/", 1)[-1]
    if fga_type == "namespace" and (action in META_READ_ACTIONS or action in DATA_READ_ACTIONS):
        return "can_get_metadata"
    if fga_type == "materialized_view":
        if action in DATA_READ_ACTIONS:
            return "can_read"
        if action in META_READ_ACTIONS:
            return "can_get_metadata"
    # `transaction` and `classification` resolve in `authorize`'s own branches, so neither is served here.
    if fga_type == "table":
        if action in DATA_READ_ACTIONS:
            return "can_read_data"
        if action in META_READ_ACTIONS:
            return "can_get_metadata"
    if (relation := _declared_relation(WRITER_SUFFIX_RELATION.get(fga_type, {}), suffix)) is not None:
        return relation
    raise undeclared_door(fga_type, suffix)


#: The rung a create asks on its parent, named after the child it creates; a table's for every other resource.
_CREATE_RELATIONS: Final = {"namespace": "can_create_namespace", "materialized_view": "can_create_materialized_view"}


def create_parent_rung(resource: str, id_segments: list[str]) -> write_judge.Rung | None:
    """The ``can_create_*`` rung a create-on-parent op asks on the child's parent, by name.

    The parent is the namespace above the child, whose uuid the grants are held by, or the warehouse for a namespace
    directly in one ([[LH-593]]): the resolver answers a one-segment name as ``warehouse:<id>``. The relation is named
    after the child being created (``can_create_namespace`` / ``can_create_materialized_view`` / ``can_create_table``),
    each of which is the writer rung on a namespace and on a warehouse alike. ``None`` for an id with no parent, which
    the shape rung has already refused.
    """
    if len(id_segments) < 2:
        return None
    relation = _CREATE_RELATIONS.get(resource, "can_create_table")
    return write_judge.Rung(relation=relation, on=write_judge.Named(kind="namespace", segments=tuple(id_segments[:-1])))


#: The transaction doors `authorize_transaction` serves.
TRANSACTION_SUFFIXES: Final = frozenset({"describe", "alter"})


#: The rung each WRITING credential tier opens, one rung per tier and never either-of ([[LH-202]]).
#: `can_write_data` is a LOGICAL writer, whose policy reaches `data/` only; `can_vend_maintain` is the
#: maintenance identity's alone (ADR 0156 C4, [[LH-473]]), a PHYSICAL writer whose policy reaches the whole
#: prefix, `_versions/` and `_refs/` included, so it can finalize a version or move a tag around
#: `version/create`. An owner is a maintainer and keeps every maintainer door the catalog judges, and is not
#: vended this. The whole-prefix credential, scoped to one table for 900 s, is the owner's ruling of
#: 2026-09-08: the alternative measured before it was 207 of 285 rewrites a tick signed by the deployment's
#: ROOT key, because a refused vend falls back to the ambient credential.
VEND_TIER_RUNGS: Final[dict[str, str]] = {"write": "can_write_data", "maintain": "can_vend_maintain"}


#: The resources whose non-read doors write a table's data or its history, and so reach the write judge.
JUDGED_RESOURCES: Final = frozenset({"table", "materialized_view"})


#: The door whose rung its body decides, and the rung it asks for a body that classifies and changes nothing else (ADR
#: 0225): a classifier labels a table's columns without holding its data or DDL rights.
CLASSIFYING_DOOR: Final = "update_field_metadata"


CLASSIFY_RELATION: Final = "can_classify"


#: The rung of the one door that reaches a table no uuid identifies: its deregister, which detaches the row and deletes no
#: byte ([[LH-534]]).
UNIDENTIFIED_TABLE_RUNG: Final = "can_deregister"


#: What the estate admin is asked, on the estate root, to deregister such a table.
UNIDENTIFIED_TABLE_ESTATE_VERB: Final = "can_deregister_unidentified_table"


#: What a namespace door serves on a warehouse's own name, a namespace id of one segment: its reads.
WAREHOUSE_NAME_SUFFIXES: Final = frozenset({"list", "table/list", "describe", "exists", "access/my-permissions"})


#: The most tables one batch request names ([[LH-585]]). The gate resolves every name a batch lists before it answers,
#: and the list is the caller's, so an unbounded one is a request any signed-in caller can make cost what it likes.
MOST_TABLES_IN_A_BATCH: Final = 100


#: How many candidates one batch check of a listing authorizes before the page is judged full.
LISTING_BATCH: Final = 100


#: The types a role's grant is held inside its tenant on ([[LH-557]]): a namespace, table or view, whose tenant is its
#: name's warehouse's project ([[LH-593]]); a warehouse, whose own record names its project; and a project, which is its
#: own tenant (a role made a project's admin or member is that project's).
TENANT_HELD = frozenset({"namespace", "table", "materialized_view", "warehouse", "project"})


#: The rung a staged commit's operation needs at ``version/create``, which the router opens to a writer or a maintainer
#: (``can_create_version``, [[LH-471]]). Each is the rung the catalog's own door for that operation asks, so staging an
#: operation never clears a lower bar than doing it through the door: the writer operations are the writer rung, restore
#: is ``restore``'s owner rung, a compaction's two commits (the ``ReserveFragments`` and the ``Rewrite`` after it) are the
#: maintainer rung, and an Overwrite of a table that already has a version replaces its tip's schema and rows, the
#: owner rung (``require_can_drop_table``). A table's first version is the writer rung whatever it is staged as
#: (:func:`require_staged_operation`).
STAGED_OPERATION_RELATION: Final[dict[str, str]] = {
    **dict.fromkeys(("Append", "Delete", "Update", "Merge", "Project", "CreateIndex", "DataReplacement", "DataOverlay", "UpdateConfig"), "can_write_data"),
    "Restore": "can_restore",
    "Rewrite": "can_maintain",
    "ReserveFragments": "can_maintain",
    "Overwrite": "can_drop",
}
