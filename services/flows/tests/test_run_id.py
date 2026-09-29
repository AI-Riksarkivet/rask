"""`run_id_for` must be INJECTIVE, because the id it derives is three things at once.

The run id is the Dapr workflow INSTANCE id, the dedupe key, and the URL of a readable resource
(`GET /flows/runs/{run_id}`). A collision is therefore not a hash curiosity: two callers land on one
workflow instance, one caller's POST is answered as a duplicate of the other's, and either can read
the other's node outputs and error text at a URL they now share.

The shipped derivation was `uuid5(NS, f"{subject}-flows-{key}")`, and a printable separator cannot
carry that weight — it occurs inside the fields it separates. `idempotency_key` is a caller-supplied
HTTP header and `subject` is a token claim, so a single crafted subject is enough to sit on someone
else's run.
"""

from flows.routes import run_id_for


#: The exact pair that collided under the old `-flows-` delimiter: both rendered the joined string
#: `alice-flows-b-flows-c`. Kept as data rather than prose so the regression is executable.
COLLIDING_PAIR = (("alice", "b-flows-c"), ("alice-flows-b", "c"))


def test_distinct_pairs_never_share_a_run_id() -> None:
    """The regression: the delimiter must not be forgeable out of the fields it separates."""
    first, second = COLLIDING_PAIR
    assert run_id_for(*first) != run_id_for(*second), "two callers derive one workflow instance id — the cross-tenant collision"


def test_the_subject_still_scopes_the_key() -> None:
    """Two callers using the SAME obvious key must not meet — the property the subject scope exists for."""
    assert run_id_for("alice", "1") != run_id_for("bob", "1")


def test_an_empty_field_is_not_absorbed() -> None:
    """Boundary: empty fields must stay distinguishable rather than collapsing into one another.

    `("", "a")` and `("a", "")` are different callers with different keys; under a delimiter that can
    be empty-adjacent they are the classic second collision, one boundary case past the first.
    """
    assert run_id_for("", "a") != run_id_for("a", "")
