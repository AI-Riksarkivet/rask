"""Recover a table's catalog IDENTIFIER from the object-store location it lives at.

TWO CONSUMERS, ONE CONVENTION, which is why this is here and not in either of them. The maintenance
plane discovers datasets by LISTING BUCKETS — recorded in ``open_cloudnative.md``, because rask's
catalog is not the commit coordinator and the medallion movers write past it — so what it holds is a
URI while the catalog's credential door needs an id. Its lineage emitter needs the same crossing, for
a different reason: the id it recovers becomes the OpenLineage ``Dataset`` name AND the OpenFGA object
id of the maintenance event.

Those two had separate implementations, and the second one was wrong in a way nothing could notice: it
split the leaf on its FIRST underscore unconditionally, so a namespace carrying an underscore lost its
first segment — ``aa3bed10_transcripts_v2$t1`` became ``v2$t1``, an id naming no table, emitted into
the lineage graph as though it did. ``transcripts_v2`` is a real namespace in this estate. The uuid8
prefix is therefore CHECKED rather than assumed.

``None`` is a first-class answer meaning "this location names no catalog table" — a stray directory, a
nested layout whose namespace is a parent directory rather than part of the leaf. Callers must degrade:
the credential caller falls back to the ambient credential, the lineage caller emits nothing. Guessing
would be worse than both — a wrong id vends a credential for a different table (surfacing as a 403 on
the right one, naming nothing about the guess) and attaches a maintenance run to a dataset node that
does not exist.
"""

from __future__ import annotations

from service_kit.lakehouse.naming import CATALOG_DELIMITER


def table_id_from_location(dataset_uri: str) -> str | None:
    """The catalog identifier for a dataset URI, or ``None`` if the location names no table.

    The catalog lays a table out as ``<bucket>/<uuid8>_<table_id>/`` (``optimize.discover_datasets``),
    where ``table_id`` is ``<namespace>$<table>`` for a namespaced table. The uuid8 prefix is stripped
    only when the leading segment actually IS one — eight lowercase hex characters — so
    ``transcripts_v2$t1`` keeps its namespace instead of being reduced to ``v2$t1``.
    """
    # The leaf is taken VERBATIM — no `.lance` suffix is stripped. A catalog-laid-out table has none
    # (`4750a5b9_acme-bronze$events`, measured live); a `.lance` suffix appears only on datasets the
    # catalog did not lay out, which are exactly the UNDECLARED ones whose id here is a fallback label
    # already recorded in the lineage graph under that name. Stripping it would silently rename
    # existing Dataset nodes for no benefit to any caller.
    leaf = dataset_uri.rstrip("/").split("/")[-1]
    prefix, separator, remainder = leaf.partition("_")
    if separator and _is_uuid8(prefix):
        return _well_formed(remainder)
    # No uuid8 prefix. A leaf that is itself an identifier (the layout written without one) is
    # answerable; a bare directory name is not, and the delimiter is what tells them apart.
    return _well_formed(leaf) if CATALOG_DELIMITER in leaf else None


def _well_formed(table_id: str) -> str | None:
    """The id, or ``None`` when it is a delimiter with an empty half.

    ``aa3bed10_$events`` and ``aa3bed10_ns$`` are directories that look like identifiers and are not:
    a table id names a namespace AND a table. Returning them would vend against, and emit lineage for,
    an object that cannot exist.
    """
    if not table_id:
        return None
    if CATALOG_DELIMITER in table_id:
        namespace, _, table = table_id.partition(CATALOG_DELIMITER)
        return table_id if namespace and table else None
    return table_id


def _is_uuid8(candidate: str) -> bool:
    """Is this leading segment the `dir` backend's directory prefix, rather than part of a namespace?

    Hex-only, and a LENGTH RANGE rather than exactly 8 — the observed prefixes are all 8 characters
    (`4750a5b9`, `aa3bed10`, `c7688659`, measured against the live estate 2026-09-03), but pinning the
    number would make a prefix-length change silently produce a WRONG identifier rather than no
    identifier, and a wrong id vends a credential for another table and emits lineage for a node that
    does not exist.

    Checking at all is the whole fix. The implementation this replaced stripped ANY leading segment up
    to the first underscore, so `transcripts_v2$t1` became `v2$t1` — `transcripts` is not hex, so
    nothing is stripped now and the namespace survives.
    """
    return 6 <= len(candidate) <= 32 and all(character in "0123456789abcdef" for character in candidate)
