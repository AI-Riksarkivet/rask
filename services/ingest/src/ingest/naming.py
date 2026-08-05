"""How this plane NAMES the thing it writes — one composition, used by everything that names it.

This module exists because the alternative was measured and it does not work. The naming lived in two
places: `lineage.py` composed the output the cascade head matches on, and `catalog_service.table_id`
composed the table id the catalog and OpenFGA key on. Both said `f"{project}${dataset}"`, both were
self-consistent, and both were wrong in the same way — a PROJECT is not a namespace.

Fixing one of them is worse than fixing neither. A graph that names `bind86-bronze$pages` while the
catalog holds `bind86$pages` describes a table that does not exist, and nothing reports it: the write
succeeds, the lineage record is well-formed, and every consumer that tries to resolve the name gets
nothing back. Two writers of one convention will drift; the only question is when.

THE HIERARCHY, established from the code and the live governance store rather than from preference:

    project    selects the STORAGE ROOT, via the warehouse registry (`produce.py:60-66`)
    namespace  is the medallion TIER, project-qualified per #84 -> `bind86-bronze`
    table      is the dataset -> `bind86-bronze$pages`

The FGA store agrees: `project:bind86 -> warehouse:bind86-wh -> namespace:alpha|beta`, with
`namespace:bronze parent table:bronze$pages` already present. `namespace:bind86` was never a thing
that existed, which is why the run's 403 named a table nobody had granted anything on.
"""

from __future__ import annotations

import os


def delimiter() -> str:
    """The catalog's table-id separator. From env so it cannot drift from the catalog's own."""
    return os.getenv("RASK_CATALOG_DELIMITER", "$")


def bronze_namespace() -> str:
    """The bronze TIER's namespace name, read from the same env the medallion reads.

    Not a constant: `MEDALLION_BRONZE_NAMESPACE` is a chart value, and a tier the writer and the
    cascade head disagree about is a write nothing downstream ever sees.
    """
    return os.getenv("MEDALLION_BRONZE_NAMESPACE", "bronze")


def tenant(project: str) -> str:
    """The project as a NAME SEGMENT — or empty when it cannot safely be one.

    The same `is_safe_project` guard the medallion's head applies (`_cascade_project`), for the same
    stated reason: a value outside the path-safe shape must never become an S3 prefix or a
    lineage-name qualifier. Applying it on only one side is how a garbage project produces a write
    that is refused for a reason invisible from the writer.
    """
    from service_kit.lakehouse.warehouse_registry import is_safe_project

    return project if is_safe_project(project) else ""


def bronze_namespace_for(project: str) -> str:
    """The namespace a bronze write for `project` lands in — `bind86-bronze`, or `bronze` untenanted."""
    from service_kit.lakehouse.warehouse_registry import project_namespace

    return project_namespace(tenant(project), bronze_namespace())


def bronze_table_id(project: str, dataset: str) -> str:
    """The ONE identifier for the table a bronze run writes — `bind86-bronze$pages`.

    The catalog keys on it, OpenFGA authorizes against it, and the cascade head matches on it. They
    are the same string because they are the same table; composing it separately per consumer is the
    defect this module replaces.
    """
    return f"{bronze_namespace_for(project)}{delimiter()}{dataset}"
