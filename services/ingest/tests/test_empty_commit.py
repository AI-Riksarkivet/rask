"""The branch no local test could reach: `finalize` against a catalog that HAS `commit`.

`finalize_run` branches on `hasattr(catalog, "commit")`. `LocalCatalog` — the default with
`RASK_INGEST_USE_CATALOG` unset, which is what every other test uses — does not have one, so the
entire catalog-service path is invisible to the suite. The chart deploys
`RASK_INGEST_USE_CATALOG: "true"`, so it is the ONLY path a deployed run takes.

What lived there: an empty fragment list was POSTed as `{"fragments": []}`, which the catalog refuses
with 400 "no fragments to commit" (`catalog/services/dataplane.py`). Deployed, that 400 raises out of
the `finalize` activity, burns its four ACTIVITY_RETRY attempts against a permanently-failing input,
and kills the workflow BEFORE `emit_terminal` — the run's own FAIL never reaches the lineage graph
and the START emitted at accept is orphaned forever.

Two ordinary paths arrive with no fragments: a source that enumerated zero units, and a run whose
every unit failed validation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest

from ingest import runtime
from ingest.runtime import BRONZE_SCHEMA, finalize_run
from ingest.workflow import RunSpec


class _CatalogWithCommit:
    """A catalog that HAS `commit` — i.e. the deployed `CatalogServiceClient` shape.

    `commit` RAISES exactly as the real one does when handed nothing, so the test fails loudly if the
    guard is removed rather than quietly asserting a value.
    """

    def __init__(self, uri: str) -> None:
        self._uri = uri
        self.commit_calls: list[list[str]] = []

    def ensure(self, project: str, dataset: str) -> str:
        return self._uri

    def describe_version(self, project: str, dataset: str) -> int:
        return lance.dataset(self._uri).version

    def commit(self, project: str, dataset: str, fragments: list[str], **kw: Any) -> tuple[int, int]:
        self.commit_calls.append(list(fragments))
        if not fragments:
            # The catalog's own refusal, reproduced: dataplane.py raises InvalidInputError, which the
            # client turns into a CatalogError. Any exception fails the activity identically.
            raise RuntimeError('catalog refused the commit (400): {"detail":"no fragments to commit"}')
        return 2, len(fragments)


def _bronze_batch() -> pa.Table:
    """One valid bronze row, built by the PLANE'S OWN row builder.

    This was a hand-rolled `pa.table({...}, schema=BRONZE_SCHEMA)` listing four columns, and it broke
    the moment the schema gained a fifth: #99 added `sha256` (payload fixity) and this copy did not
    follow, so the test failed with `KeyError: 'sha256'` from inside pyarrow — a test bug that reads
    exactly like a product failure, on a test whose whole subject is the commit path.

    Delegating to `units_to_table` means the fixture cannot drift from the schema again: whatever the
    plane writes in production is what this writes here, and a new column arrives in both at once.
    """
    from ingest.worker import units_to_table

    return units_to_table([("file:///a.tif", b"II*\x00fixture")])


@pytest.fixture
def dataset_uri(tmp_path: Path) -> str:
    uri = str(tmp_path / "bronze.lance")
    from ingest.lander import CREATION_FLAGS

    lance.write_dataset(BRONZE_SCHEMA.empty_table(), uri, mode="create", **CREATION_FLAGS)
    return uri


def test_an_empty_fragment_list_is_NOT_sent_to_the_catalog(dataset_uri: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard. Without it this raises, the activity exhausts its retries, and the workflow dies
    before it can emit its own terminal event."""
    catalog = _CatalogWithCommit(dataset_uri)
    spec = RunSpec(run_id="r-empty", project="p", dataset="d", kind="local-dir", options={})

    monkeypatch.setattr(runtime, "_catalog", lambda: catalog)
    outcome = finalize_run(spec, [], {})

    assert catalog.commit_calls == [], "an empty fragment list reached the catalog, which refuses it with 400"
    assert outcome["rows"] == 0


def test_it_reports_NO_committed_version_rather_than_the_previous_one(dataset_uri: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the defect, on the local path.

    `Lander.commit_fragments` returns the dataset's CURRENT version for an empty list — the previous
    run's, or the empty v1 that `ensure_dataset` created. Reporting that as `committed_version` tells
    an operator this run produced a version it did not write, and the API surfaced exactly that.
    """
    catalog = _CatalogWithCommit(dataset_uri)
    spec = RunSpec(run_id="r-empty-2", project="p", dataset="d", kind="local-dir", options={})

    monkeypatch.setattr(runtime, "_catalog", lambda: catalog)
    outcome = finalize_run(spec, [], {})

    assert outcome["committed_version"] is None
    assert outcome["published"] is None, "there is no version to gate, so nothing may be published"


def test_a_run_whose_every_unit_FAILED_still_reports_its_errors(dataset_uri: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The second ordinary path here, and the one that matters most for provenance.

    Before the guard, this run's 400 killed the workflow before `emit_terminal`, so its FAIL never
    reached the graph. The derivation itself is unchanged: `test_run_chain.py` pins
    COMPLETE_WITH_ERRORS for a partial run, and refusing a genuinely empty SOURCE is a separate
    decision at the enumeration seam.
    """
    catalog = _CatalogWithCommit(dataset_uri)
    spec = RunSpec(run_id="r-allfail", project="p", dataset="d", kind="local-dir", options={})

    monkeypatch.setattr(runtime, "_catalog", lambda: catalog)
    outcome = finalize_run(spec, [], {"file:///a.tif": "corrupt TIFF"})

    assert outcome["status"] == "COMPLETE_WITH_ERRORS"
    assert outcome["errors"] == {"file:///a.tif": "corrupt TIFF"}
    assert catalog.commit_calls == []


def test_a_NON_empty_commit_still_goes_to_the_catalog(dataset_uri: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must be surgical: the catalog is how a commit becomes rideable by the cascade.

    A guard that swallowed real fragments would land data locally and tell nothing downstream — the
    exact failure the catalog branch was added to prevent.
    """
    catalog = _CatalogWithCommit(dataset_uri)
    spec = RunSpec(run_id="r-real", project="p", dataset="d", kind="local-dir", options={})
    # Built through the plane's OWN row builder: `payload` is a blob_field — a struct of
    # uri/data/position/size — and a hand-rolled binary column is refused by Lance with a schema
    # mismatch, which is a test bug dressed as a product failure.
    # Through the lander's OWN writer and creation flags: blob-v2 needs data_storage_version 2.2,
    # and `write_fragments` is the one sanctioned way this plane produces a fragment.
    from ingest.lander import CREATION_FLAGS

    written = lance.fragment.write_fragments(_bronze_batch(), dataset_uri, **CREATION_FLAGS)

    monkeypatch.setattr(runtime, "_catalog", lambda: catalog)
    outcome = finalize_run(spec, [json.dumps(f.to_json()) for f in written], {})

    assert len(catalog.commit_calls) == 1, "a real fragment must still reach the catalog"
    assert outcome["committed_version"] == 2
