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
namespace's maintenance policy, the last versions maintenance's expiry keeps; each `LANCE_PROVENANCE_MAINTAINERS`
subject, a bring-your-own compaction job, is granted `reader` and `maintainer`, which is what a compaction through
`version/create` needs (ADR 0162), and never `writer`, so its commits change how the tables are stored and never what
they say. The write tier a spec door vends it can still put over a committed data file, as a writer's can: no session
policy refuses a put over an existing key (`catalog.core.vending.build_session_policy`).

THE GRANTS ARE WHAT THE SETTINGS DECLARE, both ways. Who may write and who may compact it are configuration alone, so a
direct `writer` or `maintainer` tuple on the namespace that the settings do not name is revoked at boot, with the
`reader` granted beside a maintainer; a `reader` granted any other way stands. A replica booting with an older list
during a rollout grants and revokes by that list until a replica boots again.

A ROW WRITE UNDER IT IS NEVER ANNOUNCED (`catalog.core.lineage_emit`). Lineage records every announcement as a row
of these tables, so announcing that row's own commit would record it again, without end. A compaction is announced: it
is no recorded event's commit, so lineage records it once and the record ends there, and maintenance's write-event lane
expires the table after it, as it expires every table after a commit (ADR 0162).

Every step is idempotent, so the catalog runs the whole in the background at boot and retries it until it
holds, and never fails its own boot on it: until it holds, lineage retries creating its tables.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi.concurrency import run_in_threadpool
from lance_namespace import CreateNamespaceRequest, LanceNamespace, NamespaceAlreadyExistsError
from openfga_sdk import OpenFgaClient

from catalog.core.config import Settings
from catalog.core.identifiers import parse_identifier, with_minted_uuid
from catalog.core.lineage_emit import COMPACT_TABLE
from catalog.schemas import PolicyRequest
from catalog.services import native, object_uuids
from service_kit.governed import fga
from service_kit.lakehouse import maintenance_policies, protection
from service_kit.lakehouse.naming import CATALOG_DELIMITER


log = logging.getLogger(__name__)

#: The actor recorded on what this writes. Not a person: the deployment configured it.
ACTOR = "system:provenance-namespace"


def is_provenance(namespace_id: str, settings: Settings) -> bool:
    """Whether ``namespace_id`` (a table's parent namespace, delimiter-joined) is the provenance namespace or inside it."""
    root = settings.provenance_namespace
    return bool(root) and (namespace_id == root or namespace_id.startswith(f"{root}{CATALOG_DELIMITER}"))


#: What a provenance maintainer is granted: the rungs a compaction through `version/create` needs, and no write.
_MAINTAINER_RUNGS = ("reader", "maintainer")
#: The relations on the namespace that configuration alone grants, so a direct tuple of either that the settings do not
#: name is revoked at boot.
_DECLARED_RELATIONS = frozenset({"writer", "maintainer"})


def is_unannounced(namespace_id: str, operation: str, settings: Settings) -> bool:
    """Whether a write of ``operation`` under ``namespace_id`` goes unannounced: any in the provenance namespace but a
    compaction (``COMPACT_TABLE``)."""
    return operation != COMPACT_TABLE and is_provenance(namespace_id, settings)


def provenance_tuples(settings: Settings, *, namespace_uuid: str, parent: str | None) -> list[fga.ClientTuple]:
    """The namespace's parent edge, a `writer` grant per configured writer and the compaction rungs per configured
    maintainer, on the namespace's uuid ([[LH-524]])."""
    obj = fga.namespace_object(namespace_uuid)
    return [
        *([fga.ClientTuple(user=parent, relation="parent", object=obj)] if parent else []),
        *(fga.ClientTuple(user=fga.user_object(subject), relation="writer", object=obj) for subject in settings.provenance_writers),
        *(
            fga.ClientTuple(user=fga.user_object(subject), relation=rung, object=obj)
            for subject in settings.provenance_maintainers
            for rung in _MAINTAINER_RUNGS
        ),
    ]


async def revoke_undeclared(client: OpenFgaClient, declared: list[fga.ClientTuple], *, obj: str) -> list[fga.ClientTuple]:
    """Revoke each `writer` or `maintainer` tuple on ``obj`` that ``declared`` does not hold, and the `reader` granted beside
    each such maintainer; answer the tuples removed.

    The `reader` goes first, so a boot that fails between the two finds the `maintainer` still there when it retries.
    """
    kept = {(t.user, t.relation) for t in declared}
    stale: list[fga.ClientTuple] = []
    token: str | None = None
    while True:
        page, token = await fga.read_tuples(client, obj=obj, continuation_token=token)
        for held in page:
            if held.relation not in _DECLARED_RELATIONS or (held.user, held.relation) in kept:
                continue
            if held.relation == "maintainer":
                stale.append(fga.ClientTuple(user=held.user, relation="reader", object=obj))
            stale.append(held)
        if not token:
            break
    return await fga.delete_tuples(client, stale, actor=ACTOR, origin="provenance_namespace")  # noqa: TID251


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
        revoked = await revoke_undeclared(client, tuples, obj=fga.namespace_object(namespace_uuid))
    else:
        revoked = []
    log.info(
        "provenance_namespace_ensured",
        extra={
            "namespace": canonical,
            "newly_created": not kept,
            "writers": len(settings.provenance_writers),
            "maintainers": len(settings.provenance_maintainers),
            "revoked": len(revoked),
            "retain_versions": settings.provenance_retain_versions,
        },
    )
    return not kept
