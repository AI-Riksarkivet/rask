"""Which datasets an event names are outside the governed estate: the one rule every door, record and read here applies."""

from __future__ import annotations

from lineage.core.config import get_settings
from service_kit.naming import is_external_source_namespace


def is_external_source(namespace: str, name: str) -> bool:
    """Is this dataset OUTSIDE the governed estate — a raw source rather than a table we authorize?

    R23 draws the line the whole medallion rests on: the governed tiers are exactly bronze -> silver ->
    gold, and **raw is the external world, never a governed tier**. So a producer that honestly records
    where its data came from names something that has no catalog entry, no ``table:`` object, and
    therefore no tuple that could ever be written for it. Authorizing those inputs the same way as
    governed ones is not strict — it is unsatisfiable, and it refused every such producer permanently:

        403 "can_get_metadata required on inputs: bind86-src/run1"

    on the ingest plane's START event, whose input is the S3 prefix the run reads. The run landed its
    data, the terminal event was authorized fine, and the run was never recorded as opened. Ten
    configuration causes were investigated before the service's own message was read.

    The discriminator has TWO forms and lives in `service_kit.naming`, because it is a
    convention this service and every producer must agree on. A
    store URI carrying a scheme (``s3://bucket``) is external by shape. A BARE identifier cannot be
    decided by shape at all, since that is exactly what a catalog namespace looks like (``bronze``,
    ``bind86-bronze``), so the bare external namespaces are DECLARED: ``file`` and ``lance``.

    Testing the scheme ALONE was a one-sided failure that looked like nothing. Of ingest's three source
    kinds only `s3-prefix` mints a scheme; `local-dir` mints ``file`` and `lance-append` mints
    ``lance``, so both were authorized as governed and both had their START event refused — while the
    terminal event, which carries no inputs, was accepted. The run was recorded with half its
    provenance and every check that asks "does a run exist" passed (measured on the deployed estate
    2026-09-10: ``ingest_input_denied ... inputs=['/tmp/ingest-fixtures']``, 403).

    **This does not reopen the forgery hole it sits next to.** The guard exists so an authenticated
    reader cannot record "I read ``gold$catalog``" into the feed. A namespace is PART OF A DATASET'S
    IDENTITY (`Dataset.lineage_name`), so a caller who fakes ``s3://anything`` as the namespace of
    ``gold$catalog`` names an *external* dataset ``s3://anything/gold$catalog`` — a different identity from
    the governed ``gold$catalog``, on no governed table's edges. It cannot impersonate a governed dataset; it
    can only assert an edge from a source that is, correctly, outside the estate. What remains protected is
    exactly what the guard was written to protect: claiming to have read a GOVERNED dataset you cannot see.

    Outputs are deliberately NOT filtered this way. Writing is the direction that mutates the estate,
    and this plane never writes outside it — an output naming an external namespace is a producer
    claiming to have written the outside world, which is not a case to make permissive.

    A dataset under another catalog's dataset namespace is another estate's table, and so the outside world to this one
    ([[LH-553]]): the catalog this service records for is the one its settings name.
    """
    return is_external_source_namespace(namespace, name, catalog=get_settings().catalog_dataset_namespace)
