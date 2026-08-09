"""`run_id_for` must be INJECTIVE — the id it derives is the dedupe key AND the workflow instance id.

A collision is cross-tenant by construction. `create_ingest` answers a POST whose derived id already
has a record with `deduplicated: true` and no dispatch, and `GET /v1/ingests/{run_id}` resolves the
same id — so two projects sharing an id means one caller's ingest silently becomes a no-op against
another project's run, and either can read the other's status, error map and committed version.

The shipped derivation was `uuid5(NS, f"{project}-ingest-{key}")`, and a printable separator cannot
carry that: it occurs inside the fields it separates. `PROJECT_PATTERN` permits `-`
(`warehouse_registry.py:37`), `IngestRequest.project` declares no pattern at all, and the key half is
a caller-supplied HTTP header — so the collision is reachable rather than theoretical.
"""

import uuid

from ingest.runs import RUN_NAMESPACE, run_id_for


#: The pair that collided under the old `-ingest-` delimiter: both rendered `ra-ingest-batch-ingest-7`.
#: Data, not prose, so the regression stays executable.
COLLIDING_PAIR = (("ra", "batch-ingest-7"), ("ra-ingest-batch", "7"))


def _legacy(project: str, idempotency_key: str) -> str:
    """The pre-fix derivation, kept ONLY so the test can prove the bug was real.

    Without it the assertion below reads as "these two differ", which is true of almost any two
    inputs — it would pass against a derivation that never had the defect and gate nothing.
    """
    return str(uuid.uuid5(RUN_NAMESPACE, f"{project}-ingest-{idempotency_key}"))


def test_the_legacy_derivation_really_did_collide() -> None:
    """Pin the premise. If this stops holding, the pair below stopped exercising the bug."""
    first, second = COLLIDING_PAIR
    assert _legacy(*first) == _legacy(*second), "the chosen pair no longer collides under the old form — pick one that does"


def test_two_projects_never_share_a_run_id() -> None:
    """The regression: one project's POST must not resolve to another project's run."""
    first, second = COLLIDING_PAIR
    assert run_id_for(*first) != run_id_for(*second), "two projects derive one run id — the cross-tenant dedupe collision"


def test_the_derivation_is_still_deterministic() -> None:
    """The property the whole idempotency story rests on: same inputs, same id, on any pod."""
    assert run_id_for("ra", "batch-7") == run_id_for("ra", "batch-7")


def test_the_project_still_scopes_the_key() -> None:
    """Two tenants using the same obvious key must not meet."""
    assert run_id_for("ra", "1") != run_id_for("kb", "1")


def test_an_empty_field_is_not_absorbed() -> None:
    """Boundary: empty fields stay distinguishable rather than collapsing into one another."""
    assert run_id_for("", "a") != run_id_for("a", "")


def test_the_id_is_still_a_bare_uuid_string() -> None:
    """The shape is part of the contract — it is the Dapr instance id and appears in `Location`.
    Changing the derivation must not change the FORM of what it derives."""
    assert uuid.UUID(run_id_for("ra", "batch-7"))
