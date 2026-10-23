"""Estate-wide identifier naming — the delimiter governed table ids use, and the shape a control-plane id has."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final


#: The delimiter that joins a table's or namespace's names into its string identifier (``gold$catalog``). The Lance
#: Namespace spec fixes it: a request naming no delimiter uses ``$`` (``lance_docs/ns_catalog/spec.yaml:2314-2318``), so
#: no setting moves it ([[LH-526]]). Every parse site splits on it and every producer, pipeline and reader joins with it,
#: so the ids the catalog answers and the ids every client sends cannot drift apart, and the catalog refuses a request
#: naming another (``catalog.api.fga_deps.require_request_names``). No name holds it (ADR 0173 §4). It spells a table's catalog id and its lineage
#: Dataset name; a table's and a namespace's grants and records are held by their uuids (ADR 0173 §8).
CATALOG_DELIMITER: Final = "$"


#: The read-only property DescribeTable and DescribeNamespace answer an object's uuid under (ADR 0173 §11). A namespace
#: keeps its own in its `__manifest` row's properties, which only its create writes and its undrop restores; a table's
#: lives in its location's claim (``service_kit.lakehouse.location_claims``). No door accepts a caller's ``rask.*``
#: property, so no caller sets or changes one. Every service that names an object by its uuid reads it, so it is declared
#: once.
UUID_PROPERTY: Final = "rask.uuid"


def uuid_of(properties: object) -> str | None:
    """The uuid ``properties`` keeps under :data:`UUID_PROPERTY`, or ``None`` when it keeps no non-empty string there."""
    value = properties.get(UUID_PROPERTY) if isinstance(properties, Mapping) else None
    return value if isinstance(value, str) and value else None


#: The shape a control-plane id (project, warehouse, bucket) must have: DNS-safe — lowercase
#: alphanumeric plus hyphens, 3-63 characters, never starting or ending with a hyphen. It doubles as an
#: S3 bucket name, a registry filename and an OpenFGA object id, so the strictest of those wins.
#:
#: **Anchored with ``\Z``, not ``$``, and that is load-bearing.** Python's ``$`` also matches just BEFORE
#: a trailing newline, so ``"acme\n"`` satisfied a ``$``-anchored copy of this rule. That id then became a
#: registry filename AND an FGA object id — and OpenFGA rejects whitespace in the latter, so the registry
#: record landed and the tuple write failed: precisely the record-without-tuples drift Decision 1 exists to
#: prevent. Found 2026-08-04 in an adversarial review, live in two of the three copies of this pattern.
#:
#: It is ONE constant because it was three copies that had already diverged. A shape rule duplicated per
#: endpoint is a rule that holds until someone fixes one site.
#:
#: **IT LIVES IN SERVICE-KIT BECAUSE TWO PLANES DECIDE WITH IT, AND THEY DISAGREED.** The catalog MINTS
#: against this rule; the planes downstream — `catalog.core.lineage_emit`, the medallion's ingest
#: trigger and transform — CONSUME with `warehouse_registry.is_safe_project`, which
#: had its own looser copy admitting uppercase, underscores, single characters and a trailing hyphen.
#: Anything in that gap is an id those planes would turn into an S3 key prefix, a Lance dataset URI and
#: a lineage namespace qualifier, and that the control plane could never have issued. The rule a
#: producer checks and the rule the issuer enforces cannot live in the issuer.
CONTROL_ID_PATTERN = r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]"
CONTROL_ID_RE = re.compile(rf"^{CONTROL_ID_PATTERN}\Z")


#: The lineage namespaces that denote a source OUTSIDE the governed estate.
#:
#: A convention two services must agree on cannot live inside one of them — the reason
#: `project_namespace` moved here. A PRODUCER names these on a run's input dataset; LINEAGE decides from
#: them whether to authorize that input at all. When the two disagree the failure is silent and
#: one-sided: the START event is refused 403, the terminal event carries no inputs and is accepted, so
#: the run reaches the feed missing exactly half of itself.
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
