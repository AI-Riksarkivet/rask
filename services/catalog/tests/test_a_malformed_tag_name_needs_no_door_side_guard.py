"""A malformed TAG name already answers the spec's 13, so the tag door needs no name guard.

[[LH-096]] asks for tag names to be "refused at the door like branch names". Measured on pylance
11.0.0, they must not be: every case `refuse_a_branch_name_the_backend_cannot_use` exists for is
BRANCH-specific, and the tag door is already correct without it.

* ``main`` is the default BRANCH, so a branch create cannot see it in `branches.list()` and pylance
  answers with bug-report text. It is an ordinary tag name and creating it succeeds.
* An empty ref answers ``Ref is invalid: Ref cannot be empty`` for a tag, where a branch reaches
  pylance's bug-report text instead.
* A traversal segment cannot reach the object-store path parser, because a tag rejects ``/`` outright
  (``feature/x`` is a legal BRANCH and an illegal tag) — so it is refused as an invalid character.

All three therefore carry `_classify_ref_error`'s ``Ref is invalid:`` marker and map to InvalidInput
without help. Adding a guard would do what that function's own docstring warns against: keep a second
copy of a grammar Lance owns, in the place nobody re-tests against a new pylance.

THIS IS A TRIPWIRE, NOT A TIDY-UP. It holds the measurement the decision rests on, so a pylance that
stops mapping one of these reds HERE — which is the day a door-side guard becomes the right answer.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest
from lance_namespace import CreateTableTagRequest, connect

from catalog.services.dataplane import create_table, create_tag


lance = pytest.importorskip("lance")

TABLE_ID = ["rows"]
SCHEMA = pa.schema([pa.field("id", pa.int64())])


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace is runtime-only
    namespace = connect("dir", {"root": str(tmp_path / "data")})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, SCHEMA) as writer:
        writer.write_table(pa.table({"id": pa.array([1], pa.int64())}, schema=SCHEMA))
    create_table(namespace, {}, TABLE_ID, sink.getvalue().to_pybytes(), mode="create")
    return namespace


def _create(ns, tag: str) -> int | None:  # noqa: ANN001
    """Create the tag and return the spec code it answered, or None if it succeeded."""
    try:
        create_tag(ns, {}, CreateTableTagRequest(id=TABLE_ID, tag=tag, version=1))
    except Exception as exc:  # noqa: BLE001 — the CODE is the subject
        return getattr(exc, "code", None)
    return None


@pytest.mark.parametrize(
    ("shape", "tag"),
    [
        ("empty", ""),
        ("whitespace only", "   "),
        ("a path-traversal segment", "a/../b"),
        ("a separator, which a branch allows and a tag does not", "feature/x"),
        ("an invalid character", "has space"),
    ],
)
def test_a_malformed_tag_name_answers_invalid_input(ns, shape: str, tag: str) -> None:  # noqa: ANN001
    assert _create(ns, tag) == 13, f"{shape}: answered something other than InvalidInput, so the door now needs its own guard"


@pytest.mark.parametrize("tag", ["main", "dot.name", "v1"])
def test_a_legal_tag_name_is_accepted(ns, tag: str) -> None:  # noqa: ANN001
    """`main` is the headline: it is reserved for BRANCHES only, and a guard copied across would break it."""
    assert _create(ns, tag) is None
