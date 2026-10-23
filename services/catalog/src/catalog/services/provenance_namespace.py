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

A WRITE UNDER IT IS NEVER ANNOUNCED (`catalog.core.lineage_emit`). Lineage records every announcement as a row
of these tables, so announcing that row's own commit would record it again, without end.

Every step is idempotent, so the catalog runs the whole in the background at boot and retries it until it
holds, and never fails its own boot on it: until it holds, lineage retries creating its tables.
"""

from __future__ import annotations

import logging

from fastapi.concurrency import run_in_threadpool
from lance_namespace import CreateNamespaceRequest, LanceNamespace, NamespaceAlreadyExistsError
from openfga_sdk import OpenFgaClient

from catalog.core.config import Settings
from catalog.core.identifiers import parse_identifier, with_minted_uuid
from catalog.services import native, object_uuids
from service_kit.governed import fga
from service_kit.lakehouse import protection
from service_kit.lakehouse.naming import CATALOG_DELIMITER


log = logging.getLogger(__name__)

#: The actor recorded on what this writes. Not a person: the deployment configured it.
ACTOR = "system:provenance-namespace"


def is_provenance(namespace_id: str, settings: Settings) -> bool:
    """Whether ``namespace_id`` (a table's parent namespace, delimiter-joined) is the provenance namespace or inside it."""
    root = settings.provenance_namespace
    return bool(root) and (namespace_id == root or namespace_id.startswith(f"{root}{CATALOG_DELIMITER}"))


def provenance_tuples(settings: Settings, *, namespace_uuid: str, parent: str | None) -> list[fga.ClientTuple]:
    """The namespace's parent edge and a `writer` grant per configured writer, on the namespace's uuid ([[LH-524]])."""
    obj = fga.namespace_object(namespace_uuid)
    return [
        *([fga.ClientTuple(user=parent, relation="parent", object=obj)] if parent else []),
        *(fga.ClientTuple(user=fga.user_object(subject), relation="writer", object=obj) for subject in settings.provenance_writers),
    ]


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
    if settings.fga_enabled and client is not None:
        # A top-level namespace hangs off the default warehouse; a nested one off its parent namespace's uuid.
        parent = (
            fga.namespace_object(await run_in_threadpool(object_uuids.require_namespace_uuid, ns, segments[:-1]))
            if len(segments) > 1
            else settings.fga_default_warehouse_object
        )
        tuples = provenance_tuples(settings, namespace_uuid=namespace_uuid, parent=parent)
        await fga.write_tuples(client, tuples, actor=ACTOR, origin="provenance_namespace")  # noqa: TID251
    log.info("provenance_namespace_ensured", extra={"namespace": canonical, "newly_created": not kept, "writers": len(settings.provenance_writers)})
    return not kept
