"""A retry of an already-committed run reported that nothing landed.

`finalize_run` purges the staged manifests immediately after committing. So a retry -- Dapr
re-executes an activity whose result was not recorded -- finds staging EMPTY and its carried fallback
empty too, and reaches the nothing-to-commit branch. That branch reported
`committed_version: None, rows: 0, publish_reason: "nothing to commit"` for a run whose rows were
sitting in the table.

The catalog CAN recognise its own earlier commit -- `_find_run_commit` scans for the run marker --
but the door was unreachable from this shape: `if not fragments: raise` sat above the marker check,
so an empty-list retry was refused 400 before the catalog ever looked.

Both halves are fixed: the catalog answers an empty commit that carries a known run_id with that
run's own `(version, rows)` and writes nothing, and this branch asks before asserting nothing landed.

The dev path is deliberately unchanged: `LocalCatalog` has no `commit` and no marker, so it still
reports None -- honestly, because it has no way to recognise its own earlier commit either.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from ingest.catalog import LocalCatalogSeam, ServiceCatalogSeam
from ingest.runtime import _prior_commit_for_run, finalize_run
from ingest.workflow import RunSpec


SPEC = RunSpec.model_validate({"run_id": "run-purged", "kind": "s3-prefix", "project": "acme", "dataset": "pages", "options": {}})


class _CatalogThatRemembers(ServiceCatalogSeam):
    """A catalog service client whose `commit` honours the run marker, as the real one now does.

    Declared as the seam half it stands for: the parameter names are the ones the runtime may pass by
    keyword, so a double that drifts from the real client is a type error rather than a live TypeError.
    """

    def __init__(self, answer: tuple[int, int] | None) -> None:
        self._answer = answer
        self.asked: list[tuple[str, int]] = []

    def ensure(self, namespace: str, dataset: str, external_base: str | None = None) -> str:
        """`finalize_run` resolves the table before anything else; not the subject here."""
        return "s3://wh/pages.lance"

    def commit(self, namespace: str, dataset: str, fragments_json: Sequence[str], read_version: int, run_id: str) -> tuple[int, int]:
        self.asked.append((run_id, len(fragments_json)))
        if self._answer is None:
            raise ValueError("no fragments to commit")
        return self._answer


def test_the_branch_ASKS_the_catalog_with_an_empty_list() -> None:
    """The shape matters: a post-purge replay has nothing to offer, so the question must be askable
    without fragments -- which is exactly what the catalog's guard order used to forbid."""
    catalog = _CatalogThatRemembers((9, 3))

    assert _prior_commit_for_run(catalog, SPEC) == (9, 3)
    assert catalog.asked == [("run-purged", 0)], f"the catalog was not asked, or was asked wrongly: {catalog.asked}"


def test_a_run_that_NEVER_committed_answers_None_rather_than_raising() -> None:
    """The catalog refuses an unknown run deliberately. 'I cannot tell' and 'it never committed' lead
    to the same honest report, and a status read must not raise into a terminal path."""
    assert _prior_commit_for_run(_CatalogThatRemembers(None), SPEC) is None


def test_a_catalog_with_NO_commit_answers_None() -> None:
    """LocalCatalog, the dev default. No marker, so no recognition -- and no crash."""

    class _Local(LocalCatalogSeam):
        pass

    assert _prior_commit_for_run(_Local(), SPEC) is None


@pytest.mark.parametrize(
    ("prior", "expected_version", "expected_rows", "expected_reason"),
    [
        pytest.param((9, 3), 9, 3, "already committed by this run", id="replay-of-a-committed-run"),
        pytest.param(None, None, 0, "nothing to commit", id="a-genuinely-empty-run"),
    ],
)
def test_the_terminal_record_DISTINGUISHES_the_two_empties(
    monkeypatch: pytest.MonkeyPatch,
    prior: tuple[int, int] | None,
    expected_version: int | None,
    expected_rows: int,
    expected_reason: str,
) -> None:
    """THE WEDGE, stated as what lineage ends up holding.

    Both cases arrive at this branch with an empty list, and before the fix both produced the same
    record -- so a run that committed was indistinguishable from one that wrote nothing, and the
    distinction was unrecoverable because the evidence had been purged.
    """
    from ingest import runtime as runtime_module

    # Patched at the SOURCE module: `finalize_run` imports the reader locally inside the function, so
    # binding it on `ingest.runtime` binds nothing the call will look at — the same local-import trap
    # `test_fanin_return_ceiling` records for `discover_staged`. The URI below names no real store;
    # the tier total is not this test's subject, only the terminal record derived beside it.
    monkeypatch.setattr("ingest.lander.rows_in_dataset", lambda _uri: 0)
    monkeypatch.setattr(runtime_module, "_catalog", lambda: _CatalogThatRemembers(prior))
    monkeypatch.setattr("ingest.staging.discover_staged", lambda *_a, **_k: [])
    monkeypatch.setattr("ingest.staging.purge_staged", lambda *_a, **_k: None)
    monkeypatch.setattr(runtime_module, "ensure_dataset_at", lambda _s: ("s3://wh/pages.lance", 0))

    out = finalize_run(SPEC, [], {}, read_version=1)

    assert out["committed_version"] == expected_version
    assert out["rows"] == expected_rows
    assert out["publish_reason"] == expected_reason
