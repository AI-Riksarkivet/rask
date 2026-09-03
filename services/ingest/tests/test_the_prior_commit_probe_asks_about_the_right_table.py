"""The "did this run already commit?" probe must name the NAMESPACE, not the project.

`RunSpec.namespace` exists because "every consumer that needed a namespace was handed `spec.project`
instead", and it calls itself THE ONE PLACE a project becomes a namespace. `_prior_commit_for_run` was
still a consumer that had not been converted: it asked the catalog about `acme$vendproof` where the
table is `acme-bronze$vendproof`.

MEASURED in-cluster 2026-09-03 — the deployed catalog answered

    POST /v1/table/acme$vendproof/commit  403 Forbidden

because nobody holds any relation on a table that does not exist. And the probe swallows every
exception by design ("I cannot tell" and "it never committed" lead to the same honest report), so the
403 was invisible: the probe answered `None` for every run in every project whose namespace differs
from its project name, which under `naming.py` is all of them.

WHAT THAT COSTS is precisely what the probe was added to prevent. It is asked on the no-fragments
path, where an empty fragment list has two very different causes — a run that genuinely wrote nothing,
and a RETRY of a run that already committed and then purged its staging. Always answering `None`
collapses the second into the first: the retry reports `committed_version: null, rows: 0` for rows
that DID land, which is false lineage for work that succeeded and unrecoverable, because the evidence
it would have needed was the staging it purged.
"""

from __future__ import annotations

from collections.abc import Sequence

from ingest.runtime import _prior_commit_for_run
from ingest.workflow import RunSpec
from service_kit.lakehouse.vended_credentials import VendedCredential


class _RecordingCatalog:
    """Answers like the deployed catalog: the run marker, but only for the table that exists.

    It implements the WHOLE ``ServiceCatalogSeam``, not just ``commit``, because that is what the seam
    gate demands of anything handed to a function typed against it — the double has to be the shape
    the deployed client is, or the test proves nothing about the deployed path.
    """

    def __init__(self, real_table: str) -> None:
        self._real = real_table
        self.asked: list[str] = []

    def ensure(self, namespace: str, dataset: str, external_base: str | None = None) -> str:
        return f"s3://bucket/{namespace}${dataset}"

    def commit(self, namespace: str, dataset: str, fragments_json: Sequence[str], read_version: int, run_id: str) -> tuple[int, int]:
        asked = f"{namespace}${dataset}"
        self.asked.append(asked)
        if asked != self._real:
            raise RuntimeError(f"403 Forbidden: no relation on table:{asked}")
        return (7, 4200)

    def publish(self, namespace: str, dataset: str, version: int, *, key_column: str = "id", required_columns: Sequence[str] = ()) -> dict[str, object]:
        return {}

    def describe_version(self, namespace: str, dataset: str) -> int:
        return 0

    def vend_storage_options(self, namespace: str, dataset: str, *, tier: str = "write") -> VendedCredential | None:
        return None


def _spec() -> RunSpec:
    return RunSpec(run_id="r1", kind="s3-prefix", project="acme", dataset="vendproof")


def test_the_probe_asks_the_catalog_about_the_bronze_namespace() -> None:
    catalog = _RecordingCatalog("acme-bronze$vendproof")
    _prior_commit_for_run(catalog, _spec())
    assert catalog.asked == ["acme-bronze$vendproof"], f"probed the wrong table: {catalog.asked}"


def test_a_run_that_already_committed_is_recognised() -> None:
    """The whole point: the retry must find its own commit rather than report it landed nothing."""
    catalog = _RecordingCatalog("acme-bronze$vendproof")
    assert _prior_commit_for_run(catalog, _spec()) == (7, 4200)


def test_a_run_that_never_committed_still_answers_none() -> None:
    """Unchanged. A refusal for the RIGHT table is a real answer — this run did not commit."""
    catalog = _RecordingCatalog("some-other$table")
    assert _prior_commit_for_run(catalog, _spec()) is None
