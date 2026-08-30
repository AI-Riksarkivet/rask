"""A caller can ask the gate's VERDICT without moving the tag.

The medallion cascade cannot otherwise have both of two properties it needs. Under
`cascadeViaPublish` the publish IS the promotion — the catalog runs the assertions and its tag move
wakes the next stage — so:

* deciding the promotion review BEFORE publishing makes the band able to hold, but the catalog's
  verdict never reaches the review, and a hold that cannot name its assertions cannot tell a corrupt
  finding from a reviewable one;
* publishing first preserves that verdict but the tag has already moved, so a band breach can no
  longer withhold anything.

Both hold once the verdict is separable from the promotion. `gate_only` runs the identical assertions
against the identical version and returns them with the tag UNTOUCHED, so a caller can ask "would this
pass, and is it unusual?" and only then decide to publish.

It is a QUESTION, never a write: `published` is false on this path whatever the assertions say, and
nothing about the dataset changes. That is what makes it safe to call speculatively.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from catalog.services import publication


lance = pytest.importorskip("lance")


@pytest.fixture
def dataset(tmp_path):
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(pa.table({"id": [1, 2, 3], "v": ["a", "b", "c"]}), uri)
    return uri


def _tags(uri: str) -> dict[str, object]:
    return dict(lance.dataset(uri).tags.list())


def test_gate_only_reports_the_verdict(dataset: str) -> None:
    result = publication.gate(dataset, key_column="id", required_columns=(), version=1)
    assert result.assertions, "the verdict is the whole point of the call"


def test_gate_only_never_moves_the_tag(dataset: str) -> None:
    """The property that makes it safe to ask speculatively."""
    before = _tags(dataset)
    publication.gate(dataset, key_column="id", required_columns=(), version=1)
    assert _tags(dataset) == before, "gate_only advanced `published` — it is a question, not a write"


def test_gate_only_is_never_published(dataset: str) -> None:
    """Even on a clean dataset: passing the gate is not the same act as promoting."""
    result = publication.gate(dataset, key_column="id", required_columns=(), version=1)
    assert result.published is False


def test_a_failing_assertion_is_reported_by_name(dataset: str) -> None:
    """What the review needs: a corrupt finding is distinguishable from a reviewable one.

    The name is from a FIXED vocabulary (`column_declared`), not per-column — deliberately, because
    `resolve_review_policy` keys its structural set on these exact strings, so a name that varied with
    the data would stop the review being able to tell corruption from an unusual delta.
    """
    result = publication.gate(dataset, key_column="id", required_columns=("absent_column",), version=1)
    failed = [a.assertion for a in result.assertions if not a.success]
    assert failed == ["column_declared"], failed
    assert result.reason and "column_declared" in result.reason, result.reason
