"""The namespace the catalog keeps for provenance: lineage's commit feed and edge list are tables in it (ADR 0158).

THE CATALOG KEEPS IT, NO TENANT DOES. Lineage's tables are governed like any other table — every write is
authorized, judged and audited at the catalog's doors — but they are no tenant's data, so the namespace
belongs to no warehouse and resolves to the catalog's default root, the way the `dir` catalog keeps its own
`__manifest` table in its root (`lance_docs/ns_catalog/catalog/dir/index.md`). The catalog creates it at boot
from `LANCE_PROVENANCE_NAMESPACE`, rather than through the namespace door, because the door demands a
warehouse for a top-level namespace.

WHO MAY WRITE IT is configuration, `LANCE_PROVENANCE_WRITERS`: each subject is granted `writer` on the
namespace, which reaches every table in it (`table#writer ... from parent`) and nothing outside it. The parent
edge hangs it off the estate's default warehouse, so the estate's owner owns it as they own every unbound
namespace. It is protected against deletion.

ITS HISTORY IS BOUNDED AS EVERY TABLE'S IS ([[LH-529]]). Each recorded event is a commit, so each of its tables gains a
version and a fragment per event, and Lance lists `_versions/` on every open. `LANCE_PROVENANCE_RETAIN_VERSIONS` is the
namespace's maintenance policy, the last versions maintenance's expiry keeps. A bring-your-own compaction job compacts
its tables on `reader` and `maintainer`, what a compaction through `version/create` needs (ADR 0162), granted to its own
identity at the namespace's access door ([[LH-563]]) and never `writer`, so its commits change how the tables are stored
and never what they say. The write tier a spec door vends it can still put over a committed data file, as a writer's
can: no session policy refuses a put over an existing key (`catalog.core.vending.build_session_policy`).

THE WRITERS ARE WHAT THE SETTINGS DECLARE, both ways, for the release's own accounts: recording lineage is the
lakehouse's own, so a direct `writer` tuple on the namespace naming one of `LANCE_OWN_ACCOUNTS` that `LANCE_PROVENANCE_WRITERS`
does not name is revoked at boot, as the bootstrap hook converges those accounts' estate roles ([[LH-563]]). Every other
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

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from fastapi.concurrency import run_in_threadpool
from lance_namespace import CreateNamespaceRequest, LanceNamespace, NamespaceAlreadyExistsError
from openfga_sdk import OpenFgaClient

from catalog.core.config import Settings
from catalog.core.identifiers import parse_identifier, with_minted_uuid
from catalog.core.lineage_emit import COMPACT_TABLE
from catalog.schemas import PolicyRequest
from catalog.services import native, object_uuids
from lakehouse import maintenance_policies, protection
from lakehouse.governed import fga
from lakehouse.naming import CATALOG_DELIMITER


log = logging.getLogger(__name__)

#: The actor recorded on what this writes. Not a person: the deployment configured it.
ACTOR = "system:provenance-namespace"


def is_provenance(namespace_id: str, settings: Settings) -> bool:
    """Whether ``namespace_id`` (a table's parent namespace, delimiter-joined) is the provenance namespace or inside it."""
    root = settings.provenance_namespace
    return bool(root) and (namespace_id == root or namespace_id.startswith(f"{root}{CATALOG_DELIMITER}"))


#: The relation on the namespace that configuration grants the release's own accounts, so a direct tuple of it naming
#: one of them that the settings do not name is revoked at boot.
_DECLARED_RELATION = "writer"


def is_unannounced(namespace_id: str, operation: str, settings: Settings) -> bool:
    """Whether a write of ``operation`` under ``namespace_id`` goes unannounced: any in the provenance namespace but a
    compaction (``COMPACT_TABLE``)."""
    return operation != COMPACT_TABLE and is_provenance(namespace_id, settings)


def announcer(segments: Sequence[str], settings: Settings) -> Callable[[str], bool]:
    """Whether a write of an operation on the table ``segments`` names is announced, as the emitter judges it
    (:func:`is_unannounced`): a door's table records the author only of what is announced (ADR 0187)."""
    parent = fga.parent_namespace_id(list(segments)) or ""
    return lambda operation: not is_unannounced(parent, operation, settings)


def provenance_tuples(settings: Settings, *, namespace_uuid: str, parent: str | None) -> list[fga.ClientTuple]:
    """The namespace's parent edge and a `writer` grant per configured writer, on the namespace's uuid ([[LH-524]])."""
    obj = fga.namespace_object(namespace_uuid)
    return [
        *([fga.ClientTuple(user=parent, relation="parent", object=obj)] if parent else []),
        *(fga.ClientTuple(user=fga.user_object(subject), relation=_DECLARED_RELATION, object=obj) for subject in settings.provenance_writers),
    ]


def _declared_policy(retain_versions: int, *, canonical: str, namespace_uuid: str) -> dict[str, Any]:
    """The maintenance policy this step writes: keep the last ``retain_versions``, every other knob at its default."""
    knobs = PolicyRequest.model_construct(retain_versions=retain_versions).model_dump()
    return {**knobs, "kind": "namespace", "id": canonical, "uuid": namespace_uuid, "set_by": ACTOR}


def keep_declared_retention(settings: Settings, *, canonical: str, namespace_uuid: str) -> None:
    """Make the namespace's maintenance policy the one the settings declare: keep the last ``provenance_retain_versions``.

    With none declared, the policy this step wrote is removed while it stands as written. The policy door merges a change
    into the record it finds, so a hold set there, or one set over this step's policy, stands. Blocking.
    """
    root, options = settings.registry_root, settings.storage_options()
    if settings.provenance_retain_versions is not None:
        declared = _declared_policy(settings.provenance_retain_versions, canonical=canonical, namespace_uuid=namespace_uuid)
        maintenance_policies.put_policy(root, options, declared)
        return
    held = maintenance_policies.get_policy(root, options, "namespace", namespace_uuid)
    retain = held.get("retain_versions") if held is not None else None
    if isinstance(retain, int) and held == _declared_policy(retain, canonical=canonical, namespace_uuid=namespace_uuid):
        maintenance_policies.delete_policy(root, options, "namespace", namespace_uuid)


async def ensure_provenance_namespace(ns: LanceNamespace, settings: Settings, client: OpenFgaClient | None) -> bool:
    """Create the provenance namespace if absent, protect it and grant its writers; ``True`` when it was created now."""
    if not settings.provenance_namespace:
        return False
    segments = parse_identifier(settings.provenance_namespace)
    canonical = fga.canonical_object_id(segments)
    # The `dir` backend refuses an existing namespace in every create mode, `exist_ok` included (measured on pylance
    # 13.0.0, where the spec's ExistOk would keep it), so keeping one is catching that refusal, and a kept namespace keeps
    # the uuid it was created with ([[LH-519]]).
    try:
        await run_in_threadpool(native.call, ns, "create_namespace", CreateNamespaceRequest(id=segments, properties=with_minted_uuid(None)))
        kept = False
    except NamespaceAlreadyExistsError:
        kept = True
    namespace_uuid = await run_in_threadpool(object_uuids.require_namespace_uuid, ns, segments)
    await run_in_threadpool(
        protection.set_protection,
        settings.registry_root,
        settings.storage_options(),
        {"kind": "namespace", "id": canonical, "uuid": namespace_uuid, "protected": "true", "set_by": ACTOR},
    )
    await run_in_threadpool(keep_declared_retention, settings, canonical=canonical, namespace_uuid=namespace_uuid)
    if settings.fga_enabled and client is not None:
        # A top-level namespace hangs off the default warehouse; a nested one off its parent namespace's uuid.
        parent = (
            fga.namespace_object(await run_in_threadpool(object_uuids.require_namespace_uuid, ns, segments[:-1]))
            if len(segments) > 1
            else settings.fga_default_warehouse_object
        )
        tuples = provenance_tuples(settings, namespace_uuid=namespace_uuid, parent=parent)
        await fga.write_tuples(client, tuples, actor=ACTOR, origin="provenance_namespace")  # noqa: TID251
        own = {fga.user_object(account) for account in settings.own_accounts}
        revoked = await fga.revoke_undeclared(  # noqa: TID251 — the provenance writers' revoking site
            client, tuples, obj=fga.namespace_object(namespace_uuid), relation=_DECLARED_RELATION, own=own, actor=ACTOR, origin="provenance_namespace"
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
