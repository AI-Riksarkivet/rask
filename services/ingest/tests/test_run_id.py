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

from ingest.runs import run_id_for


#: The pair that collided under the old `-ingest-` delimiter: both rendered `ra-ingest-batch-ingest-7`.
#: Data, not prose, so the regression stays executable.
COLLIDING_PAIR = (("ra", "batch-ingest-7"), ("ra-ingest-batch", "7"))


def test_two_projects_never_share_a_run_id() -> None:
    """The regression: one project's POST must not resolve to another project's run."""
    first, second = COLLIDING_PAIR
    assert run_id_for(*first) != run_id_for(*second), "two projects derive one run id — the cross-tenant dedupe collision"
