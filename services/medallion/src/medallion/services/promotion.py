"""The consume-layer provenance document a stage writes INTO its output dataset (R26, executing R25b).

R25 says the south side's product is a Lance dataset an external user can take away, with its lineage
alongside. R26 makes "alongside" literal: a ``lineage`` column of Lance's ``pa.json_()`` — stored JSONB,
queryable in place — carrying everything a consumer needs to answer "where did this row come from"
without a Cypher query against Apache AGE.

**Why it cannot disagree with the graph.** :func:`promotion_lineage` does not author provenance. It calls
``build_run_event`` — the SAME builder whose output the mover publishes to the lineage service — and
projects that event through :meth:`lineage_kit.consume.LineageDoc.from_run_event`. The mover fixes one
``event_time`` and one token for the run, so the identity event projected into the column and the
measured event published to the graph carry the same ``runId``, job, author, operation and instant.
:meth:`LineageDoc.is_consistent_with` re-checks that pairing and is pinned by a test.

**What the document adds that the event does not claim.** Two things, both facts the emit has no field
for: the upstream's on-disk Lance version + physical URI (measured just before the read), and the
``DERIVED_FROM`` tail inherited from the upstream dataset's own ``lineage`` cell — which is what makes a
gold row's chain reach bronze while every stage still knows only its own edge.
"""

from __future__ import annotations

from lineage_kit.consume import DatasetRef, LineageDoc
from lineage_kit.schemas import RunEvent

from medallion.core.config import MedallionSettings
from medallion.schemas.events import build_run_event
from medallion.services.compute import UpstreamFacts


def promotion_lineage(
    settings: MedallionSettings,
    *,
    from_namespace: str,
    from_dataset: str,
    to_namespace: str,
    to_dataset: str,
    to_uri: str,
    upstream: UpstreamFacts,
    event_time: str,
    token: str | None,
    project: str | None,
) -> LineageDoc:
    """This stage's provenance document, projected from the run event the mover is about to emit.

    The projection deliberately ignores the identity event's output ``version`` facet: the document is
    written in the very commit that mints that version, so naming it would be a guess. A consumer holds
    the dataset (``lance.dataset(uri).version`` is the answer) and joins back to the graph on ``run_id``.
    """
    identity_event = build_run_event(
        operation=settings.operation,
        author=settings.author,
        job_namespace=settings.job_namespace,
        inputs=[(from_namespace, from_dataset)],
        output_namespace=to_namespace,
        output_name=to_dataset,
        source_uri=to_uri,
        token=token,
        project=project,
        event_time=event_time,
    )
    return LineageDoc.from_run_event(
        RunEvent.model_validate(identity_event),
        inputs=[
            DatasetRef(
                namespace=from_namespace,
                name=from_dataset,
                version=str(upstream.version),
                uri=upstream.uri,
            )
        ],
        parent_chain=upstream.chain,
    )
