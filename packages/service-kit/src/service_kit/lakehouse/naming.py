"""Catalog table-id naming — the ONE delimiter the estate's governed table ids use."""

from __future__ import annotations


#: The catalog table-id delimiter (``gold$catalog``, ``silver$features``). ONE definition for the whole
#: estate: every config knob defaults to it and every parse site splits on it, so the ids the catalog
#: mints and the ids every producer / stage runner / reader parses cannot drift apart. The Lance Namespace
#: layout and the medallion's ``<stage>$<table>`` convention both fix this at ``$``; a service's env
#: override (``LANCE_NS_DELIMITER`` and its siblings) exists so an operator who changes it changes it
#: from this single default — never so one plane can diverge from another.
CATALOG_DELIMITER = "$"


#: The lineage namespaces that denote a source OUTSIDE the governed estate.
#:
#: A convention two services must agree on cannot live inside one of them — the reason
#: `project_namespace` moved here. INGEST mints these on a run's input dataset; LINEAGE decides from
#: them whether to authorize that input at all. When the two disagree the failure is silent and
#: one-sided: the START event is refused 403, the terminal event carries no inputs and is accepted, so
#: the run reaches the graph missing exactly half of itself.
#:
#: `s3-prefix` mints `s3://<bucket>`, which a URI-scheme test catches. `local-dir` mints `file` and
#: `lance-append` mints `lance` — bare identifiers, indistinguishable by shape from a catalog namespace
#: like `lane-bronze`, and both were authorized as governed until this was declared (measured on the
#: deployed estate 2026-09-10: `ingest_input_denied ... inputs=['/tmp/ingest-fixtures']`).
#:
#: Naming them EXPLICITLY rather than widening the shape test is the point. "Anything without a
#: delimiter is external" would exempt every catalog namespace and reopen the forgery the input check
#: exists to close — a reader claiming to have read a governed dataset it cannot see.
EXTERNAL_SOURCE_NAMESPACES = frozenset({"file", "lance"})


def is_external_source_namespace(namespace: str, name: str) -> bool:
    """Is this lineage dataset a source outside the governed estate?

    TWO forms, because the estate's producers legitimately mint both: a store URI carrying a scheme
    (``s3://bucket``), which is the convention for a namespaced external system, and one of the bare
    identifiers in :data:`EXTERNAL_SOURCE_NAMESPACES`.

    THE NAME IS PART OF THE ANSWER for the bare form, and only for it. A declared namespace is a bare
    identifier, which is exactly what a catalog namespace looks like — so an operator who created a
    namespace literally called ``file`` would have every table under it exempted from the input check
    this discriminator guards. Requiring the name to carry no :data:`CATALOG_DELIMITER` closes that:
    a governed table id is always ``<namespace>$<table>``, while an external source names a path or a
    URI. Both holes would then have to be open at once — a namespace named ``file`` AND a governed
    table id with no delimiter, which the catalog cannot mint.

    The scheme form ignores the name deliberately: ``s3://bucket`` is not a catalog namespace under any
    configuration, so there is nothing for the name to disambiguate.
    """
    if "://" in namespace:
        return True
    return namespace in EXTERNAL_SOURCE_NAMESPACES and CATALOG_DELIMITER not in name
