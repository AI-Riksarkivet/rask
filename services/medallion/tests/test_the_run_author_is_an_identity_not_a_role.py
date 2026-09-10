"""The author `sub` on a cascade run must be an IDENTITY, or lineage refuses the whole run.

`build_run_event` stamped `custom_facet(_PRODUCER, name=author, sub=author)`, and the medallion's
authors are ROLE NAMES — `producer_author` defaults to "ray", `author` to "data_eng", and the chart
ships `author: data_eng` on every stage runner. Lineage's ingest gate asks FGA whether that subject may
write the run's outputs, `user:ray` and `user:data_eng` hold no tuple because they are not identities,
and the event is refused.

MEASURED ON THE LIVE ESTATE 2026-09-10 by driving two `/produce` calls: both cascades RAN
(`medallion_cascade_triggered` fired for each) and lineage answered
`ingest_denied sub='ray' relation='can_write_data' outputs=['bronze$events']` twice, then the same for
`sub='data_eng'` on `silver$features`. Neither token appears anywhere in 400 scanned durable-feed
events. Every cascade write's provenance was being discarded, and NOTHING was red — the reconciler
files a degraded `RECONCILE` run minutes later, so the graph looks populated.

THE ROLE NAME IS NOT DELETED, and that is the owner's ruling (2026-09-10): it is what a person reads on
a board, and a run authored by "data_eng" is more use to them than one authored by
`service-bronze-to-silver`. What changes is that it stops being the AUTHORIZATION SUBJECT — the two
were the same string only because the facet carried one field for both jobs.
"""

from __future__ import annotations

from medallion.schemas.events import build_run_event


def _facet(event: dict[str, object]) -> dict[str, object]:
    run = event["run"]
    assert isinstance(run, dict)
    facets = run["facets"]
    assert isinstance(facets, dict)
    author = facets["author"]
    assert isinstance(author, dict)
    return author


def _event(author_subject: str | None = None) -> dict[str, object]:
    return build_run_event(
        operation="embed_features",
        author="data_eng",
        author_subject=author_subject,
        job_namespace="medallion",
        inputs=[],
        output_namespace="silver",
        output_name="silver$features",
    )


def test_the_subject_is_the_service_identity() -> None:
    """The half lineage authorizes on. `service-bronze-to-silver` holds tuples; `data_eng` cannot."""
    facet = _facet(_event(author_subject="service-bronze-to-silver"))

    assert facet["sub"] == "service-bronze-to-silver", "the authz subject is still a role name, so lineage will refuse the run"


def test_the_display_name_is_still_the_role() -> None:
    """The half a person reads. Owner ruling: the role is what makes a board legible, so it stays."""
    facet = _facet(_event(author_subject="service-bronze-to-silver"))

    assert facet["name"] == "data_eng", "the role name was lost, so every board now reads a service id"


def test_without_a_service_identity_the_facet_is_unchanged() -> None:
    """A caller that names no identity — an external producer, a test, a deployment that configures
    none — must emit exactly what it emitted before rather than a run with no subject at all."""
    facet = _facet(_event())

    assert facet["sub"] == "data_eng"
    assert facet["name"] == "data_eng"


def test_no_author_still_emits_no_facet() -> None:
    """The negative control: the facet is optional and stays optional. Without this a builder that
    always emitted an author would satisfy every assertion above."""
    event = build_run_event(
        operation="embed_features",
        author=None,
        job_namespace="medallion",
        inputs=[],
        output_namespace="silver",
        output_name="silver$features",
        author_subject="service-bronze-to-silver",
    )
    run = event["run"]
    assert isinstance(run, dict)
    facets = run["facets"]
    assert isinstance(facets, dict)
    assert "author" not in facets, "an identity with no author invented an author facet"
