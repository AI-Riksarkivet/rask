"""#30 — an empty source is a SUCCESS with zero rows, not a failure.

The ruling, and it is a product decision rather than a technical one: an empty folder or prefix is a
legitimate state of the world. A scheduled ETL over a quiet source hits it routinely, and reporting
that as FAILED would alert every time nothing happened to arrive — which is how an alert stops being
read. "0 rows" is the honest answer and it is queryable; "failed" is neither.

The second half matters as much: it must leave NO Lance version behind. A version that adds nothing
is a row in the history that an operator has to explain later, and `Restore`/time-travel has to step
over.
"""

from __future__ import annotations

import lance
import pyarrow as pa
import pytest
from ingest.lander import Lander
from ingest.runtime import BRONZE_SCHEMA


class _Catalog:
    def __init__(self, uri: str) -> None:
        self._uri = uri
        self.registered: list[tuple[str, int, str]] = []

    def ensure_dataset(self, project: str, dataset: str, schema: pa.Schema) -> str:
        return self._uri

    def register_version(self, dataset_uri: str, version: int, run_id: str) -> None:
        self.registered.append((dataset_uri, version, run_id))


@pytest.fixture
def dataset_uri(tmp_path) -> str:  # type: ignore[no-untyped-def]
    uri = str(tmp_path / "bronze.lance")
    lance.write_dataset(BRONZE_SCHEMA.empty_table(), uri, data_storage_version="2.2", enable_stable_row_ids=True)
    return uri


def test_an_empty_run_commits_NO_new_version(dataset_uri: str) -> None:
    """The half that would otherwise pollute the history.

    A version that adds nothing is a row an operator has to explain later and that time-travel has to
    step over. `commit_fragments` returns the CURRENT version rather than minting one.
    """
    before = lance.dataset(dataset_uri).version
    catalog = _Catalog(dataset_uri)

    result = Lander(catalog).commit_fragments(dataset_uri, [], run_id="run-empty")

    assert lance.dataset(dataset_uri).version == before, "an empty run minted a Lance version"
    assert result.version == before
    assert result.rows == 0
    assert catalog.registered == [], "an empty run registered a version with the catalog"


def test_the_workflow_SHORT_CIRCUITS_rather_than_relying_on_when_all_of_nothing() -> None:
    """Structural, and the reason is that the alternative rests on an undocumented guarantee.

    Without this branch an empty enumeration falls through to `wf.when_all([])`, whose behaviour on an
    empty list is not a documented promise of the runtime. The run would then be resting on an
    implementation detail of the fan-in — working today, unexplained tomorrow. Returning early makes
    the empty case something the workflow STATES rather than something it survives.
    """
    import inspect

    from ingest.workflow import ingest_run

    src = inspect.getsource(ingest_run)
    empty_at = src.index("units_total == 0")
    fanout_at = src.index("wf.when_all(")

    assert empty_at < fanout_at, "the empty check must run BEFORE the fan-out, not inside or after it"


def test_an_empty_run_reports_COMPLETE_not_FAILED() -> None:
    """The product decision, pinned so it is not quietly reversed into a refusal.

    There is a ceiling that refuses a run for having TOO MANY units, and the symmetry is tempting —
    but the cases are opposite. A mis-pointed source enumerating a whole bucket is a mistake worth
    stopping; a source with nothing in it is Tuesday.
    """
    import inspect

    from ingest.workflow import ingest_run

    src = inspect.getsource(ingest_run)
    branch = src[src.index("units_total == 0") : src.index("wf.when_all(")]

    assert 'status="COMPLETE"' in branch, "an empty source must complete, not fail"
    assert "rows=0" in branch


def test_the_empty_branch_still_emits_a_TERMINAL(dataset_uri: str) -> None:
    """A run that ends must leave a record whichever way it ends — including the boring way.

    Skipping the terminal for the empty case would make "nothing to do" indistinguishable from "never
    ran" in the graph, which is the exact defect the FAIL branch was added to close.
    """
    import inspect

    from ingest.workflow import ingest_run

    src = inspect.getsource(ingest_run)
    branch = src[src.index("units_total == 0") : src.index("wf.when_all(")]

    assert "emit_terminal" in branch, "an empty run emitted no terminal — it is indistinguishable from a run that never started"
