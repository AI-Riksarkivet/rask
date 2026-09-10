"""The maintenance sweep records provenance without saying who it was.

the lakehouse register, row E2 (drained 2026-09-10; in git history) ("bus door trusts a producer-stamped author behind one shared
token"), whose close condition is Q6: *"the bus door applies `enforce_output_authz` as the stamped
subject."*

MEASURED ON THE DEPLOYED GRAPH 2026-09-08, and the measurement is why this file is about the PRODUCER
rather than the door. **664 of 5 644 `(:Run)` nodes carry no author at all**, and the largest single
source is this service:

    518   producer .../services/maintenance/src/maintenance/core/lineage_emit.py
     79   (no producer either)
     34   .../services/compaction/core/lineage_emit.py

So Q6's fix CANNOT BE APPLIED AS WRITTEN. `enforce_output_authz` authorizes as `token.sub` and raises
`UnauthenticatedError` when the token is `None`; the bus door has no principal, only the sidecar's
shared Dapr token. Gating it on the stamped subject would refuse every author-less event — the sweep's
entire lineage — which is silent provenance loss of exactly the shape the goal ranks first. **A
consumer-side gate is only safe once the producers sign.** That is the same ordering § E6's `parent`
facet needs, and it is worth stating twice because both rows read as consumer-side work.

THE SERVICE ALREADY KNOWS ITS OWN NAME. `MAINTENANCE_CATALOG_SERVICE_IDENTITY` (default
`service-maintenance`) is what it presents to the CATALOG as `x-lance-service-identity`, proven by
`test_maintenance_presents_its_own_credential.py`. So the graph and the catalog disagree about who did
the work — one records `service-maintenance`, the other records nobody — and that is an inconsistency
inside a single service rather than a missing feature.

ANONYMOUS BEATS MISATTRIBUTED, so this stamps the service's OWN identity and never a person's.
`author_sub_from_payload` exists because a producer-supplied author is unverified; signing as the
service is a claim the service is entitled to make about itself, and it is the claim the bus gate will
later authorize against.
"""

from __future__ import annotations

from typing import Any

from maintenance.core.lineage_emit import build_maintenance_event


def _event(*, author: str) -> dict[str, Any]:
    """One maintenance event, varying only the identity under test."""
    return build_maintenance_event(
        table_id="acme-bronze$events",
        namespace="bronze",
        job_namespace="lance-maintenance",
        run_id="11111111-1111-5111-8111-111111111111",
        event_time="2026-09-08T00:00:00+00:00",
        author=author,
    )


def test_the_event_names_the_service_that_emitted_it() -> None:
    """The headline: a maintenance run must not reach the graph anonymous."""
    facets = _event(author="service-maintenance")["run"]["facets"]

    assert "author" in facets, f"the maintenance event carries no author facet, so its run lands anonymous: {sorted(facets)}"
    assert facets["author"]["sub"] == "service-maintenance"
    assert facets["author"]["name"] == "service-maintenance"


def test_the_author_facet_is_spec_legal() -> None:
    """Every facet — custom ones included — must carry `_producer` + `_schemaURL` (BaseFacet
    `required`). Validating the live feed against the OpenLineage schema once failed 14 of 200 events
    on exactly this, so a new facet gets checked rather than assumed."""
    author = _event(author="service-maintenance")["run"]["facets"]["author"]

    assert author.get("_producer"), f"the author facet has no _producer: {author}"
    assert author.get("_schemaURL"), f"the author facet has no _schemaURL: {author}"


def test_an_unnamed_emitter_stays_anonymous_rather_than_inventing_a_subject() -> None:
    """The other direction, and the one that keeps this from becoming a lie.

    A deployment that has not configured an identity must produce NO author facet — not a placeholder,
    not the producer URL. A fabricated subject is worse than an absent one: the bus gate would later
    authorize it, and `author_sub_from_payload`'s whole rule is that anonymous beats misattributed.
    """
    facets = _event(author="")["run"]["facets"]

    assert "author" not in facets, f"an unconfigured identity invented an author: {facets.get('author')}"


def test_the_lance_operation_facet_still_rides() -> None:
    """The existing contract, pinned so the new facet cannot displace it: `producers()` reads the
    `lance.operation` marker to label a compaction run as such."""
    facets = _event(author="service-maintenance")["run"]["facets"]

    assert facets["lance"]["operation"] == "compaction"


def test_a_failed_pass_is_signed_too() -> None:
    """A FAILURE is the run a person is most likely to have to chase, and a COMPLETE that carried a
    signature while its FAIL twin did not would be the more misleading of the two states."""
    from maintenance.core.lineage_emit import build_maintenance_fail_event

    event = build_maintenance_fail_event(
        table_id="acme-bronze$events",
        namespace="bronze",
        job_namespace="lance-maintenance",
        run_id="22222222-2222-5222-8222-222222222222",
        event_time="2026-09-08T00:00:00+00:00",
        error="compaction failed",
        author="service-maintenance",
    )

    assert event["eventType"] == "FAIL"
    assert event["run"]["facets"]["author"]["sub"] == "service-maintenance"
    assert event["run"]["facets"]["errorMessage"]["message"] == "compaction failed"


def test_the_deployed_path_actually_passes_the_identity() -> None:
    """THE HALF THAT MATTERS, and the one this estate has been bitten by six times: a builder that CAN
    sign and a call site that does not is the same defect one layer down.

    `service.py` is the only production caller of `make_emitter`; this reads its source and demands the
    author argument is bound to the setting rather than left at its empty default. A default that means
    "stay anonymous" is right for a library and wrong for the deployment, so nothing but this says the
    deployment chose.
    """
    import inspect

    from maintenance import service

    source = inspect.getsource(service)
    call = source[source.index("make_emitter(") : source.index("make_control_emitter(")]

    assert "author=settings.catalog_service_identity" in call, (
        "the deployed emitter is built without an author, so every maintenance run still reaches the "
        f"graph anonymous however well the builder can sign: {call.strip()[:400]}"
    )
