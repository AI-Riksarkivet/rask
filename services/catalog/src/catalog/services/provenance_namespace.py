"""The namespace the catalog keeps for provenance: lineage's commit feed and edge list are tables in it (ADR 0158).

THE CATALOG KEEPS IT, NO TENANT DOES. Lineage's tables are governed like any other table — every write is
authorized, judged and audited at the catalog's doors — but they are no tenant's data, so the namespace lies in the
estate's own warehouse (`lance_catalog$provenance` by default, [[LH-593]]), served from the catalog's own root and owned
by no project, the way the `dir` catalog keeps its own `__manifest` table in its root
(`lance_docs/ns_catalog/catalog/dir/index.md`). The catalog creates it at boot from `LANCE_PROVENANCE_NAMESPACE`, rather
than through the namespace door, because it is the catalog's own and no caller asks for it.

WHO MAY WRITE IT is configuration, `LANCE_PROVENANCE_WRITERS`: each subject is granted `writer` on the
namespace, which reaches every table in it (`table#writer ... from parent`) and nothing outside it. The parent
edge hangs it off the estate's own warehouse, so the estate's owner owns it as they own every namespace in that
warehouse. It is protected against deletion.

ITS HISTORY IS BOUNDED AS EVERY TABLE'S IS ([[LH-529]]). Each recorded event is a commit, so each of its tables gains a
version and a fragment per event, and Lance lists `_versions/` on every open. `LANCE_PROVENANCE_RETAIN_VERSIONS` is the
namespace's maintenance policy, the last versions maintenance's expiry keeps. A bring-your-own compaction job compacts
its tables on `reader` and `maintainer`, what a compaction through `version/create` needs (ADR 0162), granted to its own
identity at the namespace's access door ([[LH-563]]) and never `writer`, so its commits change how the tables are stored
and never what they say. The write tier a spec door vends it can still put over a committed data file, as a writer's
can: no session policy refuses a put over an existing key (`catalog.core.vending.build_session_policy`).

THE WRITERS ARE WHAT THE SETTINGS DECLARE, both ways, for the release's own accounts: recording lineage is the
lakehouse's own, so a direct `writer` tuple on the namespace naming one of `LANCE_OWN_ACCOUNTS` that `LANCE_PROVENANCE_WRITERS`
does not name is revoked at boot, as the declaration converge does those accounts' estate roles ([[LH-563]]). Every other
grant there was made at a door and stands, a compaction job's, a reader's and a writer granted to an account outside the
release alike. A replica booting with an older list during a rollout grants and revokes by that list until a replica
boots again.

A ROW WRITE UNDER IT IS NEVER ANNOUNCED (`catalog.core.lineage_emit`). Lineage records every announcement as a row
of these tables, so announcing that row's own commit would record it again, without end. A compaction is announced: it
is no recorded event's commit, so lineage records it once and the record ends there, and maintenance's write-event lane
expires the table after it, as it expires every table after a commit (ADR 0162).

Every step is idempotent, so the catalog runs the whole in the background at boot and retries it until it
holds, and never fails its own boot on it: until it holds, lineage retries creating its tables.
"""

import logging
from collections.abc import Callable, Sequence

from fastapi.concurrency import run_in_threadpool
from lance_namespace import ConcurrentModificationError, CreateNamespaceRequest, LanceNamespace, NamespaceAlreadyExistsError
from openfga_sdk import OpenFgaClient

from catalog.core import native
from catalog.core.config import CatalogLineageSettings, Settings
from catalog.core.identifiers import parse_identifier, with_minted_uuid
from catalog.core.lineage_emit import COMPACT_TABLE
from catalog.schemas import PolicyRequest
from catalog.services import object_uuids
from lakehouse.governed import fga_ids, fga_writes
from lakehouse.naming import CATALOG_DELIMITER
from lakehouse.records import maintenance_policies, protection
from lakehouse.records.maintenance_policies import PolicyRecord
from lakehouse.records.protection import ProtectionRecord


log = logging.getLogger(__name__)

#: The actor recorded on what this writes. Not a person: the deployment configured it.
ACTOR = "system:provenance-namespace"


def is_provenance(namespace_id: str, settings: CatalogLineageSettings) -> bool:
    """Whether ``namespace_id`` (a table's parent namespace, delimiter-joined) is the provenance namespace or inside it."""
    root = settings.provenance_namespace
    return bool(root) and (namespace_id == root or namespace_id.startswith(f"{root}{CATALOG_DELIMITER}"))


#: The relation on the namespace that configuration grants the release's own accounts, so a direct tuple of it naming
#: one of them that the settings do not name is revoked at boot.
_DECLARED_RELATION = "writer"


def is_unannounced(namespace_id: str, operation: str, settings: CatalogLineageSettings) -> bool:
    """Whether a write of ``operation`` under ``namespace_id`` goes unannounced: any in the provenance namespace but a
    compaction (``COMPACT_TABLE``)."""
    return operation != COMPACT_TABLE and is_provenance(namespace_id, settings)


def announcer(segments: Sequence[str], settings: CatalogLineageSettings) -> Callable[[str], bool]:
    """Whether a write of an operation on the table ``segments`` names is announced, as the emitter judges it
    (:func:`is_unannounced`): a door's table records the author only of what is announced (ADR 0187)."""
    parent = fga_ids.parent_namespace_id(list(segments)) or ""
    return lambda operation: not is_unannounced(parent, operation, settings)


def provenance_tuples(settings: CatalogLineageSettings, *, namespace_uuid: str, parent: str | None) -> list[fga_writes.ClientTuple]:
    """The namespace's parent edge and a `writer` grant per configured writer, on the namespace's uuid ([[LH-524]])."""
    obj = fga_ids.namespace_object(namespace_uuid)
    return [
        *([fga_writes.ClientTuple(user=parent, relation="parent", object=obj)] if parent else []),
        *(fga_writes.ClientTuple(user=fga_ids.user_object(subject), relation=_DECLARED_RELATION, object=obj) for subject in settings.provenance_writers),
    ]


def _declared_policy(retain_versions: int, *, canonical: str, namespace_uuid: str) -> PolicyRecord:
    """The maintenance policy this step writes: keep the last ``retain_versions``, every other knob at its default."""
    knobs = PolicyRequest.model_construct(retain_versions=retain_versions).model_dump()
    return PolicyRecord.model_validate({**knobs, "kind": "namespace", "id": canonical, "uuid": namespace_uuid, "set_by": ACTOR})


def keep_declared_retention(settings: Settings, *, canonical: str, namespace_uuid: str) -> None:
    """Make the namespace's maintenance policy keep the last ``provenance_retain_versions`` the settings declare.

    The retention is set onto the record stored, conditional on it (``maintenance_policies.set_policy``), as the policy door
    sets a change: every other knob stands as it is, so a hold set there stands across every boot ([[LH-576]]). With none
    declared, the policy this step wrote is removed while it stands as written. Blocking.

    Raises:
        lakehouse.storage.conditional_records.RecordChangedError: The record changed under every round of the set.
    """
    root, options = settings.registry_root, settings.storage_options()
    if (retain := settings.provenance_retain_versions) is not None:
        declared = _declared_policy(retain, canonical=canonical, namespace_uuid=namespace_uuid)
        maintenance_policies.set_policy(
            root,
            options,
            "namespace",
            key=namespace_uuid,
            compose=lambda stored: declared if stored is None else PolicyRecord.model_validate({**stored.stored(), "retain_versions": retain}),
        )
        return
    try:
        held = maintenance_policies.get_policy(root, options, "namespace", key=namespace_uuid)
    except maintenance_policies.MalformedPolicyError as exc:
        # Not the policy this step wrote, so not its to remove; the policy door replaces it.
        log.warning("provenance_policy_unreadable", extra={"namespace": canonical, "error": str(exc)[:300]})
        return
    retain = held.stored().get("retain_versions") if held is not None else None
    if isinstance(retain, int) and held == _declared_policy(retain, canonical=canonical, namespace_uuid=namespace_uuid):
        maintenance_policies.delete_policy(root, options, "namespace", key=namespace_uuid)


async def ensure_provenance_namespace(ns: LanceNamespace, settings: Settings, client: OpenFgaClient | None) -> bool:
    """Create the provenance namespace if absent, protect it and grant its writers; ``True`` when it was created now."""
    if not settings.provenance_namespace:
        return False
    segments = parse_identifier(settings.provenance_namespace)
    canonical = fga_ids.canonical_object_id(segments)
    # The `dir` backend refuses an existing namespace in every create mode, `exist_ok` included (measured on pylance
    # 13.0.0, where the spec's ExistOk would keep it), so keeping one is catching that refusal, and a kept namespace keeps
    # the uuid it was created with ([[LH-519]]). Two replicas booting on a store that does not hold it yet race on the
    # create. The directory catalog fails an operation whose manifest entry a concurrent operation writes
    # (`lance_docs/ns_catalog/catalog/dir/index.md` § Manifest Table Commits) and names error code 14,
    # ConcurrentModification, for a concurrent creation attempt (`supported-catalogs/lance-dir.md` § DeclareTable; its
    # § CreateNamespace lists only codes 2, 1 and 13). pylance 14.0.3 answers the loser of CreateNamespace with it too:
    # `ConcurrentModificationError`, "Object '…' was concurrently created by another operation" (measured 2026-11-22 over
    # moto, 10 of 10 two-replica rounds). The winner's namespace is kept the same way; either way its uuid is read next,
    # which fails if no namespace is there after all.
    try:
        await run_in_threadpool(native.call, ns, "create_namespace", CreateNamespaceRequest(id=segments, properties=with_minted_uuid(None)))
        kept = False
    except NamespaceAlreadyExistsError, ConcurrentModificationError:
        kept = True
    namespace_uuid = await run_in_threadpool(object_uuids.require_namespace_uuid, ns, segments)
    await run_in_threadpool(
        protection.set_protection,
        settings.registry_root,
        settings.storage_options(),
        ProtectionRecord(kind="namespace", id=canonical, uuid=namespace_uuid, protected=True, set_by=ACTOR),
    )
    await run_in_threadpool(keep_declared_retention, settings, canonical=canonical, namespace_uuid=namespace_uuid)
    if settings.fga_enabled and client is not None:
        # A namespace directly in the estate's warehouse hangs off it; a deeper one off its parent namespace's uuid.
        parent = (
            fga_ids.namespace_object(await run_in_threadpool(object_uuids.require_namespace_uuid, ns, segments[:-1]))
            if len(segments) > 2
            else settings.fga_default_warehouse_object
        )
        tuples = provenance_tuples(settings, namespace_uuid=namespace_uuid, parent=parent)
        await fga_writes.write_tuples(client, tuples, actor=ACTOR, origin="provenance_namespace")  # noqa: TID251
        own = {fga_ids.user_object(account) for account in settings.own_accounts}
        revoked = await fga_writes.revoke_undeclared(  # noqa: TID251 — the provenance writers' revoking site
            client, tuples, obj=fga_ids.namespace_object(namespace_uuid), relation=_DECLARED_RELATION, own=own, actor=ACTOR, origin="provenance_namespace"
        )
    else:
        revoked = []
    log.info(
        "provenance_namespace_ensured",
        extra={
            "namespace": canonical,
            "newly_created": not kept,
            "writers": len(settings.provenance_writers),
            "revoked": len(revoked),
            "retain_versions": settings.provenance_retain_versions,
        },
    )
    return not kept
