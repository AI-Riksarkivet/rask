"""What maintenance's drift reconciler reads of the estate's grants and asks the catalog to repair, judged on the
catalog's own registries ([[LH-061]], [[XC-084]]).

Maintenance holds no OpenFGA credential. OpenFGA has no per-tuple access control, so a client that may write one tuple
may write any, and one able to grant itself an owner or estate rung could lift a hold through the catalog's own doors.
The catalog therefore reads the store's grant census for maintenance's drift report, and makes the two repairs that
report drives: it revokes the grants of an object no registry names (a ghost), and writes a warehouse's tenancy edge,
which the warehouse's registry record justifies. Each candidate maintenance names is judged again here, on reads that
refuse rather than skip what they cannot read: a revoke justified by an absence is justified only by a complete read.

Every function is BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

from typing import Any

from catalog.core.config import Settings
from catalog.services import projects, table_claims, warehouses
from lakehouse import location_claims, trash
from lakehouse.governed import fga


def platform_objects(settings: Settings) -> frozenset[str]:
    """The objects a deployment keeps for itself, which no registry names and no drift report reads as a ghost: the
    estate root and the default warehouse a top-level namespace hangs off."""
    return frozenset({settings.fga_root_object, settings.fga_default_warehouse_object})


def held_table_uuids(settings: Settings) -> frozenset[str]:
    """Every table uuid a held location claim records: a live table's, or a dropped one's the trash keeps a claim for.

    Raises:
        ValueError: A claim record cannot be read, which may be a live table's.
        botocore.exceptions.ClientError: The store refused the listing.
    """
    claims = location_claims.every_claim(table_claims.claim_store(settings))
    return frozenset(claim.uuid for claim in claims if claim.released_at is None and claim.uuid)


def ghost_refusal(settings: Settings, obj: str, *, held: frozenset[str]) -> str | None:
    """Why ``obj``'s grants are not a ghost's, or ``None`` when no registry names the object.

    ``held`` is :func:`held_table_uuids`, read once for every candidate a call judges.

    Raises:
        ValueError: A registry record cannot be read, which may be the object's.
    """
    kind, _, key = obj.partition(":")
    if obj in platform_objects(settings):
        return "the deployment keeps the object for itself"
    match kind:
        case "project":
            return f"the project record {key!r} names it" if projects.get_project(settings.registry_root, settings.storage_options(), key) else None
        case "warehouse":
            return f"the warehouse record {key!r} names it" if warehouses.get_warehouse(settings.registry_root, settings.storage_options(), key) else None
        case "table" if key in held:
            return "a held location claim records the table, so it stands or the trash keeps it"
        case "table":
            return "a trash record keeps the table for its undrop" if trash.keeps(settings.registry_root, settings.storage_options(), key) else None
        case _:
            return f"no drift report finds a ghost {kind!r}, so none is judged here"


def tenancy_edge(record: dict[str, Any] | None, warehouse_id: str) -> fga.ClientTuple | str:
    """The warehouse's tenancy pointer as its record names it, or why the record justifies none.

    Relation ``project``, as the catalog seeds it at the warehouse's creation: the model's warehouse type declares
    ``project: [project]`` and no ``parent``, so the structural edge a warehouse holds is this one.
    """
    if record is None:
        return f"no warehouse record names {warehouse_id!r}"
    if not (project := record.get("project")):
        return f"the warehouse record {warehouse_id!r} names no project, so its tenancy is unknown"
    return fga.ClientTuple(user=f"project:{project}", relation="project", object=f"warehouse:{warehouse_id}")
